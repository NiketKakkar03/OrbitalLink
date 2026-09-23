# Session 002: M2 native telemetry path

## Question

Can the native C++ flight process send deterministic telemetry to Python, have
Python commit it durably to SQLite, and receive an ACK only after that commit?

## Change

- Added a shared `TELEMETRY_ACK` wire message in Python and C++.
- Added SQLite telemetry storage with unsigned 64-bit preservation, durable
  commits, unique telemetry identity, receive time, and physical-validity data.
- Added bounded event-detail and idempotent command-result tables.
- Added a loopback Python receiver and deterministic C++ sample source.
- Added status, tail, and JSON Lines export commands.
- Added EOF/truncated-stream validation and failure-path tests.

## Test command

```bash
make check
```

## Observed result

The full suite passes, including a native run of 10,000 unique samples followed
by 100 duplicate retransmissions. The final database contains the exact 10,000
expected identities and values. Malformed input creates no row, and an injected
database failure produces no ACK.

## Explanation

A TCP write proves only that bytes entered the transport. The ground station
therefore decodes and validates a frame, commits it to SQLite, and only then
sends an ACK containing the telemetry identity. A uniqueness constraint makes
retries safe: a duplicate does not create a second row, while its existing
durable row still justifies an ACK.

## Next step

M3 adds bounded queues, concurrency, and outage recovery without weakening the
M2 commit-before-ACK contract.
