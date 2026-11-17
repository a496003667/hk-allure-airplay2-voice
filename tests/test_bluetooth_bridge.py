#!/usr/bin/env python3
"""Exercise Bluetooth ownership and watchdog failure with fake audio controls."""
import os
import pathlib
import signal
import socket
import subprocess
import tempfile
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
if [ -n "${MOCK_QUERY_LOG:-}" ] && [ "$2" = getPlayerStatus ]; then echo query >>"$MOCK_QUERY_LOG"; fi
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
        self.dummy_watchdog = self.launch(['sleep', '60'])
        (self.safe / 'watchdog.pid').write_text(str(self.dummy_watchdog.pid) + '\n')
        (self.safe / 'watchdog.heartbeat').write_text('0\n')

    def tearDown(self):
        for proc in reversed(self.children):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        self.socket.close()
        if hasattr(self, 'activity_socket'): self.activity_socket.close()
        self.tmp.cleanup()

    def executable(self, path, text):
        path.write_text(text)
        path.chmod(0o755)

    def state(self, status):
        self.status.write_text('{"mode":"41","status":"%s","vol":"12","mute":"0"}' % status)

    def launch(self, command, env=None):
        p = subprocess.Popen(command, env=env or self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.children.append(p)
        return p

    def events(self):
        return self.log.read_text().splitlines()

    def reserve_bluetooth(self):
        shell = '. "$HK_AUDIO_COMMON"; SAFE=$HK_AP2_RUN/safe; RUN=$HK_AP2_RUN; HK_AP2_BT=1; audio_reserve'
        result = subprocess.run(['sh', '-c', shell], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

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
        eventually(lambda: 'start' in self.events())
        self.assertLess(self.events().index('stop'), self.events().index('airplay pause'))
        self.assertLess(self.events().index('airplay pause'), self.events().index('start'))

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
        self.fast_reverse_sample(7, 1380)
        eventually(lambda: 'start' in self.events())

    def test_quick_phone_play_pause_restarts_confirmation(self):
        self.fast_reverse_start()
        self.state('stop')
        self.fast_reverse_sample(2, 356)
        self.state('play')
        for tick, frames in ((3, 612), (4, 868)):
            self.fast_reverse_sample(tick, frames)
            self.assert_no_reverse_takeover()
        self.fast_reverse_sample(5, 1124)
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
        self.resume_phone()
        eventually(lambda: 'start' in self.events())
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
        env = self.env | {'MOCK_MQTT_FAIL': '1'}
        self.launch(['sh', str(BRIDGE), '__run'], env)
        time.sleep(.7)
        self.advance_clock(1); time.sleep(.7)
        self.resume_phone()
        eventually(lambda: (self.safe / 'tripped').exists())
        self.assertNotIn('start', self.events())
        self.assertIn('airplay pause', self.events())

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
        self.resume_phone()
        eventually(lambda: 'start' in self.events())
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
        self.set_activity(256, 2, instance=54321)
        self.state('play'); self.advance_clock(2)
        eventually(lambda: 'start' in self.events())
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
        self.arm_reverse(); self.resume_phone()
        eventually(lambda: 'airplay pause' in self.events())
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
            self.assertFalse((self.safe / 'tripped').exists())
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
        eventually(lambda: self.events().count('start') == 2, timeout=15)
        self.assertIn('onepause', self.events())
        self.assertIn('resume', self.events())
        self.assertFalse((self.safe / 'tripped').exists())

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

    def test_bridge_closes_output_when_watchdog_dies(self):
        self.state('play')
        self.launch(['sh', str(BRIDGE), '__run'])
        eventually(lambda: self.events().count('start') == 1)
        self.dummy_watchdog.kill()
        self.dummy_watchdog.wait()
        eventually(lambda: 'stop' in self.events() and not (self.run / 'bt-owner.pid').exists())

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
        self.addCleanup(lambda: os.kill(pid, signal.SIGTERM) if _alive(pid) else None)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


if __name__ == '__main__':
    unittest.main()
