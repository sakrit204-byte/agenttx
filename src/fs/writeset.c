// SPDX-License-Identifier: GPL-2.0
/*
 * src/fs/writeset.c --- what this transaction touched.  Fragment P2-06.
 *
 * The write set is not bookkeeping we maintain: it is the upper layer.
 * overlayfs already records exactly one entry per object the transaction
 * modified, because that is what copy-up means. Counting it is therefore
 * free of any hook on the write path -- there is no per-write accounting
 * to get wrong, and no way for the count to drift from reality.
 *
 * P2-12's commit-time conflict detection (OCC, docs/deadlock.md) reads this
 * and intersects it with other live transactions' sets. That is why P2-06
 * was promoted out of the multi-agent "stretch": without a write set there
 * is nothing to intersect, and without an intersection there is no conflict
 * detection at all.
 *
 * Owner: P2.
 */

#define pr_fmt(fmt) "agenttx/fs: " fmt

#include <linux/fs.h>
#include <linux/module.h>
#include <linux/namei.h>

#include "fsint.h"

int tx_fs_count(struct path *dir, int depth, u64 *n)
{
	struct tx_names ns;
	unsigned int i;
	int ret;

	if (depth > TX_FS_MAX_DEPTH)
		return -ELOOP;

	ret = tx_dir_collect(dir, &ns);
	if (ret)
		return ret;

	for (i = 0; i < ns.n; i++) {
		(*n)++;
		if (ns.type[i] == DT_DIR) {
			struct path child;

			if (vfs_path_lookup(dir->dentry, dir->mnt,
					    ns.name[i], 0, &child))
				continue;
			ret = tx_fs_count(&child, depth + 1, n);
			path_put(&child);
			if (ret)
				break;
		}
	}

	tx_names_free(&ns);
	return ret;
}

int tx_fs_writeset_count(tx_id_t tx_id, __u64 *n)
{
	struct path upper;
	u64 count = 0;
	int ret;

	if (n)
		*n = 0;

	ret = tx_fs_lookup(tx_id, "upper", &upper);
	if (ret)
		return 0;		/* nothing written; not an error */

	ret = tx_fs_count(&upper, 0, &count);
	path_put(&upper);

	if (!ret && n)
		*n = count;
	return ret;
}
EXPORT_SYMBOL_GPL(tx_fs_writeset_count);
