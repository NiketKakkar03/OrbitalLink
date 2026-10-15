# OrbitalLink Protocol Specification

Status: implemented through M2
Version: 1

## 1. Purpose

OrbitalLink messages are sent between the flight service and the ground station.

The flight service is the spacecraft-side program.

The ground station is the Earth-side program.

This file defines how both sides turn telemetry data into bytes and then back into telemetry data.

This process has two names:

- Encoding: fields to bytes
- Decoding: bytes to fields

## 2. Basic Rules

All multi-byte integer fields use network byte order.

Network byte order means big-endian.

Big-endian means the most important byte comes first.

Example:

```text
Decimal 42 as a 4-byte unsigned integer:

00 00 00 2A
```

The maximum payload length is 4096 bytes.

If a message breaks one of these rules, the receiver must reject it.

## 3. Message Shape

Every OrbitalLink message has two parts:

```text
[ 28-byte header ][ payload bytes ]
```

The header is like a shipping label.

The payload is the actual message body.

## 4. Header Layout

The header is exactly 28 bytes.

| Offset | Size | Field | Meaning |
|---:|---:|---|---|
| 0 | 4 | magic | Must be ASCII `OLNK` |
| 4 | 1 | version | Must be `1` |
| 5 | 1 | message_type | What kind of message this is |
| 6 | 2 | flags | Must be `0` for now |
| 8 | 2 | header_length | Must be `28` |
| 10 | 2 | reserved | Must be `0` |
| 12 | 4 | payload_length | Number of payload bytes |
| 16 | 8 | message_sequence | Sequence number for this frame |
| 24 | 4 | reserved | Must be `0` |

Total:

```text
4 + 1 + 1 + 2 + 2 + 2 + 4 + 8 + 4 = 28 bytes
```

## 5. Message Types

| Value | Name | Meaning |
|---:|---|---|
| 1 | TELEMETRY_SAMPLE | A telemetry measurement from flight to ground |
| 2 | TELEMETRY_ACK | Ground confirms a telemetry sample was stored |
| 3 | COMMAND_SESSION | Flight announces connection-bound command context |
| 4 | COMMAND_REQUEST | Ground asks flight to do something |
| 5 | COMMAND_RESULT | Flight reports applied or rejected command |

M2 implements `TELEMETRY_SAMPLE` and `TELEMETRY_ACK`.

## 6. TELEMETRY_SAMPLE Payload

The telemetry sample payload is exactly 44 bytes.

| Offset | Size | Field | Type | Meaning |
|---:|---:|---|---|---|
| 0 | 4 | spacecraft_id | unsigned 32-bit | Which spacecraft/system sent this |
| 4 | 8 | flight_boot_id | unsigned 64-bit | Identity for this flight-service run |
| 12 | 8 | telemetry_sequence | unsigned 64-bit | Telemetry sample number |
| 20 | 4 | device_generation | unsigned 32-bit | Changes when the device resets |
| 24 | 4 | device_sequence | unsigned 32-bit | Sensor sample number |
| 28 | 8 | timestamp_monotonic_ns | unsigned 64-bit | Monotonic timestamp in nanoseconds |
| 36 | 4 | temperature_millideg_c | signed 32-bit | Temperature in milli-degrees Celsius |
| 40 | 4 | voltage_millivolts | unsigned 32-bit | Voltage in millivolts |

Total:

```text
4 + 8 + 8 + 4 + 4 + 8 + 4 + 4 = 44 bytes
```

## 7. TELEMETRY_ACK Payload

The telemetry acknowledgement payload is exactly 20 bytes.

| Offset | Size | Field | Type | Meaning |
|---:|---:|---|---|---|
| 0 | 4 | spacecraft_id | unsigned 32-bit | Identifies the spacecraft whose sample was stored |
| 4 | 8 | flight_boot_id | unsigned 64-bit | Identifies the flight-service run that produced the sample |
| 12 | 8 | telemetry_sequence | unsigned 64-bit | Identifies the stored sample within that run |

These three fields are the durable identity of a telemetry sample. An ACK is sent only after the
sample's database transaction commits. The same ACK may be sent for a duplicate sample that is
already stored. A flight sender must match all three fields before treating its sample as stored;
the header `message_sequence` alone is not a durable sample identifier.

Total:

```text
4 + 8 + 8 = 20 bytes
```

## 8. Temperature Units

Temperature is stored as milli-degrees Celsius.

That means:

```text
25.125 C = 25125 milli-degrees Celsius
-5.250 C = -5250 milli-degrees Celsius
```

We use integers instead of decimal numbers because integers are easier to encode exactly.

## 9. Voltage Units

Voltage is stored as millivolts.

That means:

```text
3.300 V = 3300 millivolts
```

## 10. Golden Telemetry Sample

This is the first sample both C++ and Python must understand.

| Field | Value |
|---|---:|
| spacecraft_id | 1 |
| flight_boot_id | 0x0102030405060708 |
| telemetry_sequence | 42 |
| device_generation | 7 |
| device_sequence | 99 |
| timestamp_monotonic_ns | 1234567890 |
| temperature_millideg_c | -5250 |
| voltage_millivolts | 3300 |

The header values for this sample are:

| Field | Value |
|---|---:|
| magic | `OLNK` |
| version | 1 |
| message_type | 1 |
| flags | 0 |
| header_length | 28 |
| payload_length | 44 |
| message_sequence | 1 |

## 11. Golden Telemetry ACK

The golden ACK confirms storage of the golden sample above.

| Field | Value |
|---|---:|
| spacecraft_id | 1 |
| flight_boot_id | 0x0102030405060708 |
| telemetry_sequence | 42 |
| header message_sequence | 2 |

Its complete encoded frame in hexadecimal is:

```text
4f4c4e4b01020000001c000000000014000000000000000200000000000000010102030405060708000000000000002a
```

## 12. Rejection Rules

A receiver must reject the message if:

- magic is not `OLNK`
- version is not `1`
- message_type is unsupported
- flags is not `0`
- header_length is not `28`
- reserved fields are not `0`
- payload_length is greater than `4096`
- payload_length does not match the expected payload size for the message type
- the input ends before the full header arrives
- the input ends before the full payload arrives

## 13. M1 Completion Goal

M1 is complete when:

- C++ can encode and decode the golden sample
- Python can encode and decode the golden sample
- C++ and Python agree on the exact bytes
- malformed messages are rejected
- split input chunks can be parsed correctly

## M4 command wire contract

All command integers use network byte order. Flight announces exactly one session
per authenticated TLS connection. Its unpredictable nonzero 64-bit session token
is invalidated on disconnect and bound to that connection's exact certificate SAN
role and current boot. The operator role is explicitly configured with
`--authorized-command-server`; omission denies commands. Observer TLS peers may
receive telemetry without that role.

| Frame | Payload fields in order | Bytes |
|---|---|---:|
| COMMAND_SESSION | session u64, flight_boot_id u64, flight_monotonic_ns u64 | 24 |
| COMMAND_REQUEST | session u64, sequence u64, command_id u64, deadline_ns u64, opcode u32, argument u32 | 40 |
| COMMAND_RESULT | session u64, sequence u64, command_id u64, status u32, reserved zero u32 | 32 |

Sequences must be nonzero and strictly greater than the last accepted sequence;
rejected requests do not advance the accepted sequence. Requests carry an absolute
flight-monotonic deadline, strictly future and at most five seconds ahead at
admission. The acquisition owner checks expiry again immediately before applying
an effect. Header sequence is correlation; the payload sequence enforces replay
policy. Maximum combined queued work/results is 16, returning queue_full without
an effect. Network dispatch waits for acquisition completion before transmitting
an applied result; acceptance itself is never reported as completion.

Opcodes: 1 enable; 2 disable; 3 set-period (argument 1..60000 milliseconds);
4 reset (increment generation and restart device sequence); 5 clear-fault.
Other opcodes and nonzero arguments for 1,2,4,5 are invalid. The simulated source
supports development-only `--fake-fault 1` for observing clear-fault through
physical-invalid telemetry. The flag is an explicit test configuration.

Statuses: 0 applied; 1 unauthorized; 2 wrong_session; 3 replay; 4 expired;
5 invalid_argument; 6 queue_full; 7 outcome_unknown. Reserved result bytes must
be zero. Ground persists received results in SQLite. Lost connection or timed-out
completion reports outcome_unknown locally, never replays the request, and drops
an unsent expired operator request. Telemetry ACK retry remains independent.

Local operator access uses `python -m ground.orbit_commands --socket PATH COMMAND`
with `--argument` for set-period and `--ttl-ms` (1..5000). Start ground with
`--control-socket PATH`; PATH must be absent within an owner-only directory
(mode 0700). The socket mode is 0600. Queue and input size are bounded. Existing
paths are never replaced. A finite flight --count completes acquisition and exits
after telemetry drain; use a suitably large count for an interactive session.

## Durable losses and read-only state (native mitigation)

Types 1–5 remain unchanged. Type 6 is a 64-byte correlated state snapshot,
returned only for accepted opcode 6 (`query-state`, argument zero). Its network
byte order fields are session/command sequence/command ID (u64), status/reserved/
enabled/period_ms/generation/fault (u32), effects/monotonic_ns (u64).
Status and reserved are zero; enabled and fault are boolean. Expired, unauthorized,
or otherwise rejected queries receive ordinary type-5 results. Queries use the
same session, deadline, authorization and replay gates but never increment device
effects. A snapshot describes execution time, not future state; it resolves
uncertainty by observation and never automatically replays an earlier command.

Type 7 is a 48-byte loss report: spacecraft ID (u32), boot ID/report ID/first
sequence/last sequence/count (u64), reason (u32). Reason 1 denotes exact retention
loss within one boot: first is positive, last >= first, count is positive and no
larger than the inclusive span. Reason 2 denotes an exact count whose per-boot
range detail was omitted to keep metadata bounded: boot/first/last must be zero.
Report IDs are positive and immutable after being selected for delivery. Ground
identifies reports by (spacecraft, boot, report ID), rejects changed payloads
under an existing identity, and commits both report and event before sending ACK.
An identical duplicate receives the same ACK without another loss event.

Type 8 is a 20-byte loss ACK: spacecraft ID (u32), boot ID/report ID (u64).
Flight deletes only a matching durable loss record after this ACK. Lost ACKs cause
retransmission; loss-report receipt never counts as telemetry sample receipt.
