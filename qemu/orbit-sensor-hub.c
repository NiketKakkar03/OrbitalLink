/* SPDX-License-Identifier: GPL-2.0-or-later */
/* Synthetic multisensor source. No physical accuracy claim. */
#include "qemu/osdep.h"
#include "hw/sysbus.h"
#include "hw/irq.h"
#include "hw/qdev-properties.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "qemu/log.h"
#include "orbit_scenario.h"
#include "orbit_events.h"

#define TYPE_ORBIT_HUB "orbit-sensor-hub"
OBJECT_DECLARE_SIMPLE_TYPE(OrbitHub, ORBIT_HUB)
#define HUB_CAPACITY 64
typedef struct HubSample {
    uint32_t abi, category, generation, sequence;
    uint64_t ns;
    uint32_t quality, schema;
    int32_t values[8];
} HubSample;
struct OrbitHub {
    SysBusDevice parent_obj;
    MemoryRegion mmio;
    qemu_irq irq;
    QEMUTimer *timer;
    HubSample fifo[HUB_CAPACITY];
    uint32_t head, count, control, period_ms, seed, generation, sequence;
    uint32_t reset_count;
    uint32_t fault_category, fault_mode;
    int32_t fault_parameter;
    uint64_t fault_matches;
    int32_t last_values[5][8];
    bool have_last[5];
    uint64_t tick, generated, dropped, popped;
    uint64_t snapshot_generated, snapshot_dropped;
    uint64_t snapshot_ns;
    uint64_t driver_delivered, driver_dropped, snapshot_popped;
    uint64_t snapshot_driver_delivered, snapshot_driver_dropped;
    struct orbit_event_accumulator snapshot_events;
    uint32_t snapshot_queued, snapshot_generation, snapshot_resets;
    bool overrun;
    struct orbit_event_accumulator events;
};
static void hub_irq(OrbitHub *s)
{
    qemu_set_irq(s->irq, !!(s->control & 2) && s->count != 0);
}
static void hub_enqueue(OrbitHub *s, const struct orbit_scenario_record *record)
{
    uint32_t sequence = s->sequence++;
    s->generated++;
    if (s->count == HUB_CAPACITY || (record->category != 6 && s->count >= HUB_CAPACITY - 8)) {
        s->dropped++;
        s->overrun = true;
        return;
    }
    HubSample *sample = &s->fifo[(s->head + s->count++) % HUB_CAPACITY];
    *sample = (HubSample){.abi = 2, .category = record->category,
        .generation = s->generation, .sequence = sequence,
        .ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), .schema = 1,
        .quality = record->quality};
    memcpy(sample->values, record->values, sizeof(sample->values));
}
static void hub_flush_events(OrbitHub *s)
{
    struct orbit_scenario_record event;
    while (s->count < HUB_CAPACITY && orbit_event_pop(&s->events, &event)) {
        hub_enqueue(s, &event);
    }
}
static void hub_tick(void *opaque)
{
    OrbitHub *s = opaque;
    struct orbit_scenario_record routine, event;
    bool missing = false;
    if (!(s->control & 1)) {
        return;
    }
    bool has_event = orbit_scenario_tick(s->tick++, s->seed, &routine, &event);
    if (routine.category == 4) {
        routine.values[0] = (qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) / 1000000000LL) & INT32_MAX;
        routine.values[1] = s->reset_count;
        routine.values[2] = s->count + s->popped - s->driver_delivered - s->driver_dropped;
        routine.values[3] = s->generated & INT32_MAX;
        routine.values[4] = s->driver_delivered & INT32_MAX;
        routine.values[5] = (s->dropped + s->driver_dropped) & INT32_MAX;
    }
    if (routine.category >= 1 && routine.category <= 5) {
        unsigned index = routine.category - 1;
        if (routine.category == s->fault_category) {
            int64_t changed;
            uint64_t occurrence = s->fault_matches++;
            switch (s->fault_mode) {
            case 1: routine.quality |= 1; break;
            case 2:
                if (s->have_last[index]) {
                    memcpy(routine.values, s->last_values[index], sizeof(routine.values));
                }
                routine.quality |= 1;
                break;
            case 3:
                changed = (int64_t)routine.values[0] + s->fault_parameter;
                routine.values[0] = CLAMP(changed, INT32_MIN, INT32_MAX);
                routine.quality |= 1;
                break;
            case 4: {
                int64_t raw = s->fault_parameter;
                uint32_t amplitude = MIN((uint64_t)(raw < 0 ? -raw : raw), UINT64_C(1000000));
                uint32_t mixed = (uint32_t)(s->tick * UINT64_C(1103515245)) + s->seed + routine.category * 12345;
                int32_t noise = amplitude ? (int32_t)(mixed % (amplitude * 2U + 1U)) - (int32_t)amplitude : 0;
                changed = (int64_t)routine.values[0] + noise;
                routine.values[0] = CLAMP(changed, INT32_MIN, INT32_MAX);
                routine.quality |= 1;
                break;
            }
            case 5:
                s->sequence++; s->generated++; s->dropped++; s->overrun = true;
                missing = true; break;
            case 6:
                routine.values[0] = INT32_MIN; routine.quality |= 2; break;
            case 7:
                if (occurrence & 1) routine.quality |= 1;
                break;
            default: break;
            }
        }
        if (!(routine.category == s->fault_category && s->fault_mode == 2)) {
            memcpy(s->last_values[index], routine.values, sizeof(routine.values));
            s->have_last[index] = true;
        }
    }
    if (has_event) {
        orbit_event_push(&s->events, &event);
    }
    hub_flush_events(s);
    if (!missing) hub_enqueue(s, &routine);
    hub_irq(s);
    timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
              (uint64_t)s->period_ms * SCALE_MS);
}
static uint32_t sample_word(const HubSample *sample, unsigned word)
{
    switch (word) {
    case 0: return sample->abi;
    case 1: return sample->category;
    case 2: return sample->generation;
    case 3: return sample->sequence;
    case 4: return sample->ns;
    case 5: return sample->ns >> 32;
    case 6: return sample->quality;
    case 7: return sample->schema;
    default: return sample->values[word - 8];
    }
}
static uint64_t hub_read(void *opaque, hwaddr offset, unsigned size)
{
    OrbitHub *s = opaque;
    switch (offset) {
    case 0x00: return 0x4f534e32; /* OSN2 */
    case 0x04: return 2;
    case 0x08: return s->control;
    case 0x0c: return (s->count ? 1 : 0) | (s->overrun ? 2 : 0) |
                       ((s->count && (s->control & 2)) ? 4 : 0);
    case 0x10: return s->period_ms;
    case 0x14: return s->seed;
    case 0x20: return s->count;
    case 0x24: return HUB_CAPACITY;
    case 0x28: return s->generation;
    case 0x2c: return s->reset_count;
    case 0x30: return s->generated;
    case 0x34: return s->generated >> 32;
    case 0x38: return s->dropped;
    case 0x3c: return s->dropped >> 32;
    case 0x84: return s->snapshot_queued;
    case 0x88: return s->snapshot_generation;
    case 0x8c: return s->snapshot_resets;
    case 0x90: return s->snapshot_generated;
    case 0x94: return s->snapshot_generated >> 32;
    case 0x98: return s->snapshot_dropped;
    case 0x9c: return s->snapshot_dropped >> 32;
    case 0xa0: return s->events.count;
    case 0xa4: return s->events.observations;
    case 0xa8: return s->events.observations >> 32;
    case 0xac: return s->events.coalesced;
    case 0xb0: return s->events.coalesced >> 32;
    case 0xb4: return s->events.overflow;
    case 0xb8: return s->events.overflow >> 32;
    case 0xc0: return s->snapshot_ns;
    case 0xc4: return s->snapshot_ns >> 32;
    case 0xd0: return s->fault_category;
    case 0xd4: return s->fault_mode;
    case 0xd8: return (uint32_t)s->fault_parameter;
    case 0xe0: return s->driver_delivered;
    case 0xe4: return s->driver_delivered >> 32;
    case 0xe8: return s->driver_dropped;
    case 0xec: return s->driver_dropped >> 32;
    case 0xf8: return s->popped;
    case 0xfc: return s->popped >> 32;
    case 0x100: return s->snapshot_popped;
    case 0x104: return s->snapshot_popped >> 32;
    case 0x108: return s->snapshot_driver_delivered;
    case 0x10c: return s->snapshot_driver_delivered >> 32;
    case 0x110: return s->snapshot_driver_dropped;
    case 0x114: return s->snapshot_driver_dropped >> 32;
    case 0x120: return s->snapshot_events.observations;
    case 0x124: return s->snapshot_events.observations >> 32;
    case 0x128: return s->snapshot_events.coalesced;
    case 0x12c: return s->snapshot_events.coalesced >> 32;
    case 0x130: return s->snapshot_events.overflow;
    case 0x134: return s->snapshot_events.overflow >> 32;
    case 0x138: return s->snapshot_events.count;
    default:
        if (offset >= 0x40 && offset < 0x80 && s->count) {
            return sample_word(&s->fifo[s->head], (offset - 0x40) / 4);
        }
        return 0;
    }
}
static void hub_write(void *opaque, hwaddr offset, uint64_t value,
                      unsigned size)
{
    OrbitHub *s = opaque;
    switch (offset) {
    case 0x08:
        s->control = value & 3;
        timer_del(s->timer);
        if (s->control & 1) {
            timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                      (uint64_t)s->period_ms * SCALE_MS);
        }
        break;
    case 0x0c: if (value & 2) { s->overrun = false; } break;
    case 0x10:
        if (value >= 1 && value <= 60000) {
            s->period_ms = value;
            if (s->control & 1) {
                timer_mod(s->timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) +
                          (uint64_t)s->period_ms * SCALE_MS);
            }
        }
        break;
    case 0x14: if (!(s->control & 1)) { s->seed = value; } break;
    case 0x18:
        if (value == 1) {
            s->dropped += s->count;
            s->head = s->count = 0;
            s->generation++;
            s->reset_count++;
            s->sequence = 0;
            s->tick = 0;
            s->overrun = false;
            s->fault_category = 0;
            s->fault_mode = 0;
            s->fault_parameter = 0;
            s->fault_matches = 0;
            memset(s->have_last, 0, sizeof(s->have_last));
            for (unsigned i = 0; i < s->events.count; ++i) {
                s->events.overflow += s->events.pending[i].values[3];
            }
            s->events.count = 0;
            struct orbit_scenario_record event = {
                .category = 6, .values = {7, 0, 4, 1}
            };
            orbit_event_push(&s->events, &event);
            hub_flush_events(s);
        }
        break;
    case 0x1c:
        if (value == 1 && s->count) {
            s->head = (s->head + 1) % HUB_CAPACITY;
            s->count--;
            s->popped++;
            hub_flush_events(s);
        }
        break;
    case 0x80:
        if (value == 1) {
            s->snapshot_generated = s->generated;
            s->snapshot_dropped = s->dropped;
            s->snapshot_queued = s->count;
            s->snapshot_generation = s->generation;
            s->snapshot_resets = s->reset_count;
            s->snapshot_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
            s->snapshot_popped = s->popped;
            s->snapshot_driver_delivered = s->driver_delivered;
            s->snapshot_driver_dropped = s->driver_dropped;
            s->snapshot_events = s->events;
        }
        break;
    case 0xd0:
        if (value <= 5 && value != s->fault_category) {
            s->fault_category = value;
            s->fault_matches = 0;
            if (value) {
                struct orbit_scenario_record event = {
                    .category = 6, .values = {8, 1, value, 1}
                };
                orbit_event_push(&s->events, &event);
                hub_flush_events(s);
            }
        }
        break;
    case 0xd4:
        if (value <= 7) { s->fault_mode = value; s->fault_matches = 0; }
        break;
    case 0xd8:
        s->fault_parameter = (int32_t)value;
        break;
    case 0xf0:
        if (value <= s->popped - s->driver_delivered - s->driver_dropped) {
            s->driver_delivered += value;
        }
        break;
    case 0xf4:
        if (value <= s->popped - s->driver_delivered - s->driver_dropped) {
            s->driver_dropped += value;
        }
        break;
    default: break;
    }
    hub_irq(s);
}
static const MemoryRegionOps hub_ops = {
    .read = hub_read, .write = hub_write, .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {.min_access_size = 4, .max_access_size = 4, .unaligned = false},
    .impl = {.min_access_size = 4, .max_access_size = 4},
};
static void hub_init(Object *obj)
{
    OrbitHub *s = ORBIT_HUB(obj);
    s->period_ms = 100;
    s->generation = 1;
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, hub_tick, s);
    memory_region_init_io(&s->mmio, obj, &hub_ops, s, TYPE_ORBIT_HUB, 0x1000);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->mmio);
    sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq);
}
static void hub_finalize(Object *obj)
{
    OrbitHub *s = ORBIT_HUB(obj);
    timer_free(s->timer);
}
static const Property hub_properties[] = {
    DEFINE_PROP_UINT32("seed", OrbitHub, seed, 1),
    DEFINE_PROP_END_OF_LIST(),
};
static void hub_class_init(ObjectClass *klass, void *data)
{
    device_class_set_props(DEVICE_CLASS(klass), hub_properties);
}
static const TypeInfo hub_info = {
    .name = TYPE_ORBIT_HUB, .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(OrbitHub), .instance_init = hub_init,
    .instance_finalize = hub_finalize, .class_init = hub_class_init,
};
static void hub_register_types(void)
{
    type_register_static(&hub_info);
}
type_init(hub_register_types)
