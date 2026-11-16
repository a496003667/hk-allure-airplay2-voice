/* Test-only ALSA sink. Unlike null, its hardware pointer advances only
 * after start and at the negotiated sample rate, exposing buffer deadlocks. */
#define _GNU_SOURCE
#include <alsa/asoundlib.h>
#include <alsa/pcm_external.h>
#include <poll.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/timerfd.h>
#include <unistd.h>

struct clock_sink {
    snd_pcm_ioplug_t io;
    struct timespec started;
    int running, timer;
};

static int start(snd_pcm_ioplug_t *io)
{
    struct clock_sink *p = io->private_data;
    clock_gettime(CLOCK_MONOTONIC, &p->started);
    p->running = 1;
    return 0;
}

static int stop(snd_pcm_ioplug_t *io)
{
    struct clock_sink *p = io->private_data;
    p->running = 0;
    return 0;
}

static snd_pcm_sframes_t pointer_pos(snd_pcm_ioplug_t *io)
{
    struct clock_sink *p = io->private_data;
    if (!p->running) return 0;
    const char *hold = getenv("HK_TEST_CLOCK_HOLD");
    if (hold && access(hold, F_OK) == 0) return 0;
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    int64_t ns = (int64_t)(now.tv_sec - p->started.tv_sec) * 1000000000 +
                 now.tv_nsec - p->started.tv_nsec;
    return (snd_pcm_sframes_t)((uint64_t)ns * io->rate / 1000000000 % io->buffer_size);
}

static int close_pcm(snd_pcm_ioplug_t *io)
{
    struct clock_sink *p = io->private_data;
    close(p->timer);
    free(p);
    return 0;
}

static int poll_revents(snd_pcm_ioplug_t *io, struct pollfd *fds,
                        unsigned count, unsigned short *events)
{
    struct clock_sink *p = io->private_data;
    if (count != 1) return -EINVAL;
    *events = fds[0].revents & (POLLERR | POLLHUP | POLLNVAL);
    if (fds[0].revents & POLLIN) {
        uint64_t ticks;
        (void)read(p->timer, &ticks, sizeof(ticks));
        *events |= POLLOUT;
    }
    return 0;
}

static const snd_pcm_ioplug_callback_t callbacks = {
    .start = start, .stop = stop, .prepare = stop, .pointer = pointer_pos,
    .close = close_pcm, .poll_revents = poll_revents,
};

SND_PCM_PLUGIN_DEFINE_FUNC(hkclock)
{
    (void)root; (void)conf;
    struct clock_sink *p = calloc(1, sizeof(*p));
    if (!p) return -ENOMEM;
    p->timer = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC);
    if (p->timer < 0) { free(p); return -errno; }
    struct itimerspec ticks = {{0, 10000000}, {0, 10000000}};
    timerfd_settime(p->timer, 0, &ticks, NULL);
    p->io.version = SND_PCM_IOPLUG_VERSION;
    p->io.name = "Paced test sink";
    p->io.callback = &callbacks;
    p->io.private_data = p;
    p->io.poll_fd = p->timer;
    p->io.poll_events = POLLIN;
    p->io.mmap_rw = 1;
    int rc = snd_pcm_ioplug_create(&p->io, name, stream, mode);
    if (rc < 0) { close_pcm(&p->io); return rc; }
    const unsigned access[] = {SND_PCM_ACCESS_RW_INTERLEAVED, SND_PCM_ACCESS_MMAP_INTERLEAVED};
    const unsigned formats[] = {SND_PCM_FORMAT_S16_LE};
    const unsigned rates[] = {48000};
    if ((rc = snd_pcm_ioplug_set_param_list(&p->io, SND_PCM_IOPLUG_HW_ACCESS, 2, access)) < 0 ||
        (rc = snd_pcm_ioplug_set_param_list(&p->io, SND_PCM_IOPLUG_HW_FORMAT, 1, formats)) < 0 ||
        (rc = snd_pcm_ioplug_set_param_list(&p->io, SND_PCM_IOPLUG_HW_RATE, 1, rates)) < 0 ||
        (rc = snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_CHANNELS, 2, 2)) < 0 ||
        (rc = snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_PERIOD_BYTES, 64, 65536)) < 0 ||
        (rc = snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_BUFFER_BYTES, 128, 262144)) < 0 ||
        (rc = snd_pcm_ioplug_set_param_minmax(&p->io, SND_PCM_IOPLUG_HW_PERIODS, 2, 1024)) < 0) {
        snd_pcm_ioplug_delete(&p->io);
        return rc;
    }
    *pcmp = p->io.pcm;
    return 0;
}
SND_PCM_PLUGIN_SYMBOL(hkclock);
