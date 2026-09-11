# OrbitalLink: learning and implementation roadmap

This guide turns the supplied **QEMU-Based Secure Embedded Linux Telemetry Platform** brief into a sequence you can follow while learning, coding, and testing. It is a proposed implementation plan, not a report of completed software. The original brief remains the scope reference. No existing project code was reviewed or executed for this guide.

The first release ends with a custom QEMU peripheral, a Linux platform driver, a C++ flight service, a Python ground station, mutual TLS, bounded recovery, reproducible tests, and a documented demonstration. Yocto comes afterward.

## 1. Understand the system before building it

Think of the project as five programs or components passing one measurement across boundaries:

| Component | Where it runs | What it owns | What it must not do |
|---|---|---|---|
| QEMU device, written in C | Development host, inside the QEMU process | Simulated registers, sample timer, virtual IRQ | Use host wall time as a deterministic test clock |
| Linux driver, written in C | ARM64 guest kernel | MMIO access, interrupt handling, bounded record FIFO | Perform TLS, SQLite operations, or sleeping work in an ISR |
| Flight service, written in C++ | ARM64 guest user space | Sample validation, buffering, TLS, command policy | Treat a successful socket write as durable ground storage |
| Ground station, written in Python | Development host | Peer authorization, decoding, durable storage, operator commands | Accept arbitrary trusted clients without identity checks |
| Test harness | Development host | Process lifecycle, fault injection, assertions, artifacts | Decide success from a log line alone |

```text
data:
virtual timer → register snapshot → IRQ → driver FIFO → read/poll
             → C++ queue → TLS/TCP → Python decoder → SQLite → telemetry ACK

control:
local operator → ground command → TLS/TCP → flight authorization
               → ioctl → register write → device state → command result
```

**MMIO** means a CPU load or store to a mapped address accesses a device register. **An IRQ** tells the CPU a device needs attention. **Device tree** describes the device's address and interrupt wiring. **A driver** turns those hardware details into an operating-system interface. **An ABI** is the byte layout and syscall behavior shared by separately compiled programs. **TLS** authenticates peers and encrypts a byte stream; your protocol still defines message boundaries and command meaning.

Follow one example throughout development: sample sequence 42, temperature 25,125 m°C, voltage 3,300 mV. At every boundary, be able to show its identity and values. Include negative temperatures in tests so signedness errors do not hide.

## 2. Working rhythm and realistic scope

For each feature, repeat this cycle:

1. Read only the concepts needed for the next small change.
2. Write down the observable behavior and one failure case.
3. Implement the smallest behavior that satisfies that contract.
4. Run focused tests, then the existing integration smoke test.
5. Inspect evidence: decoded fields, database rows, counters, or kernel output.
6. Explain the result in your own words in a short engineering journal entry.
7. Commit the feature with its test and any changed interface documentation.

Use a journal template: **question → prediction → change → test command → observed result → explanation → next step**. When a test fails, record the first boundary where actual data differs from expected data.

The original eight-week schedule assumes considerable familiarity. For someone learning kernel and QEMU work, use the following **planning estimates**, not deadlines or measured productivity claims:

| Milestone | Focused hours | Result |
|---|---:|---|
| M0: environment and concepts | 8–12 | Reproducible native build and test loop |
| M1: bytes and protocol | 12–18 | C++/Python agree on exact messages |
| M2: local telemetry path | 12–18 | Deterministic samples stored in SQLite |
| M3: bounded concurrency and recovery | 15–25 | Outages and shutdown behave predictably |
| M4: TLS and commands | 18–30 | Authenticated transport and replay policy |
| M5: ARM64 Linux guest | 12–20 | Flight service runs inside QEMU |
| M6: driver with test source | 22–35 | Tested read/poll/ioctl boundary |
| M7: custom QEMU peripheral | 25–40 | Real emulated MMIO and IRQ data path |
| M8: fault validation and measurement | 18–30 | Repeatable failure and benchmark evidence |
| M9: release and reproduction | 8–12 | Clean-checkout demo and portfolio release |
| Total, excluding Yocto | 150–240 | Approximately 13–24 weeks at 10–12 hours/week |

Timebox research tangents. Get one sample across a boundary before adding throughput, elaborate abstractions, or presentation polish. Keep a working native demo while integrating the guest.

## 3. Decisions to settle before their dependent code

These are recommended starting choices. Record them in short architecture decision records and change them deliberately when tests expose a better choice.

### 3.1 Separate the three binary interfaces

Do not reuse a C struct indiscriminately across hardware, syscalls, and the network.

| Boundary | Recommended contract |
|---|---|
| MMIO | 32-bit little-endian registers; aligned accesses; explicit invalid-access behavior |
| Driver UAPI | Fixed-width fields, explicit padding/reserved fields, documented size/alignment, ABI version; guest-native byte order stated explicitly |
| Network | Explicit field-by-field encoding in network byte order; no raw struct serialization |

Use compile-time size/offset assertions for the UAPI. Initialize every byte returned to user space. Fixed-width types alone do not eliminate padding. Packing everything is not a substitute for designing alignment.

### 3.2 Make a sample coherent

The proposed registers can otherwise produce temperature from sample N and voltage from sample N+1. For v1, use a **single latched sample**:

- When no unread sample exists, the timer fills the data registers and sets sample-ready.
- While ready remains set, the data registers remain stable.
- New timer events increment a generated-event sequence and an overrun counter, but do not overwrite the unread snapshot.
- The driver reads the complete snapshot and acknowledges the sample-ready reason afterward.
- Acknowledgement releases the latch; the next timer event can publish another sample.

This intentionally loses samples when acquisition cannot keep up. It is simple, bounded, and testable. Document that acknowledging does not reconstruct discarded measurements. Give each subsequent published sample the corresponding generated-event sequence so gaps expose losses.

Define the interrupt as level-triggered: asserted whenever `(IRQ_STATUS & IRQ_MASK) != 0`. Recompute the line after timer events, mask writes, ACKs, fault changes, and reset. Acknowledge only reasons the driver handled. Define how persistent fault conditions reassert, to avoid accidental interrupt storms.

Add read-only overrun and reset-generation observability if needed; update the register version and specification before freezing v1. Do not invent an MMIO base or IRQ number without inspecting the selected QEMU machine source.

### 3.3 Distinguish identities and sequences

Use different fields for different purposes:

- `device_generation + device_sequence`: resets and gaps at the sensor boundary.
- `flight_boot_id + telemetry_sequence`: stable telemetry identity during one service lifetime.
- `command_session_id + command_sequence`: command authorization and replay context.
- `command_id`: correlation with an acknowledgement or an uncertain outcome.

Specify 32-bit device sequence wrap as modular progression; reset changes generation. Use 64-bit application sequences and terminate/renew their session before overflow. A frozen sequence is a fault, not necessarily a retransmission. A retransmission preserves its original telemetry identity and payload.

### 3.4 Define what “delivered” means

Add a `TELEMETRY_ACK` message to the brief's message list. A successful TLS write only means bytes were accepted by a transport layer. Use this initial delivery contract:

1. Ground validates a sample and commits it to SQLite.
2. Ground acknowledges its telemetry identity after commit.
3. Flight removes the corresponding retained sample only after that ACK.
4. If the connection fails, flight may resend retained samples.
5. A database uniqueness constraint on spacecraft identity, boot ID, and telemetry sequence prevents duplicate rows; duplicate receipts still receive ACKs.

This gives retryable delivery for samples still retained in memory, not unlimited durability. Buffer overflow can discard unacknowledged samples. A flight-process restart loses its in-memory backlog. State both limitations explicitly. Start with individual ACKs and a bounded send window; optimize only after measurement.

Use a separate bounded mechanism for gap notices, coalescing ranges where possible. If arbitrary loss ranges exceed capacity, emit a bounded summary indicating incomplete detail. Do not let the exception-reporting queue become unbounded.

### 3.5 Define command safety across reconnects

For v1, use a fresh unpredictable command session established over each authenticated connection. Bind it to the authorized peer and current flight boot. Each request carries that session, a strictly increasing command sequence, and a bounded validity deadline based on flight monotonic time advertised for the session.

Reject wrong-session, duplicate, expired, malformed, and unauthorized requests before touching the driver. On reconnect or service restart, invalidate the old session and never automatically replay pending commands. Expiration must be checked at execution time too, if a command waits in a queue.

ACK loss creates an uncertain outcome: the device may have acted. Report `outcome_unknown`; let the operator query current state. Do not claim exactly-once commands across crashes. Within a live session, use a bounded result cache if duplicate-result reporting is desired, but never reapply the effect.

For a single local operator, define “signed-in CLI” as the host OS user accessing a permission-restricted local control socket. Bind the socket to the ground process, avoid a new password system, and map the verified ground certificate to an explicit command role at the flight service. TLS authentication alone is not command authorization.

### 3.6 Choose bounded defaults

Start at 10 samples/second, kernel FIFO capacity 256 records, flight retention capacity 1,024 records, maximum wire payload 4,096 bytes, and command work queue capacity 16. These are proposed test defaults, not demonstrated performance limits.

At 10 samples/second, an empty 1,024-record buffer holds about 102.4 seconds before overflow, assuming no ACKs and no earlier backlog. A 30-second outage generates about 300 samples. Account separately for encoded frames, in-flight records, thread stacks, TLS buffers, and SQLite behavior when reporting memory.

Use drop-newest for the kernel FIFO to preserve already queued records and simplify user-copy handling; use drop-oldest for the flight backlog to preserve recent telemetry. Count both. Never evict bytes from the middle of a frame already being written: keep a bounded in-flight frame buffer and finish it or close the connection.

## 4. M0 — Establish the environment and learning loop

**Learn:** compilation versus linking, processes versus threads, file descriptors, Linux user/kernel separation, native versus cross-compilation.

**Build in order:**

1. Follow the brief's Windows/WSL setup if Windows remains the intended machine. Verify WSL 2 and put builds under the Linux home directory. The current workspace is macOS; do not run Windows setup commands there. Linux can also be a dedicated development VM.
2. Record OS, CPU, RAM, disk space, compiler, CMake, Python, and Git versions in an environment report.
3. Create only `flight/`, `ground/`, `protocol/`, `docs/`, and basic CI initially.
4. Build a C++ executable and run one meaningful test of a small telemetry value function.
5. Create a Python virtual environment, import the package, and run a corresponding test.
6. Add formatting and warnings-as-errors for project-owned C/C++, and exclude generated/vendor code from that rule.
7. Ignore build outputs, virtual environments, logs, images, generated certificates, and private keys.

**Tests and completion gate:** a fresh shell can configure, build, and run both test suites using documented commands. Deliberately break a test once and verify local and CI failure status. No generated credential or disk image is tracked.

**Explain before continuing:** where does your executable run, and why will an ARM64 executable not run directly as an ordinary x86 executable?

## 5. M1 — Build the byte contract first

**Learn:** signed integers, endian conversion, length framing, integer-overflow checks, incremental parsing, and TCP stream semantics.

**Build in order:**

1. Write `protocol/specification.md` with the brief's 28-byte fixed header, exact offsets, field byte order, allowed flags, and payload bounds.
2. Define sample units and ranges, telemetry identity, timestamp meaning, and which fields belong in each message payload.
3. Implement pure C++ encode/decode functions with no sockets or device access.
4. Implement Python equivalents using explicit standard byte order.
5. Commit fixtures containing hex bytes, expected fields, and expected rejection reason.
6. Add an incremental parser that accepts arbitrary chunks and returns zero or more complete frames.
7. Choose a fail-closed v1 rule: invalid header/version/flags/length closes the connection. Do not scan attacker-controlled payloads indefinitely for the next magic value.

**Tests:** negative and boundary temperatures; empty/maximum/oversized payloads; every message type; every possible split point through a valid frame; multiple concatenated frames; EOF during header/payload; reserved bits; arithmetic overflow; unsupported version/type; fixed memory bound on incomplete input.

**Completion gate:** C++ and Python agree on the same valid fixtures and reject the same invalid ones. A manually calculated sample fixture matches both implementations. This avoids two implementations sharing the same mistaken assumption.

**Debug:** if Python reads an absurd number, inspect the exact bytes and signedness before changing sensor logic. If failures occur only over sockets later, suspect framing or partial writes first.

**Explain:** why can one `recv()` contain half a message or several messages?

## 6. M2 — Complete a native telemetry path

**Learn:** deterministic generation, socket lifecycle, database transactions, constraints, transport validation versus sensor validity.

**Build in order:**

1. Implement a deterministic fake source behind a small `SampleSource` interface. Fix the seed and sample formula.
2. Send framed samples from the native C++ application to a Python receiver on loopback. Any plaintext transport here is a test-stage facility that must be removed from the release runtime in M4.
3. Validate structure before constructing a sample object; classify physical range violations separately.
4. Store structurally valid samples, including physically invalid sensor readings, with a validity flag and reason. Reject malformed protocol messages instead of inserting them as telemetry.
5. Add tables for telemetry, bounded event details, and command results. Include receive time and a unique telemetry identity constraint.
6. Implement ACK-after-commit behavior and duplicate receipt handling.
7. Add CLI `status`, `tail`, and result export. Keep the interface textual.

**Tests:** send exactly 10,000 predetermined samples and compare all stored identities and values to the expected generator output; resend a subset and assert no duplicate rows; force a database error and assert no success ACK; send malformed frames and verify no telemetry insertion.

**Completion gate:** one automated native test starts both processes, verifies database contents, and stops both even on failure.

**Debug:** separate “connected,” “decoded,” “validated,” “committed,” and “acknowledged” events. A connected socket is not proof of useful telemetry.

**Explain:** how does a sensor fault differ from a corrupt or malformed message?

## 7. M3 — Add bounded concurrency and outage recovery

**Learn:** ownership, mutexes, condition variables, bounded queues, cancellation, backpressure, monotonic time, nonblocking I/O.

**Build in order:**

1. Use an acquisition thread and one network thread. The network thread exclusively owns its socket and TLS object once TLS is introduced.
2. Let the acquisition thread own the device operations too. It can later wait on the driver fd and a command/shutdown wake fd, avoiding cross-thread device-state ownership.
3. Add the fixed-capacity retention queue, counters, and overflow/gap policy from Section 3.
4. Implement connection states: disconnected, connecting, authenticating, active, backoff, stopping.
5. Use nonblocking operations and bounded deadlines so a stalled peer cannot prevent shutdown or command handling.
6. Inject clock and retry-randomness providers into policy tests. Start with backoff of 250 ms, doubling to a 5-second cap, with bounded jitter and reset after a stable connection.
7. Implement a bounded number of frames processed per network iteration so backlog upload cannot starve incoming commands.
8. On termination, wake waits, stop acquisition, apply a documented bounded drain deadline, count discarded remainder, close descriptors, and join threads.

**Tests:** capacity 4 with 6 samples gives the exact documented survivors and drop count; peer stops reading; connection ends mid-frame; ACK is lost after database commit; outages below and above retention capacity; continuous reconnect failures; stop during each state; stop with a full queue. Run ThreadSanitizer separately from AddressSanitizer/UndefinedBehaviorSanitizer where supported.

**Completion gate:** a 30-second outage at the configured default rate recovers with zero sample loss when capacity is available; a deliberately longer outage produces exactly accounted losses. Retries do not duplicate database rows. Every tested shutdown finishes within the selected deadline, initially 3 seconds.

**Explain:** what can be lost on process restart, and what happens if the receiver remains unavailable indefinitely?

## 8. M4 — Secure transport and implement commands

**Learn:** CA trust, certificate identity, mutual TLS, authentication versus authorization, replay versus retransmission, and uncertain distributed outcomes.

**Build in order:**

1. Write a short threat model identifying protected data/actions, trusted host components, trust anchors, and excluded threats from the original brief.
2. Generate disposable CA/client/server certificates outside tracked files; set restrictive key permissions.
3. Configure TLS 1.3, certificate-chain validation, expected server identity, required client certificate, and a client-identity allowlist.
4. Integrate nonblocking OpenSSL handling with the network thread. Correctly handle retry requirements and partial progress. Server-name indication is not hostname verification; configure identity verification explicitly. See [OpenSSL peer-name verification](https://docs.openssl.org/3.6/man3/SSL_set1_host/).
5. Remove plaintext runtime fallback. Keep parser tests independent of transport.
6. Add fresh command-session establishment and request fields from Section 3.5.
7. Implement command validation in this order: frame shape → peer role → session → sequence → deadline → arguments → bounded dispatch. Reject queue saturation explicitly.
8. Start with enable, disable, set-period, reset, and clear-fault against the fake source. Define command completion as the device operation being applied, not merely enqueued.
9. Validate arguments again in the driver when it arrives; network checks do not replace kernel boundary checks.

**Tests:** valid peers; missing client certificate; wrong CA; expired and not-yet-valid certificates; wrong server identity; trusted but unauthorized client; plaintext attempt; invalid command arguments; duplicates; old-session request after reconnect and restart; expired request waiting in the queue; lost result ACK; full command queue.

For each rejected command, assert both the rejection and **no device-effect counter change**. Logs alone are insufficient evidence.

**Completion gate:** only authorized peers exchange data, every accepted command produces one observable effect within the live-session contract, and retries never silently repeat an uncertain reset.

**Explain:** why does TLS not automatically prevent an application from executing the same command twice?

## 9. M5 — Run the service in an ARM64 Linux guest

**Learn:** QEMU system emulation, kernel/rootfs roles, init, serial console, virtual networking, target sysroots, and dynamic linking.

**Build in order:**

1. Pin a QEMU release/source revision, Linux revision, kernel configuration, toolchain, and rootfs recipe/checksums in a versions manifest. Select supported versions when implementing; no unverified “latest” version is prescribed here.
2. Boot an unmodified ARM64 `virt` machine first using software emulation as the portable baseline. Do not assume ARM acceleration is available on an x86 WSL host.
3. Use a small scripted Linux rootfs, such as a minimal Buildroot-generated development image, with the required C++ runtime, OpenSSL, and diagnostics. This is image plumbing, not an extra portfolio objective. Match the cross-toolchain/sysroot to the image.
4. Cross-compile the flight service with a CMake toolchain file. Do not link against the host's `libssl-dev` by accident: an x86 OpenSSL library cannot satisfy an ARM64 target.
5. Enable serial console and the selected virtio network/storage drivers in the kernel. Verify guest boot and network connectivity separately.
6. Run the fake-source flight service inside the guest and ground on the host. With default QEMU user networking, test the usual host alias `10.0.2.2`; keep this configurable and verify it in the actual setup. Port forwarding is for host-initiated guest access, not normally needed for the outbound telemetry client.
7. Configure certificate identity independently of the connection address: choose a verifiable DNS identity or an IP SAN that matches the actual verification method.
8. Add serial or SSH readiness detection with a timeout and diagnostic capture.

QEMU's ARM `virt` board generates a device tree; its machine version and actual resources matter for the later custom peripheral. See the [official ARM virt documentation](https://www.qemu.org/docs/master/system/arm/virt).

**Tests and gate:** a clean build boots the guest and delivers the same known sample set over mutual TLS. Record `uname`, executable architecture, linked libraries, image identity, and software revisions. Missing-library and wrong-architecture failures should be diagnosable from retained logs.

**Explain:** which binary emulates the computer, which file is its kernel, and which filesystem contains your service?

## 10. M6 — Develop the Linux user/kernel boundary

**Learn:** misc devices, platform devices, wait queues, IRQ-safe locking, user-copy faults, file lifetime, module lifetime, and UAPI compatibility.

Use a test-only source to isolate driver behavior before QEMU device integration. Keep it behind a build option or a separate test backend. A test generator can exercise the queue/ABI; it cannot prove MMIO, device-tree, or interrupt correctness.

**Build in order:**

1. Build the exact guest kernel and external module together; keep their version/configuration artifacts matched.
2. Define a fixed-size versioned sample record and ioctl structures, including flags, sequence, generation, and timestamp semantics. Use kernel UAPI types and explicit reserved fields.
3. Add a misc device with an explicit single-open policy for v1; reject a second opener with `EBUSY`. This avoids accidental work-sharing between readers.
4. Add a bounded FIFO and a test-only periodic producer. Share the enqueue helper with the eventual IRQ path.
5. Implement the read contract below and test it before adding more features.
6. Implement `poll()` by registering the wait queue before checking readiness under the correct synchronization.
7. Add read-only counters and validated control ioctls.
8. Implement detach and open-file lifetime correctly, then replace the flight fake source with a driver adapter.

### Read/poll/ioctl contract

| Condition | Proposed behavior |
|---|---|
| `read` count zero | Return 0 |
| Nonzero count smaller than one record | `EINVAL` |
| Larger count | Return a whole number of records; never a partial record |
| Empty queue, nonblocking fd | `EAGAIN` |
| Empty queue, blocking fd | Wait interruptibly |
| Signal while waiting, no bytes transferred | Follow normal kernel restart/interruption semantics |
| Invalid user destination | `EFAULT` if nothing transferred; preserve undelivered record |
| Record available | `POLLIN`/`POLLRDNORM` |
| Device detached | Wake waiters, report `POLLHUP`/`POLLERR`; reads use documented `ENODEV` policy |
| Unknown ioctl | `ENOTTY` |
| Invalid command argument | `EINVAL`, without a device write |

Never call `copy_to_user()` while holding a spinlock. With a single reader and drop-newest producer policy, serialize reads with a mutex, peek one record under the FIFO spinlock, release the spinlock for copying, and consume only after successful copying. Ensure removal does not invalidate this staged operation. Specify short-success behavior if a later record copy fails.

### Lifetime and locking

- A spinlock protects FIFO and shared state accessed by timer/IRQ context; hold it briefly.
- A mutex serializes sleeping user-context operations. Document lock ordering and enforce it consistently.
- Initialize all IRQ-visible state before enabling interrupts.
- On detach, prevent new opens, mark the device dead, disable the producer/interrupt source, synchronize active callbacks, wake waiters, and keep any file-visible allocation alive until the last reference closes.
- Set module ownership for file operations. Module unload with open fds may be refused; platform-device unbind can still happen with an fd open and must be tested separately.
- Managed resource cleanup helps probe unwinding but does not by itself solve open-file lifetime. Consult the [Linux devres lifecycle documentation](https://docs.kernel.org/driver-api/driver-model/devres.html) against the pinned kernel.

**Tests:** blocking wakeup, nonblocking empty read, poll before/after enqueue, undersized read, multiple-record read, invalid user pointer, FIFO overflow, concurrent open rejection, interrupted wait, invalid ioctl, detach with a blocked reader, close after detach, and repeated load/use/close/unload cycles. Use KUnit for project-owned state/validation logic and guest integration for actual syscalls. Run a debug kernel with lock checking and memory diagnostics where practical.

**Completion gate:** the ground receives samples through `/dev/orbit_telemetry`; ABI tests pass; repeated lifecycle tests produce no warning, stale access, or lock report. Set an initial repeat count such as 100 cycles and record it, rather than claiming leak freedom from one unload.

**Explain:** why can a pointer still be reachable through an open fd after the device has been removed?

## 11. M7 — Implement and connect the QEMU peripheral

**Learn:** QEMU object/device model, SysBus devices, MemoryRegion callbacks, virtual timers, interrupt lines, ARM GIC wiring, and generated FDT nodes.

Split this milestone into three independently testable steps.

### M7a: Registers without the Linux driver

1. Build pinned QEMU and boot the existing guest unchanged.
2. Add the device type, source/build registration, state, reset behavior, and bounded MMIO region.
3. Implement ID/version/status reads and legal control writes.
4. Reject or explicitly define unaligned, wrong-width, unknown-offset, and read-only writes.
5. Add QTest cases for reset values and register behavior before attaching Linux.

### M7b: Timer, latch, and interrupts

1. Use the QEMU virtual clock for sample production.
2. Implement the latch and overrun contract from Section 3.2.
3. Implement masks, ACK semantics, reset-generation behavior, and sampling-period bounds.
4. In QTest, step the clock, inspect the snapshot, and verify IRQ assertion/deassertion when observable through the test harness.
5. Test masked pending interrupts, unmasking, ACKing unrelated reasons, back-to-back events, reset while enabled, and disable/re-enable.

[QTest](https://www.qemu.org/docs/master/devel/testing/qtest.html) supports device-model testing and virtual-clock control, making these checks more deterministic than sleeping in host scripts.

### M7c: Machine integration and driver probe

1. Inspect the pinned ARM `virt` memory map and interrupt allocation; reserve non-conflicting resources in a maintained patch.
2. Instantiate/connect the SysBus device from machine code and generate its FDT node there. This is the recommended v1 integration choice; preserve an external-DTB approach only if there is a concrete reason.
3. Define the YAML binding and validate the node. Distinguish a GIC interrupt specifier from the Linux virtual IRQ allocated later; they are not interchangeable integers.
4. Implement platform probe: map resources; establish safe masked device state; validate identity/version; initialize queues/locks; register handler; expose device; enable sampling last. Every failure path must leave the device quiescent.
5. Replace the test producer with `readl()`/`writel()` accesses and an IRQ handler using the same enqueue path. MMIO must use the appropriate kernel I/O accessors, as described in [Linux device I/O documentation](https://docs.kernel.org/driver-api/device-io.html).
6. Disable the test source in the release image and prove the custom QEMU timer is the source of data.

**Tests and gate:** QTest register/timer tests pass; DT binding validates; Linux matches `orbit,telemetry-v1`; wrong ID/version fails probe safely; IRQ counts track useful events; normal data travels from QEMU to SQLite without a software polling acquisition loop. Vary sample period via the command path and observe the resulting rate.

**Debug in this order:** device instantiated → FDT node present → driver bound → MMIO identity correct → timer active → IRQ pending → mask enabled → GIC wiring correct → ISR called → FIFO enqueued → reader woken.

**Explain:** why does the correct device-tree string not prove the interrupt line is wired correctly?

## 12. M8 — Fault injection, recovery, and trustworthy measurement

**Learn:** deterministic experiments, test oracles, counter accounting, clock domains, percentiles, and failure artifact capture.

Implement fault control as an explicit development-only channel. Ordinary operational commands can reset or change period; arbitrary fault injection requires a test build/configuration and separate authorization. A test register is acceptable initially, provided release behavior disables its effects and tests confirm that boundary.

Every fault test needs four phases: **establish healthy baseline → inject fault → assert detection/effect → clear fault and assert recovery**. Use deadlines and condition checks rather than long fixed sleeps.

| Fault or edge | Injection | Required observation and response |
|---|---|---|
| Over-temperature | Fixed out-of-range measurement | Invalid flag/reason stored; service remains alive |
| Under-voltage | Low voltage | Fault event and counter; documented continued acquisition |
| Invalid sentinel | Reserved numeric value | Correct validity classification; no arithmetic failure |
| Frozen sequence | Stop sequence advancement | Duplicate-source diagnostic; transport retransmission not confused with sensor behavior |
| Suppressed interrupt | Prevent line assertion | Acquisition watchdog reports timeout without deadlock; clear fault externally and verify recovery |
| Delayed interrupt | Bounded virtual delay | Snapshot remains coherent; lateness or missed samples accounted for |
| Unexpected device reset | Reset model while active | Generation change or health failure detected; bounded reinitialization restores configured period and masks |
| Stuck ready/fault bit | Prevent expected clear | Bounded ISR behavior; storm detection/masking and explicit recovery result |
| Excessive generation rate | Test-only override | Device overrun/kernel/flight drops classified; memory remains bounded |
| Kernel FIFO saturation | Pause application reads | Drop-newest count and surviving sequence set match policy |
| Flight retention saturation | Block connection beyond capacity | Recent retained samples and reported losses match policy |
| Ground restart | Terminate/restart server | Reconnect; deduplicate already committed retransmissions |
| ACK loss | Disconnect after commit before ACK | Same telemetry ID resends; one database row |
| Flight restart | Restart client process | New boot/session IDs; no false claim of recovering memory backlog |
| Database failure | Read-only/full test database or injected commit error | No successful persistence ACK; bounded failure behavior |
| Slow peer | Stop reading/writing mid-frame | Deadline fires; no unbounded queue or shutdown hang |
| Certificate/replay failures | M4 fixtures | Rejection with no unauthorized command effect |

Do not use a background register polling loop to replace IRQ acquisition. A low-rate health check or timeout-driven recovery path can inspect control/status, but must be documented separately. If reset clears IRQ enable state, the watchdog needs a bounded way to reinitialize without waiting forever for an interrupt that cannot arrive.

### Harness and evidence

Create one harness that allocates per-run ports/directories, builds or selects verified artifacts, starts ground and QEMU, waits for readiness, runs scenarios, queries SQLite and counters, writes results, and always terminates child processes. Fail if required assertions are missing.

Save a manifest, seed, scenario parameters, kernel log, QEMU log, flight/ground JSON logs, database or query export, counter snapshots, JUnit results, and benchmark JSON in one run directory. Use bounded logging/rotation for long runs and define storage retention; bounded queues alone do not bound disk growth.

### Counter accounting

At a quiescent checkpoint within one device generation, reconcile disjoint categories:

```text
generated events = published snapshots + device-discarded events
published snapshots = acquired snapshots + still-pending snapshots + reset-discarded snapshots
driver enqueues = completed user reads + FIFO occupancy + explicitly discarded queued records
```

Track kernel insertion failures separately from successful enqueues. Track telemetry created, retained, overflow-discarded, acknowledged, and abandoned on shutdown separately from network transmission attempts. A retransmission increments attempts, not unique samples. Classifying a sensor value invalid does not mean it disappeared from accounting.

### Measurement methodology

- Use guest monotonic timestamps for driver-enqueue-to-flight-read timing.
- Use host monotonic timestamps for a host-controlled outage and reconnection observation.
- Device generation uses QEMU virtual time, which is not automatically comparable to host or guest wall time.
- Do not subtract a host receive timestamp from a guest timestamp and call it one-way latency without a synchronization/calibration method and an error bound.
- Initially report guest-local processing latency and same-clock round-trip/ACK latency. Mark device-to-driver and one-way end-to-end latency as unmeasured until correlated tracing or a validated clock mapping exists.
- Use rate sweeps and retained raw results for throughput, queue high-water marks, RSS, CPU, drops, and recovery. Record offered and achieved rate.
- Report sample count, warmup exclusion, median, p95, max, host load/specification, emulation configuration, and source revisions. Include at least three repetitions for comparative claims.

**Completion gate:** every supported fault has an automated assertion and recovery outcome; the critical smoke suite boots QEMU in CI; no performance claim exists without its script and run artifacts.

**Explain:** why might a larger throughput number on another computer say little about an improvement in your code?

## 13. M9 — Package a reproducible first release

**Build in order:**

1. Add documented build/test/demo commands and a prerequisites check with useful failure messages.
2. Add a concise README architecture diagram and a capability table linked to tests.
3. Document register behavior, ABI, protocol, security/replay policy, faults, reproduction, debugging, and limitations.
4. Run the quick start from a fresh checkout with clean generated state.
5. Record a short demo: normal telemetry → outage → recovery → rejected replay or sensor fault → test summary.
6. Tag a release only after the clean-checkout demonstration passes.

**Release gate:** custom QEMU source and patches are pinned; the test generator is disabled; no credentials are tracked; mTLS is mandatory; all core requirements have passing evidence; benchmark claims link to results; the system is clearly labeled fully emulated.

Update resume claims from actual gates, not the original stage labels. In particular, the brief's Stage A bullets mention bounded buffering and outage recovery that are not complete at its initial user-space MVP. Use those claims only after M3 passes. Security claims wait for M4; MMIO/IRQ claims wait for M7; reliability claims wait for M8. Yocto stays out until its own image is built and tested.

## 14. Optional M10 — Yocto packaging

Start only after the first release is useful without Yocto.

1. Select a supported Yocto release and compatible host environment using its release-specific documentation.
2. Create `meta-orbitallink` and pinned layer/build configuration.
3. Add recipes for the flight service, driver, configuration, service unit, and required kernel options.
4. Keep private keys out of recipes and image artifacts; provision development credentials at test runtime.
5. Boot the image with the same custom QEMU device and run the existing smoke/security subset.
6. Verify service ordering, restart behavior, permissions, licensing metadata, and clean rebuild instructions.

**Gate:** clean build → boot → driver probe → authenticated telemetry → successful test report. Use the [Yocto build-host guide](https://docs.yoctoproject.org/current/dev-manual/start.html) to choose the compatible environment when beginning this optional stage.

## 15. Repository and command evolution

Keep the structure in the brief, but grow it with each milestone. Add `docs/decisions/` early, `integration/` with M2, `driver/` with M6, and the QEMU patch/test directories with M7. Keep third-party build trees and output artifacts outside tracked project sources.

The following are **targets to implement**, not commands that already exist:

| Target | Introduce | Contract |
|---|---|---|
| `make doctor` | M0 | Report required tools and environment problems |
| `make check` | M1 | Format/lint, native build, C++/Python unit and vector tests |
| `make demo-native` | M2 | Start deterministic local telemetry demo |
| `make test-native-e2e` | M2 | Verify exact SQLite contents and cleanup |
| `make test-recovery` | M3 | Native outage, overflow, ACK-loss, shutdown cases |
| `make test-security` | M4 | Certificate, role, replay, and stale-command cases |
| `make guest-image` | M5 | Build pinned kernel/rootfs/service artifacts |
| `make test-driver` | M6 | KUnit plus guest syscall/lifecycle tests |
| `make test-device` | M7 | Build custom QEMU and execute QTest cases |
| `make demo` | M7 | Boot custom device and show real driver-origin telemetry |
| `make test-e2e` | M8 | Full deterministic core fault matrix |
| `make benchmark` | M8 | Parameterized measurements and machine-readable output |
| `make release-check` | M9 | Reproduction, core gates, documentation, credential checks |

CI should start small and grow with these commands. Pull requests run native tests and sanitizers; driver/device changes add targeted kernel/QTest checks; main runs the guest smoke test; scheduled/release runs perform the full fault matrix and longer measurements. Separate sanitizer configurations when incompatible. Cache by source/configuration hashes, publish failure artifacts, set job timeouts, and prevent stale image caches from hiding a broken build.

## 16. Requirements mapped to evidence

| Original requirement | Implemented by | Primary evidence |
|---|---|---|
| FR-01–03: generation, MMIO, IRQ | M7 | QTest plus guest IRQ/sample checks |
| FR-04: device-tree discovery | M7 | Binding validation and positive/negative probe tests |
| FR-05–06: ABI and poll | M6 | Guest syscall contract suite |
| FR-07: range and sequence validation | M1–M2, M7 | Fixtures and generation/reset/frozen-sequence tests |
| FR-08: authenticated encryption | M4–M5 | TLS positive and negative tests in native and guest modes |
| FR-09: SQLite storage | M2 | Exact expected rows, deduplication, commit failure tests |
| FR-10–11: commands and rejection | M4, M6–M7 | Accepted effects and no-effect rejection assertions |
| FR-12: bounded outages | M3, M8 | Short/long outages, survivor sets, memory bounds |
| FR-13: counters | M2–M8 | Counter snapshots reconciled against deterministic inputs |
| FR-14: all supported faults | M8 | Fault matrix with automatic assertions |
| NFR-01–02: bounds and lengths | M1–M4 | Adversarial frames, queue limits, slow-peer tests |
| NFR-03: clean shutdown | M3, M6 | Shutdown/lifetime tests and race diagnostics |
| NFR-04: no sleeping in IRQ/spinlock | M6–M7 | Code review, debug-kernel diagnostics, stressed paths |
| NFR-05–06: library crypto and keys | M4, M9 | TLS configuration, key generation/ignore checks |
| NFR-07: repeatability | Every milestone | Documented commands, pinned inputs, clean builds |
| NFR-08: emulation disclosure | M9 | README and resume review |
| NFR-09: warning policy | M0 onward | Project-owned warnings-as-errors builds |
| NFR-10: measured claims | M8–M9 | Committed scripts and retained benchmark results |

## 17. Your first five coding sessions

Each session should produce something you can inspect, not just more scaffolding.

1. **Build and test:** create the smallest C++/Python packages, record versions, run tests, commit.
2. **One sample on paper:** specify exact bytes for a header and sample, including negative temperature and identity. Write the first fixture.
3. **C++ codec:** encode/decode that sample, test malformed length and truncated input, inspect hex output.
4. **Python agreement:** load the same fixture and verify identical values; feed split chunks through the parser.
5. **One persisted sample:** send it locally, verify the SQLite row, and return its ACK after commit. Automate process cleanup.

At that point, continue through the milestones in order. Before moving on, answer the milestone's explanation question without copying the documentation. If you cannot explain a boundary, reduce it to one sample and inspect it there.

## 18. Focused reference shelf

Read these when you reach the relevant milestone; use documentation matching your pinned versions where available.

- M0: [Microsoft WSL installation](https://learn.microsoft.com/windows/wsl/install).
- M4: [Python ssl](https://docs.python.org/3/library/ssl.html), [OpenSSL SSL_get_error](https://docs.openssl.org/3.6/man3/SSL_get_error/), and the peer-name verification reference in M4.
- M5/M7: [QEMU system introduction](https://www.qemu.org/docs/master/system/introduction.html), ARM virt and QTest links above.
- M6: [Linux driver API](https://docs.kernel.org/driver-api/index.html), [KUnit](https://docs.kernel.org/dev-tools/kunit/index.html), and devres/device-I/O links above.
- M7: [Linux device-tree usage](https://docs.kernel.org/devicetree/usage-model.html).
- Optional M10: Yocto build-host guide above, then the selected release's recipe and layer manuals.

The most useful learning outcome is the ability to explain a failure using evidence: which component owned the data, which contract was violated, which test caught it, and why the fix restores the contract.
