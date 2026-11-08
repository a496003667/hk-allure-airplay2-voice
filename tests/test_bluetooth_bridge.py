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
if [ "$2" = setPlayerCmd:vol:30 ]; then
  echo clamp >>"$MOCK_LOG"
  sed 's/"vol":"52"/"vol":"30"/' "$MOCK_STATUS" >"$MOCK_STATUS.tmp" && mv "$MOCK_STATUS.tmp" "$MOCK_STATUS"
else
  cat "$MOCK_STATUS"
fi
''')
        self.executable(self.root / 'hk-ap2-audio-hook.sh', '''#!/bin/sh
echo "$1" >>"$MOCK_LOG"
if [ "$1" = start ]; then
  echo "$HK_AP2_MASTER_SAFE" >"$MOCK_MASTER"
  echo "$HK_AP2_DIGITAL_SAFE,$HK_AP2_PLAYBACK_SAFE" >"$MOCK_LEVELS"
  echo 0 >"$HK_AP2_RUN/safe/play.started"
else
  rm -f "$HK_AP2_RUN/safe/play.started"
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
            'MOCK_MASTER': str(self.base / 'master'),
            'MOCK_LEVELS': str(self.base / 'levels'),
        }
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
                          'HK_AP2_DIGITAL_SAFE': '192', 'HK_AP2_PLAYBACK_SAFE': '1'}
        self.launch(['sh', str(WATCHDOG), '__watchdog'], env)
        eventually(lambda: (self.safe / 'watchdog.heartbeat').exists())
        bridge.kill()
        bridge.wait()
        eventually(lambda: (self.safe / 'tripped').exists(), timeout=8)
        self.assertEqual((self.safe / 'tripped').read_text().strip(), 'Bluetooth bridge lost')
        self.assertFalse((self.safe / 'play.started').exists())
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
        env = self.env | {'HK_AP2_AMIXER': str(mixer), 'HK_AP2_MAX_PLAY_SECONDS': '0',
                          'HK_AP2_PLAYBACK_SAFE': '1',
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
        env = self.env | {'HK_AP2_AMIXER': str(mixer), 'HK_AP2_MAX_PLAY_SECONDS': '0',
                          'HK_AP2_PLAYBACK_SAFE': '1',
                          'PATH': str(self.base) + ':' + os.environ['PATH']}
        self.launch(['sh', str(WATCHDOG), '__watchdog'], env)
        eventually(lambda: not (self.run / 'bt-owner.pid').exists())
        time.sleep(1)
        self.assertFalse((self.safe / 'tripped').exists(), 'normal release must not latch a false trip')

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
        env = self.env | {'HK_AP2_AMIXER': str(mixer), 'MOCK_MIXER_LOG': str(mixer_log),
                          'HK_AP2_MAX_PLAY_SECONDS': '0', 'HK_AP2_MASTER_SAFE': '4',
                          'HK_AP2_PLAYBACK_SAFE': '1'}
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
