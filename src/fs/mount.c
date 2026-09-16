// SPDX-License-Identifier: GPL-2.0
/*
 * src/fs/mount.c --- per-transaction CoW area.  Fragment P2-03.
 *
 * Creates <tx_root>/tx-<id>/{upper,work,merged} when a transaction opens,
 * and carries the directory-walking helpers the rest of src/fs/ uses.
 *
 * The helpers are here rather than in a util.c because Kbuild lists this
 * directory's objects explicitly and adding a file to that list is an edit
 * to a change-controlled file.  Keeping them beside the code that creates
 * the tree is the smaller cost.
 *
 * Owner: P2.
 */

#define pr_fmt(fmt) "agenttx/fs: " fmt

#include <linux/dcache.h>
#include <linux/file.h>
#include <linux/fs.h>
#include <linux/module.h>
#include <linux/namei.h>
#include <linux/slab.h>
#include <linux/string.h>

#include "fsint.h"

char *tx_fs_root = "/var/lib/agenttx";
module_param_named(tx_root, tx_fs_root, charp, 0644);
MODULE_PARM_DESC(tx_root, "directory holding per-transaction CoW state");

int tx_fs_txdir(tx_id_t id, const char *sub, char *buf, size_t len)
{
	int n;

	if (sub)
		n = snprintf(buf, len, "%s/tx-%llu/%s", tx_fs_root,
			     (unsigned long long)id, sub);
	else
		n = snprintf(buf, len, "%s/tx-%llu", tx_fs_root,
			     (unsigned long long)id);
	return (n < 0 || (size_t)n >= len) ? -ENAMETOOLONG : 0;
}

int tx_fs_lookup(tx_id_t id, const char *sub, struct path *out)
{
	char buf[256];
	int ret;

	ret = tx_fs_txdir(id, sub, buf, sizeof(buf));
	if (ret)
		return ret;
	return kern_path(buf, LOOKUP_FOLLOW | LOOKUP_DIRECTORY, out);
}

/* mkdir -p, one component at a time, tolerating EEXIST. */
static int tx_mkdir(const char *path, umode_t mode)
{
	struct dentry *dentry;
	struct path parent;
	int ret;

	dentry = kern_path_create(AT_FDCWD, path, &parent, LOOKUP_DIRECTORY);
	if (IS_ERR(dentry)) {
		ret = PTR_ERR(dentry);
		return ret == -EEXIST ? 0 : ret;
	}
	ret = vfs_mkdir(mnt_idmap(parent.mnt), d_inode(parent.dentry),
			dentry, mode);
	if (ret == -EEXIST)
		ret = 0;
	done_path_create(&parent, dentry);
	return ret;
}

/* ------------------------------------------------------------------ */
/* directory listing                                                   */
/* ------------------------------------------------------------------ */

struct tx_collect {
	struct dir_context	ctx;
	struct tx_names		*ns;
	int			err;
};

static int names_push(struct tx_names *ns, const char *name, int len,
		      unsigned char type)
{
	char *copy;

	if (ns->n == ns->cap) {
		unsigned int cap = ns->cap ? ns->cap * 2 : 32;
		char **nn = krealloc(ns->name, cap * sizeof(*nn), GFP_KERNEL);
		unsigned char *nt;

		if (!nn)
			return -ENOMEM;
		ns->name = nn;
		nt = krealloc(ns->type, cap * sizeof(*nt), GFP_KERNEL);
		if (!nt)
			return -ENOMEM;
		ns->type = nt;
		ns->cap = cap;
	}
	copy = kmalloc(len + 1, GFP_KERNEL);
	if (!copy)
		return -ENOMEM;
	memcpy(copy, name, len);
	copy[len] = '\0';
	ns->name[ns->n] = copy;
	ns->type[ns->n] = type;
	ns->n++;
	return 0;
}

static bool tx_collect_actor(struct dir_context *ctx, const char *name,
			     int namlen, loff_t off, u64 ino,
			     unsigned int d_type)
{
	struct tx_collect *c = container_of(ctx, struct tx_collect, ctx);

	if (namlen == 1 && name[0] == '.')
		return true;
	if (namlen == 2 && name[0] == '.' && name[1] == '.')
		return true;

	c->err = names_push(c->ns, name, namlen, (unsigned char)d_type);
	return c->err == 0;
}

void tx_names_free(struct tx_names *ns)
{
	unsigned int i;

	for (i = 0; i < ns->n; i++)
		kfree(ns->name[i]);
	kfree(ns->name);
	kfree(ns->type);
	memset(ns, 0, sizeof(*ns));
}

/*
 * Read every name out of @dir first, and only then mutate.
 *
 * iterate_dir() holds the directory's i_rwsem for the whole walk, so
 * calling vfs_unlink() from inside the actor deadlocks against it -- and it
 * deadlocks in a way that looks like a hang rather than a bug, which is
 * worse.  Collect, release, then act.
 */
int tx_dir_collect(struct path *dir, struct tx_names *ns)
{
	struct tx_collect c = {
		.ctx = { .actor = tx_collect_actor, .pos = 0 },
		.ns = ns,
		.err = 0,
	};
	struct file *f;
	int ret;

	memset(ns, 0, sizeof(*ns));

	f = dentry_open(dir, O_RDONLY | O_DIRECTORY | O_NOATIME,
			current_cred());
	if (IS_ERR(f))
		return PTR_ERR(f);

	ret = iterate_dir(f, &c.ctx);
	fput(f);

	if (!ret)
		ret = c.err;
	if (ret)
		tx_names_free(ns);
	return ret;
}

/* ------------------------------------------------------------------ */
/* the provider entry point                                            */
/* ------------------------------------------------------------------ */

int tx_fs_begin(tx_id_t tx_id, const char *workdir)
{
	char buf[256];
	int ret;
	static const char * const subs[] = { NULL, "upper", "work", "merged" };
	unsigned int i;

	/*
	 * workdir is part of the frozen signature and is currently always
	 * NULL: the layout is a convention (fsint.h) rather than something
	 * the caller chooses.  Honour it if a caller ever passes one, so the
	 * parameter does not quietly become a lie.
	 */
	if (workdir && *workdir) {
		pr_debug("tx=%llu: caller supplied workdir %s (ignored; layout is by convention)\n",
			 (unsigned long long)tx_id, workdir);
	}

	ret = tx_mkdir(tx_fs_root, 0700);
	if (ret && ret != -EEXIST) {
		pr_err("tx=%llu: cannot create %s: %d\n",
		       (unsigned long long)tx_id, tx_fs_root, ret);
		return ret;
	}

	for (i = 0; i < ARRAY_SIZE(subs); i++) {
		ret = tx_fs_txdir(tx_id, subs[i], buf, sizeof(buf));
		if (ret)
			return ret;
		ret = tx_mkdir(buf, 0700);
		if (ret) {
			pr_err("tx=%llu: mkdir %s failed: %d\n",
			       (unsigned long long)tx_id, buf, ret);
			return ret;
		}
	}

	tx_fs_txdir(tx_id, NULL, buf, sizeof(buf));
	pr_info("tx=%llu CoW area ready at %s\n",
		(unsigned long long)tx_id, buf);
	return 0;
}
EXPORT_SYMBOL_GPL(tx_fs_begin);
