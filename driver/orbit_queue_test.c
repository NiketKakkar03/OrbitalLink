// SPDX-License-Identifier: GPL-2.0
#include <kunit/test.h>
#include <linux/module.h>
#include "orbit_queue.h"
static void queue_contract(struct kunit *test)
{
	struct orbit_queue *q = kunit_kzalloc(test, sizeof(*q), GFP_KERNEL);
	struct orbit_sample s = { .abi_version = 1, .temperature_mc = -10000 };
	unsigned int i;
	KUNIT_ASSERT_NOT_NULL(test, q);
	for (i = 0; i < ORBIT_FIFO_CAPACITY; i++) {
		s.device_sequence = i;
		KUNIT_EXPECT_EQ(test, orbit_queue_push(q, &s), 1);
	}
	KUNIT_EXPECT_EQ(test, orbit_queue_push(q, &s), 0);
	KUNIT_EXPECT_EQ(test, q->dropped, (__u64)1);
	for (i = 0; i < ORBIT_FIFO_CAPACITY; i++) {
		KUNIT_ASSERT_NOT_NULL(test, orbit_queue_peek(q));
		KUNIT_EXPECT_EQ(test, orbit_queue_peek(q)->device_sequence, i);
		orbit_queue_consume(q);
	}
	KUNIT_EXPECT_EQ(test, q->count, 0U);
	KUNIT_EXPECT_EQ(test, q->enqueued, q->delivered);
}
static void rate_contract(struct kunit *test)
{
	struct orbit_rate r = { .abi_version = 1, .period_ms = 100 };
	KUNIT_EXPECT_TRUE(test, orbit_rate_valid(&r));
	r.period_ms = 0;
	KUNIT_EXPECT_FALSE(test, orbit_rate_valid(&r));
	r.period_ms = 60001;
	KUNIT_EXPECT_FALSE(test, orbit_rate_valid(&r));
	r.period_ms = 1;
	r.reserved[0] = 1;
	KUNIT_EXPECT_FALSE(test, orbit_rate_valid(&r));
}
static struct kunit_case orbit_cases[] = {
	KUNIT_CASE(queue_contract), KUNIT_CASE(rate_contract), {}
};
static struct kunit_suite orbit_suite = {
	.name = "orbit-queue", .test_cases = orbit_cases
};
kunit_test_suite(orbit_suite);
MODULE_LICENSE("GPL");
