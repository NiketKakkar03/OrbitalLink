# OrbitalLink

OrbitalLink is a learning project that builds a secure embedded telemetry path in
small milestones. M3 provides a concurrent native macOS/Linux implementation:

```text
C++ acquisition thread -> bounded retention queue -> C++ network thread
    -> TCP -> Python ground station -> SQLite commit -> ACK
```

The native transport is intentionally plaintext and bound to loopback by default. It
is a development stage, not the release security design; mutual TLS arrives in
M4.

## Verify M0 through M3

Create the Python environment described in `docs/environment.md`, then run:

```bash
make check
```

This validates both protocol fixtures, Python storage and protocol behavior,
C++ protocol behavior, compilation of the native sender, and the full native
integration path. The integration test sends 10,000 deterministic samples,
replays 100 duplicates, and verifies that SQLite contains exactly 10,000 rows.
It also tests bounded queues, short and overflowing outages, lost and partial
ACKs, a stalled peer, retry backoff, and bounded shutdown.

Run the concurrency policy under the supported sanitizers separately:

```bash
make test-sanitizers
make test-tsan
```

## Run the native path manually

In one terminal, start the ground receiver:

```bash
.venv/bin/python -m ground.orbit_ground \
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

## Run the M3 flight service

With the same ground receiver running on port 9000, compile and start the
concurrent service:

```bash
c++ -std=c++20 -Wall -Wextra -Werror -pthread \
  flight/flight_service.cpp -o /tmp/orbit-flight-service
/tmp/orbit-flight-service 127.0.0.1 9000 \
  --count 100 \
  --interval-ms 100 \
  --capacity 1024
```

The default generation rate is 10 samples per second and the default backlog
capacity is 1,024 samples. When the backlog is full, the oldest queued sample is
dropped so recent telemetry is retained. The one partially or fully transmitted
but unacknowledged frame is bounded separately and is never evicted midway.

Each service run generates a fresh random 64-bit boot identity so restarted
sequence numbers remain distinct in SQLite. Use `--boot-id NUMBER` for explicit
replay experiments. The small `send_sample` fixture tool deliberately retains a
fixed identity for deterministic duplicate tests.

The service prints one final JSON object containing counters such as:

```text
generated, acknowledged, dropped_oldest, discarded_shutdown,
connect_attempts, connect_failures, reconnects, transmission_attempts,
retransmissions, queue_high_watermark, gap_ranges
```

At shutdown, sample accounting must satisfy:

```text
generated = acknowledged + dropped_oldest + discarded_shutdown
```

Overflow losses are also recorded in a separate bounded gap summary. Consecutive
telemetry sequences coalesce into ranges. Up to 16 ranges retain details; after
that, the summary keeps an exact omitted-loss count and marks its detailed report
incomplete instead of allocating memory without a bound.

In M3 this gap notice is emitted locally in the service's final JSON summary; it
is not yet transported to ground as a new wire message. A live ground-bound gap
message can be added with the later operational protocol without making the
in-memory loss-reporting mechanism unbounded.

Connection retries begin at 250 ms, double to a five-second cap, include bounded
jitter, and reset after a successful ACK. Socket operations use nonblocking I/O
and deadlines. SIGINT or SIGTERM stops acquisition, attempts a bounded drain,
accounts for every remaining sample, closes descriptors, and joins both threads
within the three-second external shutdown contract.

The flight backlog exists only in memory. A flight-process crash or restart can
therefore lose unacknowledged samples. If ground remains unavailable indefinitely,
the queue stays bounded, old samples are dropped with counters, and shutdown
discards and accounts for whatever remains.
