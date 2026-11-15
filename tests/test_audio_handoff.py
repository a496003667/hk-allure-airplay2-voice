#!/usr/bin/env python3
"""Exercise handoff decisions without opening ALSA or a real speaker."""
import os
import json
import pathlib
import socket
import subprocess
import tempfile
import threading
import time
import unittest

HOOK = pathlib.Path(__file__).resolve().parents[1] / 'airplay2/hk-ap2-audio-hook.sh'


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.run = self.root / 'run'
        (self.run / 'safe').mkdir(parents=True)
        (self.root / 'clock').write_text('0.00 0.00\n')
        self.env = os.environ | {'HK_AP2_ROOT': str(self.root), 'HK_AP2_RUN': str(self.run), 'HK_AUDIO_UPTIME_FILE': str(self.root / 'clock')}

    def tearDown(self):
        self.tmp.cleanup()

    def test_airplay_stop_does_not_mute_bluetooth_owner(self):
        (self.run / 'bt-owner.pid').write_text('123\n')
        result = subprocess.run(['sh', str(HOOK), 'stop'], env=self.env, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('AirPlay stop ignored', (self.run / 'hook.log').read_text())

    def test_airplay_start_requests_bluetooth_release_before_arming(self):
        owner = self.run / 'bt-owner.pid'
        release = self.run / 'bt-release'
        owner.write_text('123\n')
        seen = []
        def release_owner():
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if release.exists():
                    seen.append(True)
                    owner.unlink()
                    return
                time.sleep(.05)
        thread = threading.Thread(target=release_owner)
        thread.start()
        result = subprocess.run(['sh', str(HOOK), 'start'], env=self.env, capture_output=True, text=True, timeout=10)
        thread.join(timeout=6)
        self.assertTrue(seen, 'AirPlay must request Bluetooth release')
        self.assertTrue((self.run / 'ap2-claim').exists())
        self.assertEqual(result.returncode, 1, 'unarmed output must fail closed')

    def test_bluetooth_enables_pa_once_after_silent_clock_baseline(self):
        safe = self.run / 'safe'
        (safe / 'armed').touch()
        (safe / 'watchdog.pid').write_text(str(os.getpid()))
        (safe / 'watchdog.heartbeat').write_text('0')
        (self.root / 'lib').mkdir()
        for name in ('mute', 'unmute'):
            (self.root / 'lib' / (name + '.so')).touch()
        state = self.root / 'mixer.json'
        state.write_text(json.dumps({'Master': '0', '1': '0,0', '2': '1,1',
                                     'Digital': 'off', 'Ext Spk': 'off',
                                     'aml audio i2s mute': 'on'}))
        events = self.root / 'events'
        mixer = self.root / 'amixer'
        mixer.write_text('''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ['MOCK_MIXER_STATE']
with open(state_path) as f: state = json.load(f)
a = sys.argv[3:]
action, name = a[:2]
key = name.removeprefix('numid=')
if action in ('sset', 'cset'):
    state[key] = a[2]
    if os.environ.get('MOCK_ANALOG_TAMPER') and key == 'Ext Spk' and a[2] == 'on':
        state['2'] = '0,0'
    with open(state_path, 'w') as f: json.dump(state, f)
    with open(os.environ['MOCK_EVENTS'], 'a') as f: f.write('mixer ' + key + ' ' + a[2] + '\\n')
elif action == 'cget': print('  : values=' + state[key])
elif key == 'Master': print('Front Left: ' + state[key])
else: print('Front Left: Playback [' + state[key] + ']')
''')
        mixer.chmod(0o755)
        remote = self.root / 'apple_remote'
        remote.write_text('''#!/bin/sh
case "$LD_PRELOAD" in */unmute.so) level=0;; *) level=1;; esac
echo "pa $level" >>"$MOCK_EVENTS"
echo "Snd_Ctrl airplay to mute $level"
''')
        remote.chmod(0o755)
        delay = self.root / 'usleep'
        delay.write_text('#!/bin/sh\nsleep 0.01\n')
        delay.chmod(0o755)
        pcm = self.root / 'pcm-status'
        pcm.write_text('state: RUNNING\nowner_pid   : 42\n')
        (self.root / 'proc/42').mkdir(parents=True)
        (self.root / 'proc/42/comm').write_text('a01controller\n')
        env = self.env | {
            'HK_AP2_BT': '1', 'HK_AP2_USE_STOCK_PA': '1',
            'HK_AP2_DIGITAL_SAFE': '100', 'HK_AP2_PLAYBACK_SAFE': '1',
            'HK_AP2_AMIXER': str(mixer), 'HK_AP2_APPLE_REMOTE': str(remote),
            'HK_AP2_APPLE_REMOTE_SOCKET': str(self.root / 'socket'),
            'HK_AP2_PA_MUTE_LIB': str(self.root / 'lib/mute.so'),
            'HK_AP2_PA_UNMUTE_LIB': str(self.root / 'lib/unmute.so'),
            'HK_AP2_PCM_STATUS': str(pcm), 'HK_AP2_PROC_ROOT': str(self.root / 'proc'),
            'PATH': str(self.root) + ':' + os.environ['PATH'],
            'MOCK_MIXER_STATE': str(state), 'MOCK_EVENTS': str(events),
        }
        (self.run / 'bt-owner.pid').write_text(str(os.getpid()))
        def acknowledge():
            deadline = time.monotonic() + 10
            while not (safe / 'play.started').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            if not safe.exists(): return
            (self.root / 'clock').write_text('1.00 0.00\n')
            (safe / 'watchdog.heartbeat').write_text('1')
        thread = threading.Thread(target=acknowledge)
        thread.start()
        result = subprocess.run(['sh', str(HOOK), 'start'], env=env, capture_output=True, text=True, timeout=25)
        thread.join(timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr + '\n' + (self.run / 'hook.log').read_text() + '\n' + events.read_text())
        log = events.read_text().splitlines()
        self.assertLess(log.index('mixer 1 0,0'), log.index('pa 1'))
        self.assertLess(log.index('pa 1'),
                        log.index('mixer aml audio i2s mute off'))
        self.assertLess(log.index('mixer aml audio i2s mute off'),
                        log.index('pa 0'))
        self.assertLess(log.index('pa 0'), log.index('mixer Ext Spk on'))
        self.assertLess(log.index('mixer Ext Spk on'),
                        log.index('mixer 1 100,100'))
        self.assertEqual(log.count('pa 0'), 1)
        self.assertEqual(log.count('pa 1'), 1)
        self.assertEqual(log.count('mixer Ext Spk on'), 1)
        self.assertEqual(log.count('mixer aml audio i2s mute off'), 1)
        self.assertEqual(log.count('mixer 1 0,0'), 2)

        (safe / 'play.started').unlink()
        (safe / 'lease').unlink()
        (self.root / 'clock').write_text('0.00 0.00\n')
        (safe / 'watchdog.heartbeat').write_text('0')
        pcm.write_text('closed\n')
        events.write_text('')
        thread = threading.Thread(target=acknowledge)
        thread.start()
        result = subprocess.run(['sh', str(HOOK), 'start'], env=env, capture_output=True, text=True, timeout=25)
        thread.join(timeout=2)
        self.assertNotEqual(result.returncode, 0, 'a missing PCM clock must refuse output')
        self.assertNotIn('pa 0', events.read_text())
        self.assertNotIn('mixer Ext Spk on', events.read_text())

        # If the analogue step changes after the baseline check while the
        # amplifier warms up, output must remain digitally silent.
        (safe / 'play.started').unlink(missing_ok=True)
        (safe / 'lease').unlink(missing_ok=True)
        (self.root / 'clock').write_text('0.00 0.00\n')
        (safe / 'watchdog.heartbeat').write_text('0')
        pcm.write_text('state: RUNNING\nowner_pid   : 42\n')
        events.write_text('')
        thread = threading.Thread(target=acknowledge)
        thread.start()
        result = subprocess.run(['sh', str(HOOK), 'start'], env=env | {'MOCK_ANALOG_TAMPER': '1'},
                                capture_output=True, text=True, timeout=25)
        thread.join(timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('mixer 1 100,100', events.read_text())
        self.assertIn('Playback Volume changed during amplifier warm-up', (self.run / 'hook.log').read_text())

    def test_shared_guard_never_enables_output_and_rejects_unsafe_paths(self):
        safe = self.run / 'safe'
        (safe / 'armed').touch()
        (safe / 'watchdog.pid').write_text(str(os.getpid()))
        (safe / 'watchdog.heartbeat').write_text('0')
        (safe / 'play.started').write_text('1\n')
        state = self.root / 'mixer.json'
        state.write_text(json.dumps({'Master': '4', '1': '192,192', '2': '1,1',
                                     'Digital': 'on', 'Ext Spk': 'off',
                                     'aml audio i2s mute': 'on'}))
        events = self.root / 'events'
        mixer = self.root / 'amixer'
        mixer.write_text('''#!/usr/bin/env python3
import json, os, sys
state_path = os.environ['MOCK_MIXER_STATE']
with open(state_path) as f: state = json.load(f)
a = sys.argv[3:]
action, name = a[:2]
key = name.removeprefix('numid=')
if action in ('sset', 'cset'):
    with open(os.environ['MOCK_EVENTS'], 'a') as f: f.write('mixer ' + key + ' ' + a[2] + '\\n')
elif action == 'cget': print('  : values=' + state[key])
elif key == 'Master': print('Front Left: ' + state[key])
else: print('Front Left: Playback [' + state[key] + ']')
''')
        mixer.chmod(0o755)
        pcm = self.root / 'pcm-status'
        pcm.write_text('state: RUNNING\nowner_pid   : 42\n')
        (self.root / 'proc/42').mkdir(parents=True)
        (self.root / 'proc/42/comm').write_text('a01controller\n')
        env = self.env | {
            'HK_AP2_BT': '1',
            'HK_AP2_AMIXER': str(mixer),
            'HK_AP2_PCM_STATUS': str(pcm), 'HK_AP2_PROC_ROOT': str(self.root / 'proc'),
            'MOCK_MIXER_STATE': str(state), 'MOCK_EVENTS': str(events),
        }
        env['HK_AP2_VOICE_OWNER'] = str(os.getpid())
        (self.run / 'bt-owner.pid').write_text(str(os.getpid()))
        reserve = subprocess.run(['sh', str(HOOK), 'reserve'], env=env, capture_output=True, text=True, timeout=5)
        self.assertEqual(reserve.returncode, 0, reserve.stderr)
        result = subprocess.run(['sh', str(HOOK), 'guard'], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(events.exists(), 'guard must not change any mixer control')
        self.assertIn('output guard passed', (self.run / 'hook.log').read_text())

        hold = self.root / 'hold-mute'
        hold.touch()
        result = subprocess.run(['sh', str(HOOK), 'guard'],
                                env=env | {'HK_AP2_HOLD_MUTE_FILE': str(hold)},
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        hold.unlink()

        state.write_text(json.dumps({'Master': '4', '1': '192,192', '2': '1,1',
                                     'Digital': 'off', 'Ext Spk': 'on',
                                     'aml audio i2s mute': 'on'}))
        result = subprocess.run(['sh', str(HOOK), 'guard'], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0, 'an already-open amplifier must fail closed')
        state.write_text(json.dumps({'Master': '4', '1': '192,192', '2': '1,1',
                                     'Digital': 'on', 'Ext Spk': 'off',
                                     'aml audio i2s mute': 'on'}))
        pcm.write_text('closed\n')
        result = subprocess.run(['sh', str(HOOK), 'guard'], env=env,
                                capture_output=True, text=True, timeout=12)
        self.assertNotEqual(result.returncode, 0, 'a missing PCM clock must fail closed')

        pcm.write_text('state: RUNNING\nowner_pid   : 42\n')
        state.write_text(json.dumps({'Master': '4', '1': '0,0', '2': '0,0',
                                     'Digital': 'on', 'Ext Spk': 'on',
                                     'aml audio i2s mute': 'off'}))
        result = subprocess.run(['sh', str(HOOK), 'guard', 'active'], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        state.write_text(json.dumps({'Master': '4', '1': '207,207', '2': '0,0',
                                     'Digital': 'on', 'Ext Spk': 'on',
                                     'aml audio i2s mute': 'off'}))
        result = subprocess.run(['sh', str(HOOK), 'guard', 'active'], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0, 'an active path at full gain must fail closed')


class PcmParkingHandoffTest(unittest.TestCase):
    setUp = HandoffTest.setUp
    tearDown = HandoffTest.tearDown

    def parking_env(self):
        safe = self.run / 'safe'
        (safe / 'armed').touch()
        (safe / 'watchdog.pid').write_text(str(os.getpid()))
        (safe / 'watchdog.heartbeat').write_text('0\n')
        (self.root / 'proc/42').mkdir(parents=True)
        (self.root / 'proc/42/comm').write_text('a01controller\n')
        self.pcm = self.root / 'pcm'
        self.pcm.write_text('state: XRUN\nowner_pid   : 42\n')
        self.events = self.root / 'events'
        self.events.touch()
        mixer = self.root / 'mixer'
        mixer.write_text('''#!/bin/sh
echo "$*" >>"$MOCK_EVENTS"
case "$3:$4" in
 cget:numid=1) echo ' : values=0,0';;
 sget:Ext*) echo 'Front Left: Playback [off]';;
 sget:aml*) echo 'Front Left: Playback [on]';;
esac
''')
        mixer.chmod(0o755)
        delay = self.root / 'usleep'
        delay.write_text('#!/bin/sh\nexit 0\n'); delay.chmod(0o755)
        request = self.root / 'request'
        subprocess.run(['cc', '-D_POSIX_C_SOURCE=200809L',
                        str(HOOK.parents[1] / 'tools/hk-unix-request-raw.c'),
                        '-o', str(request)], check=True, capture_output=True)
        return self.env | {'HK_AP2_PCM_STATUS': str(self.pcm),
                           'HK_AP2_PROC_ROOT': str(self.root / 'proc'),
                           'HK_AP2_AMIXER': str(mixer), 'HK_AP2_REQUEST': str(request),
                           'HK_AP2_USE_STOCK_PA': '0', 'MOCK_EVENTS': str(self.events),
                           'PATH': str(self.root) + ':' + os.environ['PATH']}

    def run_park(self, close_pcm):
        env = self.parking_env()
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(self.run.parent / 'bt-pcm-control')); server.listen(1)
        seen = []
        def serve():
            try:
                server.settimeout(3)
                with server.accept()[0] as client:
                    request = b''
                    while chunk := client.recv(100): request += chunk
                    token = (self.run / 'bt-pcm-park').read_text().strip()
                    log = self.events.read_text()
                    seen.append(request == ('park:' + token).encode() + b'\0'
                                and not (self.run / 'safe/lease').exists()
                                and 'cset numid=1 0,0' in log
                                and 'sset Ext Spk off' in log
                                and 'sset aml audio i2s mute on' in log)
                    if close_pcm: self.pcm.write_text('closed\n')
                    client.sendall(b'parked\n')
            finally:
                server.close()
        thread = threading.Thread(target=serve); thread.start()
        result = subprocess.run(['sh', str(HOOK), 'park'], env=env,
                                text=True, capture_output=True, timeout=5)
        thread.join(timeout=4)
        self.assertEqual(seen, [True], result.stderr + self.events.read_text())
        self.assertFalse((self.run / 'safe/lease').exists())
        self.assertFalse((self.run / 'bt-pcm-park').exists())
        self.assertNotIn('Ext Spk on', self.events.read_text())
        return result

    def test_guarded_park_mutes_before_ipc_and_does_not_reserve_or_open(self):
        result = self.run_park(close_pcm=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.pcm.read_text(), 'closed\n')

    def test_park_ack_without_hardware_release_is_not_success(self):
        result = self.run_park(close_pcm=False)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
