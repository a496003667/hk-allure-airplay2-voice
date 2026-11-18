#!/usr/bin/env python3
"""Exercise Bluetooth ownership and watchdog failure with fake audio controls."""
import os
import pathlib
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
BRIDGE = REPO / 'airplay2/hk-bt-bridge.sh'
WATCHDOG = REPO / 'airplay2/runtime/hk-ap2-safe-test'


def eventually(predicate, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError('condition not reached before timeout')


class BluetoothBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.root = self.base / 'opt'
        self.run = self.base / 'run'
        self.safe = self.run / 'safe'
        self.proc = self.base / 'proc'
        for p in (self.root / 'bin', self.safe, self.proc / '42'):
            p.mkdir(parents=True)
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.bind(str(self.base / 'player.sock'))
        (self.proc / '42/comm').write_text('a01controller\n')
        self.pcm = self.base / 'pcm'
        self.pcm.write_text('state: RUNNING\nowner_pid   : 42\n')
        self.status = self.base / 'status.json'
        self.state('stop')
        (self.safe / 'armed').touch()
        self.executable(self.root / 'bin/hk-unix-request-raw', '''#!/bin/sh
if [ -n "${MOCK_QUERY_LOG:-}" ] && [ "$2" = getPlayerStatus ]; then
  if [ "${MOCK_QUERY_DETAILS:-0}" = 1 ]; then
    printf 'query ' >>"$MOCK_QUERY_LOG"
    cat "$MOCK_STATUS" >>"$MOCK_QUERY_LOG"
    printf '\\n' >>"$MOCK_QUERY_LOG"
  else
    echo query >>"$MOCK_QUERY_LOG"
  fi
fi
if [ "$2" = getPlayerStatus ] && [ -n "${MOCK_STATUS_FAIL:-}" ] && [ -f "$MOCK_STATUS_FAIL" ]; then exit 1; fi
if [ "$2" = setPlayerCmd:vol:30 ]; then
  echo clamp >>"$MOCK_LOG"
  sed 's/"vol":"52"/"vol":"30"/' "$MOCK_STATUS" >"$MOCK_STATUS.tmp" && mv "$MOCK_STATUS.tmp" "$MOCK_STATUS"
elif [ "$2" = setPlayerCmd:onepause ]; then
  echo onepause >>"$MOCK_LOG"
  sed 's/"status":"play"/"status":"stop"/' "$MOCK_STATUS" >"$MOCK_STATUS.tmp" && mv "$MOCK_STATUS.tmp" "$MOCK_STATUS"
elif [ "$2" = setPlayerCmd:resume ]; then
  echo resume >>"$MOCK_LOG"
  sed 's/"status":"stop"/"status":"play"/' "$MOCK_STATUS" >"$MOCK_STATUS.tmp" && mv "$MOCK_STATUS.tmp" "$MOCK_STATUS"
  printf 'state: RUNNING\\nowner_pid   : 42\\n' >"$MOCK_PCM"
elif [ "$2" = activity ]; then
  if [ -n "${MOCK_ACTIVITY_QUERY_LOG:-}" ]; then echo activity >>"$MOCK_ACTIVITY_QUERY_LOG"; fi
  cat "$MOCK_ACTIVITY"
else
  cat "$MOCK_STATUS"
fi
''')
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
if [ "$1" = start ]; then
  echo "$HK_AP2_MASTER_SAFE" >"$MOCK_MASTER"
  echo "$HK_AP2_DIGITAL_SAFE,$HK_AP2_PLAYBACK_SAFE" >"$MOCK_LEVELS"
  SAFE=$HK_AP2_RUN/safe; RUN=$HK_AP2_RUN
  . "$HK_AUDIO_COMMON"
  HK_AP2_BT=1
  audio_reserve || exit 1
  lease_phase=active
  audio_write_lease || exit 1
else
  rm -f "$HK_AP2_RUN/safe/play.started" "$HK_AP2_RUN/safe/lease"
  if [ -z "${HK_AP2_BT:-}" ]; then rm -f "$HK_AP2_RUN/ap2-claim"; fi
fi
''')
        self.executable(self.root / 'bin/hk-ap2-safe-test', '''#!/bin/sh
echo mute >>"$MOCK_LOG"
echo manual >"$HK_AP2_RUN/safe/tripped"
''')
        self.log = self.base / 'events'
        self.log.touch()
        self.env = os.environ | {
            'HK_AP2_ROOT': str(self.root), 'HK_AP2_RUN': str(self.run),
            'HK_BT_PCM_STATUS': str(self.pcm), 'HK_BT_PROC_ROOT': str(self.proc),
            'HK_BT_PLAYER_SOCKET': str(self.base / 'player.sock'),
            'MOCK_STATUS': str(self.status), 'MOCK_LOG': str(self.log),
            'MOCK_PCM': str(self.pcm),
            'MOCK_MASTER': str(self.base / 'master'),
            'MOCK_LEVELS': str(self.base / 'levels'),
            'HK_BT_STALL_SECONDS': '0',
            'HK_BT_STALL_COOLDOWN': '0',
        }
        (self.base / 'clock').write_text('0.00 0.00\n')
        self.env['HK_AUDIO_UPTIME_FILE'] = str(self.base / 'clock')
        self.env['HK_AUDIO_COMMON'] = str(REPO / 'airplay2/hk-audio-common.sh')
        self.env['HK_AP2_PROC_ROOT'] = str(self.proc)
        self.env['HK_AP2_PCM_STATUS'] = str(self.pcm)
        self.children = []
        self.extra_pids = []
        self.dummy_watchdog = self.launch(['sleep', '60'])
        (self.safe / 'watchdog.pid').write_text(str(self.dummy_watchdog.pid) + '\n')
        (self.safe / 'watchdog.heartbeat').write_text('0\n')

    def tearDown(self):
        (self.safe / 'armed').unlink(missing_ok=True)
        for proc in reversed(self.children):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        for pid in reversed(self.extra_pids):
            if _alive(pid):
                os.kill(pid, signal.SIGTERM)
            deadline = time.monotonic() + 4
            while _alive(pid) and time.monotonic() < deadline:
                time.sleep(.02)
            if _alive(pid):
                os.kill(pid, signal.SIGKILL)
        self.socket.close()
        if hasattr(self, 'activity_socket'): self.activity_socket.close()
        self.tmp.cleanup()

    def executable(self, path, text):
        path.write_text(text)
        path.chmod(0o755)

    def state(self, status):
        temporary = self.status.with_name(self.status.name + '.tmp')
        temporary.write_text(
            '{"mode":"41","status":"%s","vol":"12","mute":"0"}' % status
        )
        temporary.replace(self.status)

    def launch(self, command, env=None):
        p = subprocess.Popen(command, env=env or self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(p)
        return p

    def events(self):
        return self.log.read_text().splitlines()

    def reserve_bluetooth(self, token=None):
        shell = '. "$HK_AUDIO_COMMON"; SAFE=$HK_AP2_RUN/safe; RUN=$HK_AP2_RUN; HK_AP2_BT=1; audio_reserve'
        env = self.env.copy()
        if token is not None:
            env['HK_AUDIO_TOKEN'] = token
        result = subprocess.run(['sh', '-c', shell], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def launch_validation_watchdog(self):
        mixer = self.base / 'validation-amixer'
        self.executable(mixer, '''#!/bin/sh
echo "$*" >>"$MOCK_MIXER_LOG"
case "$*" in
  *"sget Master"*) echo 'Front Left: 4';;
  *"cget numid=1"*) echo '  : values=100,100';;
  *"cget numid=2"*) echo '  : values=1,1';;
esac
''')
        mixer_log = self.base / 'validation-mixer.log'
        mixer_log.touch()
        env = self.env | {
            'HK_AP2_AMIXER': str(mixer), 'MOCK_MIXER_LOG': str(mixer_log),
            'HK_AP2_MAX_PLAY_SECONDS': '0', 'HK_AP2_PLAYBACK_SAFE': '1',
            'HK_AP2_BT_PLAYBACK_SAFE': '1',
        }
        return self.launch(['sh', str(WATCHDOG), '__watchdog'], env), mixer_log

    def test_claim_release_and_airplay_handoff(self):
        bridge = self.launch(['sh', str(BRIDGE), '__run'])
        time.sleep(2)
        self.assertNotIn('start', self.events(), 'stopped Bluetooth must not open the speaker')
        self.state('play')
        eventually(lambda: self.events().count('start') == 1)
        self.assertTrue((self.run / 'bt-owner.pid').exists())
        self.state('stop')
        eventually(lambda: 'stop' in self.events() and not (self.run / 'bt-owner.pid').exists())
        (self.run / 'voice-owns').write_text('0\n')
        self.state('play')
        time.sleep(2)
        self.assertEqual(self.events().count('start'), 1, 'voice ownership blocks Bluetooth')
        (self.run / 'voice-owns').unlink()
        eventually(lambda: self.events().count('start') == 2)
        (self.run / 'ap2-claim').write_text('0\n')
        (self.run / 'bt-release').touch()
        eventually(lambda: self.events().count('stop') == 2 and not (self.run / 'bt-owner.pid').exists())
        time.sleep(2)
        self.assertEqual(self.events().count('start'), 2, 'AirPlay claim blocks reacquisition')
        self.assertIsNone(bridge.poll())

    def test_bluetooth_release_parks_the_hardware_slave_before_reacquisition(self):
        self.launch(['sh', str(BRIDGE), '__run'])
        self.state('play')
        eventually(lambda: self.events().count('start') == 1)
        eventually(lambda: (self.safe / 'lease').exists())
        token = (self.safe / 'lease').read_text().split()[3]
        self.state('stop')
        eventually(lambda: self.events().count('park') == 1)
        self.assertFalse((self.run / 'bt-owner.pid').exists())
        eventually(lambda: not (self.run / 'bt-heartbeat').exists())
        self.assertEqual((self.run / 'bt-wait-pcm').read_text().strip(), token)
        self.assertEqual(self.events().count('start'), 1)

    def test_stale_airplay_barrier_recovers_a_prepared_bluetooth_clock(self):
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
if [ "$1" = park ]; then
  printf 'closed\\n' >"$MOCK_PCM"
elif [ "$1" = start ]; then
  echo "$HK_AP2_MASTER_SAFE" >"$MOCK_MASTER"
  echo "$HK_AP2_DIGITAL_SAFE,$HK_AP2_PLAYBACK_SAFE" >"$MOCK_LEVELS"
  SAFE=$HK_AP2_RUN/safe; RUN=$HK_AP2_RUN
  . "$HK_AUDIO_COMMON"
  HK_AP2_BT=1
  audio_reserve || exit 1
  lease_phase=active
  audio_write_lease || exit 1
else
  rm -f "$HK_AP2_RUN/safe/play.started" "$HK_AP2_RUN/safe/lease"
  if [ -z "${HK_AP2_BT:-}" ]; then rm -f "$HK_AP2_RUN/ap2-claim"; fi
fi
''')
        self.seed_airplay()
        for name in ('safe/lease', 'safe/play.started', 'ap2-claim'):
            (self.run / name).unlink(missing_ok=True)
        self.pcm.write_text('state: PREPARED\nowner_pid   : 42\n')
        (self.run / 'bt-wait-pcm').write_text('airplay-stale\n')
        self.state('play')
        self.env.update({
            # This case tests evidence-driven reacquisition after the orphaned
            # PREPARED clock is parked, not the fallback transport rebuild.
            'HK_BT_STALL_REPAIR': '0',
            'HK_BT_DEBUG_HANDOFF': '1',
        })
        self.launch(['sh', str(BRIDGE), '__run'])
        self.advance_clock(1)
        self.set_activity(100, 1)
        eventually(lambda: (self.run / 'bt-wait-pcm').exists() is False)
        eventually(lambda: self.pcm.read_text().strip() == 'closed')
        self.assertIn('park', self.events())
        bridge_log = self.run / 'bt-bridge.log'
        eventually(
            lambda: 'paused-PCM evidence started frames=100'
            in bridge_log.read_text()
        )
        self.set_activity(356, 2)
        self.advance_clock(2)
        # Wait until the bridge has consumed a fresh frame. The stock status
        # query happens before the activity read, so polling its count under
        # load can let a later sample overwrite the first.
        try:
            eventually(
                lambda: 'paused-PCM evidence samples=1'
                in bridge_log.read_text()
            )
        except AssertionError as error:
            raise AssertionError(f'{error}\n{bridge_log.read_text()}') from error
        # The retained virtual handle now reopens its physical slave. Output is
        # still closed until the bridge reserves its lease and the shared hook
        # establishes the muted amplifier baseline.
        self.pcm.write_text('state: RUNNING\nowner_pid   : 42\n')
        eventually(lambda: self.events().count('start') == 1)
        self.assertFalse((self.safe / 'tripped').exists())

    def test_orphaned_stopped_bluetooth_dsp_clock_is_parked_without_a_token(self):
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
if [ "$1" = park ]; then
  printf 'closed\\n' >"$MOCK_PCM"
elif [ "$1" = start ]; then
  SAFE=$HK_AP2_RUN/safe; RUN=$HK_AP2_RUN
  . "$HK_AUDIO_COMMON"
  HK_AP2_BT=1
  audio_reserve || exit 1
  lease_phase=active
  audio_write_lease || exit 1
fi
''')
        self.pcm.write_text('state: PREPARED\nowner_pid   : 42\n')
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: 'park' in self.events())
        eventually(lambda: self.pcm.read_text().strip() == 'closed')
        time.sleep(1)
        self.assertNotIn('start', self.events())
        self.assertFalse((self.run / 'bt-heartbeat').exists())
        self.assertFalse((self.run / 'bt-owner.pid').exists())
        self.assertIn(
            'released orphaned Bluetooth DSP clock',
            (self.run / 'bt-bridge.log').read_text(),
        )

    def test_orphaned_clock_uses_surviving_holder_proof_after_opener_thread_exits(self):
        holder_pid = self.dummy_watchdog.pid
        holder = self.proc / str(holder_pid)
        (holder / 'fd').mkdir(parents=True)
        (holder / 'comm').write_text('a01controller\n')
        (holder / 'fd/30').symlink_to('/dev/snd/pcmC0D0p')
        start = subprocess.run(
            ['sh', '-c', '. "$HK_AUDIO_COMMON"; audio_pid_start "$1"',
             'sh', str(holder_pid)],
            env=self.env, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertTrue(start)
        (self.base / 'bt-pcm-owner').write_text(
            f'{holder_pid} {start} 30 987654321\n'
        )
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
if [ "$1" = park ]; then
  printf 'closed\\n' >"$MOCK_PCM"
fi
''')
        # owner_pid is the now-dead opener thread; only the fd proof identifies
        # the surviving a01controller process that still owns the clock.
        self.pcm.write_text('state: PREPARED\nowner_pid   : 42\n')
        self.launch(['sh', str(BRIDGE), '__run'])
        bridge_log = self.run / 'bt-bridge.log'
        eventually(
            lambda: self.pcm.read_text().strip() == 'closed' and
            'released orphaned Bluetooth DSP clock' in bridge_log.read_text()
        )
        self.assertIn('park', self.events())
        self.assertFalse((self.run / 'bt-owner.pid').exists())
        self.assertFalse((self.run / 'bt-heartbeat').exists())

    def test_orphan_reaper_never_parks_a_running_bluetooth_clock(self):
        self.pcm.write_text('state: RUNNING\nowner_pid   : 42\n')
        self.launch(['sh', str(BRIDGE), '__run'])
        time.sleep(2)
        self.assertNotIn('park', self.events())
        self.assertNotIn('start', self.events())
        self.assertTrue(self.pcm.read_text().startswith('state: RUNNING'))

    def test_orphan_reaper_respects_every_ownership_and_fault_gate(self):
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
if [ "$1" = park ]; then
  printf 'closed\\n' >"$MOCK_PCM"
fi
''')
        self.env['HK_AP2_HOLD_MUTE_FILE'] = str(self.base / 'hold-mute')
        definitions = BRIDGE.read_text().split('trap cleanup EXIT', 1)[0]
        pid = self.dummy_watchdog.pid
        start = subprocess.run(
            ['sh', '-c', '. "$HK_AUDIO_COMMON"; audio_pid_start "$1"',
             'sh', str(pid)],
            env=self.env, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertTrue(start)

        def run_case(name, setup, should_park):
            for marker in (
                'safe/lease', 'safe/play.started', 'safe/tripped',
                'ap2-claim', 'bt-owner.pid', 'bt-heartbeat',
                'bt-wait-pcm', 'voice-owns',
            ):
                (self.run / marker).unlink(missing_ok=True)
            (self.base / 'hold-mute').unlink(missing_ok=True)
            (self.base / 'clock').write_text('0.00 0.00\n')
            (self.safe / 'watchdog.heartbeat').write_text('0\n')
            self.log.write_text('')
            self.pcm.write_text('state: PREPARED\nowner_pid   : 42\n')
            setup()
            result = subprocess.run(
                ['sh', '-c', definitions + '\nreap_orphaned_bluetooth_slave\n'],
                env=self.env, capture_output=True, text=True, timeout=8,
            )
            self.assertEqual(result.returncode, 0, f'{name}: {result.stderr}')
            self.assertEqual(
                'park' in self.events(), should_park,
                f'{name}: {"expected" if should_park else "must not"} park',
            )
            self.assertEqual(
                self.pcm.read_text().strip(),
                'closed' if should_park else 'state: PREPARED\nowner_pid   : 42',
            )

        cases = (
            ('baseline', lambda: None, True),
            ('maintenance hold',
             lambda: (self.base / 'hold-mute').touch(), False),
            ('fault trip',
             lambda: (self.safe / 'tripped').write_text('manual mute\n'), False),
            ('voice ownership',
             lambda: (self.run / 'voice-owns').write_text('0\n'), False),
            ('AirPlay claim',
             lambda: (self.run / 'ap2-claim').write_text(
                 f'0 {pid} {start} test-claim\n'), False),
            ('AirPlay lease',
             lambda: (self.safe / 'lease').write_text(
                 f'airplay {pid} {start} test-lease 0 active\n'), False),
            ('AirPlay play marker',
             lambda: (self.safe / 'play.started').write_text('0\n'), False),
            ('Bluetooth owner',
             lambda: (self.run / 'bt-owner.pid').write_text(
                 f'{pid}\n'), False),
            ('Bluetooth pause barrier',
             lambda: (self.run / 'bt-wait-pcm').write_text(
                 'bt-pause\n'), False),
            ('stale watchdog',
             lambda: ((self.base / 'clock').write_text('9.00 0.00\n'),
                      (self.safe / 'watchdog.heartbeat').write_text('0\n')),
             False),
        )
        for name, setup, should_park in cases:
            with self.subTest(name=name):
                run_case(name, setup, should_park)

    def test_active_bridge_reaps_an_expired_airplay_claim(self):
        self.launch(['sh', str(BRIDGE), '__run'])
        self.state('play')
        eventually(lambda: self.events().count('start') == 1)
        (self.run / 'ap2-claim').write_text('0\n')
        self.advance_clock(10)
        eventually(lambda: not (self.run / 'ap2-claim').exists())
        self.assertTrue((self.run / 'bt-owner.pid').exists())
        self.assertEqual(self.events().count('stop'), 0)

    def test_repeated_bt_airplay_bt_transitions_leave_no_barrier(self):
        self.seed_airplay()
        for name in ('safe/lease', 'safe/play.started', 'ap2-claim'):
            (self.run / name).unlink(missing_ok=True)
        self.pcm.write_text('state: RUNNING\nowner_pid   : 42\n')
        queries = self.base / 'sequence-activity-queries'
        self.env['MOCK_ACTIVITY_QUERY_LOG'] = str(queries)
        self.launch(['sh', str(BRIDGE), '__run'])
        self.state('play')
        eventually(lambda: self.events().count('start') == 1)

        for cycle in (1, 2):
            # A normal Bluetooth pause must release and park the hardware slave.
            self.state('stop')
            eventually(lambda starts=self.events().count('start'):
                       self.events().count('park') >= starts)
            eventually(lambda: not (self.run / 'bt-owner.pid').exists())

            # AirPlay takeover replaces the Bluetooth pause barrier. No phone
            # activity is present while AirPlay owns the speaker.
            (self.run / 'shairport.pid').write_text(
                str(self.dummy_watchdog.pid) + '\n')
            token = f'airplay-cycle-{cycle}'
            shell = ('. "$HK_AUDIO_COMMON"; SAFE=$HK_AP2_RUN/safe; '
                     'RUN=$HK_AP2_RUN; audio_reserve && lease_phase=active && '
                     'audio_write_lease')
            result = subprocess.run(
                ['sh', '-c', shell], env=self.env | {'HK_AUDIO_TOKEN': token},
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            (self.run / 'bt-wait-pcm').write_text(f'airplay-{cycle}\n')
            time.sleep(1)
            self.assertEqual(self.events().count('start'), cycle)

            # Simulate an interrupted AirPlay stop that leaves its barrier.
            for name in ('safe/lease', 'safe/play.started'):
                (self.run / name).unlink(missing_ok=True)
            self.pcm.write_text('state: PREPARED\nowner_pid   : 42\n')
            self.state('play')
            base_tick = cycle * 10
            self.advance_clock(base_tick)
            self.set_activity(100 * cycle, base_tick, instance=9000 + cycle)
            eventually(lambda: not (self.run / 'bt-wait-pcm').exists())
            for offset, frames in enumerate((356, 612, 868, 1124), 1):
                tick = base_tick + offset
                before = len(queries.read_text().splitlines()) if queries.exists() else 0
                self.set_activity(
                    frames * cycle, tick, instance=9000 + cycle)
                self.advance_clock(tick)
                eventually(
                    lambda before=before:
                    queries.exists() and
                    len(queries.read_text().splitlines()) > before
                )
            lock_tick = base_tick + 5
            self.set_activity(
                1380 * cycle, lock_tick, instance=9000 + cycle)
            self.advance_clock(lock_tick)
            eventually(lambda count=cycle + 1:
                       self.events().count('start') == count)

        self.assertFalse((self.safe / 'tripped').exists())
        self.assertFalse((self.run / 'ap2-claim').exists())

    def test_pending_phone_pause_blocks_reverse_takeover_despite_fresh_frames(self):
        self.seed_airplay()
        pending = self.run / 'bt-pause-pending'
        pending.write_text('airplay-100\n')
        (self.run / 'bt-wait-pcm').write_text('airplay-100\n')
        self.env['HK_BT_DEBUG_HANDOFF'] = '1'
        self.state('play')
        definitions = BRIDGE.read_text().split('trap cleanup EXIT', 1)[0]
        for tick in (1, 2, 3):
            self.set_activity(356 * tick, tick)
            self.advance_clock(tick)
            result = subprocess.run(
                ['sh', '-c', definitions + '\nobserve_airplay_takeover\n'],
                env=self.env, capture_output=True, text=True, timeout=8,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

        self.assert_no_reverse_takeover()
        self.assertEqual(pending.read_text().strip(), 'airplay-100')
        self.assertIn(
            'AirPlay takeover witness paused for pending phone pause',
            (self.run / 'bt-bridge.log').read_text(),
        )

    def test_pending_phone_pause_is_retried_until_status_confirms_stop(self):
        self.seed_airplay()
        pending = self.run / 'bt-pause-pending'
        pending.write_text('airplay-100\n')
        self.state('play')
        self.launch(['sh', str(BRIDGE), '__run'])
        for tick in range(1, 8):
            self.advance_clock(tick)
            time.sleep(.3)
            if not pending.exists():
                break
        eventually(lambda: not pending.exists())
        self.assertIn('onepause', self.events())
        self.assertIn('stop', self.status.read_text())

    def test_pending_phone_pause_tolerates_a_muted_play_timeline(self):
        self.seed_airplay()
        pending = self.run / 'bt-pause-pending'
        pending.write_text('airplay-100\n')
        self.status.write_text(
            '{"mode":"41","status":"play","vol":"12","mute":"1"}'
        )
        self.launch(['sh', str(BRIDGE), '__run'])
        for tick in range(1, 8):
            self.advance_clock(tick)
            time.sleep(.3)
            if not pending.exists():
                break
        eventually(lambda: not pending.exists())
        self.assertIn('onepause', self.events())
        self.assertIn('stop', self.status.read_text())

    def test_pending_phone_pause_is_not_cleared_by_malformed_status(self):
        self.seed_airplay()
        pending = self.run / 'bt-pause-pending'
        pending.write_text('airplay-100\n')
        self.status.write_text('{"mode":"41","status":')
        self.launch(['sh', str(BRIDGE), '__run'])
        self.advance_clock(1)
        time.sleep(1.2)
        self.assertTrue(pending.exists())
        self.assertNotIn('onepause', self.events())

    def test_stale_pending_phone_pause_is_removed_after_airplay_ends(self):
        pending = self.run / 'bt-pause-pending'
        pending.write_text('airplay-100\n')
        self.launch(['sh', str(BRIDGE), '__run'])
        self.advance_clock(1)
        eventually(lambda: not pending.exists())
        self.assertNotIn('onepause', self.events())

    def test_pending_phone_pause_survives_airplay_claim_before_lease(self):
        pending = self.run / 'bt-pause-pending'
        pending.write_text('airplay-100\n')
        pid = self.dummy_watchdog.pid
        start = subprocess.run(
            ['sh', '-c', '. "$HK_AUDIO_COMMON"; audio_pid_start "$1"',
             'sh', str(pid)],
            env=self.env, capture_output=True, text=True, check=True,
        ).stdout.strip()
        self.assertTrue(start)
        (self.run / 'ap2-claim').write_text(
            f'0 {pid} {start} starting-airplay\n'
        )
        definitions = BRIDGE.read_text().split('trap cleanup EXIT', 1)[0]
        result = subprocess.run(
            ['sh', '-c', definitions + '\nretry_pending_phone_pause\n'],
            env=self.env, capture_output=True, text=True, timeout=8,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(pending.exists())
        self.assertNotIn('onepause', self.events())

    def test_pending_phone_pause_retry_respects_fault_hold_and_voice(self):
        definitions = BRIDGE.read_text().split('trap cleanup EXIT', 1)[0]
        self.seed_airplay()
        pending = self.run / 'bt-pause-pending'
        cases = (
            ('fault', lambda: (self.safe / 'tripped').write_text('test\n')),
            ('maintenance hold',
             lambda: (self.base / 'hold-mute').touch()),
            ('voice', lambda: (self.run / 'voice-owns').write_text('0\n')),
        )
        for tick, (name, enable_gate) in enumerate(cases, 1):
            with self.subTest(name=name):
                for marker in (
                    self.safe / 'tripped',
                    self.base / 'hold-mute',
                    self.run / 'voice-owns',
                ):
                    marker.unlink(missing_ok=True)
                pending.write_text(f'airplay-{100 + tick}\n')
                enable_gate()
                self.advance_clock(tick)
                result = subprocess.run(
                    ['sh', '-c', definitions + '\nretry_pending_phone_pause\n'],
                    env=self.env, capture_output=True, text=True, timeout=8,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(pending.exists())
                self.assertNotIn('onepause', self.events())

    def test_bridge_repairs_stock_mute_during_active_airplay(self):
        self.seed_airplay()
        self.state('stop')
        state_tool = self.base / 'stock-state'
        self.executable(
            state_tool,
            '#!/bin/sh\nprintf "PA mute=1 force_mute=0 gate=1\\n"\n',
        )
        self.env['HK_AP2_STOCK_AUDIO_STATE'] = str(state_tool)
        self.launch(['sh', str(BRIDGE), '__run'])
        self.advance_clock(1)
        eventually(lambda: 'repair-stock-pa' in self.events())
        self.assert_no_reverse_takeover()

    def test_active_airplay_mute_repair_respects_source_and_fault_gates(self):
        self.seed_airplay()
        state_tool = self.base / 'stock-state-muted'
        self.executable(
            state_tool,
            '#!/bin/sh\nprintf "PA mute=1 force_mute=0 gate=1\\n"\n',
        )
        unmuted_tool = self.base / 'stock-state-unmuted'
        self.executable(
            unmuted_tool,
            '#!/bin/sh\nprintf "PA mute=0 force_mute=0 gate=1\\n"\n',
        )
        definitions = BRIDGE.read_text().split('trap cleanup EXIT', 1)[0]

        def run_case(name, setup, should_repair, tool=None):
            for marker in (
                self.safe / 'tripped',
                self.base / 'hold-mute',
                self.run / 'voice-owns',
                self.run / 'bt-owner.pid',
            ):
                marker.unlink(missing_ok=True)
            self.log.write_text('')
            (self.run / 'bt-bridge.log').write_text('')
            shell = ('. "$HK_AUDIO_COMMON"; SAFE=$HK_AP2_RUN/safe; '
                     'RUN=$HK_AP2_RUN; audio_reserve && lease_phase=active && '
                     'audio_write_lease')
            restored = subprocess.run(
                ['sh', '-c', shell], env=self.env,
                capture_output=True, text=True, timeout=8,
            )
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.env['HK_AP2_STOCK_AUDIO_STATE'] = str(tool or state_tool)
            self.advance_clock(1)
            setup()
            result = subprocess.run(
                ['sh', '-c', definitions
                 + '\nrepair_active_airplay_stock_mute\n'],
                env=self.env, capture_output=True, text=True, timeout=8,
            )
            self.assertEqual(result.returncode, 0, f'{name}: {result.stderr}')
            self.assertEqual(
                'repair-stock-pa' in self.events(), should_repair,
                name + '\n' + (self.run / 'bt-bridge.log').read_text(),
            )

        run_case('idle phone', lambda: self.state('stop'), True)
        run_case(
            'stale play timeline',
            lambda: ((self.base / 'activity').unlink(missing_ok=True),
                     self.state('play')),
            True,
        )
        run_case(
            'fresh phone frames',
            lambda: (self.state('play'), self.set_activity(356, 1)),
            False,
        )
        run_case(
            'source status unavailable',
            lambda: (self.base / 'activity').unlink(missing_ok=True),
            True,
        )
        run_case(
            'fault',
            lambda: (self.state('stop'),
                     (self.safe / 'tripped').write_text('test\n')),
            False,
        )
        run_case(
            'maintenance hold',
            lambda: (self.state('stop'),
                     (self.base / 'hold-mute').touch()),
            False,
        )
        run_case(
            'voice ownership',
            lambda: (self.state('stop'),
                     (self.run / 'voice-owns').write_text('0\n')),
            False,
        )
        run_case(
            'Bluetooth owner',
            lambda: (self.state('stop'),
                     (self.run / 'bt-owner.pid').write_text(
                         f'{self.dummy_watchdog.pid}\n')),
            False,
        )
        run_case(
            'already unmuted',
            lambda: self.state('stop'),
            False,
            unmuted_tool,
        )

    def seed_airplay(self):
        (self.run / 'shairport.pid').write_text(str(self.dummy_watchdog.pid))
        shell = '. "$HK_AUDIO_COMMON"; SAFE=$HK_AP2_RUN/safe; RUN=$HK_AP2_RUN; '
        shell += 'audio_reserve && lease_phase=active && audio_write_lease'
        subprocess.run(['sh', '-c', shell], env=self.env, check=True, capture_output=True)
        (self.run / 'ap2-claim').write_text('0\n')
        self.pcm.write_text('state: RUNNING\nowner_pid : 99\n')
        publisher = self.base / 'mqtt'
        self.executable(publisher, '''#!/bin/sh
echo "airplay $2" >>"$MOCK_LOG"
[ "${MOCK_MQTT_FAIL:-0}" != 1 ] || exit 1
printf 'state: RUNNING\\nowner_pid : 42\\n' >"$MOCK_PCM"
if [ "${MOCK_VOICE_ON_PAUSE:-0}" = 1 ]; then echo 0 >"$HK_AP2_RUN/voice-owns"; fi
''')
        self.env['HK_BT_MQTT'] = str(publisher)
        self.env['HK_AP2_HOLD_MUTE_FILE'] = str(self.base / 'hold-mute')
        self.activity_socket = socket.socket(socket.AF_UNIX)
        self.activity_socket.bind(str(self.base / 'pcm-control.sock'))
        self.env['HK_BT_PCM_CONTROL'] = str(self.base / 'pcm-control.sock')
        self.env['MOCK_ACTIVITY'] = str(self.base / 'activity')
        holder = self.proc / str(self.dummy_watchdog.pid)
        holder.mkdir(exist_ok=True)
        (holder / 'comm').write_text('a01controller\n')
        self.set_activity(100, 0)

    def set_activity(self, frames, tick, pid=None, instance=12345):
        pid = pid or self.dummy_watchdog.pid
        tmp = self.base / 'activity.tmp'
        tmp.write_text(f'activity {pid} {instance} {frames} {tick}\n')
        os.replace(tmp, self.base / 'activity')

    def arm_reverse(self):
        self.state('stop')
        self.env.setdefault(
            'MOCK_ACTIVITY_QUERY_LOG', str(self.base / 'activity-queries'))
        self.launch(['sh', str(BRIDGE), '__run'])
        time.sleep(.7)
        self.advance_clock(1)
        time.sleep(.7)

    def arm_disconnected(self):
        self.status.write_text('{"mode":"-1","status":"curpos":"0"}')
        self.launch(['sh', str(BRIDGE), '__run'])
        time.sleep(.7)
        self.advance_clock(1)
        time.sleep(.7)

    def resume_phone(self):
        self.set_activity(356, 2)
        self.state('play')
        self.advance_clock(2)

    def stream_phone_until_airplay_pauses(self, frames=356, tick=2, instance=12345):
        stop = threading.Event()

        def feed():
            frame = frames
            current = tick
            query_path = self.env.get('MOCK_ACTIVITY_QUERY_LOG')
            query_log = pathlib.Path(query_path) if query_path else None
            previous_queries = -1
            deadline = time.monotonic() + 20
            while not stop.is_set() and time.monotonic() < deadline:
                if 'start' in self.events():
                    return
                self.set_activity(frame, current, instance=instance)
                self.advance_clock(current)
                # Advance one synthetic uptime second per actual bridge
                # activity read. This keeps the three-sample confirmation
                # deterministic on hosts where usleep is unavailable and the
                # bridge falls back to one-second polling.
                if query_log:
                    while not stop.is_set() and time.monotonic() < deadline:
                        try:
                            query_count = len(query_log.read_text().splitlines())
                        except FileNotFoundError:
                            query_count = 0
                        if query_count > previous_queries:
                            previous_queries = query_count
                            break
                        time.sleep(.02)
                else:
                    time.sleep(1.1)
                frame += 256
                current += 1

        thread = threading.Thread(target=feed, daemon=True)
        thread.start()
        return stop, thread

    def advance_clock(self, tick):
        (self.base / 'clock').write_text(f'{tick}.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text(str(tick))

    def fast_reverse_start(self):
        """Observe one quiet sample, then simulate a quick phone play."""
        self.seed_airplay()
        self.env['MOCK_ACTIVITY_QUERY_LOG'] = str(self.base / 'activity-queries')
        self.env['MOCK_QUERY_LOG'] = str(self.base / 'player-queries')
        self.state('stop')
        self.launch(['sh', str(BRIDGE), '__run'])
        queries = self.base / 'activity-queries'
        eventually(lambda: queries.exists() and len(queries.read_text().splitlines()) >= 1)
        time.sleep(.2)  # Allow the real bridge to finish recording this observation.
        self.state('play')
        self.fast_reverse_sample(1, 356)

    def fast_reverse_sample(self, tick, frames):
        queries = self.base / 'player-queries'
        before = len(queries.read_text().splitlines())
        self.set_activity(frames, tick)
        self.advance_clock(tick)
        eventually(lambda: len(queries.read_text().splitlines()) > before)
        time.sleep(.2)

    def assert_no_reverse_takeover(self):
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_quick_phone_play_takes_airplay_after_continuous_fresh_frames(self):
        self.fast_reverse_start()
        self.fast_reverse_sample(2, 612)
        self.assert_no_reverse_takeover()
        self.fast_reverse_sample(3, 868)
        self.fast_reverse_sample(4, 1124)
        eventually(lambda: 'start' in self.events())
        self.assertLess(self.events().index('stop'), self.events().index('airplay pause'))
        self.assertLess(self.events().index('airplay pause'), self.events().index('start'))

    def test_phone_play_without_any_quiet_sample_takes_airplay_after_fresh_frames(self):
        # AirPlay is already playing and the phone connects directly in the
        # play state. No paused-baseline witness exists for this lease.
        self.seed_airplay()
        self.state('play')
        activity_queries = self.base / 'activity-queries'
        player_queries = self.base / 'player-queries'
        self.env['MOCK_ACTIVITY_QUERY_LOG'] = str(activity_queries)
        self.env['MOCK_QUERY_LOG'] = str(player_queries)
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: player_queries.exists() and player_queries.read_text())
        for tick, frames in ((1, 356), (2, 612), (3, 868), (4, 1124)):
            self.fast_reverse_sample(tick, frames)
        stop, thread = self.stream_phone_until_airplay_pauses(1380, 5)
        try:
            eventually(lambda: 'start' in self.events())
        finally:
            stop.set()
            thread.join(2)
        events = self.events()
        self.assertLess(events.index('stop'), events.index('airplay pause'))
        self.assertLess(events.index('airplay pause'), events.index('start'))

    def test_single_old_phone_tail_without_quiet_sample_cannot_take_airplay(self):
        self.seed_airplay()
        self.state('play')
        player_queries = self.base / 'player-tail-queries'
        self.env['MOCK_QUERY_LOG'] = str(player_queries)
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: player_queries.exists() and player_queries.read_text())
        for tick in (1, 2, 3, 4):
            before = len(player_queries.read_text().splitlines())
            self.set_activity(100, tick)
            self.advance_clock(tick)
            eventually(lambda: len(player_queries.read_text().splitlines()) > before)
            time.sleep(.2)
        self.assert_no_reverse_takeover()

    def test_quick_phone_play_single_tail_cannot_take_airplay(self):
        self.fast_reverse_start()
        for tick in (2, 3, 4):
            self.fast_reverse_sample(tick, 356)
        self.assert_no_reverse_takeover()

    def test_quick_phone_play_missed_samples_restart_confirmation(self):
        self.fast_reverse_start()
        for tick, frames in ((4, 612), (5, 868), (6, 1124)):
            self.fast_reverse_sample(tick, frames)
            self.assert_no_reverse_takeover()
        stop, thread = self.stream_phone_until_airplay_pauses(1380, 7)
        try:
            eventually(lambda: 'start' in self.events())
        finally:
            stop.set()
            thread.join(2)

    def test_quick_phone_play_pause_restarts_confirmation(self):
        self.fast_reverse_start()
        self.state('stop')
        self.fast_reverse_sample(2, 356)
        self.state('play')
        for tick, frames in ((3, 612), (4, 868)):
            self.fast_reverse_sample(tick, frames)
            self.assert_no_reverse_takeover()
        self.fast_reverse_sample(5, 1124)
        self.fast_reverse_sample(6, 1380)
        eventually(lambda: 'start' in self.events())

    def test_quick_phone_play_failed_status_query_restarts_confirmation(self):
        self.env['MOCK_STATUS_FAIL'] = str(self.base / 'status-fail')
        self.fast_reverse_start()
        (self.base / 'status-fail').touch()
        self.fast_reverse_sample(2, 612)
        (self.base / 'status-fail').unlink()
        for tick, frames in ((3, 868), (4, 1124)):
            self.fast_reverse_sample(tick, frames)
            self.assert_no_reverse_takeover()
        self.fast_reverse_sample(5, 1380)
        self.fast_reverse_sample(6, 1636)
        eventually(lambda: 'start' in self.events())

    def test_quick_phone_play_inactive_airplay_lease_restarts_confirmation(self):
        self.fast_reverse_start()
        fields = (self.safe / 'lease').read_text().split()
        fields[-1] = 'draining'
        (self.safe / 'lease').write_text(' '.join(fields) + '\n')
        self.fast_reverse_sample(2, 612)
        fields[-1] = 'active'
        (self.safe / 'lease').write_text(' '.join(fields) + '\n')
        for tick, frames in ((3, 868), (4, 1124)):
            self.fast_reverse_sample(tick, frames)
            self.assert_no_reverse_takeover()
        self.fast_reverse_sample(5, 1380)
        self.fast_reverse_sample(6, 1636)
        eventually(lambda: 'start' in self.events())

    def assert_quick_reverse_witness_cancelled(self, interruption):
        self.fast_reverse_start()
        if interruption == 'hold':
            marker = self.base / 'hold-mute'
            marker.touch()
        elif interruption == 'voice':
            marker = self.run / 'voice-owns'
            marker.write_text('2\n')
        elif interruption == 'lease':
            fields = (self.safe / 'lease').read_text().split()
            fields[3] = 'new-airplay'
            (self.safe / 'lease').write_text(' '.join(fields) + '\n')
            marker = None
        else:
            raise AssertionError(interruption)
        self.advance_clock(2)
        time.sleep(1.5)  # The guard is checked before the activity query;
                          # hosts without usleep poll at one-second intervals.
        if marker:
            marker.unlink()
        for tick, frames in ((3, 612), (4, 868), (5, 1124), (6, 1380)):
            self.fast_reverse_sample(tick, frames)
            self.assert_no_reverse_takeover()

    def test_quick_phone_play_maintenance_hold_cancels_witness(self):
        self.assert_quick_reverse_witness_cancelled('hold')

    def test_quick_phone_play_voice_cancels_witness(self):
        self.assert_quick_reverse_witness_cancelled('voice')

    def test_quick_phone_play_new_airplay_lease_cancels_witness(self):
        self.assert_quick_reverse_witness_cancelled('lease')

    def test_phone_resume_pauses_airplay_before_bluetooth_acquires(self):
        self.seed_airplay()
        self.arm_reverse()
        self.state('play')
        stop, thread = self.stream_phone_until_airplay_pauses(356, 2)
        try:
            eventually(lambda: 'start' in self.events())
        finally:
            stop.set()
            thread.join(2)
        events = self.events()
        self.assertLess(events.index('stop'), events.index('airplay pause'))
        self.assertLess(events.index('airplay pause'), events.index('start'))
        self.assertEqual(events.count('airplay pause'), 1)
        self.assertFalse((self.run / 'release-dac').exists())
        self.assertTrue((self.run / 'bt-owner.pid').exists())

    def test_stale_play_without_paused_edge_does_not_steal_airplay(self):
        self.seed_airplay(); self.state('play')
        self.launch(['sh', str(BRIDGE), '__run'])
        time.sleep(.5); self.advance_clock(1); time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_new_airplay_generation_discards_old_phone_pause_witness(self):
        self.seed_airplay(); self.arm_reverse()
        fields = (self.safe / 'lease').read_text().split(); fields[3] = 'new-airplay'
        (self.safe / 'lease').write_text(' '.join(fields) + '\n')
        self.resume_phone(); time.sleep(1)
        self.assertNotIn('airplay pause', self.events())

    def test_failed_remote_pause_keeps_bluetooth_output_closed(self):
        self.seed_airplay(); self.state('stop')
        self.env['MOCK_MQTT_FAIL'] = '1'
        self.env['MOCK_ACTIVITY_QUERY_LOG'] = str(
            self.base / 'activity-queries')
        self.launch(['sh', str(BRIDGE), '__run'])
        time.sleep(.7)
        self.advance_clock(1); time.sleep(.7)
        self.state('play')
        stop, thread = self.stream_phone_until_airplay_pauses(356, 2)
        try:
            eventually(lambda: (self.safe / 'tripped').exists())
        finally:
            stop.set()
            thread.join(2)
        self.assertNotIn('start', self.events())
        self.assertIn('airplay pause', self.events())

    def check_unreleased_airplay_clock_blocks_bluetooth(self, state):
        self.seed_airplay()
        self.executable(self.base / 'mqtt-no-release', '''#!/bin/sh
echo "airplay $2" >>"$MOCK_LOG"
''')
        self.env['HK_BT_MQTT'] = str(self.base / 'mqtt-no-release')
        self.env['HK_BT_RELEASE_WAIT_TRIES'] = '5'
        self.pcm.write_text(f'state: {state}\nowner_pid   : 99\n')
        self.arm_reverse()
        self.state('play')
        stop, thread = self.stream_phone_until_airplay_pauses(356, 2)
        try:
            eventually(lambda: (self.run / 'release-dac').exists())
            eventually(lambda: (self.safe / 'tripped').exists())
        finally:
            stop.set()
            thread.join(2)
        self.assertNotIn('start', self.events())
        self.assertFalse((self.run / 'bt-owner.pid').exists())
        self.assertFalse((self.safe / 'lease').exists())
        self.assertFalse((self.run / 'ap2-claim').exists())
        self.assertIn(
            'AirPlay hardware release timed out',
            (self.run / 'bt-bridge.log').read_text(),
        )

        # Once the real clock closes and the operator clears the intentional
        # protection, the stale release flag is reaped without starting a
        # phone stream or reopening audio.
        self.state('stop')
        (self.safe / 'tripped').unlink()
        self.pcm.write_text('closed\n')
        self.advance_clock(3)
        eventually(lambda: not (self.run / 'release-dac').exists())
        self.assertNotIn('start', self.events())

    def test_prepared_airplay_clock_release_timeout_keeps_bluetooth_closed(self):
        self.check_unreleased_airplay_clock_blocks_bluetooth('PREPARED')

    def test_xrun_airplay_clock_release_timeout_keeps_bluetooth_closed(self):
        self.check_unreleased_airplay_clock_blocks_bluetooth('XRUN')

    def test_maintenance_hold_blocks_reverse_takeover(self):
        self.seed_airplay(); self.arm_reverse()
        (self.base / 'hold-mute').touch()
        self.resume_phone(); time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_voice_ownership_blocks_reverse_takeover(self):
        self.seed_airplay(); self.arm_reverse()
        (self.run / 'voice-owns').write_text('0\n')
        self.resume_phone(); time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_stale_play_after_pause_without_fresh_pcm_does_not_steal_airplay(self):
        self.seed_airplay(); self.arm_reverse()
        self.state('play'); self.advance_clock(2); time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_new_phone_connecting_and_playing_can_take_airplay(self):
        self.seed_airplay(); self.arm_disconnected()
        self.state('play')
        stop, thread = self.stream_phone_until_airplay_pauses(356, 2)
        try:
            eventually(lambda: 'start' in self.events())
        finally:
            stop.set()
            thread.join(2)
        self.assertLess(self.events().index('airplay pause'), self.events().index('start'))

    def test_new_phone_connecting_paused_does_not_steal_airplay(self):
        self.seed_airplay(); self.arm_disconnected()
        self.state('stop'); self.advance_clock(2); time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_reconnect_with_stale_play_but_no_fresh_pcm_does_not_steal_airplay(self):
        self.seed_airplay(); self.arm_disconnected()
        self.state('play'); self.advance_clock(2); time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_pcm_while_phone_reports_paused_does_not_steal_airplay(self):
        self.seed_airplay(); self.arm_reverse()
        self.set_activity(356, 2); self.advance_clock(2); time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_unreadable_activity_is_not_treated_as_a_quiet_pause(self):
        self.seed_airplay(); self.state('stop')
        self.launch(['sh', str(BRIDGE), '__run'])
        time.sleep(.7)
        (self.base / 'activity').write_text('refused\n')
        self.advance_clock(1); time.sleep(.7)
        self.resume_phone(); time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_unknown_activity_timestamp_cannot_trigger_takeover(self):
        self.seed_airplay(); self.arm_reverse()
        self.set_activity(356, 0)
        self.state('play'); self.advance_clock(2); time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_factory_player_restart_invalidates_old_pause_witness(self):
        self.seed_airplay(); self.arm_reverse()
        replacement = self.launch(['sleep', '60'])
        (self.proc / str(replacement.pid)).mkdir()
        (self.proc / str(replacement.pid) / 'comm').write_text('a01controller\n')
        self.set_activity(356, 2, pid=replacement.pid, instance=54321)
        self.state('play'); self.advance_clock(2); time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_new_pcm_after_stably_paused_phone_can_take_airplay(self):
        self.seed_airplay()
        self.activity_socket.close()
        (self.base / 'pcm-control.sock').unlink()
        self.arm_reverse()
        self.activity_socket = socket.socket(socket.AF_UNIX)
        self.activity_socket.bind(str(self.base / 'pcm-control.sock'))
        self.state('play')
        stop, thread = self.stream_phone_until_airplay_pauses(
            256, 2, instance=54321
        )
        try:
            eventually(lambda: 'start' in self.events())
        finally:
            stop.set()
            thread.join(2)
        self.assertLess(self.events().index('airplay pause'), self.events().index('start'))

    def test_phone_pausing_while_waiting_for_speaker_lock_cancels_takeover(self):
        self.seed_airplay()
        self.env['MOCK_ACTIVITY_QUERY_LOG'] = str(self.base / 'activity-queries')
        self.arm_reverse()
        lock = subprocess.Popen(['sh', '-c', '. "$HK_AUDIO_COMMON"; '
                                 'audio_lock "$HK_AP2_RUN/speaker.lock" && '
                                 'echo locked && read answer && audio_unlock "$HK_AP2_RUN/speaker.lock"'],
                                env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        self.children.append(lock)
        self.assertEqual(lock.stdout.readline().strip(), 'locked')
        queries = self.base / 'activity-queries'
        before = len(queries.read_text().splitlines())
        self.resume_phone()
        eventually(lambda: len(queries.read_text().splitlines()) > before)
        self.state('stop')
        lock.stdin.write('release\n'); lock.stdin.flush(); lock.wait(timeout=4)
        lock.stdin.close(); lock.stdout.close()
        time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_stale_play_after_witness_without_a_new_frame_cancels_takeover(self):
        self.seed_airplay()
        self.env['MOCK_ACTIVITY_QUERY_LOG'] = str(self.base / 'activity-queries')
        self.arm_reverse()
        lock = subprocess.Popen(['sh', '-c', '. "$HK_AUDIO_COMMON"; '
                                 'audio_lock "$HK_AP2_RUN/speaker.lock" && '
                                 'echo locked && read answer && audio_unlock "$HK_AP2_RUN/speaker.lock"'],
                                env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        self.children.append(lock)
        self.assertEqual(lock.stdout.readline().strip(), 'locked')
        queries = self.base / 'activity-queries'
        before = len(queries.read_text().splitlines())
        self.resume_phone()
        eventually(lambda: len(queries.read_text().splitlines()) > before)
        # Keep the factory's "play" string and activity timestamp fresh, but
        # publish no new audio frame. The post-lock recheck must abort.
        lock.stdin.write('release\n'); lock.stdin.flush(); lock.wait(timeout=4)
        lock.stdin.close(); lock.stdout.close()
        time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def hold_speaker_lock(self):
        lock = subprocess.Popen(
            ['sh', '-c', '. "$HK_AUDIO_COMMON"; '
             'audio_lock "$HK_AP2_RUN/speaker.lock" && echo locked && '
             'read answer && audio_unlock "$HK_AP2_RUN/speaker.lock"'],
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.children.append(lock)
        self.assertEqual(lock.stdout.readline().strip(), 'locked')
        return lock

    @staticmethod
    def release_speaker_lock(lock):
        lock.stdin.write('release\n')
        lock.stdin.flush()
        lock.wait(timeout=4)
        lock.stdin.close()
        lock.stdout.close()

    def test_lock_contended_takeover_accepts_progress_after_lock_was_taken(self):
        self.seed_airplay()
        self.env['MOCK_ACTIVITY_QUERY_LOG'] = str(self.base / 'activity-queries')
        self.env['MOCK_QUERY_LOG'] = str(self.base / 'player-queries')
        self.arm_reverse()
        lock = self.hold_speaker_lock()
        self.resume_phone()
        self.fast_reverse_sample(3, 612)
        self.set_activity(868, 4)
        self.advance_clock(4)
        time.sleep(.4)
        # The bridge is now waiting for speaker.lock. Publish a newer frame
        # before releasing it; the post-lock check must accept this progress.
        self.set_activity(1124, 5)
        self.advance_clock(5)
        self.release_speaker_lock(lock)
        eventually(lambda: 'start' in self.events())
        self.assertLess(self.events().index('airplay pause'), self.events().index('start'))

    def test_maintenance_hold_while_waiting_for_speaker_lock_cancels_takeover(self):
        self.seed_airplay()
        self.env['MOCK_ACTIVITY_QUERY_LOG'] = str(self.base / 'activity-queries')
        self.arm_reverse()
        lock = subprocess.Popen(['sh', '-c', '. "$HK_AUDIO_COMMON"; '
                                 'audio_lock "$HK_AP2_RUN/speaker.lock" && '
                                 'echo locked && read answer && audio_unlock "$HK_AP2_RUN/speaker.lock"'],
                                env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
        self.children.append(lock)
        self.assertEqual(lock.stdout.readline().strip(), 'locked')
        queries = self.base / 'activity-queries'
        before = len(queries.read_text().splitlines())
        self.resume_phone()
        eventually(lambda: len(queries.read_text().splitlines()) > before)
        (self.base / 'hold-mute').touch()
        lock.stdin.write('release\n'); lock.stdin.flush(); lock.wait(timeout=4)
        lock.stdin.close(); lock.stdout.close()
        time.sleep(1)
        self.assertNotIn('airplay pause', self.events())
        self.assertNotIn('stop', self.events())

    def test_voice_arriving_during_airplay_pause_retains_its_release_flag(self):
        self.seed_airplay()
        self.env['MOCK_VOICE_ON_PAUSE'] = '1'
        self.arm_reverse()
        self.state('play')
        stop, thread = self.stream_phone_until_airplay_pauses(356, 2)
        try:
            eventually(lambda: 'airplay pause' in self.events())
        finally:
            stop.set()
            thread.join(2)
        time.sleep(1)
        self.assertTrue((self.run / 'release-dac').exists())
        self.assertNotIn('start', self.events())

    def test_dead_pcm_opener_uses_verified_holder_for_acquire_and_watchdog(self):
        pid = self.dummy_watchdog.pid
        holder = self.proc / str(pid)
        (holder / 'fd').mkdir(parents=True)
        (holder / 'comm').write_text('a01controller\n')
        (holder / 'fd/26').symlink_to('/dev/snd/pcmC0D0p')
        result = subprocess.run(['sh', '-c', '. "$HK_AUDIO_COMMON"; audio_pid_start "$1"',
                                 'sh', str(pid)], env=self.env, capture_output=True, text=True, check=True)
        (self.base / 'bt-pcm-owner').write_text(f'{pid} {result.stdout.strip()} 26 12345\n')
        (self.proc / '42/comm').unlink()  # The recorded opener thread is gone.
        self.state('play')
        bridge = self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: 'start' in self.events())
        eventually(lambda: (self.safe / 'lease').exists())
        self.assertFalse((self.safe / 'tripped').exists())
        self.assertIsNone(bridge.poll())
        # Shared checks are also used by the real watchdog and hook.
        result = subprocess.run(['sh', '-c', '. "$HK_AUDIO_COMMON"; RUN=$HK_AP2_RUN; '
                                 'SAFE=$RUN/safe; audio_bt_valid'], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        (holder / 'fd/26').unlink()
        eventually(lambda: 'stop' in self.events())

    def test_play_without_pcm_rebuilds_a2dp_transport(self):
        self.pcm.write_text('state: closed\n')
        self.state('play')
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: self.events().count('start') == 1, timeout=15)
        events = self.events()
        self.assertLess(events.index('onepause'), events.index('resume'))
        self.assertLess(events.index('resume'), events.index('start'))
        self.assertTrue((self.run / 'bt-owner.pid').exists())

    def test_not_playing_sample_does_not_consume_a_transport_repair_tick(self):
        self.pcm.write_text('state: closed\n')
        self.state('play')
        definitions = BRIDGE.read_text().split('trap cleanup EXIT', 1)[0]
        batch = '''
wait_source_playing=0
repair_stalled_bluetooth || true
wait_source_playing=1
repair_stalled_bluetooth
'''
        result = subprocess.run(
            ['sh', '-c', definitions + batch],
            env=self.env, capture_output=True, text=True, timeout=6,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.events().count('onepause'), 1,
            'a stale not-playing sample must not consume the current tick',
        )
        self.assertEqual(self.events().count('resume'), 1)

    def clock_recovery_watchdog(self):
        mixer = self.base / 'clock-mixer'
        self.executable(mixer, '''#!/bin/sh
echo "$*" >>"$MOCK_MIXER_LOG"
case "$*" in
 *"sget Master"*) echo 'Front Left: 4';;
 *"cget numid=1"*) echo '  : values=207,207';;
 *"cget numid=2"*) echo '  : values=0,0';;
esac
''')
        env = self.env | {'HK_AP2_AMIXER': str(mixer),
                          'MOCK_MIXER_LOG': str(self.base / 'clock-mixer.log'),
                          'HK_AP2_MAX_PLAY_SECONDS': '0', 'HK_AP2_PLAYBACK_SAFE': '1',
                          'HK_AP2_BT_PLAYBACK_SAFE': '0'}
        return self.launch(['sh', str(WATCHDOG), '__watchdog'], env)

    def check_clock_recovery(self, watchdog_first):
        self.state('play')
        bridge = self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: (self.safe / 'lease').exists()
                   and (self.safe / 'lease').read_text().endswith('active\n'))
        old_token = (self.safe / 'lease').read_text().split()[3]
        stopped = False
        try:
            if watchdog_first:
                # Delay bridge handling deterministically. The independent
                # watchdog must mute before the bridge can process the pause.
                bridge.send_signal(signal.SIGSTOP)
                stopped = True
                # Freeze the stopped bridge with a fresh heartbeat. Otherwise
                # scheduler delay can make this test exercise the unrelated
                # "bridge lost" fault before the intended XRUN recovery path.
                (self.base / 'clock').write_text('10.00 0.00\n')
                (self.run / 'bt-heartbeat').write_text('10\n')
                (self.safe / 'watchdog.heartbeat').write_text('10\n')
                self.clock_recovery_watchdog()
            self.pcm.write_text('state: XRUN\nowner_pid   : 42\n')
            eventually(lambda: (self.run / 'bt-wait-pcm').exists())
            self.assertFalse((self.safe / 'tripped').exists(), 'ordinary XRUN must not latch global mute')
            if watchdog_first:
                eventually(lambda: (self.base / 'clock-mixer.log').exists()
                           and 'Ext Spk off' in (self.base / 'clock-mixer.log').read_text())
                bridge.send_signal(signal.SIGCONT)
                stopped = False
            eventually(lambda: not (self.run / 'bt-owner.pid').exists()
                       and not (self.safe / 'lease').exists())
            # The source incorrectly retains "play" while paused. Neither
            # retained XRUN nor a later close may auto-resume the phone.
            time.sleep(2)
            self.pcm.write_text('closed\n')
            time.sleep(2)
            self.assertNotIn('onepause', self.events())
            self.assertNotIn('resume', self.events())
            self.assertEqual(self.events().count('start'), 1)
            self.assertFalse(
                (self.safe / 'tripped').exists(),
                (self.safe / 'tripped').read_text() if (self.safe / 'tripped').exists() else '',
            )
            # Only new manual audio/PCM permits a fresh guarded lease.
            self.pcm.write_text('state: RUNNING\nowner_pid   : 42\n')
            eventually(lambda: self.events().count('start') == 2
                       and (self.safe / 'lease').exists()
                       and (self.safe / 'lease').read_text().endswith('active\n'))
            self.assertNotEqual((self.safe / 'lease').read_text().split()[3], old_token)
            self.assertFalse((self.run / 'bt-wait-pcm').exists())
            self.assertFalse((self.safe / 'tripped').exists())
        finally:
            if stopped: bridge.send_signal(signal.SIGCONT)

    def test_watchdog_first_bluetooth_xrun_waits_for_manual_audio(self):
        self.check_clock_recovery(True)

    def test_bridge_first_bluetooth_xrun_waits_for_manual_audio(self):
        self.check_clock_recovery(False)

    def test_pause_barrier_requires_two_seconds_of_fresh_frames(self):
        self.pcm.write_text('state: closed\n')
        self.state('play')
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
if [ "$1" = start ]; then
  printf 'state: RUNNING\\nowner_pid   : 42\\n' >"$MOCK_PCM"
  SAFE=$HK_AP2_RUN/safe; RUN=$HK_AP2_RUN
  . "$HK_AUDIO_COMMON"
  HK_AP2_BT=1
  audio_reserve || exit 1
  lease_phase=active
  audio_write_lease || exit 1
else
  rm -f "$HK_AP2_RUN/safe/play.started" "$HK_AP2_RUN/safe/lease"
fi
''')
        (self.run / 'bt-wait-pcm').write_text('bt-paused-generation\n')
        activity_log = self.base / 'barrier-activity-queries'
        self.env['MOCK_ACTIVITY_QUERY_LOG'] = str(activity_log)
        self.activity_socket = socket.socket(socket.AF_UNIX)
        self.activity_socket.bind(str(self.base / 'barrier-control.sock'))
        self.env['HK_BT_PCM_CONTROL'] = str(self.base / 'barrier-control.sock')
        self.env['MOCK_ACTIVITY'] = str(self.base / 'activity')
        holder = self.proc / str(self.dummy_watchdog.pid)
        holder.mkdir(exist_ok=True)
        (holder / 'comm').write_text('a01controller\n')
        self.set_activity(100, 10)
        (self.base / 'clock').write_text('10.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('10\n')
        self.launch(['sh', str(BRIDGE), '__run'])

        def publish_after_query(frames):
            before = len(activity_log.read_text().splitlines()) if activity_log.exists() else 0
            eventually(
                lambda: activity_log.exists() and
                len(activity_log.read_text().splitlines()) > before
            )
            self.set_activity(frames, 10)
            time.sleep(.1)

        # Three rapid frame-counter advances are still consistent with a
        # short retained tail and must not remove the barrier.
        publish_after_query(356)
        publish_after_query(612)
        publish_after_query(868)
        time.sleep(.5)
        self.assertNotIn('start', self.events())
        self.assertTrue((self.run / 'bt-wait-pcm').exists())

        # Two monotonic seconds of continuing frames for the same PCM can
        # release it; the bridge still obtains a fresh guarded lease.
        (self.base / 'clock').write_text('12.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('12\n')
        frames = 1124
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if 'start' in self.events():
                break
            self.set_activity(frames, 12)
            frames += 256
            time.sleep(.15)
        eventually(lambda: self.events().count('start') == 1)
        self.assertFalse((self.run / 'bt-wait-pcm').exists())
        self.assertFalse((self.safe / 'tripped').exists())

    def check_foreign_owner_fault(self, state):
        self.state('play')
        bridge = self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: (self.safe / 'lease').exists()
                   and (self.safe / 'lease').read_text().endswith('active\n'))
        bridge.send_signal(signal.SIGSTOP)
        try:
            (self.proc / '42/comm').write_text('aplay\n')
            self.pcm.write_text('state: %s\nowner_pid   : 42\n' % state)
            self.clock_recovery_watchdog()
            eventually(lambda: (self.safe / 'tripped').exists())
            self.assertEqual((self.safe / 'tripped').read_text().strip(), 'PCM owner or clock lost')
            self.assertFalse((self.run / 'bt-wait-pcm').exists())
        finally:
            bridge.send_signal(signal.SIGCONT)

    def test_running_wrong_pcm_owner_still_latches_fault(self):
        self.check_foreign_owner_fault('RUNNING')

    def test_stopped_wrong_pcm_owner_still_latches_fault(self):
        self.check_foreign_owner_fault('XRUN')

    def test_disconnect_allows_initial_repair_for_a_new_connection(self):
        self.env['HK_BT_DEBUG_HANDOFF'] = '1'
        query_log = self.base / 'disconnect-repair-queries'
        self.env['MOCK_QUERY_LOG'] = str(query_log)
        self.env['MOCK_QUERY_DETAILS'] = '1'
        self.state('play')
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: (self.safe / 'lease').exists())
        self.state('stop')
        eventually(lambda: not (self.run / 'bt-owner.pid').exists()
                   and (self.run / 'bt-wait-pcm').exists())
        self.pcm.write_text('closed\n')
        self.status.write_text('{"mode":"-1","status":"","vol":"12","mute":"0"}')
        (self.base / 'clock').write_text('1.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('1\n')
        eventually(lambda: not (self.run / 'bt-wait-pcm').exists())
        self.state('play')
        (self.base / 'clock').write_text('2.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('2\n')
        try:
            eventually(lambda: self.events().count('start') == 2, timeout=15)
        except AssertionError as error:
            raise AssertionError(
                f'{error}\n{(self.run / "bt-bridge.log").read_text()}\n'
                f'{query_log.read_text() if query_log.exists() else ""}\n'
                f'{self.log.read_text()}'
            ) from error
        self.assertIn('onepause', self.events())
        self.assertIn('resume', self.events())
        self.assertFalse((self.safe / 'tripped').exists())

    def test_disconnect_cannot_delete_a_concurrent_airplay_barrier(self):
        self.seed_airplay()
        queries = self.base / 'disconnect-airplay-queries'
        self.env['MOCK_QUERY_LOG'] = str(queries)
        self.status.write_text('{"mode":"-1","status":"curpos":"0"}')
        marker = self.run / 'bt-wait-pcm'
        marker.write_text('airplay-live-generation\n')
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: queries.exists() and queries.read_text())
        time.sleep(1)
        self.assertEqual(marker.read_text().strip(), 'airplay-live-generation')
        self.assertTrue((self.safe / 'lease').exists())
        self.assertTrue((self.run / 'ap2-claim').exists())

    def test_completed_airplay_release_marker_is_removed_after_clock_closes(self):
        marker = self.run / 'release-dac'
        marker.write_text('1\n')
        self.pcm.write_text('closed\n')
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: not marker.exists())
        self.assertIn(
            'removed completed AirPlay release marker',
            (self.run / 'bt-bridge.log').read_text(),
        )

    def test_airplay_release_marker_stays_while_clock_or_session_is_live(self):
        self.seed_airplay()
        marker = self.run / 'release-dac'
        marker.write_text('1\n')
        self.launch(['sh', str(BRIDGE), '__run'])
        time.sleep(1.2)
        self.assertTrue(marker.exists(), 'a live AirPlay session must retain the release barrier')
        (self.safe / 'lease').unlink()
        (self.run / 'ap2-claim').unlink()
        self.pcm.write_text('state: PREPARED\nowner_pid   : 99\n')
        time.sleep(1.2)
        self.assertTrue(marker.exists(), 'a retained hardware clock must retain the release barrier')

    def test_fast_idle_poll_keeps_paused_source_rpc_at_one_per_second(self):
        (self.run / 'bt-wait-pcm').write_text('paused\n')
        self.pcm.write_text('state: XRUN\nowner_pid   : 42\n')
        queries = self.base / 'queries'
        env = self.env | {'MOCK_QUERY_LOG': str(queries)}
        definitions = BRIDGE.read_text().split('trap cleanup EXIT', 1)[0]
        batch = 'for n in 1 2 3 4 5 6 7 8 9 10; do refresh_paused_source; repair_stalled_bluetooth || true; done\n'
        check = definitions + '\n' + batch + '''
[ "$(wc -l <"$MOCK_QUERY_LOG")" -eq 1 ] || exit 1
printf '1.00 0.00\\n' >"$HK_AUDIO_UPTIME_FILE"
printf '1\\n' >"$HK_AP2_RUN/safe/watchdog.heartbeat"
''' + batch
        subprocess.run(['sh', '-c', check], env=env, check=True, timeout=4)
        self.assertEqual(queries.read_text().splitlines(), ['query', 'query'])
        self.assertNotIn('resume', self.events())

    def test_voice_mixer_keeps_the_bluetooth_lease(self):
        self.state('play')
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: self.events().count('start') == 1)
        (self.run / 'voice-owns').write_text('0\n')
        (self.run / 'voice-inject').mkdir()
        (self.run / 'voice-inject/active').touch()
        time.sleep(2)
        self.assertNotIn('stop', self.events())
        self.assertTrue((self.run / 'bt-owner.pid').exists())
        (self.run / 'voice-inject/active').unlink()
        time.sleep(2)
        self.assertNotIn('stop', self.events())
        self.assertTrue(
            (self.run / 'bt-owner.pid').exists(),
            'a still-running Bluetooth PCM must keep its output lease after voice mixing',
        )
        (self.run / 'voice-owns').unlink()
        time.sleep(2)
        self.assertNotIn('stop', self.events())
        self.assertTrue((self.run / 'bt-owner.pid').exists())
        self.state('stop')
        eventually(
            lambda: 'stop' in self.events()
            and not (self.run / 'bt-owner.pid').exists()
        )

    def test_watchdog_latches_mute_when_bridge_dies(self):
        self.state('play')
        bridge = self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: self.events().count('start') == 1)
        player = self.launch(['sleep', '30'])
        (self.run / 'shairport.pid').write_text(str(player.pid) + '\n')
        mixer = self.base / 'fake-amixer'
        self.executable(mixer, '''#!/bin/sh
echo "$*" >>"$MOCK_MIXER_LOG"
case "$*" in
  *"sget Master"*) echo 'Front Left: 4';;
  *"cget numid=1"*) echo '  : values=100,100';;
  *"cget numid=2"*) echo '  : values=1,1';;
esac
''')
        mixer_log = self.base / 'mixer.log'
        mixer_log.touch()
        env = self.env | {'HK_AP2_AMIXER': str(mixer), 'MOCK_MIXER_LOG': str(mixer_log),
                          'HK_AP2_MAX_PLAY_SECONDS': '0', 'HK_AP2_MASTER_SAFE': '4',
                          'HK_AP2_DIGITAL_SAFE': '192', 'HK_AP2_PLAYBACK_SAFE': '1', 'HK_AP2_BT_PLAYBACK_SAFE': '1'}
        self.launch(['sh', str(WATCHDOG), '__watchdog'], env)
        eventually(lambda: (self.safe / 'watchdog.heartbeat').exists())
        bridge.kill()
        bridge.wait()
        eventually(lambda: (self.safe / 'tripped').exists(), timeout=8)
        self.assertEqual((self.safe / 'tripped').read_text().strip(), 'Bluetooth bridge lost')
        eventually(lambda: not (self.safe / 'play.started').exists())
        eventually(lambda: 'Ext Spk off' in mixer_log.read_text())
        self.assertIn('Ext Spk off', mixer_log.read_text())

    def test_watchdog_rechecks_clock_after_reading_bluetooth_heartbeat(self):
        # Simulate the monotonic clock crossing a second between the loop's
        # first clock read and the bridge heartbeat read.
        self.executable(self.base / 'cut', '''#!/bin/sh
n=$(cat "$MOCK_CLOCK_COUNT" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" >"$MOCK_CLOCK_COUNT"
if [ "$n" -le 2 ]; then echo 0; else echo 1; fi
''')
        mixer = self.base / 'fake-amixer'
        self.executable(mixer, '''#!/bin/sh
case "$*" in
  *"sget Master"*) echo 'Front Left: 4';;
  *"cget numid=1"*) echo '  : values=100,100';;
  *"cget numid=2"*) echo '  : values=1,1';;
esac
''')
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('1')
        (self.run / 'shairport.pid').write_text(str(self.dummy_watchdog.pid))
        (self.safe / 'play.started').write_text('1')
        self.reserve_bluetooth()
        env = self.env | {'HK_AP2_AMIXER': str(mixer), 'HK_AP2_MAX_PLAY_SECONDS': '0',
                          'HK_AP2_PLAYBACK_SAFE': '1', 'HK_AP2_BT_PLAYBACK_SAFE': '1',
                          'PATH': str(self.base) + ':' + os.environ['PATH'],
                          'MOCK_CLOCK_COUNT': str(self.base / 'clock-count')}
        self.launch(['sh', str(WATCHDOG), '__watchdog'], env)
        eventually(lambda: (self.base / 'clock-count').exists())
        time.sleep(2)
        self.assertFalse((self.safe / 'tripped').exists(), 'a fresh heartbeat must not trip at a clock boundary')
        self.assertTrue((self.safe / 'play.started').exists())

    def test_watchdog_accepts_owner_removed_during_clean_release(self):
        self.executable(self.base / 'sed', '''#!/bin/sh
case "$3" in
  */bt-heartbeat)
    rm -f "$HK_AP2_RUN/safe/play.started" "$HK_AP2_RUN/bt-owner.pid" "$HK_AP2_RUN/bt-heartbeat"
    exit 0;;
esac
exec /usr/bin/sed "$@"
''')
        mixer = self.base / 'fake-amixer'
        self.executable(mixer, '''#!/bin/sh
case "$*" in
  *"sget Master"*) echo 'Front Left: 4';;
  *"cget numid=1"*) echo '  : values=100,100';;
  *"cget numid=2"*) echo '  : values=1,1';;
esac
''')
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        (self.run / 'shairport.pid').write_text(str(self.dummy_watchdog.pid))
        (self.safe / 'play.started').write_text('1')
        self.reserve_bluetooth()
        env = self.env | {'HK_AP2_AMIXER': str(mixer), 'HK_AP2_MAX_PLAY_SECONDS': '0',
                          'HK_AP2_PLAYBACK_SAFE': '1', 'HK_AP2_BT_PLAYBACK_SAFE': '1',
                          'PATH': str(self.base) + ':' + os.environ['PATH']}
        self.launch(['sh', str(WATCHDOG), '__watchdog'], env)
        eventually(lambda: not (self.run / 'bt-owner.pid').exists())
        time.sleep(1)
        self.assertFalse((self.safe / 'tripped').exists(), 'normal release must not latch a false trip')

    def test_watchdog_discards_lease_when_owner_was_removed_first(self):
        token = f'bt-{self.dummy_watchdog.pid}-0-1'
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        self.reserve_bluetooth(token)
        (self.run / 'bt-wait-pcm').write_text(token + '\n')
        (self.run / 'bt-owner.pid').unlink()
        (self.run / 'bt-heartbeat').unlink()
        self.launch_validation_watchdog()
        eventually(lambda: not (self.safe / 'lease').exists())
        eventually(
            lambda: (self.safe / 'actions.log').exists() and
            'Bluetooth release in progress' in
            (self.safe / 'actions.log').read_text()
        )
        self.assertFalse((self.safe / 'tripped').exists())

    def test_watchdog_discards_completed_release_with_only_play_marker_left(self):
        token = f'bt-{self.dummy_watchdog.pid}-0-1'
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        self.reserve_bluetooth(token)
        fields = (self.safe / 'lease').read_text().split()
        (self.run / 'bt-wait-pcm').write_text(token + '\n')
        (self.run / 'release-intent').write_text(
            f'{fields[0]} {fields[3]} {fields[4]}\n'
        )
        for name in ('safe/lease', 'bt-owner.pid', 'bt-heartbeat'):
            (self.run / name).unlink()
        self.launch_validation_watchdog()
        eventually(
            lambda: not (self.safe / 'play.started').exists() and
            (self.safe / 'actions.log').exists() and
            'source release in progress' in
            (self.safe / 'actions.log').read_text()
        )
        self.assertFalse((self.safe / 'tripped').exists())
        self.assertIn('source release in progress', (self.safe / 'actions.log').read_text())

    def test_watchdog_recovers_a_draining_lease_after_stop_was_killed(self):
        token = f'bt-{self.dummy_watchdog.pid}-0-1'
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        self.reserve_bluetooth(token)
        fields = (self.safe / 'lease').read_text().split()
        fields[-1] = 'draining'
        (self.safe / 'lease').write_text(' '.join(fields) + '\n')
        self.launch_validation_watchdog()
        eventually(lambda: not (self.safe / 'lease').exists())
        self.assertFalse((self.safe / 'tripped').exists())
        self.assertTrue((self.run / 'bt-wait-pcm').exists())
        eventually(
            lambda: 'draining source recovered'
            in (self.safe / 'actions.log').read_text()
        )

    def test_watchdog_recovers_an_expired_priming_lease_without_global_latch(self):
        token = f'bt-{self.dummy_watchdog.pid}-0-1'
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        self.reserve_bluetooth(token)
        (self.base / 'clock').write_text('12.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('12')
        self.launch_validation_watchdog()
        eventually(lambda: not (self.safe / 'lease').exists())
        eventually(lambda: 'stale priming lease cleared'
                   in (self.safe / 'actions.log').read_text())
        self.assertFalse((self.safe / 'tripped').exists())

    def test_watchdog_keeps_a_priming_lease_inside_its_grace_period(self):
        token = f'bt-{self.dummy_watchdog.pid}-0-1'
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        self.reserve_bluetooth(token)
        (self.base / 'clock').write_text('11.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('11')
        self.launch_validation_watchdog()
        time.sleep(1.2)
        self.assertTrue((self.safe / 'lease').exists())
        self.assertFalse((self.safe / 'tripped').exists())
        actions = self.safe / 'actions.log'
        self.assertNotIn(
            'stale priming lease cleared',
            actions.read_text() if actions.exists() else '',
        )

    def test_airplay_stop_between_marker_and_lease_read_is_recoverable(self):
        self.seed_airplay()
        sentinel = self.base / 'airplay-release-sampled'
        self.executable(self.base / 'sed', f'''#!/bin/sh
if [ "$3" = "$HK_AP2_RUN/safe/play.started" ] && [ ! -f "{sentinel}" ]; then
  /usr/bin/sed -n '1p' "$3"
  : >"{sentinel}"
  rm -f "$HK_AP2_RUN/safe/play.started" "$HK_AP2_RUN/safe/lease" \
    "$HK_AP2_RUN/safe/lease.request" "$HK_AP2_RUN/safe/lease.ack"
  exit 0
fi
exec /usr/bin/sed "$@"
''')
        self.env['PATH'] = str(self.base) + ':' + os.environ['PATH']
        self.launch_validation_watchdog()
        eventually(lambda: sentinel.exists())
        self.assertFalse((self.safe / 'play.started').exists())
        self.assertFalse((self.safe / 'lease').exists())
        eventually(
            lambda: (self.safe / 'actions.log').exists() and
            'recoverable' in (self.safe / 'actions.log').read_text()
        )
        self.assertFalse((self.safe / 'tripped').exists())

    def test_airplay_release_intent_bridges_partial_lease_cleanup(self):
        self.seed_airplay()
        fields = (self.safe / 'lease').read_text().split()
        (self.run / 'release-intent').write_text(
            f'{fields[0]} {fields[3]} {fields[4]}\n'
        )
        (self.safe / 'lease').unlink()
        self.launch_validation_watchdog()
        eventually(lambda: not (self.safe / 'play.started').exists())
        tripped = self.safe / 'tripped'
        actions = self.safe / 'actions.log'
        self.assertFalse(
            tripped.exists(),
            (tripped.read_text() if tripped.exists() else '')
            + '\n' + (actions.read_text() if actions.exists() else ''),
        )
        eventually(
            lambda: actions.exists() and
            'source release in progress' in actions.read_text()
        )

    def test_release_cleanup_discards_a_replacement_lease(self):
        self.seed_airplay()
        fields = (self.safe / 'lease').read_text().split()
        (self.run / 'release-intent').write_text(
            f'{fields[0]} {fields[3]} {fields[4]}\n'
        )
        (self.safe / 'lease').unlink()
        sentinel = self.base / 'replacement-lease-created'
        self.executable(self.base / 'mkdir', f'''#!/bin/sh
if [ "$1" = "$HK_AP2_RUN/speaker.lock" ] && [ ! -f "{sentinel}" ]; then
  : >"{sentinel}"
  printf 'airplay %s %s replacement-token %s priming\\n' \
    "{fields[1]}" "{fields[2]}" "{fields[4]}" \
    >"$HK_AP2_RUN/safe/lease.new"
  mv "$HK_AP2_RUN/safe/lease.new" "$HK_AP2_RUN/safe/lease"
fi
exec /usr/bin/mkdir "$@"
''')
        self.env['PATH'] = str(self.base) + ':' + os.environ['PATH']
        watchdog = self.launch_validation_watchdog()[0]
        deadline = time.monotonic() + 12
        while not sentinel.exists() and time.monotonic() < deadline:
            time.sleep(.1)
        actions = self.safe / 'actions.log'
        self.assertTrue(
            sentinel.exists(),
            'replacement was not observed; rc=%r actions=%r mock=%r'
            % (
                watchdog.poll(),
                actions.read_text() if actions.exists() else '',
                (self.base / 'mkdir').read_text(),
            ),
        )
        eventually(
            lambda: 'superseded' in (self.safe / 'actions.log').read_text()
            if (self.safe / 'actions.log').exists() else False
        )
        self.assertTrue((self.safe / 'lease').exists())
        self.assertIn('replacement-token', (self.safe / 'lease').read_text())
        self.assertFalse((self.safe / 'tripped').exists())

    def test_watchdog_recovers_an_orphaned_lease_without_a_play_marker(self):
        self.seed_airplay()
        (self.safe / 'play.started').unlink()
        self.launch_validation_watchdog()
        eventually(lambda: not (self.safe / 'lease').exists())
        self.assertFalse((self.safe / 'tripped').exists())
        self.assertFalse((self.run / 'ap2-claim').exists())
        self.assertIn(
            'orphaned lease without play marker recovered',
            (self.safe / 'actions.log').read_text(),
        )

    def test_watchdog_clears_an_owner_that_died_before_publishing_a_lease(self):
        dead = self.launch(['sleep', '60'])
        dead.terminate()
        dead.wait(timeout=4)
        (self.run / 'bt-owner.pid').write_text(str(dead.pid) + '\n')
        (self.run / 'bt-heartbeat').write_text('0\n')
        (self.run / 'bt-release').touch()
        self.launch_validation_watchdog()
        eventually(lambda: not (self.run / 'bt-owner.pid').exists())
        self.assertFalse((self.run / 'bt-heartbeat').exists())
        self.assertFalse((self.run / 'bt-release').exists())
        self.assertFalse((self.safe / 'tripped').exists())
        eventually(
            lambda: 'orphaned Bluetooth owner cleared'
            in (self.safe / 'actions.log').read_text()
        )

    def test_stale_release_intent_cannot_mask_an_unexplained_lease_loss(self):
        self.seed_airplay()
        fields = (self.safe / 'lease').read_text().split()
        (self.run / 'release-intent').write_text(
            f'{fields[0]} {fields[3]} 99\n'
        )
        (self.safe / 'lease').unlink()
        self.launch_validation_watchdog()
        eventually(lambda: (self.safe / 'tripped').exists())
        self.assertEqual((self.safe / 'tripped').read_text().strip(), 'invalid playback lease')

    def test_airplay_clock_loss_after_marker_read_does_not_trip_after_release(self):
        self.seed_airplay()
        sentinel = self.base / 'airplay-clock-release-sampled'
        self.executable(self.base / 'sed', f'''#!/bin/sh
if [ "$3" = "$HK_AP2_PCM_STATUS" ] && [ ! -f "{sentinel}" ]; then
  : >"{sentinel}"
  echo 'state: STOPPED'
  echo 'owner_pid   : 99'
  rm -f "$HK_AP2_RUN/safe/play.started" "$HK_AP2_RUN/safe/lease" \
    "$HK_AP2_RUN/safe/lease.request" "$HK_AP2_RUN/safe/lease.ack"
  exit 0
fi
exec /usr/bin/sed "$@"
''')
        self.env['PATH'] = str(self.base) + ':' + os.environ['PATH']
        self.launch_validation_watchdog()
        eventually(lambda: sentinel.exists())
        eventually(
            lambda: (self.safe / 'actions.log').exists() and
            'previous fault discarded' in
            (self.safe / 'actions.log').read_text()
        )
        self.assertFalse((self.safe / 'tripped').exists())
        self.assertFalse((self.safe / 'lease').exists())

    def test_clock_fault_from_old_generation_cannot_delete_a_new_lease(self):
        self.seed_airplay()
        (self.base / 'clock').write_text('10.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('10\n')
        sentinel = self.base / 'new-airplay-generation'
        self.executable(self.base / 'sed', f'''#!/bin/sh
if [ "$3" = "$HK_AP2_PCM_STATUS" ] && [ ! -f "{sentinel}" ]; then
  : >"{sentinel}"
  read _source _pid _start _old_token _old_time _old_phase \
    <"$HK_AP2_RUN/safe/lease"
  _new_time=$((_old_time + 1))
  _new_token="new-$_new_time"
  printf '%s %s %s %s %s active\\n' \
    "$_source" "$_pid" "$_start" "$_new_token" "$_new_time" \
    >"$HK_AP2_RUN/safe/lease.new"
  mv "$HK_AP2_RUN/safe/lease.new" "$HK_AP2_RUN/safe/lease"
  printf '%s\\n' "$_new_time" >"$HK_AP2_RUN/safe/play.started"
  echo 'state: STOPPED'
  echo 'owner_pid   : 99'
  exit 0
fi
exec /usr/bin/sed "$@"
''')
        self.env['PATH'] = str(self.base) + ':' + os.environ['PATH']
        self.launch_validation_watchdog()
        eventually(lambda: sentinel.exists())
        lease = self.safe / 'lease'
        eventually(lambda: lease.exists() and lease.read_text().split()[3].startswith('new-'))
        fields = lease.read_text().split()
        self.assertEqual((self.safe / 'play.started').read_text().strip(), fields[4])
        eventually(
            lambda: (self.safe / 'actions.log').exists() and
            'previous fault discarded' in
            (self.safe / 'actions.log').read_text()
        )
        self.assertFalse((self.safe / 'tripped').exists())

    def test_old_bluetooth_marker_cannot_mask_invalid_airplay_generation(self):
        (self.run / 'shairport.pid').write_text(str(self.dummy_watchdog.pid))
        shell = '. "$HK_AUDIO_COMMON"; SAFE=$HK_AP2_RUN/safe; RUN=$HK_AP2_RUN; audio_reserve'
        subprocess.run(['sh', '-c', shell], env=self.env, check=True, capture_output=True)
        lease = self.safe / 'lease'
        fields = lease.read_text().split()
        fields[-1] = 'corrupt'
        lease.write_text(' '.join(fields) + '\n')
        (self.run / 'bt-wait-pcm').write_text(
            f'bt-{self.dummy_watchdog.pid}-99-1\n'
        )
        self.launch_validation_watchdog()
        eventually(lambda: (self.safe / 'tripped').exists())
        self.assertEqual((self.safe / 'tripped').read_text().strip(), 'invalid playback lease')

    def test_stale_bluetooth_marker_does_not_hide_a_dead_bridge_heartbeat(self):
        token = f'bt-{self.dummy_watchdog.pid}-0-1'
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        self.reserve_bluetooth(token)
        (self.run / 'bt-wait-pcm').write_text(
            f'bt-{self.dummy_watchdog.pid}-99-1\n'
        )
        (self.base / 'clock').write_text('10.00 0.00\n')
        self.launch_validation_watchdog()
        eventually(lambda: (self.safe / 'tripped').exists())
        self.assertEqual((self.safe / 'tripped').read_text().strip(), 'Bluetooth bridge lost')

    def test_current_release_token_allows_live_bridge_stop_with_stale_heartbeat(self):
        token = f'bt-{self.dummy_watchdog.pid}-0-1'
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        self.reserve_bluetooth(token)
        (self.run / 'bt-wait-pcm').write_text(token + '\n')
        fields = (self.safe / 'lease').read_text().split()
        (self.run / 'release-intent').write_text(
            f'{fields[0]} {fields[3]} {fields[4]}\n'
        )
        (self.base / 'clock').write_text('10.00 0.00\n')
        self.launch_validation_watchdog()
        eventually(lambda: not (self.safe / 'lease').exists())
        self.assertFalse((self.safe / 'tripped').exists())
        eventually(
            lambda: 'Bluetooth release in progress'
            in (self.safe / 'actions.log').read_text()
        )

    def test_hook_child_that_ignores_term_is_killed_with_a_bound(self):
        child = subprocess.Popen(
            ['sh', '-c', 'trap "" TERM; exec sleep 30'],
            start_new_session=True,
        )
        try:
            source = BRIDGE.read_text()
            function = 'terminate_hook_child() {' + source.split(
                'terminate_hook_child() {', 1
            )[1].split('\nrun_hook() {', 1)[0]
            shell = function + '''
audio_delay() { sleep 0.05; }
child=$CHILD_PID
terminate_hook_child
'''
            before = time.monotonic()
            result = subprocess.run(
                ['sh', '-c', shell],
                env=self.env | {'CHILD_PID': str(child.pid),
                                'PATH': os.environ['PATH']},
                capture_output=True, text=True, timeout=5,
            )
            elapsed = time.monotonic() - before
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLess(elapsed, 3.5)
            child.wait(timeout=2)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_corrupt_bluetooth_lease_with_unrelated_marker_still_latches(self):
        token = f'bt-{self.dummy_watchdog.pid}-0-1'
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        self.reserve_bluetooth(token)
        lease = self.safe / 'lease'
        fields = lease.read_text().split()
        fields[-1] = 'corrupt'
        lease.write_text(' '.join(fields) + '\n')
        (self.run / 'bt-wait-pcm').write_text(
            f'bt-{self.dummy_watchdog.pid}-99-1\n'
        )
        self.launch_validation_watchdog()
        eventually(lambda: (self.safe / 'tripped').exists())
        self.assertEqual((self.safe / 'tripped').read_text().strip(), 'Bluetooth bridge lost')

    def test_old_bluetooth_marker_without_exact_release_intent_still_latches(self):
        token = f'bt-{self.dummy_watchdog.pid}-0-1'
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        self.reserve_bluetooth(token)
        (self.run / 'bt-wait-pcm').write_text(token + '\n')
        (self.safe / 'lease').unlink()
        self.launch_validation_watchdog()
        eventually(lambda: (self.safe / 'tripped').exists())
        self.assertEqual((self.safe / 'tripped').read_text().strip(), 'Bluetooth bridge lost')

    def test_control_file_symlinks_are_not_accepted(self):
        target = self.base / 'control-target'
        target.write_text('airplay 1 1 token 1 active\n')
        (self.run / 'release-intent').symlink_to(target)
        request_target = self.base / 'request-target'
        request_target.write_text('token nonce\n')
        (self.safe / 'lease.request').symlink_to(request_target)
        definitions = WATCHDOG.read_text().split('mixer_set()', 1)[0]
        shell = definitions + '''
read_release_intent && exit 10
read_lease_request && exit 11
exit 0
'''
        result = subprocess.run(['sh', '-c', shell], env=self.env,
                                capture_output=True, text=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_control_file_fifo_does_not_block_reader(self):
        fifo = self.safe / 'watchdog.heartbeat'
        fifo.unlink()
        os.mkfifo(fifo)
        result = subprocess.run(
            ['sh', '-c', '. "$HK_AUDIO_COMMON"; audio_control_read "$1"',
             'sh', str(fifo)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=2,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')

    def test_live_airplay_claim_keeps_priority_past_legacy_timeout(self):
        start = subprocess.check_output(
            ['sh', '-c', '. "$HK_AUDIO_COMMON"; audio_pid_start "$1"', 'sh',
             str(os.getpid())],
            env=self.env,
            text=True,
        ).strip()
        self.assertTrue(start)
        (self.base / 'clock').write_text('100.00 0.00\n')
        (self.run / 'ap2-claim').write_text(
            f'80 {os.getpid()} {start} {os.getpid()}-80\n'
        )
        result = subprocess.run(
            ['sh', '-c',
             '. "$HK_AUDIO_COMMON"; audio_claim_recent "$HK_AP2_RUN/ap2-claim"'],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_dead_or_legacy_expired_claim_is_not_recent(self):
        (self.base / 'clock').write_text('100.00 0.00\n')
        (self.run / 'ap2-claim').write_text('80 999999999 1 dead-claim\n')
        dead = subprocess.run(
            ['sh', '-c',
             '. "$HK_AUDIO_COMMON"; audio_claim_recent "$HK_AP2_RUN/ap2-claim"'],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(dead.returncode, 0)
        (self.run / 'ap2-claim').write_text('80\n')
        legacy = subprocess.run(
            ['sh', '-c',
             '. "$HK_AUDIO_COMMON"; audio_claim_recent "$HK_AP2_RUN/ap2-claim"'],
            env=self.env,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(legacy.returncode, 0)

    def test_stale_trip_recheck_cannot_delete_a_new_session(self):
        sentinel = self.base / 'trip-lock-sampled'
        proceed = self.base / 'trip-lock-proceed'
        mkdir = self.base / 'mkdir'
        real_mkdir = subprocess.check_output(
            ['sh', '-c', 'command -v mkdir'], text=True
        ).strip()
        self.executable(mkdir, f'''#!/bin/sh
if [ "$1" = "$HK_AP2_RUN/speaker.lock" ] && [ ! -f "{sentinel}" ]; then
  : >"{sentinel}"
  while [ ! -f "{proceed}" ]; do sleep .01; done
fi
exec "{real_mkdir}" "$@"
''')
        self.env['PATH'] = str(self.base) + ':' + os.environ['PATH']
        (self.base / 'clock').write_text('10.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('10\n')
        (self.safe / 'tripped').write_text('maintenance')
        self.launch_validation_watchdog()
        eventually(lambda: sentinel.exists())

        # The operator clears maintenance and a new source publishes while the
        # watchdog is still committed to examining the old trip file.
        (self.safe / 'tripped').unlink()
        token = f'bt-{self.dummy_watchdog.pid}-10-1'
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('10')
        self.reserve_bluetooth(token)
        fields = (self.safe / 'lease').read_text().split()
        fields[-1] = 'active'
        replacement = self.safe / 'lease.replacement'
        replacement.write_text(' '.join(fields) + '\n')
        replacement.replace(self.safe / 'lease')
        proceed.touch()

        time.sleep(2)
        self.assertFalse((self.safe / 'tripped').exists())
        self.assertTrue((self.safe / 'lease').exists())
        self.assertEqual((self.safe / 'play.started').read_text().strip(),
                         fields[4])

    def test_old_invalid_mixer_sample_cannot_latch_a_replacement_lease(self):
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('10')
        (self.base / 'clock').write_text('10.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('10\n')
        old_token = f'bt-{self.dummy_watchdog.pid}-10-old'
        self.reserve_bluetooth(old_token)
        fields = (self.safe / 'lease').read_text().split()
        fields[-1] = 'active'
        (self.safe / 'lease').write_text(' '.join(fields) + '\n')

        sentinel = self.base / 'old-mixer-sampled'
        proceed = self.base / 'old-mixer-proceed'
        count = self.base / 'old-mixer-count'
        mixer = self.base / 'stale-mixer-amixer'
        self.executable(mixer, f'''#!/bin/sh
case "$*" in
  *"sget Master"*)
    n=$(cat "{count}" 2>/dev/null || echo 0)
    n=$((n + 1)); echo "$n" >"{count}"
    if [ "$n" = 1 ] && [ ! -f "{sentinel}" ]; then
      : >"{sentinel}"
      while [ ! -f "{proceed}" ]; do sleep .01; done
    fi
    [ "$n" -le 2 ] || echo 'Front Left: 4'
    ;;
  *"cget numid=1"*) echo '  : values=100,100';;
  *"cget numid=2"*) echo '  : values=1,1';;
  *"sget Ext Spk"*) echo 'Front Left: Playback [on]';;
esac
''')
        env = self.env | {
            'HK_AP2_AMIXER': str(mixer), 'HK_AP2_MAX_PLAY_SECONDS': '0',
            'HK_AP2_BT_DIGITAL_SAFE': '207', 'HK_AP2_BT_PLAYBACK_SAFE': '1',
        }
        self.launch(['sh', str(WATCHDOG), '__watchdog'], env)
        eventually(lambda: sentinel.exists())
        fields = (self.safe / 'lease').read_text().split()
        fields[3] = 'replacement-generation'
        replacement = self.safe / 'lease.replacement'
        replacement.write_text(' '.join(fields) + '\n')
        replacement.replace(self.safe / 'lease')
        proceed.touch()

        eventually(
            lambda: (self.safe / 'actions.log').exists() and
            'invalid Master reading superseded' in
            (self.safe / 'actions.log').read_text()
        )
        self.assertFalse((self.safe / 'tripped').exists())
        self.assertTrue((self.safe / 'lease').exists())
        self.assertIn('replacement-generation',
                      (self.safe / 'lease').read_text())

    def test_bluetooth_lease_does_not_require_shairport(self):
        mixer = self.base / 'fake-amixer'
        self.executable(mixer, '''#!/bin/sh
case "$*" in
  *"sget Master"*) echo 'Front Left: 17';;
  *"cget numid=1"*) echo '  : values=207,207';;
  *"cget numid=2"*) echo '  : values=0,0';;
esac
''')
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        (self.safe / 'play.started').write_text('1')
        self.reserve_bluetooth()
        env = self.env | {
            'HK_AP2_AMIXER': str(mixer), 'HK_AP2_MAX_PLAY_SECONDS': '0',
            'HK_AP2_BT_DIGITAL_SAFE': '207', 'HK_AP2_BT_PLAYBACK_SAFE': '0',
        }
        self.launch(['sh', str(WATCHDOG), '__watchdog'], env)
        eventually(lambda: (self.safe / 'watchdog.heartbeat').exists())
        time.sleep(3)
        self.assertFalse(
            (self.safe / 'tripped').exists(),
            'Bluetooth must survive Shairport being recycled by the memory guard',
        )
        self.assertTrue((self.safe / 'play.started').exists())
        self.assertTrue((self.run / 'bt-owner.pid').exists())

    def test_watchdog_allows_bluetooth_master_above_four(self):
        mixer = self.base / 'fake-amixer'
        self.executable(mixer, '''#!/bin/sh
echo "$*" >>"$MOCK_MIXER_LOG"
case "$*" in
  *"sget Master"*) echo 'Front Left: 17';;
  *"cget numid=1"*) echo '  : values=100,100';;
  *"cget numid=2"*) echo '  : values=1,1';;
esac
''')
        mixer_log = self.base / 'mixer.log'
        mixer_log.touch()
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        (self.run / 'shairport.pid').write_text(str(self.dummy_watchdog.pid))
        (self.safe / 'play.started').write_text('1')
        self.reserve_bluetooth()
        env = self.env | {'HK_AP2_AMIXER': str(mixer), 'MOCK_MIXER_LOG': str(mixer_log),
                          'HK_AP2_MAX_PLAY_SECONDS': '0', 'HK_AP2_MASTER_SAFE': '4',
                          'HK_AP2_PLAYBACK_SAFE': '1', 'HK_AP2_BT_PLAYBACK_SAFE': '1'}
        self.launch(['sh', str(WATCHDOG), '__watchdog'], env)
        eventually(lambda: 'sget Master' in mixer_log.read_text())
        self.assertNotIn('sset Master 4', mixer_log.read_text())
        self.assertFalse((self.safe / 'tripped').exists())

    def test_watchdog_retries_one_transient_invalid_master_reading(self):
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('10')
        (self.base / 'clock').write_text('10.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('10')
        self.reserve_bluetooth()
        fields = (self.safe / 'lease').read_text().split()
        fields[-1] = 'active'
        (self.safe / 'lease').write_text(' '.join(fields) + '\n')
        count = self.base / 'master-count'
        mixer = self.base / 'transient-master-amixer'
        self.executable(mixer, f'''#!/bin/sh
case "$*" in
  *"sget Master"*)
    n=$(cat "{count}" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" >"{count}"
    [ "$n" -gt 1 ] && echo 'Front Left: 4'
    ;;
  *"cget numid=1"*) echo '  : values=100,100';;
  *"cget numid=2"*) echo '  : values=0,0';;
  *"sget Ext Spk"*) echo 'Front Left: Playback [on]';;
esac
''')
        env = self.env | {
            'HK_AP2_AMIXER': str(mixer), 'HK_AP2_MAX_PLAY_SECONDS': '0',
            'HK_AP2_BT_DIGITAL_SAFE': '207', 'HK_AP2_BT_PLAYBACK_SAFE': '0',
        }
        self.launch(['sh', str(WATCHDOG), '__watchdog'], env)
        eventually(lambda: count.exists() and int(count.read_text()) >= 2)
        time.sleep(1)
        self.assertFalse((self.safe / 'tripped').exists())
        self.assertTrue((self.safe / 'lease').exists())

    def test_watchdog_retries_one_transient_invalid_digital_reading(self):
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('10')
        (self.base / 'clock').write_text('10.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('10')
        self.reserve_bluetooth()
        fields = (self.safe / 'lease').read_text().split()
        fields[-1] = 'active'
        (self.safe / 'lease').write_text(' '.join(fields) + '\n')
        count = self.base / 'digital-count'
        mixer = self.base / 'transient-digital-amixer'
        self.executable(mixer, f'''#!/bin/sh
case "$*" in
  *"sget Master"*) echo 'Front Left: 4';;
  *"cget numid=1"*)
    n=$(cat "{count}" 2>/dev/null || echo 0)
    n=$((n + 1))
    echo "$n" >"{count}"
    [ "$n" -gt 1 ] && echo '  : values=100,100'
    ;;
  *"cget numid=2"*) echo '  : values=0,0';;
  *"sget Ext Spk"*) echo 'Front Left: Playback [on]';;
esac
''')
        env = self.env | {
            'HK_AP2_AMIXER': str(mixer), 'HK_AP2_MAX_PLAY_SECONDS': '0',
            'HK_AP2_BT_DIGITAL_SAFE': '207', 'HK_AP2_BT_PLAYBACK_SAFE': '0',
        }
        self.launch(['sh', str(WATCHDOG), '__watchdog'], env)
        eventually(lambda: count.exists() and int(count.read_text()) >= 2)
        time.sleep(1)
        self.assertFalse((self.safe / 'tripped').exists())
        self.assertTrue((self.safe / 'lease').exists())

    def test_priming_exception_rechecks_both_gates_before_accepting_old_analog(self):
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('10')
        (self.base / 'clock').write_text('10.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('10')
        self.reserve_bluetooth()
        digital_count = self.base / 'priming-digital-count'
        mixer_log = self.base / 'priming-mixer.log'
        mixer = self.base / 'priming-amixer'
        self.executable(mixer, f'''#!/bin/sh
echo "$*" >>"{mixer_log}"
case "$*" in
  *"sget Master"*) echo 'Front Left: 4';;
  *"cget numid=1"*)
    n=$(cat "{digital_count}" 2>/dev/null || echo 0)
    n=$((n + 1)); echo "$n" >"{digital_count}"
    if [ "$n" -le 2 ]; then echo '  : values=0,0'; else echo '  : values=100,100'; fi
    ;;
  *"cget numid=2"*) echo '  : values=1,1';;
  *"sget Ext Spk"*) echo 'Front Left: Playback [off]';;
esac
''')
        env = self.env | {
            'HK_AP2_AMIXER': str(mixer), 'HK_AP2_MAX_PLAY_SECONDS': '0',
            'HK_AP2_BT_DIGITAL_SAFE': '207',
            'HK_AP2_BT_PLAYBACK_SAFE': '0',
        }
        self.launch(['sh', str(WATCHDOG), '__watchdog'], env)
        eventually(lambda: (self.safe / 'tripped').exists())
        self.assertEqual(
            (self.safe / 'tripped').read_text().strip(),
            'analog baseline changed',
        )
        self.assertNotIn('cset numid=2', mixer_log.read_text())

    def test_bridge_closes_output_when_watchdog_dies(self):
        self.state('play')
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: self.events().count('start') == 1)
        self.dummy_watchdog.kill()
        self.dummy_watchdog.wait()
        eventually(lambda: 'stop' in self.events() and not (self.run / 'bt-owner.pid').exists())

    def test_duplicate_bridge_cannot_replace_the_owner(self):
        self.state('play')
        first = self.launch(['sh', str(BRIDGE), '__run'])
        pidfile = self.run / 'bt-bridge.pid'
        eventually(lambda: pidfile.exists() and bool(pidfile.read_text().strip()))
        eventually(lambda: self.events().count('start') == 1)
        owner_pid = pidfile.read_text().strip()
        second = self.launch(['sh', str(BRIDGE), '__run'])
        second.wait(timeout=8)
        self.assertNotEqual(second.returncode, 0)
        self.assertIsNone(first.poll())
        self.assertEqual(pidfile.read_text().strip(), owner_pid)
        self.assertNotIn('stop', self.events())
        self.assertFalse((self.safe / 'tripped').exists())
        self.assertIn(
            'duplicate Bluetooth bridge refused',
            (self.run / 'bt-bridge.log').read_text(),
        )

    def test_manual_mute_blocks_bluetooth_open(self):
        self.state('play')
        (self.safe / 'tripped').write_text('manual mute\n')
        self.launch(['sh', str(BRIDGE), '__run'])
        time.sleep(3)
        self.assertNotIn('start', self.events())
        self.assertFalse((self.run / 'bt-owner.pid').exists())

    def test_pause_during_start_closes_output_and_can_retry(self):
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
if [ "$1" = start ]; then
  sleep 3
  echo 0 >"$HK_AP2_RUN/safe/play.started"
else
  rm -f "$HK_AP2_RUN/safe/play.started"
fi
''')
        self.state('play')
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: self.events().count('start') == 1)
        self.state('stop')
        eventually(lambda: 'stop' in self.events() and not (self.run / 'bt-owner.pid').exists())
        self.assertFalse((self.safe / 'tripped').exists(), 'a source pause should not latch manual mute')
        self.state('play')
        eventually(lambda: self.events().count('start') == 2)
        eventually(lambda: (self.safe / 'play.started').exists())
        self.assertFalse((self.safe / 'tripped').exists())

    def test_cold_start_accepts_fresh_virtual_frames_while_hardware_is_closed(self):
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
[ "$1" != start ] || sleep 0.4
''')
        activity = self.base / 'activity'
        self.env['MOCK_ACTIVITY'] = str(activity)
        self.activity_socket = socket.socket(socket.AF_UNIX)
        self.activity_socket.bind(str(self.base / 'pcm-control.sock'))
        self.env['HK_BT_PCM_CONTROL'] = str(self.base / 'pcm-control.sock')
        self.pcm.write_text('state: PREPARED\nowner_pid   : 42\n')
        self.state('play')
        holder = self.proc / str(self.dummy_watchdog.pid)
        holder.mkdir(exist_ok=True)
        (holder / 'comm').write_text('a01controller\n')
        self.set_activity(5000, 1, instance=12345)
        (self.base / 'clock').write_text('1.00 0.00\n')

        stop = threading.Event()

        def advance_frames():
            frames = 5000
            while not stop.wait(.05):
                frames += 128
                self.set_activity(frames, 1, instance=12345)

        thread = threading.Thread(target=advance_frames)
        thread.start()
        definitions = BRIDGE.read_text().split('trap cleanup EXIT', 1)[0]
        try:
            start = subprocess.run(
                ['sh', '-c', definitions + '''
audio_delay() { sleep 0.05; }
vol=12
BT_TOKEN="bt-cold-start"
printf "%s\\n" "$$" >"$OWNER"
wait_activity_pid=$START_PID
wait_activity_start=$(audio_pid_start "$wait_activity_pid") || exit 1
wait_activity_instance=12345
wait_activity_frames=5000
run_hook start
''',],
                env=self.env | {
                    'START_PID': str(self.dummy_watchdog.pid),
                },
                capture_output=True, text=True, timeout=8,
            )
        finally:
            stop.set()
            thread.join(2)
        self.assertEqual(
            start.returncode, 0, start.stdout + '\n' + start.stderr)
        self.assertEqual(self.events().count('start'), 1)
        self.assertFalse((self.safe / 'tripped').exists())
        self.assertTrue((self.run / 'bt-owner.pid').read_text().strip().isdigit())

    def virtual_start_environment(self):
        self.pcm.write_text('state: PREPARED\nowner_pid   : 42\n')
        self.state('play')
        holder = self.proc / str(self.dummy_watchdog.pid)
        holder.mkdir(exist_ok=True)
        (holder / 'comm').write_text('a01controller\n')
        self.activity_socket = socket.socket(socket.AF_UNIX)
        self.activity_socket.bind(str(self.base / 'pcm-control.sock'))
        self.env['HK_BT_PCM_CONTROL'] = str(self.base / 'pcm-control.sock')
        self.env['MOCK_ACTIVITY'] = str(self.base / 'activity')
        self.env['HK_BT_DEBUG_HANDOFF'] = '1'
        (self.base / 'clock').write_text('1.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('1\n')

    def isolated_start(self, extra_env=None, timeout=10):
        definitions = BRIDGE.read_text().split('trap cleanup EXIT', 1)[0]
        command = definitions + '''
audio_delay() { sleep 0.05; }
HOOK_POLL_LIMIT=120
vol=12
BT_TOKEN="bt-start-test"
printf "%s\\n" "$$" >"$OWNER"
wait_activity_pid=$START_PID
wait_activity_start=$(audio_pid_start "$wait_activity_pid") || exit 1
wait_activity_instance=12345
wait_activity_frames=5000
run_hook start
rc=$?
printf 'RUN_HOOK_RC=%s\\n' "$rc"
exit "$rc"
'''
        return subprocess.run(
            ['sh', '-c', command],
            env=self.env | {'START_PID': str(self.dummy_watchdog.pid)}
            | (extra_env or {}),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_cold_start_tolerates_transient_status_rpc_failures(self):
        self.virtual_start_environment()
        self.set_activity(5000, 1, instance=12345)
        failures = self.base / 'status-fail-count'
        failures.write_text('0\n')
        self.executable(self.root / 'bin/hk-unix-request-raw', f'''#!/bin/sh
if [ "$2" = getPlayerStatus ]; then
  count=$(cat "{failures}" 2>/dev/null || echo 0)
  if [ "$count" -lt 4 ]; then
    echo $((count + 1)) >"{failures}"
    exit 1
  fi
fi
if [ "$2" = activity ]; then
  cat "$MOCK_ACTIVITY"
else
  cat "$MOCK_STATUS"
fi
''')
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
[ "$1" != start ] || sleep 1.5
''')

        result = self.isolated_start()

        self.assertEqual(result.returncode, 0,
                         result.stdout + '\n' + result.stderr + '\n' +
                         (self.run / 'bt-bridge.log').read_text())
        self.assertEqual(failures.read_text().strip(), '4')
        self.assertIn('RUN_HOOK_RC=0', result.stdout)
        self.assertIn('source check missed count=4',
                      (self.run / 'bt-bridge.log').read_text())
        self.assertEqual(self.events().count('start'), 1)
        self.assertFalse((self.safe / 'tripped').exists())

    def test_cold_start_tolerates_brief_flat_virtual_frame_count(self):
        self.virtual_start_environment()
        self.set_activity(5000, 1, instance=12345)
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
[ "$1" != start ] || sleep 1.5
''')
        process = subprocess.Popen(
            ['sh', '-c', BRIDGE.read_text().split(
                'trap cleanup EXIT', 1
            )[0] + '''
audio_delay() { sleep 0.05; }
HOOK_POLL_LIMIT=120
vol=12
BT_TOKEN="bt-flat-start"
printf "%s\\n" "$$" >"$OWNER"
wait_activity_pid=$START_PID
wait_activity_start=$(audio_pid_start "$wait_activity_pid") || exit 1
wait_activity_instance=12345
wait_activity_frames=5000
run_hook start
'''],
            env=self.env | {'START_PID': str(self.dummy_watchdog.pid)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.children.append(process)
        eventually(lambda: 'start' in self.events())

        # Frame-count sampling can be briefly flat while the DSP slave is
        # rebuilt. Keep the same live virtual PCM and factory play state.
        (self.base / 'clock').write_text('2.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('2\n')
        self.set_activity(5000, 2, instance=12345)
        time.sleep(.15)
        (self.base / 'clock').write_text('3.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('3\n')
        self.set_activity(5000, 3, instance=12345)
        time.sleep(.15)
        (self.base / 'clock').write_text('4.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('4\n')
        self.set_activity(5128, 4, instance=12345)

        stdout, stderr = process.communicate(timeout=8)
        bridge_log = self.run / 'bt-bridge.log'
        self.assertEqual(
            process.returncode, 0,
            stdout + '\n' + stderr + '\n' +
            (bridge_log.read_text() if bridge_log.exists() else '<no log>'),
        )
        self.assertEqual(self.events().count('start'), 1)
        self.assertFalse((self.safe / 'tripped').exists())

    def test_stalled_virtual_frames_close_output_without_global_latch(self):
        self.virtual_start_environment()
        self.set_activity(5000, 1, instance=12345)
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
[ "$1" != start ] || sleep 8
''')
        definitions = BRIDGE.read_text().split('trap cleanup EXIT', 1)[0]
        stop = threading.Event()

        def keep_clock_fresh_but_do_not_advance_frames():
            tick = 2
            while not stop.wait(.12):
                (self.base / 'clock').write_text(f'{tick}.00 0.00\n')
                (self.safe / 'watchdog.heartbeat').write_text(f'{tick}\n')
                self.set_activity(5000, tick, instance=12345)
                tick += 1

        thread = threading.Thread(
            target=keep_clock_fresh_but_do_not_advance_frames,
            daemon=True,
        )
        thread.start()
        try:
            result = subprocess.run(
                ['sh', '-c', definitions + '''
audio_delay() { sleep 0.05; }
vol=12
BT_TOKEN="bt-stalled-start"
printf "%s\\n" "$$" >"$OWNER"
wait_activity_pid=$START_PID
wait_activity_start=$(audio_pid_start "$wait_activity_pid") || exit 1
wait_activity_instance=12345
wait_activity_frames=5000
run_hook start
saved_rc=$?
[ "$saved_rc" = 10 ] || exit 20
active=1
release_output
printf 'RUN_HOOK_RC=%s\\n' "$saved_rc"
'''],
                env=self.env | {'START_PID': str(self.dummy_watchdog.pid)},
                capture_output=True,
                text=True,
                timeout=10,
            )
        finally:
            stop.set()
            thread.join(2)

        self.assertEqual(
            result.returncode, 0,
            result.stdout + '\n' + result.stderr + '\n' +
            (self.run / 'bt-bridge.log').read_text(),
        )
        self.assertIn(
            'RUN_HOOK_RC=10', result.stdout,
            result.stdout + '\n' + result.stderr + '\n' +
            (self.run / 'bt-bridge.log').read_text(),
        )
        self.assertIn(
            'Bluetooth source was not sustained during start; output closed',
            (self.run / 'bt-bridge.log').read_text(),
        )
        self.assertIn('stop', self.events())
        self.assertIn('park', self.events())
        self.assertNotIn('mute', self.events())
        self.assertFalse((self.safe / 'tripped').exists())

    def test_stale_watchdog_heartbeat_prevents_recoverable_start_retry(self):
        self.virtual_start_environment()
        (self.base / 'clock').write_text('10.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('0\n')
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
[ "$1" != start ] || exec sleep 8
''')
        definitions = BRIDGE.read_text().split('trap cleanup EXIT', 1)[0]
        result = subprocess.run(
            ['sh', '-c', definitions + '''
audio_delay() { sleep 0.01; }
HOOK_POLL_LIMIT=1200
vol=12
BT_TOKEN="bt-stale-watchdog-start"
printf "%s\\n" "$$" >"$OWNER"
wait_activity_pid=$START_PID
wait_activity_start=$(audio_pid_start "$wait_activity_pid") || exit 1
wait_activity_instance=12345
wait_activity_frames=5000
run_hook start
saved_rc=$?
[ "$saved_rc" = 1 ] || exit 20
"$TEST" mute
printf 'RUN_HOOK_RC=%s\\n' "$saved_rc"
'''],
            env=self.env | {'START_PID': str(self.dummy_watchdog.pid)},
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(
            result.returncode, 0,
            result.stdout + '\n' + result.stderr + '\n' +
            (self.run / 'bt-bridge.log').read_text(),
        )
        self.assertIn('RUN_HOOK_RC=1', result.stdout)
        self.assertEqual(
            (self.safe / 'tripped').read_text().strip(), 'manual')

    def test_transition_claim_allows_recoverable_start_close(self):
        self.virtual_start_environment()
        (self.base / 'clock').write_text('10.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('4\n')
        start = subprocess.run(
            ['sh', '-c', '. "$HK_AUDIO_COMMON"; audio_pid_start "$1"',
             'sh', str(self.dummy_watchdog.pid)],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        self.assertTrue(start)
        (self.run / 'ap2-claim').write_text(
            f'10 {self.dummy_watchdog.pid} {start} bt-start-transition\n')
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
[ "$1" != start ] || exec sleep 8
''')
        definitions = BRIDGE.read_text().split('trap cleanup EXIT', 1)[0]
        result = subprocess.run(
            ['sh', '-c', definitions + '''
audio_delay() { sleep 0.01; }
HOOK_POLL_LIMIT=1200
vol=12
BT_TOKEN="bt-transition-start"
printf "%s\\n" "$$" >"$OWNER"
wait_activity_pid=$START_PID
wait_activity_start=$(audio_pid_start "$wait_activity_pid") || exit 1
wait_activity_instance=12345
wait_activity_frames=5000
run_hook start
saved_rc=$?
[ "$saved_rc" = 10 ] || exit 20
active=1
release_output
printf 'RUN_HOOK_RC=%s\\n' "$saved_rc"
'''],
            env=self.env | {'START_PID': str(self.dummy_watchdog.pid)},
            capture_output=True,
            text=True,
            timeout=8,
        )
        self.assertEqual(
            result.returncode, 0,
            result.stdout + '\n' + result.stderr + '\n' +
            (self.run / 'bt-bridge.log').read_text(),
        )
        self.assertIn('RUN_HOOK_RC=10', result.stdout)
        self.assertIn('stop', self.events())
        self.assertIn('park', self.events())
        self.assertNotIn('mute', self.events())
        self.assertFalse((self.safe / 'tripped').exists())

    def test_park_failure_after_start_still_latches_global_mute(self):
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
case "$1" in
  start)
    SAFE=$HK_AP2_RUN/safe; RUN=$HK_AP2_RUN
    . "$HK_AUDIO_COMMON"
    HK_AP2_BT=1
    audio_reserve || exit 1
    lease_phase=active
    audio_write_lease || exit 1
    ;;
  stop)
    rm -f "$HK_AP2_RUN/safe/play.started" "$HK_AP2_RUN/safe/lease"
    ;;
  park)
    exit 1
    ;;
esac
''')
        self.state('play')
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: self.events().count('start') == 1)
        eventually(lambda: (self.safe / 'lease').exists())
        self.state('stop')
        eventually(lambda: (self.safe / 'tripped').exists())
        self.assertEqual(
            (self.safe / 'tripped').read_text().strip(), 'manual')
        self.assertIn('park', self.events())
        eventually(
            lambda: 'Bluetooth DSP release failed; manual mute latched'
            in (self.run / 'bt-bridge.log').read_text()
        )

    def test_bluetooth_volume_above_30_opens_without_clamping(self):
        self.status.write_text('{"mode":"41","status":"play","vol":"52","mute":"0"}')
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: 'start' in self.events())
        self.assertNotIn('clamp', self.events())
        self.assertEqual((self.base / 'master').read_text().strip(), '17')
        self.assertEqual((self.base / 'levels').read_text().strip(), '207,0',
                         'Bluetooth must use digital unity and the stock analogue step')
        self.assertIn('"vol":"52"', self.status.read_text())

    def test_low_nonzero_bluetooth_volume_uses_lowest_audible_step(self):
        self.status.write_text('{"mode":"41","status":"play","vol":"1","mute":"0"}')
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: 'start' in self.events())
        self.assertEqual((self.base / 'master').read_text().strip(), '1',
                         'a nonzero phone volume must not round down to Master mute')
        self.assertEqual((self.base / 'levels').read_text().strip(), '207,0')

    def test_watchdog_allows_bluetooth_dac_unity_above_ap2_limit(self):
        mixer = self.base / 'fake-amixer'
        self.executable(mixer, '''#!/bin/sh
echo "$*" >>"$MOCK_MIXER_LOG"
case "$*" in
  *"sget Master"*) echo 'Front Left: 17';;
  *"cget numid=1"*) echo '  : values=207,207';;
  *"cget numid=2"*) echo '  : values=0,0';;
esac
''')
        mixer_log = self.base / 'mixer.log'
        mixer_log.touch()
        (self.run / 'bt-owner.pid').write_text(str(self.dummy_watchdog.pid))
        (self.run / 'bt-heartbeat').write_text('0')
        (self.run / 'shairport.pid').write_text(str(self.dummy_watchdog.pid))
        (self.safe / 'play.started').write_text('1')
        self.reserve_bluetooth()
        env = self.env | {
            'HK_AP2_AMIXER': str(mixer), 'MOCK_MIXER_LOG': str(mixer_log),
            'HK_AP2_MAX_PLAY_SECONDS': '0', 'HK_AP2_MASTER_SAFE': '4',
            'HK_AP2_DIGITAL_SAFE': '128', 'HK_AP2_PLAYBACK_SAFE': '0',
        }
        self.launch(['sh', str(WATCHDOG), '__watchdog'], env)
        eventually(lambda: 'sget Master' in mixer_log.read_text())
        time.sleep(1)
        log = mixer_log.read_text()
        self.assertNotIn('numid=1 128,128', log,
                         'the AirPlay DAC limit must not clamp Bluetooth')
        self.assertNotIn('numid=2 0,0', log,
                         'the AirPlay analogue setting must not alter Bluetooth')
        self.assertFalse((self.safe / 'tripped').exists())

    def test_invalid_volume_does_not_open(self):
        self.status.write_text('{"mode":"41","status":"play","vol":"101","mute":"0"}')
        self.launch(['sh', str(BRIDGE), '__run'])
        time.sleep(3)
        self.assertNotIn('start', self.events())

    def test_maintenance_hold_is_latched_before_player_start(self):
        hold = self.base / 'hold-mute'
        hold.touch()
        self.executable(self.base / 'setsid', '#!/bin/sh\nexec "$@"\n')
        mixer = self.base / 'fake-amixer'
        self.executable(mixer, '#!/bin/sh\nexit 0\n')
        controller = self.root / 'bin/hk-ap2ctl'
        self.executable(controller, '''#!/bin/sh
if [ "$1" = start ]; then
  [ "$(cat "$HK_AP2_RUN/safe/tripped" 2>/dev/null)" = 'manual mute' ] || exit 1
  echo latched >"$MOCK_LOG"
fi
''')
        (self.safe / 'armed').unlink()
        (self.safe / 'watchdog.pid').unlink()
        (self.safe / 'watchdog.heartbeat').unlink()
        env = self.env | {'HK_AP2_AMIXER': str(mixer), 'HK_AP2_HOLD_MUTE_FILE': str(hold),
                          'HK_AP2_MAX_PLAY_SECONDS': '0', 'PATH': str(self.base) + ':' + os.environ['PATH']}
        executable_watchdog = self.base / 'safe-test'
        self.executable(executable_watchdog, WATCHDOG.read_text())
        result = subprocess.run(['sh', str(executable_watchdog), 'arm'], env=env, capture_output=True, text=True, timeout=18)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.log.read_text().strip(), 'latched')
        pid = int((self.safe / 'watchdog.pid').read_text().strip())
        self.extra_pids.append(pid)


def _alive(pid):
    try:
        os.kill(pid, 0)
        stat = pathlib.Path(f'/proc/{pid}/stat')
        if stat.exists() and stat.read_text().split()[2] == 'Z':
            return False
        return True
    except ProcessLookupError:
        return False


if __name__ == '__main__':
    unittest.main()
