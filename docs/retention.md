# Durable flight retention

Normal flight builds require `--outbox PATH`; the directory must already exist.
Use a dedicated SQLite file on local storage and protect its directory according
to the deployment's telemetry confidentiality requirements. One service owns the
outbox at a time through an exclusive nonblocking file lock.

`--outbox-bytes` accepts 256 KiB through 1 GiB and defaults to 64 MiB. The budget
includes an allowance for the database and its live rollback journal, not just
telemetry payloads. SQLite's database page cap is:

```text
floor((budget_bytes - 65536) / 3 / 4096) * 4096
```

The remaining allowance covers rollback journal page entries and headers.
Journal mode is DELETE, with synchronous FULL and macOS fullfsync enabled; WAL
growth is not part of this design. Another 32 KiB within the database allowance
reserves schema, counters, and bounded loss metadata. A conservative record bound
is derived from the remaining database capacity at 256 bytes per sample.
`--capacity N` can impose a smaller explicit record bound. The default 64 MiB
budget yields an initial bound of 87,168 records, about 2 hours 25 minutes at
10 Hz when empty; this is a configured conservative bound, not a measured
maximum throughput or guaranteed outage duration.
Startup refuses a record limit below the existing backlog, or a byte budget below
the existing database's page footprint. Drain the backlog before reducing limits.
Persistent SQLite capacity failure is retried with at most eight transactions
and a maximum batch of 64 evictions, then fails without committing partial loss.

Acquisition commits each sample before it can be sent. Ground commits telemetry
before ACKing it; flight deletes a retained record only after a matching ACK.
Restart retransmits unchanged old identities and payloads. New acquisition uses
a new random boot identity, and production rejects manual boot overrides.

At capacity, a transaction discards the oldest unclaimed samples, records their
loss, and inserts the new sample. The claimed in-flight sample remains protected.
An artificially tiny capacity of one can therefore stop acquisition if that
record is still awaiting an ACK; configure at least two records to retain a new
sample while protecting an in-flight record. Capacity exhaustion that cannot be
resolved, metadata exhaustion, or storage errors fail explicitly.

Loss reports coalesce adjacent unsent ranges within one boot. Once claimed,
their identity and payload remain immutable across retries and restarts. There
are at most 63 detailed reports and two bounded aggregate summaries; aggregates
retain exact omitted counts and mark range detail incomplete. Ground records and
deduplicates a loss report before ACKing it. Flight removes the report after the
matching ACK. Unacknowledged reports remain committed at shutdown.

Persistent counters satisfy:

```text
durable_generated = durable_acknowledged + durable_dropped + durable_retained
```

These count committed samples across all boots of this outbox. The separate
current-run generated counter can include a failed persistence attempt; a
storage-failure exit must not be interpreted as ordinary successful conservation.
`durable_loss_acknowledged` counts lost samples already reported durably to ground.

Tests cover transaction rollback, exclusive ownership, process crash/reopen,
SQLite page-cap exhaustion, protected in-flight records, frozen loss identities,
telemetry ACK loss, and report ACK loss. SQLite page-cap failure is distinct from
an actual physically full disk. The tested shutdown deadline assumes responsive
OS storage operations; a blocked kernel/filesystem cannot be made interruptible
by the SQLite timeout. Process-crash recovery does not guarantee recovery from
disk destruction, filesystem corruption, or storage that ignores synchronization.

The read-only operator `query-state` reports the native fake-device settings and
effect count without reapplying a command. Device-mode state querying is rejected
until a truthful driver state UAPI is implemented; see [operator setup](security.md).
