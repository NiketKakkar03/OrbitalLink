#!/usr/bin/env python3
"""Apply the pinned QEMU9.2 virt extension; reject unfamiliar source anchors."""
from pathlib import Path
import shutil
import sys

source = Path(sys.argv[1])
repo = Path(__file__).resolve().parent.parent


def insert(path, anchor, replacement):
    text = path.read_text()
    if replacement in text:
        return
    if text.count(anchor) != 1:
        raise RuntimeError(f'QEMU9.2 patch anchor mismatch: {path}: {anchor!r}')
    path.write_text(text.replace(anchor, replacement))


shutil.copyfile(repo / 'qemu/orbit-sensor-hub.c', source / 'hw/misc/orbit-sensor-hub.c')
shutil.copyfile(repo / 'simulation/orbit_scenario.h', source / 'hw/misc/orbit_scenario.h')
shutil.copyfile(repo / 'simulation/orbit_events.h', source / 'hw/misc/orbit_events.h')
insert(source / 'hw/misc/meson.build', "system_ss.add(when: 'CONFIG_UNIMP', if_true: files('unimp.c'))",
       "system_ss.add(when: 'CONFIG_UNIMP', if_true: files('unimp.c'))\n"
       "system_ss.add(when: 'CONFIG_ARM_VIRT', if_true: files('orbit-sensor-hub.c'))")
insert(source / 'include/hw/arm/virt.h', '    VIRT_LOWMEMMAP_LAST,',
       '    VIRT_ORBIT_SENSORS,\n    VIRT_LOWMEMMAP_LAST,')
insert(source / 'hw/arm/virt.c', '    [VIRT_SECURE_GPIO] =        { 0x090b0000, 0x00001000 },',
       '    [VIRT_SECURE_GPIO] =        { 0x090b0000, 0x00001000 },\n'
       '    [VIRT_ORBIT_SENSORS] =      { 0x090c0000, 0x00001000 },')
insert(source / 'hw/arm/virt.c', '    [VIRT_ACPI_GED] = 9,',
       '    [VIRT_ACPI_GED] = 9,\n    [VIRT_ORBIT_SENSORS] = 10,')
function = '''static void create_orbit_sensors(VirtMachineState *vms)
{
    DeviceState *dev = qdev_new("orbit-sensor-hub");
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);
    hwaddr base = vms->memmap[VIRT_ORBIT_SENSORS].base;
    hwaddr size = vms->memmap[VIRT_ORBIT_SENSORS].size;
    int irq = vms->irqmap[VIRT_ORBIT_SENSORS];
    char *node = g_strdup_printf("/orbit-sensors@%" PRIx64, base);
    sysbus_realize_and_unref(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, base);
    sysbus_connect_irq(sbd, 0, qdev_get_gpio_in(vms->gic, irq));
    qemu_fdt_add_subnode(MACHINE(vms)->fdt, node);
    qemu_fdt_setprop_string(MACHINE(vms)->fdt, node, "compatible", "orbital,multisensor-v2");
    qemu_fdt_setprop_sized_cells(MACHINE(vms)->fdt, node, "reg", 2, base, 2, size);
    qemu_fdt_setprop_cells(MACHINE(vms)->fdt, node, "interrupts", GIC_FDT_IRQ_TYPE_SPI,
                           irq, GIC_FDT_IRQ_FLAGS_LEVEL_HI);
    g_free(node);
}

'''
insert(source / 'hw/arm/virt.c', 'static void create_platform_bus(VirtMachineState *vms)\n',
       function + 'static void create_platform_bus(VirtMachineState *vms)\n')
insert(source / 'hw/arm/virt.c', '    create_platform_bus(vms);',
       '    create_platform_bus(vms);\n    create_orbit_sensors(vms);')
print('Applied OrbitalLink synthetic multisensor hub to pinned QEMU9.2 sources')
