"""Exercise the real preload's write contract with nonblocking sink behavior."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class BluetoothWriteFidelityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hk-bt-write-build-')
        cls.root = Path(cls.tmp.name)
        source = cls.root / 'client.c'
        source.write_text(r'''
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <errno.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
static void *mock_dlsym(void *, const char *);
#define dlsym mock_dlsym
#include "''' + str(REPO / 'airplay2/hk-bt-open-redirect.c') + r'''"
#undef dlsym
static FILE *output;
static const char *behavior;
static unsigned calls;
static long sink(snd_pcm_t *pcm, const void *data, unsigned long frames) {
    (void)pcm;
    ++calls;
    if (!strcmp(behavior,"stall")) return -EAGAIN;
    if (!strcmp(behavior,"again") && calls % 7 == 0) return -EAGAIN;
    if (!strcmp(behavior,"zero") && calls % 7 == 0) return 0;
    if (strcmp(behavior,"full") && frames > 127) frames = 127;
    return fwrite(data, 4, frames, output);
}
static int open_sink(snd_pcm_t **pcm, const char *name, int stream, int mode) {
    (void)name; (void)stream; (void)mode;
    *pcm = (snd_pcm_t *)(uintptr_t)1;
    return 0;
}
static void *mock_dlsym(void *handle, const char *name) {
    (void)handle;
    if (!strcmp(name,"snd_pcm_open")) return (void *)open_sink;
    return !strcmp(name,"snd_pcm_writei") ? (void *)sink : NULL;
}
int main(int argc,char **argv) {
    if (argc != 4) return 1;
    behavior = argv[1];
    output = fopen(argv[2],"wb");
    if (!output) return 2;
    unsigned frames = 6000;
    short *data = malloc(frames * 4);
    for (unsigned i=0;i<frames;++i) {
        data[i*2] = (short)((i*109+17)%20001-10000);
        data[i*2+1] = (short)((i*73+311)%18001-9000);
    }
    bt_pcm = (snd_pcm_t *)(uintptr_t)1;
    snd_pcm_t *pcm = !strcmp(argv[3],"other") ? (snd_pcm_t *)(uintptr_t)2 : bt_pcm;
    if (!strcmp(argv[3],"reused")) {
        if (snd_pcm_open(&pcm,"another-source",0,1) != 0) return 3;
    }
    long rc = snd_pcm_writei(pcm,data,frames);
    fclose(output);
    printf("%ld %u\n",rc,calls);
    free(data);
    return 0;
}
''')
        cls.client = cls.root / 'client'
        subprocess.run(['cc', '-O2', '-Wall', '-Wextra', '-Werror', str(source), '-ldl',
                        '-o', str(cls.client)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.case = tempfile.TemporaryDirectory(prefix='hk-bt-write-')
        self.path = Path(self.case.name)
        self.inject = self.path / 'run/voice-inject'
        self.inject.mkdir(parents=True)
        self.env = os.environ | {'HK_BT_INJECT_DIR': str(self.inject),
                                 'HK_BT_PCM_RUN': str(self.path / 'run')}

    def tearDown(self):
        self.case.cleanup()

    def run_sink(self, behavior, other=False, reused=False):
        kind = 'reused' if reused else 'other' if other else 'bt'
        out = self.path / (behavior + '-' + kind + '.raw')
        p = subprocess.run([str(self.client), behavior, str(out), kind],
                           env=self.env, capture_output=True, text=True, timeout=3, check=True)
        rc, calls = map(int, p.stdout.split())
        return rc, calls, out.read_bytes()

    def test_nonblocking_short_writes_and_again_preserve_whole_packet(self):
        expected = self.run_sink('full')[2]
        self.assertEqual(len(expected), 6000 * 4)
        for behavior in ('partial', 'again', 'zero'):
            with self.subTest(behavior=behavior):
                rc, calls, data = self.run_sink(behavior)
                self.assertEqual(rc, 6000)
                self.assertGreater(calls, 1)
                self.assertEqual(data, expected)

    def test_voice_is_mixed_once_even_when_a_packet_needs_retries(self):
        (self.inject / 'active').touch()
        (self.inject / 'pcm.raw').write_bytes((100).to_bytes(2, 'little', signed=True) * 12000)
        (self.inject / 'start').touch()
        reference = self.run_sink('full')[2]
        (self.inject / 'start').touch()
        rc, calls, data = self.run_sink('again')
        self.assertEqual(rc, 6000)
        self.assertGreater(calls, 1)
        self.assertEqual(data, reference)

    def test_other_pcm_keeps_the_original_partial_write_contract(self):
        rc, calls, data = self.run_sink('partial', other=True)
        self.assertEqual((rc, calls, len(data)), (127, 1, 127 * 4))

    def test_reused_handle_does_not_apply_bluetooth_retries_to_another_source(self):
        rc, calls, data = self.run_sink('partial', reused=True)
        self.assertEqual((rc, calls, len(data)), (127, 1, 127 * 4))

    def test_permanent_backpressure_is_bounded_and_requests_resynchronization(self):
        rc, calls, data = self.run_sink('stall')
        self.assertEqual(rc, -11)
        self.assertGreater(calls, 1)
        self.assertEqual(data, b'')
        self.assertTrue((self.path / 'run/bt-wait-pcm').exists())

    def test_opt_in_level_diagnostics_preserve_audio_and_record_only_aggregates(self):
        reference = self.run_sink('full')[2]
        log = self.path / 'run/bt-voice-mix.log'
        self.assertFalse(log.exists())
        (self.path / 'run/bt-pcm-diagnostics').touch()
        self.assertEqual(self.run_sink('full')[2], reference)
        self.assertRegex(log.read_text(), r'^signal stage=player-input frames=6000 nonzero=\d+ peak=\d+,\d+\n$')
        self.assertEqual(log.stat().st_mode & 0o777, 0o600)
