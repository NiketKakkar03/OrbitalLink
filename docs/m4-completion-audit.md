# M4 Completion Audit

Date: 2026-09-22. Scope: native M4 assessed against
`OrbitalLink_Roadmap.md` sections 3.5 and 8.

Verdict: the native M4 completion gate is satisfied. Only explicitly authorized
TLS peers exchange commands, each accepted command has one observable acquisition-
owned effect, and retries do not silently repeat an uncertain reset.

## Acceptance Matrix

| Requirement | Evidence | Status |
| --- | --- | --- |
| Threat model and exclusions | README and session 004 | Pass |
| Disposable credentials and restrictive permissions | Portable generator; directory 0700 and keys 0600 tests | Pass |
| TLS 1.3, chain validation, client certificate, server identity | Python and native C++ positive/negative tests | Pass |
| Exact client identity authorization | Exact-CN, misleading-subject, unauthorized, and deny-all tests | Pass |
| Nonblocking flight TLS and no plaintext runtime fallback | Native recovery and plaintext rejection tests | Pass |
| Fresh peer/boot-bound command session | Live session announcement and reconnect tests | Pass |
| Certificate-derived command role | Telemetry-only peer forging role 1 is rejected with zero effects | Pass |
| Ordered validation and bounded dispatch | Dispatcher tests plus live queue saturation | Pass |
| Execution-time expiration | Live queued-expiration test with unchanged effect counter | Pass |
| Five observable fake-device operations | Live operator/TLS/acquisition/result integration test | Pass |
| Bounded result cache and duplicate safety | Dispatcher cache, conflict, eviction, and duplicate tests | Pass |
| Reconnect/restart uncertainty | Durable ground lifecycle tests and live lost-reset-result reconnect test | Pass |
| Permission-restricted operator interface | Mode-0600 Unix socket and CLI tests | Pass |
| Durable command-result visibility | SQLite result correlation and CLI visibility tests | Pass |
| Cross-language command framing | Shared golden request/result/session bytes and every split point | Pass |

## Required Test Matrix

The suite covers valid peers; missing client certificates; wrong CA; expired and
not-yet-valid certificates; wrong server identity in Python and C++; trusted but
unauthorized and misleading-subject clients; plaintext; invalid arguments; exact
and conflicting duplicates; old sessions after reconnect; execution-time expiry;
lost reset results; and full command queues. Rejection tests assert unchanged
device state or effect counters.

## Verification

```bash
make check
make test-sanitizers
make test-tsan
git diff --check
```

These gates pass on the recorded ARM64 macOS development host. CI is configured
for Ubuntu and will provide independent clean-host evidence when the branch is
published. No generated credentials are tracked.

## Next Milestone

M5 begins by pinning QEMU, Linux, the ARM64 toolchain, and root filesystem inputs.
It must boot an unmodified ARM64 `virt` guest before adding the later driver or
custom peripheral work.
