# Session 004: complete native M4 before embedded foundations

Date: October 14, 2026.

## Question

Can an explicitly authorized ground peer command the native fake device without
replaying stale or uncertain operations, while preserving bounded telemetry,
durable ACKs, and stop-responsive shutdown?

## Implementation

Flight establishes a fresh cryptographically random command session on each
mutually authenticated TLS connection and announces its boot identity and
monotonic time. An exact verified certificate DNS SAN must also have the explicit
command role. Requests require an increasing sequence, valid arguments, and a
bounded deadline; acquisition rechecks expiry before applying an effect.

Enable, disable, set-period, reset, and clear-fault change the native fake device.
Acquisition owns the effects; network owns TLS and session policy. The runtime
dispatches one operation at a time; the dispatcher and ground broker enforce
fixed capacity limits. Disconnect invalidates retained commands. Result loss is
reported as `outcome_unknown`, and no automatic command replay occurs.

Ground persists command completions in SQLite and exposes a local operator
socket in an owner-only directory. Its CLI reads bounded newline-framed
responses and returns success only for `applied`. Rejection, incomplete responses,
timeouts, and unavailable connections cannot silently become successful commands.

Fixed interrupted-integration shutdown, idle reconnect while acquisition is
disabled, sequence gaps after rejection, expired pending-request queue exhaustion,
malformed result isolation, and local socket shutdown/path replacement handling.

Native setup now includes `make doctor`, `make demo-native`, retained validation
manifests/logs, and Ubuntu/macOS CI configuration with explicit OpenSSL setup.
CI publishes validation and demo logs without generated certificate private keys.
Remote CI has not been run as part of this local session.

## Verification

`make check` passes **122 Python tests**:

- 70 protocol/storage/broker/operator CLI tests.
- 5 native persistence tests, including 10,000 unique samples and duplicates.
- 13 recovery/restart/shutdown tests.
- 23 real TLS and command tests.
- 11 validation-runner failure/cleanup tests.

C++ temperature, protocol, bounded recovery, and concurrent command policy tests
pass with warnings as errors. `make test-sanitizers` and `make test-tsan` pass,
including concurrent session invalidation/dispatch stress. The 23 real TLS and
command tests also pass with the production flight binary instrumented by ASan
and UBSan via `make test-security-sanitizers`.

`make demo-native` generates and acknowledges exactly 100 samples, verifies
their complete SQLite sequence set, and reports zero drops/retransmissions.

Local evidence directories:

```text
build/validation/20261014T175833Z-0ecf475f/  ASan/UBSan and TSan
build/validation/20261014T180207Z-01dc62ac/  native and production TLS sanitizers
build/native-demo/run-1ektn844/            authenticated demo
```

Socket tests require execution permission for local TCP/Unix-domain binding;
sandbox denials were rerun outside the restricted sandbox.

## Limits and next gate

This completes native M4 against a deterministic fake device. Execution-time
expiry and dispatcher saturation are verified by policy tests; the native wire
path intentionally executes only one command at a time. Real TLS tests cover all
five effects, rejection without effects, observer role denial, result loss,
reconnect, service restart, and local CLI completion persistence.

There is no live state-query opcode. Operators inspect telemetry, SQLite results,
and the final service state; an unresolved outcome remains unknown. Backlog is
memory-only and gap notices remain local. Development credentials do not provide
production provisioning, rotation, revocation, or hardware attestation.

Existing guest and driver prototypes were preserved and not advanced during
this session. The next work is M5 guest build/boot and M6 kernel/runtime validation,
followed by M7 custom MMIO/IRQ integration. The full embedded fault matrix and
benchmarks remain later gates.
