# M4 authenticated native transport

Normal flight-service builds require TLS credentials and have no plaintext
fallback. Ground requires TLS unless its explicit `--test-plaintext` loopback
fixture transport is selected. The recovery tests compile a separate
`ORBIT_TEST_PLAINTEXT` binary; never ship that binary.

Both endpoints require TLS 1.3, certificate-chain verification and certificate
validity. Flight verifies the configured ground DNS SAN using OpenSSL
`SSL_set1_host`, with common-name fallback disabled. Ground requires a client
certificate and authorizes only its configured exact DNS SAN. A trusted CA alone
does not authorize a telemetry peer.

Generate disposable credentials (two-day lifetime) and run:

```sh
.venv/bin/python -m ground.dev_certificates /tmp/orbit-certificates
.venv/bin/python -m ground.orbit_ground --database /tmp/orbit-tls.db --port 9000 \
  --ca /tmp/orbit-certificates/ca.pem --cert /tmp/orbit-certificates/ground.pem \
  --key /tmp/orbit-certificates/ground.key --allowed-client flight.orbital.test
make test-cpp
/tmp/orbit_flight_service 127.0.0.1 9000 --count 100 \
  --outbox /tmp/orbit-flight-outbox.db \
  --ca /tmp/orbit-certificates/ca.pem --cert /tmp/orbit-certificates/flight.pem \
  --key /tmp/orbit-certificates/flight.key --server-name ground.orbital.test
```

Generated private keys are permission-restricted and ignored by Git. This CA is
for isolated development only. Certificate deployment, rotation and revocation
remain release-operation work.

Flight's TLS object stays owned by the network thread. Nonblocking handshakes,
reads and writes follow OpenSSL WANT_READ/WANT_WRITE, preserve partial progress,
and use the existing stop-responsive polling deadlines. Failed TLS handshakes
close the connection and enter bounded retry without sending telemetry.

Run `make test-security` for valid peers, wrong ground identity, trusted but
unauthorized clients, expired/not-yet-valid client certificates, missing client
certificates, wrong trust roots and plaintext attempts. C++ credential parsing
fails before threads start for missing or invalid files.

References: [OpenSSL hostname verification](https://docs.openssl.org/3.6/man3/SSL_set1_host/)
and [OpenSSL nonblocking error handling](https://docs.openssl.org/3.6/man3/SSL_get_error/).

## Native commands and operator access

Flight announces a fresh random command session after each authenticated
connection. Requests bind to that session and an increasing sequence, carry a
flight-monotonic execution deadline, and pass argument validation before entering
the bounded acquisition dispatcher. Acquisition checks expiry again before the
device effect. A reconnect invalidates queued commands. Flight accepts command
roles only when `--authorized-command-server ground.orbital.test` is explicitly
configured and the verified server certificate contains that exact DNS SAN.
Omitting that option permits telemetry but rejects commands.

Start ground with `--control-socket /path/to/private-directory/control.sock`.
The directory must be owned by the current user with mode 0700; the socket is
created with mode 0600. Submit one local operator request:

```sh
.venv/bin/python -m ground.orbit_commands --socket /path/to/private-directory/control.sock reset
.venv/bin/python -m ground.orbit_commands --socket /path/to/private-directory/control.sock set-period --argument 50
.venv/bin/python -m ground.orbit_commands --socket /path/to/private-directory/control.sock query-state
```

Available operations are `enable`, `disable`, `set-period` (1–60000 milliseconds),
`reset`, `clear-fault`, and the read-only `query-state`. Native operations change the deterministic fake-device
state; they do not control hardware. Ground stores received completions in SQLite
`command_results`. `applied` means acquisition performed the operation, while
rejections have no device effect. A timeout or disconnect yields `outcome_unknown`:
the effect may have occurred before its result was lost. Neither flight nor ground
automatically replays an ambiguous request. Operators must inspect current state
before deliberately submitting another command. `query-state` reports enabled
state, period, generation, fault state, effect count, and flight monotonic time.
It requires the configured command role and adds no device effect. If connectivity
prevents obtaining a snapshot, keep the outcome unknown.

The native threat model protects telemetry and control from unauthenticated
network peers, spoofed server identities, trusted but unauthorized roles, stale
sessions, replayed sequences and late or invalid operations. It trusts the host
OS, local user account, configured CA and private-key storage. A compromised
operator account or an authorized ground endpoint remains able to command the
fake device. Development credentials and deterministic emulation provide no
hardware attestation or production key-management guarantees.

The security tests exercise certificate validity on both peers, foreign-CA
clients, explicit credential requirements, plaintext rejection and cancellation
of stalled TLS handshakes. Command tests use real mutual TLS between Python and
the C++ service, including the protected local operator CLI and SQLite result
persistence. Embedded driver and QEMU command validation remain later milestone
gates.
