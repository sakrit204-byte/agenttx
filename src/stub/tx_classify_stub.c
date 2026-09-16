// SPDX-License-Identifier: GPL-2.0
/*
 * src/stub/tx_classify_stub.c --- fake classifier.  Fragment P4-01.
 *
 * Always TX_REVERSIBLE at full confidence.  This unblocks P3 in week 3:
 * the hooks can call the classifier seam and get an answer long before a
 * model exists, and the swap at P3-11 is then a link-time change rather
 * than a rewrite.
 *
 * NOTE FOR REVIEWERS: this stub is the *least* safe possible policy --
 * everything is reversible, nothing is ever deferred or escalated.  That
 * is correct for a stub (it keeps the system transparent while the rest is
 * being built) and catastrophic as a default.  src/core/ must refuse to
 * enter enforcing mode when CONFIG_AGENTTX_STUB=y; see the check in
 * tx_core init.  Do not remove that check to make a test pass.
 *
 * Owner: P4.  Nobody else edits this file.
 */

#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/printk.h>

#include "agenttx.h"

#define pr_fmt_stub "agenttx/classify-stub: "

enum tx_class tx_classify(const struct tx_features *f, __u8 *confidence)
{
	if (confidence)
		*confidence = 255;

	if (f)
		pr_debug(pr_fmt_stub "hook=%u syscall=%u dport=%u -> reversible\n",
			 f->f[TX_FEAT_HOOK_ID], f->f[TX_FEAT_SYSCALL_NR],
			 (unsigned int)f->f[TX_FEAT_DPORT_LO] |
			 ((unsigned int)f->f[TX_FEAT_DPORT_HI] << 8));

	return TX_REVERSIBLE;
}
EXPORT_SYMBOL_GPL(tx_classify);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("AgentTx stub: classifier (P4)");
