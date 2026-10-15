# Multisensor validation and release preparation

Approved profile: power, thermal, simplified orientation, synthetic radiation;
health and events mandatory. A single coherent QEMU hub and separate ABI2 driver
carry the same typed records as the native scenario source. Legacy ABI1 and wire
type 1 remain compatible. See [sensor contract](../protocol/sensor-contract.md)
and [MMIO/IRQ interface](../qemu/README.md).

## Baselines and how to reproduce

The baseline is **600 seconds**, ten routine reports/sec plus bounded events:
power 2, thermal 1, orientation 5, health 1, science 1. The first full native run
produced 6,060 records: 1,200 power, 600 thermal, 3,000 orientation, 600 health, 600 science,
60 events. Interactions include eclipse/power/heater changes, pointing disturbance
and payload pause, low battery and electronics overheating. Values are synthetic;
this is not a calibrated physical spacecraft model.

```sh
make check
python3 integration/multisensor_benchmark.py --output build/multisensor/new-baseline --seconds 600 --rate 10
python3 integration/multisensor_benchmark.py --output build/multisensor/new-performance --seconds 30 --rate 100
```

Use the configured virtualenv interpreter if dependencies are not global. Build
`/tmp/orbit_flight_service` through `make test-cpp` before running the benchmark.
Newer benchmark runs copy/hash their executable before launch so another build
cannot change the executed artifact. Credentials stay in a private ignored
subdirectory; do not publish that directory as a release artifact.

After building the patched image using [guest setup](guest.md), run inside the
Linux builder, with a fresh output directory for every attempt:

```sh
python3 qemu/test_mmio.py --qemu /work/guest/output/host/bin/qemu-system-aarch64 --output /work/evidence/new-qtest
python3 guest/run_m7_gate.py --work /work/guest --output /work/evidence/new-baseline --ticks 6000 --period-ms 100 --seed 1 --storage-batch 1
```

The guest runner first checks the actual driver contract, including rebind,
then boots its separate baseline state disk. Native and guest resource clocks
and evidence are kept separate. Never reuse another run's writable state disk.

Acceptance: exact durable sample identity/count reconciliation, all categories,
all expected events, no source/outbox drops under the healthy baseline, 95th
percentile source-to-durable-ACK <=1 second, complete latency sample coverage and
bounded memory. Memory check compares first/last-quarter means with a 32 MiB bound;
it is a finite-run guard, not proof that no leak can occur indefinitely.

Record throughput in reports/sec and application bytes/sec separately. The
`wire_application_bytes_sent` counter counts telemetry frames, including retries;
it excludes control/ACK frames and TLS/TCP/IP overhead. ACK/source latency histograms use fixed 1 ms
buckets; reported percentile is a conservative bucket bound. Samples beyond the
histogram range are explicitly counted and an overflowing percentile is null.
Replay from earlier boots is excluded from current-source latency calculations.

The guest driver calibrates QEMU virtual timestamps into Linux monotonic time;
initial/current offset and bracket uncertainty are recorded. Host wall time is
not subtracted from guest timestamps to invent a latency number. Clock coverage
and drift must pass before accepting embedded source-to-ACK measurements.

## Current native measurements

Local artifacts are ignored by Git and are not a published release archive.
These results are workload/machine observations, not universal performance claims.
The first 600 second baseline used the initial integrated native scenario binary;
its exact executable hash is in `execution-identity.json`, with dirty-source
status recorded. Subsequent refinements are validated by regression and guest
runs rather than retroactively attributed to that binary.
Those initial native runs predate the health snapshot conservation check: the
native source omitted its one staged record from the queued field. The source
and validator are now corrected. Their row-count and latency observations remain
useful, but they are not evidence that the old health counters were consistent.

| Scenario | Artifact | Result |
|---|---|---|
|600 seconds,10 routine Hz|`build/multisensor/baseline-600s-001/result.json`|6,060 stored;0 outbox drops;source-to-ACKp95<=13 ms;no measured memory growth|
|30 seconds,100 routine Hz|`build/multisensor/performance-100hz-001/result.json`|3,030 stored;0 outbox drops;source-to-ACKp95<=27 ms|
|30 seconds,100 routine Hz, corrected health counters|`build/multisensor/performance-100hz-final-001/result.json`|3,030 stored; zero drops; source-to-ACK p95 <=9 ms; exact health/category checks pass|
|600 seconds,10 routine Hz, corrected health counters, pre-directory-sync fix|`build/multisensor/baseline-600s-final-001/result.json`|6,060 stored; zero drops; source-to-ACK p95 <=12 ms; exact health/category/resource checks pass|
|Slow durable TLS receiver versus fast producer|`ground/test_load_e2e.py`|Capacity 32/window 8 stays bounded; intentional drops and their durable loss ACK counts reconcile; retained rows recover after the finite drain deadline|

The 100 Hz result is a 30 second performance observation; it is not a 600 second
100 Hz endurance gate. Baseline speed is 10 Hz. Device results must be reported
separately from the native scenario source.

## Embedded measurements on the M1 Pro setup

The actual custom QEMU hub → IRQ driver → flight → mutual-TLS ground path passed
the full 600-second, 10 Hz baseline in
`build/guest-evidence/m7-baseline-001/result.json`. It stored and acknowledged all
6,060 reports, with the same category counts as the native baseline and zero
driver/outbox drops or retained records. Source-to-durable-ACK p95 was at most
48 ms, covering all 6,060 samples. Driver totals were 6,060 generated, enqueued
and delivered, with zero queued at the final snapshot. All 60 transition events
were observed without event coalescing or overflow.

Clock drift was 9,681 ns against the declared 1,046,392 ns budget. The 61 guest
RSS measurements exceeded the required 48; final-versus-initial quarter mean
growth was about 0.55 MiB against the 32 MiB limit. Process peak RSS was
10,637,312 bytes. The tightened memory oracle also passed against the retained
raw log in `posthoc-memory-validation.json`. Host wall time was 614.61 seconds,
including boot and shutdown. Image and executable identities identify the
actual tested build; later native-only counter fixes are not attributed to it.

These baseline measurements used DELETE/FULL before the directory-sync fix;
they establish healthy-link behavior, not power-cut retention. The first actual
VM power-cut gate (`build/guest-evidence/m7-crash-001/`) exposed a missing last
commit: 115 records confirmed committed before SIGKILL, 114 after recovery.
SQLite documents this DELETE/FULL limitation; EXTRA additionally synchronizes
the journal-deletion directory. The failed artifact is preserved, and passing
recovery must be established separately after tightening the storage setting.
See [SQLite synchronous modes](https://www.sqlite.org/pragma.html#pragma_synchronous).

An earlier short 100 Hz guest attempt failed: 15 outbox drops and source-to-ACK
p95 around four seconds under per-record FULL storage transactions and TCG.
That failure remains in the evidence. A passing native 100 Hz result does not
establish the same embedded performance. Optional ACK batching is measured
separately; it does not remove acquisition's per-record commit cost.

## Transmission and priority behavior

`--send-window` accepts 1..64; default 1 preserves legacy behavior. The new profile
uses 32, allowing several immutable samples to await receipts. Each ACK must match
both its frame sequence and complete durable identity. Out-of-order valid ACKs
work; unknown/duplicate ACKs force reconnection without deleting unacknowledged
rows. Reconnect releases in-memory claims for replay. The window is bounded and
does not eliminate ground commit speed, filesystem latency or TCP head-of-line
blocking as bottlenecks.

`--priority-scheduling 1` requires durable storage. Category-derived scheduling
uses urgent event/operational/bulk weights 4:2:1, skipping empty classes. Reserved
urgent outbox capacity is min(32,capacity/8) where capacity>=8. All in-flight rows
remain protected. Existing backlog priorities are rederived atomically when
priority mode opens; payloads and counters are unchanged. Startup work is bounded
by the retained backlog, with 64-row memory batches.

Repetitive unpublished events use a fixed-eight-entry coalescer and repeat_count;
published/claimed payloads never change. Observation/coalescing/overflow counts
are separate from generated telemetry-report counts. QEMU and driver reserve
urgent FIFO slots. Priority service cannot promise delivery during permanent
storage or link failure; all exhausted capacity must remain explicit.

## Test matrix

| Boundary/failure | Implemented verification |
|---|---|
|Wire format/schema|Shared C++/Python 76-byte type 9 fixture, all categories, signed fields, reserved slots, bad schema/quality/bools, fragmented input|
|Old databases|SQLite migration, legacy fixtures/status preserved, typed metadata roundtrip|
|Window/reordering|Real C++ process sends 4 before first ACK; reverse-order receipts; exact identity matching|
|Connection faults|Disconnect replay, unknown/duplicate ACK rejection and valid-prefix commit with storage batches 1 and 16; legacy outage and lost telemetry/loss ACK tests retained|
|Priority/capacity|Weighted fairness, reserved urgent capacity, all-claimed protection, overload admission, mode-reopen atomic rollback|
|Source contracts|ABI2 whole 64-byte reads, invalid category 0, malformed/partial records, counter equations, supported real rate effects|
|Scenario interactions|Repeatable category mix, eclipse/power/thermal, orientation/payload pause, bounded repeated-event reports|
|Hardware/MMIO|Actual patched QEMU qtests for coherent latch, GIClevel IRQ mask/unmask, overflow, snapshot, rate/reset and complete scenario mix|
|Guest/endurance|Actual ABI2 module and custom hub require their own result artifacts;600 second gate distinct from native baseline|
|Security|Existing mutualTLS and command authorization/replay/deadline suites; no plaintext production fallback|

Still distinct from these checks: a complete physical-full-disk fault campaign,
sensor-specific stuck/bias/noise fault controls, long randomized stress, higher
rate embedded endurance, production key operations and custom-device live VM
migration. Guest process/power-loss tests must preserve committed typed backlog
and stable identities; a clean reboot alone is not an abrupt-power-loss test.

## Release preparation gates

The runtime correction at `5f50e65` passed Ubuntu and macOS native, sanitizer,
real-TLS sanitizer and supported macOS TSan checks in
[CI run 38793706847](https://github.com/NiketKakkar03/OrbitalLink/actions/runs/38793706847).
Guest evidence remains a separate local gate; CI does not execute QEMU endurance.

Before calling this a release:

- Finish and retain custom-device guest smoke/endurance and crash-recovery results.
- Run the native suite, ASan/UBSan, supported TSan and realTLS security checks for
  the final committed source; CI must pass on Ubuntu and macOS.
- Reproduce from a clean source checkout, with exact source/patch/executable/image
  hashes, commands, host configuration, scenario seed and failure artifacts.
- Keep build outputs, databases, state disks and private credentials out of Git;
  publish only reviewed non-secret evidence and intentional release assets.
- Audit kernel/QEMU/OpenSSL and builder dependencies for the release patch level;
  development compatibility pins are not a claim of current security patches.
- Complete dependency/license notices and choose an explicit project license;
  the repository currently has no top-level LICENSE selection.
- Define ground database archive/disk policy and certificate provisioning,
  rotation/revocation, backup/recovery and upgrade procedures.
- Document unsupported features and a reproducible demonstration. Do not label
  synthetic measurements as mission-qualified sensor physics or this custom
  protocol as CCSDS compliant.

No release tag or publication is implied by passing an individual benchmark.

Optional `--storage-batch 1..16` groups only already-arrived matching ACKs into
one EXTRA-synchronous deletion transaction. Default 1 preserves single-record
commit path. A batch never waits to become full. Unknown/duplicate ACKs retain
unacknowledged samples; a valid prefix commits independently before reconnect.
Acquisition still commits samples individually. The optimization does not imply
a measured embedded throughput improvement until its comparison gate passes.

Both endpoints need type 9 support for multisensor exchange; no automatic
downgrade discards category fields. Existing type 1 messages remain supported.
Database migrations preserve the legacy rows and full typed metadata.
