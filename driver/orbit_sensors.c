// SPDX-License-Identifier: GPL-2.0
/* ABI2 driver for the synthetic QEMU MMIO/IRQ sensor hub. */
#include <linux/compat.h>
#include <linux/fs.h>
#include <linux/interrupt.h>
#include <linux/io.h>
#include <linux/kref.h>
#include <linux/ktime.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/poll.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/uaccess.h>
#include "include/orbit_sensors.h"

struct sensor_device {
	struct miscdevice misc;
	struct kref refs;
	struct mutex io;
	spinlock_t lock;
	wait_queue_head_t wait;
	void __iomem *base;
	int irq;
	struct orbit_sensor_sample fifo[ORBIT_SENSOR_FIFO_CAPACITY];
	u32 head, count;
	u64 delivered, dropped;
	u64 command_effects;
	s64 clock_offset_ns;
	u64 clock_uncertainty_ns;
	bool opened, dead;
};
static void sensor_free(struct kref *ref)
{
	kfree(container_of(ref, struct sensor_device, refs));
}
static void sensor_fetch(struct sensor_device *d, struct orbit_sensor_sample *s)
{
	u32 w[16];
	unsigned int i;
	for (i = 0; i < ARRAY_SIZE(w); i++)
		w[i] = readl(d->base + 0x40 + i * 4);
	s->abi_version = w[0]; s->category = w[1]; s->generation = w[2];
	s->sequence = w[3];
	s->monotonic_ns = (s64)(((u64)w[5] << 32) | w[4]) + d->clock_offset_ns;
	s->quality = w[6]; s->schema = w[7];
	for (i = 0; i < ARRAY_SIZE(s->values); i++)
		s->values[i] = (s32)w[i + 8];
}
static irqreturn_t sensor_irq(int irq, void *opaque)
{
	struct sensor_device *d = opaque;
		struct orbit_sensor_sample sample;
	unsigned long flags;
	unsigned int budget = 64;
	bool handled = false;
	spin_lock_irqsave(&d->lock, flags);
	while (!d->dead && budget-- && readl(d->base + 0x20)) {
		bool dropped = false;
		sensor_fetch(d, &sample);
		if (d->count < ORBIT_SENSOR_FIFO_CAPACITY &&
		    (sample.category == 6 || d->count < ORBIT_SENSOR_FIFO_CAPACITY - 8)) {
			d->fifo[(d->head + d->count++) % ORBIT_SENSOR_FIFO_CAPACITY] = sample;
		} else {
			d->dropped++;
			dropped = true;
		}
		writel(1, d->base + 0x1c);
		if (dropped)
			writel(1, d->base + 0xf4);
		handled = true;
	}
	spin_unlock_irqrestore(&d->lock, flags);
	if (handled)
		wake_up_interruptible(&d->wait);
	return handled ? IRQ_HANDLED : IRQ_NONE;
}
static int sensor_open(struct inode *inode, struct file *file)
{
	struct sensor_device *d = container_of(file->private_data, struct sensor_device, misc);
	unsigned long flags;
	int ret = 0;
	spin_lock_irqsave(&d->lock, flags);
	if (d->dead)
		ret = -ENODEV;
	else if (d->opened)
		ret = -EBUSY;
	else {
		d->opened = true;
		kref_get(&d->refs);
		file->private_data = d;
		/* Acquisition starts on the first open, not during Linux boot. */
		writel(3, d->base + 0x08);
	}
	spin_unlock_irqrestore(&d->lock, flags);
	return ret;
}
static int sensor_release(struct inode *inode, struct file *file)
{
	struct sensor_device *d = file->private_data;
	unsigned long flags;
	spin_lock_irqsave(&d->lock, flags);
	if (!d->dead)
		writel(0, d->base + 0x08);
	d->opened = false;
	spin_unlock_irqrestore(&d->lock, flags);
	kref_put(&d->refs, sensor_free);
	return 0;
}
static bool sensor_ready(struct sensor_device *d)
{
	unsigned long flags;
	bool ready;
	spin_lock_irqsave(&d->lock, flags);
	ready = d->dead || d->count;
	spin_unlock_irqrestore(&d->lock, flags);
	return ready;
}
static ssize_t sensor_read(struct file *file, char __user *buf, size_t count, loff_t *offset)
{
	struct sensor_device *d = file->private_data;
	struct orbit_sensor_sample sample;
	unsigned long flags;
	size_t done = 0;
	int ret;
	if (!count)
		return 0;
	if (count < sizeof(sample))
		return -EINVAL;
	ret = mutex_lock_interruptible(&d->io);
	if (ret)
		return ret;
	while (count - done >= sizeof(sample)) {
		spin_lock_irqsave(&d->lock, flags);
		if (d->dead) {
			spin_unlock_irqrestore(&d->lock, flags);
			ret = -ENODEV;
			break;
		}
		if (!d->count) {
			spin_unlock_irqrestore(&d->lock, flags);
			if (done) { ret = 0; break; }
			if (file->f_flags & O_NONBLOCK) { ret = -EAGAIN; break; }
			mutex_unlock(&d->io);
			ret = wait_event_interruptible(d->wait, sensor_ready(d));
			if (ret)
				return ret;
			ret = mutex_lock_interruptible(&d->io);
			if (ret)
				return ret;
			continue;
		}
		sample = d->fifo[d->head];
		spin_unlock_irqrestore(&d->lock, flags);
		if (copy_to_user(buf + done, &sample, sizeof(sample))) { ret = -EFAULT; break; }
		spin_lock_irqsave(&d->lock, flags);
		d->head = (d->head + 1) % ORBIT_SENSOR_FIFO_CAPACITY;
		d->count--; d->delivered++;
		writel(1, d->base + 0xf0);
		spin_unlock_irqrestore(&d->lock, flags);
		done += sizeof(sample); ret = 0;
	}
	mutex_unlock(&d->io);
	return done ? (ssize_t)done : ret;
}
static __poll_t sensor_poll(struct file *file, poll_table *wait)
{
	struct sensor_device *d = file->private_data;
	unsigned long flags;
	__poll_t mask = 0;
	poll_wait(file, &d->wait, wait);
	spin_lock_irqsave(&d->lock, flags);
	if (d->dead) mask = EPOLLERR | EPOLLHUP;
	else if (d->count) mask = EPOLLIN | EPOLLRDNORM;
	spin_unlock_irqrestore(&d->lock, flags);
	return mask;
}
static long sensor_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	struct sensor_device *d = file->private_data;
	struct orbit_rate rate;
	struct orbit_stats stats = {.abi_version = 2, .capacity = 320};
	struct orbit_sensor_clock clock = {0};
	struct orbit_sensor_events events = {0};
	struct orbit_sensor_state state = {.abi_version = 2};
	unsigned long flags;
	u64 hardware_dropped;
	int ret;
	if (cmd != ORBIT_SENSOR_GET_STATS && cmd != ORBIT_SENSOR_SET_RATE && cmd != ORBIT_SENSOR_RESET &&
	    cmd != ORBIT_SENSOR_GET_CLOCK && cmd != ORBIT_SENSOR_CLOCK_CURRENT && cmd != ORBIT_SENSOR_GET_EVENTS &&
	    cmd != ORBIT_SENSOR_ENABLE && cmd != ORBIT_SENSOR_DISABLE && cmd != ORBIT_SENSOR_CLEAR_FAULT &&
	    cmd != ORBIT_SENSOR_GET_STATE)
		return -ENOTTY;
	if (cmd == ORBIT_SENSOR_SET_RATE) {
		if (copy_from_user(&rate, (void __user *)arg, sizeof(rate))) return -EFAULT;
		if (rate.abi_version != 2 || rate.period_ms < 1 || rate.period_ms > 60000 || rate.reserved[0] || rate.reserved[1])
			return -EINVAL;
	}
	ret = mutex_lock_interruptible(&d->io);
	if (ret) return ret;
	spin_lock_irqsave(&d->lock, flags);
	if (d->dead) ret = -ENODEV;
	else if (cmd == ORBIT_SENSOR_GET_STATS) {
		/* Device snapshots all hardware totals atomically; lock excludes ISR
		 * transfer so software+hardware pipeline counts describe one instant. */
		writel(1, d->base + 0x80);
		stats.generated = ((u64)readl(d->base + 0x94) << 32) | readl(d->base + 0x90);
		hardware_dropped = ((u64)readl(d->base + 0x9c) << 32) | readl(d->base + 0x98);
		stats.dropped = hardware_dropped + d->dropped;
		stats.delivered = d->delivered;
		stats.queued = d->count + readl(d->base + 0x84);
		stats.enqueued = stats.generated - stats.dropped;
		stats.reserved[0] = readl(d->base + 0x88);
		stats.reserved[1] = readl(d->base + 0x8c);
	} else if (cmd == ORBIT_SENSOR_GET_CLOCK) {
		clock.offset_ns = d->clock_offset_ns;
		clock.uncertainty_ns = d->clock_uncertainty_ns;
	} else if (cmd == ORBIT_SENSOR_CLOCK_CURRENT) {
		u64 before = ktime_get_ns(), hardware, after;
		writel(1, d->base + 0x80);
		hardware = ((u64)readl(d->base + 0xc4) << 32) | readl(d->base + 0xc0);
		after = ktime_get_ns();
		clock.offset_ns = (s64)(before + (after - before) / 2) - (s64)hardware;
		clock.uncertainty_ns = (after - before + 1) / 2;
	} else if (cmd == ORBIT_SENSOR_SET_RATE) {
		writel(rate.period_ms, d->base + 0x10);
		d->command_effects++;
	} else if (cmd == ORBIT_SENSOR_GET_EVENTS) {
		writel(1, d->base + 0x80);
		events.observations = ((u64)readl(d->base + 0x124) << 32) | readl(d->base + 0x120);
		events.coalesced = ((u64)readl(d->base + 0x12c) << 32) | readl(d->base + 0x128);
		events.overflow = ((u64)readl(d->base + 0x134) << 32) | readl(d->base + 0x130);
		events.pending = readl(d->base + 0x138);
	} else if (cmd == ORBIT_SENSOR_GET_STATE) {
		state.enabled = !!(readl(d->base + 0x08) & 1);
		state.period_ms = readl(d->base + 0x10);
		state.generation = readl(d->base + 0x28);
		state.reset_count = readl(d->base + 0x2c);
		state.fault_category = readl(d->base + 0xd0);
		state.command_effects = d->command_effects;
	} else if (cmd == ORBIT_SENSOR_ENABLE) {
		writel(readl(d->base + 0x08) | 3, d->base + 0x08);
		d->command_effects++;
	} else if (cmd == ORBIT_SENSOR_DISABLE) {
		writel(readl(d->base + 0x08) & ~1U, d->base + 0x08);
		d->command_effects++;
	} else if (cmd == ORBIT_SENSOR_CLEAR_FAULT) {
		writel(0, d->base + 0xd0);
		d->command_effects++;
	} else {
		d->dropped += d->count;
		writel(d->count, d->base + 0xf4);
		d->head = d->count = 0;
		writel(1, d->base + 0x18);
		d->command_effects++;
	}
	spin_unlock_irqrestore(&d->lock, flags);
	if (!ret && cmd == ORBIT_SENSOR_GET_STATS && copy_to_user((void __user *)arg, &stats, sizeof(stats))) ret = -EFAULT;
	if (!ret && (cmd == ORBIT_SENSOR_GET_CLOCK || cmd == ORBIT_SENSOR_CLOCK_CURRENT) &&
	    copy_to_user((void __user *)arg, &clock, sizeof(clock))) ret = -EFAULT;
	if (!ret && cmd == ORBIT_SENSOR_GET_EVENTS && copy_to_user((void __user *)arg, &events, sizeof(events))) ret = -EFAULT;
	if (!ret && cmd == ORBIT_SENSOR_GET_STATE && copy_to_user((void __user *)arg, &state, sizeof(state))) ret = -EFAULT;
	mutex_unlock(&d->io);
	return ret;
}
#ifdef CONFIG_COMPAT
static long sensor_compat_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	return sensor_ioctl(file, cmd, (unsigned long)compat_ptr(arg));
}
#endif
static const struct file_operations sensor_fops = {
	.owner = THIS_MODULE, .open = sensor_open, .release = sensor_release,
	.read = sensor_read, .poll = sensor_poll, .unlocked_ioctl = sensor_ioctl,
#ifdef CONFIG_COMPAT
	.compat_ioctl = sensor_compat_ioctl,
#endif
};
static int sensor_probe(struct platform_device *pdev)
{
	struct sensor_device *d;
	u64 before_ns, after_ns, hardware_ns, popped, previously_accounted;
	int ret;
	static_assert(sizeof(struct orbit_sensor_sample) == 64);
	d = kzalloc(sizeof(*d), GFP_KERNEL);
	if (!d) return -ENOMEM;
	kref_init(&d->refs); mutex_init(&d->io); spin_lock_init(&d->lock);
	init_waitqueue_head(&d->wait);
	d->base = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(d->base)) { ret = PTR_ERR(d->base); goto fail; }
	if (readl(d->base) != 0x4f534e32 || readl(d->base + 4) != 2) { ret = -ENODEV; goto fail; }
	before_ns = ktime_get_ns();
	writel(1, d->base + 0x80);
	hardware_ns = ((u64)readl(d->base + 0xc4) << 32) | readl(d->base + 0xc0);
	after_ns = ktime_get_ns();
	d->clock_offset_ns = (s64)(before_ns + (after_ns - before_ns) / 2) - (s64)hardware_ns;
	d->clock_uncertainty_ns = (after_ns - before_ns + 1) / 2;
	d->delivered = ((u64)readl(d->base + 0x10c) << 32) | readl(d->base + 0x108);
	d->dropped = ((u64)readl(d->base + 0x114) << 32) | readl(d->base + 0x110);
	popped = ((u64)readl(d->base + 0x104) << 32) | readl(d->base + 0x100);
	previously_accounted = d->delivered + d->dropped;
	if (popped < previously_accounted) { ret = -EIO; goto fail; }
	/* A removed/crashed prior binding may have consumed hardware records
	 * without delivering them. Preserve those losses across bindings. */
	while (popped > previously_accounted) {
		u32 lost = min_t(u64, popped - previously_accounted, U32_MAX);
		writel(lost, d->base + 0xf4);
		d->dropped += lost; previously_accounted += lost;
	}
	dev_info(&pdev->dev, "clock calibration offset=%lld ns uncertainty<=%llu ns\n",
		 (long long)d->clock_offset_ns, (unsigned long long)((after_ns - before_ns + 1) / 2));
	d->irq = platform_get_irq(pdev, 0);
	if (d->irq < 0) { ret = d->irq; goto fail; }
	writel(0, d->base + 8);
	ret = devm_request_irq(&pdev->dev, d->irq, sensor_irq, 0, "orbit-sensors", d);
	if (ret) goto fail;
	d->misc.minor = MISC_DYNAMIC_MINOR; d->misc.name = "orbit_sensors";
	d->misc.fops = &sensor_fops; d->misc.parent = &pdev->dev; d->misc.mode = 0600;
	ret = misc_register(&d->misc);
	if (ret) { devm_free_irq(&pdev->dev, d->irq, d); goto fail; }
	platform_set_drvdata(pdev, d);
	return 0;
fail:
	kref_put(&d->refs, sensor_free);
	return ret;
}
static void sensor_remove(struct platform_device *pdev)
{
	struct sensor_device *d = platform_get_drvdata(pdev);
	unsigned long flags;
	misc_deregister(&d->misc);
	mutex_lock(&d->io);
	spin_lock_irqsave(&d->lock, flags);
	d->dead = true;
	writel(0, d->base + 8);
	d->dropped += d->count;
	writel(d->count, d->base + 0xf4);
	d->head = d->count = 0;
	spin_unlock_irqrestore(&d->lock, flags);
	mutex_unlock(&d->io);
	devm_free_irq(&pdev->dev, d->irq, d);
	wake_up_interruptible(&d->wait);
	kref_put(&d->refs, sensor_free);
}
static const struct of_device_id sensor_of_match[] = {
	{.compatible = "orbital,multisensor-v2"}, {}
};
MODULE_DEVICE_TABLE(of, sensor_of_match);
static struct platform_driver sensor_driver = {
	.probe = sensor_probe, .remove = sensor_remove,
	.driver = {.name = "orbit-sensors", .of_match_table = sensor_of_match},
};
module_platform_driver(sensor_driver);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("OrbitalLink synthetic QEMU multisensor MMIO/IRQ source ABI2");
