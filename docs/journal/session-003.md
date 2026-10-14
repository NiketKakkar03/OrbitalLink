# Session 003: M3 bounded concurrency and outage recovery

## Question

Can flight continue acquiring telemetry during a ground outage without
unbounded memory growth, recover safely when ground returns, and always shut
down within three seconds?

## Change

- Added a thread-safe fixed-capacity retention queue with drop-oldest overflow.
- Added an injectable exponential retry policy with bounded jitter.
- Added a flight service with separate acquisition and network threads.
- Made the network thread the exclusive owner of the TCP socket.
- Added nonblocking connect, send, and receive operations with deadlines.
- Added retained-frame retransmission after lost, partial, or invalid ACKs.
- Added explicit connection states and machine-readable counters.
- Added bounded, coalesced gap ranges with an incomplete-detail summary.
- Added stop-responsive waits and a bounded shutdown drain.
- Added deterministic outage and fault-injection integration tests.

## Policy

The queued backlog has a fixed capacity, defaulting to 1,024 samples. One
in-flight frame is bounded separately because bytes already being transmitted
must never be evicted from the middle of a frame. Queue overflow drops the oldest
queued sample and retains the newest one. Every generated sample ends as exactly
one of acknowledged, overflow-dropped, or discarded during shutdown.

Overflow gap details are kept separately from telemetry retention. Adjacent
dropped sequence numbers coalesce, detailed storage is fixed at 16 ranges, and
additional losses increment an exact omitted-detail count.
M3 emits this notice in the final local JSON summary. It does not yet define a
ground-bound gap-notice wire message.

Retry delay starts at 250 ms, doubles to a five-second cap, uses bounded jitter,
and resets after a successful ACK. Default acquisition is 10 samples per second.

## Test commands

```bash
make check
make test-sanitizers
make test-tsan
```

## Observed result

- Capacity four with six samples preserves sequences 3, 4, 5, and 6 and reports
  exactly two overflow drops.
- A logical 30-second outage at 10 Hz preserves and later commits all 300 samples
  when the default 1,024-sample capacity is available.
- Lost and partial ACKs cause retransmission without duplicate SQLite rows.
- Continuous failure and stalled peers remain bounded.
- SIGTERM interrupts long acquisition sleeps, mature multi-second backoff, and
  a configured 10-second I/O wait; tested shutdowns complete within three seconds.
- AddressSanitizer, UndefinedBehaviorSanitizer, and ThreadSanitizer policy tests
  complete without findings on the current Apple Clang toolchain.
- Repeated early SIGTERM tests cover the producer/queue-close interleaving that
  could otherwise terminate a process during shutdown.

## Limitations

The queue is memory-only. A process crash loses unacknowledged telemetry. An
indefinite outage eventually drops old samples, but memory remains bounded and
losses remain accounted. Transport is still plaintext until M4.

## Next step

M4 introduces mutual TLS, authenticated peer roles, and the command protocol
without changing the M2 commit-before-ACK or M3 bounded-recovery contracts.
