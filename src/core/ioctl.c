// SPDX-License-Identifier: GPL-2.0
/*
 * src/core/ioctl.c --- the user/kernel boundary.  Fragment P1-04.
 *
 * Tracker note: "Check every copy_from_user return. Never deref a user
 * pointer."  Both rules are absolute here and there are no exceptions
 * below, so a reviewer can check this file by grepping rather than by
 * reading.
 *
 * Three rules this file follows, and the reason for each:
 *
 *  1. Copy in, validate the copy, act, copy out.  The struct userspace
 *     handed us is re-read never -- a TOCTTOU between validating a field
 *     and using it is exactly the bug class the threat model names.
 *  2. Every struct carries an `abi` field which is checked first.  A
 *     supervisor built against a different contract gets -EPROTO, not
 *     undefined behaviour, and the version skew is legible in dmesg.
 *  3. The commit authority check lives here and nowhere else.
 *
 * On rule 3, from PROPOSAL.md: "tx_commit from inside the transaction
 * returns -EPERM; only the supervisor commits."  That single invariant is
 * what kills the premature-commit attack, so it is implemented once, in
 * tx_check_commit_authority(), and every commit path calls it.
 *
 * Owner: P1.
 */

#define pr_fmt(fmt) "agenttx: " fmt

#include <linux/capability.h>
#include <linux/fs.h>
#include <linux/module.h>
#include <linux/printk.h>
#include <linux/sched.h>
#include <linux/uaccess.h>

#include "core.h"

/*
 * A supervisor registers once and is remembered per transaction.  The
 * registration is global-ish for now (one supervisor pid per tx, recorded
 * at BEGIN if the registering process got there first); P1's phase-2 work
 * can make this a richer relationship, but the invariant it exists to
 * enforce must not weaken: the transacting tgid can never be the
 * committer.
 */
static u32 tx_registered_supervisor;	/* 0 = none; written under the table lock
					   discipline of ctx.c via ioctl only */

/*
 * THE invariant.  Returns 0 if @caller may commit @ctx, -EPERM otherwise.
 *
 * Deliberately conservative in both directions:
 *   - the transacting process may never commit itself, registered or not
 *   - if no supervisor ever registered, nobody may commit, and the
 *     transaction can only be aborted or reach its deadline
 *
 * The second half is the fail-closed reading.  An unsupervised transaction
 * that anyone may commit is strictly worse than one that nobody may commit,
 * because the attacker we are modelling has full control of agent output.
 */
static int tx_check_commit_authority(const struct tx_ctx *ctx, u32 caller_tgid)
{
	/*
	 * Membership, not equality.  Transaction membership is inherited by
	 * descendants (src/core/ctx.c), so comparing tgids alone would let
	 * the agent fork a child and have the child commit -- the
	 * premature-commit attack plus one fork().  Nobody inside the
	 * transaction may commit it, at any depth.
	 */
	if (tx_ctx_covers_current(ctx)) {
		pr_warn_ratelimited(
			"tx=%llu: COMMIT refused -- caller (tgid %u) is inside the transaction\n",
			(unsigned long long)ctx->tx_id, caller_tgid);
		return -EPERM;
	}

	if (caller_tgid == ctx->tgid) {
		pr_warn_ratelimited(
			"tx=%llu: COMMIT refused -- the transacting process (tgid %u) may not commit itself\n",
			(unsigned long long)ctx->tx_id, caller_tgid);
		return -EPERM;
	}

	if (ctx->supervisor_pid == 0) {
		pr_warn_ratelimited(
			"tx=%llu: COMMIT refused -- no supervisor registered\n",
			(unsigned long long)ctx->tx_id);
		return -EPERM;
	}

	if (caller_tgid != ctx->supervisor_pid) {
		pr_warn_ratelimited(
			"tx=%llu: COMMIT refused -- caller %u is not the supervisor (%u)\n",
			(unsigned long long)ctx->tx_id, caller_tgid,
			ctx->supervisor_pid);
		return -EPERM;
	}

	return 0;
}

/* ---------------------------------------------------------------- */

static long tx_ioctl_begin(void __user *uarg)
{
	struct tx_begin_arg arg;
	struct tx_ctx *ctx = NULL;
	u32 tgid = (u32)task_tgid_vnr(current);
	int ret;

	if (copy_from_user(&arg, uarg, sizeof(arg)))
		return -EFAULT;
	if (arg.abi != AGENTTX_ABI_VERSION)
		return -EPROTO;
	if (arg.flags & ~TX_F_ALL)
		return -EINVAL;

	/*
	 * P1-09: we flatten, and "inside a transaction" means INHERITED
	 * membership, not an exact tgid match.  A subprocess of a transacting
	 * agent that calls tx_begin joins the enclosing transaction; it does
	 * not open a second one.
	 *
	 * Checking the exact tgid here instead would make `sh -c 'tx_begin'`
	 * inside a transaction create a nested sibling, which is precisely
	 * the true-nesting semantics P1-09 decided against -- and it would
	 * do it silently, which is worse than deciding either way.
	 */
	ctx = tx_ctx_get_inherited();
	if (ctx) {
		arg.tx_id = ctx->tx_id;
		pr_debug("tx=%llu BEGIN flattened into the enclosing transaction (tgid %u)\n",
			 (unsigned long long)ctx->tx_id, tgid);
		tx_ctx_put(ctx);
		if (copy_to_user(uarg, &arg, sizeof(arg)))
			return -EFAULT;
		return 0;
	}

	ret = tx_ctx_create(tgid, (u32)task_pid_vnr(current),
			    arg.flags, arg.timeout_ms, &ctx);
	if (ret == -EEXIST) {
		/* Raced with another thread in our own group. Join it. */
		ctx = tx_ctx_get_by_tgid(tgid);
		if (!ctx)
			return -EAGAIN;
		arg.tx_id = ctx->tx_id;
		tx_ctx_put(ctx);
		if (copy_to_user(uarg, &arg, sizeof(arg)))
			return -EFAULT;
		return 0;
	}
	if (ret)
		return ret;

	spin_lock(&ctx->lock);
	ret = tx_state_set(ctx, TX_STATE_ACTIVE);
	if (!ret && tx_registered_supervisor &&
	    tx_registered_supervisor != tgid)
		ctx->supervisor_pid = tx_registered_supervisor;
	spin_unlock(&ctx->lock);

	if (ret) {
		tx_ctx_unlink(ctx);
		tx_ctx_put(ctx);
		return ret;
	}

	/*
	 * Tell the providers a transaction opened.  Order is fs then
	 * effects, matching the commit order in commit.c; if the effect
	 * layer cannot start we must undo the fs side before returning.
	 */
	ret = tx_fs_begin(ctx->tx_id, NULL);
	if (ret) {
		pr_err("tx=%llu: fs_begin failed: %d\n",
		       (unsigned long long)ctx->tx_id, ret);
		goto err_unlink;
	}
	ret = tx_eff_begin(ctx->tx_id, ctx->flags);
	if (ret) {
		u64 dropped = 0;

		pr_err("tx=%llu: eff_begin failed: %d\n",
		       (unsigned long long)ctx->tx_id, ret);
		tx_fs_abort(ctx->tx_id, &dropped);
		goto err_unlink;
	}

	arg.tx_id = ctx->tx_id;
	if (copy_to_user(uarg, &arg, sizeof(arg))) {
		/*
		 * We have a live transaction the caller will never learn the
		 * id of.  Leaving it would be a leak that only the exit hook
		 * could clean up, so tear it down here.
		 */
		u64 a = 0, b = 0;

		tx_do_abort(ctx, TX_REASON_UNSPEC, &a, &b);
		tx_ctx_unlink(ctx);
		tx_ctx_put(ctx);
		return -EFAULT;
	}

	pr_info("tx=%llu BEGIN tgid=%u flags=0x%x\n",
		(unsigned long long)ctx->tx_id, tgid, ctx->flags);
	tx_ctx_put(ctx);
	return 0;

err_unlink:
	tx_ctx_unlink(ctx);
	tx_ctx_put(ctx);
	return ret;
}

/*
 * COMMIT and ABORT share almost everything: resolve the transaction,
 * check who is allowed to ask, run the two-phase sequence, report the
 * counters.  The differences are the authority check and which sequence
 * runs, so they are parameters rather than two near-identical functions.
 */
static long tx_ioctl_end(void __user *uarg, bool commit)
{
	struct tx_end_arg arg;
	struct tx_ctx *ctx;
	u32 tgid = (u32)task_tgid_vnr(current);
	u64 n_effects = 0, n_files = 0;
	int ret;

	if (copy_from_user(&arg, uarg, sizeof(arg)))
		return -EFAULT;
	if (arg.abi != AGENTTX_ABI_VERSION)
		return -EPROTO;
	if (arg.reason >= TX_REASON_MAX)
		return -EINVAL;

	/* TX_ID_NONE means "the transaction I am inside", which for a
	 * descendant of the opener is the inherited one. */
	ctx = arg.tx_id == TX_ID_NONE ? tx_ctx_get_inherited()
				      : tx_ctx_get_by_id(arg.tx_id);
	if (!ctx)
		return -ENOENT;

	if (commit) {
		spin_lock(&ctx->lock);
		ret = tx_check_commit_authority(ctx, tgid);
		spin_unlock(&ctx->lock);
		if (ret)
			goto out;
		ret = tx_do_commit(ctx, arg.reason, &n_effects, &n_files);
	} else {
		/*
		 * Abort is deliberately NOT authority-checked the same way.
		 * The agent may abort its own transaction: discarding your own
		 * speculative work is always safe, and refusing it would make
		 * a stuck agent unable to clean up after itself.  What the
		 * agent may not do is make its work real.
		 */
		if (!tx_ctx_covers_current(ctx) && ctx->supervisor_pid &&
		    tgid != ctx->supervisor_pid) {
			ret = -EPERM;
			goto out;
		}
		ret = tx_do_abort(ctx, arg.reason, &n_effects, &n_files);
	}

	arg.n_effects = n_effects;
	arg.n_files   = n_files;
	if (copy_to_user(uarg, &arg, sizeof(arg)))
		ret = ret ? ret : -EFAULT;

out:
	tx_ctx_put(ctx);
	return ret;
}

static long tx_ioctl_stat(void __user *uarg)
{
	struct tx_stat_arg arg;
	struct tx_ctx *ctx;

	if (copy_from_user(&arg, uarg, sizeof(arg)))
		return -EFAULT;
	if (arg.abi != AGENTTX_ABI_VERSION)
		return -EPROTO;

	ctx = arg.tx_id == TX_ID_NONE ? tx_ctx_get_inherited()
				      : tx_ctx_get_by_id(arg.tx_id);
	if (!ctx) {
		/*
		 * Two different questions, two different answers.
		 *
		 * TX_ID_NONE asks "am I in a transaction?", and "no" is a
		 * legitimate answer the harness polls for constantly --
		 * returning -ENOENT there would put an error in the log on
		 * every poll.
		 *
		 * An EXPLICIT id asks "what is the state of transaction 5?",
		 * and if 5 does not exist that is -ENOENT. Answering it with a
		 * zeroed struct and success was a real bug: every dead or
		 * invented transaction reported itself as a healthy `none`,
		 * so a caller could not tell "finished" from "never existed"
		 * from "I made this number up". tests/p1/t03 caught it.
		 */
		if (arg.tx_id != TX_ID_NONE)
			return -ENOENT;

		memset(&arg, 0, sizeof(arg));
		arg.abi = AGENTTX_ABI_VERSION;
		arg.state = TX_STATE_NONE;
		arg.deadline_ns = -1;
		return copy_to_user(uarg, &arg, sizeof(arg)) ? -EFAULT : 0;
	}

	spin_lock(&ctx->lock);
	arg.tx_id	= ctx->tx_id;
	arg.state	= ctx->state;
	arg.worst_class	= ctx->worst_class;
	arg.n_deferred	= ctx->n_deferred;
	arg.n_written	= ctx->n_written;
	arg.deadline_ns	= ctx->deadline_ns;
	arg.owner_pid	= ctx->owner_pid;
	arg._pad	= 0;
	arg._pad2	= 0;
	spin_unlock(&ctx->lock);

	tx_ctx_put(ctx);
	return copy_to_user(uarg, &arg, sizeof(arg)) ? -EFAULT : 0;
}

static long tx_ioctl_supervisor(void __user *uarg)
{
	struct tx_supervisor_arg arg;
	u32 tgid = (u32)task_tgid_vnr(current);

	if (copy_from_user(&arg, uarg, sizeof(arg)))
		return -EFAULT;
	if (arg.abi != AGENTTX_ABI_VERSION)
		return -EPROTO;

	/*
	 * Registering as supervisor is a privileged act.  Without this check
	 * a compromised agent could register *itself* the moment before it
	 * wanted to commit, and the whole authority model would be theatre.
	 */
	if (!capable(CAP_SYS_ADMIN))
		return -EPERM;

	tx_registered_supervisor = tgid;
	pr_info("supervisor registered: tgid=%u (agent_pid hint=%u)\n",
		tgid, arg.agent_pid);
	return 0;
}

/*
 * TX_IOC_WAIT registers an edge and runs detection before returning, so the
 * caller learns immediately whether it just closed a cycle. That is the
 * point: a detector on a timer leaves a deadlock undiscovered for up to one
 * period, and the claim is that the kernel notices when the cycle forms.
 */
static long tx_ioctl_wait(void __user *uarg, bool add)
{
	struct tx_wait_edge arg;
	struct tx_ctx *self = NULL;
	tx_id_t waiter;
	int ret;

	if (copy_from_user(&arg, uarg, sizeof(arg)))
		return -EFAULT;
	if (arg.abi != AGENTTX_ABI_VERSION)
		return -EPROTO;
	if (arg.kind >= TX_WAIT_MAX)
		return -EINVAL;

	waiter = arg.waiter;
	if (waiter == TX_ID_NONE) {
		/* "the transaction I am inside", resolved by inheritance. */
		self = tx_ctx_get_inherited();
		if (!self)
			return -ENOENT;
		waiter = self->tx_id;
	}

	if (!add) {
		ret = tx_wait_del(waiter, arg.holder);
		tx_ctx_put(self);
		return ret;
	}

	arg.waiter = waiter;
	arg.since_ns = 0;
	arg.cycle_len = 0;
	arg.victim_tx = TX_ID_NONE;
	arg.victim_pid = 0;
	arg.unresolvable = 0;
	arg._pad = 0;

	ret = tx_wait_add(waiter, arg.holder, (enum tx_wait_kind)arg.kind);
	tx_ctx_put(self);

	if (ret == -EDEADLK) {
		arg.unresolvable = 1;
		arg.cycle_len = 1;		/* a cycle exists; length is in dmesg */
	} else if (ret == 1) {
		arg.cycle_len = 1;		/* detected and broken */
	} else if (ret < 0) {
		return ret;
	}

	if (copy_to_user(uarg, &arg, sizeof(arg)))
		return -EFAULT;

	/*
	 * -EDEADLK reaches userspace deliberately. A caller about to sleep on
	 * this wait must be told that sleeping would hang, and it is the only
	 * answer that is both true and actionable.
	 */
	return ret == -EDEADLK ? -EDEADLK : 0;
}

long tx_ioctl(struct file *filp, unsigned int cmd, unsigned long arg)
{
	void __user *uarg = (void __user *)arg;

	if (_IOC_TYPE(cmd) != AGENTTX_IOC_MAGIC)
		return -ENOTTY;

	switch (cmd) {
	case TX_IOC_BEGIN:	return tx_ioctl_begin(uarg);
	case TX_IOC_COMMIT:	return tx_ioctl_end(uarg, true);
	case TX_IOC_ABORT:	return tx_ioctl_end(uarg, false);
	case TX_IOC_STAT:	return tx_ioctl_stat(uarg);
	case TX_IOC_SUPERVISOR:	return tx_ioctl_supervisor(uarg);
	case TX_IOC_WAIT:	return tx_ioctl_wait(uarg, true);
	case TX_IOC_UNWAIT:	return tx_ioctl_wait(uarg, false);
	case TX_IOC_ABI: {
		u32 v = AGENTTX_ABI_VERSION;

		return copy_to_user(uarg, &v, sizeof(v)) ? -EFAULT : 0;
	}
	default:
		return -ENOTTY;
	}
}
