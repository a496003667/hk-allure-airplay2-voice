/*
 * Move Shairport's raw PCM pipe into the stock Harman DSP.
 *
 * Shairport 5.6 in the AP2 runtime is a soft-float ARM build, while the
 * stock libasound_module_pcm_harmandsp.so is a hard-float library.  The AP2
 * dynamic loader therefore cannot load the stock plugin.  This tiny bridge
 * runs with the stock ARM ABI and libasound, reads the fixed-format pipe,
 * and opens the DSP only while audio is arriving.
 *
 * Build (the target libasound is taken from an extracted stock rootfs):
 *
 * armv7-linux-gnueabihf-gcc -march=armv7-a -Os -ffreestanding -fno-builtin \
 *   -fno-stack-protector -nostdlib -Wl,-e,_start -Wl,--gc-sections \
 *   -o hk-ap2-dsp-bridge hk-ap2-dsp-bridge.c <rootfs>/lib/libasound.so.2
 */
typedef struct _snd_pcm snd_pcm_t;
extern int snd_pcm_open(snd_pcm_t **, const char *, int, int);
extern int snd_pcm_set_params(snd_pcm_t *, int, int, unsigned, unsigned, int,
                              unsigned);
extern long snd_pcm_writei(snd_pcm_t *, const void *, unsigned long);
extern int snd_pcm_recover(snd_pcm_t *, int, int);
extern int snd_pcm_drain(snd_pcm_t *);
extern int snd_pcm_close(snd_pcm_t *);

struct pollfd {
    int fd;
    short events;
    short revents;
};

enum {
    SYS_EXIT = 1,
    SYS_READ = 3,
    SYS_OPEN = 5,
    SYS_CLOSE = 6,
    SYS_POLL = 168,
    OPEN_READ_ONLY = 0,
    OPEN_NONBLOCK = 00004000,
    POLL_IN = 1,
    PCM_STREAM_PLAYBACK = 0,
    SND_PCM_FORMAT_S32_LE = 10,
    SND_PCM_ACCESS_RW_INTERLEAVED = 3,
    CLOSE_AFTER_MS = 1000,
    BUFFER_BYTES = 32768
};

static long syscall3(long number, long a0, long a1, long a2) {
    register long r7 __asm__("r7") = number;
    register long r0 __asm__("r0") = a0;
    register long r1 __asm__("r1") = a1;
    register long r2 __asm__("r2") = a2;
    __asm__ volatile("svc 0"
                     : "+r"(r0)
                     : "r"(r7), "r"(r1), "r"(r2)
                     : "memory");
    return r0;
}

static void wait_retry(void) {
    long poll_result;
    do {
        poll_result = syscall3(SYS_POLL, 0, 0, 1000);
    } while (poll_result < 0);
}

static int open_pcm(snd_pcm_t **pcm) {
    int result = snd_pcm_open(pcm, "hk_harman_dsp", PCM_STREAM_PLAYBACK, 0);
    if (result < 0) {
        *pcm = 0;
        return -1;
    }

    /* The pipe backend is configured for S32_LE, stereo, 48 kHz. */
    result = snd_pcm_set_params(*pcm, SND_PCM_FORMAT_S32_LE,
                                SND_PCM_ACCESS_RW_INTERLEAVED, 2, 48000, 0,
                                500000);
    if (result < 0) {
        snd_pcm_close(*pcm);
        *pcm = 0;
        return -1;
    }
    return 0;
}

static int file_exists(const char *path) {
    long fd = syscall3(SYS_OPEN, (long)path, OPEN_READ_ONLY, 0);
    if (fd < 0)
        return 0;
    syscall3(SYS_CLOSE, fd, 0, 0);
    return 1;
}

static int airplay_allowed(void) {
    return !file_exists("/tmp/hk-ap2-hold-mute") &&
           !file_exists("/tmp/hk-ap2/run/safe/tripped") &&
           file_exists("/tmp/hk-ap2/run/safe/play.started");
}

static int write_frames(snd_pcm_t *pcm, const unsigned char *data,
                        unsigned long frames) {
    unsigned long written = 0;
    while (written < frames) {
        long result = snd_pcm_writei(pcm, data + written * 8, frames - written);
        if (result == -32) {
            result = snd_pcm_recover(pcm, (int)result, 1);
            if (result >= 0)
                continue;
        }
        if (result <= 0)
            return -1;
        written += (unsigned long)result;
    }
    return 0;
}

static void close_pcm(snd_pcm_t *pcm) {
    if (pcm) {
        snd_pcm_drain(pcm);
        snd_pcm_close(pcm);
    }
}

void _start(void) {
    static const char fifo[] = "/tmp/hk-ap2/run/airplay-pcm.fifo";
    static unsigned char buffer[BUFFER_BYTES + 8] __attribute__((aligned(8)));
    struct pollfd poll_fd;
    snd_pcm_t *pcm = 0;
    long fifo_fd = -1;
    unsigned pending = 0;

    for (;;) {
        long result;

        if (fifo_fd < 0) {
            fifo_fd = syscall3(SYS_OPEN, (long)fifo, OPEN_READ_ONLY |
                                                     OPEN_NONBLOCK, 0);
            if (fifo_fd < 0) {
                wait_retry();
                continue;
            }
        }

        poll_fd.fd = (int)fifo_fd;
        poll_fd.events = POLL_IN;
        poll_fd.revents = 0;
        result = syscall3(SYS_POLL, (long)&poll_fd, 1, CLOSE_AFTER_MS);
        if (result == 0) {
            close_pcm(pcm);
            pcm = 0;
            pending = 0;
            continue;
        }
        if (result < 0)
            continue;

        if ((poll_fd.revents & (POLL_IN | 0x10 | 0x08)) == 0)
            continue;

        result = syscall3(SYS_READ, fifo_fd,
                          (long)(buffer + pending),
                          BUFFER_BYTES - pending);
        if (result == 0) {
            syscall3(SYS_CLOSE, fifo_fd, 0, 0);
            fifo_fd = -1;
            continue;
        }
        if (result < 0)
            continue;

        pending += (unsigned)result;
        if (!pcm && !airplay_allowed()) {
            pending = 0;
            continue;
        }
        if (!pcm && open_pcm(&pcm) < 0) {
            pending = 0;
            continue;
        }

        unsigned frames = pending / 8;
        if (frames && write_frames(pcm, buffer, frames) < 0) {
            close_pcm(pcm);
            pcm = 0;
            pending = 0;
            continue;
        }

        unsigned consumed = frames * 8;
        pending -= consumed;
        for (unsigned i = 0; i < pending; i++)
            buffer[i] = buffer[consumed + i];
    }
}
