# Session 005 — durable retention and Apple Silicon gates

Status recorded 2026-10-15. This is an evidence snapshot, not a declaration that
all embedded milestones are complete.

Native flight now uses a bounded SQLite outbox, persists original sample
identities across restart, removes samples only after a matching durable ground
ACK, and keeps bounded acknowledged loss reports. Operators can query live fake
acquisition state over the authorized command channel. See
[retention policy](../retention.md) for capacity, overflow and failure limits.

The native, sanitizer, security and demo gates passed on both macOS and Linux in
[CI run 38660069732](https://github.com/NiketKakkar03/OrbitalLink/actions/runs/38660069732).
SQLite crash tests use fresh child executables, including a kill with a live
rollback journal. The memory-only fixture's shutdown check accounts for its
separate in-flight slot as well as the bounded queue.

Docker Desktop provides a Linux aarch64 builder on the Mac. The pinned GCC
cross-toolchain and Linux kernel compiled. The complete image built. Guest boot and two-boot replay passed in
`build/guest-evidence/m5-run-005/result.json`: 107 authenticated ground rows with
boot counts of seven and 100. Kernel module lifecycle tests and driver-origin
telemetry remain separate gates. See [guest setup](../guest.md).

The prepared two-boot gate uses an isolated persistent state disk, retains seven
samples on the first boot, then expects those seven plus 100 new samples on the
second boot through mutual TLS. Runtime evidence is written only after clean
QEMU shutdown. Each run preserves existing operator state and captures its own
artifacts.

The driver test backend, UAPI and syscall/lifecycle harness remain development
work until actually loaded and tested in the matched Linux guest. A custom QEMU
MMIO/IRQ device, full embedded fault/benchmark coverage and release/security
patch auditing remain later gates. No Mac firmware changes are required by the
selected Linux builder and QEMU TCG approach.

The first boot exposed reversed MMIO disk enumeration; explicit PCI disk slots
fixed root/state identity. Actual serial runs then exposed doubled carriage
returns and absent `ldd` in the minimal guest. The runner now normalizes terminal
line endings, uses the glibc loader for dependency inspection, and validates the
state label with BusyBox-compatible commands. The fifth run passed both boots.
