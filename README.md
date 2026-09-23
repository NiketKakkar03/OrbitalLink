# OrbitalLink

OrbitalLink is a learning project that builds a secure embedded telemetry path in
small milestones. M0 through M4 provide concurrent native telemetry, mutual TLS,
and a live operator command path:

```text
C++ acquisition thread -> bounded retention queue -> C++ network thread
    -> TLS 1.3/TCP -> Python ground station -> SQLite commit -> ACK
local operator -> mode-0600 Unix socket -> ground -> TLS command
    -> acquisition-owned fake device -> durable command result
```

Runtime telemetry transport is TLS-only. Parser and socketpair tests stay
transport-independent, but the ground listener, native sender, and flight
service require certificates.

## Verify M0 Through M4

Create the Python environment described in `docs/environment.md`, then run:

```bash
make check
```

This validates both protocol fixtures, Python storage and protocol behavior,
C++ protocol behavior, compilation of the native sender, and the full native
integration path. The integration test sends 10,000 deterministic samples,
replays 100 duplicates, and verifies that SQLite contains exactly 10,000 rows.
It also tests bounded queues, short and overflowing outages, lost and partial
ACKs, a stalled peer, retry backoff, bounded shutdown, mutual-TLS rejection
cases, and command validation/effect accounting.

Run the concurrency policy under the supported sanitizers separately:

```bash
make test-sanitizers
make test-tsan
```

## Run the native path manually

In one terminal, start the ground receiver:

```bash
bash tools/make_dev_certs.sh .local/certs
uv run --with pytest==8.3.4 python -m ground.orbit_ground \
  --database /tmp/orbit-telemetry.db \
  --port 9000 \
  --ca-file .local/certs/ca.cert.pem \
  --server-cert .local/certs/server.cert.pem \
  --server-key .local/certs/server.key.pem \
  --authorized-client orbital-ground \
  --control-socket /tmp/orbit-control.sock
```

In another terminal, compile and run the flight sender:

```bash
c++ -std=c++20 -Wall -Wextra -Werror -I/opt/homebrew/include \
  flight/send_sample.cpp -o /tmp/orbit-send-sample \
  -L/opt/homebrew/lib -lssl -lcrypto
/tmp/orbit-send-sample 127.0.0.1 9000 10 \
  .local/certs/ca.cert.pem \
  .local/certs/client.cert.pem \
  .local/certs/client.key.pem \
  orbital-ground
```

Stop the receiver with Ctrl-C. Inspect the database through the textual CLI:

```bash
uv run --with pytest==8.3.4 python -m ground.orbit_ground --database /tmp/orbit-telemetry.db --status
uv run --with pytest==8.3.4 python -m ground.orbit_ground --database /tmp/orbit-telemetry.db --tail 5
uv run --with pytest==8.3.4 python -m ground.orbit_ground --database /tmp/orbit-telemetry.db \
  --export /tmp/orbit-telemetry.jsonl
```

The receiver sends an ACK only after SQLite commits a sample. Retransmitted
samples are not inserted twice, but are ACKed because their existing row is
already durable. Structurally valid but physically implausible readings remain
stored with a validity flag and reason; malformed protocol frames are rejected.

## Run the M4 flight service

With the same ground receiver running on port 9000, compile and start the
concurrent service:

```bash
c++ -std=c++20 -Wall -Wextra -Werror -pthread -I/opt/homebrew/include \
  flight/flight_service.cpp -o /tmp/orbit-flight-service \
  -L/opt/homebrew/lib -lssl -lcrypto
/tmp/orbit-flight-service 127.0.0.1 9000 \
  --count 100 \
  --interval-ms 100 \
  --capacity 1024 \
  --ca-file .local/certs/ca.cert.pem \
  --client-cert .local/certs/client.cert.pem \
  --client-key .local/certs/client.key.pem \
  --server-name orbital-ground \
  --command-authorized-server-name orbital-ground
```

The default generation rate is 10 samples per second and the default backlog
capacity is 1,024 samples. When the backlog is full, the oldest queued sample is
dropped so recent telemetry is retained. The one partially or fully transmitted
but unacknowledged frame is bounded separately and is never evicted midway.

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
jitter, and reset after a successful ACK. Socket and TLS operations use
nonblocking I/O and deadlines. SIGINT or SIGTERM stops acquisition, attempts a bounded drain,
accounts for every remaining sample, closes descriptors, and joins both threads
within the three-second external shutdown contract.

The flight backlog exists only in memory. A flight-process crash or restart can
therefore lose unacknowledged samples. If ground remains unavailable indefinitely,
the queue stays bounded, old samples are dropped with counters, and shutdown
discards and accounts for whatever remains.

## M4 security and commands

The development CA script writes disposable credentials outside tracked source
and sets private keys to mode 600. Do not reuse these credentials outside local
tests.

The threat model for M4 protects telemetry confidentiality/integrity and device
control actions on the host loopback/native test path. Trusted components are
the local OS account, the generated development CA, the Python ground process,
and the C++ flight process. Out of scope for M4 are compromised host root,
production key management, QEMU guest isolation, and kernel-driver argument
validation, which arrives with the driver milestone.

The command contract uses a fresh session per authenticated connection, a strictly
increasing command sequence, a monotonic deadline, and a certificate identity
allowlist. Validation happens before device effects: role, session, sequence,
deadline, arguments, then bounded dispatch. Duplicate commands in a live session
return the cached result when known and do not reapply reset-like effects; after
reconnect or restart, old sessions are stale and pending commands are not
replayed automatically. Lost result acknowledgement is therefore an uncertain
outcome, not permission to silently execute the command again.

The flight service grants command authority only when the verified server name
matches `--command-authorized-server-name`. Omitting that option leaves telemetry
enabled while advertising no command role. Submit and inspect commands through
the local socket while both services run:

```bash
uv run --with pytest==8.3.4 python -m ground.orbit_ground \
  --control-socket /tmp/orbit-control.sock --submit-command set-period --argument 250
uv run --with pytest==8.3.4 python -m ground.orbit_ground \
  --control-socket /tmp/orbit-control.sock --control-state
uv run --with pytest==8.3.4 python -m ground.orbit_ground \
  --control-socket /tmp/orbit-control.sock --command-results
```

See `docs/m4-completion-audit.md` for the completed acceptance matrix and test
evidence. The next milestone is M5: pinning and booting the ARM64 Linux guest.
