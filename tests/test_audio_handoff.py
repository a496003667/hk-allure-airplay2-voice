#!/usr/bin/env python3
"""Exercise handoff decisions without opening ALSA or a real speaker."""
import os
import hashlib
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
        self.assertFalse(
            (self.run / 'ap2-claim').exists(),
            'an unsuccessful AirPlay start must not leave a stale claim that blocks Bluetooth',
        )
        self.assertEqual(result.returncode, 1, 'unarmed output must fail closed')

    def test_older_airplay_start_cannot_delete_a_replacement_claim(self):
        safe = self.run / 'safe'
        (safe / 'armed').touch()
        (safe / 'watchdog.pid').write_text(str(os.getpid()))
        (safe / 'watchdog.heartbeat').write_text('0')
        owner = self.run / 'bt-owner.pid'
        owner.write_text('123\n')
        env = self.env | {'HK_AP2_HOLD_MUTE_FILE': str(self.root / 'hold')}
        start = subprocess.Popen(
            ['sh', str(HOOK), 'start'],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        claim = self.run / 'ap2-claim'
        deadline = time.monotonic() + 5
        while not claim.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(claim.exists())
        first = claim.read_text()
        replacement = '999999 1 1 replacement-claim\n'
        claim.write_text(replacement)
        owner.unlink()
        _, stderr = start.communicate(timeout=8)
        self.assertNotEqual(start.returncode, 0, stderr)
        self.assertEqual(claim.read_text(), replacement)
        self.assertIn('start superseded by a newer AirPlay claim',
                      (self.run / 'hook.log').read_text())
        self.assertNotEqual(first, replacement)

    def bluetooth_start_fixture(self):
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
        remote.write_text('''#!/usr/bin/env python3
import json, os
level = 0 if os.environ['LD_PRELOAD'].endswith('/unmute.so') else 1
path = os.environ['MOCK_PA_STATE']
s = json.load(open(path))
if not ((level == 0 and os.environ.get('MOCK_NO_UNMUTE_ACK')) or
        (level == 0 and os.environ.get('MOCK_PA_STAYS_MUTED'))):
    s['pa'] = int(bool(level or s['force']))
json.dump(s, open(path, 'w'))
with open(os.environ['MOCK_EVENTS'], 'a') as f: print('pa', level, file=f)
print('Snd_Ctrl airplay to mute', level)
''')
        remote.chmod(0o755)
        audio_lib = self.root / 'libmvmsg.so'
        audio_lib.write_bytes(b'verified stock layout')
        pa_state = self.root / 'pa-state.json'
        pa_state.write_text(json.dumps({'pa': 1, 'force': 1, 'gate': 1}))
        reader = self.root / 'audio-state'
        reader.write_text('''#!/usr/bin/env python3
import json, os, sys
s = json.load(open(os.environ['MOCK_PA_STATE']))
print('PA mute=%s force_mute=%s gate=%s' % (s['pa'], s['force'], s['gate']))
blocked = False if len(sys.argv) == 1 else s['force'] if sys.argv[1] == '--global-unmuted' else s['force'] or s['pa'] or not s['gate']
sys.exit(3 if blocked else 0)
''')
        reader.chmod(0o755)
        request = self.root / 'request'
        request.write_text('''#!/usr/bin/env python3
import json, os, sys
command = sys.argv[2]
if command == 'getPlayerStatus':
    status = open(os.environ['MOCK_PHONE_STATUS']).read().strip() if os.environ.get('MOCK_PHONE_STATUS') else 'play'
    print(json.dumps({'mode':'41', 'status':status, 'mute':os.environ.get('MOCK_PLAYER_MUTE','0')}, separators=(',',':')))
elif command.startswith('park:'):
    open(os.environ['HK_AP2_PCM_STATUS'], 'w').write('closed\\n')
    os.unlink(os.environ['MOCK_OWNER_PROOF'])
    with open(os.environ['MOCK_EVENTS'], 'a') as f: print('parked Bluetooth hardware', file=f)
    print('parked')
elif command == 'setPlayerCmd:onepause':
    assert not os.path.exists(os.environ['HK_AP2_RUN'] + '/safe/lease')
    assert open(os.environ['HK_AP2_PCM_STATUS']).read().strip() == 'closed'
    with open(os.environ['MOCK_EVENTS'], 'a') as f: print('phone pause', file=f)
    if not os.environ.get('MOCK_REFUSE_PAUSE'): open(os.environ['MOCK_PHONE_STATUS'], 'w').write('stop')
elif command == 'GNOTIFY=AUDIOMUTE:1':
    mixer = json.load(open(os.environ['MOCK_MIXER_STATE']))
    assert mixer['1'] == '0,0' and mixer['Ext Spk'] == 'off'
    with open(os.environ['MOCK_EVENTS'], 'a') as f: print('global pre-mute', file=f)
    if not os.environ.get('MOCK_PREMUTE_NO_ACK'):
        path = os.environ['MOCK_PA_STATE']; s = json.load(open(path)); s['force'] = 1; s['pa'] = 1
        json.dump(s, open(path, 'w'))
elif command == 'GNOTIFY=AUDIOMUTE:0':
    mixer = json.load(open(os.environ['MOCK_MIXER_STATE']))
    if os.environ.get('MOCK_REPAIR_ACTIVE'):
        assert mixer['1'] == '0,0' and mixer['Ext Spk'] == 'on' and mixer['aml audio i2s mute'] == 'off'
    else:
        assert mixer['1'] == '0,0' and mixer['Ext Spk'] == 'off'
    with open(os.environ['MOCK_EVENTS'], 'a') as f:
        print('global unmute with silent clock' if mixer['aml audio i2s mute'] == 'off' else 'global cleanup after physical mute', file=f)
    if not os.environ.get('MOCK_NO_UNMUTE_ACK'):
        path = os.environ['MOCK_PA_STATE']; s = json.load(open(path))
        old_force = s['force']
        s['force'] = 0
        # Real firmware only clears the amplifier in response to the global
        # force-mute transition. A PA-only mute remains and needs the verified
        # native helper fallback.
        if old_force and not os.environ.get('MOCK_PA_STAYS_MUTED'):
            s['pa'] = 0
        json.dump(s, open(path, 'w'))
    if os.environ.get('MOCK_HOLD_DURING_UNMUTE'):
        open(os.environ['HK_AP2_HOLD_MUTE_FILE'], 'w').close()
else:
    sys.exit(2)
''')
        request.chmod(0o755)
        delay = self.root / 'usleep'
        delay.write_text('#!/bin/sh\nsleep 0.01\n')
        delay.chmod(0o755)
        pcm = self.root / 'pcm-status'
        pcm.write_text('state: RUNNING\nowner_pid   : 42\n')
        (self.root / 'proc/42').mkdir(parents=True)
        (self.root / 'proc/42/comm').write_text('a01controller\n')
        holder = self.root / 'proc' / str(os.getpid())
        (holder / 'fd').mkdir(parents=True)
        (holder / 'comm').write_text('a01controller\n')
        (holder / 'fd/26').symlink_to('/dev/snd/pcmC0D0p')
        started = pathlib.Path('/proc/self/stat').read_text().rsplit(') ', 1)[1].split()[19]
        proof = f'{os.getpid()} {started} 26 12345\n'
        (self.root / 'bt-pcm-owner').write_text(proof)
        (self.run / 'bt-pa-ready').write_text(proof)
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
            'HK_AP2_STOCK_AUDIO_STATE': str(reader), 'HK_AP2_STOCK_AUDIO_LIB': str(audio_lib),
            'HK_AP2_STOCK_AUDIO_LIB_SHA256': hashlib.sha256(audio_lib.read_bytes()).hexdigest(),
            'MOCK_PA_STATE': str(pa_state), 'HK_AP2_REQUEST': str(request),
            'HK_AP2_HOLD_MUTE_FILE': str(self.root / 'hold-mute'),
        }
        (self.run / 'bt-owner.pid').write_text(str(os.getpid()))
        def acknowledge():
            deadline = time.monotonic() + 10
            while not (safe / 'play.started').exists() and time.monotonic() < deadline:
                time.sleep(.02)
            if not safe.exists(): return
            if env.get('MOCK_DELAY_ACK_UNTIL_DIGITAL_MUTE'):
                while time.monotonic() < deadline:
                    if events.exists() and 'mixer 1 0,0' in events.read_text(): break
                    time.sleep(.01)
            lease = safe / 'lease'
            if not lease.exists():
                return
            while not (safe / 'lease.request').exists() and time.monotonic() < deadline:
                time.sleep(.01)
            if not env.get('MOCK_HEARTBEAT_ONLY'):
                token, nonce = (safe / 'lease.request').read_text().split()
                if env.get('MOCK_ACK_DELAY_SECONDS'):
                    time.sleep(float(env['MOCK_ACK_DELAY_SECONDS']))
                if env.get('MOCK_STALE_ACK_FIRST'):
                    (safe / 'lease.ack').write_text(token + ' previous-start\n')
                    with events.open('a') as f: print('stale watchdog ack', file=f)
                    time.sleep(.08)
                if env.get('MOCK_STALE_ACK_ONLY'):
                    return
                if env.get('MOCK_ACK_THEN_TRIP'):
                    (safe / 'tripped').write_text('manual mute\n')
                    (safe / 'lease.ack').write_text(token + ' ' + nonce + '\n')
                    return
                if env.get('MOCK_ACK_EXTRA'):
                    (safe / 'lease.ack').write_text(token + ' ' + nonce + ' unexpected\n')
                    return
                if env.get('MOCK_REFRESH_BEFORE_ACK'):
                    (self.root / 'clock').write_text('1.00 0.00\n')
                    (safe / 'watchdog.heartbeat').write_text('1')
                (safe / 'lease.ack').write_text(token + ' ' + nonce + '\n')
            with events.open('a') as f: print('watchdog ack', file=f)
            (self.root / 'clock').write_text('1.00 0.00\n')
            (safe / 'watchdog.heartbeat').write_text('1')
        return env, pcm, events, acknowledge

    def test_bluetooth_uses_official_amplifier_ack_after_silent_clock_baseline(self):
        env, pcm, events, acknowledge = self.bluetooth_start_fixture()
        safe = self.run / 'safe'
        thread = threading.Thread(target=acknowledge)
        thread.start()
        result = subprocess.run(['sh', str(HOOK), 'start'], env=env, capture_output=True, text=True, timeout=25)
        thread.join(timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr + '\n' + (self.run / 'hook.log').read_text() + '\n' + events.read_text())
        log = events.read_text().splitlines()
        self.assertLess(log.index('mixer 1 0,0'),
                        log.index('mixer aml audio i2s mute off'))
        self.assertLess(log.index('mixer aml audio i2s mute off'),
                        log.index('global unmute with silent clock'))
        self.assertLess(log.index('global unmute with silent clock'), log.index('mixer Ext Spk on'))
        self.assertLess(log.index('mixer Ext Spk on'),
                        log.index('mixer 1 100,100'))
        self.assertNotIn('pa 0', log)
        self.assertNotIn('pa 1', log)
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

    def run_bluetooth_start_case(self, extra=None):
        env, pcm, events, acknowledge = self.bluetooth_start_fixture()
        env.update(extra or {})
        if env.get('MOCK_COLD_PCM'):
            (self.run / 'bt-pa-ready').unlink()
            (self.root / 'pa-state.json').write_text(json.dumps({'pa': 0, 'force': 0, 'gate': 1}))
        if env.get('MOCK_ALREADY_UNMUTED'):
            (self.root / 'pa-state.json').write_text(json.dumps({'pa': 0, 'force': 0, 'gate': 1}))
        if env.get('MOCK_ONLY_PA_MUTED'):
            (self.root / 'pa-state.json').write_text(json.dumps({'pa': 1, 'force': 0, 'gate': 1}))
        if env.get('MOCK_COLD_PA_ONLY'):
            (self.run / 'bt-pa-ready').unlink()
            (self.root / 'pa-state.json').write_text(json.dumps({'pa': 1, 'force': 0, 'gate': 1}))
        thread = threading.Thread(target=acknowledge); thread.start()
        result = subprocess.run(['sh', str(HOOK), 'start'], env=env, capture_output=True, text=True, timeout=25)
        thread.join(timeout=2)
        return result, events.read_text(), json.loads((self.root / 'mixer.json').read_text())

    def test_global_unmute_requires_readback_not_just_successful_ipc_delivery(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_NO_UNMUTE_ACK': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('global unmute with silent clock', events)
        self.assertNotIn('mixer Ext Spk on', events)
        self.assertEqual(state['1'], '0,0')

    def test_global_unmute_respects_factory_source_mute(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_PLAYER_MUTE': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('global unmute with silent clock', events)
        self.assertNotIn('mixer Ext Spk on', events)

    def test_new_hold_during_global_unmute_prevents_gain_restoration(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_HOLD_DURING_UNMUTE': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('mixer Ext Spk on', events)
        self.assertEqual(state['1'], '0,0')

    def test_wrong_stock_layout_never_sends_global_unmute(self):
        result, events, state = self.run_bluetooth_start_case({'HK_AP2_STOCK_AUDIO_LIB_SHA256': '0' * 64})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('global unmute with silent clock', events)

    def test_bluetooth_pre_mute_can_overlap_a_delayed_watchdog_ack(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_DELAY_ACK_UNTIL_DIGITAL_MUTE': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = events.splitlines()
        self.assertLess(lines.index('mixer 1 0,0'), lines.index('watchdog ack'))
        self.assertLess(lines.index('watchdog ack'), lines.index('mixer aml audio i2s mute off'))

    def test_watchdog_heartbeat_without_exact_lease_ack_cannot_open_speaker(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_HEARTBEAT_ONLY': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('watchdog did not acknowledge playback lease', (self.run / 'hook.log').read_text())
        self.assertNotIn('mixer Ext Spk on', events)
        self.assertEqual(state['1'], '0,0')

    def test_delayed_heartbeat_does_not_abort_an_exact_transition_ack(self):
        started = time.monotonic()
        result, events, state = self.run_bluetooth_start_case({
            'MOCK_ACK_DELAY_SECONDS': '1.5',
            'MOCK_REFRESH_BEFORE_ACK': '1',
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(time.monotonic() - started, 1.4)
        self.assertIn('watchdog ack', events)
        self.assertIn('mixer Ext Spk on', events)
        self.assertEqual(state['Ext Spk'], 'on')

    def test_transition_grace_rejects_a_heartbeat_older_than_its_bound(self):
        env, pcm, events, acknowledge = self.bluetooth_start_fixture()
        (self.root / 'clock').write_text('100.00 0.00\n')
        (self.run / 'safe/watchdog.heartbeat').write_text('84')
        env.update({
            'MOCK_ACK_DELAY_SECONDS': '.2',
            'MOCK_REFRESH_BEFORE_ACK': '1',
            'HK_AP2_WATCHDOG_TRANSITION_GRACE_SECONDS': '15',
        })
        thread = threading.Thread(target=acknowledge)
        thread.start()
        result = subprocess.run(
            ['sh', str(HOOK), 'start'], env=env,
            capture_output=True, text=True, timeout=10,
        )
        thread.join(timeout=2)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(
            events.exists() and 'mixer Ext Spk on' in events.read_text())
        self.assertFalse((self.run / 'safe/lease.ack').exists())

    def test_dead_watchdog_process_fails_before_waiting_for_ack(self):
        env, pcm, events, acknowledge = self.bluetooth_start_fixture()
        dead = subprocess.Popen(['sleep', '30'])
        dead.kill()
        dead.wait(timeout=3)
        (self.run / 'safe/watchdog.identity').write_text(
            f'{dead.pid} 1\n')
        (self.run / 'safe/watchdog.pid').unlink()
        started = time.monotonic()
        result = subprocess.run(
            ['sh', str(HOOK), 'start'], env=env,
            capture_output=True, text=True, timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(
            events.exists() and 'mixer Ext Spk on' in events.read_text())

    def test_ack_for_the_same_token_and_previous_start_is_not_reused(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_STALE_ACK_ONLY': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('watchdog did not acknowledge playback lease', (self.run / 'hook.log').read_text())
        self.assertNotIn('mixer Ext Spk on', events)
        self.assertEqual(state['1'], '0,0')

    def test_ack_with_extra_fields_is_rejected(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_ACK_EXTRA': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('watchdog did not acknowledge playback lease', (self.run / 'hook.log').read_text())
        self.assertNotIn('mixer Ext Spk on', events)
        self.assertEqual(state['1'], '0,0')

    def test_stale_ack_is_ignored_until_the_current_start_nonce_is_confirmed(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_STALE_ACK_FIRST': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = events.splitlines()
        self.assertLess(lines.index('stale watchdog ack'), lines.index('watchdog ack'))
        self.assertLess(lines.index('watchdog ack'), lines.index('mixer Ext Spk on'))

    def test_trip_after_ack_prevents_every_audible_enable(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_ACK_THEN_TRIP': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('mixer aml audio i2s mute off', events)
        self.assertNotIn('mixer Ext Spk on', events)
        self.assertEqual(state['1'], '0,0')

    def test_stop_clears_the_start_request_and_ack(self):
        env, pcm, events, acknowledge = self.bluetooth_start_fixture()
        thread = threading.Thread(target=acknowledge)
        thread.start()
        result = subprocess.run(['sh', str(HOOK), 'start'], env=env,
                                capture_output=True, text=True, timeout=25)
        thread.join(timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        safe = self.run / 'safe'
        self.assertTrue((safe / 'lease.request').exists())
        self.assertTrue((safe / 'lease.ack').exists())
        lease_fields = (safe / 'lease').read_text().split()
        self.assertFalse((self.run / 'release-intent').exists())
        result = subprocess.run(['sh', str(HOOK), 'stop', 'ignored'], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((safe / 'lease.request').exists())
        self.assertFalse((safe / 'lease.ack').exists())
        self.assertEqual(
            (self.run / 'release-intent').read_text().split(),
            [lease_fields[0], lease_fields[3], lease_fields[4]],
        )
        result = subprocess.run(['sh', str(HOOK), 'stop', 'ignored'], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((safe / 'lease').exists())

    def test_bluetooth_does_not_launch_temporary_airplay_helper(self):
        result, events, state = self.run_bluetooth_start_case({'HK_AP2_APPLE_REMOTE': str(self.root / 'absent-apple-remote')})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('pa 0', events)
        self.assertNotIn('pa 1', events)

    def test_new_bluetooth_clock_pre_mutes_pa_before_analogue_and_i2s(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_COLD_PCM': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = events.splitlines()
        self.assertLess(lines.index('mixer 1 0,0'), lines.index('global pre-mute'))
        self.assertLess(lines.index('global pre-mute'), lines.index('mixer 2 1,1'))
        self.assertLess(lines.index('global pre-mute'), lines.index('mixer aml audio i2s mute off'))
        self.assertLess(lines.index('mixer aml audio i2s mute off'), lines.index('global unmute with silent clock'))
        self.assertLess(lines.index('global unmute with silent clock'), lines.index('mixer Ext Spk on'))
        self.assertNotIn('pa 1', lines)
        self.assertNotIn('pa 0', lines)
        self.assertEqual((self.run / 'bt-pa-ready').read_text(), (self.root / 'bt-pcm-owner').read_text())

    def test_native_pre_mute_requires_actual_pa_acknowledgement(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_COLD_PCM': '1', 'MOCK_PREMUTE_NO_ACK': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('global pre-mute', events)
        self.assertNotIn('mixer aml audio i2s mute off', events)
        self.assertNotIn('mixer Ext Spk on', events)
        self.assertFalse((self.run / 'bt-pa-ready').exists())

    def test_new_descriptor_generation_cannot_reuse_warm_amplifier_state(self):
        env, pcm, events, acknowledge = self.bluetooth_start_fixture()
        proof = self.root / 'bt-pcm-owner'
        proof.write_text(proof.read_text().replace('12345', '12346'))
        (self.root / 'pa-state.json').write_text(json.dumps({'pa': 0, 'force': 0, 'gate': 1}))
        thread = threading.Thread(target=acknowledge); thread.start()
        result = subprocess.run(['sh', str(HOOK), 'start'], env=env, capture_output=True, text=True, timeout=25)
        thread.join(2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('global pre-mute', events.read_text())
        self.assertEqual((self.run / 'bt-pa-ready').read_text(), proof.read_text())

    def test_failed_cold_start_clears_its_global_override_after_physical_mute(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_COLD_PCM': '1', 'MOCK_PLAYER_MUTE': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('global cleanup after physical mute', events)
        self.assertNotIn('mixer Ext Spk on', events)
        self.assertFalse((self.root / 'bt-cold-global-mute').exists())
        self.assertEqual(json.loads((self.root / 'pa-state.json').read_text())['force'], 0)

    def test_failed_cleanup_keeps_its_marker_for_next_guarded_source(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_COLD_PCM': '1', 'MOCK_PLAYER_MUTE': '1', 'MOCK_NO_UNMUTE_ACK': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue((self.root / 'bt-cold-global-mute').exists())
        self.assertEqual(state['1'], '0,0')

    def active_airplay_fixture(self, pa, force):
        env, pcm, events, _ = self.bluetooth_start_fixture()
        env = env.copy()
        env.pop('HK_AP2_BT', None)
        env['HK_AP2_USE_STOCK_PA'] = '1'
        env['MOCK_REPAIR_ACTIVE'] = '1'
        env['HK_AUDIO_COMMON'] = str(HOOK.parent / 'hk-audio-common.sh')
        (self.run / 'bt-owner.pid').unlink(missing_ok=True)
        (self.run / 'shairport.pid').write_text(str(os.getpid()))

        driver = self.root / 'proc/99'
        driver.mkdir()
        (driver / 'comm').write_text('hk-ap2-dsp-bri\n')
        (self.run / 'dsp-bridge.pid').write_text('99\n')
        pcm.write_text('state: RUNNING\nowner_pid   : 99\n')

        mixer_state = self.root / 'mixer.json'
        mixer_state.write_text(json.dumps({
            'Master': '4', '1': '77,77', '2': '1,1',
            'Digital': 'on', 'Ext Spk': 'on',
            'aml audio i2s mute': 'off',
        }))
        (self.root / 'pa-state.json').write_text(
            json.dumps({'pa': pa, 'force': force, 'gate': 1})
        )
        shell = (
            '. "$HK_AUDIO_COMMON"; SAFE=$HK_AP2_RUN/safe; '
            'RUN=$HK_AP2_RUN; audio_reserve && lease_phase=active && '
            'audio_write_lease'
        )
        result = subprocess.run(
            ['sh', '-c', shell], env=env, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return env, events, mixer_state

    def test_active_airplay_repairs_a_pa_only_mute_under_digital_silence(self):
        env, events, mixer_state = self.active_airplay_fixture(pa=1, force=0)
        result = subprocess.run(
            ['sh', str(HOOK), 'repair-stock-pa'], env=env,
            capture_output=True, text=True, timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = events.read_text().splitlines()
        self.assertLess(lines.index('mixer 1 0,0'), lines.index('pa 0'))
        self.assertLess(lines.index('pa 0'), lines.index('mixer 1 77,77'))
        self.assertEqual(json.loads(mixer_state.read_text())['1'], '77,77')
        state = json.loads((self.root / 'pa-state.json').read_text())
        self.assertEqual((state['pa'], state['force']), (0, 0))

    def test_active_airplay_repairs_a_global_mute_before_restoring_gain(self):
        env, events, mixer_state = self.active_airplay_fixture(pa=1, force=1)
        result = subprocess.run(
            ['sh', str(HOOK), 'repair-stock-pa'], env=env,
            capture_output=True, text=True, timeout=12,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = events.read_text().splitlines()
        self.assertLess(
            lines.index('mixer 1 0,0'),
            lines.index('global unmute with silent clock'),
        )
        self.assertLess(
            lines.index('global unmute with silent clock'),
            lines.index('mixer 1 77,77'),
        )
        self.assertEqual(json.loads(mixer_state.read_text())['1'], '77,77')
        state = json.loads((self.root / 'pa-state.json').read_text())
        self.assertEqual((state['pa'], state['force']), (0, 0))

    def test_active_airplay_repair_retry_restores_original_gain(self):
        env, events, mixer_state = self.active_airplay_fixture(pa=1, force=1)
        delayed = env | {'MOCK_NO_UNMUTE_ACK': '1'}
        first = subprocess.run(
            ['sh', str(HOOK), 'repair-stock-pa'], env=delayed,
            capture_output=True, text=True, timeout=12,
        )
        self.assertNotEqual(first.returncode, 0)
        self.assertEqual(json.loads(mixer_state.read_text())['1'], '0,0')
        saved = (self.run / 'airplay-mute-repair-gain').read_text().split()
        self.assertEqual(saved[2:], ['77,77'])

        second = subprocess.run(
            ['sh', str(HOOK), 'repair-stock-pa'], env=env,
            capture_output=True, text=True, timeout=12,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(mixer_state.read_text())['1'], '77,77')
        self.assertFalse((self.run / 'airplay-mute-repair-gain').exists())

    def test_active_airplay_repair_does_not_restore_a_previous_generation_gain(self):
        env, events, mixer_state = self.active_airplay_fixture(pa=1, force=1)
        delayed = env | {'MOCK_NO_UNMUTE_ACK': '1'}
        first = subprocess.run(
            ['sh', str(HOOK), 'repair-stock-pa'], env=delayed,
            capture_output=True, text=True, timeout=12,
        )
        self.assertNotEqual(first.returncode, 0)
        lease = self.run / 'safe/lease'
        fields = lease.read_text().split()
        fields[3] = 'replacement-token'
        lease.write_text(' '.join(fields) + '\n')

        second = subprocess.run(
            ['sh', str(HOOK), 'repair-stock-pa'], env=env,
            capture_output=True, text=True, timeout=12,
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(mixer_state.read_text())['1'], '100,100')
        self.assertFalse((self.run / 'airplay-mute-repair-gain').exists())

    def run_airplay_takeover(self, refuse_pause=False, orphaned_global=False, no_bt_hardware=False):
        env, pcm, events, acknowledge = self.bluetooth_start_fixture()
        env.pop('HK_AP2_BT')
        env.update({'MOCK_OWNER_PROOF': str(self.root / 'bt-pcm-owner'),
                    'MOCK_PHONE_STATUS': str(self.root / 'phone-status')})
        if refuse_pause: env['MOCK_REFUSE_PAUSE'] = '1'
        (self.root / 'phone-status').write_text('play')
        (self.run / 'shairport.pid').write_text(str(os.getpid()))
        (self.run / 'dsp-bridge.pid').write_text('99')
        (self.root / 'proc/99').mkdir()
        (self.root / 'proc/99/comm').write_text('hk-ap2-dsp-bri')
        (self.root / 'pa-state.json').write_text(json.dumps({'pa': int(orphaned_global), 'force': int(orphaned_global), 'gate': 1}))
        if orphaned_global: (self.root / 'bt-cold-global-mute').write_text('old-generation')
        control = socket.socket(socket.AF_UNIX)
        control.bind(str(self.root / 'bt-pcm-control'))
        player = socket.socket(socket.AF_UNIX)
        player.bind(str(self.root / 'player-control'))
        env['HK_BT_PLAYER_SOCKET'] = str(self.root / 'player-control')
        if no_bt_hardware:
            pcm.write_text('closed\n')
            (self.root / 'bt-pcm-owner').unlink()
            (self.run / 'bt-owner.pid').unlink()
        def transfer():
            deadline = time.monotonic() + 8
            while not no_bt_hardware and time.monotonic() < deadline:
                if (self.run / 'bt-release').exists():
                    (self.run / 'bt-owner.pid').unlink(missing_ok=True)
                    break
                time.sleep(.01)
            while time.monotonic() < deadline:
                if (self.run / 'safe/play.started').exists():
                    pcm.write_text('state: RUNNING\nowner_pid : 99\n')
                    acknowledge()
                    return
                time.sleep(.01)
        thread = threading.Thread(target=transfer); thread.start()
        try:
            result = subprocess.run(['sh', str(HOOK), 'start'], env=env, capture_output=True, text=True, timeout=25)
        finally:
            thread.join(9); control.close(); player.close()
        return result, events.read_text().splitlines()

    def test_airplay_takeover_parks_then_pauses_phone_before_opening_output(self):
        result, events = self.run_airplay_takeover()
        self.assertEqual(result.returncode, 0, result.stderr + (self.run / 'hook.log').read_text())
        self.assertLess(events.index('parked Bluetooth hardware'), events.index('phone pause'))
        self.assertLess(events.index('phone pause'), events.index('mixer Ext Spk on'))
        self.assertEqual((self.root / 'phone-status').read_text(), 'stop')

    def test_unconfirmed_phone_pause_opens_airplay_and_schedules_retry(self):
        result, events = self.run_airplay_takeover(refuse_pause=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('mixer Ext Spk on', events)
        self.assertTrue((self.run / 'bt-pause-pending').exists())
        self.assertIn(
            'Bluetooth pause not acknowledged; AirPlay proceeds with retry pending',
            (self.run / 'hook.log').read_text(),
        )

    def test_next_guarded_airplay_start_recovers_orphaned_global_override(self):
        result, events = self.run_airplay_takeover(orphaned_global=True)
        self.assertEqual(result.returncode, 0, result.stderr + (self.run / 'hook.log').read_text())
        self.assertLess(events.index('global unmute with silent clock'), events.index('mixer Ext Spk on'))
        self.assertFalse((self.root / 'bt-cold-global-mute').exists())

    def test_airplay_pauses_connected_phone_even_without_bluetooth_hardware(self):
        result, events = self.run_airplay_takeover(no_bt_hardware=True)
        self.assertEqual(result.returncode, 0, result.stderr + (self.run / 'hook.log').read_text())
        self.assertNotIn('parked Bluetooth hardware', events)
        self.assertLess(events.index('phone pause'), events.index('mixer Ext Spk on'))

    def test_already_unmuted_bluetooth_skips_redundant_notification(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_ALREADY_UNMUTED': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('global unmute with silent clock', events)

    def test_pa_only_mute_is_synchronized_even_if_global_flag_is_clear(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_ONLY_PA_MUTED': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('global unmute with silent clock', events)
        self.assertIn('using verified native unmute', (self.run / 'hook.log').read_text())
        self.assertIn('pa 0', events)
        self.assertEqual(json.loads((self.root / 'pa-state.json').read_text())['pa'], 0)

    def test_pa_only_cold_start_is_already_safe_without_global_ack(self):
        result, events, state = self.run_bluetooth_start_case({
            'MOCK_COLD_PA_ONLY': '1',
            'MOCK_PREMUTE_NO_ACK': '1',
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = events.splitlines()
        self.assertLess(lines.index('global pre-mute'), lines.index('mixer aml audio i2s mute off'))
        self.assertLess(lines.index('mixer aml audio i2s mute off'), lines.index('pa 0'))
        self.assertLess(lines.index('pa 0'), lines.index('mixer Ext Spk on'))

    def test_clock_loss_before_cold_pre_mute_is_recoverable_and_stays_silent(self):
        env, pcm, events, acknowledge = self.bluetooth_start_fixture()
        (self.run / 'bt-pa-ready').unlink()
        (self.root / 'pa-state.json').write_text(json.dumps({'pa': 1, 'force': 0, 'gate': 1}))
        pcm.write_text('closed\n')
        thread = threading.Thread(target=acknowledge)
        thread.start()
        result = subprocess.run(
            ['sh', str(HOOK), 'start'], env=env,
            capture_output=True, text=True, timeout=25,
        )
        thread.join(timeout=2)
        self.assertEqual(
            result.returncode, 10,
            result.stderr + '\n' +
            ((self.run / 'hook.log').read_text() if (self.run / 'hook.log').exists() else '') +
            '\n' +
            (events.read_text() if events.exists() else ''),
        )
        self.assertIn('start deferred: Bluetooth clock', (self.run / 'hook.log').read_text())
        self.assertFalse(events.exists() and 'mixer aml audio i2s mute off' in events.read_text())
        self.assertFalse(events.exists() and 'mixer Ext Spk on' in events.read_text())
        self.assertFalse((self.run / 'safe/lease').exists())
        self.assertFalse((self.run / 'bt-cold-global-mute').exists())

    def test_global_ack_without_amplifier_ack_never_restores_gain(self):
        result, events, state = self.run_bluetooth_start_case({'MOCK_PA_STAYS_MUTED': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('mixer Ext Spk on', events)
        self.assertEqual(state['1'], '0,0')

    def test_airplay_does_not_use_bluetooth_global_mute_synchronization(self):
        env, pcm, events, acknowledge = self.bluetooth_start_fixture()
        env.pop('HK_AP2_BT')
        env['HK_AP2_STOCK_AUDIO_STATE'] = str(self.root / 'missing-reader')
        (self.run / 'bt-owner.pid').unlink()
        (self.root / 'bt-pcm-owner').unlink()
        (self.run / 'shairport.pid').write_text(str(os.getpid()))
        (self.run / 'dsp-bridge.pid').write_text('42\n')
        (self.root / 'proc/42/comm').write_text('hk-ap2-dsp-bri\n')
        (self.root / 'pa-state.json').write_text(json.dumps({'pa': 1, 'force': 0, 'gate': 1}))
        thread = threading.Thread(target=acknowledge); thread.start()
        result = subprocess.run(['sh', str(HOOK), 'start'], env=env, capture_output=True, text=True, timeout=25)
        thread.join(timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr + '\n' + (self.run / 'hook.log').read_text())
        self.assertNotIn('global unmute with silent clock', events.read_text())
        self.assertEqual(events.read_text().splitlines().count('pa 1'), 1)
        self.assertEqual(events.read_text().splitlines().count('pa 0'), 1)
        self.assertRegex((self.run / 'bt-wait-pcm').read_text(), r'^airplay-[0-9]+$')

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


class BluetoothPhonePauseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.state = self.root / 'status'
        self.state.write_text('{"mode":"41","status":"play"}')
        self.calls = self.root / 'calls'
        self.request = self.root / 'request'
        self.request.write_text('''#!/bin/sh
if [ "$2" = getPlayerStatus ]; then
  [ "${MOCK_STATUS_FAIL:-0}" != 1 ] || exit 1
  cat "$MOCK_STATUS"
  exit 0
fi
echo "$2" >>"$MOCK_CALLS"
if [ "$2" = setPlayerCmd:onepause ] && [ "${MOCK_REFUSE_PAUSE:-0}" != 1 ]; then
    echo '{"mode":"41","status":"stop"}' >"$MOCK_STATUS"
fi
''')
        self.request.chmod(0o755)
        self.env = os.environ | {'HK_AP2_RUN': str(self.root), 'HK_AP2_REQUEST': str(self.request),
                                 'HK_AUDIO_COMMON': str(HOOK.parent / 'hk-audio-common.sh'),
                                 'MOCK_STATUS': str(self.state), 'MOCK_CALLS': str(self.calls)}

    def tearDown(self):
        self.tmp.cleanup()

    def run_pause(self, extra=None):
        functions = HOOK.read_text().split('\ncase "${1:-}" in', 1)[0]
        return subprocess.run(['sh', '-c', functions + '\npause_bluetooth_for_airplay'],
                              env=self.env | (extra or {}), capture_output=True, text=True, timeout=12)

    def test_takeover_pauses_phone_and_does_not_schedule_resume(self):
        p = self.run_pause()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.calls.read_text(), 'setPlayerCmd:onepause\n')
        self.assertIn('stop', self.state.read_text())
        self.assertTrue((self.root / 'bt-wait-pcm').exists())
        self.assertFalse((self.root / 'bt-pause-pending').exists())

    def test_delivery_without_pause_ack_keeps_airplay_available_and_retries(self):
        p = self.run_pause({'MOCK_REFUSE_PAUSE': '1'})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.calls.read_text().count('onepause'), 1)
        self.assertRegex(
            (self.root / 'bt-pause-pending').read_text(),
            r'^airplay-[0-9]+$',
        )

    def test_status_rpc_failure_keeps_airplay_available_and_retries(self):
        p = self.run_pause({'MOCK_STATUS_FAIL': '1'})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertFalse(self.calls.exists())
        self.assertRegex(
            (self.root / 'bt-pause-pending').read_text(),
            r'^airplay-[0-9]+$',
        )
        self.assertRegex(
            (self.root / 'bt-wait-pcm').read_text(),
            r'^airplay-[0-9]+$',
        )

    def test_already_paused_phone_is_not_toggled(self):
        self.state.write_text('{"mode":"41","status":"stop"}')
        self.assertEqual(self.run_pause().returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assertRegex((self.root / 'bt-wait-pcm').read_text(), r'^airplay-[0-9]+$')
        self.assertFalse((self.root / 'bt-pause-pending').exists())

    def test_disconnected_malformed_stock_status_does_not_pause_another_source(self):
        self.state.write_text('{"mode":"-1","status":"curpos":"0"}')
        self.assertEqual(self.run_pause().returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assertRegex((self.root / 'bt-wait-pcm').read_text(), r'^airplay-[0-9]+$')
        self.assertFalse((self.root / 'bt-pause-pending').exists())

    def test_missing_or_invalid_mode_keeps_airplay_available_and_blocks_late_frames(self):
        for payload in ('{"status":"play"}', '{"mode":"unknown","status":"play"}'):
            with self.subTest(payload=payload):
                self.state.write_text(payload)
                self.assertEqual(self.run_pause().returncode, 0)
                self.assertFalse(self.calls.exists())
                self.assertTrue((self.root / 'bt-pause-pending').exists())
                self.assertRegex(
                    (self.root / 'bt-wait-pcm').read_text(),
                    r'^airplay-[0-9]+$',
                )
                (self.root / 'bt-pause-pending').unlink()

    def test_unknown_numeric_phone_mode_proceeds_but_blocks_late_frames(self):
        self.state.write_text('{"mode":"42","status":"play"}')
        result = self.run_pause()
        self.assertEqual(result.returncode, 0)
        self.assertFalse(self.calls.exists())
        self.assertRegex(
            (self.root / 'bt-wait-pcm').read_text(),
            r'^airplay-[0-9]+$',
        )
        self.assertTrue((self.root / 'bt-pause-pending').exists())


if __name__ == '__main__':
    unittest.main()
