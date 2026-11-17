"""Real ALSA PCM/Unix control tests; the slave writes a file over null audio."""
import os
from pathlib import Path
import re
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
#include <time.h>
#include <string.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>
int main(void) {
    snd_pcm_t *pcm;
    int use_mmap = getenv("HK_TEST_MMAP") != NULL;
    int rc = snd_pcm_open(&pcm, "test_bt", SND_PCM_STREAM_PLAYBACK, SND_PCM_NONBLOCK);
    if (rc < 0) { fprintf(stderr, "open: %s\n", snd_strerror(rc)); return 2; }
    rc = snd_pcm_set_params(pcm, SND_PCM_FORMAT_S16_LE,
                            use_mmap ? SND_PCM_ACCESS_MMAP_INTERLEAVED : SND_PCM_ACCESS_RW_INTERLEAVED,
                            2, getenv("HK_TEST_RATE") ? (unsigned)atoi(getenv("HK_TEST_RATE")) : 48000, 1, 500000);
    if (rc < 0) { fprintf(stderr, "params: %s\n", snd_strerror(rc)); return 3; }
    puts("ready"); fflush(stdout);
    char line[40];
    void *mapping = MAP_FAILED;
    size_t mapping_bytes = 0;
    int mapping_fd = -1;
    while (fgets(line, sizeof(line), stdin)) {
        if (line[0] == 'q') break;
        if (line[0] == 'r') {
            printf("prepared %d\n", snd_pcm_prepare(pcm)); fflush(stdout);
            continue;
        }
        if (line[0] == 'm') {
            snd_pcm_uframes_t buffer = 0, period = 0;
            if (snd_pcm_get_params(pcm, &buffer, &period) < 0 || mapping != MAP_FAILED) return 7;
            mapping_bytes = buffer * 4;
            mapping_fd = open(getenv("HK_BT_PCM_DEVICE"), O_RDWR);
            if (mapping_fd < 0 || ftruncate(mapping_fd, (off_t)mapping_bytes)) return 7;
            mapping = mmap(NULL, mapping_bytes, PROT_READ | PROT_WRITE, MAP_SHARED, mapping_fd, 0);
            if (mapping == MAP_FAILED) return 7;
            short *area = mapping;
            for (unsigned i = 0; i < buffer; ++i) { area[2*i] = 1111; area[2*i+1] = -2222; }
            printf("mapped %lu\n", (unsigned long)buffer); fflush(stdout);
            continue;
        }
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
        char *size = strchr(line, ' ');
        unsigned frames = size ? (unsigned)atoi(size + 1) : 256;
        if (!frames || frames > 256) return 4;
        long n = use_mmap ? snd_pcm_mmap_writei(pcm, samples, frames) : snd_pcm_writei(pcm, samples, frames);
        if (getenv("HK_TEST_RETRY")) {
            long total = n > 0 ? n : 0;
            for (int tries = 0; total < frames && tries < 200 && (n >= 0 || n == -EAGAIN); ++tries) {
                if (n <= 0) {
                    snd_pcm_wait(pcm, 10);
                    struct timespec retry = {0, 1000000};
                    nanosleep(&retry, NULL);
                }
                n = snd_pcm_writei(pcm, samples + total * 2, frames - total);
                if (n > 0) total += n;
            }
            n = total;
        }
        printf("wrote %ld\n", n); fflush(stdout);
    }
    snd_pcm_close(pcm);
    if (mapping != MAP_FAILED) munmap(mapping, mapping_bytes);
    if (mapping_fd >= 0) close(mapping_fd);
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
        cls.clock = cls.build / 'libasound_module_pcm_hkclock.so'
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-DPIC', '-shared', '-fPIC',
                        str(REPO / 'tests/alsa-clock-sink.c'), '-lasound', '-o', str(cls.clock)],
                       check=True, capture_output=True)
        faults = cls.build / 'write-faults.c'
        faults.write_text(r'''
#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
static unsigned calls;
static __thread int fail_clock;
int clock_gettime(clockid_t id, struct timespec *stamp) {
    int (*next)(clockid_t, struct timespec *) = dlsym(RTLD_NEXT, "clock_gettime");
    const char *activity_gate = getenv("HK_TEST_ACTIVITY_CLOCK_FAIL");
    if (id == CLOCK_BOOTTIME && activity_gate && access(activity_gate, F_OK) == 0) {
        errno = EIO; return -1;
    }
    if (fail_clock && id == CLOCK_MONOTONIC) {
        fail_clock = 0; errno = EIO; return -1;
    }
    return next(id, stamp);
}
snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *pcm, const void *data, snd_pcm_uframes_t frames) {
    snd_pcm_sframes_t (*next)(snd_pcm_t *, const void *, snd_pcm_uframes_t) =
        dlsym(RTLD_NEXT, "snd_pcm_writei");
    if (strcmp(snd_pcm_name(pcm), "capture")) return next(pcm, data, frames);
    const char *fault = getenv("HK_TEST_SINK_FAULT");
    if (!fault || access(fault, F_OK)) return next(pcm, data, frames);
    char kind[32] = {0};
    int fd = open(fault, O_RDONLY);
    if (fd >= 0) { (void)read(fd, kind, sizeof(kind) - 1); close(fd); }
    const char *seen = getenv("HK_TEST_SINK_SEEN");
    if (seen) { fd = open(seen, O_WRONLY | O_CREAT, 0600); if (fd >= 0) close(fd); }
    if (!strncmp(kind, "xrun", 4)) return -EPIPE;
    if (!strncmp(kind, "stall", 5)) return -EAGAIN;
    ++calls;
    if (!strncmp(kind, "again", 5) && calls % 7 == 0) return -EAGAIN;
    if (frames > 127) frames = 127;
    snd_pcm_sframes_t n = next(pcm, data, frames);
    if (!strncmp(kind, "clock", 5)) fail_clock = 1;
    return n;
}
int snd_pcm_recover(snd_pcm_t *pcm, int error, int silent) {
    int (*next)(snd_pcm_t *, int, int) = dlsym(RTLD_NEXT, "snd_pcm_recover");
    const char *path = getenv("HK_TEST_RECOVER_LOG");
    if (path) {
        int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
        if (fd >= 0) { (void)write(fd, "recover\n", 8); close(fd); }
    }
    return next(pcm, error, silent);
}
''')
        cls.write_faults = cls.build / 'write-faults.so'
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', '-shared', '-fPIC',
                        str(faults), '-ldl', '-o', str(cls.write_faults)],
                       check=True, capture_output=True)
        packets = cls.build / 'packets.c'
        packets.write_text(r'''
#include <alsa/asoundlib.h>
#include <stdio.h>
#include <stdlib.h>
int main(int argc, char **argv) {
    if (argc != 2) return 1;
    unsigned rate = (unsigned)atoi(argv[1]);
    snd_pcm_t *pcm;
    int rc = snd_pcm_open(&pcm, "test_bt", SND_PCM_STREAM_PLAYBACK, SND_PCM_NONBLOCK);
    if (rc < 0) return 2;
    rc = snd_pcm_set_params(pcm, SND_PCM_FORMAT_S16_LE, SND_PCM_ACCESS_RW_INTERLEAVED,
                            2, rate, 1, 1000000);
    if (rc < 0) { snd_pcm_close(pcm); return 3; }
    short samples[12000];
    for (unsigned block = 0; block < 30; ++block) {
        for (unsigned i = 0; i < 6000; ++i) {
            samples[i*2] = (short)(((block*6000+i)*109+17)%20001-10000);
            samples[i*2+1] = (short)(((block*6000+i)*73+311)%18001-9000);
        }
        unsigned done = 0;
        for (unsigned calls = 0; done < 6000 && calls < 1000; ++calls) {
            long n = snd_pcm_writei(pcm, samples + done*2, 6000-done);
            if (n == -EAGAIN || !n) { snd_pcm_wait(pcm, 10); continue; }
            if (n < 0 || (unsigned)n > 6000-done) { snd_pcm_close(pcm); return 4; }
            done += (unsigned)n;
        }
        if (done != 6000) { snd_pcm_close(pcm); return 5; }
    }
    return snd_pcm_close(pcm) < 0 ? 6 : 0;
}
''')
        cls.packets = cls.build / 'packets'
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', str(packets),
                        '-lasound', '-o', str(cls.packets)], check=True, capture_output=True)

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
        self.env = os.environ | {'ALSA_CONFIG_PATH': str(self.config), 'HK_BT_PCM_RUN': str(self.run),
                                 'HK_BT_PCM_DEVICE': str(self.capture)}
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

    def write(self, proc, sample=1111, frames=256):
        proc.stdin.write(f'{sample} {frames}\n'); proc.stdin.flush()
        self.assertEqual(self.line(proc), f'wrote {frames}')

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

    def activity(self):
        with socket.socket(socket.AF_UNIX) as client:
            client.settimeout(2)
            client.connect(str(self.control))
            client.sendall(b'activity\0')
            client.shutdown(socket.SHUT_WR)
            return client.recv(192).decode().strip().split()

    def test_activity_counts_discarded_phone_frames_without_recording_audio(self):
        proc = self.launch()
        initial = self.activity()
        self.assertEqual(len(initial), 5)
        self.assertEqual(initial[0], 'activity')
        self.assertEqual(int(initial[1]), proc.pid)
        self.assertGreater(int(initial[2]), 0)
        self.assertEqual(int(initial[3]), 0)
        self.write(proc, 1111)
        opened = self.activity()
        self.assertEqual(int(opened[3]), 256)
        self.assertGreater(int(opened[4]), 0)
        (self.run / 'ap2-claim').write_text('1\n')
        self.assertEqual(self.park(), b'parked\n')
        self.write(proc, 2222)
        discarded = self.activity()
        self.assertEqual(discarded[1:3], opened[1:3])
        self.assertEqual(int(discarded[3]), 512)
        self.assertFalse(self.holds_capture(proc))
        self.assertEqual(len(discarded), 5)
        self.assertTrue(all(part.isdecimal() for part in discarded[1:]))

    def test_activity_survives_prepare_and_distinguishes_a_new_pcm_instance(self):
        proc = self.launch(); self.write(proc)
        before = self.activity()
        proc.stdin.write('r\n'); proc.stdin.flush()
        self.assertEqual(self.line(proc), 'prepared 0')
        self.assertEqual(self.activity()[3], before[3])
        self.write(proc)
        self.assertEqual(int(self.activity()[3]), 512)
        proc.stdin.write('q\n'); proc.stdin.flush(); proc.wait(timeout=2)
        new = self.launch()
        after = self.activity()
        self.assertNotEqual(before[2], after[2])
        self.assertEqual(int(after[1]), new.pid)
        self.assertEqual(after[3], '0')

    def test_activity_survives_disposable_runtime_directory_rebuild(self):
        proc = self.launch(); self.write(proc)
        before = self.activity()
        shutil.rmtree(self.run)
        (self.run / 'safe').mkdir(parents=True)
        (self.run / 'safe/armed').touch()
        after = self.activity()
        self.assertEqual(after, before)
        self.write(proc)
        self.assertEqual(int(self.activity()[3]), 512)

    def test_activity_clock_failure_does_not_reuse_an_old_timestamp(self):
        self.enable_write_faults()
        gate = self.root / 'activity-clock-fail'
        self.env['HK_TEST_ACTIVITY_CLOCK_FAIL'] = str(gate)
        proc = self.launch(); self.write(proc)
        before = self.activity()
        self.assertGreater(int(before[4]), 0)
        gate.touch()
        self.write(proc, 2222)
        after = self.activity()
        self.assertEqual(int(after[3]), 512)
        self.assertEqual(after[4], '0')

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

    def test_mmap_client_parks_and_reopens_without_replaying_samples(self):
        self.env['HK_TEST_MMAP'] = '1'
        self.test_retained_handle_parks_and_reopens_only_on_new_allowed_samples()

    def test_alsa_software_layer_can_negotiate_mmap_slave(self):
        text = self.config.read_text().replace('pcm.test_bt { type hkbtpark', 'pcm.virtual_bt { type hkbtpark')
        text += '''pcm.test_bt {
 type route
 slave.pcm "virtual_bt"
 slave.channels 2
 ttable.0.0 1
 ttable.1.1 1
}\n'''
        self.config.write_text(text)
        self.test_retained_handle_parks_and_reopens_only_on_new_allowed_samples()

    def use_shipped_adapter(self):
        shipped = (REPO / 'airplay2/hk-bt-alsa.conf').read_text()
        adapter = re.search(r'pcm\.hk_bt_dsp_client\s*\{[^}]+\}', shipped)
        parking = re.search(r'pcm\.hk_bt_parking\s*\{[^}]+\}', shipped)
        self.assertIsNotNone(adapter)
        self.assertIsNotNone(parking)
        text = re.sub(r'pcm\.test_bt\s*\{[^}]+\}', '''pcm.test_bt {
 type route
 slave.pcm "hk_bt_dsp_client"
 slave.channels 2
 ttable.0.0 1
 ttable.1.1 1
}''', self.config.read_text())
        self.config.write_text(text + adapter.group() + '\n' +
                               parking.group().replace('slave "hk_bt_dsp"', 'slave "capture"') + '\n')
        self.env['HK_TEST_RATE'] = '44100'
        self.env['HK_TEST_RETRY'] = '1'

    def test_shipped_dsp_adapter_resamples_phone_input_before_parking(self):
        # Use the actual shipped adapter with a file/null sink in place of
        # proprietary Harman DSP. Count output frames to prove resampling;
        # a permissive null open alone cannot catch the target-only failure.
        self.use_shipped_adapter()
        proc = self.launch()
        blocks = 1000
        for _ in range(blocks):
            self.write(proc)
        self.assertTrue(self.holds_capture(proc))
        proc.stdin.write('q\n'); proc.stdin.flush(); proc.wait(timeout=2)
        output_frames = self.capture.stat().st_size // 4
        input_frames = blocks * 256
        self.assertGreater(output_frames, input_frames * 1.05)
        self.assertLess(output_frames, input_frames * 1.10)
        log = (self.run / 'bt-pcm.log').read_text()
        self.assertIn('virtual params: rate=48000 ', log,
                      'the retained PCM must count frames at the DSP hardware rate')

    def test_shipped_rate_adapter_streams_across_paced_hardware_buffers(self):
        self.use_shipped_adapter()
        text = self.config.read_text().replace('slave.pcm "null"', 'slave.pcm "paced"')
        self.config.write_text(text + f'\npcm_type.hkclock {{ lib "{self.clock}" }}\npcm.paced {{ type hkclock }}\n')
        proc = self.launch()
        # Cross more than two 0.5-second buffers at actual playback speed,
        # not just the instant consumption provided by an ALSA null sink.
        for _ in range(220):
            self.write(proc)
        self.assertTrue(self.holds_capture(proc))
        (self.run / 'ap2-claim').touch()
        self.assertEqual(self.park(), b'parked\n')
        self.assertFalse(self.holds_capture(proc))
        for _ in range(220):
            self.write(proc, 2222)
        self.assertFalse(self.holds_capture(proc))
        (self.run / 'ap2-claim').unlink()
        for _ in range(220):
            self.write(proc, 3333)
        self.assertTrue(self.holds_capture(proc))

    def test_parking_exactly_full_ring_still_allows_discard_and_reopen(self):
        hold = self.root / 'freeze-clock'
        hold.touch()
        self.env.update({'HK_TEST_CLOCK_HOLD': str(hold), 'HK_TEST_RETRY': '1'})
        self.config.write_text(self.config.read_text().replace('slave.pcm "null"', 'slave.pcm "paced"') +
                               f'\npcm_type.hkclock {{ lib "{self.clock}" }}\npcm.paced {{ type hkclock }}\n')
        proc = self.launch()
        for _ in range(93):
            self.write(proc)
        self.write(proc, frames=192)  # exactly the 24,000-frame ring
        (self.run / 'ap2-claim').touch()
        self.assertEqual(self.park(), b'parked\n')
        self.assertFalse(self.holds_capture(proc))
        for _ in range(120):
            self.write(proc, 2222)
        self.assertFalse(self.holds_capture(proc))
        hold.unlink()
        (self.run / 'ap2-claim').unlink()
        for _ in range(220):
            self.write(proc, 3333)
        self.assertTrue(self.holds_capture(proc))

    def test_holder_record_tracks_real_descriptor_and_park_reopen(self):
        proc = self.launch(); self.write(proc)
        owner = self.run.parent / 'bt-pcm-owner'
        first = owner.read_text()
        pid, started, fd, generation = first.split()
        self.assertEqual(int(pid), proc.pid)
        fields = (Path('/proc') / pid / 'stat').read_text().rsplit(')', 1)[1].split()
        self.assertEqual(started, fields[19])
        self.assertEqual((Path('/proc') / pid / 'fd' / fd).resolve(), self.capture)
        self.assertEqual(owner.stat().st_mode & 0o777, 0o600)
        (self.run / 'ap2-claim').touch()
        self.assertEqual(self.park(), b'parked\n')
        self.assertFalse(owner.exists())
        self.write(proc, 2222)
        self.assertFalse(owner.exists())
        (self.run / 'ap2-claim').unlink()
        self.write(proc, 3333)
        self.assertNotEqual(owner.read_text(), first)
        proc.stdin.write('q\n'); proc.stdin.flush(); proc.wait(timeout=2)
        self.assertFalse(owner.exists())

    def test_missing_descriptor_identity_closes_slave_without_publishing_proof(self):
        self.env['HK_BT_PCM_DEVICE'] = str(self.root / 'missing-device')
        proc = self.launch(); self.write(proc)
        self.assertFalse(self.holds_capture(proc))
        self.assertFalse((self.run.parent / 'bt-pcm-owner').exists())
        self.assertIn('holder identity failed', (self.run / 'bt-pcm.log').read_text())

    def enable_write_faults(self, kind=None):
        fault = self.root / 'sink-fault'
        self.env.update({'LD_PRELOAD': str(self.write_faults),
                         'HK_TEST_SINK_FAULT': str(fault),
                         'HK_TEST_SINK_SEEN': str(self.root / 'sink-seen'),
                         'HK_TEST_RECOVER_LOG': str(self.root / 'recover.log')})
        if kind: fault.write_text(kind)
        return fault

    def test_partial_backend_commits_preserve_samples_through_rate_adapter(self):
        self.use_shipped_adapter()
        config = self.config.read_text()
        self.enable_write_faults()
        # Compare against direct ALSA rate->file, including a nonconstant
        # stereo pattern. A successful outer write can hide lost rate output.
        for rate in (48000, 44100):
            with self.subTest(rate=rate):
                direct = config.replace('slave.pcm "hk_bt_parking"', 'slave.pcm "capture"')
                self.config.write_text(direct)
                subprocess.run([str(self.packets), str(rate)], env=self.env,
                               check=True, capture_output=True, timeout=5)
                reference = self.capture.read_bytes()
                self.assertGreaterEqual(len(reference), 180000 * 4)
                self.config.write_text(config)
                for kind in ('partial', 'again'):
                    (self.root / 'sink-fault').write_text(kind)
                    subprocess.run([str(self.packets), str(rate)], env=self.env,
                                   check=True, capture_output=True, timeout=5)
                    self.assertEqual(self.capture.read_bytes(), reference, kind)
                    self.assertFalse((self.run / 'bt-wait-pcm').exists())
                (self.root / 'sink-fault').unlink()

    def test_active_xrun_requests_muted_resync_without_resetting_clock(self):
        fault = self.enable_write_faults()
        proc = self.launch(); self.write(proc)
        lease = self.run / 'safe/lease'
        owner = self.run / 'bt-owner.pid'
        lease.write_text('bluetooth 42 1 token 0 active\n')
        owner.write_text('42\n')
        fault.write_text('xrun')
        self.write(proc, 2222)
        self.assertEqual((self.run / 'bt-wait-pcm').read_text(), 'backend-clock-stopped\n')
        self.assertFalse((self.root / 'recover.log').exists(), 'live output must mute before recovery')
        self.assertTrue(self.holds_capture(proc), 'active slave cannot close before output is muted')
        lease.unlink(); owner.unlink()
        (self.run / 'ap2-claim').touch()
        self.assertEqual(self.park(), b'parked\n')

    def test_slave_backpressure_is_bounded_and_requests_muted_resync(self):
        self.enable_write_faults('stall')
        proc = self.launch()
        before = time.monotonic()
        self.write(proc)
        self.assertLess(time.monotonic() - before, 1.5)
        self.assertEqual((self.run / 'bt-wait-pcm').read_text(), 'backend-write-stalled\n')

    def test_new_source_cancels_pending_packet_and_allows_prompt_park(self):
        self.enable_write_faults('stall')
        proc = self.launch()
        proc.stdin.write('1111\n'); proc.stdin.flush()
        deadline = time.monotonic() + 2
        while not (self.root / 'sink-seen').exists() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue((self.root / 'sink-seen').exists())
        before = time.monotonic()
        (self.run / 'ap2-claim').touch()
        self.assertEqual(self.line(proc), 'wrote 256')
        self.assertEqual(self.park(), b'parked\n')
        self.assertLess(time.monotonic() - before, .5)
        self.assertFalse(self.holds_capture(proc))

    def test_failed_clock_read_requests_muted_resync(self):
        self.enable_write_faults('clock')
        proc = self.launch(); self.write(proc)
        self.assertEqual((self.run / 'bt-wait-pcm').read_text(), 'backend-clock-read-failed\n')

    def test_opt_in_backend_levels_preserve_samples_and_do_not_store_audio(self):
        (self.run / 'bt-pcm-diagnostics').touch()
        proc = self.launch(); self.write(proc, 1111)
        proc.stdin.write('q\n'); proc.stdin.flush(); proc.wait(timeout=2)
        self.assertEqual(self.capture.read_bytes(), (1111).to_bytes(2, 'little', signed=True) * 512)
        log = (self.run / 'bt-pcm.log').read_text()
        self.assertIn('signal stage=post-master frames=256 nonzero=512 peak=1111,1111\n', log)

    def test_output_levels_require_opt_in_and_read_only_the_mapped_audio_buffer(self):
        proc = self.launch(); self.write(proc)
        def levels():
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(2)
                client.connect(str(self.control))
                client.sendall(b'levels\0')
                return client.recv(192).decode()
        self.assertEqual(levels(), 'refused\n')
        (self.run / 'bt-pcm-diagnostics').touch()
        self.assertEqual(levels(), 'refused\n', 'a descriptor without a PCM data mapping is insufficient')
        proc.stdin.write('m\n'); proc.stdin.flush()
        mapped = int(self.line(proc).split()[1])
        before = self.capture.read_bytes()
        self.assertEqual(levels(), f'signal stage=pcm-output frames={mapped} nonzero={mapped},{mapped} peak=1111,2222\n')
        self.assertEqual(self.capture.read_bytes(), before, 'output observation must not modify samples')


if __name__ == '__main__':
    unittest.main()
