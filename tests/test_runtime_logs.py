"""Check real daemon stdout descriptors against the inode-preserving rotator."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'airplay2/runtime/hk-ap2ctl').read_text()
LAUNCH = 'launch_kind() {' + SOURCE.split('launch_kind() {', 1)[1].split('\npreflight() {', 1)[0]


class RuntimeLogTest(unittest.TestCase):
    def check_writer(self, preload):
        with tempfile.TemporaryDirectory(prefix='hk-log-writer-') as directory:
            root = Path(directory)
            log = root / 'daemon.log'
            loader = root / 'loader'
            loader.write_text('#!/bin/sh\nshift 2\nexec "$@"\n')
            loader.chmod(0o755)
            writer = root / 'writer.py'
            writer.write_text('''import fcntl,json,os,pathlib,time
root=pathlib.Path(os.environ['FIXTURE_ROOT'])
print(json.dumps({str(fd):bool(fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_APPEND) for fd in (1,2)}),flush=True)
os.write(1,b'x'*100000+b'\\n')
(root/'ready').touch()
deadline=time.monotonic()+10
while not (root/'go').exists() and time.monotonic()<deadline:time.sleep(.01)
os.write(1,b'AFTER_ROTATE\\n')
(root/'done').touch()
''')
            env = os.environ | {
                'FIXTURE_ROOT': str(root), 'RUN': str(root), 'LOADER': str(loader),
                'LIBRARY_PATH': str(root), 'INFO_SHIM': str(root / 'absent.so'),
                'NSS_SHIM': str(root / 'absent.so'), 'TEST_LOG': str(log),
                'TEST_PRELOAD': preload, 'TEST_PYTHON': sys.executable, 'TEST_WRITER': str(writer),
                'HK_AUDIO_COMMON': str(ROOT / 'airplay2/hk-audio-common.sh'),
            }
            shell = '''rotate_log() { :; }
pidfile() { printf '%s/%s.pid\\n' "$RUN" "$1"; }
wait_for_pid() { printf '%s\\n' "$pid"; }
set_optional_oom_priority() { :; }
show_log() { :; }
''' + LAUNCH + '\nlaunch_kind shairport "$TEST_LOG" "$TEST_PRELOAD" "$TEST_PYTHON" "$TEST_WRITER"\n'
            def wait_for(name):
                deadline = time.monotonic() + 4
                while not (root / name).exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue((root / name).exists(), name)
            try:
                result = subprocess.run(['sh', '-c', shell], env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                wait_for('ready')
                initial = log.read_bytes()
                flags = next(json.loads(line) for line in initial.splitlines() if line.startswith(b'{'))
                self.assertEqual(flags, {'1': True, '2': True}, 'daemon descriptors must append after truncation')
                inode = log.stat().st_ino
                result = subprocess.run(['sh', '-c', '. "$HK_AUDIO_COMMON"; audio_trim_log "$TEST_LOG" 32768'],
                                        env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                (root / 'go').touch()
                wait_for('done')
                final = log.read_bytes()
                self.assertEqual(log.stat().st_ino, inode)
                self.assertLessEqual(len(final), 32768)
                self.assertNotIn(b'\0', final, 'truncation must not leave sparse holes')
                self.assertTrue(final.endswith(b'AFTER_ROTATE\n'))
            finally:
                (root / 'go').touch()
                if (root / 'ready').exists():
                    wait_for('done')

    def test_plain_daemon_appends_after_live_rotation(self):
        self.check_writer('no')

    def test_preloaded_daemon_appends_after_live_rotation(self):
        self.check_writer('info')


if __name__ == '__main__':
    unittest.main()
