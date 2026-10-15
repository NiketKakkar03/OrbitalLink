# OrbitalLink

OrbitalLink is a learning project that builds a secure embedded telemetry path in
small milestones. Start with the [complete project walkthrough](docs/project-overview.md)
for architecture, implemented behavior, validation evidence and remaining work.
The implementation runs natively on macOS/Linux and inside an ARM64 Linux guest:

```text
C++ acquisition thread -> bounded retention queue -> C++ network thread
    -> TLS/TCP -> Python ground station -> SQLite commit -> ACK
```

The normal flight service and ground receiver use mutual TLS 1.3 with explicit
peer identity authorization. See [security setup](docs/security.md) for the
authenticated demo. The deterministic sender below uses the explicit loopback
fixture transport retained for M2/M3 regression tests.

## Verify the native implementation

Create the Python environment described in `docs/environment.md`, then run:

```bash
make doctor
make check
```

This validates both protocol fixtures, Python storage and protocol behavior,
C++ protocol behavior, compilation of the native sender, and the full native
integration path. The integration test sends 10,000 deterministic samples,
replays 100 duplicates, and verifies that SQLite contains exactly 10,000 rows.
It also tests bounded queues, short and overflowing outages, lost and partial
ACKs, a stalled peer, retry backoff, and bounded shutdown.
Security tests use real TLS connections and disposable development certificates.

Run the concurrency policy under the supported sanitizers separately:

```bash
make test-sanitizers
make test-tsan
make test-security-sanitizers
```

Run the authenticated automated demo:

```bash
make demo-native
```

It starts both native processes, verifies 100 acknowledged samples and their
exact stored sequence set, and saves the database and process logs under
`build/native-demo/`. See [security setup](docs/security.md) for manual TLS and
operator-command use, and [validation evidence](docs/validation.md) for retained
check results.

## Run the native path manually

In one terminal, start the ground receiver:

```bash
.venv/bin/python -m ground.orbit_ground \
  --test-plaintext \
  --database /tmp/orbit-telemetry.db \
  --port 9000
```

In another terminal, compile and run the flight sender:

```bash
c++ -std=c++20 -Wall -Wextra -Werror \
  flight/send_sample.cpp -o /tmp/orbit-send-sample
/tmp/orbit-send-sample 127.0.0.1 9000 10
```

Stop the receiver with Ctrl-C. Inspect the database through the textual CLI:

```bash
.venv/bin/python -m ground.orbit_ground --database /tmp/orbit-telemetry.db --status
.venv/bin/python -m ground.orbit_ground --database /tmp/orbit-telemetry.db --tail 5
.venv/bin/python -m ground.orbit_ground --database /tmp/orbit-telemetry.db \
  --export /tmp/orbit-telemetry.jsonl
```

The receiver sends an ACK only after SQLite commits a sample. Retransmitted
samples are not inserted twice, but are ACKed because their existing row is
already durable. Structurally valid but physically implausible readings remain
stored with a validity flag and reason; malformed protocol frames are rejected.

## Run the concurrent flight service

With the TLS receiver and disposable certificates described in
`docs/security.md`, compile and start the concurrent service:

```bash
make test-cpp
/tmp/orbit_flight_service 127.0.0.1 9000 \
  --outbox /tmp/orbit-flight-outbox.db \
  --ca /tmp/orbit-certificates/ca.pem \
  --cert /tmp/orbit-certificates/flight.pem \
  --key /tmp/orbit-certificates/flight.key \
  --server-name ground.orbital.test \
  --count 100 \
  --interval-ms 100 \
  --capacity 1024
```

The default generation rate is 10 samples per second. Normal flight builds require
a SQLite outbox and default to a 64 MiB total storage budget; `--outbox-bytes`
changes that budget and `--capacity` adds an optional record-count limit.
Old queued samples are dropped when retention fills, while an in-flight sample
stays protected until its matching ACK. The memory-only regression fixture keeps
its original 1,024 queued samples plus a separately bounded in-flight frame.

Each service run generates a fresh random 64-bit boot identity so restarted
sequence numbers remain distinct in SQLite. Persisted samples replay with their
original identities and payloads. Explicit boot-ID overrides are limited to the
plaintext regression fixture. The small `send_sample` fixture tool retains a
fixed identity for deterministic duplicate tests.

The service prints one final JSON object containing counters such as:

```text
generated, acknowledged, dropped_oldest, discarded_shutdown,
connect_attempts, connect_failures, reconnects, transmission_attempts,
retransmissions, queue_high_watermark, gap_ranges
```

The memory-only fixture accounts at shutdown as:

```text
generated = acknowledged + dropped_oldest + discarded_shutdown
```

Durable totals persist across runs and satisfy:

```text
durable_generated = durable_acknowledged + durable_dropped + durable_retained
```

Current-run ACKs may include samples recovered from earlier runs; use the durable
totals to reconcile a restarted service. Storage failures produce an explicit
failure exit and preserve whatever remains committed in the outbox.

Overflow losses are also recorded in a separate bounded gap summary. Consecutive
telemetry sequences coalesce into ranges. Up to 16 ranges retain details; after
that, the summary keeps an exact omitted-loss count and marks its detailed report
incomplete instead of allocating memory without a bound.

Normal flight also persists bounded loss reports, retransmits immutable report
identities, and removes them only after ground commits and ACKs them. Ground
deduplicates reports and rejects conflicting payloads under an existing identity.
When detailed ranges fill, bounded summaries preserve exact loss counts while
marking the missing range detail. See [retention policy](docs/retention.md).

Connection retries begin at 250 ms, double to a five-second cap, include bounded
jitter, and reset after a successful ACK. Socket operations use nonblocking I/O
and deadlines. SIGINT or SIGTERM stops acquisition, attempts a bounded drain,
accounts for every remaining sample, closes descriptors, and joins both threads
within the three-second external shutdown contract on responsive storage.
Uninterruptible filesystem I/O can delay a durable-service shutdown; the native
network tests do not establish a hard deadline under failing storage hardware.

Committed telemetry and loss reports survive a flight-process restart. An
indefinite outage still exceeds finite storage: old queued samples are dropped
with durable loss accounting. Disk corruption, hardware failure, or a filesystem
that does not honor synchronization remain outside that guarantee.

## Embedded milestone gates

Native M4 is implemented and locally validated; see
[the M4 journal](docs/journal/session-004.md) for results and limitations.
The ARM64 cross-toolchain, kernel and complete image build pass in the Linux
builder on Apple Silicon; see [guest setup](docs/guest.md).
Guest boot and two-boot persistent replay passed: seven retained samples plus
100 new samples reached durable ground storage over mutual TLS.
The [driver test backend and flight adapter](docs/driver.md) passed the Linux
syscall suite, 100 lifecycle cycles and a 100-sample driver-to-ground TLS gate.
The `feature/multisensor-validation` branch adds a custom QEMU MMIO/IRQ hub and
ABI2 driver, with power, thermal, orientation, health, synthetic radiation and
event reports. Shared deterministic interactions include eclipse, heater changes
and a pointing disturbance that pauses science collection. Legacy records remain
supported. See [sensor validation](docs/sensor-validation.md) for the 10-minute
baseline, separate native/embedded measurements, priority retention, bounded send
window and remaining fault/release gates. Native results establish the host path;
custom-device results require their own guest evidence.
