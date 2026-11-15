"""Real ALSA PCM/Unix control tests; the slave writes a file over null audio."""
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform.startswith('linux'), 'native Linux ALSA ioplug test')
class BluetoothPcmTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_tmp = tempfile.TemporaryDirectory(prefix='hk-bt-pcm-build-')
        cls.build = Path(cls.build_tmp.name)
        cls.plugin = cls.build / 'libasound_module_pcm_hkbtpark.so'
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-DPIC', '-fPIC', '-shared',
                        str(REPO / 'airplay2/hk-bt-pcm.c'), '-lasound', '-pthread', '-ldl',
                        '-o', str(cls.plugin)], check=True, capture_output=True)
        helper = cls.build / 'client.c'
        helper.write_text(r'''
#include <alsa/asoundlib.h>
#include <stdio.h>
#include <stdlib.h>
#include <poll.h>
int main(void) {
    snd_pcm_t *pcm;
    int rc = snd_pcm_open(&pcm, "test_bt", SND_PCM_STREAM_PLAYBACK, SND_PCM_NONBLOCK);
    if (rc < 0) { fprintf(stderr, "open: %s\n", snd_strerror(rc)); return 2; }
    rc = snd_pcm_set_params(pcm, SND_PCM_FORMAT_S16_LE, SND_PCM_ACCESS_RW_INTERLEAVED,
                            2, 48000, 0, 500000);
    if (rc < 0) { fprintf(stderr, "params: %s\n", snd_strerror(rc)); return 3; }
    puts("ready"); fflush(stdout);
    char line[40];
    while (fgets(line, sizeof(line), stdin)) {
        if (line[0] == 'q') break;
        if (line[0] == 'p') {
            struct pollfd fd;
            unsigned short events = 0;
            int n = snd_pcm_poll_descriptors(pcm, &fd, 1);
            if (n == 1 && poll(&fd, 1, 200) > 0)
                snd_pcm_poll_descriptors_revents(pcm, &fd, 1, &events);
            printf("poll %d\n", !!(events & POLLOUT)); fflush(stdout);
            continue;
        }
        short samples[512];
        for (int i = 0; i < 512; ++i) samples[i] = (short)atoi(line);
        long n = snd_pcm_writei(pcm, samples, 256);
        printf("wrote %ld\n", n); fflush(stdout);
    }
    snd_pcm_close(pcm);
    return 0;
}
''')
        cls.client = cls.build / 'client'
        subprocess.run(['cc', str(helper), '-lasound', '-o', str(cls.client)],
                       check=True, capture_output=True)
        delayed = cls.build / 'delayed.c'
        delayed.write_text(r'''
#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int snd_pcm_open(snd_pcm_t **p, const char *name, snd_pcm_stream_t stream, int mode) {
    int (*next)(snd_pcm_t **,const char *,snd_pcm_stream_t,int) = dlsym(RTLD_NEXT,"snd_pcm_open");
    const char *gate = getenv("MOCK_OPEN_GATE");
    if (gate && !strcmp(name,"capture")) {
        for (int i = 0; i < 300 && access(gate,F_OK); ++i) usleep(10000);
    }
    return next(p,name,stream,mode);
}
''')
        cls.delayed = cls.build / 'delayed.so'
        subprocess.run(['cc', '-shared', '-fPIC', str(delayed), '-ldl',
                        '-o', str(cls.delayed)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.build_tmp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='hk-bt-pcm-')
        self.root = Path(self.tmp.name)
        self.run = self.root / 'runtime/run'
        self.control = self.run.parent / 'bt-pcm-control'
        self.opening = self.run.parent / 'bt-pcm-opening'
        (self.run / 'safe').mkdir(parents=True)
        (self.run / 'safe/armed').touch()
        self.capture = self.root / 'samples.raw'
        self.config = self.root / 'alsa.conf'
        self.config.write_text(f'''</usr/share/alsa/alsa.conf>
pcm_type.hkbtpark {{ lib "{self.plugin}" }}
pcm.capture {{ type file slave.pcm "null" file "{self.capture}" format "raw" }}
pcm.test_bt {{ type hkbtpark slave "capture" }}
''')
        self.env = os.environ | {'ALSA_CONFIG_PATH': str(self.config), 'HK_BT_PCM_RUN': str(self.run)}
        self.env.pop('LD_PRELOAD', None)
        self.children = []

    def tearDown(self):
        for proc in reversed(self.children):
            if proc.poll() is None:
                proc.stdin.write('q\n'); proc.stdin.flush()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
            for file in (proc.stdin, proc.stdout, proc.stderr):
                file.close()
        self.tmp.cleanup()

    def launch(self):
        proc = subprocess.Popen([str(self.client)], env=self.env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
        self.children.append(proc)
        self.assertEqual(self.line(proc), 'ready')
        return proc

    def line(self, proc):
        self.assertTrue(select.select([proc.stdout], [], [], 3)[0], 'ALSA client stalled')
        line = proc.stdout.readline().strip()
        if not line and proc.poll() is not None:
            self.fail(proc.stderr.read())
        return line

    def write(self, proc, sample=1111):
        proc.stdin.write(f'{sample}\n'); proc.stdin.flush()
        self.assertEqual(self.line(proc), 'wrote 256')

    def holds_capture(self, proc):
        for fd in (Path('/proc') / str(proc.pid) / 'fd').iterdir():
            try:
                if fd.resolve() == self.capture: return True
            except OSError:
                pass
        return False

    def park(self, token='round1', marker=True):
        if marker: (self.run / 'bt-pcm-park').write_text(token + '\n')
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(2)
            client.connect(str(self.control))
            client.sendall(('park:' + token).encode() + b'\0')
            client.shutdown(socket.SHUT_WR)
            return client.recv(100)

    def test_retained_handle_parks_and_reopens_only_on_new_allowed_samples(self):
        proc = self.launch()
        self.assertFalse(self.holds_capture(proc))
        self.write(proc)
        self.assertTrue(self.holds_capture(proc))
        (self.run / 'ap2-claim').write_text('1\n')
        self.assertEqual(self.park(), b'parked\n')
        self.assertFalse(self.holds_capture(proc))
        self.write(proc, 2222)
        self.assertFalse(self.holds_capture(proc), 'AirPlay claim must prevent Bluetooth reopening')
        (self.run / 'ap2-claim').unlink()
        self.write(proc, 3333)
        self.assertTrue(self.holds_capture(proc))
        self.assertIsNone(proc.poll(), 'the original virtual handle must survive the handoff')
        proc.stdin.write('q\n'); proc.stdin.flush(); proc.wait(timeout=2)
        samples = self.capture.read_bytes()
        self.assertEqual(samples, (3333).to_bytes(2, 'little', signed=True) * 512,
                         'parked samples must not be replayed after takeover ends')
        self.assertFalse(self.control.exists())

    def test_park_requires_current_marker_and_revoked_lease_and_owner(self):
        proc = self.launch(); self.write(proc)
        self.assertEqual(self.park(marker=False), b'refused\n')
        for leaf, value in [('safe/lease', 'bluetooth 42 1 token 0 active\n'),
                            ('safe/play.started', '0\n'), ('bt-owner.pid', '42\n')]:
            path = self.run / leaf; path.write_text(value)
            self.assertEqual(self.park(), b'refused\n')
            self.assertTrue(self.holds_capture(proc))
            path.unlink()
        self.assertEqual(self.park(token='stale', marker=False), b'refused\n')
        self.assertEqual(self.park(), b'parked\n')

    def test_fault_hold_and_other_source_lease_prevent_initial_slave_open(self):
        proc = self.launch()
        for path, text in [(self.run / 'safe/tripped', 'manual mute\n'),
                           (self.root / 'runtime-hold-mute', ''),
                           (self.run / 'safe/lease', 'voice 42 1 t 0 active\n'),
                           (self.run / 'voice-owns', str(int(float(Path('/proc/uptime').read_text().split()[0])))+'\n')]:
            path.write_text(text)
            self.write(proc)
            self.assertFalse(self.holds_capture(proc))
            path.unlink()
        self.write(proc)
        self.assertTrue(self.holds_capture(proc))

    def test_poll_wait_is_bounded_and_reports_playback_readiness(self):
        proc = self.launch()
        for _ in range(2):
            proc.stdin.write('p\n'); proc.stdin.flush()
            self.assertEqual(self.line(proc), 'poll 1')

    def test_duplicate_open_preserves_the_live_socket_and_handle(self):
        proc = self.launch(); self.write(proc)
        inode = self.control.stat().st_ino
        result = subprocess.run([str(self.client)], env=self.env, input='q\n',
                                text=True, capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.control.stat().st_ino, inode)
        self.assertEqual(self.park(), b'parked\n')

    def test_unexpected_socket_path_is_not_removed(self):
        target = self.control
        target.write_text('keep this file\n')
        result = subprocess.run([str(self.client)], env=self.env, input='q\n',
                                text=True, capture_output=True, timeout=3)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), 'keep this file\n')

    def test_stale_socket_recovers_and_slow_client_cannot_hold_shutdown(self):
        with socket.socket(socket.AF_UNIX) as old:
            old.bind(str(self.control))
        proc = self.launch()
        slow = socket.socket(socket.AF_UNIX)
        slow.connect(str(self.control))
        before = time.monotonic()
        proc.stdin.write('q\n'); proc.stdin.flush()
        proc.wait(timeout=1)
        slow.close()
        self.assertLess(time.monotonic() - before, .7)

    def test_runtime_restart_preserves_control_and_stale_voice_does_not_block(self):
        proc = self.launch(); self.write(proc)
        inode = self.control.stat().st_ino
        shutil.rmtree(self.run)
        (self.run / 'safe').mkdir(parents=True)
        (self.run / 'safe/armed').touch()
        (self.run / 'ap2-claim').touch()
        self.assertEqual(self.park(), b'parked\n')
        self.assertFalse(self.holds_capture(proc))
        self.assertEqual(self.control.stat().st_ino, inode)
        (self.run / 'ap2-claim').unlink()
        (self.run / 'voice-owns').write_text('0\n')
        self.write(proc)
        self.assertTrue(self.holds_capture(proc), 'expired voice marker must match shell recovery policy')

    def test_new_claim_waits_out_an_inflight_bluetooth_open(self):
        gate = self.root / 'allow-open'
        self.env.update({'LD_PRELOAD': str(self.delayed), 'MOCK_OPEN_GATE': str(gate)})
        proc = self.launch()
        proc.stdin.write('1111\n'); proc.stdin.flush()
        deadline = time.monotonic() + 2
        while not self.opening.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.opening.exists(), 'opening must be published before touching the slave')
        (self.run / 'ap2-claim').touch()
        gate.touch()
        self.assertEqual(self.line(proc), 'wrote 256')
        self.assertFalse(self.opening.exists())
        self.assertFalse(self.holds_capture(proc), 'late open must back out after a new claim')
        self.assertEqual(self.park(), b'parked\n')


if __name__ == '__main__':
    unittest.main()
