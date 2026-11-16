"""Exercise the production read-only helper against actual SysV shared memory."""
import ctypes
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux SysV shared memory')
class StockAudioStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='hk-audio-state-')
        cls.key = 0x30000000 | secrets.randbits(28)
        cls.exe = Path(cls.temp.name) / 'state'
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror',
                        f'-DHK_STOCK_AUDIO_SHM_KEY={cls.key}',
                        str(ROOT / 'airplay2/hk-stock-audio-state.c'), '-o', str(cls.exe)],
                       check=True, capture_output=True)
        cls.libc = ctypes.CDLL(None, use_errno=True)
        cls.libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
        cls.libc.shmget.restype = ctypes.c_int
        cls.libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        cls.libc.shmat.restype = ctypes.c_void_p
        cls.libc.shmdt.argtypes = [ctypes.c_void_p]
        cls.libc.shmctl.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.segment = None
        self.address = None

    def tearDown(self):
        if self.address is not None:
            self.assertEqual(self.libc.shmdt(self.address), 0)
        if self.segment is not None:
            self.assertEqual(self.libc.shmctl(self.segment, 0, None), 0)  # IPC_RMID

    def allocate(self, size=0x2700):
        self.size = size
        self.segment = self.libc.shmget(self.key, size, 0o1000 | 0o2000 | 0o600)
        self.assertGreaterEqual(self.segment, 0, os.strerror(ctypes.get_errno()))
        self.address = self.libc.shmat(self.segment, None, 0)
        self.assertNotEqual(self.address, ctypes.c_void_p(-1).value)
        ctypes.memset(self.address, 0, size)

    def state(self, pa=0, force=0, gate=1):
        for offset, value in [(0x20bc, pa), (0x2368, force), (0x2614, gate)]:
            ctypes.c_uint32.from_address(self.address + offset).value = value

    def run_state(self, *args):
        return subprocess.run([str(self.exe), *args], text=True, capture_output=True, timeout=5)

    def test_reads_only_audio_flags_and_preserves_the_shared_context(self):
        self.allocate(); self.state(pa=1, force=1)
        before = ctypes.string_at(self.address, self.size)
        p = self.run_state()
        self.assertEqual((p.returncode, p.stdout), (0, 'PA mute=1 force_mute=1 gate=1\n'))
        self.assertEqual(ctypes.string_at(self.address, self.size), before)

    def test_global_and_amplifier_acknowledgements_are_separate(self):
        self.allocate(); self.state(pa=1, force=0)
        self.assertEqual(self.run_state('--global-unmuted').returncode, 0)
        self.assertEqual(self.run_state('--unmuted').returncode, 3)
        self.state(pa=0, force=0)
        self.assertEqual(self.run_state('--unmuted').returncode, 0)
        self.state(pa=0, force=1)
        self.assertEqual(self.run_state('--global-unmuted').returncode, 3)

    def test_absent_short_or_invalid_context_fails_closed(self):
        self.assertEqual(self.run_state('--unmuted').returncode, 2)
        self.allocate(128)
        self.assertEqual(self.run_state('--unmuted').returncode, 2)

    def test_invalid_flags_and_disabled_gate_are_never_acknowledged(self):
        self.allocate(); self.state(force=7)
        self.assertEqual(self.run_state('--unmuted').returncode, 4)
        self.state(gate=0)
        self.assertEqual(self.run_state('--unmuted').returncode, 3)

    def test_unknown_arguments_are_rejected(self):
        self.assertEqual(self.run_state('--write').returncode, 2)
        self.assertEqual(self.run_state('--unmuted', 'extra').returncode, 2)
