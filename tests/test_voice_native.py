"""Compile the actual voice client natively; execute only offline subcommands."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

REPO=Path(__file__).resolve().parents[1]


class VoiceNativeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='hk-native-')
        cls.root=Path(cls.temp.name)
        cls.binary=cls.root/'voice'
        stub=cls.root/'trim.c'
        stub.write_text('#include <stddef.h>\nint malloc_trim(size_t n) {(void)n;return 0;}\n')
        subprocess.run(['cc','-O1',str(REPO/'voice/hk-voice.c'),str(stub),'-o',str(cls.binary)],
                       check=True,capture_output=True)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        self.work=tempfile.TemporaryDirectory(prefix='hk-native-case-')
        self.path=Path(self.work.name)
        (self.path/'conf').touch()
        self.env=os.environ|{'HK_VOICE_CONF':str(self.path/'conf'),
            'HK_VOICE_REMINDERS':str(self.path/'reminders'),
            'HK_VOICE_VOLUME_STATE':str(self.path/'volume.state'), 'HK_VOICE_SOURCE':'airplay'}

    def tearDown(self): self.work.cleanup()

    def run_voice(self,*args):
        return subprocess.run([str(self.binary),*args],env=self.env,capture_output=True,text=True,timeout=3)

    def test_reminder_is_retained_until_explicit_acknowledgement(self):
        reminders=self.path/'reminders'
        original=f'{int(time.time())-1} first\n{int(time.time())+1000} later\n'
        reminders.write_text(original)
        due=self.run_voice('due')
        self.assertEqual(due.returncode,0,due.stderr)
        token,text=due.stdout.strip().split('\t')
        self.assertEqual(text,'first')
        self.assertEqual(reminders.read_text(),original)
        self.assertEqual(self.run_voice('due').stdout,due.stdout,'failed playback must be retryable')
        self.assertEqual(self.run_voice('ack',token).returncode,0)
        self.assertNotIn('first',reminders.read_text())
        self.assertIn('later',reminders.read_text())
        self.assertNotEqual(self.run_voice('due').returncode,0)
        self.assertNotEqual(self.run_voice('ack','not-a-token').returncode,0)

    def test_airplay_voice_uses_actual_attenuation_and_honors_mute(self):
        for protocol,db,mute,expected in [(-30,-30,0,-30),(-15,-18,0,-18),(0,-6,0,-6),(-144,-144,1,-144)]:
            with self.subTest(protocol=protocol):
                (self.path/'volume.state').write_text(f'100 {protocol} {db} {mute}\n')
                result=self.run_voice('volume-gain')
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertAlmostEqual(float(result.stdout),expected,places=2)

    def test_invalid_volume_does_not_become_full_scale(self):
        for state in ('100 nan nan 0','100 0 9999 0','corrupt'):
            (self.path/'volume.state').write_text(state)
            result=self.run_voice('volume-gain')
            self.assertLessEqual(float(result.stdout),-20)

    def test_airplay_tcp_columns_ipv4_ipv6_and_false_remote_port(self):
        source=(REPO/'voice/hk-voice.c').read_text()
        f='static int ap2_table_connected('+source.split('static int ap2_table_connected(',1)[1].split('\nstatic int ap2_connected',1)[0]
        fixture='''#include <stdio.h>
#include <string.h>
'''+f+'''
int main(void) {
 const char *rows[]={
 " 0: 0100007F:1B58 0200007F:ABCD 01 0\\n",
 " 1: 00000000000000000000000001000000:1B58 00000000000000000000000002000000:ABCD 01 0\\n",
 " 0: 0100007F:AAAA 0200007F:1B58 01 0\\n",
 " 0: 0100007F:1B58 00000000:0000 0A 0\\n", "corrupt\\n"};
 for(int i=0;i<5;i++) printf("%d\\n",ap2_table_connected(rows[i]));
 return 0;
}
'''
        c=self.path/'detector.c';c.write_text(fixture)
        binary=self.path/'detector'
        subprocess.run(['cc',str(c),'-o',str(binary)],check=True,capture_output=True)
        result=subprocess.check_output([str(binary)],text=True)
        self.assertEqual(result.splitlines(),['1','1','0','0','0'])


class VolumeHookTest(unittest.TestCase):
    def test_endpoints_mute_and_corrupt_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'clock').write_text('100.00 0.00\n')
            env=os.environ|{'HK_AP2_RUN':str(root),'HK_AUDIO_COMMON':str(REPO/'airplay2/hk-audio-common.sh'),
                'HK_AUDIO_UPTIME_FILE':str(root/'clock'),
                'HK_AP2_VOLUME_CONF':str(REPO/'airplay2/shairport-sync.conf')}
            hook=REPO/'airplay2/runtime/hk-ap2-volume-log'
            for value,expected,mute in [('-30',-30,0),('-15',-18,0),('0',-6,0),('-144',-144,1)]:
                result=subprocess.run(['sh',str(hook),value],env=env,capture_output=True,text=True)
                self.assertEqual(result.returncode,0,result.stderr)
                fields=(root/'volume.state').read_text().split()
                self.assertEqual(float(fields[2]),expected)
                self.assertEqual(int(fields[3]),mute)
            old=(root/'volume.state').read_text()
            self.assertNotEqual(subprocess.run(['sh',str(hook),'nan'],env=env,capture_output=True).returncode,0)
            self.assertEqual((root/'volume.state').read_text(),old)


if __name__=='__main__': unittest.main()
