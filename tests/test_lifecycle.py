"""Integration regressions for real shell transitions with fake audio hardware."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
COMMON = REPO / 'airplay2/hk-audio-common.sh'
HOOK = REPO / 'airplay2/hk-ap2-audio-hook.sh'


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
 s[k]=a[2]; json.dump(s,open(p,'w')); open(os.environ['EVENTS'],'a').write(k+' '+a[2]+'\\n')
elif a[0]=='cget': print('  : values='+s[k])
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
                (self.root/'clock').write_text('101.00 0.00\n')
                (self.safe/'watchdog.heartbeat').write_text('101')
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
        self.assertNotIn('2 0,0',events,'normal stop must not toggle analog gain')

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

    def test_old_generation_start_cannot_mute_current_owner(self):
        self.env['HK_AUDIO_TOKEN']='new-generation'
        self.start('voice')
        self.events.write_text('')
        result=self.call('start',env=self.env|{'HK_AUDIO_TOKEN':'old-generation'})
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(self.events.read_text(),'')
        self.assertIn('new-generation',(self.safe/'lease').read_text())

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
            while (self.safe/'play.started').exists() and time.monotonic()<deadline: time.sleep(.05)
            self.assertFalse((self.safe/'play.started').exists())
            self.assertFalse((self.safe/'tripped').exists(),'AP2 recovery must not leave a manual-mute latch')
            self.assertIn('recoverable',(self.safe/'actions.log').read_text())
        finally:
            if owner.poll() is None: owner.kill();owner.wait()
            (self.safe/'armed').unlink(missing_ok=True)
            if watchdog:
                try: watchdog.wait(3)
                except subprocess.TimeoutExpired: watchdog.kill();watchdog.wait()

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
