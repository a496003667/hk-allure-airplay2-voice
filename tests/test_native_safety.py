"""Run real native C/IPC behavior; never contact a speaker or cloud service."""
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


class IpcTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='hk-ipc-')
        cls.binary = Path(cls.tmp.name) / 'request'
        subprocess.run(['cc', '-std=c99', '-D_POSIX_C_SOURCE=200809L', '-O2',
                        str(ROOT / 'tools/hk-unix-request-raw.c'), '-o', str(cls.binary)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def peer(self, behavior, command='status', timeout='180'):
        with tempfile.TemporaryDirectory(prefix='hk-sock-') as directory:
            path = str(Path(directory) / 'socket')
            server = socket.socket(socket.AF_UNIX)
            server.bind(path)
            server.listen(1)
            request = []
            def serve():
                try:
                    with server.accept()[0] as client:
                        data = b''
                        while chunk := client.recv(8192):
                            data += chunk
                        request.append(data)
                        behavior(client)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    server.close()
            worker = threading.Thread(target=serve, daemon=True)
            worker.start()
            before = time.monotonic()
            result = subprocess.run([str(self.binary), path, command, timeout],
                                    capture_output=True, timeout=3)
            elapsed = time.monotonic() - before
            worker.join(1)
            return result, elapsed, request

    def test_reply_is_complete_and_request_has_nul_and_eof(self):
        def response(client):
            client.sendall(b'{"vol":')
            time.sleep(.02)
            client.sendall(b'"12"}')
        result, _, request = self.peer(response)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'{"vol":"12"}')
        self.assertEqual(request, [b'status\0'])

    def test_peer_without_eof_times_out_and_discards_partial_state(self):
        def response(client):
            client.sendall(b'{"vol":"100"}')
            time.sleep(.5)
        result, elapsed, _ = self.peer(response)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.stdout, b'')
        self.assertLess(elapsed, .45)

    def test_slow_drip_does_not_extend_total_deadline(self):
        def response(client):
            for _ in range(20):
                client.sendall(b'x')
                time.sleep(.04)
        result, elapsed, _ = self.peer(response)
        self.assertEqual(result.returncode, 124)
        self.assertLess(elapsed, .45)
        self.assertEqual(result.stdout, b'')

    def test_oversized_reply_is_not_success(self):
        result, _, _ = self.peer(lambda c: c.sendall(b'x' * 70000), timeout='1000')
        self.assertEqual(result.returncode, 8)
        self.assertEqual(result.stdout, b'')

    def test_large_request_handles_partial_writes(self):
        result, _, request = self.peer(lambda c: c.sendall(b'ok'), command='x' * 60000,
                                       timeout='1000')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(request, [b'x' * 60000 + b'\0'])

    def test_invalid_timeout_and_missing_peer_fail(self):
        for timeout in ('0', '-1', 'no', '60001', '99999999999999999999999'):
            result = subprocess.run([str(self.binary), '/absent', 'status', timeout],
                                    capture_output=True, timeout=2)
            self.assertEqual(result.returncode, 2, timeout)
        result = subprocess.run([str(self.binary), '/absent', 'status', '100'],
                                capture_output=True, timeout=2)
        self.assertNotEqual(result.returncode, 0)


class ForkSafetyTest(unittest.TestCase):
    def test_failed_asr_fork_falls_back_without_signalling_or_waiting(self):
        source = (ROOT / 'voice/hk-voice.c').read_text()
        function = 'static char *asr_best(' + source.split('static char *asr_best(', 1)[1].split(
            '\n/* ---------- LLM', 1)[0]
        c = r'''
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <signal.h>
#include <sys/wait.h>
#include <unistd.h>
typedef struct { char *p; size_t n, cap; } buf_t;
static const char *conf_get(const char *k,const char *d) {(void)d;return !strcmp(k,"SV_WAIT")?"4":"key";}
static char *baidu_asr(const int16_t *p,size_t n) {(void)p;(void)n;return strdup("fallback");}
static char *sv_asr(const int16_t *p,size_t n,long t) {(void)p;(void)n;(void)t;abort();}
static long mono_ms(void) {return 0;}
static void buf_add(buf_t *b,const void *p,size_t n) {(void)b;(void)p;(void)n;}
static int mock_pipe(int p[2]) {p[0]=900;p[1]=901;return 0;}
static pid_t mock_fork(void) {errno=EAGAIN;return -1;}
static int mock_kill(pid_t p,int s) {(void)p;(void)s;abort();}
static pid_t mock_wait(pid_t p,int *s,int o) {(void)p;(void)s;(void)o;abort();}
static int mock_close(int fd) {(void)fd;return 0;}
#define pipe mock_pipe
#define fork mock_fork
#define kill mock_kill
#define waitpid mock_wait
#define close mock_close
'''
        c += function + '\nint main(void) {char *s=asr_best(NULL,0);int r=!s||strcmp(s,"fallback");free(s);return r;}\n'
        with tempfile.TemporaryDirectory() as directory:
            src, binary = Path(directory) / 'test.c', Path(directory) / 'test'
            src.write_text(c)
            subprocess.run(['cc', str(src), '-o', str(binary)], check=True, capture_output=True)
            result = subprocess.run([str(binary)], capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
