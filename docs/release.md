# Release operations

OrbitalLink's supported embedded profile is ARM64 Buildroot under the pinned
QEMU machine at ten routine reports per second. Apple silicon runs Docker and
QEMU user-space tools; it does not load the Linux driver into macOS and requires
no Mac firmware modification. The 100 Hz embedded observation is a capacity
measurement, not a supported real-time profile.

## Candidate gate

Start from a clean checkout of the candidate commit and record `git rev-parse
HEAD`. Run `make check`, `make test-sanitizers`, `make test-security-sanitizers`
and the macOS-supported `make test-tsan`. Build the pinned guest, run
`qemu/test_mmio.py`, then run `guest/run_m7_gate.py` at 6,000 ticks, 100 ms and
storage batch 16. Retain the generated result, image manifest, executable hashes,
serial logs and resource samples. Review every failure artifact; never replace
or reuse a writable state disk between attempts.

The evidence bundle may contain public manifests and logs after review. Exclude
private keys, certificates containing private material, databases, state disks,
Docker volumes and build trees. Generate checksums for the reviewed bundle and
verify it from a second clean checkout before creating a tag.

## Ground retention and recovery

Monitor database and filesystem usage against the configured retention period.
Quiesce the receiver before a filesystem-level copy, or use SQLite's online
backup API. Encrypt backups, keep at least one independent copy, regularly test
restoration into a temporary database, and record the newest telemetry identity
before and after restoration. Archive or delete data only under an operator's
documented retention policy. The flight outbox is bounded outage protection and
cannot replace ground archives.

## Certificates

Create the CA offline, issue distinct flight and ground identities, restrict
private-key permissions, and distribute only the trust material each endpoint
needs. Rotate by adding the new trust chain before deploying new leaf certificates,
then remove the old chain after every endpoint has moved. Treat a compromised
identity as revoked: remove its authorization, deploy updated trust material and
restart affected services. Never package operational keys in an image or release.

## Upgrade and rollback

Back up and restore-test the ground database before upgrading. Deploy ground
support for existing and new wire types first, then flight images. Keep the prior
signed image and configuration available. Roll back the binary and configuration
together if health, loss, latency or storage alarms fail their acceptance limits;
preserve the database and evidence for diagnosis. Database migrations must remain
forward-readable by the deployed ground version.

## Demonstration and release decision

Run `make demo-native` for a short local authenticated-path demonstration, then
use the full candidate gate for release evidence. A release note must state the
tested commit, dependency/image identities, 10 Hz support limit, synthetic sensor
model, unsupported live migration and absence of physical mission qualification.
Creating or pushing a version tag and publishing assets is an explicit operator
decision after the candidate evidence has passed review.
