// SPDX-License-Identifier: GPL-2.0
/*
 * src/fs/commit.c --- merge the upper layer into the lower.  Fragment P2-05.
 *
 * Abort is easy: delete the upper layer. Commit is where the subtlety is,
 * and the tracker names it -- "Deletes inside a tx are whiteouts, and
 * merging them is the subtle case."
 *
 * THE THREE KINDS OF UPPER-LAYER ENTRY
 *
 *   whiteout    a character device with rdev 0:0 (IS_WHITEOUT). It records
 *               that the agent DELETED a file which exists in the lower
 *               layer. Merging it means unlinking the lower file -- NOT
 *               copying a device node down. Getting this wrong is silent:
 *               the commit "succeeds" and the deleted file is still there,
 *               so a transaction that removed a secret did not remove it.
 *
 *   directory   merged recursively. It may be *opaque*
 *               (trusted.overlay.opaque="y"), meaning the agent replaced
 *               the whole directory rather than adding to it, so the lower
 *               contents must be dropped first. We detect it and refuse
 *               rather than silently doing the additive thing -- see below.
 *
 *   anything else   moved down, replacing whatever was there.
 *
 * ATOMICITY, HONESTLY
 *
 * This merge is NOT atomic. It is a sequence of renames, and a crash in the
 * middle leaves the lower layer half-updated. P2-09 (crash consistency) is
 * the fragment that addresses it, and until that lands the honest statement
 * is that AgentTx gives atomic *visibility* of effects (the WAL holds them
 * until commit) and non-atomic durability of files. Claiming otherwise
 * would be claiming P2-09's work.
 *
 * Owner: P2.
 */

#define pr_fmt(fmt) "agenttx/fs: " fmt

#include <linux/dcache.h>
#include <linux/fs.h>
#include <linux/module.h>
#include <linux/namei.h>
#include <linux/slab.h>
#include <linux/xattr.h>

#include "fsint.h"

#define OVL_XATTR_OPAQUE	"trusted.overlay.opaque"

static bool tx_is_whiteout(struct dentry *d)
{
	struct inode *i = d_inode(d);

	/* overlayfs marks a deletion as a chardev 0:0; IS_WHITEOUT is that
	 * test, and it is the only reliable one. */
	return i && IS_WHITEOUT(i);
}

static bool tx_is_opaque(struct dentry *d)
{
	char val[2];
	int n;

	if (!d_inode(d) || !d_is_dir(d))
		return false;
	n = vfs_getxattr(&nop_mnt_idmap, d, OVL_XATTR_OPAQUE, val, sizeof(val));
	return n == 1 && val[0] == 'y';
}

/* Ensure @name exists as a directory under @parent. */
static int tx_ensure_dir(struct path *parent, const char *name)
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
	if (d_really_is_positive(child))
		ret = d_is_dir(child) ? 0 : -ENOTDIR;
	else
		ret = vfs_mkdir(mnt_idmap(parent->mnt), pdir, child, 0700);
	dput(child);
unlock:
	inode_unlock(pdir);
	mnt_drop_write(parent->mnt);
	return ret;
}

/* Remove @name under @parent, whatever it is.  Used to apply a whiteout. */
static int tx_unlink_name(struct path *parent, const char *name)
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
		ret = 0;			/* already absent: the goal */
	} else if (d_is_dir(child)) {
		struct path sub = { .mnt = parent->mnt, .dentry = child };
		u64 dropped = 0;

		inode_unlock(pdir);
		ret = tx_fs_rm_contents(&sub, 0, &dropped);
		inode_lock_nested(pdir, I_MUTEX_PARENT);
		if (!ret)
			ret = vfs_rmdir(mnt_idmap(parent->mnt), pdir, child);
	} else {
		ret = vfs_unlink(mnt_idmap(parent->mnt), pdir, child, NULL);
	}
	dput(child);
unlock:
	inode_unlock(pdir);
	mnt_drop_write(parent->mnt);
	return ret;
}

/* Move @name from @from into @to, replacing whatever is there. */
static int tx_move_down(struct path *from, struct path *to, const char *name)
{
	struct inode *fdir = d_inode(from->dentry);
	struct inode *tdir = d_inode(to->dentry);
	struct dentry *old = NULL, *new = NULL;
	struct renamedata rd = {};
	int ret;

	ret = mnt_want_write(to->mnt);
	if (ret)
		return ret;

	lock_rename(from->dentry, to->dentry);

	old = lookup_one_len(name, from->dentry, strlen(name));
	if (IS_ERR(old)) {
		ret = PTR_ERR(old);
		old = NULL;
		goto unlock;
	}
	new = lookup_one_len(name, to->dentry, strlen(name));
	if (IS_ERR(new)) {
		ret = PTR_ERR(new);
		new = NULL;
		goto unlock;
	}

	rd.old_mnt_idmap = mnt_idmap(from->mnt);
	rd.old_dir	 = fdir;
	rd.old_dentry	 = old;
	rd.new_mnt_idmap = mnt_idmap(to->mnt);
	rd.new_dir	 = tdir;
	rd.new_dentry	 = new;

	ret = vfs_rename(&rd);

unlock:
	dput(old);
	dput(new);
	unlock_rename(from->dentry, to->dentry);
	mnt_drop_write(to->mnt);

	if (ret == -EXDEV) {
		/*
		 * upper/ and lower/ are on different filesystems, so the merge
		 * would have to be a copy rather than a rename. We do not do
		 * that silently: a copy has different cost, different atomicity
		 * and different inode identity, and a commit that quietly
		 * became a copy is a commit whose semantics changed without
		 * anyone deciding.
		 */
		pr_err("merge: %s crosses a filesystem boundary; upper/ and lower/ must share a filesystem\n",
		       name);
	}
	return ret;
}

/* Recursive merge of @upper into @lower. */
static int tx_merge(struct path *upper, struct path *lower, int depth,
		    u64 *merged, u64 *deleted)
{
	struct tx_names ns;
	unsigned int i;
	int ret;

	if (depth > TX_FS_MAX_DEPTH)
		return -ELOOP;

	ret = tx_dir_collect(upper, &ns);
	if (ret)
		return ret;

	for (i = 0; i < ns.n; i++) {
		const char *name = ns.name[i];
		struct path uchild;
		struct dentry *ud;

		ret = vfs_path_lookup(upper->dentry, upper->mnt, name, 0, &uchild);
		if (ret) {
			if (ret == -ENOENT) {
				ret = 0;
				continue;
			}
			break;
		}
		ud = uchild.dentry;

		if (tx_is_whiteout(ud)) {
			/* The agent deleted this. Apply that to the lower layer. */
			ret = tx_unlink_name(lower, name);
			if (!ret && deleted)
				(*deleted)++;
			path_put(&uchild);
			if (ret)
				break;
			continue;
		}

		if (d_is_dir(ud)) {
			struct path lchild;
			bool opaque = tx_is_opaque(ud);
			int have_lower;

			/*
			 * OPAQUE IS NOT THE SAME AS "REPLACED".
			 *
			 * overlayfs marks *any* directory it creates in the
			 * upper layer as opaque, so that lookups inside it do
			 * not fall through to a lower directory of the same
			 * name. A brand-new `mkdir` is therefore opaque too.
			 *
			 * What distinguishes wholesale replacement is whether
			 * a counterpart exists in the lower layer:
			 *
			 *   opaque + lower exists  -> the agent replaced a
			 *                             directory. The lower
			 *                             contents must go, or the
			 *                             merge resurrects files
			 *                             the agent removed.
			 *   opaque + no lower      -> just a new directory.
			 *                             Merge its contents; there
			 *                             is nothing to drop.
			 *
			 * The first version of this refused on `opaque` alone
			 * and so failed on an ordinary `mkdir -p a/b` inside a
			 * transaction, which is about as common as it gets.
			 */
			have_lower = vfs_path_lookup(lower->dentry, lower->mnt,
						     name, 0, &lchild);

			if (opaque && have_lower == 0) {
				u64 dropped = 0;

				pr_info("merge: %s was replaced wholesale; dropping %s lower contents\n",
					name, name);
				ret = tx_fs_rm_contents(&lchild, 0, &dropped);
				if (ret) {
					path_put(&lchild);
					path_put(&uchild);
					break;
				}
				if (deleted)
					*deleted += dropped;
			}

			if (have_lower) {
				ret = tx_ensure_dir(lower, name);
				if (!ret)
					ret = vfs_path_lookup(lower->dentry,
							      lower->mnt, name,
							      0, &lchild);
				if (ret) {
					path_put(&uchild);
					break;
				}
			}

			ret = tx_merge(&uchild, &lchild, depth + 1,
				       merged, deleted);
			path_put(&lchild);
			path_put(&uchild);
			if (ret)
				break;
			continue;
		}

		path_put(&uchild);
		ret = tx_move_down(upper, lower, name);
		if (ret)
			break;
		if (merged)
			(*merged)++;
	}

	tx_names_free(&ns);
	return ret;
}

int tx_fs_commit(tx_id_t tx_id, __u64 *n_files)
{
	struct path upper, lower, upper_keep = {};
	u64 merged = 0, deleted = 0;
	int ret;

	if (n_files)
		*n_files = 0;

	ret = tx_fs_lookup(tx_id, "upper", &upper);
	if (ret) {
		pr_debug("tx=%llu: nothing to merge\n", (unsigned long long)tx_id);
		return 0;
	}

	/*
	 * `lower` is a symlink the supervisor created pointing at the
	 * directory under protection (fsint.h explains why a symlink rather
	 * than an ioctl argument). No symlink means the transaction was
	 * never given a lower layer, and there is nowhere to merge TO.
	 * That is a configuration error, not a no-op: merging into nothing
	 * and reporting success would lose the agent's work.
	 */
	ret = tx_fs_lookup(tx_id, "lower", &lower);
	if (ret) {
		u64 n = 0;

		tx_fs_count(&upper, 0, &n);
		path_put(&upper);
		if (n == 0)
			return 0;		/* nothing written; harmless */
		pr_err("tx=%llu: %llu upper-layer entr%s and no lower/ symlink -- refusing to commit into nowhere\n",
		       (unsigned long long)tx_id, (unsigned long long)n,
		       n == 1 ? "y" : "ies");
		return -ENOENT;
	}

	ret = tx_merge(&upper, &lower, 0, &merged, &deleted);

	path_put(&lower);
	/* upper is still needed by the sweep below; released after it. */
	upper_keep = upper;

	if (ret) {
		/*
		 * Deliberately leave the upper layer on disk. A failed merge
		 * is the one case where the CoW area is evidence: it holds
		 * exactly the work that did not make it down, and discarding
		 * it would destroy the only record of what a partial commit
		 * left behind.
		 */
		pr_err("tx=%llu: MERGE FAILED after %llu file(s) and %llu deletion(s): %d -- the lower layer is partly updated; upper/ kept for inspection\n",
		       (unsigned long long)tx_id, (unsigned long long)merged,
		       (unsigned long long)deleted, ret);
	} else {
		u64 swept = 0;
		int rc;

		/*
		 * Drain the upper layer.
		 *
		 * The merge moves regular files down with rename(), but
		 * directories and spent whiteout nodes stay behind -- a
		 * directory cannot be renamed down because the lower one
		 * already exists, and a whiteout has been consumed by
		 * unlinking its lower counterpart. So a "successful" commit
		 * used to leave the CoW area full, which costs disk and, worse,
		 * makes "is upper/ empty?" useless as a statement about whether
		 * the transaction finished.
		 *
		 * Only on success. See the failure branch above.
		 */
		rc = tx_fs_rm_contents(&upper_keep, 0, &swept);
		if (rc)
			pr_warn("tx=%llu: merge succeeded but the CoW area could not be drained: %d\n",
				(unsigned long long)tx_id, rc);

		pr_info("tx=%llu COMMIT merged %llu file(s), applied %llu deletion(s), swept %llu leftover(s)\n",
			(unsigned long long)tx_id, (unsigned long long)merged,
			(unsigned long long)deleted, (unsigned long long)swept);
	}

	path_put(&upper_keep);

	if (n_files)
		*n_files = merged + deleted;
	return ret;
}
EXPORT_SYMBOL_GPL(tx_fs_commit);
