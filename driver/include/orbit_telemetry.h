/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef ORBIT_TELEMETRY_H
#define ORBIT_TELEMETRY_H
#ifdef __linux__
#include <linux/types.h>
#include <linux/ioctl.h>
#else
/* Host ABI checks only; ioctl numbers are Linux-specific. */
#include <stdint.h>
#include <sys/ioctl.h>
typedef uint32_t __u32;
typedef int32_t __s32;
typedef uint64_t __u64;
#endif
#define ORBIT_ABI_VERSION 1U
#define ORBIT_FIFO_CAPACITY 64U
#define ORBIT_SAMPLE_TEST_SOURCE 1U
struct orbit_sample {
	__u32 abi_version;
	__u32 flags;
	__u32 device_sequence;
	__u32 device_generation;
	__u64 monotonic_ns;
	__s32 temperature_mc;
	__u32 voltage_mv;
	__u32 reserved[2];
};
struct orbit_stats {
	__u32 abi_version;
	__u32 capacity;
	__u64 generated;
	__u64 enqueued;
	__u64 dropped;
	__u64 delivered;
	__u32 queued;
	__u32 dead;
	__u32 reserved[2];
};
struct orbit_rate {
	__u32 abi_version;
	__u32 period_ms;
	__u32 reserved[2];
};
#define ORBIT_IOC_GET_STATS _IOR('O', 0, struct orbit_stats)
#define ORBIT_IOC_SET_RATE _IOW('O', 1, struct orbit_rate)
#endif
