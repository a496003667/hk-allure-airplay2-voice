/*
 * Private Bluetooth PCM route and voice mix for a01controller.
 *
 * The Allure's stock player opens "default" from 0x7e34c for Bluetooth and
 * keeps that exclusive hardware PCM open while paused.  Redirect that one
 * open to the Harman DSP path, then mix a 48 kHz stereo S16 voice file into
 * the same snd_pcm_writei buffers.  The exclusive PCM remains owned by the
 * stock player, so voice does not need a second hardware open.  Samples from
 * the phone are faded to silence while /tmp/hk-ap2/run/voice-inject/active
 * exists.  A launcher must verify the build-specific stock executable before
 * preloading this file.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

typedef struct _snd_pcm snd_pcm_t;
typedef long snd_pcm_sframes_t;
typedef unsigned long snd_pcm_uframes_t;
typedef int (*open_fn)(snd_pcm_t **, const char *, int, int);
typedef snd_pcm_sframes_t (*writei_fn)(snd_pcm_t *, const void *,
                                       snd_pcm_uframes_t);

static snd_pcm_t *bt_pcm;
static int injection_active;
static int voice_fd = -1;
static float duck_gain = 1.0f;
static int16_t *music_buf;
static int16_t *voice_buf;
static size_t scratch_frames;
static const char *inject_dir;
static unsigned signal_frames, signal_nonzero, signal_peak[2];
static int signal_started;

static int bt_run_path(const char *leaf, char path[256])
{
    const char *run = getenv("HK_BT_PCM_RUN");
    if (!run || !*run) run = "/tmp/hk-ap2/run";
    if (*run != '/') return 0;
    int n = snprintf(path, 256, "%s/%s", run, leaf);
    return n > 0 && n < 256;
}

static void mix_logf(const char *format, ...)
{
    char message[512];
    va_list args;
    va_start(args, format);
    int n = vsnprintf(message, sizeof(message), format, args);
    va_end(args);
    if (n <= 0) return;
    if ((size_t)n >= sizeof(message)) n = (int)sizeof(message) - 1;
    char path[256];
    if (!bt_run_path("bt-voice-mix.log", path)) return;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0600);
    if (fd < 0) return;
    if (lseek(fd, 0, SEEK_END) > 65536 && ftruncate(fd, 0) != 0) { close(fd); return; }
    (void)write(fd, message, (size_t)n);
    (void)write(fd, "\n", 1);
    close(fd);
}

/* Explicit diagnostics contain aggregate levels only, never samples. */
static void observe_bt_input(const void *data, snd_pcm_uframes_t frames)
{
    char marker[256];
    if (!bt_run_path("bt-pcm-diagnostics", marker) || access(marker, F_OK)) {
        signal_frames = signal_nonzero = signal_peak[0] = signal_peak[1] = 0;
        signal_started = 0;
        return;
    }
    const int16_t *samples = data;
    for (snd_pcm_uframes_t i = 0; i < frames; ++i) {
        for (unsigned channel = 0; channel < 2; ++channel) {
            int value = samples[i * 2 + channel];
            unsigned magnitude = (unsigned)(value < 0 ? -value : value);
            if (magnitude) ++signal_nonzero;
            if (magnitude > signal_peak[channel]) signal_peak[channel] = magnitude;
        }
    }
    signal_frames += (unsigned)frames;
    if (!signal_started || signal_frames >= 48000) {
        mix_logf("signal stage=player-input frames=%u nonzero=%u peak=%u,%u",
                 signal_frames, signal_nonzero, signal_peak[0], signal_peak[1]);
        signal_started = 1;
        signal_frames = signal_nonzero = signal_peak[0] = signal_peak[1] = 0;
    }
}

static int16_t clip_sample(int value)
{
    if (value > 32767) return 32767;
    if (value < -32768) return -32768;
    return (int16_t)value;
}

static const char *inject_path(const char *leaf, char path[256])
{
    if (!inject_dir)
        inject_dir = getenv("HK_BT_INJECT_DIR");
    if (!inject_dir || !*inject_dir)
        inject_dir = "/tmp/hk-ap2/run/voice-inject";
    snprintf(path, 256, "%s/%s", inject_dir, leaf);
    return path;
}

static int marker_exists(const char *leaf)
{
    char path[256];
    return access(inject_path(leaf, path), F_OK) == 0;
}

static int marker_create(const char *leaf)
{
    char path[256];
    int fd = open(inject_path(leaf, path), O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return -1;
    close(fd);
    return 0;
}

static int marker_remove(const char *leaf)
{
    char path[256];
    return unlink(inject_path(leaf, path));
}

static int resize_scratch(snd_pcm_uframes_t frames)
{
    if (frames <= scratch_frames) return 0;
    size_t bytes = (size_t)frames * 2 * sizeof(int16_t);
    int16_t *m = realloc(music_buf, bytes);
    if (!m) return -1;
    music_buf = m;
    int16_t *v = realloc(voice_buf, bytes);
    if (!v) return -1;
    voice_buf = v;
    scratch_frames = frames;
    return 0;
}

static void request_write_resync(void)
{
    char path[256], temp[288];
    if (!bt_run_path("bt-wait-pcm", path)) return;
    snprintf(temp, sizeof(temp), "%s.tmp.%ld", path, (long)getpid());
    int fd = open(temp, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) return;
    static const char reason[] = "write-backpressure\n";
    int ok = write(fd, reason, sizeof(reason) - 1) == (ssize_t)sizeof(reason) - 1;
    close(fd);
    if (!ok || rename(temp, path) < 0) unlink(temp);
}

static uint64_t write_clock_ns(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) return 0;
    return (uint64_t)now.tv_sec * 1000000000 + (uint64_t)now.tv_nsec;
}

/* The verified stock caller opens nonblocking and drops the rest of a
 * packet on ANY nonnegative result, including a short write. Complete its
 * packet here; voice mixing must run only once, before these retries. */
static snd_pcm_sframes_t write_bt_packet(snd_pcm_t *pcm, const void *data,
                                        snd_pcm_uframes_t frames)
{
    static writei_fn next;
    static int (*wait_pcm)(snd_pcm_t *, int);
    static int retry_logged;
    if (!next) next = (writei_fn)dlsym(RTLD_NEXT, "snd_pcm_writei");
    if (!wait_pcm) wait_pcm = (int (*)(snd_pcm_t *, int))dlsym(RTLD_NEXT, "snd_pcm_wait");
    if (!next) return -ENOSYS;
    if (frames > SIZE_MAX / 4) return -EINVAL;
    uint64_t budget_ms = 500 + (uint64_t)frames * 1000 / 44100;
    if (budget_ms > 2000) budget_ms = 2000;
    uint64_t began = write_clock_ns();
    snd_pcm_uframes_t done = 0;
    unsigned calls = 0;
    while (done < frames) {
        snd_pcm_sframes_t n = next(pcm, (const char *)data + done * 4, frames - done);
        ++calls;
        if (n > 0 && (snd_pcm_uframes_t)n <= frames - done) done += (snd_pcm_uframes_t)n;
        else if (n != 0 && n != -EAGAIN && n != -EINTR) {
            request_write_resync();
            return n < 0 ? n : -EIO;
        }
        if (done == frames) break;
        uint64_t now = write_clock_ns();
        if ((began && now >= began && now - began >= budget_ms * 1000000) || calls >= 4096) {
            mix_logf("Bluetooth packet stalled frames=%lu written=%lu", (unsigned long)frames,
                     (unsigned long)done);
            request_write_resync();
            return -EAGAIN;
        }
        if (n <= 0) {
            if (wait_pcm) (void)wait_pcm(pcm, 10);
            usleep(1000);
        }
    }
    if (calls > 1 && !retry_logged) {
        mix_logf("completed Bluetooth packet across short writes frames=%lu calls=%u",
                 (unsigned long)frames, calls);
        retry_logged = 1;
    }
    return (snd_pcm_sframes_t)frames;
}

static void close_voice(void)
{
    if (voice_fd >= 0) close(voice_fd);
    voice_fd = -1;
}

static snd_pcm_sframes_t mix_voice(snd_pcm_t *pcm, const void *data,
                                   snd_pcm_uframes_t frames, writei_fn real_write)
{
    char path[256];
    size_t voice_frames = 0;
    int active = marker_exists("active");
    if (marker_exists("abort")) {
        close_voice();
        marker_remove("start");
        marker_remove("abort");
        marker_create("aborted");
        mix_logf("aborted voice generation");
    }

    if (!active && duck_gain >= 0.999f && voice_fd < 0)
        return real_write(pcm, data, frames);
    if (resize_scratch(frames) != 0)
        return real_write(pcm, data, frames);

    memcpy(music_buf, data, (size_t)frames * 2 * sizeof(int16_t));

    if (active && !injection_active) {
        marker_create("attached");
        mix_logf("attached frames=%lu", (unsigned long)frames);
    }
    if (!active) {
        if (injection_active)
            mix_logf("detached");
        close_voice();
        marker_remove("start");
    }
    injection_active = active;

    if (active && voice_fd < 0 && marker_exists("start")) {
        inject_path("pcm.raw", path);
        voice_fd = open(path, O_RDONLY);
        marker_remove("start");
        marker_remove("done");
        if (voice_fd >= 0) {
            ssize_t got = read(voice_fd, voice_buf,
                               (size_t)frames * 2 * sizeof(int16_t));
            if (got > 0) voice_frames = (size_t)got / 4;
            mix_logf("open read=%ld frames=%lu", (long)got,
                     (unsigned long)voice_frames);
            if (got <= 0 ||
                (size_t)got < (size_t)frames * 2 * sizeof(int16_t)) {
                close_voice();
                marker_create("done");
                mix_logf("eof after first read");
            }
        } else {
            marker_create("done");
            mix_logf("open failed");
        }
    } else if (active && voice_fd >= 0) {
        ssize_t got = read(voice_fd, voice_buf,
                           (size_t)frames * 2 * sizeof(int16_t));
        if (got > 0) voice_frames = (size_t)got / 4;
        if (got <= 0 || (size_t)got < (size_t)frames * 2 * sizeof(int16_t)) {
            close_voice();
            marker_create("done");
            mix_logf("eof read=%ld", (long)got);
        }
    }

    const float step = 1.0f / 2400.0f; /* 50 ms ramp at 48 kHz */
    for (snd_pcm_uframes_t i = 0; i < frames; i++) {
        if (active && duck_gain > 0) {
            duck_gain -= step;
            if (duck_gain < 0) duck_gain = 0;
        } else if (!active && duck_gain < 1) {
            duck_gain += step;
            if (duck_gain > 1) duck_gain = 1;
        }
        int32_t music = (int32_t)(music_buf[2 * i] * duck_gain);
        int32_t music_r = (int32_t)(music_buf[2 * i + 1] * duck_gain);
        if (i < voice_frames) {
            music += voice_buf[2 * i];
            music_r += voice_buf[2 * i + 1];
        }
        music_buf[2 * i] = clip_sample(music);
        music_buf[2 * i + 1] = clip_sample(music_r);
    }
    return real_write(pcm, music_buf, frames);
}

/* Do not propagate the process-specific hook to executables it launches. */
__attribute__((constructor)) static void avoid_child_preload(void)
{
    unsetenv("LD_PRELOAD");
}

const char *hk_bt_selected_pcm(const char *name, int stream,
                               unsigned long caller)
{
    static const char original[] = "default";
    static const char redirected[] = "hk_bt_softvol";
    unsigned int i = 0;

    if (!name || stream != 0 || caller != 0x7e34cUL)
        return name;
    while (name[i] && original[i] && name[i] == original[i])
        i++;
    if (name[i] || original[i])
        return name;
    return redirected;
}

__attribute__((noinline))
int snd_pcm_open(snd_pcm_t **pcm, const char *name, int stream, int mode)
{
    open_fn real_open = (open_fn)dlsym(RTLD_NEXT, "snd_pcm_open");
    const char *selected;

    if (!real_open)
        return -38;                    /* -ENOSYS */
    selected = hk_bt_selected_pcm(name, stream,
                                  (unsigned long)__builtin_return_address(0));
    int rc = real_open(pcm, selected, stream, mode);
    if (rc == 0 && selected && !strcmp(selected, "hk_bt_softvol"))
        bt_pcm = *pcm;
    else if (rc == 0 && *pcm == bt_pcm)
        bt_pcm = NULL; /* A later non-Bluetooth open reused the old handle. */
    return rc;
}

__attribute__((noinline, visibility("default")))
snd_pcm_sframes_t snd_pcm_writei(snd_pcm_t *pcm, const void *data,
                                 snd_pcm_uframes_t frames)
{
    static writei_fn real_write;
    if (!real_write)
        real_write = (writei_fn)dlsym(RTLD_NEXT, "snd_pcm_writei");
    if (!real_write)
        return -38;
    if (!pcm || !data || frames == 0 || pcm != bt_pcm)
        return real_write(pcm, data, frames);
    observe_bt_input(data, frames);
    return mix_voice(pcm, data, frames, write_bt_packet);
}
