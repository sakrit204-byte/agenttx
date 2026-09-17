/* SPDX-License-Identifier: GPL-2.0 */
/*
 * src/core/core.h --- P1-internal contract.
 *
 * NOT the project contract.  include/agenttx.h is the thing that is frozen
 * and that the other three streams compile against; this header is private
 * to src/core/ and may change without a contract-change PR.
 *
 * The split matters: if a symbol needs to be visible to P2, P3 or P4 it
 * belongs in include/agenttx.h and needs four approvals.  If it is only
 * how P1 talks to itself, it belongs here and needs none.
 *
 * Owner: P1.
 */

#ifndef _AGENTTX_CORE_H
#define _AGENTTX_CORE_H

#include <linux/hashtable.h>
#include <linux/refcount.h>
#include <linux/rcupdate.h>
#include <linux/spinlock.h>
#include <linux/types.h>

#include "agenttx.h"

#define pr_fmt_tx "agenttx: "

/*
 * Hashtable sizing.  Keyed by tgid (P1-05).  256 buckets: an agent host
 * runs tens of transacting processes, not thousands, and the table is
 * read on every BPF hook invocation, so we want it to stay in L1 rather
 * than to scale to a workload we do not have.  If that assumption ever
 * breaks the fix is a per-task pointer (P1-12), not a bigger table.
 */
#define TX_HASH_BITS	8

/*
 * struct tx_ctx --- one live transaction.
 *
 * LOCKING.  Two different disciplines, deliberately:
 *
 *   the table   RCU for lookup, tx_table_lock for mutation.  Lookup is on
 *               the hot path -- every LSM hook calls tx_current_id() --
 *               and hooks can run in non-sleepable context, so the read
 *               side must not sleep and must not contend.
 *
 *   the context ctx->lock (spinlock) for every field below the marker.
 *               Held across a state transition, never across a call into
 *               P2 or P3: tx_fs_commit() and tx_eff_flush() can sleep.
 *               commit.c is where that rule is easy to break; see the
 *               comment there.
 *
 * A reader that found us under RCU may still be looking at us after we
 * leave the table, which is why the free goes through kfree_rcu() and why
 * refcount is a refcount_t rather than an int.
 */
struct tx_ctx {
	struct hlist_node	node;		/* tx_table */
	struct rcu_head		rcu;

	tx_id_t			tx_id;		/* immutable after create */
	u32			tgid;		/* immutable; the hash key  */
	u32			owner_pid;	/* immutable                */
	u32			flags;		/* immutable; TX_F_*        */

	refcount_t		refcount;

	/* ---- everything below is under ctx->lock ---- */
	spinlock_t		lock;

	enum tx_state		state;
	enum tx_class		worst_class;	/* monotonic: only ever max()ed */

	u64			n_deferred;	/* effects held in the WAL      */
	u64			n_written;	/* inodes in the upper layer    */

	s64			deadline_ns;	/* -1 when there is none        */
	u32			supervisor_pid;	/* 0 until one registers        */
};

/* ---------------------------------------------------------------- */
/* ctx.c  (P1-05)                                                    */
/* ---------------------------------------------------------------- */

int  tx_ctx_init(void);
void tx_ctx_exit(void);

/*
 * Create a context for @tgid.  Fails with -EEXIST if that tgid already has
 * a live transaction: we flatten rather than nest (P1-09), and flattening
 * is a decision the caller has to make, not something ctx.c does silently.
 */
int  tx_ctx_create(u32 tgid, u32 owner_pid, u32 flags, u32 timeout_ms,
		   struct tx_ctx **out);

/* Both return a context with a reference taken, or NULL. */
struct tx_ctx *tx_ctx_get_by_tgid(u32 tgid);
struct tx_ctx *tx_ctx_get_by_id(tx_id_t tx_id);

void tx_ctx_put(struct tx_ctx *ctx);

/* Unlink from the table.  The caller still holds its own reference. */
void tx_ctx_unlink(struct tx_ctx *ctx);

/* For the exit hook (P1-08) and module teardown: iterate every live ctx. */
typedef void (*tx_ctx_visit_fn)(struct tx_ctx *ctx, void *arg);
void tx_ctx_for_each(tx_ctx_visit_fn fn, void *arg);

unsigned int tx_ctx_count(void);

/* Exact-tgid lookup, no ancestor walk: only the opener ends a tx by dying. */
tx_id_t tx_owned_id(void);

/* True if current, or any ancestor within the walk bound, owns @ctx. */
bool tx_ctx_covers_current(const struct tx_ctx *ctx);

/* Resolve the transaction current is inside, taking a reference. */
struct tx_ctx *tx_ctx_get_inherited(void);

/* ---------------------------------------------------------------- */
/* state.c  (P1-06)                                                  */
/* ---------------------------------------------------------------- */

/*
 * The whole legality question lives in one place.  Every transition in
 * the system goes through tx_state_set(); there is no other writer of
 * ctx->state, and state.c has a BUILD_BUG-style table to prove the set of
 * legal edges matches the one documented in include/agenttx.h.
 *
 * Returns 0, or -EINVAL for an illegal transition (and WARNs, because an
 * illegal transition is a bug in us, not bad input from userspace).
 * Caller must hold ctx->lock.
 */
int  tx_state_set(struct tx_ctx *ctx, enum tx_state to);
bool tx_state_is_legal(enum tx_state from, enum tx_state to);
bool tx_state_is_terminal(enum tx_state s);
const char *tx_state_name(enum tx_state s);
const char *tx_class_name(enum tx_class c);
int  tx_state_selfcheck(void);

/* ---------------------------------------------------------------- */
/* commit.c  (P1-07)                                                 */
/* ---------------------------------------------------------------- */

int  tx_do_commit(struct tx_ctx *ctx, u32 reason, u64 *n_effects, u64 *n_files);
int  tx_do_abort (struct tx_ctx *ctx, u32 reason, u64 *n_effects, u64 *n_files);

/* ---------------------------------------------------------------- */
/* ioctl.c  (P1-04)                                                  */
/* ---------------------------------------------------------------- */

long tx_ioctl(struct file *filp, unsigned int cmd, unsigned long arg);

/* ---------------------------------------------------------------- */
/* exit.c  (P1-08)                                                   */
/* ---------------------------------------------------------------- */

int  tx_exit_hook_install(void);
void tx_exit_hook_remove(void);

/* ---------------------------------------------------------------- */
/* main.c                                                            */
/* ---------------------------------------------------------------- */

extern atomic64_t tx_next_id;

/* kfunc.c (P1-10) */
int  tx_kfunc_register(void);

/* ---------------------------------------------------------------- */
/* waitfor.c  (P1-15 .. P1-18)                                       */
/* ---------------------------------------------------------------- */
int  tx_waitfor_init(void);
void tx_waitfor_exit(void);
unsigned int tx_wait_count(void);

#endif /* _AGENTTX_CORE_H */
