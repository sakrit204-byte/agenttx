// SPDX-License-Identifier: GPL-2.0
/*
 * src/stub/tx_eff_stub.c --- fake effect interception and WAL.
 * Fragment P3-01.
 *
 * Satisfies the P3 seam so that P1's commit-ordering work (P1-07) and
 * P1's process-death work (P1-08) can proceed in week 3 against something
 * that returns predictable counts.
 *
 * Owner: P3.  Nobody else edits this file.
 */

#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/printk.h>

#include "agenttx.h"

#define pr_fmt_stub "agenttx/eff-stub: "

/*
 * A fixed pretend effect count.  Non-zero on purpose: a stub that reports
 * zero work done lets a broken commit path look correct, because "flushed
 * 0 effects" and "flushed nothing because the loop never ran" are the same
 * observation.  P1's tests assert on this constant.
 */
#define STUB_PRETEND_EFFECTS	3ULL

int tx_eff_begin(tx_id_t tx_id, __u32 flags)
{
	pr_info(pr_fmt_stub "begin   tx=%llu flags=0x%x\n",
		(unsigned long long)tx_id, flags);
	return 0;
}
EXPORT_SYMBOL_GPL(tx_eff_begin);

int tx_eff_flush(tx_id_t tx_id, __u64 *n_effects)
{
	pr_info(pr_fmt_stub "flush   tx=%llu (pretending %llu effects)\n",
		(unsigned long long)tx_id, STUB_PRETEND_EFFECTS);
	if (n_effects)
		*n_effects = STUB_PRETEND_EFFECTS;
	return 0;
}
EXPORT_SYMBOL_GPL(tx_eff_flush);

int tx_eff_discard(tx_id_t tx_id, __u64 *n_effects)
{
	pr_info(pr_fmt_stub "discard tx=%llu (pretending %llu effects)\n",
		(unsigned long long)tx_id, STUB_PRETEND_EFFECTS);
	if (n_effects)
		*n_effects = STUB_PRETEND_EFFECTS;
	return 0;
}
EXPORT_SYMBOL_GPL(tx_eff_discard);

int tx_eff_count(tx_id_t tx_id, __u64 *n)
{
	pr_debug(pr_fmt_stub "count   tx=%llu\n", (unsigned long long)tx_id);
	if (n)
		*n = STUB_PRETEND_EFFECTS;
	return 0;
}
EXPORT_SYMBOL_GPL(tx_eff_count);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("AgentTx stub: effect interception and WAL (P3)");
