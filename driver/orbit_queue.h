/* SPDX-License-Identifier: GPL-2.0 */
#ifndef ORBIT_QUEUE_H
#define ORBIT_QUEUE_H
#include "include/orbit_telemetry.h"
/* Caller provides synchronization. A producer never replaces a staged head. */
struct orbit_queue {
	struct orbit_sample records[ORBIT_FIFO_CAPACITY];
	unsigned int head, count;
	__u64 generated, enqueued, dropped, delivered;
};
static inline int orbit_queue_push(struct orbit_queue *q,
				   const struct orbit_sample *sample)
{
	q->generated++;
	if (q->count == ORBIT_FIFO_CAPACITY) {
		q->dropped++;
		return 0;
	}
	q->records[(q->head + q->count) % ORBIT_FIFO_CAPACITY] = *sample;
	q->count++;
	q->enqueued++;
	return 1;
}
static inline const struct orbit_sample *orbit_queue_peek(struct orbit_queue *q)
{
	return q->count ? &q->records[q->head] : 0;
}
static inline void orbit_queue_consume(struct orbit_queue *q)
{
	if (q->count) {
		q->head = (q->head + 1) % ORBIT_FIFO_CAPACITY;
		q->count--;
		q->delivered++;
	}
}
static inline int orbit_rate_valid(const struct orbit_rate *r)
{
	return r->abi_version == ORBIT_ABI_VERSION && r->period_ms >= 1 &&
		r->period_ms <= 60000 && !r->reserved[0] && !r->reserved[1];
}
#endif
