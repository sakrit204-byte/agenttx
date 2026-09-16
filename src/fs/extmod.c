// SPDX-License-Identifier: GPL-2.0
/*
 * src/fs/extmod.c --- did the lower layer move under us?  Fragment P2-08.
 *
 * The tracker asks the right question: "What does commit mean if the lower
 * file changed mid-tx? Document the answer."
 *
 * THE ANSWER WE IMPLEMENT: detect and report, do not refuse.
 *
 * Refusing would be defensible and it is what a database would do. We do
 * not, for a specific reason: the transaction's writes are already in the
 * upper layer and the agent has already reasoned about them. Refusing the
 * commit turns a concurrency problem into an availability problem without
 * telling anyone anything they did not already have. Reporting it lets the
 * supervisor -- which, unlike the kernel, has application semantics -- make
 * the call.
 *
 * P2-12 (OCC validation, docs/deadlock.md) is where this becomes a decision
 * rather than a report: intersecting write sets tells you whether the
 * external change actually collided with anything this transaction touched,
 * and only then is aborting the right answer. This fragment is the signal
 * that makes that possible; it is deliberately not the policy.
 *
 * Owner: P2.
 */

#define pr_fmt(fmt) "agenttx/fs: " fmt

#include <linux/fs.h>
#include <linux/module.h>
#include <linux/namei.h>

#include "fsint.h"

/*
 * Baseline: the mtime of the tx directory itself, stamped when the CoW area
 * was created. Comparing the lower layer against it answers "did anything
 * below the protected directory change after this transaction opened".
 *
 * Coarse on purpose. A per-inode i_version comparison is the precise answer
 * and needs the read set (P2-07) to know which inodes to check; this is the
 * cheap signal that works today with no read-path hook at all.
 */
int tx_fs_extmod_check(tx_id_t tx_id)
{
	struct path txdir, lower;
	struct kstat tst, lst;
	int ret;

	ret = tx_fs_lookup(tx_id, NULL, &txdir);
	if (ret)
		return 0;

	ret = tx_fs_lookup(tx_id, "lower", &lower);
	if (ret) {
		path_put(&txdir);
		return 0;		/* no lower layer; nothing to compare */
	}

	ret = vfs_getattr(&txdir, &tst, STATX_MTIME, AT_STATX_SYNC_AS_STAT);
	if (!ret)
		ret = vfs_getattr(&lower, &lst, STATX_MTIME,
				  AT_STATX_SYNC_AS_STAT);

	path_put(&lower);
	path_put(&txdir);

	if (ret)
		return 0;		/* cannot tell; do not cry wolf */

	if (timespec64_compare(&lst.mtime, &tst.mtime) > 0) {
		pr_warn("tx=%llu: the lower layer was modified after this transaction opened\n",
			(unsigned long long)tx_id);
		return 1;
	}
	return 0;
}
EXPORT_SYMBOL_GPL(tx_fs_extmod_check);
