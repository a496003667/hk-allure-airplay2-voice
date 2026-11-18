/* ARM: -Os -fomit-frame-pointer -fno-builtin -fno-stack-protector
 *      -nostdlib -static -Wl,-e,_start hk-unix-request-raw.c
 * Native builds exercise the same request/deadline implementation. */
#if defined(__arm__) && defined(__linux__)
#define HK_IPC_RAW_ARM
__asm__(".text\n.global _start\n.type _start,%function\n_start:\n"
        "ldr r0,[sp]\nadd r1,sp,#4\nbl main\nmov r7,#1\nsvc 0\n");
#endif
#include "hk-ipc.h"

int main(int argc, char **argv) {
  static char response[65536];
  unsigned long length;
  int timeout = 1200;
  if (argc != 3 && argc != 4) return 2;
  if (argc == 4) {
    timeout = 0;
    for (const char *p = argv[3]; *p; p++) {
      if (*p < '0' || *p > '9' || timeout > 6000) return 2;
      timeout = timeout * 10 + *p - '0';
    }
    if (timeout < 1 || timeout > 60000) return 2;
  }
  int rc = hk_ipc_request(argv[1], argv[2], timeout, response, sizeof response, &length);
  if (rc) return rc;
  unsigned long sent = 0;
  while (sent < length) {
#ifdef HK_IPC_RAW_ARM
    long n = hk_sys(4, 1, (long)(response + sent), length - sent, 0, 0, 0);
#else
    long n = write(1, response + sent, length - sent);
    if (n < 0) n = -errno;
#endif
    if (n == -EINTR) continue;
    if (n <= 0) return HK_IPC_WRITE;
    sent += (unsigned long)n;
  }
  return 0;
}
