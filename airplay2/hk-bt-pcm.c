/* Keep the stock Bluetooth PCM handle while returning its exclusive DSP
 * slave to another source. Only the guarded hook can request a park, after
 * fading/muting and revoking the Bluetooth lease. Incoming phone data may
 * reopen the slave once the other source has released its claim/lease.
 * This ALSA ioplug belongs only to the verified stock Bluetooth call site.
 */
#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include <alsa/pcm_external.h>
#include <errno.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/timerfd.h>
#include <sys/un.h>
#include <unistd.h>

struct bt_pcm {
    snd_pcm_ioplug_t io;
    snd_pcm_t *slave;
    char *slave_name;
    char run[192], socket_path[sizeof(((struct sockaddr_un *)0)->sun_path)];
    char opening_path[256];
    pthread_mutex_t mutex;
    pthread_t thread;
    int server, timer, threaded, stopped;
    int transfer_logged;
    dev_t socket_dev;
    ino_t socket_ino;
    uint64_t written, reported;
    uint64_t retry_after_ns;
};

static int path_for(struct bt_pcm *p, const char *leaf, char path[256])
{
    int n = snprintf(path, 256, "%s/%s", p->run, leaf);
    return n > 0 && n < 256;
}

static int exists(struct bt_pcm *p, const char *leaf)
{
    char path[256];
    return path_for(p, leaf, path) && access(path, F_OK) == 0;
}

static int read_marker(struct bt_pcm *p, const char *leaf, char value[128])
{
    char path[256];
    struct stat st;
    if (!path_for(p, leaf, path)) return -1;
    int fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return -1;
    ssize_t n = -1;
    if (fstat(fd, &st) == 0 && S_ISREG(st.st_mode) && st.st_size < 128)
        n = read(fd, value, 127);
    close(fd);
    if (n <= 0) return -1;
    value[n] = 0;
    value[strcspn(value, "\r\n")] = 0;
    return 0;
}

static void log_event(struct bt_pcm *p, const char *event)
{
    char path[256];
    if (!path_for(p, "bt-pcm.log", path)) return;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
    if (fd < 0) return;
    struct stat st;
    if (fstat(fd, &st) == 0 && st.st_size > 65536) (void)ftruncate(fd, 0);
    (void)write(fd, event, strlen(event));
    (void)write(fd, "\n", 1);
    close(fd);
}

/* mutex held: never close another player's PCM or change amplifier state. */
static void close_slave(struct bt_pcm *p)
{
    if (!p->slave) return;
    snd_pcm_drop(p->slave);
    snd_pcm_close(p->slave);
    p->slave = NULL;
    log_event(p, "Bluetooth DSP slave released");
}

static int voice_pending(struct bt_pcm *p)
{
    char value[128];
    if (read_marker(p, "voice-owns", value) < 0) return 0;
    uint64_t stamp = 0;
    for (const char *c = value; *c; ++c) {
        if (*c < '0' || *c > '9' || stamp > UINT64_MAX / 10 - 10) return 0;
        stamp = stamp * 10 + (unsigned)(*c - '0');
    }
    struct timespec now;
    if (clock_gettime(CLOCK_BOOTTIME, &now) < 0) return 1;
    return *value && stamp <= (uint64_t)now.tv_sec && (uint64_t)now.tv_sec - stamp < 300;
}

static int open_allowed(struct bt_pcm *p)
{
    char value[128];
    if (!exists(p, "safe/armed") || exists(p, "safe/tripped") || exists(p, "ap2-claim") ||
        voice_pending(p)) return 0;
    char hold[256];
    int n = snprintf(hold, sizeof(hold), "%.*s-hold-mute",
                     (int)strlen(p->run) - 4, p->run);
    if (n <= 0 || n >= (int)sizeof(hold) || access(hold, F_OK) == 0) return 0;
    if (!exists(p, "safe/lease")) return 1;
    return read_marker(p, "safe/lease", value) == 0 &&
           strncmp(value, "bluetooth ", 10) == 0;
}

static int open_slave(struct bt_pcm *p)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) return -EAGAIN;
    uint64_t ns = (uint64_t)now.tv_sec * 1000000000 + (uint64_t)now.tv_nsec;
    if (ns < p->retry_after_ns) return -EAGAIN;
    p->retry_after_ns = ns + 250000000;
    /* Publish before rechecking source claims. The hook publishes its
     * claim first, then waits for this marker before testing PCM ownership.
     * Thus a new source cannot miss an in-flight Bluetooth hardware open. */
    int marker = open(p->opening_path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (marker < 0) return -EAGAIN;
    close(marker);
    if (!open_allowed(p)) { unlink(p->opening_path); return -EAGAIN; }
    int rc = snd_pcm_open(&p->slave, p->slave_name,
                          SND_PCM_STREAM_PLAYBACK, SND_PCM_NONBLOCK);
    if (rc < 0) {
        p->slave = NULL;
        log_event(p, "Bluetooth DSP slave open failed; bounded retry");
        unlink(p->opening_path);
        return rc;
    }
    /* The verified factory Bluetooth caller tags PCM client 11 after
     * open. Apply the same stock-only API to each lazily reopened slave. */
    int (*stock_info_set)(snd_pcm_t *, int) = dlsym(RTLD_DEFAULT, "snd_pcm_info_set");
    if (stock_info_set) (void)stock_info_set(p->slave, 11);
    rc = snd_pcm_set_params(p->slave, p->io.format,
                           SND_PCM_ACCESS_RW_INTERLEAVED,
                           p->io.channels, p->io.rate, 1, 500000);
    if (rc >= 0) {
        /* A fresh slave must start before filling all the retained
         * virtual ring: the outer software/rate layers can already have
         * pending frames. Keep a half-buffer prefill, at least a period. */
        snd_pcm_uframes_t buffer = 0, period = 0;
        snd_pcm_sw_params_t *sw;
        snd_pcm_sw_params_alloca(&sw);
        rc = snd_pcm_get_params(p->slave, &buffer, &period);
        if (rc >= 0 && (!buffer || !period || period > buffer)) rc = -EINVAL;
        snd_pcm_uframes_t threshold = buffer / 2;
        if (threshold < period) threshold = period;
        if (rc >= 0) rc = snd_pcm_sw_params_current(p->slave, sw);
        if (rc >= 0) rc = snd_pcm_sw_params_set_start_threshold(p->slave, sw, threshold);
        if (rc >= 0) rc = snd_pcm_sw_params(p->slave, sw);
    }
    if (rc < 0 || !open_allowed(p)) {
        if (rc < 0) {
            char message[192];
            snprintf(message, sizeof(message), "Bluetooth DSP slave parameters failed: %d (%s)",
                     rc, snd_strerror(rc));
            log_event(p, message);
        }
        close_slave(p); unlink(p->opening_path);
        return rc < 0 ? rc : -EAGAIN;
    }
    p->retry_after_ns = 0;
    log_event(p, "Bluetooth DSP slave opened");
    unlink(p->opening_path);
    return 0;
}

static snd_pcm_sframes_t transfer(snd_pcm_ioplug_t *io,
                                  const snd_pcm_channel_area_t *areas,
                                  snd_pcm_uframes_t offset,
                                  snd_pcm_uframes_t frames)
{
    struct bt_pcm *p = io->private_data;
    unsigned width = (unsigned)snd_pcm_format_physical_width(io->format);
    if (!p->transfer_logged) {
        char message[192];
        snprintf(message, sizeof(message), "first transfer: rate=%u channels=%u format=%d first=%u step=%u frames=%lu",
                 io->rate, io->channels, (int)io->format, areas[0].first,
                 areas[0].step, (unsigned long)frames);
        log_event(p, message);
        p->transfer_logged = 1;
    }
    if (areas[0].step != width * io->channels || areas[0].first != 0)
        return -EINVAL;
    const char *data = (const char *)areas[0].addr + offset * areas[0].step / 8;
    pthread_mutex_lock(&p->mutex);
    snd_pcm_sframes_t rc;
    if (!p->slave && !open_allowed(p)) {
        /* Preserve the retained virtual handle while another source owns
         * the output. Discard these samples; never queue a later surprise. */
        rc = (snd_pcm_sframes_t)frames;
    } else if (!p->slave && (rc = open_slave(p)) < 0) {
        /* A busy hardware device remains silent and is retried only when
         * new phone samples arrive. The bridge cannot acquire without PCM. */
        rc = (snd_pcm_sframes_t)frames;
    } else {
        rc = snd_pcm_writei(p->slave, data, frames);
        if (rc == -EPIPE || rc == -ESTRPIPE) {
            int recovery = snd_pcm_recover(p->slave, (int)rc, 1);
            if (recovery >= 0) rc = snd_pcm_writei(p->slave, data, frames);
        }
    }
    if (rc > 0) p->written += (snd_pcm_uframes_t)rc;
    pthread_mutex_unlock(&p->mutex);
    return rc;
}

static snd_pcm_sframes_t pointer_pos(snd_pcm_ioplug_t *io)
{
    struct bt_pcm *p = io->private_data;
    snd_pcm_sframes_t delay = 0;
    pthread_mutex_lock(&p->mutex);
    if (p->slave && snd_pcm_delay(p->slave, &delay) < 0) delay = 0;
    uint64_t pos = p->written;
    if (delay > 0 && (uint64_t)delay <= pos) pos -= (uint64_t)delay;
    /* The stock ioplug ABI accepts a buffer-modulo pointer. Parking a
     * full slave advances by exactly one ring, which would look like no
     * progress and deadlock future writes. Report that jump over multiple
     * polls, each strictly shorter than a ring. Never move backwards when
     * a reopened slave begins accumulating its own delay. */
    if (io->buffer_size > 1 && pos > p->reported) {
        uint64_t advance = pos - p->reported;
        if (advance >= io->buffer_size) advance = io->buffer_size - 1;
        p->reported += advance;
    }
    pos = p->reported;
    pthread_mutex_unlock(&p->mutex);
    return (snd_pcm_sframes_t)(io->buffer_size ? pos % io->buffer_size : 0);
}

static int prepare(snd_pcm_ioplug_t *io)
{
    struct bt_pcm *p = io->private_data;
    pthread_mutex_lock(&p->mutex);
    p->written = 0;
    p->reported = 0;
    int rc = p->slave ? snd_pcm_prepare(p->slave) : 0;
    pthread_mutex_unlock(&p->mutex);
    return rc;
}
static int hw_params(snd_pcm_ioplug_t *io, snd_pcm_hw_params_t *params)
{
    (void)params;
    struct bt_pcm *p = io->private_data;
    char message[192];
    snprintf(message, sizeof(message), "virtual params: rate=%u channels=%u format=%d period=%lu buffer=%lu",
             io->rate, io->channels, (int)io->format,
             (unsigned long)io->period_size, (unsigned long)io->buffer_size);
    log_event(p, message);
    return 0;
}
static int start(snd_pcm_ioplug_t *io) { (void)io; return 0; }
static int stop(snd_pcm_ioplug_t *io) { (void)io; return 0; }

static int poll_revents(snd_pcm_ioplug_t *io, struct pollfd *fds,
                       unsigned count, unsigned short *events)
{
    struct bt_pcm *p = io->private_data;
    if (count != 1) return -EINVAL;
    *events = fds[0].revents & (POLLERR | POLLHUP | POLLNVAL);
    if (fds[0].revents & POLLIN) {
        uint64_t ticks;
        (void)read(p->timer, &ticks, sizeof(ticks));
        *events |= POLLOUT;
    }
    return 0;
}

static int park(struct bt_pcm *p, const char *token)
{
    char marker[128];
    if (!*token || read_marker(p, "bt-pcm-park", marker) < 0 || strcmp(token, marker))
        return 0;
    pthread_mutex_lock(&p->mutex);
    int allowed = !exists(p, "safe/play.started") &&
                  !exists(p, "safe/lease") && !exists(p, "bt-owner.pid");
    if (allowed) close_slave(p);
    pthread_mutex_unlock(&p->mutex);
    return allowed;
}

static void *control_loop(void *arg)
{
    struct bt_pcm *p = arg;
    while (!__atomic_load_n(&p->stopped, __ATOMIC_ACQUIRE)) {
        struct pollfd fds = {p->server, POLLIN, 0};
        if (poll(&fds, 1, 100) <= 0 || !(fds.revents & POLLIN)) continue;
        int client = accept4(p->server, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (client < 0) continue;
        struct pollfd input = {client, POLLIN, 0};
        char request[128];
        ssize_t n = -1;
        if (poll(&input, 1, 200) > 0) n = recv(client, request, sizeof(request) - 1, 0);
        const char *reply = "refused\n";
        if (n > 0) {
            request[n] = 0;
            if (!strncmp(request, "park:", 5) && park(p, request + 5)) reply = "parked\n";
        }
        (void)send(client, reply, strlen(reply), MSG_NOSIGNAL | MSG_DONTWAIT);
        close(client);
    }
    return NULL;
}

static int close_pcm(snd_pcm_ioplug_t *io)
{
    struct bt_pcm *p = io->private_data;
    __atomic_store_n(&p->stopped, 1, __ATOMIC_RELEASE);
    if (p->threaded) pthread_join(p->thread, NULL);
    if (p->server >= 0) close(p->server);
    if (p->timer >= 0) close(p->timer);
    struct stat st;
    if (p->socket_ino && lstat(p->socket_path, &st) == 0 &&
        st.st_dev == p->socket_dev && st.st_ino == p->socket_ino)
        unlink(p->socket_path);
    close_slave(p);
    pthread_mutex_destroy(&p->mutex);
    free(p->slave_name);
    free(p);
    return 0;
}

static const snd_pcm_ioplug_callback_t callbacks = {
    .start = start, .stop = stop, .pointer = pointer_pos, .transfer = transfer,
    .prepare = prepare, .close = close_pcm,
    .hw_params = hw_params,
    .poll_revents = poll_revents,
};

static int start_control(struct bt_pcm *p)
{
    struct sockaddr_un addr = {.sun_family = AF_UNIX};
    /* Shared-runtime restarts remove run/, while the stock player stays
     * alive. Keep its control socket outside that disposable directory. */
    int n = snprintf(p->socket_path, sizeof(p->socket_path), "%.*s/bt-pcm-control",
                     (int)strlen(p->run) - 4, p->run);
    if (n <= 0 || n >= (int)sizeof(p->socket_path)) return -ENAMETOOLONG;
    strcpy(addr.sun_path, p->socket_path);
    p->server = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (p->server < 0) return -errno;
    if (bind(p->server, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        /* A dead process may leave a socket. Never remove a live server or
         * an unexpected file, including a symlink at this private path. */
        struct stat st;
        int probe = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
        if (probe < 0) return -errno;
        int rc = connect(probe, (struct sockaddr *)&addr, sizeof(addr));
        int saved = errno;
        close(probe);
        if (rc == 0 || saved != ECONNREFUSED || lstat(p->socket_path, &st) < 0 ||
            !S_ISSOCK(st.st_mode) || st.st_uid != geteuid()) return -EADDRINUSE;
        if (unlink(p->socket_path) < 0 ||
            bind(p->server, (struct sockaddr *)&addr, sizeof(addr)) < 0) return -errno;
    }
    struct stat st;
    if (lstat(p->socket_path, &st) < 0) return -errno;
    p->socket_dev = st.st_dev;
    p->socket_ino = st.st_ino;
    n = snprintf(p->opening_path, sizeof(p->opening_path), "%.*s/bt-pcm-opening",
                 (int)strlen(p->run) - 4, p->run);
    if (n <= 0 || n >= (int)sizeof(p->opening_path)) return -ENAMETOOLONG;
    /* A successfully bound exclusive server excludes a live instance.
     * A prior process death may have left its in-flight marker behind. */
    if (lstat(p->opening_path, &st) == 0) {
        if (!S_ISREG(st.st_mode) || st.st_uid != geteuid()) return -EEXIST;
        if (unlink(p->opening_path) < 0) return -errno;
    }
    if (chmod(p->socket_path, 0600) < 0 || listen(p->server, 2) < 0) return -errno;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    long minimum = sysconf(_SC_THREAD_STACK_MIN);
    size_t stack_size = minimum > 65536 ? (size_t)minimum : 65536;
    int rc = pthread_attr_setstacksize(&attr, stack_size);
    if (rc == 0) rc = pthread_create(&p->thread, &attr, control_loop, p);
    pthread_attr_destroy(&attr);
    if (rc) return -rc;
    p->threaded = 1;
    return 0;
}

SND_PCM_PLUGIN_DEFINE_FUNC(hkbtpark)
{
    (void)root;
    if (stream != SND_PCM_STREAM_PLAYBACK) return -EINVAL;
    const char *slave = NULL;
    snd_config_iterator_t i, next;
    snd_config_for_each(i, next, conf) {
        snd_config_t *entry = snd_config_iterator_entry(i);
        const char *id;
        if (snd_config_get_id(entry, &id) < 0) continue;
        if (!strcmp(id, "type") || !strcmp(id, "comment") || !strcmp(id, "hint")) continue;
        if (strcmp(id, "slave") || snd_config_get_string(entry, &slave) < 0) {
            SNDERR("hkbtpark: invalid configuration field %s", id);
            return -EINVAL;
        }
    }
    if (!slave) return -EINVAL;
    struct bt_pcm *p = calloc(1, sizeof(*p));
    if (!p) return -ENOMEM;
    p->server = -1;
    p->timer = -1;
    p->slave_name = strdup(slave);
    const char *run = getenv("HK_BT_PCM_RUN");
    if (!run || !*run) run = "/tmp/hk-ap2/run";
    if (!p->slave_name || run[0] != '/' || strlen(run) < 5 ||
        strlen(run) >= sizeof(p->run) || strcmp(run + strlen(run) - 4, "/run")) {
        free(p->slave_name); free(p); return -EINVAL;
    }
    strcpy(p->run, run);
    int rc = pthread_mutex_init(&p->mutex, NULL);
    if (rc) { free(p->slave_name); free(p); return -rc; }
    /* Stock ALSA 1.0.25 accepts ioplug 1.0.0/1.0.1. The handle layout and
     * callbacks used here are shared with newer headers; channel-map
     * callbacks from protocol 1.0.2 are deliberately not requested. */
    p->io.version = 0x010001;
    p->io.name = "Allure releasable Bluetooth PCM";
    p->io.callback = &callbacks;
    p->io.private_data = p;
    p->timer = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC);
    struct itimerspec ticks = {{0, 10000000}, {0, 10000000}};
    if (p->timer < 0 || timerfd_settime(p->timer, 0, &ticks, NULL) < 0) {
        rc = -errno; close_pcm(&p->io); return rc;
    }
    p->io.poll_fd = p->timer;
    p->io.poll_events = POLLIN;
    /* softvol/route negotiate an mmap slave. Advertise that access below,
     * while ordinary writei must still invoke transfer directly: setting
     * mmap_rw would make RW commits advance without forwarding samples. */
    p->io.mmap_rw = 0;
    p->io.flags = SND_PCM_IOPLUG_FLAG_MONOTONIC;
    rc = snd_pcm_ioplug_create(&p->io, name, stream, mode);
    if (rc < 0) { close_pcm(&p->io); return rc; }
    const unsigned access[] = {SND_PCM_ACCESS_RW_INTERLEAVED, SND_PCM_ACCESS_MMAP_INTERLEAVED};
    const unsigned formats[] = {SND_PCM_FORMAT_S16_LE};
    const unsigned rates[] = {44100, 48000};
    if ((rc = snd_pcm_ioplug_set_param_list(&p->io, SND_PCM_IOPLUG_HW_ACCESS, 2, access)) < 0 ||
        (rc = snd_pcm_ioplug_set_param_list(&p->io, SND_PCM_IOPLUG_HW_FORMAT, 1, formats)) < 0 ||
        (rc = snd_pcm_ioplug_set_param_list(&p->io, SND_PCM_IOPLUG_HW_RATE, 2, rates)) < 0 ||
        (rc = snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_CHANNELS, 2, 2)) < 0 ||
        (rc = snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_PERIOD_BYTES, 64, 65536)) < 0 ||
        (rc = snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_BUFFER_BYTES, 128, 262144)) < 0 ||
        (rc = snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_PERIODS, 2, 1024)) < 0 ||
        (rc = start_control(p)) < 0) {
        SNDERR("hkbtpark: initialisation failed: %s", snd_strerror(rc));
        snd_pcm_ioplug_delete(&p->io);
        return rc;
    }
    *pcmp = p->io.pcm;
    log_event(p, "virtual Bluetooth PCM created");
    return 0;
}
SND_PCM_PLUGIN_SYMBOL(hkbtpark);
