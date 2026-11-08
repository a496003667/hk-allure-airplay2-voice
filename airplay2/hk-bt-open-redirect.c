/*
 * Redirect only the stock Bluetooth PCM open to the private Harman DSP path.
 *
 * The Allure's a01controller opens the literal ALSA device name "default"
 * from 0x7e34c for Bluetooth playback.  This shim checks both the device name
 * and the return address, so other opens inside the same process keep using
 * their original device.  A launcher must verify the build-specific stock
 * executable before preloading this file.
 */
typedef struct _snd_pcm snd_pcm_t;
typedef int (*open_fn)(snd_pcm_t **, const char *, int, int);

extern void *dlsym(void *, const char *);
extern int unsetenv(const char *);

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
    open_fn real_open = (open_fn)dlsym((void *)-1, "snd_pcm_open");

    if (!real_open)
        return -38;                    /* -ENOSYS */
    return real_open(pcm,
                     hk_bt_selected_pcm(name, stream,
                                        (unsigned long)__builtin_return_address(0)),
                     stream, mode);
}
