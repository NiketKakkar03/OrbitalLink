# ARM64 Linux guest on Apple Silicon

macOS remains the editor/native test host. Docker Desktop runs an ARM64 Linux
builder; its Linux-built QEMU runs inside that builder with TCG software
emulation. No Mac firmware modification or nested hardware virtualization is
required. Kernel modules are loaded only in the QEMU guest.

## Reproduce the builder

Start Docker Desktop, then run from the repository root:

```sh
docker build --platform linux/arm64 -t orbit-builder:2025.02.18 guest/container
docker volume create orbit-buildroot-arm64
docker run -d --name orbit-build --platform linux/arm64 --cpus 2 --memory 6g \
  --mount type=volume,src=orbit-buildroot-arm64,dst=/work \
  --mount type=bind,src="$PWD",dst=/input,readonly \
  orbit-builder:2025.02.18 sh /input/guest/container/build.sh
docker logs --tail 30 orbit-build
```

The source is copied into Linux storage; large sources, downloaded archives,
compiler objects and artifacts stay in the named volume. The container has no
privileged/device access. The first build compiles the toolchain, kernel, QEMU,
OpenSSL, SQLite and flight, so allow substantial time and disk space. Two jobs
and a 6 GiB memory limit are conservative defaults, not a measured minimum.
After a failed build, `docker start orbit-build` recopies current project
sources and resumes cached packages. Inspect the error first. Do not remove
the volume: it contains persistent guest state as well as caches.

## Version audit and gate status (2026-10-14)

Docker Desktop was started and reported server 28.1.1, Linux, aarch64. The
ARM64 builder was built successfully and its Linux tool preflight passed.
Buildroot configuration succeeded; full image compilation is underway.
Guest boot, TLS telemetry and driver runtime remain unverified until executed.

The supported Buildroot 2025.02.18 LTS release was selected from the
[official download page](https://buildroot.org/download.html), with exact Git
commit in `guest/versions.json`. Kernel 6.12.27, GCC 13.4.0 and QEMU 9.2.0
match that revision's upstream metadata and hashes. These are audited
development compatibility pins, not a claim that the kernel/QEMU are the latest
security patch levels. Updating and auditing those patches is a release gate.
The Ubuntu base image uses a digest; apt package versions and their source
repositories still evolve, so the builder is not yet bit-for-bit reproducible.

## Persistent guest state

`guest.py run` and `smoke.py` create a separate 128 MiB ext4 state disk once;
an existing disk is never truncated. The root disk uses a temporary snapshot,
while the state disk explicitly disables snapshots. The smoke mounts `/dev/vdb`
at `/var/lib/orbit` and supplies `--outbox /var/lib/orbit/outbox.db`.
Only one QEMU instance may use that disk at a time. Shut down cleanly before
copying it for backup. A snapshot of the root disk alone does not preserve the
outbox. Two-boot replay and abrupt-power-loss tests remain required.

The prepared Linux-only gate runner is:

```sh
python3 guest/run_gate.py --work /work/guest --output /work/evidence/m5-run-001
```

It boots twice: first retaining seven unsent samples, then delivering those
seven plus 100 new samples through mTLS. It checks the ground database has 107
rows with distinct boot identities containing seven and 100 samples. Each new
evidence directory receives its own state disk, reused by those two boots;
operator state is preserved. It runs QEMU and ground in one Linux network
namespace. Successful execution, rather than existence of this runner, clears
the gate. Abrupt power-loss recovery is a separate test.

Successful builds write image hashes, compiler identity, ELF architecture,
source/config hashes and version pins to
`/work/guest/output/images/orbit-manifest.json`. Serial smoke artifacts contain
no private key material. Start ground in the same Linux container/network
namespace so QEMU's `10.0.2.2` alias reaches it; a ground server on macOS instead
needs separately verified Docker-to-host networking.
