# Session 004: M4 secure transport and command policy

## Question

Can the native telemetry runtime reject unauthenticated peers, preserve the M2
commit-before-ACK and M3 bounded-recovery behavior under TLS, and define command
handling so rejected or duplicate commands do not silently repeat device
effects?

## Change

- Added disposable development CA, client certificate, and server certificate
  tooling in `tools/make_dev_certs.sh`; private keys are mode 600 and `.local/`
  is ignored.
- Made the Python ground listener require TLS 1.3, a trusted client
  certificate, and an explicit allowed client identity.
- Made the C++ flight service and native sender require CA, client certificate,
  client key, and expected server identity arguments.
- Integrated OpenSSL TLS handshakes and read/write retry handling into the
  network-thread-owned socket path with bounded deadlines.
- Removed plaintext runtime I/O helpers from the flight service.
- Added command request/result codecs in Python and C++.
- Added an acquisition-owned fake-source command dispatcher for enable,
  disable, set-period, reset, and clear-fault.
- Added validation order for role, session, sequence, deadline, arguments, and
  bounded dispatch, plus cached duplicate results.

## Threat model

M4 protects telemetry confidentiality/integrity and fake-device control actions
on the native host path. The trust anchors are the disposable development CA and
the local OS user account that owns the credential files. Trusted components are
the Python ground process, the C++ flight process, and the local filesystem
permissions used to protect private keys.

M4 does not attempt to survive host root compromise, stolen private keys,
production certificate lifecycle failures, kernel driver bugs, or QEMU guest
escape. Driver-side argument validation is intentionally repeated later at the
kernel boundary.

## Test commands

```bash
make check
make test-sanitizers
make test-tsan
```

## Observed result

- `make check` passed the Python unit/protocol/storage/control/security suites,
  native telemetry and live command integration, 12 recovery scenarios, and
  C++ protocol/policy/command tests.
- `make test-sanitizers` passed with AddressSanitizer and
  UndefinedBehaviorSanitizer.
- `make test-tsan` passed with ThreadSanitizer.
- Missing, wrong-CA, expired, not-yet-valid, misleading-subject, unauthorized,
  plaintext, and wrong-server-identity peers are rejected.
- The M3 outage, retransmission, queue overflow, and shutdown tests pass through
  mutual TLS.
- Rejected commands leave fake-source state and effect counters unchanged.
  Duplicate commands return cached results and do not repeat effects.
- The live operator path applies all five commands, persists results, rejects a
  forged role, invalidates old sessions, and does not repeat a reset after its
  result is deliberately discarded.
- A live 17-command burst explicitly rejects one full-queue request and expires
  16 queued requests at execution time with zero effects.

## Limitations

M4 evidence is native-host only. It does not establish ARM64 Linux guest
packaging, kernel-driver validation, MMIO/IRQ behavior, or production certificate
lifecycle; those belong to later milestones.
