/* Read only the verified stock firmware's amplifier/global-mute fields.
 * The caller pins libmvmsg before trusting this layout. Never dump the
 * shared context, which also contains unrelated private device settings.
 */
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ipc.h>
#include <sys/shm.h>
#include <unistd.h>

#ifndef HK_STOCK_AUDIO_SHM_KEY
#define HK_STOCK_AUDIO_SHM_KEY 0x8641
#endif

static uint32_t word(const unsigned char *base, unsigned offset)
{
    uint32_t value;
    memcpy(&value, base + offset, sizeof(value));
    return value;
}

int main(int argc, char **argv)
{
    int global = argc == 2 && !strcmp(argv[1], "--global-unmuted");
    int all = argc == 2 && !strcmp(argv[1], "--unmuted");
    if (argc != 1 && !global && !all) return 2;
    alarm(3);
    int id = shmget((key_t)HK_STOCK_AUDIO_SHM_KEY, 0, 0);
    struct shmid_ds info;
    if (id < 0 || shmctl(id, IPC_STAT, &info) < 0 || info.shm_segsz < 0x2618) return 2;
    const unsigned char *context = shmat(id, NULL, SHM_RDONLY);
    if (context == (void *)-1) return 2;
    uint32_t pa = word(context, 0x20bc);
    uint32_t force = word(context, 0x2368);
    uint32_t gate = word(context, 0x2614);
    shmdt(context);
    if (pa > 1 || force > 1 || gate > 1) return 4;
    printf("PA mute=%u force_mute=%u gate=%u\n", pa, force, gate);
    if ((global && force) || (all && (force || pa || !gate))) return 3;
    return 0;
}
