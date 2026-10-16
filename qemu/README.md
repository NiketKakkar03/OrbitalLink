# OrbitalLink synthetic multisensor hub

This extends the pinned QEMU 9.2 `virt-9.2` board; it is a development board
extension, not an upstream compatibility or physical-accuracy claim. One hub
uses the shared deterministic `simulation/orbit_scenario.h` engine. Default
period 100 ms gives ten routine reports/sec in the prescribed category mix;
transition events add at most one extra record per tick. `-global
orbit-sensor-hub.seed=42` explicitly selects a reproducible radiation seed.

The actual QEMU9.2 `base_memmap` and `a15irqmap` were inspected before allocation:
MMIO `0x090c0000..0x090c0fff` follows secure GPIO and precedes virtio MMIO;
SPI10 (GIC INTID42) is unused between GED SPI9 and virtio SPI16. The patched
board reserves those entries and emits a device-tree node with compatible
`orbital,multisensor-v2` and level-high interrupt. The Linux platform driver
binds that node and exposes a separate `/dev/orbit_sensors` ABI2 interface.
ABI1 `/dev/orbit_telemetry` is unchanged.

All registers are naturally aligned 32-bit little-endian accesses:

| Offset | Register | Behavior |
|---|---|---|
| 00 | ID | `0x4f534e32` |
| 04 | ABI | 2 |
| 08 | CONTROL | bit0 timer enable, bit1 IRQ enable; starts disabled |
| 0c | STATUS | bit0 data, bit1 sticky overrun, bit2 IRQ asserted; write bit1 clears overrun |
| 10 | PERIOD_MS | 1..60000; invalid writes ignored |
| 14 | SEED | writable only while timer disabled |
| 18 | RESET | write1 discards queued hardware records, counts their loss, increments generation/reset count, restarts scenario/sequence, queues bounded reset event |
| 1c | POP | write1 consumes head and lowers IRQ when empty |
| 20 / 24 | QUEUED / CAPACITY | live hardware FIFO count /64 |
| 28 / 2c | GENERATION / RESETS | generation initially1, reset count initially0 |
| 30..3c | GENERATED / DROPPED | live low/high64-bit counters; use snapshot for coherent accounting |
| 40..7c | HEAD | latched64-byte ABI2 record, coherent until POP; empty reads return0 |
| 80 | SNAP | write1 atomically captures hardware counters/count/generation |
| 84 /88 /8c | SNAP_QUEUED /GENERATION /RESETS | immutable until next SNAP |
| 90 /94 | SNAP_GENERATED | low/high64-bit total |
| 98 /9c | SNAP_DROPPED | low/high64-bit total |
| a0 | EVENT_PENDING | bounded unpublished event reports (maximum8) |
| a4..b8 | EVENT_OBSERVATIONS /COALESCED /OVERFLOW | separate low/high64-bit observation counters |
| c0 /c4 | SNAP_CLOCK_NS | atomic virtual-clock snapshot for guest clock calibration |
| d0 | FAULT_CATEGORY | 0 clears;1..5 marks that category quality-fault and emits bounded sensor-fault event8; invalid writes ignored |
| e0..ec | DRIVER_DELIVERED /DRIVER_DROPPED | retained64-bit software accounting across driver bindings |
| f0 /f4 | DRIVER_DELIVERED_ADD /DRIVER_DROPPED_ADD | commit consumed/discarded popped records; additions exceeding unaccounted popped count are rejected |
| f8 /fc | POPPED | cumulative hardware-to-driver transfers |
| 100..114 | SNAP_POPPED /DRIVER_DELIVERED /DRIVER_DROPPED | coherent accounting snapshot |
| 120..138 | SNAP_EVENT_OBSERVATIONS /COALESCED /OVERFLOW /PENDING | atomic event metadata snapshot |

FIFO overflow drops newest, preserving the record being read across MMIO
accesses. IRQ is level-high while nonempty and IRQ-enabled; the driver drains
at most64 records per interrupt into a256-record software FIFO. The shared
capacity is320, with eight urgent slots reserved in each FIFO. Repetitive
unpublished events coalesce through the shared fixed-eight-entry accumulator;
published/claimed records never change. Pending observations are not counted as
generated reports until publication; observation/coalescing/overflow counters
remain separately visible. Driver read commits consumption only after a successful user
copy. Reset discards in both FIFOs are counted, not hidden. Combined snapshot
identities are `generated=enqueued+dropped` and
`enqueued=delivered+queued`. Software counters are64-bit; category4 health
payloads are explicitly modular snapshots.

The driver calibrates the device virtual clock to Linux `ktime_get_ns` at probe,
using an atomic hardware snapshot bracketed by two kernel-clock reads. It
translates the latched source timestamp with that fixed offset and logs the
half-bracket uncertainty. Thus ABI2 timestamps and flight ACK measurements use
the same guest monotonic domain, rather than mixing host wall time or assuming
the QEMU and guest boot epochs coincide. Category4 uptime is actual QEMU virtual
seconds, independent of the selected reporting period.

Driver unbind counts every discarded software record before releasing memory.
Cumulative delivered/software-dropped totals live in QEMU and survive module
rebind; probe recovers any previously popped but uncommitted records as explicit
loss. A new binding therefore cannot silently reset a counter epoch. ABI2
`GET_EVENTS` exposes the atomic event metadata separately from report counts.

`qemu/test_mmio.py` tests actual QEMU virtual clock/MMIO behavior, including
600 ticks/606 records, category mix, events, coherent latch, overflow, reset,
rate limits, counter snapshot, and stop control. Linux
`driver/tests/sensors_contract.c` separately tests real IRQ-driven acquisition,
ABI2 read/ioctl behavior, enable/disable/rate/reset/clear-fault and truthful state
queries, the complete scenario mix and reset generation. The matching
device-tree schema is `driver/bindings/orbital,multisensor-v2.yaml`.
Presence of test code is not a pass: retain the actual test result artifacts.

Live VM migration/snapshots of the hub are not implemented. The supported
recovery mechanism is the separately persisted flight outbox; QEMU process
restart creates a fresh emulated device generation context and a fresh flight
boot identity. The scenario is synthetic and does not implement sensor physics.
