// SPDX-License-Identifier: GPL-2.0
/*
 * src/core/commit.c --- the two-phase commit sequence.  Fragment P1-07.
 *
 * THE ORDERING, and why it is this way round
 * ------------------------------------------
 * Commit runs the filesystem first and the effects second.
 *
 * Both halves can fail.  The asymmetry is that a filesystem merge that
 * fails leaves data we still hold -- the upper layer is intact and the
 * merge can be retried or discarded -- whereas an effect flush that fails
 * has already put some packets on the wire.  Emission is the irreversibility
 * boundary this entire project is built around, so it goes last: the step
 * that cannot be undone is the step taken when every step that can be
 * undone has already succeeded.
 *
 * Abort runs the reverse: effects are discarded first (they never left,
 * so this cannot fail in a way that matters), then the filesystem upper
 * layer is dropped.
 *
 * PARTIAL FAILURE
 * ---------------
 * The tracker asks: what if fs commits and effects fail?  The answer is
 * TX_STATE_FAILED, and it is a distinct state from both DONE and ABORTED
 * precisely because the transaction is now neither.  The files are real.
 * Some effects are on the wire.  The rest are still in the WAL and will be
 * discarded.  We cannot repair that from inside the kernel -- we have no
 * application semantics with which to compensate -- so the honest thing is
 * to name the state, report the counts, and make the supervisor deal with
 * it.  Silently reporting success here would be the worst available
 * behaviour, and it is the easy one to write.
 *
 * LOCKING
 * -------
 * tx_fs_commit() and tx_eff_flush() may sleep.  ctx->lock is a spinlock.
 * Therefore: take the lock, transition, DROP THE LOCK, call the provider,
 * retake the lock, transition again.  Every provider call below is outside
 * the lock and the code is shaped so that is visually obvious.  This is
 * the file where the rule is easy to break, which is why it is restated
 * here rather than only in core.h.
 *
 * Owner: P1.
 */

#define pr_fmt(fmt) "agenttx: " fmt

#include <linux/module.h>
#include <linux/printk.h>

#include "core.h"

/*
 * Move to @to, reporting whether the machine allowed it.  Exists so the
 * lock/unlock pair around a single transition does not get copy-pasted
 * eight times below, each with its own chance of an early return that
 * forgets the unlock.
 */
static int tx_transition(struct tx_ctx *ctx, enum tx_state to)
{
	int ret;

	spin_lock(&ctx->lock);
	ret = tx_state_set(ctx, to);
	spin_unlock(&ctx->lock);
	return ret;
}

int tx_do_commit(struct tx_ctx *ctx, u32 reason, u64 *n_effects, u64 *n_files)
{
	u64 files = 0, effects = 0;
	int fs_ret, eff_ret, ret;
	bool was_doomed;

	spin_lock(&ctx->lock);
	was_doomed = (ctx->state == TX_STATE_DOOMED);
	ret = tx_state_set(ctx, TX_STATE_COMMITTING);
	spin_unlock(&ctx->lock);
	if (ret)
		return ret;

	if (was_doomed)
		pr_info("tx=%llu: committing a DOOMED transaction (worst class %s) -- this was the only exit\n",
			(unsigned long long)ctx->tx_id,
			tx_class_name(ctx->worst_class));

	/*
	 * P2-08.  If the lower layer moved underneath us the merge is
	 * semantically questionable: we are about to overwrite somebody
	 * else's write with a value computed from a stale read.  We do not
	 * refuse -- P2 owns that policy and the answer is documented there --
	 * but it is recorded, because a commit that silently lost a
	 * concurrent edit is the kind of thing that must never be invisible.
	 */
	if (tx_fs_extmod_check(ctx->tx_id) > 0)
		pr_warn("tx=%llu: the lower layer changed during this transaction; committing anyway\n",
			(unsigned long long)ctx->tx_id);

	/* ---- phase 1: the undoable half ---- */
	fs_ret = tx_fs_commit(ctx->tx_id, &files);
	if (fs_ret) {
		pr_err("tx=%llu: fs commit failed (%d); NOT flushing effects\n",
		       (unsigned long long)ctx->tx_id, fs_ret);
		/*
		 * Nothing was emitted, so the effects can still be discarded
		 * cleanly.  This is the good failure: the transaction did not
		 * happen at all.
		 */
		tx_eff_discard(ctx->tx_id, &effects);
		tx_transition(ctx, TX_STATE_FAILED);
		goto out;
	}

	/* ---- phase 2: the point of no return ---- */
	eff_ret = tx_eff_flush(ctx->tx_id, &effects);
	if (eff_ret) {
		/*
		 * The bad failure, and the one the tracker asks about.  Files
		 * are merged.  An unknown prefix of the WAL is on the wire.
		 * We cannot roll either back.
		 */
		pr_err("tx=%llu: PARTIAL COMMIT -- %llu file(s) merged, effect flush failed (%d) after %llu effect(s)\n",
		       (unsigned long long)ctx->tx_id,
		       (unsigned long long)files, eff_ret,
		       (unsigned long long)effects);
		pr_err("tx=%llu: state FAILED; the supervisor must reconcile -- the kernel cannot\n",
		       (unsigned long long)ctx->tx_id);
		tx_transition(ctx, TX_STATE_FAILED);
		ret = eff_ret;
		goto out;
	}

	ret = tx_transition(ctx, TX_STATE_DONE);
	pr_info("tx=%llu COMMIT reason=%u files=%llu effects=%llu\n",
		(unsigned long long)ctx->tx_id, reason,
		(unsigned long long)files, (unsigned long long)effects);

out:
	spin_lock(&ctx->lock);
	ctx->n_written  = files;
	ctx->n_deferred = 0;
	spin_unlock(&ctx->lock);

	if (n_files)
		*n_files = files;
	if (n_effects)
		*n_effects = effects;

	/*
	 * Drop every wait-for edge touching this transaction. Leaving one
	 * behind lets a finished transaction hold a cycle open forever, and
	 * the detector then reports a deadlock between parties one of which
	 * no longer exists.
	 */
	tx_wait_drop_all(ctx->tx_id);
	tx_ctx_unlink(ctx);
	return ret;
}

int tx_do_abort(struct tx_ctx *ctx, u32 reason, u64 *n_effects, u64 *n_files)
{
	u64 files = 0, effects = 0;
	int fs_ret, eff_ret, ret;

	/*
	 * A DOOMED transaction cannot be aborted, and the refusal happens
	 * here rather than being left to the state machine's WARN, because
	 * this is a legitimate request from userspace rather than an
	 * internal bug: the agent asked for something the semantics forbid.
	 * -EPERM, not a WARN_ON backtrace.
	 */
	spin_lock(&ctx->lock);
	if (ctx->state == TX_STATE_DOOMED) {
		spin_unlock(&ctx->lock);
		pr_warn("tx=%llu: ABORT refused -- transaction is DOOMED (worst class %s); it can only be committed\n",
			(unsigned long long)ctx->tx_id,
			tx_class_name(ctx->worst_class));
		return -EPERM;
	}
	ret = tx_state_set(ctx, TX_STATE_ABORTING);
	spin_unlock(&ctx->lock);
	if (ret)
		return ret;

	/* ---- effects first: they never left, so this is free ---- */
	eff_ret = tx_eff_discard(ctx->tx_id, &effects);
	if (eff_ret)
		pr_err("tx=%llu: effect discard failed (%d); continuing to drop the fs layer anyway\n",
		       (unsigned long long)ctx->tx_id, eff_ret);

	/* ---- then the filesystem upper layer ---- */
	fs_ret = tx_fs_abort(ctx->tx_id, &files);
	if (fs_ret)
		pr_err("tx=%llu: fs abort failed (%d) -- upper layer may be left on disk\n",
		       (unsigned long long)ctx->tx_id, fs_ret);

	/*
	 * Abort reaches ABORTED even when a provider complained.  The
	 * alternative -- leaving it in ABORTING -- would strand the context
	 * in a non-terminal state forever and leak it at rmmod.  The errors
	 * are logged and returned; the state machine still finishes.
	 */
	tx_transition(ctx, TX_STATE_ABORTED);

	pr_info("tx=%llu ABORT reason=%u files_dropped=%llu effects_dropped=%llu\n",
		(unsigned long long)ctx->tx_id, reason,
		(unsigned long long)files, (unsigned long long)effects);

	spin_lock(&ctx->lock);
	ctx->n_written  = 0;
	ctx->n_deferred = 0;
	spin_unlock(&ctx->lock);

	if (n_files)
		*n_files = files;
	if (n_effects)
		*n_effects = effects;

	tx_wait_drop_all(ctx->tx_id);
	tx_ctx_unlink(ctx);
	return eff_ret ? eff_ret : fs_ret;
}
