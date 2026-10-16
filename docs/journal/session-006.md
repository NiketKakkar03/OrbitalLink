# Session 006 — interacting sensors and measurable delivery

Snapshot recorded 2026-10-15 on `feature/multisensor-validation`. The approved
baseline is ten minutes at ten routine reports/sec, plus bounded events.

Power, thermal, simplified orientation, health, synthetic radiation and events
share a deterministic tick/seed model. Eclipse affects power and heater state;
pointing disturbances pause science. Typed wire records preserve each category's
units, schema and quality. Legacy type 1 and ABI1 remain supported.

The custom QEMU hub and separate IRQ-driven ABI2 Linux driver implement latched
records, bounded hardware/software FIFOs, urgent reserves, reset generations,
clock calibration and atomic counter/event snapshots. Actual installed-QEMU
QTest runs passed. Driver review found a rebind accounting discontinuity;
persisted delivery/drop counters and explicit removal losses fix it. The updated
guest contract tests unbind/rebind while a file is open.

Flight adds bounded windows, exact out-of-order ACK matching and category-based
fair scheduling. Retained payloads remain immutable. Optional ACK batches commit
only already-arrived matching receipts in one EXTRA SQLite transaction; acquisition
still commits each sample individually. Valid ACK prefixes commit before a later
bad receipt forces reconnection. Unit and real-process tests cover both batch
sizes, rollback, disconnects, duplicates and unknown identities.

The first native 600-second run stored all 6,060 records with zero outbox drops
and source-to-ACK p95 at most 13 ms. A separate 30-second native 100 Hz observation
stored 3,030 records with zero outbox drops and p95 at most 27 ms. Exact executed
binary identities are retained; these earlier runs are not attributed to later
revisions. A clean source archive passed the native suite, and a clean checkout
passed a short benchmark. See [sensor validation](../sensor-validation.md).

The first short embedded 100 Hz attempt exposed a real limit: per-record FULL
storage transactions under QEMU TCG could not keep up. It reported 15 outbox drops
and source-to-ACK p95 around four seconds. This is retained as a failed performance
observation, not a passing target. A short embedded 10 Hz run delivered all 121
records, p95 at most 190 ms, with complete clock coverage and drift within bounds.
The complete embedded 600-second gate subsequently passed all 6,060 reports,
zero source/outbox drops, p95 at most 48 ms, consistent health snapshots and clock
coverage. Its 61 RSS measurements passed the tightened memory oracle. The
corrected native 100 Hz repeat also passed all 3,030 reports and consistent health
snapshots, p95 at most 9 ms. Exact artifacts and the remaining batching/crash
comparisons are tracked in the validation document.

The final EXTRA-synchronous runs close the approved task. Native delivered all
6,060 reports with source-to-ACK p95 <=34 ms. The actual QEMU/driver path also
delivered all 6,060, with p95 <=389 ms, zero drops or retained records, bounded
memory and valid clock calibration. Three abrupt SIGKILL/reboot cycles preserved
every committed row and replayed byte-identical records. Bounded receipt batching
made the stronger durability setting practical without waiting to fill a batch.
The optional embedded 100 Hz comparison retained its backlog safely but exceeded
the emulated platform's capacity, so 10 Hz remains the supported baseline.

Overload verification uses a deterministic first-ACK barrier rather than assuming
a particular SSD speed. A finite shutdown drain may leave committed telemetry or
loss notices; subsequent recovery must empty both and reconcile ground/durable
counts. Disk corruption and physical power-loss durability are separate limits;
a clean restart alone does not test an abrupt guest power cut.

This work does not complete the entire M8/M9 roadmap. Device command/state
coverage, sensor-specific fault controls, broader embedded failure tests,
dependency security auditing, project license selection and deployment/key/archive
operations remain explicit work. No release was published and no Mac firmware
changes are required.
