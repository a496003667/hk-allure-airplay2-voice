/*
 * Minimal reader/writer for the stock g_wiimu_shm play_status field.
 *
 *   hk-shm-play get          print selected play-state fields
 *   hk-shm-play on           play_status 0/3 -> 4 and the imuzop focus
 *                            suspend flag (s2+0x36c) 1 -> 0, so mv_ioguard
 *                            shows its stock HK_Playing animation
 *   hk-shm-play off          undo: play_status 4/5 -> 0 (never 5, which makes
 *                            a01controller issue setPlayerCmd:resume) and
 *                            the suspend flag 0 -> 1
 *
 * No libc: raw ARM EABI syscalls, like hk-unix-request-raw.
 */

#define SHM_KEY 0x8641
#define SHM_SIZE 14440
#define SHM_RDONLY 010000

#define OFF_PLAY_MODE 0x410
#define OFF_PLAY_STATUS 0x438
#define OFF_BT_STATUS 0x43c
#define OFF_SPOTIFY_STATUS 0x440
#define OFF_AIRPLAY_STATUS 0x448
#define OFF_DEVICE_STATUS 0x75c
#define OFF_ASR_ONGOING 0x2024
#define OFF_MCU_MUTE 0x20bc
#define OFF_ASR_ALERT 0x20c8
#define OFF_PLAY_BLOCK 0x236c
#define OFF_RETAIL 0x26dc
#define OFF_MODEL 0x26f0

/* libgcc's divide-by-zero path references raise(); nothing divides by 0. */
int raise(int signal)
{
	(void)signal;
	return 0;
}

static long syscall1(long number, long a)
{
	register long r7 __asm__("r7") = number;
	register long r0 __asm__("r0") = a;

	__asm__ volatile("svc 0" : "+r"(r0) : "r"(r7) : "memory");
	return r0;
}

static long syscall3(long number, long a, long b, long c)
{
	register long r7 __asm__("r7") = number;
	register long r0 __asm__("r0") = a;
	register long r1 __asm__("r1") = b;
	register long r2 __asm__("r2") = c;

	__asm__ volatile("svc 0"
			 : "+r"(r0)
			 : "r"(r7), "r"(r1), "r"(r2)
			 : "memory");
	return r0;
}

static void put(const char *text)
{
	long length = 0;

	while (text[length])
		length++;
	syscall3(4, 1, (long)text, length);
}

static void put_number(long value)
{
	char buffer[16];
	int at = sizeof(buffer) - 1;
	int negative = value < 0;
	unsigned long magnitude = negative ? -(unsigned long)value : value;

	buffer[at] = 0;
	do {
		buffer[--at] = '0' + magnitude % 10;
		magnitude /= 10;
	} while (magnitude && at > 1);
	if (negative)
		buffer[--at] = '-';
	put(buffer + at);
}

static void field(const char *name, volatile int *base, int offset)
{
	put(name);
	put("=");
	put_number(base[offset / 4]);
	put(" ");
}

static int same(const char *a, const char *b)
{
	while (*a && *a == *b) {
		a++;
		b++;
	}
	return *a == *b;
}

int main(int argc, char **argv)
{
	int write_mode;
	long id;
	long address;
	volatile int *shm;
	int status;

	if (argc != 2)
		return 2;
	write_mode = same(argv[1], "on") || same(argv[1], "off");
	if (!write_mode && !same(argv[1], "get"))
		return 2;

	/* shmget without IPC_CREAT: fail rather than create a new segment. */
	id = syscall3(307, SHM_KEY, SHM_SIZE, 0);
	if (id < 0)
		return 3;
	address = syscall3(305, id, 0, write_mode ? 0 : SHM_RDONLY);
	if (address < 0 && address > -4096)
		return 4;
	shm = (volatile int *)address;
	status = shm[OFF_PLAY_STATUS / 4];

	if (same(argv[1], "on")) {
		if (status == 0 || status == 3)
			shm[OFF_PLAY_STATUS / 4] = 4;
		if (shm[OFF_PLAY_STATUS / 4] == 4 && shm[OFF_PLAY_BLOCK / 4] == 1)
			shm[OFF_PLAY_BLOCK / 4] = 0;
	} else if (same(argv[1], "off")) {
		if (status == 4 || status == 5)
			shm[OFF_PLAY_STATUS / 4] = 0;
		if (shm[OFF_PLAY_BLOCK / 4] == 0)
			shm[OFF_PLAY_BLOCK / 4] = 1;
	}

	if (write_mode) {
		field("play_status", shm, OFF_PLAY_STATUS);
		field("play_block", shm, OFF_PLAY_BLOCK);
		put("\n");
	} else {
		field("play_mode", shm, OFF_PLAY_MODE);
		field("play_status", shm, OFF_PLAY_STATUS);
		field("bt", shm, OFF_BT_STATUS);
		field("spotify", shm, OFF_SPOTIFY_STATUS);
		field("airplay", shm, OFF_AIRPLAY_STATUS);
		field("device_status", shm, OFF_DEVICE_STATUS);
		field("asr_ongoing", shm, OFF_ASR_ONGOING);
		field("mcu_mute", shm, OFF_MCU_MUTE);
		field("asr_alert", shm, OFF_ASR_ALERT);
		field("play_block", shm, OFF_PLAY_BLOCK);
		put("retail=");
		put_number(((volatile unsigned char *)shm)[OFF_RETAIL]);
		put(" ");
		field("model", shm, OFF_MODEL);
		put("\n");
	}
	syscall1(306, address);
	return 0;
}
