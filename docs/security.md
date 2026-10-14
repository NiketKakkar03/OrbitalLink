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
