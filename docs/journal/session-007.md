# Session 007: embedded fault and release gates

M8 added seven deterministic category-specific hub faults and the ABI2 control
needed to configure and query them. Actual QEMU qtests passed the complete mode
matrix. The rebuilt ARM64 guest passed the corrected driver contract and short
authenticated device-to-ground regression. Flight now watches the persistent
driver drop counter and records positive deltas as durable aggregate loss reports;
unit tests prove coalescing, claiming and acknowledgement.

Prior evidence already covers the 600-second 10 Hz embedded baseline, three
abrupt power cuts, network replay faults, simulated storage FULL rollback,
hot-journal recovery, bounded stress and the unsupported 100 Hz capacity result.
Together these close M8 for the declared synthetic/QEMU scope. Physical media
failure and live migration remain separate platform campaigns.

M9 adds the project license selection, component license declarations and an
operator runbook for clean candidate validation, evidence review, database
retention and recovery, certificate lifecycle, upgrade/rollback and release
notes. No tag or public release is created by these changes.
