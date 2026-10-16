# OrbitalLink: project walkthrough

Snapshot: 2026-10-15. The implemented M1–M6 work was merged into `main` at
`3b3a7f0`. Multisensor work is on `feature/multisensor-validation`; its measurements
and outstanding gates are in [sensor validation](sensor-validation.md).
This guide describes the current system and distinguishes it from the
planned final system. The [roadmap](../OrbitalLink_Roadmap.md) remains the design
plan; historical journal entries describe the state at their own dates.

## 1. What we are building

OrbitalLink is a secure embedded telemetry and command platform. A simulated
sensor produces power, thermal, orientation, health, science and event reports.
Linux exposes those
measurements through a character device. A C++ flight application retains them
locally and sends them to a Python ground station, which stores them in SQLite.
An authorized operator can send commands in the opposite direction.

“Flight” and “ground” describe software roles. We have not connected a physical
spacecraft, radio, sensor board or flight-qualified hardware. The current driver
retains a kernel timer test source for regression tests and adds a custom QEMU
sensor peripheral with real emulated registers and interrupts. Its values are
deterministic examples, not a calibrated spacecraft model.

## 2. Where the programs run

Your Mac is the development host. It runs the editor, native builds and native
integration tests. Docker Desktop provides ARM64 Linux for building and running
the embedded setup. Inside that Linux environment, QEMU uses TCG software CPU
emulation to run a separate ARM64 Linux guest.

```text
MacBook M1 Pro / macOS
├── native C++ flight + Python ground tests
└── Docker Desktop: ARM64 Linux builder
    ├── Buildroot, cross-compiler and Linux-built QEMU
    ├── Python ground station for embedded gates
    └── QEMU ARM64 Linux guest
        ├── custom QEMU multisensor hub → Linux IRQ-driven driver
        ├── /dev/orbit_sensors (ABI2); legacy /dev/orbit_telemetry (ABI1)
        ├── C++ flight service
        └── persistent SQLite outbox on a separate state disk
```

This arrangement has passed actual guest and driver gates on this Mac. It
requires no Mac firmware modification and loads no OrbitalLink kernel module
into macOS or Docker Desktop's host kernel. The guest module is loaded only
inside QEMU. TCG avoids requiring nested hardware virtualization.

Development pins are Buildroot 2025.02.18, GCC 13.4.0, glibc, Linux 6.12.27,
QEMU 9.2.0 and machine `virt-9.2`. Exact revisions and source hashes are in
[versions.json](../guest/versions.json). These are coherent tested development
pins; latest security-patch auditing and fully reproducible dependency packaging
remain release work. A pinned base image does not freeze apt repositories.

## 3. The implemented data path

```text
QEMU virtual timer → hardware FIFO → IRQ → bounded driver FIFO → read/poll
    → flight acquisition thread → committed SQLite outbox
    → flight network thread → mutual TLS 1.3
    → Python frame parser → ground SQLite commit → ACK
    → flight deletes the matching retained sample
```

The default native source is a deterministic fake device inside flight. Explicit
`--source device --device /dev/orbit_telemetry` selects the Linux driver. Device
mode never silently falls back to the fake source after an error.
`--source scenario` selects the native multisensor simulation;
`--source sensors` selects the custom peripheral through `/dev/orbit_sensors`.
Both use the shared category schedule and interaction rules.

The two flight threads separate acquisition from networking. Acquisition owns
device access and command effects; the network thread owns the TLS connection.
A network outage therefore does not require acquisition to wait for a remote
ACK, although local persistence still performs real storage I/O.

The device adapter polls in bounded intervals, reads at most 16 whole records per
batch, validates ABI/version/reserved bits, and fails explicitly on detach,
invalid records or unrecoverable syscall errors. It preserves the device's
sequence, generation, monotonic time, temperature and voltage.

## 4. What a sample means

Every sample carries:

| Field | Purpose |
|---|---|
| spacecraft ID | Identifies this system |
| flight boot ID | Random identity for one flight-service run |
| telemetry sequence | Distinguishes samples within that flight run |
| device generation | Distinguishes device resets |
| device sequence | Tracks sensor production and gaps |
| monotonic timestamp | Time since the producing system's monotonic-clock origin |
| temperature | Signed integer millidegrees Celsius |
| voltage | Unsigned integer millivolts |

The final two fields describe legacy type 1. Typed type 9 samples instead carry
a category, schema version, quality flags and eight signed integer slots. The
[sensor contract](../protocol/sensor-contract.md) specifies each category and unit.

For example, `-5250` means −5.250 °C, and `3300` means 3.300 V.
Monotonic time is useful for ordering within a run; it is not a UTC timestamp
that can be directly compared across boots. Ground separately records receipt
time.

Ground identifies a sample by `(spacecraft_id, flight_boot_id,
telemetry_sequence)`. Old retained samples keep their original identity and
payload after a restart. Newly acquired samples use the new flight boot ID.
Device identity, telemetry identity and command identity are deliberately
separate.

## 5. The three byte interfaces

The final design has three distinct interfaces:

| Interface | Current state |
|---|---|
| Network protocol | Implemented: explicit big-endian encoding, not raw C structs |
| Linux driver UAPI | Implemented: fixed-width guest-native fields and explicit ABI version |
| QEMU MMIO registers | Implemented custom ABI2 hub; installed-binary QTest checks passed |

Network frames have a 28-byte header and at most 4,096 payload bytes. The header
contains `OLNK`, version, type, lengths, sequence and reserved fields. A telemetry
payload is 44 bytes for legacy type 1 or 76 bytes for typed type 9. The parser
handles fragmented/coalesced TCP input and rejects
malformed headers, sizes, versions and reserved bits.

Current message types are telemetry sample, telemetry ACK, command session,
command request, command result, state snapshot, loss report, loss ACK and typed
sensor sample.
C++ and Python share golden byte fixtures and rejection tests. See the
[wire specification](../protocol/specification.md).

The Linux UAPI has a 40-byte sample, 56-byte statistics response and 16-byte rate
request. It exposes read/poll plus `GET_STATS` and `SET_RATE` ioctls. Layout,
reserved fields and ABI compatibility are checked. These bytes are converted to
the network representation rather than transmitted as a raw kernel struct.
The separate ABI2 UAPI uses 64-byte typed records and adds reset, clock calibration
and event-accounting ioctls; legacy layouts remain supported.

## 6. What “delivered” guarantees

Writing bytes to a socket does not prove ground stored a measurement. Our ACK
contract is stronger:

1. Flight commits the sample to its local outbox.
2. Ground receives and validates the complete frame.
3. Ground commits it to its database.
4. Ground sends an ACK for the complete sample identity.
5. Flight deletes only the matching retained record.

If an ACK disappears, flight resends. Ground's primary key prevents a second
stored row for an already stored identity, and ground ACKs that durable duplicate.
This is retryable delivery with duplicate suppression for retained samples; it
is not unlimited or universally lossless delivery.

Structurally valid but physically implausible telemetry is stored with a validity
flag/reason for inspection. Malformed protocol frames are rejected. Ground also
stores command results, events and loss reports. Unsigned 64-bit fields use
fixed-width hexadecimal text where needed to avoid SQLite's signed-integer limit.

## 7. Why storage is bounded, and what fills first

There are separate loss boundaries:

| Boundary | Limit/policy | Meaning |
|---|---|---|
| Legacy driver FIFO | 64 records, drop newest | Preserve records already queued for the reader |
| ABI2 hardware/software FIFOs | 64 + 256 records, urgent reserves | Bound the custom sensor pipeline and count losses |
| Durable flight outbox | Default 64 MiB budget, evict oldest unclaimed | Preserve recent telemetry during an extended outage |
| Loss metadata | Bounded detail plus aggregate counts | Never make error reporting itself unbounded |
| Ground database | No application-level archive/size cap implemented | Deployment needs its own disk/archive policy |

Think of the outbox as a box of unsent messages. A larger box lets us wait longer
for connectivity, but it eventually fills. The policy then decides which old
messages to remove and records the loss. It cannot promise infinite retention.

The default outbox budget conservatively permits 87,168 records, approximately
2 hours 25 minutes at 10 Hz when empty. This is a configured capacity estimate,
not a measured outage guarantee. Database pages, live rollback journal, schema,
counters and loss metadata all consume space. `--capacity` can impose a smaller
record limit, and `--outbox-bytes` accepts 256 KiB through 1 GiB.

Each in-flight record awaiting its ACK is protected from eviction. The bounded
send window accepts 1–64 records; the multisensor profile uses 32. Optional
priority scheduling reserves urgent capacity and fairly services event,
operational and science queues; rejected incoming records are also counted.
Extremely small capacity can stop acquisition in the default legacy mode;
capacity one cannot always admit another
sample. Startup refuses a limit reduction below the existing backlog. One process
owns an outbox through an exclusive lock.

SQLite now uses DELETE journal mode, synchronous EXTRA and macOS fullfsync. EXTRA
adds the directory synchronization needed after journal removal; an actual VM
power-cut test exposed a lost last commit under the earlier FULL setting. A bounded
set of retry transactions handles page-cap exhaustion; unresolved storage errors
fail explicitly instead of reporting success or silently losing committed state.
The accounting equation across restarts is:

```text
durable_generated = durable_acknowledged + durable_dropped + durable_retained
```

Flight persists losses before reporting them. Claimed report IDs/payloads remain
immutable; ground commits and deduplicates each report before its loss ACK.
Detailed ranges eventually collapse into bounded summaries with exact omitted
counts. Current durable loss reports cover flight-retention loss. Kernel FIFO
loss is exposed through driver counters; durable end-to-end reporting of those
kernel losses remains work to complete.

Disk destruction, corruption, dishonest synchronization and uninterruptible OS
I/O are outside the guarantee. A SQLite page-cap test is not a physical-full-disk
test. The shutdown deadline assumes responsive storage. See
[retention details](retention.md).

## 8. Security and commands

Normal flight and ground use mutual TLS 1.3. Both peers validate certificate
chains and validity. Flight verifies the configured ground DNS SAN; ground
requires and authorizes its configured client SAN. A certificate from a trusted
CA is insufficient if the identity is not authorized. Production flight has no
plaintext fallback; plaintext is an explicitly compiled loopback test fixture.

The local operator connects to a Unix control socket in a user-owned 0700
directory; the socket is 0600. Flight additionally requires an explicit command
role bound to the authenticated ground identity. Telemetry permission does not
implicitly grant command permission.

Each connection creates a fresh random command session. Commands carry an
increasing sequence, command ID and a flight-monotonic deadline at most five
seconds ahead. Admission validates them, and acquisition checks expiry again
before execution. Queued work/results are bounded to 16. Reconnect invalidates
the old session and queued work.

| Command | Native fake source | Linux device source |
|---|---|---|
| enable / disable | Implemented | Implemented through real ioctl/register effects |
| set-period, 1–60000 ms | Implemented | Implemented through real ioctl |
| reset | Implemented | Implemented through real ioctl/register effects |
| clear-fault | Implemented | Implemented through real ioctl/register effects |
| query-state | Implemented, read-only | Reports actual enable/rate/generation/fault/reset state through ioctl |

A device rate command reports applied only after its ioctl succeeds. An uncertain
failure is not represented as simulated success. If a connection disappears
around a command, the effect may have happened even if its result was lost.
Ground reports `outcome_unknown` and never automatically replays the request.
Native query-state can help an operator inspect current fake-device state;
device-mode state-query support remains a real limitation.

Development certificates are disposable, two-day credentials. Production key
provisioning, rotation, revocation, compromised-host defenses and hardware
attestation have not been delivered. The threat model trusts the host OS,
operator account, configured CA and key storage. See [security setup](security.md).

## 9. What the driver already proves

The timer-driven platform-driver test backend implements a bounded FIFO,
single-open ownership, whole-record reads, nonblocking `EAGAIN`, blocking wakeups,
poll readiness, rate control and statistics. Inaccessible user buffers do not
consume their staged record. Reads serialize user-copy work outside spinlocks.
Reference counting keeps allocations alive through a reader's last close.
Detach stops the timer, marks the device dead and wakes readers with terminal
errors. The syscall harness covers interruption, invalid ioctls, invalid ABI,
reserved fields, bad buffers, overflow, seek rejection and detach behavior.

The real M6 run passed the syscall suite and 100 load/unbind/rebind/unload cycles,
a clean kernel diagnostic scan, and 100 driver-origin samples delivered through
mutual TLS into ground SQLite. Stored fields matched generation 1, consecutive
device sequences, the kernel temperature formula, 3,300 mV and positive timestamps.

Measured counters were 177 generated = 164 enqueued + 13 dropped, and 164 enqueued
= 100 delivered + 64 still queued. All 100 acquired flight samples were ACKed.
This is explicit bounded-loss accounting, not evidence of zero driver drops or
a sustained-throughput benchmark. See [driver details](driver.md).

## 10. Milestones and evidence

| Milestone | Current result |
|---|---|
| M0 environment | Native loop and ARM64 Linux builder operational |
| M1 protocol | C++/Python codecs, fixtures, framing and rejection tests implemented |
| M2 telemetry/storage | Native path, durable ground commit/ACK and duplicate suppression tested |
| M3 recovery | Bounded concurrency, retry, outage/shutdown behavior; now durable outbox and loss ACKs |
| M4 security/control | Mutual TLS, identity authorization, native commands and replay/deadline policy tested |
| M5 guest | Passed two real boots: 7 retained samples + 100 new = 107 ground rows across two boot IDs |
| M6 driver test source | Passed syscall/lifecycle gate and 100 driver-origin TLS samples |
| M7 custom peripheral | Complete: ABI2 hub/IRQ driver, binding schema, controls/state UAPI, QTest, driver rebind contract, EXTRA-durable baseline and abrupt-power-cut recovery passed |
| M8 fault/measurement | Native and embedded 600-second baselines passed; overload/window/recovery tests expanded; embedded 100 Hz limit measured; full fault matrix pending |
| M9 release | Clean source tests and clean-checkout smoke passed; release, patch/license and operational work pending |

The native suite includes protocol/storage, recovery, typed sensors, bounded
windows and overload tests, C++ checks and driver host ABI/FIFO checks.
ASan/UBSan and macOS TSan checks are part of validation. CI runs Ubuntu and macOS;
it does not yet rebuild and execute the entire guest/module matrix remotely.

Validated implementation commit `3b3a7f0` passed
[CI run 38752529854](https://github.com/NiketKakkar03/OrbitalLink/actions/runs/38752529854).
The final multisensor implementation `66c3d6d` passed
[CI run 38846899940](https://github.com/NiketKakkar03/OrbitalLink/actions/runs/38846899940)
on Ubuntu and macOS. A separately identified embedded image passed an
EXTRA-durable 600-second baseline with 6,060 reports, zero source/outbox drops
and source-to-ACK p95 at most 389 ms. Three abrupt-power-cut cycles preserved and
replayed every committed typed record. See
[sensor validation](sensor-validation.md) for exact tested-image identities and
the separate native and embedded results.
Local embedded evidence is retained, ignored by Git, at:

- `build/guest-evidence/m5-run-005/`: result, manifest and both serial runs.
- `build/guest-evidence/m6-run-001/`: result, manifest, executable hashes, serial
  output and extracted syscall/lifecycle/kernel artifacts.
- `build/guest-evidence/m7-baseline-001/`: custom-device contract, 600-second
  typed run, clock/resource measurements and image manifest.
- `build/guest-evidence/m7-extra-baseline-001/`: final EXTRA-durable 600-second
  typed run, raw serial/resource data, ground database and exact image manifest.
- `build/guest-evidence/m7-controls-001/`: rebuilt ARM64 image, ABI2 control/state
  syscall contract and short authenticated sensor-path regression.

These directories contain no copied private keys. They are local evidence,
not a published release archive. Earlier failed attempts were preserved in the
Linux build volume. A passing gate means those scenarios passed on that build;
it does not establish correctness for every fault or deployment.

## 11. Remaining design to implement

The custom hub now implements coherent latched samples, little-endian MMIO,
a level-triggered IRQ, masking, overrun/reset accounting, a deterministic
virtual timer and machine-generated device-tree wiring. Its maintained patch
uses MMIO 0x090c0000 and GIC SPI 10 (INTID 42) on pinned virt-9.2. The driver binds
to these real emulated resources. Complete roadmap acceptance, negative probe
and binding-schema coverage should be checked separately from a passing data path.

The driver control/state UAPI now supports real enable/disable/reset/clear-fault
and truthful query-state. The existing authorization/replay/deadline rules feed
these ioctl/register effects through the acquisition-owned command dispatcher.

M8 still needs abrupt guest-power-loss tests, physical-disk/storage fault
scenarios, the full embedded network/device fault matrix, kernel-loss reporting,
rate/backpressure calibration, throughput/latency/resource measurements and
repeated stress evidence. M9 needs clean-checkout reproduction, patch/license
review, release artifacts, deployment/credential operations, ground archive policy
and a documented demonstration. Yocto is a later extension after the first
Buildroot-based release, not a completed component.

## 12. How to explore and run it

Read this guide, then follow one sample through these entry points:

| Area | Start here |
|---|---|
| Flight threads and lifecycle | `flight/flight_service.cpp` |
| Device adapter | `flight/device_source.hpp` |
| Durable outbox | `flight/durable_outbox.hpp`, `flight/durable_outbox.cpp` |
| Command policy | `flight/command_policy.hpp` |
| Wire encoding | `flight/orbit_protocol.hpp`, `ground/orbit_protocol.py` |
| Ground receiver and CLI | `ground/orbit_ground.py`, `ground/orbit_commands.py` |
| Ground database | `ground/orbit_storage.py` |
| Kernel test driver and ABI | `driver/orbit_test.c`, `driver/include/orbit_telemetry.h` |
| Guest build and gates | `guest/guest.py`, `guest/smoke.py`, `guest/run_gate.py` |
| Evidence runner and CI | `integration/run_checks.py`, `.github/workflows/native.yml` |

From the repository root:

```sh
make doctor
make check
make demo-native
make test-sanitizers
make test-tsan
make test-security-sanitizers
```

Use [environment setup](environment.md), [guest reproduction](guest.md),
[driver testing](driver.md) and [validation evidence](validation.md) for prerequisites
and embedded commands. Large builds and state live in the named Linux volume
`orbit-buildroot-arm64`; do not delete it casually. Existing state disks are
preserved. The normal root disk uses a snapshot, while the separate ext4 state
disk deliberately persists the outbox across VM boots.

The project has authenticated durable telemetry from native and emulated-device
sources. See [sensor validation](sensor-validation.md) for the current measurement
results and the remaining fault, performance and release gates.
