/* SPDX-License-Identifier: GPL-2.0 */
/*
 * src/fs/fsint.h --- P2-internal contract.  Not include/agenttx.h.
 *
 * THE LAYOUT, which is the whole interface between P2 and the supervisor:
 *
 *   <tx_root>/tx-<id>/
 *        upper/     the copy-on-write upper layer
 *        work/      overlayfs's private workdir (must share a fs with upper)
 *        merged/    the mountpoint the agent sees
 *        lower      a symlink to the directory being protected
 *
 * Why a symlink rather than an ioctl argument: `struct tx_begin_arg` in the
 * frozen contract carries no path, and adding one is a contract-change PR
 * needing four approvals (WORKFLOW.md Rule 1).  A symlink the supervisor
 * creates after BEGIN communicates the lower directory with no ABI change
 * at all.  If P2 later needs richer per-transaction configuration, THAT is
 * when the contract change is worth spending.
 *
 * WHO MOUNTS.  The supervisor does, from userspace, in a private mount
 * namespace.  `do_add_mount()` is not exported to modules, so a module
 * cannot graft a mount into a namespace however much of the fs_context API
 * it is allowed to call.  This is not a workaround: it is how container
 * runtimes do it, and "the agent's private mount namespace" in PROPOSAL.md
 * is precisely a userspace unshare.  The kernel owns what is actually hard
 * and actually transactional --- discard, merge, write-set, external
 * modification --- and that is all in this directory.
 *
 * Owner: P2.
 */

#ifndef _AGENTTX_FS_INT_H
#define _AGENTTX_FS_INT_H

#include <linux/fs.h>
#include <linux/namei.h>
#include <linux/path.h>

#include "agenttx.h"

/* Where per-transaction state lives.  Module parameter `tx_root`. */
extern char *tx_fs_root;

/*
 * Recursion bound for the tree walks below.  The kernel stack is 16 KiB and
 * each frame here is not small; an agent that creates a 10 000-deep tree
 * inside a transaction must not be able to overflow it on abort.  Hitting
 * the cap is reported as -ELOOP and fails the abort loudly rather than
 * leaving a half-discarded upper layer that looks like success.
 */
#define TX_FS_MAX_DEPTH	32

/* Names collected from one directory, so iteration and mutation do not
 * interleave.  iterate_dir() holds i_rwsem; calling vfs_unlink() from
 * inside the actor would deadlock against it. */
struct tx_names {
	char		**name;
	unsigned char	*type;		/* DT_* */
	unsigned int	n, cap;
};

void tx_names_free(struct tx_names *ns);
int  tx_dir_collect(struct path *dir, struct tx_names *ns);

/* Build <tx_root>/tx-<id> and, optionally, a subdirectory of it. */
int  tx_fs_txdir(tx_id_t id, const char *sub, char *buf, size_t len);
int  tx_fs_lookup(tx_id_t id, const char *sub, struct path *out);

/* Depth-first removal of everything *inside* @dir.  @dir itself survives. */
int  tx_fs_rm_contents(struct path *dir, int depth, u64 *removed);

/* Count entries below @dir, recursively. */
int  tx_fs_count(struct path *dir, int depth, u64 *n);

#endif /* _AGENTTX_FS_INT_H */
