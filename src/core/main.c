// SPDX-License-Identifier: GPL-2.0
/*
 * src/core/main.c --- module lifecycle and /dev/agenttx.  Fragment P1-03.
 *
 * The tracker note is "insmod/rmmod x10 must not leak", and that is the
 * property this file is written to have.  Everything acquired in
 * agenttx_init() is released in exactly the reverse order by the error
 * ladder below it and by agenttx_exit(), and there is one label per
 * acquisition.  WORKFLOW.md section 5 item 4 says 80% of kernel bugs in
 * student projects live in that ladder; the way to not be in that 80% is
 * to write the ladder before the feature.
 *
 * misc_register() rather than alloc_chrdev_region() + cdev_add(): we want
 * exactly one device node with a kernel-assigned minor, udev creates it
 * for us, and the teardown is a single call that cannot half-fail.  The
 * ABI that matters is the ioctl set, not the device numbering.
 *
 * Owner: P1.
 */

#define pr_fmt(fmt) "agenttx: " fmt

#include <linux/fs.h>
#include <linux/init.h>
#include <linux/miscdevice.h>
#include <linux/module.h>
#include <linux/printk.h>
#include <linux/sched.h>
#include <linux/uaccess.h>

#include "core.h"

/*
 * Transaction ids are global and monotonic, never reused within a boot.
 * Reuse would make the WAL ambiguous: a record carrying tx_id 7 has to
 * mean one transaction, not "whichever 7 was live when you read it".
 * Starting at TX_ID_NONE means the first real id is 1.
 */
atomic64_t tx_next_id = ATOMIC64_INIT(TX_ID_NONE);

/*
 * Open does not start a transaction.  That is deliberate: a process opens
 * /dev/agenttx once and may run many transactions through it, and the
 * supervisor holds its own fd on the same node.  Binding a transaction to
 * an fd would make "who may commit" an fd-ownership question, and the
 * proposal is explicit that it is a process-identity question.
 */
static int agenttx_open(struct inode *inode, struct file *filp)
{
	filp->private_data = NULL;
	return 0;
}

static int agenttx_release(struct inode *inode, struct file *filp)
{
	return 0;
}

static const struct file_operations agenttx_fops = {
	.owner		= THIS_MODULE,
	.open		= agenttx_open,
	.release	= agenttx_release,
	.unlocked_ioctl	= tx_ioctl,
	/*
	 * A 32-bit userspace on a 64-bit kernel must reach the same handler.
	 * Every struct in the ioctl ABI was laid out with explicit padding
	 * and fixed-width types precisely so that compat is the identity --
	 * include/agenttx.h is checked under -Wpadded for this reason, so
	 * pointing compat_ioctl at the same function is sound rather than
	 * lazy.  If a future contract-change adds a long or a pointer to one
	 * of those structs, this line becomes a bug.
	 */
	.compat_ioctl	= tx_ioctl,
	/*
	 * `no_llseek` was removed in 6.12 (it became the default for any
	 * file_operations that does not set .llseek).  We use noop_llseek
	 * rather than leaving it unset because misc_register()'s own fops
	 * does the same, and because an lseek on a control device should
	 * succeed-and-do-nothing rather than return -ESPIPE to a harness
	 * that opened us with a buffered stdio wrapper.
	 */
	.llseek		= noop_llseek,
};

static struct miscdevice agenttx_misc = {
	.minor	= MISC_DYNAMIC_MINOR,
	.name	= AGENTTX_DEV_NAME,
	.fops	= &agenttx_fops,
	.mode	= 0600,		/* root only; the supervisor is privileged */
};

static bool misc_registered;
static bool exit_hook_installed;

static int __init agenttx_init(void)
{
	int ret;

	ret = tx_state_selfcheck();
	if (ret) {
		pr_err("state machine self-check failed; refusing to load\n");
		return ret;
	}

	ret = tx_ctx_init();
	if (ret) {
		pr_err("context table init failed: %d\n", ret);
		return ret;
	}

	ret = misc_register(&agenttx_misc);
	if (ret) {
		pr_err("misc_register failed: %d\n", ret);
		goto err_ctx;
	}
	misc_registered = true;

	/*
	 * The exit hook last, because from the moment it is live it can be
	 * called for any dying task on the system.  Installing it before the
	 * table exists would be a race we could not test for.
	 */
	ret = tx_exit_hook_install();
	if (ret) {
		pr_err("exit hook install failed: %d\n", ret);
		goto err_misc;
	}
	exit_hook_installed = true;

	/*
	 * kfunc registration last and non-fatally.  A kernel without
	 * CONFIG_DEBUG_INFO_BTF_MODULES cannot resolve them, but the module
	 * is still perfectly usable through the ioctl path -- rung 1 does not
	 * need BPF at all.  Refusing to load here would make a P1-only
	 * developer's machine depend on P3's kernel config.
	 */
	if (tx_kfunc_register())
		pr_warn("kfuncs unavailable; BPF hooks cannot gate on transaction state\n");

	pr_info("loaded, abi %u, %s providers, %s\n",
		AGENTTX_ABI_VERSION,
#ifdef CONFIG_AGENTTX_STUB
		"stub",
#else
		"real",
#endif
		AGENTTX_DEV_PATH);
	return 0;

err_misc:
	misc_deregister(&agenttx_misc);
	misc_registered = false;
err_ctx:
	tx_ctx_exit();
	return ret;
}

static void __exit agenttx_exit(void)
{
	/*
	 * Reverse order of init, and the ordering is load-bearing rather
	 * than stylistic:
	 *
	 *  1. stop new transactions arriving   (deregister the device)
	 *  2. stop the exit hook firing        (nothing may call us after)
	 *  3. only then tear down the table
	 *
	 * Doing 3 before 1 means an ioctl already inside the kernel can
	 * touch a table that is being freed underneath it.
	 */
	if (misc_registered) {
		misc_deregister(&agenttx_misc);
		misc_registered = false;
	}
	if (exit_hook_installed) {
		tx_exit_hook_remove();
		exit_hook_installed = false;
	}

	tx_ctx_exit();
	pr_info("unloaded\n");
}

module_init(agenttx_init);
module_exit(agenttx_exit);

MODULE_LICENSE("GPL");
MODULE_AUTHOR("AgentTx P1");
MODULE_DESCRIPTION("AgentTx: kernel transactions for AI agents -- transaction core");
MODULE_VERSION("0.1");
