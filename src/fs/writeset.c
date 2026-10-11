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
#include <linux/limits.h>
#include <linux/module.h>
#include <linux/namei.h>
#include <linux/slab.h>

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

/* ------------------------------------------------------------------ */
/* P2-12: the write set as PATHS, not just a count.                    */
/*                                                                     */
/* Counting answered "how much did this transaction touch". Validation */
/* needs "which objects", because two transactions that each wrote     */
/* four files conflict only if one of those files is the same file.    */
/*                                                                     */
/* We hand each relative path to a callback rather than building a     */
/* list, so the caller decides what to keep. The hash set in occ.c     */
/* keeps a 64 bit digest and never the string, which bounds what one   */
/* transaction can make the kernel allocate.                           */
/* ------------------------------------------------------------------ */

static int tx_fs_walk_rel(struct path *dir, char *rel, size_t rellen,
			  size_t used, int depth,
			  tx_fs_path_fn fn, void *arg)
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
		size_t len = strlen(ns.name[i]);
		size_t next;

		/* used + '/' + name + NUL */
		if (used + (used ? 1 : 0) + len + 1 > rellen)
			continue;		/* too deep to name; skip */

		next = used;
		if (used)
			rel[next++] = '/';
		memcpy(rel + next, ns.name[i], len);
		next += len;
		rel[next] = '\0';

		if (ns.type[i] == DT_DIR) {
			struct path child;

			if (vfs_path_lookup(dir->dentry, dir->mnt,
					    ns.name[i], 0, &child))
				continue;
			ret = tx_fs_walk_rel(&child, rel, rellen, next,
					     depth + 1, fn, arg);
			path_put(&child);
			if (ret)
				break;
		} else {
			/*
			 * A whiteout is a char device 0:0 in the upper layer,
			 * and it means the transaction DELETED this path.
			 * A delete conflicts with another transaction's write
			 * to the same path just as a write does, so it counts.
			 */
			ret = fn(rel, arg);
			if (ret)
				break;
		}
	}

	tx_names_free(&ns);
	return ret;
}

int tx_fs_writeset_each(tx_id_t tx_id, tx_fs_path_fn fn, void *arg)
{
	struct path upper;
	char *rel;
	int ret;

	ret = tx_fs_lookup(tx_id, "upper", &upper);
	if (ret)
		return 0;		/* nothing written; not an error */

	rel = kzalloc(PATH_MAX, GFP_KERNEL);
	if (!rel) {
		path_put(&upper);
		return -ENOMEM;
	}

	ret = tx_fs_walk_rel(&upper, rel, PATH_MAX, 0, 0, fn, arg);

	kfree(rel);
	path_put(&upper);
	return ret;
}
EXPORT_SYMBOL_GPL(tx_fs_writeset_each);
