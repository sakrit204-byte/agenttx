// SPDX-License-Identifier: GPL-2.0
/*
 * src/stub/tx_fs_stub.c --- fake copy-on-write storage.  Fragment P2-01.
 *
 * WORKFLOW.md Rule 2: the provider ships the fake before the real one.
 * With CONFIG_AGENTTX_STUB=y this file satisfies every symbol P1 calls,
 * so P1 can run commit-ordering tests in week 3 without a line of P2's
 * overlayfs code existing.
 *
 * It logs and succeeds.  It must never be the thing under test -- if a
 * test passes against this file and fails against src/fs/, the test was
 * measuring the stub.
 *
 * Owner: P2.  Nobody else edits this file.
 */

#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/printk.h>

#include "agenttx.h"

#define pr_fmt_stub "agenttx/fs-stub: "

/*
 * A counter per operation, exposed only so the stub tests can assert that
 * P1 called us the right number of times in the right order.  Deliberately
 * not a spinlock-protected structure: this is scaffolding, and adding
 * locking here would invite someone to mistake it for an implementation.
 */
static atomic_t stub_begin_count  = ATOMIC_INIT(0);
static atomic_t stub_commit_count = ATOMIC_INIT(0);
static atomic_t stub_abort_count  = ATOMIC_INIT(0);

int tx_fs_begin(tx_id_t tx_id, const char *workdir)
{
	atomic_inc(&stub_begin_count);
	pr_info(pr_fmt_stub "begin  tx=%llu workdir=%s\n",
		(unsigned long long)tx_id, workdir ? workdir : "(null)");
	return 0;
}
EXPORT_SYMBOL_GPL(tx_fs_begin);

int tx_fs_commit(tx_id_t tx_id, __u64 *n_files)
{
	atomic_inc(&stub_commit_count);
	pr_info(pr_fmt_stub "commit tx=%llu\n", (unsigned long long)tx_id);
	if (n_files)
		*n_files = 0;
	return 0;
}
EXPORT_SYMBOL_GPL(tx_fs_commit);

int tx_fs_abort(tx_id_t tx_id, __u64 *n_files)
{
	atomic_inc(&stub_abort_count);
	pr_info(pr_fmt_stub "abort  tx=%llu\n", (unsigned long long)tx_id);
	if (n_files)
		*n_files = 0;
	return 0;
}
EXPORT_SYMBOL_GPL(tx_fs_abort);

int tx_fs_writeset_count(tx_id_t tx_id, __u64 *n)
{
	pr_debug(pr_fmt_stub "writeset tx=%llu\n", (unsigned long long)tx_id);
	if (n)
		*n = 0;
	return 0;
}
EXPORT_SYMBOL_GPL(tx_fs_writeset_count);

/*
 * The stub never observes external modification.  This is a lie that
 * matters: P2-08's real answer decides what commit means when the lower
 * layer moved underneath us, and no P1 test may depend on the answer
 * being "it did not".
 */
int tx_fs_extmod_check(tx_id_t tx_id)
{
	pr_debug(pr_fmt_stub "extmod tx=%llu -> 0 (stub always says clean)\n",
		 (unsigned long long)tx_id);
	return 0;
}
EXPORT_SYMBOL_GPL(tx_fs_extmod_check);

MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("AgentTx stub: copy-on-write storage (P2)");
