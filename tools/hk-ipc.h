/* Stock Unix socket protocol with one monotonic total deadline. Shared by
 * the freestanding ARM tool and libc voice client; no partial state on error. */
#ifndef HK_IPC_H
#define HK_IPC_H
#ifdef HK_IPC_RAW_ARM
struct sockaddr_un { unsigned short sun_family; char sun_path[108]; };
struct pollfd { int fd; short events, revents; };
struct timespec { long tv_sec, tv_nsec; };
#define AF_UNIX 1
#define SOCK_STREAM 1
#define POLLIN 1
#define POLLOUT 4
#define EINTR 4
#define EAGAIN 11
#define EINPROGRESS 115
static long hk_sys(long n, long a, long b, long c, long d, long e, long f) {
  register long r7 __asm__("r7") = n;
  register long r0 __asm__("r0") = a;
  register long r1 __asm__("r1") = b;
  register long r2 __asm__("r2") = c;
  register long r3 __asm__("r3") = d;
  register long r4 __asm__("r4") = e;
  register long r5 __asm__("r5") = f;
  __asm__ volatile("svc 0" : "+r"(r0) : "r"(r7), "r"(r1), "r"(r2),
                   "r"(r3), "r"(r4), "r"(r5) : "memory", "cc");
  return r0;
}
static int hk_socket(void) { return (int)hk_sys(281, AF_UNIX, SOCK_STREAM, 0, 0, 0, 0); }
static void hk_close(int fd) { hk_sys(6, fd, 0, 0, 0, 0, 0); }
static int hk_nonblock(int fd) { return (int)hk_sys(55, fd, 4, 2048, 0, 0, 0); }
static int hk_connect(int fd, struct sockaddr_un *a) {
  return (int)hk_sys(283, fd, (long)a, sizeof *a, 0, 0, 0);
}
static int hk_socket_error(int fd) {
  int error = 0, len = sizeof error;
  long r = hk_sys(295, fd, 1, 4, (long)&error, (long)&len, 0);
  return r < 0 ? (int)r : -error;
}
static long hk_send(int fd, const char *b, unsigned long n) {
  return hk_sys(290, fd, (long)b, n, 0x4000 /* MSG_NOSIGNAL */, 0, 0);
}
static long hk_recv(int fd, char *b, unsigned long n) { return hk_sys(3, fd, (long)b, n, 0, 0, 0); }
static int hk_shutdown(int fd) { return (int)hk_sys(293, fd, 1, 0, 0, 0, 0); }
static int hk_poll(struct pollfd *p, int ms) { return (int)hk_sys(168, (long)p, 1, ms, 0, 0, 0); }
static int hk_clock(struct timespec *t) { return (int)hk_sys(263, 1, (long)t, 0, 0, 0, 0); }
#else
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>
static int hk_socket(void) {
#ifdef __linux__
  /* Linux 3.10 supports atomic nonblocking sockets. Avoid modern glibc's
   * fcntl@2.28 wrapper, unavailable in the speaker's glibc 2.19. */
  int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
#else
  int fd = socket(AF_UNIX, SOCK_STREAM, 0);
#endif
#ifdef SO_NOSIGPIPE
  if (fd >= 0) { int one = 1; setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one); }
#endif
  return fd < 0 ? -errno : fd;
}
static void hk_close(int fd) { close(fd); }
static int hk_nonblock(int fd) {
#ifdef __linux__
  (void)fd;
  return 0;
#else
  int flags = fcntl(fd, F_GETFL, 0);
  return flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) < 0 ? -errno : 0;
#endif
}
static int hk_connect(int fd, struct sockaddr_un *a) {
  return connect(fd, (struct sockaddr *)a, sizeof *a) < 0 ? -errno : 0;
}
static int hk_socket_error(int fd) {
  int error = 0; socklen_t len = sizeof error;
  return getsockopt(fd, SOL_SOCKET, SO_ERROR, &error, &len) < 0 ? -errno : -error;
}
static long hk_send(int fd, const char *b, unsigned long n) {
#ifdef MSG_NOSIGNAL
  long r = send(fd, b, n, MSG_NOSIGNAL);
#else
  long r = send(fd, b, n, 0);
#endif
  return r < 0 ? -errno : r;
}
static long hk_recv(int fd, char *b, unsigned long n) {
  long r = read(fd, b, n); return r < 0 ? -errno : r;
}
static int hk_shutdown(int fd) { return shutdown(fd, SHUT_WR) < 0 ? -errno : 0; }
static int hk_poll(struct pollfd *p, int ms) { int r = poll(p, 1, ms); return r < 0 ? -errno : r; }
static int hk_clock(struct timespec *t) { return clock_gettime(CLOCK_MONOTONIC, t); }
#endif

enum { HK_IPC_OK = 0, HK_IPC_PATH = 3, HK_IPC_SOCKET = 4,
       HK_IPC_CONNECT = 5, HK_IPC_WRITE = 6, HK_IPC_READ = 7,
       HK_IPC_LIMIT = 8, HK_IPC_TIMEOUT = 124 };
static long long hk_ipc_ms(void) {
  struct timespec t;
  if (hk_clock(&t) < 0) return -1;
  return (long long)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
static int hk_ipc_ready(int fd, short events, long long deadline) {
  for (;;) {
    long long now = hk_ipc_ms();
    if (now < 0) return HK_IPC_READ;
    if (now >= deadline) return HK_IPC_TIMEOUT;
    struct pollfd p = {fd, events, 0};
    int r = hk_poll(&p, (int)(deadline - now));
    if (r > 0) return 0;
    if (r == 0) return HK_IPC_TIMEOUT;
    if (r != -EINTR) return HK_IPC_READ;
  }
}
static int hk_ipc_request(const char *path, const char *cmd, int timeout_ms,
                          char *response, unsigned long capacity, unsigned long *length) {
  struct sockaddr_un a;
  /* GCC can lower a struct initializer to memset even in a freestanding
   * build. Volatile stores keep the raw client independent of libc. */
  for (unsigned long i = 0; i < sizeof a; i++) ((volatile char *)&a)[i] = 0;
  unsigned long n = 0, sent = 0, used = 0;
  *length = 0;
  if (!response || !capacity || timeout_ms < 1 || timeout_ms > 60000) return HK_IPC_PATH;
  response[0] = 0;
  while (path[n]) { if (n + 1 >= sizeof a.sun_path) return HK_IPC_PATH; a.sun_path[n] = path[n]; n++; }
  if (!n) return HK_IPC_PATH;
  a.sun_family = AF_UNIX;
  long long started = hk_ipc_ms();
  if (started < 0) return HK_IPC_READ;
  long long deadline = started + timeout_ms;
  int fd = hk_socket(), rc = HK_IPC_SOCKET;
  if (fd < 0) return rc;
  if (hk_nonblock(fd) < 0) goto done;
  int r = hk_connect(fd, &a);
  rc = HK_IPC_CONNECT;
  if (r == -EINPROGRESS || r == -EAGAIN || r == -EINTR) {
    rc = hk_ipc_ready(fd, POLLOUT, deadline);
    if (rc) goto done;
    r = hk_socket_error(fd);
  }
  if (r < 0) { rc = HK_IPC_CONNECT; goto done; }
  n = 1;
  while (cmd[n - 1]) n++;
  while (sent < n) {
    rc = hk_ipc_ready(fd, POLLOUT, deadline);
    if (rc) goto done;
    long w = hk_send(fd, cmd + sent, n - sent);
    if (w == -EINTR || w == -EAGAIN) continue;
    if (w <= 0) { rc = HK_IPC_WRITE; goto done; }
    sent += (unsigned long)w;
  }
  if (hk_shutdown(fd) < 0) { rc = HK_IPC_WRITE; goto done; }
  for (;;) {
    rc = hk_ipc_ready(fd, POLLIN, deadline);
    if (rc) goto done;
    char extra;
    long got = hk_recv(fd, used + 1 < capacity ? response + used : &extra,
                       used + 1 < capacity ? capacity - used - 1 : 1);
    if (got == -EINTR || got == -EAGAIN) continue;
    if (got < 0) { rc = HK_IPC_READ; goto done; }
    if (!got) { response[used] = 0; *length = used; rc = 0; goto done; }
    if (used + 1 >= capacity) { rc = HK_IPC_LIMIT; goto done; }
    used += (unsigned long)got;
  }
done:
  hk_close(fd);
  if (rc) response[0] = 0;
  return rc;
}
#endif
