/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "include/orbit_telemetry.h"
static void pause_ms(unsigned int ms)
{
	struct timespec t = {.tv_sec = ms / 1000, .tv_nsec = (ms % 1000) * 1000000L};
	while (nanosleep(&t, &t) && errno == EINTR) {}
}
static void set_rate(int fd, unsigned int ms)
{
	struct orbit_rate r = {.abi_version = 1, .period_ms = ms};
	assert(ioctl(fd, ORBIT_IOC_SET_RATE, &r) == 0);
}
static void drain(int fd)
{
	struct orbit_sample samples[64];
	while (read(fd, samples, sizeof(samples)) > 0) {}
	assert(errno == EAGAIN);
}
static void nothing(int sig) { (void)sig; }
int main(int argc, char **argv)
{
	int fd = open("/dev/orbit_telemetry", O_RDONLY | O_NONBLOCK);
	struct orbit_sample samples[64];
	struct orbit_stats before, after;
	struct orbit_rate bad = {.abi_version = 1, .period_ms = 0};
	struct pollfd p = {.fd = fd, .events = POLLIN};
	assert(fd >= 0);
	assert(lseek(fd, 0, SEEK_SET) == -1 && errno == ESPIPE);
	alarm(20);
	assert(open("/dev/orbit_telemetry", O_RDONLY) == -1 && errno == EBUSY);
	assert(read(fd, samples, 0) == 0);
	assert(read(fd, samples, 1) == -1 && errno == EINVAL);
	assert(ioctl(fd, _IO('O', 99), 0) == -1 && errno == ENOTTY);
	assert(ioctl(fd, ORBIT_IOC_SET_RATE, &bad) == -1 && errno == EINVAL);
	bad.period_ms = 100; bad.abi_version = 2;
	assert(ioctl(fd, ORBIT_IOC_SET_RATE, &bad) == -1 && errno == EINVAL);
	bad.abi_version = 1; bad.reserved[0] = 1;
	assert(ioctl(fd, ORBIT_IOC_SET_RATE, &bad) == -1 && errno == EINVAL);
	set_rate(fd, 60000);
	drain(fd);
	assert(poll(&p, 1, 0) == 0);
	assert(read(fd, samples, sizeof(samples)) == -1 && errno == EAGAIN);
	set_rate(fd, 1);
	assert(poll(&p, 1, 2000) == 1 && (p.revents & POLLIN));
	set_rate(fd, 60000);
	assert(ioctl(fd, ORBIT_IOC_GET_STATS, &before) == 0);
	void *bad_page = mmap(NULL, 4096, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	assert(bad_page != MAP_FAILED);
	assert(ioctl(fd, ORBIT_IOC_GET_STATS, bad_page) == -1 && errno == EFAULT);
	assert(ioctl(fd, ORBIT_IOC_SET_RATE, bad_page) == -1 && errno == EFAULT);
	assert(read(fd, bad_page, sizeof(samples[0])) == -1 && errno == EFAULT);
	assert(munmap(bad_page, 4096) == 0);
	assert(ioctl(fd, ORBIT_IOC_GET_STATS, &after) == 0);
	assert(before.delivered == after.delivered && before.queued == after.queued);
	assert(after.abi_version == ORBIT_ABI_VERSION && after.capacity == ORBIT_FIFO_CAPACITY);
	assert(!after.dead && !after.reserved[0] && !after.reserved[1]);
	ssize_t n = read(fd, samples, sizeof(samples));
	assert(n >= (ssize_t)sizeof(samples[0]) && n % sizeof(samples[0]) == 0);
	assert(samples[0].abi_version == 1 && samples[0].flags == ORBIT_SAMPLE_TEST_SOURCE);
	assert(!samples[0].reserved[0] && !samples[0].reserved[1]);
	set_rate(fd, 1);
	pause_ms(1000);
	set_rate(fd, 60000);
	assert(ioctl(fd, ORBIT_IOC_GET_STATS, &after) == 0);
	assert(after.queued == 64 && after.dropped > 0);
	assert(after.generated == after.enqueued + after.dropped);
	assert(after.enqueued == after.delivered + after.queued);
	drain(fd);
	assert(fcntl(fd, F_SETFL, 0) == 0);
	if (argc == 2 && !strcmp(argv[1], "--detach")) {
		pid_t child = fork();
		assert(child >= 0);
		if (!child) {
			close(fd);
			pause_ms(100);
			int unbind = open("/sys/bus/platform/drivers/orbit-test/unbind", O_WRONLY);
			if (unbind < 0 || write(unbind, "orbit-test", 10) != 10)
				_exit(1);
			close(unbind);
			_exit(0);
		}
		assert(read(fd, samples, sizeof(samples)) == -1 && errno == ENODEV);
		assert(poll(&p, 1, 0) == 1 && (p.revents & POLLHUP) && (p.revents & POLLERR));
		assert(ioctl(fd, ORBIT_IOC_GET_STATS, &after) == -1 && errno == ENODEV);
		int status;
		assert(waitpid(child, &status, 0) == child && WIFEXITED(status) && !WEXITSTATUS(status));
	} else {
		/* Blocking wakeup from producer; then signal interruption of empty wait. */
		set_rate(fd, 10);
		assert(read(fd, samples, sizeof(samples)) >= (ssize_t)sizeof(samples[0]));
		set_rate(fd, 60000);
		assert(fcntl(fd, F_SETFL, O_NONBLOCK) == 0);
		drain(fd);
		assert(fcntl(fd, F_SETFL, 0) == 0);
		struct sigaction action = {.sa_handler = nothing};
		sigemptyset(&action.sa_mask);
		assert(sigaction(SIGALRM, &action, NULL) == 0);
		alarm(1);
		assert(read(fd, samples, sizeof(samples)) == -1 && errno == EINTR);
		alarm(0);
	}
	assert(close(fd) == 0);
	puts("PASS: Linux driver syscall contract");
	return 0;
}
