"""Integration regressions for real shell transitions with fake audio hardware."""
import json
import os
import signal
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
COMMON = REPO / 'airplay2/hk-audio-common.sh'
HOOK = REPO / 'airplay2/hk-ap2-audio-hook.sh'
WATCHDOG = REPO / 'airplay2/runtime/hk-ap2-safe-test'


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hk-life-')
        self.root = Path(self.tmp.name)
        self.run = self.root / 'run'
        self.safe = self.run / 'safe'
        self.safe.mkdir(parents=True)
        (self.root / 'proc/42').mkdir(parents=True)
        (self.root / 'voice').mkdir()
        (self.root / 'clock').write_text('100.00 0.00\n')
        (self.safe / 'armed').touch()
        (self.safe / 'watchdog.pid').write_text(str(os.getpid()))
        (self.safe / 'watchdog.heartbeat').write_text('100')
        (self.root / 'voice/voice.pid').write_text(str(os.getpid()))
        (self.root / 'pcm').write_text('state: RUNNING\nowner_pid : 42\n')
        (self.root / 'mixer').write_text(json.dumps({'Master':'0', '1':'0,0','2':'1,1',
            'Ext Spk':'off','Digital':'off','aml audio i2s mute':'on'}))
        self.events = self.root / 'events'
        self.events.touch()
        self.executable('hk-ap2-audio-hook.sh', '#!/bin/sh\nexec sh \"$REAL_HOOK\" \"$@\"\n')
        self.executable('amixer', '''#!/usr/bin/env python3
import json,os,sys
p=os.environ['MIXER_STATE']; a=sys.argv[3:]; k=a[1].replace('numid=','')
s=json.load(open(p))
if a[0] in ('sset','cset'):
 s[k]=a[2]; temporary=p+'.'+str(os.getpid()); json.dump(s,open(temporary,'w')); os.replace(temporary,p)
 open(os.environ['EVENTS'],'a').write(k+' '+a[2]+'\\n')
elif a[0]=='cget':
 if k=='2' and os.environ.get('ANALOG_PREVIOUS')==s[k]:
  open(os.environ['ANALOG_READ'],'w').close()
 print('  : values='+s[k])
elif k=='Master': print('Front Left: '+s[k])
else: print('Front Left: Playback ['+s[k]+']')
''')
        self.executable('usleep', '#!/bin/sh\nsleep 0.005\n')
        self.env = os.environ | {
            'HK_AP2_ROOT':str(self.root), 'HK_AP2_RUN':str(self.run),
            'HK_AP2_PROC_ROOT':str(self.root/'proc'), 'HK_AP2_PCM_STATUS':str(self.root/'pcm'),
            'HK_AP2_AMIXER':str(self.root/'amixer'), 'HK_AP2_USE_STOCK_PA':'0',
            'HK_AUDIO_COMMON':str(COMMON), 'HK_AUDIO_UPTIME_FILE':str(self.root/'clock'),
            'HK_AP2_HOLD_MUTE_FILE':str(self.root/'hold'), 'HK_VOICE_RUN':str(self.root/'voice'),
            'HK_AP2_DIGITAL_SAFE':'100', 'HK_AP2_MASTER_SAFE':'4', 'HK_AP2_PLAYBACK_SAFE':'1',
            'MIXER_STATE':str(self.root/'mixer'), 'EVENTS':str(self.events), 'REAL_HOOK':str(HOOK),
            'PATH':str(self.root)+':'+os.environ['PATH'],
        }

    def tearDown(self):
        self.tmp.cleanup()

    def executable(self, name, body):
        p=self.root/name; p.write_text(body); p.chmod(0o755)

    def call(self, *args, env=None):
        return subprocess.run(['sh',str(HOOK),*args],env=env or self.env,
                              capture_output=True,text=True,timeout=8)

    def start(self, source):
        if source=='voice':
            self.env['HK_AP2_VOICE']='1'
            if self.env.get('HK_AUDIO_TOKEN'):
                (self.run/'voice-token').write_text(self.env['HK_AUDIO_TOKEN'])
                (self.run/'current-voice-token').write_text(self.env['HK_AUDIO_TOKEN'])
            (self.run/'voice-owns').write_text('100')
            (self.root/'proc/42/comm').write_text('aplay\n')
            # This exact sequence used to fail: guard needed a lease that only start created.
            self.assertNotEqual(self.call('guard').returncode,0)
            self.assertEqual(self.call('reserve').returncode,0)
            self.assertEqual(self.call('guard').returncode,0)
            (self.safe/'play.started').unlink()
            (self.safe/'lease').unlink()
        else:
            self.env['HK_AP2_BT']='1'
            (self.run/'bt-owner.pid').write_text(str(os.getpid()))
            (self.run/'bt-heartbeat').write_text('100')
            (self.root/'proc/42/comm').write_text('a01controller\n')
        def ack():
            end=time.monotonic()+4
            while not (self.safe/'play.started').exists() and time.monotonic()<end: time.sleep(.01)
            if self.safe.exists():
                lease = self.safe/'lease'
                if not lease.exists():
                    return
                request = self.safe/'lease.request'
                while not request.exists() and time.monotonic()<end: time.sleep(.01)
                if not request.exists():
                    return
                token, nonce = (self.safe/'lease.request').read_text().split()
                (self.safe/'lease.ack').write_text(token+' '+nonce+'\n')
                # /proc/uptime reads and the real watchdog's rename never
                # expose a truncated empty value to a concurrent reader.
                clock_next = self.root/'clock.next'
                clock_next.write_text('101.00 0.00\n')
                clock_next.replace(self.root/'clock')
                beat_next = self.safe/'watchdog.heartbeat.next'
                beat_next.write_text('101')
                beat_next.replace(self.safe/'watchdog.heartbeat')
        t=threading.Thread(target=ack);t.start()
        result=self.call('start');t.join(5)
        self.assertEqual(result.returncode,0,result.stderr+(self.run/'hook.log').read_text())

    def test_idle_voice_reserves_then_opens_and_stops_on_running_clock(self):
        self.start('voice')
        self.assertTrue((self.safe/'lease').read_text().endswith('active\n'))
        self.events.write_text('')
        self.assertEqual(self.call('stop').returncode,0)
        events=self.events.read_text().splitlines()
        self.assertLess(events.index('1 0,0'),events.index('Ext Spk off'))
        self.assertLess(events.index('Ext Spk off'),events.index('aml audio i2s mute on'))
        self.assertFalse((self.safe/'lease').exists())
        self.assertNotIn('2 0,0', events, 'normal stop must not toggle analog gain')

    def test_repeated_stop_is_idempotent_and_leaves_no_lease(self):
        self.start('voice')
        self.assertEqual(self.call('stop').returncode, 0)
        events_after_first = self.events.read_text()
        result = self.call('stop')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.safe / 'lease').exists())
        self.assertFalse((self.safe / 'play.started').exists())
        self.assertGreaterEqual(
            len(self.events.read_text().splitlines()),
            len(events_after_first.splitlines()),
        )

    def test_airplay_stop_clears_only_its_wait_marker(self):
        wait = self.run/'bt-wait-pcm'
        pending = self.run/'bt-pause-pending'
        wait.write_text('airplay-100\n')
        pending.write_text('airplay-100\n')
        self.assertEqual(self.call('stop').returncode, 0)
        self.assertFalse(wait.exists())
        self.assertFalse(pending.exists())

        wait.write_text('backend-clock-stopped\n')
        pending.write_text('airplay-other\n')
        self.assertEqual(self.call('stop').returncode, 0)
        self.assertEqual(wait.read_text(), 'backend-clock-stopped\n')
        self.assertEqual(pending.read_text(), 'airplay-other\n')

    def test_ap2_resource_trip_preserves_valid_bluetooth(self):
        self.start('bluetooth')
        (self.run/'bt-heartbeat').write_text('101')
        guard=(REPO/'airplay2/runtime/hk-ap2-guard.sh').read_text().split('\nlog "guard started',1)[0]
        shell=guard+'\nstop_kind() { :; }\ntrip "shairport exited"\n'
        env=self.env.copy();env.pop('HK_AP2_BT')
        result=subprocess.run(['sh','-c',shell],env=env,capture_output=True,text=True,timeout=8)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((self.safe/'play.started').exists())
        self.assertEqual(json.loads((self.root/'mixer').read_text())['Ext Spk'],'on')

    def test_old_generation_stop_cannot_close_current_owner(self):
        self.env['HK_AUDIO_TOKEN']='new-generation'
        self.start('voice')
        self.events.write_text('')
        result=self.call('stop',env=self.env|{'HK_AUDIO_TOKEN':'old-generation'})
        self.assertEqual(result.returncode,0)
        self.assertEqual(self.events.read_text(),'')
        self.assertIn('new-generation',(self.safe/'lease').read_text())

    def test_reverse_stop_without_its_original_lease_does_not_touch_audio(self):
        result = self.call('stop', env=self.env | {'HK_AUDIO_TOKEN':'old-generation',
                                                  'HK_AP2_REVERSE_STOP':'1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events.read_text(), '')
        self.assertFalse((self.safe/'lease').exists())

    def test_reverse_stop_rejects_new_or_priming_airplay_generation(self):
        (self.run/'shairport.pid').write_text(str(os.getpid()))
        shell = '. "$HK_AUDIO_COMMON"; SAFE=$HK_AP2_RUN/safe; RUN=$HK_AP2_RUN; '
        shell += 'audio_reserve && lease_phase=active && audio_write_lease'
        subprocess.run(['sh','-c',shell],env=self.env|{'HK_AUDIO_TOKEN':'new-generation'},
                       check=True,capture_output=True,text=True)
        for token, phase in [('old-generation','active'),('new-generation','priming')]:
            with self.subTest(token=token,phase=phase):
                lease=(self.safe/'lease'); fields=lease.read_text().split(); fields[-1]=phase
                lease.write_text(' '.join(fields)+'\n')
                result=self.call('stop',env=self.env|{'HK_AUDIO_TOKEN':token,'HK_AP2_REVERSE_STOP':'1'})
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(self.events.read_text(),'')
                self.assertTrue(lease.exists())
        lease = self.safe / 'lease'
        fields = lease.read_text().split()
        fields[-1] = 'active'
        lease.write_text(' '.join(fields) + '\n')
        result = self.call('stop', env=self.env | {
            'HK_AUDIO_TOKEN': 'new-generation',
            'HK_AUDIO_LEASE_TIME': str(int(fields[4]) + 1),
            'HK_AP2_REVERSE_STOP': '1',
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.events.read_text(), '')
        self.assertTrue(lease.exists())

    def test_reverse_stop_mutes_the_matching_active_airplay_lease(self):
        (self.run/'shairport.pid').write_text(str(os.getpid()))
        shell = '. "$HK_AUDIO_COMMON"; SAFE=$HK_AP2_RUN/safe; RUN=$HK_AP2_RUN; '
        shell += 'audio_reserve && lease_phase=active && audio_write_lease'
        env=self.env|{'HK_AUDIO_TOKEN':'current-generation','HK_AP2_REVERSE_STOP':'1'}
        subprocess.run(['sh','-c',shell],env=env,check=True,capture_output=True,text=True)
        lease_time=(self.safe/'lease').read_text().split()[4]
        result=self.call('stop',env=env|{'HK_AUDIO_LEASE_TIME':lease_time})
        self.assertEqual(result.returncode,0,result.stderr)
        events=self.events.read_text().splitlines()
        self.assertLess(events.index('1 0,0'),events.index('Ext Spk off'))
        self.assertLess(events.index('Ext Spk off'),events.index('aml audio i2s mute on'))
        self.assertFalse((self.safe/'lease').exists())

    def test_old_generation_start_cannot_mute_current_owner(self):
        self.env['HK_AUDIO_TOKEN']='new-generation'
        self.start('voice')
        self.events.write_text('')
        result=self.call('start',env=self.env|{'HK_AUDIO_TOKEN':'old-generation'})
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.events.read_text(),'')
        self.assertIn('new-generation',(self.safe/'lease').read_text())

    def test_bluetooth_start_preserves_a_live_airplay_claim(self):
        (self.run/'bt-owner.pid').write_text(str(os.getpid()))
        (self.run/'bt-heartbeat').write_text('100')
        start = subprocess.check_output(
            ['sh','-c','. "$HK_AUDIO_COMMON"; audio_pid_start "$1"','sh',
             str(os.getpid())],
            env=self.env,
            text=True,
        ).strip()
        claim = f'100 {os.getpid()} {start} {os.getpid()}-100'
        (self.run/'ap2-claim').write_text(claim+'\n')
        result = self.call('start', env=self.env | {
            'HK_AP2_BT':'1', 'HK_AUDIO_TOKEN':'bt-token',
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.run/'ap2-claim').read_text().strip(), claim)
        self.assertIn('AirPlay start claim is active',
                      (self.run/'hook.log').read_text())

    def test_bluetooth_mix_guard_borrows_its_original_lease(self):
        self.start('bluetooth')
        state=json.loads((self.root/'mixer').read_text());state['1']='0,0'
        (self.root/'mixer').write_text(json.dumps(state));self.events.write_text('')
        source=(REPO/'voice/hk-voice.sh').read_text()
        guard='speaker_guard() {'+source.split('speaker_guard() {',1)[1].split('\n# A silence stream',1)[0]
        env=self.env|{'HOOK':str(HOOK),'HOOK_ENV':'HK_AP2_VOICE=1','HK_AUDIO_TOKEN':'voice-generation'}
        result=subprocess.run(['sh','-c',guard+'\nspeaker_guard\n'],env=env,capture_output=True,text=True,timeout=4)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.events.read_text(),'')

    def test_live_lock_is_not_stolen_after_timeout(self):
        lock=self.run/'speaker.lock';lock.mkdir()
        shell='. "$HK_AUDIO_COMMON"; audio_pid_start "$OWNER_PID"'
        token=subprocess.check_output(['sh','-c',shell],env=self.env|{'OWNER_PID':str(os.getpid())},text=True).strip()
        (lock/'owner').write_text(f'{os.getpid()} {token}\n')
        result=self.call('reserve')
        self.assertNotEqual(result.returncode,0)
        self.assertTrue((lock/'owner').exists())
        self.assertEqual(self.events.read_text(),'')

    def test_ownerless_lock_is_recovered_after_the_creation_grace(self):
        lock = self.run / 'ownerless.lock'
        lock.mkdir()
        result = subprocess.run(
            ['sh', '-c',
             '. "$HK_AUDIO_COMMON"; audio_lock "$1"; rc=$?; '
             'audio_unlock "$1" 2>/dev/null || true; exit "$rc"',
             'sh', str(lock)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=4,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(lock.exists())

    def test_ownerless_lock_recovers_from_a_dead_writer_temp(self):
        dead = subprocess.Popen(['sleep', '30'])
        dead_pid = dead.pid
        dead.kill()
        dead.wait(timeout=3)

        lock = self.run / 'dead-writer.lock'
        lock.mkdir()
        (lock / 'owner').touch()
        (lock / f'owner.tmp.{dead_pid}').write_text('partial')
        result = subprocess.run(
            ['sh', '-c',
             '. "$HK_AUDIO_COMMON"; audio_lock "$1"; rc=$?; '
             'audio_unlock "$1" 2>/dev/null || true; exit "$rc"',
             'sh', str(lock)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=4,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(lock.exists())

    def test_watchdog_identity_and_heartbeat_must_be_current(self):
        shell = '. "$HK_AUDIO_COMMON"; RUN=$HK_AP2_RUN; SAFE=$RUN/safe; audio_watchdog_live'

        def live():
            return subprocess.run(
                ['sh', '-c', shell], env=self.env,
                capture_output=True, text=True, timeout=3,
            )

        start = subprocess.check_output(
            ['sh', '-c', '. "$HK_AUDIO_COMMON"; audio_pid_start "$1"',
             'sh', str(os.getpid())],
            env=self.env, text=True,
        ).strip()
        (self.safe / 'watchdog.identity').write_text(
            f'{os.getpid()} {start}\n')
        (self.safe / 'watchdog.heartbeat').write_text('100\n')
        self.assertEqual(live().returncode, 0)
        (self.safe / 'watchdog.heartbeat').write_text('101\n')
        self.assertNotEqual(live().returncode, 0)
        (self.safe / 'watchdog.heartbeat').write_text('95\n')
        self.assertNotEqual(live().returncode, 0)
        (self.safe / 'watchdog.identity').write_text(
            f'{os.getpid()} {int(start) + 1}\n')
        (self.safe / 'watchdog.heartbeat').write_text('100\n')
        self.assertNotEqual(live().returncode, 0)

    def test_watchdog_transition_grace_is_bounded_and_source_scoped(self):
        shell = (
            '. "$HK_AUDIO_COMMON"; RUN=$HK_AP2_RUN; SAFE=$RUN/safe; '
            'audio_watchdog_transition_live'
        )

        def transition_live():
            return subprocess.run(
                ['sh', '-c', shell], env=self.env,
                capture_output=True, text=True, timeout=4,
            )

        start = subprocess.check_output(
            ['sh', '-c', '. "$HK_AUDIO_COMMON"; audio_pid_start "$1"',
             'sh', str(os.getpid())],
            env=self.env, text=True,
        ).strip()
        (self.safe / 'watchdog.identity').write_text(
            f'{os.getpid()} {start}\n')
        (self.safe / 'watchdog.heartbeat').write_text('90\n')
        (self.root / 'clock').write_text('100.00 0.00\n')
        self.assertNotEqual(
            transition_live().returncode, 0,
            'a stale heartbeat without a source transition must fail',
        )

        lease = self.safe / 'lease'
        lease.write_text(
            f'airplay {os.getpid()} {start} prime 90 priming\n')
        (self.safe / 'play.started').write_text('90\n')
        self.assertEqual(transition_live().returncode, 0)
        lease.write_text(
            f'airplay {os.getpid()} {start} active 90 active\n')
        self.assertNotEqual(
            transition_live().returncode, 0,
            'active playback must retain the strict heartbeat',
        )
        lease.write_text(
            f'airplay {os.getpid()} {start} drain 90 draining\n')
        self.assertEqual(transition_live().returncode, 0)

        lease.unlink()
        (self.safe / 'play.started').unlink()
        (self.run / 'ap2-claim').write_text(
            f'100 {os.getpid()} {start} claim-100\n')
        self.assertEqual(transition_live().returncode, 0)
        (self.safe / 'watchdog.heartbeat').write_text('84\n')
        self.assertNotEqual(
            transition_live().returncode, 0,
            'the transition grace must have a hard upper bound',
        )

    def test_supervisor_distinguishes_dead_watchdog_from_a_transition_delay(self):
        source = (REPO / 'airplay2/hk-ap2-prodctl').read_text()
        function = 'watchdog_supervision_state() {' + source.split(
            'watchdog_supervision_state() {', 1
        )[1].split('\n}\n\nsupervisor_run() {', 1)[0] + '\n}\n'
        shell = '. "$HK_AUDIO_COMMON"\nRUN=$HK_AP2_RUN\nSAFE=$RUN/safe\n' + function

        def state():
            return subprocess.run(
                ['sh', '-c', shell + '\nwatchdog_supervision_state'],
                env=self.env, capture_output=True, text=True, timeout=4,
            )

        start = subprocess.check_output(
            ['sh', '-c', '. "$HK_AUDIO_COMMON"; audio_pid_start "$1"',
             'sh', str(os.getpid())],
            env=self.env, text=True,
        ).strip()
        (self.safe / 'watchdog.identity').write_text(
            f'{os.getpid()} {start}\n')
        (self.safe / 'watchdog.heartbeat').write_text('100\n')
        self.assertEqual(state().stdout.strip(), 'live')

        (self.root / 'clock').write_text('108.00 0.00\n')
        (self.safe / 'watchdog.heartbeat').write_text('100\n')
        (self.run / 'ap2-claim').write_text(
            f'100 {os.getpid()} {start} claim-100\n')
        self.assertEqual(state().stdout.strip(), 'transition')

        (self.safe / 'lease').write_text(
            f'airplay {os.getpid()} {start} active 100 active\n')
        (self.safe / 'play.started').write_text('100\n')
        self.assertEqual(state().stdout.strip(), 'stale')

        (self.safe / 'watchdog.identity').write_text(
            f'{os.getpid()} {int(start) + 1}\n')
        self.assertEqual(state().stdout.strip(), 'dead')

    def test_no_lease_reaper_defers_to_a_live_airplay_transition_claim(self):
        source = (WATCHDOG).read_text()
        function = 'mute_without_active_generation() {' + source.split(
            'mute_without_active_generation() {', 1
        )[1].split('\nread_lease_request() {', 1)[0]
        shell = '''
RUN=$HK_AP2_RUN
SAFE=$RUN/safe
HOLD_MUTE=$HK_AP2_HOLD_MUTE_FILE
audio_claim_recent() { return 0; }
state_lock() { echo unexpected-lock; return 0; }
state_unlock() { :; }
hard_mute() { echo unexpected-mute; }
read_file() { :; }
BT_OWNER=$RUN/bt-owner.pid
BT_HEARTBEAT=$RUN/bt-heartbeat
''' + function + '\nmute_without_active_generation\n'
        result = subprocess.run(
            ['sh', '-c', shell], env=self.env,
            capture_output=True, text=True, timeout=4,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, '')

    def test_lease_token_and_time_must_both_match_the_exact_generation(self):
        start = subprocess.check_output(
            ['sh', '-c', '. "$HK_AUDIO_COMMON"; audio_pid_start "$1"',
             'sh', str(os.getpid())],
            env=self.env, text=True,
        ).strip()
        lease = self.safe / 'lease'
        lease.write_text(
            f'bluetooth {os.getpid()} {start} token123 100 active\n'
        )
        script = '''
. "$HK_AUDIO_COMMON"
RUN=$HK_AP2_RUN
SAFE=$RUN/safe
audio_lease_is_caller
'''
        for token, lease_time, expected in (
                ('wrong', '100', False),
                ('token123', '99', False),
                ('token123', '100', True)):
            with self.subTest(token=token, lease_time=lease_time):
                result = subprocess.run(
                    ['sh', '-c', script], env=self.env | {
                        'HK_AP2_BT': '1', 'HK_AUDIO_TOKEN': token,
                        'HK_AUDIO_LEASE_TIME': lease_time,
                    }, capture_output=True, text=True, timeout=3,
                )
                self.assertEqual(result.returncode == 0, expected, result.stderr)
        lease.write_text(
            f'bluetooth {os.getpid()} {start} token123 100 active extra\n'
        )
        result = subprocess.run(
            ['sh', '-c',
             '. "$HK_AUDIO_COMMON"; RUN=$HK_AP2_RUN; SAFE=$RUN/safe; audio_read_lease'],
            env=self.env, capture_output=True, text=True, timeout=3,
        )
        self.assertNotEqual(result.returncode, 0)

    def test_watchdog_term_releases_speaker_lock_and_identity(self):
        for name in ('lease', 'play.started', 'tripped', 'watchdog.identity'):
            (self.safe / name).unlink(missing_ok=True)
        (self.safe / 'watchdog.pid').write_text(str(os.getpid()))
        (self.safe / 'watchdog.heartbeat').write_text('100')
        (self.run / 'bt-owner.pid').write_text(str(os.getpid()))
        (self.run / 'bt-heartbeat').write_text('100')
        state = json.loads((self.root / 'mixer').read_text())
        state.update({'1': '100,100', '2': '1,1', 'Ext Spk': 'off'})
        (self.root / 'mixer').write_text(json.dumps(state))
        result = self.call('reserve', env=self.env | {'HK_AP2_BT': '1'})
        self.assertEqual(result.returncode, 0, result.stderr)

        watchdog = subprocess.Popen(
            ['sh', str(REPO / 'airplay2/runtime/hk-ap2-safe-test'), '__watchdog'],
            env=self.env | {
                'HK_AP2_DIGITAL_SAFE': '192',
                'HK_AP2_PLAYBACK_SAFE': '1',
                'HK_AP2_BT_PLAYBACK_SAFE': '0',
                'HK_AP2_MAX_PLAY_SECONDS': '0',
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 4
            while not (self.safe / 'tripped').exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
        finally:
            watchdog.terminate()
            watchdog.wait(4)

        self.assertFalse((self.run / 'speaker.lock').exists())
        self.assertFalse((self.safe / 'watchdog.lock').exists())
        self.assertFalse((self.safe / 'watchdog.identity').exists())
        self.assertFalse((self.safe / 'watchdog.pid').exists())
        self.assertFalse((self.safe / 'watchdog.heartbeat').exists())

    def test_watchdog_installs_cleanup_before_acquiring_its_startup_lock(self):
        loop = WATCHDOG.read_text().split('watchdog_loop() {', 1)[1].split(
            '\nstatus() {', 1
        )[0]
        self.assertLess(loop.index("trap 'watchdog_cleanup' 0"),
                        loop.index('audio_lock "$SAFE/watchdog.lock"'))
        self.assertIn('[ -n "${watchdog_self_identity:-}" ]', loop)

    def test_irregular_safety_markers_fail_closed_without_blocking(self):
        armed = self.safe / 'armed'
        fake_armed = self.root / 'fake-armed'
        fake_armed.touch()
        armed.unlink()
        armed.symlink_to(fake_armed)
        try:
            result = self.call('reserve')
            self.assertNotEqual(result.returncode, 0)
        finally:
            armed.unlink()
            armed.touch()

        hold = self.root / 'hold'
        os.mkfifo(hold)
        try:
            result = self.call('reserve')
            self.assertNotEqual(result.returncode, 0)
        finally:
            hold.unlink()

        trip = self.safe / 'tripped'
        trip.mkdir()
        try:
            result = self.call('reserve')
            self.assertNotEqual(result.returncode, 0)
        finally:
            trip.rmdir()

    def test_bluetooth_clock_stop_states_require_the_factory_owner(self):
        (self.root / 'proc/42/comm').write_text('a01controller\n')
        shell = '. "$HK_AUDIO_COMMON"; audio_bt_clock_stopped'
        for state in ('XRUN', 'SETUP', 'PREPARED', 'PAUSED', 'SUSPENDED', 'DRAINING'):
            with self.subTest(state=state):
                (self.root / 'pcm').write_text(f'state: {state}\nowner_pid : 42\n')
                result = subprocess.run(
                    ['sh', '-c', shell], env=self.env,
                    capture_output=True, text=True, timeout=3,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
        (self.root / 'pcm').write_text('closed\n')
        result = subprocess.run(
            ['sh', '-c', shell], env=self.env,
            capture_output=True, text=True, timeout=3,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for state, comm in (
            ('RUNNING', 'a01controller'),
            ('UNKNOWN', 'a01controller'),
            ('XRUN', 'aplay'),
        ):
            with self.subTest(state=state, comm=comm):
                (self.root / 'pcm').write_text(f'state: {state}\nowner_pid : 42\n')
                (self.root / 'proc/42/comm').write_text(comm + '\n')
                result = subprocess.run(
                    ['sh', '-c', shell], env=self.env,
                    capture_output=True, text=True, timeout=3,
                )
                self.assertNotEqual(result.returncode, 0)

    def test_log_rotation_keeps_open_writer_inode_and_bounds_storage(self):
        log=self.root/'long.log'
        with log.open('ab',buffering=0) as writer:
            before=os.fstat(writer.fileno()).st_ino
            for _ in range(3):
                writer.write(b'x'*100000)
                result=subprocess.run(['sh','-c','. "$HK_AUDIO_COMMON"; audio_trim_log "$LOGFILE" 32768'],
                    env=self.env|{'LOGFILE':str(log)},capture_output=True,text=True,timeout=3)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(log.stat().st_ino,before)
                self.assertLessEqual(log.stat().st_size,32768)
            writer.write(b'last')
        self.assertTrue(log.read_bytes().endswith(b'last'))

    def test_rotation_recovers_after_an_old_rotator_dies(self):
        log=self.root/'stale.log';log.write_bytes(b'x'*100000)
        (self.root/'stale.log.trim-lock').write_text('99999999 1\n')
        result=subprocess.run(['sh','-c','. "$HK_AUDIO_COMMON"; audio_trim_log "$LOGFILE" 32768'],
            env=self.env|{'LOGFILE':str(log)},capture_output=True,text=True,timeout=3)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertLessEqual(log.stat().st_size,32768)
        self.assertFalse((self.root/'stale.log.trim-lock').exists())

    def test_optional_airplay_owner_exit_mutes_without_manual_latch(self):
        owner=subprocess.Popen(['sleep','30'])
        watchdog=None
        try:
            (self.run/'shairport.pid').write_text(str(owner.pid))
            reserve='. "$HK_AUDIO_COMMON"; RUN=$HK_AP2_RUN; SAFE=$RUN/safe; audio_reserve'
            result=subprocess.run(['sh','-c',reserve],env=self.env,capture_output=True,text=True,timeout=3)
            self.assertEqual(result.returncode,0,result.stderr)
            watchdog=subprocess.Popen(['sh',str(REPO/'airplay2/runtime/hk-ap2-safe-test'),'__watchdog'],
                env=self.env|{'HK_AP2_MAX_PLAY_SECONDS':'0'},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            time.sleep(.3)
            owner.terminate();owner.wait(3)
            deadline=time.monotonic()+5
            actions=self.safe/'actions.log'
            while ((self.safe/'play.started').exists() or
                   'recoverable' not in
                   (actions.read_text() if actions.exists() else '')) \
                    and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertFalse((self.safe/'play.started').exists())
            self.assertFalse((self.safe/'tripped').exists(),'AP2 recovery must not leave a manual-mute latch')
            self.assertIn('recoverable',actions.read_text())
        finally:
            if owner.poll() is None: owner.kill();owner.wait()
            (self.safe/'armed').unlink(missing_ok=True)
            if watchdog:
                try: watchdog.wait(3)
                except subprocess.TimeoutExpired: watchdog.kill();watchdog.wait()

    def test_optional_airplay_already_exited_before_watchdog_sample_is_recoverable(self):
        owner=subprocess.Popen(['sleep','30'])
        watchdog=None
        try:
            (self.run/'shairport.pid').write_text(str(owner.pid))
            reserve='. "$HK_AUDIO_COMMON"; RUN=$HK_AP2_RUN; SAFE=$RUN/safe; audio_reserve'
            result=subprocess.run(['sh','-c',reserve],env=self.env,
                                  capture_output=True,text=True,timeout=3)
            self.assertEqual(result.returncode,0,result.stderr)
            owner.terminate();owner.wait(3)
            self.assertTrue((self.safe/'lease').exists())
            (self.safe/'lease.request').write_text('airplay-request request-1\n')
            (self.safe/'lease.ack').write_text('stale ack\n')
            watchdog=subprocess.Popen(
                ['sh',str(REPO/'airplay2/runtime/hk-ap2-safe-test'),'__watchdog'],
                env=self.env|{'HK_AP2_MAX_PLAY_SECONDS':'0'},
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            deadline=time.monotonic()+5
            actions=self.safe/'actions.log'
            expected='AP2 stream process ended; hard mute (recoverable)'
            while ((self.safe/'play.started').exists() or
                   (self.safe/'lease').exists() or
                   expected not in (actions.read_text() if actions.exists() else '')) \
                    and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertFalse((self.safe/'play.started').exists())
            self.assertFalse((self.safe/'lease').exists())
            self.assertFalse((self.safe/'lease.request').exists())
            self.assertFalse((self.safe/'lease.ack').exists())
            self.assertFalse(
                (self.safe/'tripped').exists(),
                (self.safe/'tripped').read_text() if (self.safe/'tripped').exists() else '',
            )
            self.assertIn(expected, actions.read_text())
        finally:
            if owner.poll() is None: owner.kill();owner.wait()
            (self.safe/'armed').unlink(missing_ok=True)
            if watchdog:
                try: watchdog.wait(3)
                except subprocess.TimeoutExpired: watchdog.kill();watchdog.wait()

    def test_release_intent_finishes_a_play_marker_removed_before_lease(self):
        owner=subprocess.Popen(['sleep','30'])
        watchdog=None
        try:
            (self.run/'shairport.pid').write_text(str(owner.pid))
            reserve='. "$HK_AUDIO_COMMON"; RUN=$HK_AP2_RUN; SAFE=$RUN/safe; audio_reserve'
            result=subprocess.run(['sh','-c',reserve],env=self.env,
                                  capture_output=True,text=True,timeout=3)
            self.assertEqual(result.returncode,0,result.stderr)
            fields=(self.safe/'lease').read_text().split()
            (self.run/'release-intent').write_text(
                f'{fields[0]} {fields[3]} {fields[4]}\n')
            (self.safe/'play.started').unlink()
            watchdog=subprocess.Popen(
                ['sh',str(REPO/'airplay2/runtime/hk-ap2-safe-test'),'__watchdog'],
                env=self.env|{'HK_AP2_MAX_PLAY_SECONDS':'0'},
                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            deadline=time.monotonic()+5
            actions=self.safe/'actions.log'
            expected='source release in progress; hard mute (recoverable)'
            while ((self.safe/'lease').exists() or
                   expected not in (actions.read_text() if actions.exists() else '')) \
                    and time.monotonic()<deadline:
                time.sleep(.05)
            self.assertFalse((self.safe/'lease').exists())
            self.assertFalse((self.safe/'tripped').exists())
            self.assertIn(expected, actions.read_text())
        finally:
            if owner.poll() is None: owner.kill();owner.wait()
            (self.safe/'armed').unlink(missing_ok=True)
            if watchdog:
                try: watchdog.wait(3)
                except subprocess.TimeoutExpired: watchdog.kill();watchdog.wait()

    def reopen_after_voice(self):
        source = (REPO / 'voice/hk-voice.sh').read_text()
        reopen = 'reopen_watch() {' + source.split('reopen_watch() {', 1)[1].split('\nhandle_wake() {', 1)[0]
        voice = self.root / 'voice'
        sender = self.root / 'connected'
        sender.touch()
        helper = voice / 'hk-voice'
        helper.write_text('#!/bin/sh\n[ "$1" = connected ] && [ -f "$CONNECTED" ]\n')
        helper.chmod(0o755)
        shell = '''. "$HK_AUDIO_COMMON"
log() { printf '%s\\n' "$*" >>"$REOPEN_LOG"; }
airplay_owns_dac() { grep -q '^owner_pid.*42' "$PCM_STATUS"; }
pcm_running() { grep -q '^state: RUNNING' "$PCM_STATUS"; }
mx_get() { "$HK_AP2_AMIXER" -c 0 sget "$1" | sed -n 's/.*\\[\\(on\\|off\\)\\].*/\\1/p'; }
''' + reopen + '\nreopen_watch\n'
        env = self.env | {
            'RUN': str(voice), 'SAFE': str(self.safe), 'D': str(voice),
            'HOOK': str(HOOK), 'HOOK_ENV': '', 'CONNECTED': str(sender),
            'REOPEN_LOG': str(self.root / 'reopen.log'),
            'PCM_STATUS': str(self.root / 'pcm'),
            'RELEASE': str(self.run / 'release-dac'),
            'VOICE_OWNS': str(self.run / 'voice-owns'),
            'BT_OWNER': str(self.run / 'bt-owner.pid'),
        }
        return subprocess.run(['sh', '-c', shell], env=env, capture_output=True, text=True, timeout=10)

    def test_voice_reopens_a_closed_airplay_pipe_before_waiting_for_its_clock(self):
        (self.run / 'shairport.pid').write_text(str(os.getpid()))
        (self.run / 'dsp-bridge.pid').write_text('42\n')
        (self.root / 'proc/42/comm').write_text('hk-ap2-dsp-bridge\n')
        (self.root / 'pcm').write_text('closed\n')
        stop = threading.Event()
        observed = []
        def bridge_and_watchdog():
            while not stop.wait(.01):
                # The actual DSP bridge cannot open until BOTH markers exist.
                if (self.run / 'ap2-claim').exists() and (self.safe / 'lease').exists():
                    observed.append(True)
                    (self.root / 'pcm').write_text('state: RUNNING\nowner_pid : 42\n')
                    (self.root / 'clock').write_text('101.00 0.00\n')
                    request = self.safe / 'lease.request'
                    end = time.monotonic() + 4
                    while not request.exists() and time.monotonic() < end:
                        time.sleep(.01)
                    if not request.exists():
                        return
                    token, nonce = (self.safe / 'lease.request').read_text().split()
                    (self.safe / 'lease.ack').write_text(token + ' ' + nonce + '\n')
                    (self.safe / 'watchdog.heartbeat').write_text('101')
                    return
        thread = threading.Thread(target=bridge_and_watchdog)
        thread.start()
        try:
            result = self.reopen_after_voice()
        finally:
            stop.set()
            thread.join(2)
        details = result.stderr + '\n' + self.events.read_text()
        for path in (self.run / 'hook.log', self.root / 'reopen.log'):
            if path.exists(): details += '\n' + path.read_text()
        self.assertEqual(result.returncode, 0, details)
        self.assertTrue(observed, 'handoff must bootstrap the gated silent clock')
        self.assertTrue((self.safe / 'lease').read_text().endswith('active\n'))
        self.assertIn('reopened and verified', (self.root / 'reopen.log').read_text())
        self.assertEqual(json.loads((self.root / 'mixer').read_text())['Ext Spk'], 'on')
        self.assertFalse((self.root / 'voice/reopen.lock').exists())

    def test_voice_reopen_preserves_faults_and_new_source_ownership(self):
        (self.run / 'shairport.pid').write_text(str(os.getpid()))
        for marker in (self.safe / 'tripped', self.root / 'hold',
                       self.run / 'bt-owner.pid', self.run / 'voice-owns',
                       self.run / 'release-dac'):
            with self.subTest(marker=marker.name):
                marker.touch()
                try:
                    result = self.reopen_after_voice()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(self.events.read_text(), '')
                    self.assertFalse((self.safe / 'lease').exists())
                    self.assertFalse((self.run / 'ap2-claim').exists())
                    self.assertFalse((self.root / 'voice/reopen.lock').exists())
                finally:
                    marker.unlink()

    def test_delayed_voice_reopen_cannot_steal_bluetooth(self):
        # Even if Bluetooth appears after the watcher's source check, the
        # shared hook must check again under its speaker lock.
        (self.run / 'bt-owner.pid').write_text(str(os.getpid()))
        result = self.call('start', env=self.env | {'HK_AP2_REOPEN': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.run / 'bt-release').exists())
        self.assertFalse((self.run / 'ap2-claim').exists())
        self.assertEqual(self.events.read_text(), '')

    def check_analog_handoff(self, source):
        previous = '1,1' if source == 'bluetooth' else '0,0'
        target = '0,0' if source == 'bluetooth' else '1,1'
        state = json.loads((self.root / 'mixer').read_text())
        state['2'] = previous
        (self.root / 'mixer').write_text(json.dumps(state))
        (self.run / 'shairport.pid').write_text(str(os.getpid()))
        (self.run / 'dsp-bridge.pid').write_text('42')
        if source == 'bluetooth':
            (self.run / 'bt-owner.pid').write_text(str(os.getpid()))
            (self.run / 'bt-heartbeat').write_text('100')
            (self.root / 'proc/42/comm').write_text('a01controller\n')
        else:
            (self.root / 'proc/42/comm').write_text('hk-ap2-dsp-bridge\n')
        analog_read = self.root / 'analog-read'
        self.executable('sleep', '#!/bin/sh\nexec /bin/sleep 0.02\n')
        self.executable('usleep', '''#!/bin/sh
if [ "$1" = 300000 ]; then
 i=0
 while [ ! -f "$ANALOG_READ" ] && [ "$i" -lt 150 ]; do /bin/sleep 0.02; i=$((i + 1)); done
 /bin/sleep 0.4
else /bin/sleep 0.02
fi
''')
        env = self.env | {
            'HK_AP2_DIGITAL_SAFE': '192', 'HK_AP2_PLAYBACK_SAFE': '1',
            'HK_AP2_BT_DIGITAL_SAFE': '207', 'HK_AP2_BT_PLAYBACK_SAFE': '0',
            'HK_AP2_MAX_PLAY_SECONDS': '0', 'ANALOG_PREVIOUS': previous,
            'ANALOG_READ': str(analog_read),
        }
        watchdog = subprocess.Popen(['sh', str(REPO / 'airplay2/runtime/hk-ap2-safe-test'), '__watchdog'],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        stop = threading.Event()
        def advance_clock():
            while not stop.wait(.01):
                if (self.safe / 'lease').exists():
                    clock = self.root / 'clock-next'
                    clock.write_text('101.00 0.00\n')
                    clock.replace(self.root / 'clock')
                    if source == 'bluetooth':
                        beat = self.run / 'bt-heartbeat-next'
                        beat.write_text('101')
                        beat.replace(self.run / 'bt-heartbeat')
                    return
        thread = threading.Thread(target=advance_clock)
        thread.start()
        try:
            deadline = time.monotonic() + 3
            while (self.safe / 'watchdog.pid').read_text().strip() != str(watchdog.pid):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.01)
            hook_env = env | {'HK_AP2_PLAYBACK_SAFE': target[0],
                              'HK_AP2_DIGITAL_SAFE': '207' if source == 'bluetooth' else '192'}
            if source == 'bluetooth': hook_env['HK_AP2_BT'] = '1'
            result = subprocess.run(['sh', str(HOOK), 'start'], env=hook_env,
                                    capture_output=True, text=True, timeout=12)
            details = result.stderr + '\n' + (self.run / 'hook.log').read_text()
            if (self.safe / 'actions.log').exists(): details += '\n' + (self.safe / 'actions.log').read_text()
            self.assertEqual(result.returncode, 0, details)
            self.assertTrue(analog_read.exists(), 'watchdog must inspect the old step before the hook changes it')
            self.assertFalse((self.safe / 'tripped').exists(), details)
            self.assertTrue((self.safe / 'lease').read_text().endswith('active\n'))
            self.assertEqual(json.loads((self.root / 'mixer').read_text())['2'], target)

            # The same mismatch after activation must still latch a fault,
            # mute, and leave analogue gain untouched rather than toggling it.
            self.events.write_text('')
            state = json.loads((self.root / 'mixer').read_text())
            state['2'] = previous
            temporary = self.root / 'tampered-mixer'
            temporary.write_text(json.dumps(state))
            temporary.replace(self.root / 'mixer')
            deadline = time.monotonic() + 4
            while not (self.safe / 'tripped').exists():
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            self.assertEqual((self.safe / 'tripped').read_text().strip(), 'analog baseline changed')
            self.assertFalse(any(s.startswith('2 ') for s in self.events.read_text().splitlines()))
        finally:
            stop.set()
            thread.join(2)
            # Let the loop finish its synchronous mixer writes before
            # removing the fixture. Killing only the shell can orphan a
            # mixer child which recreates files during TemporaryDirectory
            # cleanup. Keep a separate process group for bounded fallback.
            (self.safe / 'armed').unlink(missing_ok=True)
            try:
                watchdog.wait(4)
            except subprocess.TimeoutExpired:
                os.killpg(watchdog.pid, signal.SIGKILL)
                watchdog.wait(4)

    def test_bluetooth_priming_accepts_airplay_step_only_before_muted_transition(self):
        self.check_analog_handoff('bluetooth')

    def test_airplay_priming_accepts_bluetooth_step_only_before_muted_transition(self):
        self.check_analog_handoff('airplay')

    def test_analog_priming_exception_rejects_audible_or_unknown_baselines(self):
        for digital, speaker, analog in (('100,100', 'off', '1,1'),
                                         ('0,0', 'on', '1,1'),
                                         ('0,0', 'off', '2,2')):
            with self.subTest(digital=digital, speaker=speaker, analog=analog):
                for name in ('lease', 'play.started', 'tripped'):
                    (self.safe / name).unlink(missing_ok=True)
                (self.safe / 'watchdog.pid').write_text(str(os.getpid()))
                (self.safe / 'watchdog.heartbeat').write_text('100')
                (self.run / 'bt-owner.pid').write_text(str(os.getpid()))
                (self.run / 'bt-heartbeat').write_text('100')
                state = json.loads((self.root / 'mixer').read_text())
                state.update({'1': digital, '2': analog, 'Ext Spk': speaker})
                (self.root / 'mixer').write_text(json.dumps(state))
                self.events.write_text('')
                self.assertEqual(self.call('reserve', env=self.env | {'HK_AP2_BT': '1'}).returncode, 0)
                env = self.env | {'HK_AP2_DIGITAL_SAFE': '192', 'HK_AP2_PLAYBACK_SAFE': '1',
                                  'HK_AP2_BT_PLAYBACK_SAFE': '0', 'HK_AP2_MAX_PLAY_SECONDS': '0'}
                watchdog = subprocess.Popen(['sh', str(REPO / 'airplay2/runtime/hk-ap2-safe-test'), '__watchdog'],
                                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    deadline = time.monotonic() + 4
                    while not (self.safe / 'tripped').exists():
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.02)
                    self.assertEqual((self.safe / 'tripped').read_text().strip(), 'analog baseline changed')
                    action_log = self.safe / 'actions.log'
                    while not (
                        action_log.exists() and
                        'analog baseline changed; hard mute' in action_log.read_text()
                    ):
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.02)
                    self.assertFalse(any(s.startswith('2 ') for s in self.events.read_text().splitlines()))
                finally:
                    watchdog.terminate()
                    watchdog.wait(4)

    def test_supervisor_closes_output_before_dsp_process(self):
        source=(REPO/'airplay2/hk-ap2-prodctl').read_text()
        cleanup='cleanup() {'+source.split('    cleanup() {',1)[1].split('\n    trap cleanup',1)[0]
        shell='''
log() { :; }
hard_mute_raw() { echo mute; }
stop_bt_bridge() { echo bt; }
stop_ap2() { echo ap2; }
stop_dsp_bridge() { echo dsp; }
unmount_runtime() { :; }
'''+cleanup+'\ncleanup\n'
        result=subprocess.run(['sh','-c',shell],env=self.env|{'STOPFILE':str(self.root/'stop'),'PIDFILE':str(self.root/'pid')},
                              capture_output=True,text=True,timeout=3)
        self.assertEqual(result.returncode,0,result.stderr)
        events=result.stdout.splitlines()
        self.assertLess(events.index('mute'),events.index('dsp'))
        self.assertLess(events.index('mute'),events.index('ap2'))

    def test_dsp_hold_blocks_new_output_but_keeps_existing_clock(self):
        source=(REPO/'airplay2/hk-ap2-dsp-bridge.c').read_text()
        functions='static int airplay_clock_held(void) {'+source.split('static int airplay_clock_held(void) {',1)[1].split('\nstatic int write_frames',1)[0]
        c='''
#include <string.h>
#define HK_AP2_RUNTIME_DIR ""
static int held=0,tripped=0,lease=1;
static int file_exists(const char *p) {
 if (strstr(p,"-hold-mute")) return held;
 if (strstr(p,"/tripped")) return tripped;
 if (strstr(p,"/bt-owner.pid") || strstr(p,"/voice-owns")) return 0;
 if (strstr(p,"/play.started")) return lease;
 return 1;
}
'''+functions+'''
int main(void) {
 if (!airplay_allowed() || !airplay_clock_held()) return 1;
 held=1;
 if (airplay_allowed() || !airplay_clock_held()) return 2;
 held=0;tripped=1;
 if (airplay_allowed() || !airplay_clock_held()) return 3;
 lease=0;
 if (airplay_clock_held()) return 4;
 return 0;
}
'''
        src=self.root/'clock.c';src.write_text(c)
        binary=self.root/'clock-test'
        subprocess.run(['cc',str(src),'-o',str(binary)],check=True,capture_output=True)
        self.assertEqual(subprocess.run([str(binary)],timeout=3).returncode,0)


if __name__=='__main__': unittest.main()
