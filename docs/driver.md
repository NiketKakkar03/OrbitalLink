# Linux driver validation

The timer-driven test backend passed its first matched-kernel guest runtime
gate on 2026-10-15. Host C contract checks alone do not establish runtime safety. Load it only in the pinned
QEMU guest, never in macOS or Docker Desktop's underlying Linux kernel.

After the M5 image has built, cross-compile against that exact kernel tree:

```sh
export ARCH=arm64
export CROSS_COMPILE=/work/guest/output/host/bin/aarch64-buildroot-linux-gnu-
kernel_version=$(python3 -c 'import json; print(json.load(open("/work/source/guest/versions.json"))["linux"]["version"])')
make -C /work/source/driver module KDIR="/work/guest/output/build/linux-${kernel_version}"
make -C /work/source/driver guest-tests CC="${CROSS_COMPILE}gcc"
```

Provision `orbit_test.ko` and `orbit-syscall-contract` into the guest, load the
module with `insmod`, and run the harness there. Validate kernel warnings,
blocking/nonblocking reads, poll wakeups, invalid and inaccessible buffers,
rate controls, overflow counters, interruption, and safe detach while a reader
holds the device. The harness contains destructive device-detach scenarios;
use only the disposable development guest. Retain serial output and the exact
kernel/module hashes. A successful native contract check is not a substitute.

M6 runtime gate passed in `build/guest-evidence/m6-run-001/result.json`: the
syscall suite, 100 load/unbind/rebind/unload cycles, clean kernel diagnostic
scan, and 100 device-origin mutual-TLS samples committed at ground. The database
checks generation, consecutive device sequences, deterministic temperatures,
3,300 mV voltage and positive monotonic timestamps. Guest shutdown also passed. M7 still requires a real emulated MMIO/IRQ
backend plus driver binding; the timer-driven test backend alone is not M7.


Validated on October 14, 2026: the macOS host ABI/FIFO contract passes, and a
separate `linux/arm64` container built and ran that same contract with strict C
warnings. The syscall harness also compiles as an AArch64 Linux ELF executable.
These checks used no guest build volume and loaded no kernel modules. They do
not establish module compilation or syscall behavior inside the pinned guest.

The test backend uses a 64-record FIFO with drop-newest overflow. This preserves
a record staged for `copy_to_user`; an inaccessible buffer never consumes it.
One open file owns the device, and an I/O mutex serializes operations on duplicated
file descriptors. Detach marks the allocation dead, stops the timer, wakes blocked
readers, and keeps the allocation alive through the last file reference. Runtime
stress must still verify those lifetime assumptions in Linux.

The guest kernel needs `CONFIG_MODULES=y`, `CONFIG_MODULE_UNLOAD=y`, sysfs, misc
character devices, and devtmpfs mounted at `/dev`. Build the external module only
against the completed kernel output and toolchain from the same pinned build.
A Buildroot external package can use the `kernel-module` infrastructure with
`ORBIT_TEST_BACKEND=1` in its module make options, plus `generic-package` to install
the syscall harness. Keep the package test-only and install its module via the
kernel-module infrastructure so kernel release paths remain correct.

The matched Linux 6.12.27 module, installed syscall harness and flight adapter
were tested together in the disposable guest. Serial evidence includes the
syscall/lifecycle output and kernel logs.
The syscall suite now includes invalid ABI/reserved inputs and inaccessible
ioctl buffers in addition to read faults. Its detach loops must pass before M6
can be called runtime validated.

Flight's explicit device mode is `--source device --device
/dev/orbit_telemetry`, alongside its normal TLS and outbox arguments. The
acquisition thread owns one nonblocking device descriptor, validates whole UAPI
records and preserves device sequence/generation/time/value fields. It fails on
detach or invalid records rather than silently switching sources. The default
source remains the fake device for native demonstrations.

The current test driver supports rate changes and counter queries. Device-mode
flight accepts only the rate command, and reports success after the ioctl
succeeds. Enable, disable, reset, clear-fault and live state-query commands are
rejected until real driver controls/state are implemented. Driver FIFO loss
counters are separate from the durable outbox's acknowledged loss reports.
Device-to-ground validation passed for this test backend. Wider fault injection,
benchmarking and real MMIO/IRQ acquisition remain later gates.

The first device telemetry run reported 177 generated records: 164 enqueued plus
13 drop-newest losses. Of the enqueued records, 100 were delivered to flight and
64 remained queued. All 100 acquired flight samples were durably acknowledged.
These counters reconcile, but do not establish lossless sustained throughput.
Rate/backpressure measurements and durable reporting of kernel FIFO losses are
M8 work; outbox loss reports currently describe flight-retention losses only.
