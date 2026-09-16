// SPDX-License-Identifier: GPL-2.0
/*
 * src/stub/tx_core_stub.c --- fake transaction identity.  Fragment P1-02.
 *
 * tx_current_id() returns 1, unconditionally: from the point of view of a
 * hook, every task is always inside transaction 1.  This is what lets P3
 * write and test the tx gate (P3-04) in week 3, before P1's hashtable
 * (P1-05) or the real kfunc (P1-10) exist.
 *
 * P1 writes this BEFORE writing the real core.  That ordering is the
 * single most load-bearing instruction in WORKFLOW.md: it converts P3's
 * whole phase-1 stream from blocked to parallel.
 *
 * Owner: P1.  Nobody else edits this file.
 */

#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/printk.h>

#include "agenttx.h"

#define pr_fmt_stub "agenttx/core-stub: "

#define STUB_TX_ID	((tx_id_t)1)

tx_id_t tx_current_id(void)
{
	return STUB_TX_ID;
}
EXPORT_SYMBOL_GPL(tx_current_id);

enum tx_state tx_current_state(void)
{
	return TX_STATE_ACTIVE;
}
EXPORT_SYMBOL_GPL(tx_current_state);

/*
 * The stub accepts any class and forgets it.  A consequence worth stating
 * in review: with the core stubbed there is no DOOMED state, so a P3 test
 * cannot show that an irrevocable effect made a transaction non-abortable.
 * That test belongs to P1-06 and must be written against the real core.
 */
int tx_note_class(tx_id_t tx_id, enum tx_class klass)
{
	pr_debug(pr_fmt_stub "note tx=%llu class=%u (dropped)\n",
		 (unsigned long long)tx_id, (unsigned int)klass);
	return 0;
}
EXPORT_SYMBOL_GPL(tx_note_class);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("AgentTx stub: transaction identity (P1)");
