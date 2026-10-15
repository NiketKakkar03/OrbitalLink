/* SPDX-License-Identifier: GPL-2.0 */
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>
#include "include/orbit_sensors.h"
static void stats(int fd, struct orbit_stats *s)
{
	assert(ioctl(fd, ORBIT_SENSOR_GET_STATS, s) == 0);
	assert(s->abi_version == 2 && s->capacity == 320);
	assert(s->generated == s->enqueued + s->dropped);
	assert(s->enqueued == s->delivered + s->queued);
}
int main(void)
{
	struct orbit_sensor_sample samples[16];
	struct orbit_stats before, after;
	struct orbit_rate rate = {.abi_version = 2, .period_ms = 10};
	unsigned counts[7] = {0}, total = 0;
	uint64_t last_ns = 0;
	int fd = open("/dev/orbit_sensors", O_RDONLY | O_NONBLOCK);
	assert(fd >= 0);
	alarm(40);
	assert(sizeof(samples[0]) == 64);
	assert(lseek(fd, 0, SEEK_SET) == -1 && errno == ESPIPE);
	assert(open("/dev/orbit_sensors", O_RDONLY) == -1 && errno == EBUSY);
	assert(read(fd, samples, 1) == -1 && errno == EINVAL);
	assert(ioctl(fd, ORBIT_IOC_GET_STATS, &before) == -1 && errno == ENOTTY);
	assert(ioctl(fd, ORBIT_SENSOR_SET_RATE, &rate) == 0);
	while (total < 606) {
		struct pollfd p = {.fd = fd, .events = POLLIN};
		assert(poll(&p, 1, 2000) == 1 && (p.revents & POLLIN));
		unsigned remaining = 606 - total;
		ssize_t n = read(fd, samples, (remaining < 16 ? remaining : 16) * sizeof(samples[0]));
		assert(n > 0 && n % sizeof(samples[0]) == 0);
		for (unsigned i = 0; i < (unsigned)n / sizeof(samples[0]); i++) {
			struct orbit_sensor_sample *s = &samples[i];
			assert(s->abi_version == 2 && s->schema == 1 && !s->quality);
			assert(s->generation == 1 && s->sequence == total);
			assert(s->monotonic_ns >= last_ns && s->monotonic_ns > 0);
			last_ns = s->monotonic_ns;
			assert(s->category >= 1 && s->category <= 6);
			counts[s->category]++; total++;
		}
		stats(fd, &after);
	}
	rate.period_ms = 60000;
	assert(ioctl(fd, ORBIT_SENSOR_SET_RATE, &rate) == 0);
	assert(total == 606);
	assert(counts[1] == 120 && counts[2] == 60 && counts[3] == 300);
	assert(counts[4] == 60 && counts[5] == 60 && counts[6] == 6);
	stats(fd, &before);
	assert(before.dropped == 0);
	void *bad = mmap(NULL, 4096, PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	assert(bad != MAP_FAILED);
	assert(ioctl(fd, ORBIT_SENSOR_GET_STATS, bad) == -1 && errno == EFAULT);
	assert(munmap(bad, 4096) == 0);
	assert(ioctl(fd, ORBIT_SENSOR_RESET) == 0);
	struct pollfd p = {.fd = fd, .events = POLLIN};
	assert(poll(&p, 1, 2000) == 1);
	assert(read(fd, samples, sizeof(samples[0])) == (ssize_t)sizeof(samples[0]));
	assert(samples[0].category == 6 && samples[0].generation == 2);
	assert(samples[0].sequence == 0 && samples[0].values[0] == 7);
	stats(fd, &after);
	assert(after.reserved[0] == 2 && after.reserved[1] == 1);
	assert(after.delivered == before.delivered + 1);
	assert(close(fd) == 0);
	puts("PASS: ABI2 real MMIO/IRQ coherent samples, profile, counters, reset event");
	return 0;
}
