"""Validate retained Bluetooth ownership independently of an expired opener TID."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

COMMON = Path(__file__).resolve().parents[1] / 'airplay2/hk-audio-common.sh'


class PcmHolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.proc = self.root / 'proc'
        self.pid = os.getpid()
        self.holder = self.proc / str(self.pid)
        (self.holder / 'fd').mkdir(parents=True)
        (self.holder / 'comm').write_text('a01controller\n')
        # /proc stat field 22 is field 20 after the parenthesized name.
        (self.holder / 'stat').write_text(f'{self.pid} (a01controller) ' +
                                         ' '.join(['S'] + ['0'] * 18 + ['1234', '0']) + '\n')
        self.device = self.root / 'pcmC0D0p'
        (self.holder / 'fd/26').symlink_to(self.device)
        self.record = self.root / 'bt-pcm-owner'
        self.record.write_text(f'{self.pid} 1234 26 5000000\n')
        self.pcm = self.root / 'pcm-status'
        self.pcm.write_text('state: XRUN\nowner_pid : 999999\n')
        self.env = os.environ | {
            'HK_AP2_PROC_ROOT': str(self.proc), 'HK_AUDIO_ID_PROC_ROOT': str(self.proc),
            'HK_BT_PCM_OWNER': str(self.record), 'HK_BT_PCM_DEVICE': str(self.device),
            'HK_AP2_PCM_STATUS': str(self.pcm), 'HK_AP2_RUN': str(self.root / 'run'),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, command='audio_bt_pcm_owned 999999'):
        return subprocess.run(['sh', '-c', '. "$1"; SAFE="$2/run/safe"; ' + command,
                               'sh', str(COMMON), str(self.root)], env=self.env,
                              capture_output=True, text=True, timeout=3).returncode

    def test_dead_opener_is_accepted_only_with_live_process_and_pcm_descriptor(self):
        self.assertEqual(self.check(), 0)

    def test_reused_opener_tid_does_not_replace_actual_holder_identity(self):
        (self.proc / '999999').mkdir()
        (self.proc / '999999/comm').write_text('unrelated-process\n')
        self.assertEqual(self.check(), 0)

    def test_stale_or_inconsistent_proof_is_rejected(self):
        good = self.record.read_text()
        for value in [f'{self.pid} 1235 26 5000000\n', f'{self.pid} 1234 27 5000000\n',
                      '999999 1234 26 5000000\n', good.rstrip() + ' extra\n', 'invalid\n']:
            with self.subTest(record=value):
                self.record.write_text(value)
                self.assertNotEqual(self.check(), 0)
        self.record.write_text(good)
        (self.holder / 'comm').write_text('wrong-holder\n')
        self.assertNotEqual(self.check(), 0)
        (self.holder / 'comm').write_text('a01controller\n')
        (self.holder / 'fd/26').unlink()
        (self.holder / 'fd/26').symlink_to(self.root / 'another-pcm')
        self.assertNotEqual(self.check(), 0)

    def test_symbolic_proof_is_rejected(self):
        other = self.root / 'other'
        self.record.rename(other)
        self.record.symlink_to(other)
        self.assertNotEqual(self.check(), 0)

    def test_missing_proof_preserves_legacy_live_opener_check(self):
        self.record.unlink()
        self.assertNotEqual(self.check(), 0)
        (self.proc / '999999').mkdir()
        (self.proc / '999999/comm').write_text('a01controller\n')
        self.assertEqual(self.check(), 0)

    def test_stopped_pcm_uses_the_same_descriptor_proof(self):
        self.assertEqual(self.check('audio_bt_clock_stopped'), 0)
        (self.holder / 'fd/26').unlink()
        self.assertNotEqual(self.check('audio_bt_clock_stopped'), 0)
