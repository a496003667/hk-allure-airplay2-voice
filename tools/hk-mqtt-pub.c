/*
 * Minimal MQTT 3.1.1 QoS 0 publisher for the local broker on 127.0.0.1:1883.
 *
 *   hk-mqtt-pub <topic> <payload>
 *
 * Used to send Shairport Sync 5.6 remote commands ("<topic>/remote" with
 * "playpause", "volumeup", ...).  No libc: raw ARM EABI syscalls, like
 * hk-unix-request-raw.  Exit status 0 only after the broker's CONNACK.
 */

struct sockaddr_in {
	unsigned short sin_family;
	unsigned short sin_port;
	unsigned int sin_addr;
	unsigned char sin_zero[8];
};

struct timeval {
	long tv_sec;
	long tv_usec;
};

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

static long syscall5(long number, long a, long b, long c, long d, long e)
{
	register long r7 __asm__("r7") = number;
	register long r0 __asm__("r0") = a;
	register long r1 __asm__("r1") = b;
	register long r2 __asm__("r2") = c;
	register long r3 __asm__("r3") = d;
	register long r4 __asm__("r4") = e;

	__asm__ volatile("svc 0"
			 : "+r"(r0)
			 : "r"(r7), "r"(r1), "r"(r2), "r"(r3), "r"(r4)
			 : "memory");
	return r0;
}

static int string_length(const char *value)
{
	int length = 0;

	while (value[length])
		length++;
	return length;
}

static int put_string(unsigned char *out, const char *value, int length)
{
	int i;

	out[0] = (unsigned char)(length >> 8);
	out[1] = (unsigned char)length;
	for (i = 0; i < length; i++)
		out[2 + i] = (unsigned char)value[i];
	return 2 + length;
}

static int put_remaining_length(unsigned char *out, int length)
{
	int used = 0;

	do {
		unsigned char byte = length % 128;

		length /= 128;
		if (length > 0)
			byte |= 0x80;
		out[used++] = byte;
	} while (length > 0);
	return used;
}

static int send_all(int fd, const unsigned char *data, int length)
{
	while (length > 0) {
		long written = syscall3(4, fd, (long)data, length);

		if (written <= 0)
			return -1;
		data += written;
		length -= (int)written;
	}
	return 0;
}

/* libgcc's divide-by-zero path references raise(); nothing divides by 0. */
int raise(int signal)
{
	(void)signal;
	return 0;
}

int main(int argc, char **argv)
{
	static const unsigned char connect_header[] = {
		0x00, 0x04, 'M', 'Q', 'T', 'T', 0x04, 0x02, 0x00, 0x0a
	};
	static const char client_id[] = "hk-mqtt-pub";
	unsigned char packet[512];
	unsigned char body[512];
	unsigned char reply[4];
	struct sockaddr_in address;
	struct timeval timeout = { 2, 0 };
	int topic_length;
	int payload_length;
	int body_length;
	int used;
	int fd;
	int i;

	if (argc != 3)
		return 2;
	topic_length = string_length(argv[1]);
	payload_length = string_length(argv[2]);
	if (topic_length < 1 || topic_length > 200 || payload_length > 200)
		return 2;

	fd = (int)syscall3(281, 2, 1, 0); /* socket(AF_INET, SOCK_STREAM) */
	if (fd < 0)
		return 3;
	/* SO_RCVTIMEO / SO_SNDTIMEO so a stuck broker cannot hang callers. */
	syscall5(294, fd, 1, 20, (long)&timeout, sizeof(timeout));
	syscall5(294, fd, 1, 21, (long)&timeout, sizeof(timeout));

	for (i = 0; i < (int)sizeof(address); i++)
		((char *)&address)[i] = 0;
	address.sin_family = 2;
	address.sin_port = (1883 >> 8) | ((1883 & 0xff) << 8);
	address.sin_addr = 0x0100007f; /* 127.0.0.1 */
	if (syscall3(283, fd, (long)&address, sizeof(address)) < 0) {
		syscall1(6, fd);
		return 4;
	}

	/* CONNECT, clean session, keepalive 10 s. */
	body_length = 0;
	for (i = 0; i < (int)sizeof(connect_header); i++)
		body[body_length++] = connect_header[i];
	body_length += put_string(body + body_length, client_id, sizeof(client_id) - 1);
	packet[0] = 0x10;
	used = 1 + put_remaining_length(packet + 1, body_length);
	for (i = 0; i < body_length; i++)
		packet[used + i] = body[i];
	if (send_all(fd, packet, used + body_length) < 0 ||
	    syscall3(3, fd, (long)reply, 4) != 4 || reply[0] != 0x20 || reply[3] != 0) {
		syscall1(6, fd);
		return 5;
	}

	/* PUBLISH, QoS 0. */
	body_length = put_string(body, argv[1], topic_length);
	for (i = 0; i < payload_length; i++)
		body[body_length++] = (unsigned char)argv[2][i];
	packet[0] = 0x30;
	used = 1 + put_remaining_length(packet + 1, body_length);
	for (i = 0; i < body_length; i++)
		packet[used + i] = body[i];
	if (send_all(fd, packet, used + body_length) < 0) {
		syscall1(6, fd);
		return 6;
	}

	packet[0] = 0xe0; /* DISCONNECT */
	packet[1] = 0x00;
	send_all(fd, packet, 2);
	syscall1(6, fd);
	return 0;
}
