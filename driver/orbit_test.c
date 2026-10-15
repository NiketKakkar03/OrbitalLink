// SPDX-License-Identifier: GPL-2.0
/* Explicit test backend: no MMIO, IRQ or hardware discovery claims. */
#include <linux/compat.h>
#include <linux/fs.h>
#include <linux/jiffies.h>
#include <linux/kref.h>
#include <linux/ktime.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/mutex.h>
#include <linux/platform_device.h>
#include <linux/poll.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/timer.h>
#include <linux/uaccess.h>
#include "orbit_queue.h"

struct orbit_device {
	struct miscdevice misc;
	struct kref refs;
	/* Lock order: io_mutex -> lock. Never sleep holding lock. */
	struct mutex io_mutex;
	spinlock_t lock;
	wait_queue_head_t wait;
	struct timer_list timer;
	struct orbit_queue queue;
	bool opened, dead;
	u32 sequence, period_ms;
};
static struct platform_device *test_device;
static unsigned int initial_period_ms = 100;
module_param(initial_period_ms, uint, 0444);
MODULE_PARM_DESC(initial_period_ms, "Test generator period (1..60000 ms)");

static void orbit_free(struct kref *ref)
{
	kfree(container_of(ref, struct orbit_device, refs));
}
static void orbit_tick(struct timer_list *timer)
{
	struct orbit_device *d = from_timer(d, timer, timer);
	struct orbit_sample sample = { .abi_version = ORBIT_ABI_VERSION,
		.flags = ORBIT_SAMPLE_TEST_SOURCE, .device_generation = 1 };
	unsigned long flags;
	bool added = false;

	spin_lock_irqsave(&d->lock, flags);
	if (!d->dead) {
		sample.device_sequence = d->sequence++;
		sample.monotonic_ns = ktime_get_ns();
		sample.temperature_mc = -10000 + (s32)(sample.device_sequence % 1000) * 125;
		sample.voltage_mv = 3300;
		added = orbit_queue_push(&d->queue, &sample);
		mod_timer(&d->timer, jiffies + msecs_to_jiffies(d->period_ms));
	}
	spin_unlock_irqrestore(&d->lock, flags);
	if (added)
		wake_up_interruptible(&d->wait);
}
static int orbit_open(struct inode *inode, struct file *file)
{
	struct orbit_device *d = container_of(file->private_data,
					     struct orbit_device, misc);
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
	}
	spin_unlock_irqrestore(&d->lock, flags);
	return ret;
}
static int orbit_release(struct inode *inode, struct file *file)
{
	struct orbit_device *d = file->private_data;
	unsigned long flags;

	spin_lock_irqsave(&d->lock, flags);
	d->opened = false;
	spin_unlock_irqrestore(&d->lock, flags);
	kref_put(&d->refs, orbit_free);
	return 0;
}
static bool orbit_ready(struct orbit_device *d)
{
	unsigned long flags;
	bool ready;
	spin_lock_irqsave(&d->lock, flags);
	ready = d->dead || d->queue.count;
	spin_unlock_irqrestore(&d->lock, flags);
	return ready;
}
static ssize_t orbit_read(struct file *file, char __user *buf, size_t count,
			  loff_t *offset)
{
	struct orbit_device *d = file->private_data;
	struct orbit_sample sample;
	unsigned long flags;
	size_t done = 0;
	int ret;

	if (!count)
		return 0;
	if (count < sizeof(sample))
		return -EINVAL;
	ret = mutex_lock_interruptible(&d->io_mutex);
	if (ret)
		return ret;
	while (count - done >= sizeof(sample)) {
		spin_lock_irqsave(&d->lock, flags);
		if (d->dead) {
			spin_unlock_irqrestore(&d->lock, flags);
			ret = -ENODEV;
			break;
		}
		if (!d->queue.count) {
			spin_unlock_irqrestore(&d->lock, flags);
			if (done) {
				ret = 0;
				break;
			}
			if (file->f_flags & O_NONBLOCK) {
				ret = -EAGAIN;
				break;
			}
			/* Do not hold io_mutex while asleep: detach must wake us. */
			mutex_unlock(&d->io_mutex);
			ret = wait_event_interruptible(d->wait, orbit_ready(d));
			if (ret)
				return ret;
			ret = mutex_lock_interruptible(&d->io_mutex);
			if (ret)
				return ret;
			continue;
		}
		sample = *orbit_queue_peek(&d->queue);
		spin_unlock_irqrestore(&d->lock, flags);
		/* Single reader mutex and drop-newest preserve the staged record. */
		if (copy_to_user(buf + done, &sample, sizeof(sample))) {
			ret = -EFAULT;
			break;
		}
		spin_lock_irqsave(&d->lock, flags);
		orbit_queue_consume(&d->queue);
		spin_unlock_irqrestore(&d->lock, flags);
		done += sizeof(sample);
		ret = 0;
	}
	mutex_unlock(&d->io_mutex);
	return done ? (ssize_t)done : ret;
}
static __poll_t orbit_poll(struct file *file, poll_table *wait)
{
	struct orbit_device *d = file->private_data;
	unsigned long flags;
	__poll_t mask = 0;

	poll_wait(file, &d->wait, wait);
	spin_lock_irqsave(&d->lock, flags);
	if (d->dead)
		mask = EPOLLHUP | EPOLLERR;
	else if (d->queue.count)
		mask = EPOLLIN | EPOLLRDNORM;
	spin_unlock_irqrestore(&d->lock, flags);
	return mask;
}
static long orbit_ioctl(struct file *file, unsigned int cmd, unsigned long arg)
{
	struct orbit_device *d = file->private_data;
	struct orbit_stats stats = { .abi_version = ORBIT_ABI_VERSION,
		.capacity = ORBIT_FIFO_CAPACITY };
	struct orbit_rate rate;
	unsigned long flags;
	int ret;

	if (cmd != ORBIT_IOC_GET_STATS && cmd != ORBIT_IOC_SET_RATE)
		return -ENOTTY;
	if (cmd == ORBIT_IOC_SET_RATE) {
		if (copy_from_user(&rate, (void __user *)arg, sizeof(rate)))
			return -EFAULT;
		if (!orbit_rate_valid(&rate))
			return -EINVAL;
	}
	ret = mutex_lock_interruptible(&d->io_mutex);
	if (ret)
		return ret;
	spin_lock_irqsave(&d->lock, flags);
	if (d->dead) {
		ret = -ENODEV;
	} else if (cmd == ORBIT_IOC_GET_STATS) {
		stats.generated = d->queue.generated;
		stats.enqueued = d->queue.enqueued;
		stats.dropped = d->queue.dropped;
		stats.delivered = d->queue.delivered;
		stats.queued = d->queue.count;
	} else {
		d->period_ms = rate.period_ms;
		mod_timer(&d->timer, jiffies + msecs_to_jiffies(d->period_ms));
	}
	spin_unlock_irqrestore(&d->lock, flags);
	if (!ret && cmd == ORBIT_IOC_GET_STATS &&
	    copy_to_user((void __user *)arg, &stats, sizeof(stats)))
		ret = -EFAULT;
	mutex_unlock(&d->io_mutex);
	return ret;
}
#ifdef CONFIG_COMPAT
static long orbit_compat_ioctl(struct file *file, unsigned int cmd,
			       unsigned long arg)
{
	return orbit_ioctl(file, cmd, (unsigned long)compat_ptr(arg));
}
#endif
static const struct file_operations orbit_fops = {
	.owner = THIS_MODULE, .open = orbit_open, .release = orbit_release,
	.read = orbit_read, .poll = orbit_poll, .unlocked_ioctl = orbit_ioctl,
#ifdef CONFIG_COMPAT
	.compat_ioctl = orbit_compat_ioctl,
#endif
	/* Linux 6.12 removed no_llseek; a NULL llseek rejects seeking. */
	.llseek = NULL,
};
static int orbit_probe(struct platform_device *pdev)
{
	struct orbit_device *d;
	int ret;

	static_assert(sizeof(struct orbit_sample) == 40);
	static_assert(sizeof(struct orbit_stats) == 56);
	static_assert(sizeof(struct orbit_rate) == 16);
	d = kzalloc(sizeof(*d), GFP_KERNEL);
	if (!d)
		return -ENOMEM;
	kref_init(&d->refs);
	mutex_init(&d->io_mutex);
	spin_lock_init(&d->lock);
	init_waitqueue_head(&d->wait);
	timer_setup(&d->timer, orbit_tick, 0);
	d->period_ms = initial_period_ms;
	d->misc.minor = MISC_DYNAMIC_MINOR;
	d->misc.name = "orbit_telemetry";
	d->misc.fops = &orbit_fops;
	d->misc.parent = &pdev->dev;
	d->misc.mode = 0600;
	ret = misc_register(&d->misc);
	if (ret) {
		kref_put(&d->refs, orbit_free);
		return ret;
	}
	platform_set_drvdata(pdev, d);
	mod_timer(&d->timer, jiffies + msecs_to_jiffies(d->period_ms));
	return 0;
}
static void orbit_remove(struct platform_device *pdev)
{
	struct orbit_device *d = platform_get_drvdata(pdev);
	unsigned long flags;

	/* misc registration serializes new open callbacks; retain base ref. */
	misc_deregister(&d->misc);
	mutex_lock(&d->io_mutex);
	spin_lock_irqsave(&d->lock, flags);
	d->dead = true;
	spin_unlock_irqrestore(&d->lock, flags);
	mutex_unlock(&d->io_mutex);
	del_timer_sync(&d->timer);
	wake_up_interruptible(&d->wait);
	platform_set_drvdata(pdev, NULL);
	kref_put(&d->refs, orbit_free);
}
static struct platform_driver orbit_driver = {
	.probe = orbit_probe, .remove = orbit_remove,
	.driver = { .name = "orbit-test" },
};
static int __init orbit_init(void)
{
	int ret;
	if (initial_period_ms < 1 || initial_period_ms > 60000)
		return -EINVAL;
	ret = platform_driver_register(&orbit_driver);
	if (ret)
		return ret;
	test_device = platform_device_register_simple("orbit-test", -1, NULL, 0);
	if (IS_ERR(test_device)) {
		ret = PTR_ERR(test_device);
		platform_driver_unregister(&orbit_driver);
		return ret;
	}
	if (!platform_get_drvdata(test_device)) {
		platform_device_unregister(test_device);
		platform_driver_unregister(&orbit_driver);
		return -ENODEV;
	}
	return 0;
}
static void __exit orbit_exit(void)
{
	platform_device_unregister(test_device);
	platform_driver_unregister(&orbit_driver);
}
module_init(orbit_init);
module_exit(orbit_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("OrbitalLink M6 TEST-ONLY FIFO/syscall backend");
