/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef ORBIT_SENSORS_H
#define ORBIT_SENSORS_H
#include "orbit_telemetry.h"
#ifndef __linux__
typedef int64_t __s64;
#endif
#define ORBIT_SENSOR_ABI_VERSION 2U
#define ORBIT_SENSOR_FIFO_CAPACITY 256U
struct orbit_sensor_sample {
	__u32 abi_version, category, generation, sequence;
	__u64 monotonic_ns;
	__u32 quality, schema;
	__s32 values[8];
};
/* orbit_stats layout preserved, abi_version=2; queued covers both pipeline
 * FIFOs. reserved[0]=generation, reserved[1]=reset_count. Exact source totals:
 * generated=enqueued+dropped; enqueued=delivered+queued. */
#define ORBIT_SENSOR_GET_STATS _IOR('S', 0, struct orbit_stats)
#define ORBIT_SENSOR_SET_RATE _IOW('S', 1, struct orbit_rate)
#define ORBIT_SENSOR_RESET _IO('S', 2)
struct orbit_sensor_clock { __s64 offset_ns; __u64 uncertainty_ns; };
#define ORBIT_SENSOR_GET_CLOCK _IOR('S', 3, struct orbit_sensor_clock)
/* Fresh comparison sample for drift evidence; does not change mapping. */
#define ORBIT_SENSOR_CLOCK_CURRENT _IOR('S', 4, struct orbit_sensor_clock)
/* SET_RATE takes abi_version=2, period_ms in1..60000, reserved zero. */
#endif
