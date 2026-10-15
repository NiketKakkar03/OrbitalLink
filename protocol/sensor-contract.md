# Multisensor contract

Approved scope: power, thermal, simplified orientation, synthetic radiation;
health and bounded events mandatory; interacting deterministic scenarios. Baseline
10 total routine reports/sec for 600 seconds (not 30 minutes), additional bounded
events; performance target 100 reports/sec, measured not presumed.

Backward compatibility: legacy type1 remains unchanged. New frame type9 payload
76 bytes: spacecraft u32, flight boot u64, telemetry sequence u64, generation u32,
device sequence u32, monotonic ns u64 (36 bytes), category u16, schema u16=1,
quality u32 (0 valid; bit0 sensor fault), eight signed i32 values (32 bytes).
Unused values must be zero. Category determines units and priority, not an
untrusted arbitrary priority byte. Telemetry ACK identity remains unchanged.
C++ TelemetrySample adds category=0/schema=1/quality=0/values[8] defaults after
legacy fields; encode_telemetry_sample emits type9 iff category!=0. Decoding type9
fills compatibility temperature/voltage=0 unless relevant values supply them.
Python dataclass adds equivalent defaults. Parser/storage accept both variants.

Categories/slots (remaining zero):
1 power: voltage_mV,current_mA,charge_permille,solar_mW,load_mW,eclipse_bool.
2 thermal: battery_mC,electronics_mC,heater_bool.
3 orientation: x_mdeg_s,y_mdeg_s,z_mdeg_s,pointing_error_mdeg,payload_paused_bool.
4 health: uptime_seconds,reset_count,source_queued,source_generated_mod2_31,
source_delivered_mod2_31,source_dropped_mod2_31.
5 science: radiation_count,integration_ms,paused_bool.
6 event: event_code,severity(0info1warning2critical),related_category,repeat_count.
Event codes1 eclipse_enter,2 eclipse_exit,3 low_battery,4 thermal_high,
5 pointing_disturbance,6 pointing_recovered,7 reset,8 sensor_fault.

Priority class0 urgent category6; class1 operational categories1,2,3,4 and legacy;
class2 bulk category5. Bounded urgent reserve, bounded repetitive-event coalescing,
weighted service to avoid starving either health or bulk; claimed rows immutable.
Acquisition telemetry_sequence is global within flight boot across categories;
device sequence global across source reports. Legacy sample semantics unchanged.

M7 ABI2: new separate struct orbit_sensor_sample (do not alter ABI1) common
abi_version u32,category u32,generation u32,sequence u32,monotonic_ns u64,
quality u32,schema u32,values[8]i32 =64 bytes. New device path and driver/source
mode may use separate ABI2 read interface while preserving /dev/orbit_telemetry.
The implemented [register/IRQ contract](../qemu/README.md) uses one custom QEMU
multisensor hub with categories, rather than six independent QEMU models.
No physical-accuracy or CCSDS-conformance claim.

Normal profile per second: power2 thermal1 orientation5 health1 science1.
Clocked deterministic seed/scenario controls. Health counters at source are
modular snapshots; run artifacts MUST retain exact64bit software counters,
queue/storage statistics,retry/ACK delay,reset/fault events,resource measurements.
Baseline target no source/outbox drops in healthy declared profile, exact
accounting,95th percentile acquisition-to-durableACK <=1sec measured guest clock.
Fault outcomes/losses must be accounted, not hidden to satisfy no-loss assertion.
