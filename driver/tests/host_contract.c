/* SPDX-License-Identifier: GPL-2.0 */
#include <assert.h>
#include <stddef.h>
#include <stdio.h>
#include "orbit_queue.h"
_Static_assert(sizeof(struct orbit_sample) == 40, "sample size");
_Static_assert(offsetof(struct orbit_sample, monotonic_ns) == 16, "timestamp offset");
_Static_assert(offsetof(struct orbit_sample, temperature_mc) == 24, "temperature offset");
_Static_assert(offsetof(struct orbit_sample, reserved) == 32, "reserved offset");
_Static_assert(sizeof(struct orbit_stats) == 56, "stats size");
_Static_assert(offsetof(struct orbit_stats, generated) == 8, "counter offset");
_Static_assert(offsetof(struct orbit_stats, queued) == 40, "depth offset");
_Static_assert(sizeof(struct orbit_rate) == 16, "rate size");
int main(void)
{
	struct orbit_queue q = {0};
	struct orbit_sample sample = {.abi_version = 1, .temperature_mc = -10000};
	struct orbit_rate rate = {.abi_version = 1, .period_ms = 1};
	assert(!orbit_queue_peek(&q));
	for (unsigned int i = 0; i < ORBIT_FIFO_CAPACITY; i++) {
		sample.device_sequence = i;
		assert(orbit_queue_push(&q, &sample));
	}
	/* Overflow cannot mutate a peeked head; failed copies leave it staged. */
	struct orbit_sample staged = *orbit_queue_peek(&q);
	for (unsigned int i = 0; i < 100; i++)
		assert(!orbit_queue_push(&q, &sample));
	assert(orbit_queue_peek(&q)->device_sequence == staged.device_sequence);
	assert(orbit_queue_peek(&q)->temperature_mc == -10000);
	for (unsigned int i = 0; i < ORBIT_FIFO_CAPACITY; i++) {
		assert(orbit_queue_peek(&q)->device_sequence == i);
		orbit_queue_consume(&q);
	}
	assert(q.generated == 164 && q.enqueued == 64 && q.dropped == 100);
	assert(q.delivered == 64 && !q.count);
	/* Many ring wrap cycles retain ordered identity and counter equations. */
	for (unsigned int i = 0; i < 10000; i++) {
		sample.device_sequence = i;
		assert(orbit_queue_push(&q, &sample));
		assert(orbit_queue_peek(&q)->device_sequence == i);
		orbit_queue_consume(&q);
	}
	assert(q.generated == q.enqueued + q.dropped);
	assert(q.enqueued == q.delivered + q.count);
	assert(orbit_rate_valid(&rate));
	rate.period_ms = 60000; assert(orbit_rate_valid(&rate));
	rate.period_ms = 0; assert(!orbit_rate_valid(&rate));
	rate.period_ms = 60001; assert(!orbit_rate_valid(&rate));
	rate.period_ms = 100; rate.abi_version = 2; assert(!orbit_rate_valid(&rate));
	rate.abi_version = 1; rate.reserved[1] = 1; assert(!orbit_rate_valid(&rate));
	puts("PASS: UAPI layout, shared FIFO overflow/wrap/counters, rate validation");
	return 0;
}
