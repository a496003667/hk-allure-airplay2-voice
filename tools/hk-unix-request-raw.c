typedef unsigned long uptr;

struct sockaddr_un {
	unsigned short sun_family;
	char sun_path[108];
};

static long syscall1(long number, long a)
{
	register long r7 __asm__("r7") = number;
	register long r0 __asm__("r0") = a;

	__asm__ volatile("svc 0"
			 : "+r"(r0)
			 : "r"(r7)
			 : "memory");
	return r0;
}

static long syscall2(long number, long a, long b)
{
	register long r7 __asm__("r7") = number;
	register long r0 __asm__("r0") = a;
	register long r1 __asm__("r1") = b;

	__asm__ volatile("svc 0"
			 : "+r"(r0)
			 : "r"(r7), "r"(r1)
			 : "memory");
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

static long string_length(const char *value)
{
	long length = 0;

	while (value[length])
		length++;
	return length;
}

int main(int argc, char **argv)
{
	struct sockaddr_un address;
	const char *path;
	const char *command;
	char response[8192];
	long length;
	long result;
	int fd;
	int i;

	if (argc < 3)
		return 2;

	path = argv[1];
	command = argv[2];
	length = string_length(path);
	if (length < 1 || length >= (long)sizeof(address.sun_path))
		return 3;

	for (i = 0; i < (int)sizeof(address); i++)
		((char *)&address)[i] = 0;
	address.sun_family = 1;
	for (i = 0; i < (int)length; i++)
		address.sun_path[i] = path[i];

	fd = (int)syscall3(281, 1, 1, 0);
	if (fd < 0)
		return 4;

	result = syscall3(283, fd, (long)&address, sizeof(address));
	if (result < 0) {
		syscall1(6, fd);
		return 5;
	}

	length = string_length(command);
	result = syscall3(4, fd, (long)command, length + 1);
	if (result != length + 1) {
		syscall1(6, fd);
		return 6;
	}

	/* Half-close so the server knows the request is complete. */
	syscall2(293, fd, 1);

	for (;;) {
		result = syscall3(3, fd, (long)response, sizeof(response));
		if (result <= 0)
			break;
		syscall3(4, 1, (long)response, result);
	}

	syscall1(6, fd);
	return 0;
}
