// SPDX-License-Identifier: GPL-2.0
/*
 * src/fs/abort.c --- discard the upper layer.  Fragment P2-04.
 *
 * THE M1 DEMO PATH:  echo hi > f  ;  tx_abort  ;  the file is gone.
 *
 * What makes that true is that the write never touched the lower layer at
 * all -- overlayfs put it in `upper/`, and abort deletes `upper/`.  There
 * is no undo log and nothing to replay: the rollback is a recursive
 * unlink of a directory the agent's writes were diverted into.  That is
 * the entire argument for copy-on-write as the storage substrate, and it
 * is why abort is cheap enough to be the deadlock recovery primitive
 * (docs/deadlock.md).
 *
 * TWO THINGS THAT ARE EASY TO GET WRONG HERE
 *
 * 1. iterate_dir() holds the directory's i_rwsem for the whole walk. A
 *    vfs_unlink() from inside the filldir actor deadlocks against it, and
 *    it presents as a hang rather than as a bug. So: collect every name,
 *    finish the walk, and only then mutate.
 *
 * 2. Recursion depth is bounded. The kernel stack is 16 KiB. An agent that
 *    creates a 10 000-deep tree inside a transaction would otherwise
 *    overflow it during its own rollback -- a denial of service reachable
 *    by an ordinary `mkdir -p`. TX_FS_MAX_DEPTH caps it and abort fails
 *    loudly rather than half-finishing.
 *
 * Owner: P2.
 */

#define pr_fmt(fmt) "agenttx/fs: " fmt

#include <linux/dcache.h>
#include <linux/fs.h>
#include <linux/module.h>
#include <linux/namei.h>
#include <linux/slab.h>

#include "fsint.h"

/* Resolve one name below @parent into its own struct path. */
static int tx_child_path(struct path *parent, const char *name,
			 struct path *out)
{
	return vfs_path_lookup(parent->dentry, parent->mnt, name, 0, out);
}

/*
 * Remove one entry from @parent.  Takes the parent's i_rwsem, which is
 * what lookup_one_len() requires and what vfs_unlink()/vfs_rmdir() expect.
 */
static int tx_remove_one(struct path *parent, const char *name, bool isdir)
{
	struct inode *pdir = d_inode(parent->dentry);
	struct dentry *child;
	int ret;

	ret = mnt_want_write(parent->mnt);
	if (ret)
		return ret;

	inode_lock_nested(pdir, I_MUTEX_PARENT);

	child = lookup_one_len(name, parent->dentry, strlen(name));
	if (IS_ERR(child)) {
		ret = PTR_ERR(child);
		goto unlock;
	}
	if (d_really_is_negative(child)) {
		/* Vanished under us.  Nothing to do, and not an error: the
		 * postcondition we want is "it is not there". */
		ret = 0;
		dput(child);
		goto unlock;
	}

	if (isdir)
		ret = vfs_rmdir(mnt_idmap(parent->mnt), pdir, child);
	else
		ret = vfs_unlink(mnt_idmap(parent->mnt), pdir, child, NULL);

	dput(child);
unlock:
	inode_unlock(pdir);
	mnt_drop_write(parent->mnt);
	return ret;
}

/*
 * Depth-first removal of everything inside @dir.  @dir itself survives.
 *
 * Two passes rather than one, deliberately.  Emptying subdirectories first,
 * while holding no ancestor's lock, keeps the locking flat: every mutation
 * below happens with exactly one inode lock held, its own parent's. A
 * single recursive pass would hold every ancestor lock at the deepest
 * point, which lockdep would rightly complain about and which would make
 * the depth bound a lock-nesting bound as well.
 */
int tx_fs_rm_contents(struct path *dir, int depth, u64 *removed)
{
	struct tx_names ns;
	unsigned int i;
	int ret;

	if (depth > TX_FS_MAX_DEPTH) {
		pr_err("abort: tree deeper than %d; refusing to recurse further\n",
		       TX_FS_MAX_DEPTH);
		return -ELOOP;
	}

	ret = tx_dir_collect(dir, &ns);
	if (ret)
		return ret;

	/* pass 1: empty the subdirectories */
	for (i = 0; i < ns.n; i++) {
		struct path child;

		if (ns.type[i] != DT_DIR)
			continue;
		ret = tx_child_path(dir, ns.name[i], &child);
		if (ret) {
			if (ret == -ENOENT) {
				ret = 0;
				continue;
			}
			goto out;
		}
		ret = tx_fs_rm_contents(&child, depth + 1, removed);
		path_put(&child);
		if (ret)
			goto out;
	}

	/* pass 2: remove every entry, now that directories are empty */
	for (i = 0; i < ns.n; i++) {
		ret = tx_remove_one(dir, ns.name[i], ns.type[i] == DT_DIR);
		if (ret) {
			pr_err("abort: cannot remove %s: %d\n", ns.name[i], ret);
			goto out;
		}
		if (removed)
			(*removed)++;
	}

out:
	tx_names_free(&ns);
	return ret;
}

int tx_fs_abort(tx_id_t tx_id, __u64 *n_files)
{
	struct path upper;
	u64 removed = 0;
	int ret;

	ret = tx_fs_lookup(tx_id, "upper", &upper);
	if (ret) {
		/*
		 * No upper layer.  Either the transaction never wrote
		 * anything, or begin failed before creating it.  Nothing to
		 * roll back either way, and reporting an error here would
		 * make every abort of a read-only transaction look broken.
		 */
		pr_debug("tx=%llu: no upper layer to discard (%d)\n",
			 (unsigned long long)tx_id, ret);
		if (n_files)
			*n_files = 0;
		return 0;
	}

	ret = tx_fs_rm_contents(&upper, 0, &removed);
	path_put(&upper);

	if (ret) {
		pr_err("tx=%llu: abort left the upper layer partly on disk: %d\n",
		       (unsigned long long)tx_id, ret);
	} else {
		pr_info("tx=%llu ABORT discarded %llu upper-layer entr%s -- the writes never happened\n",
			(unsigned long long)tx_id, (unsigned long long)removed,
			removed == 1 ? "y" : "ies");
	}

	if (n_files)
		*n_files = removed;
	return ret;
}
EXPORT_SYMBOL_GPL(tx_fs_abort);
