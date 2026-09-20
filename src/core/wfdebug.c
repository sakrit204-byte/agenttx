// SPDX-License-Identifier: GPL-2.0
/*
 * src/core/wfdebug.c --- read the live wait-for graph out of the kernel.
 *
 * WHY THIS EXISTS.
 *
 * Until now the wait-for graph only ever spoke through dmesg, and only
 * when something went wrong: an edge was added silently, and a cycle
 * printed a report. That is enough to prove the detector works and not
 * nearly enough to SHOW it working. A console that wants to display the
 * graph while agents are running has nothing to read, and the honest
 * alternative -- counting log lines -- reports history, not state. An
 * edge that was added and then released still counts, so the number only
 * ever goes up. A panel that says "3 wait-for edges" when there are none
 * is worse than a panel that says nothing.
 *
 * So: two debugfs files holding the CURRENT state.
 *
 *   /sys/kernel/debug/agenttx/waitfor      one line per live edge
 *   /sys/kernel/debug/agenttx/transactions one line per live transaction
 *
 * Read-only, root-only, and entirely optional: if debugfs is not mounted
 * or the directory cannot be made, the module loads and works exactly as
 * before. Nothing in the transaction path may depend on a debugging
 * interface being present.
 *
 * Owner: P1.
 */

#include <linux/debugfs.h>
#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/seq_file.h>

#include "core.h"

static struct dentry *tx_debug_dir;

/*
 * The edge list and its lock live in waitfor.c and stay there. Exposing
 * them through a pair of iterator calls keeps the locking discipline in
 * one file -- a second file taking tx_edges_lock directly is how the
 * next person introduces a lock-ordering bug against a printer.
 */
static void print_edge(tx_id_t waiter, tx_id_t holder, const char *kind,
		       u64 age_ms, void *arg)
{
	seq_printf((struct seq_file *)arg, "%llu %llu %s %llu\n",
		   (unsigned long long)waiter, (unsigned long long)holder,
		   kind, (unsigned long long)age_ms);
}

static int waitfor_show(struct seq_file *m, void *v)
{
	seq_puts(m, "# waiter holder kind age_ms\n");
	tx_wait_for_each(print_edge, m);
	return 0;
}

static const char * const state_names[] = TX_STATE_NAMES;
static const char * const class_names[] = TX_CLASS_NAMES;

static void print_ctx(struct tx_ctx *ctx, void *arg)
{
	struct seq_file *m = arg;
	u32 st = READ_ONCE(ctx->state);
	u32 wc = READ_ONCE(ctx->worst_class);

	seq_printf(m, "%llu %s %s %llu %llu %u\n",
		   (unsigned long long)ctx->tx_id,
		   st < TX_STATE_MAX ? state_names[st] : "?",
		   wc < TX_CLASS_MAX ? class_names[wc] : "?",
		   (unsigned long long)READ_ONCE(ctx->n_deferred),
		   (unsigned long long)READ_ONCE(ctx->n_written),
		   READ_ONCE(ctx->tgid));
}

static int transactions_show(struct seq_file *m, void *v)
{
	seq_puts(m, "# tx state worst_class n_deferred n_written pid\n");
	tx_ctx_for_each(print_ctx, m);
	return 0;
}

DEFINE_SHOW_ATTRIBUTE(waitfor);
DEFINE_SHOW_ATTRIBUTE(transactions);

void tx_debugfs_init(void)
{
	tx_debug_dir = debugfs_create_dir("agenttx", NULL);
	if (IS_ERR_OR_NULL(tx_debug_dir)) {
		/* Not fatal.  debugfs may simply not be mounted. */
		tx_debug_dir = NULL;
		return;
	}
	debugfs_create_file("waitfor", 0400, tx_debug_dir, NULL,
			    &waitfor_fops);
	debugfs_create_file("transactions", 0400, tx_debug_dir, NULL,
			    &transactions_fops);
}

void tx_debugfs_exit(void)
{
	debugfs_remove_recursive(tx_debug_dir);
	tx_debug_dir = NULL;
}
