// SPDX-License-Identifier: GPL-2.0
/*
 * src/core/exit.c --- the agent died mid-transaction.  Fragment P1-08.
 *
 * Tracker: "Hardest correctness bug in the stream. kill -9 during
 * COMMITTING."  Three separate problems live here and it is worth naming
 * them before the code, because the code looks simple once they are.
 *
 * 1. HOW DO WE FIND OUT?
 *    A syscall probe on exit_group() misses the case that matters: a task
 *    killed by SIGKILL never makes that call.  We attach to the
 *    `sched_process_exit` tracepoint instead, which fires for every dying
 *    task however it died.  It is looked up by name via
 *    for_each_kernel_tracepoint() rather than referenced directly, so the
 *    module does not depend on the tracepoint symbol being exported.
 *
 * 2. WE CANNOT DO THE WORK THERE.
 *    A tracepoint probe runs with preemption disabled.  Cleanup means
 *    tx_do_abort(), which calls into P2 and P3, both of which sleep.
 *    Sleeping there is an instant lockup, and CONFIG_DEBUG_ATOMIC_SLEEP
 *    is in the debug config specifically to catch it.  So the probe does
 *    the smallest possible thing -- record a transaction id, queue work --
 *    and the abort happens later in process context.
 *
 * 3. THE CONTEXT MAY BE GONE BY THEN.
 *    Between queueing and running, the transaction may have been committed
 *    or aborted by the supervisor.  The work item therefore carries a
 *    tx_id, never a pointer, and re-resolves it.  If the lookup fails the
 *    transaction finished on its own and there is nothing to do.  Carrying
 *    a struct tx_ctx * here would be a use-after-free, and it is the
 *    obvious thing to write.
 *
 * WHAT WE DO NOT DO
 * -----------------
 * A transaction in COMMITTING is left alone.  Commit is driven by the
 * supervisor, in the supervisor's context, and the agent dying does not
 * interrupt it -- the whole point of "who may commit" is that the agent is
 * not the one finishing the transaction.  Aborting underneath an in-flight
 * commit would race two providers against each other.  DOOMED is likewise
 * left: it cannot be aborted by anyone, including us.
 *
 * Owner: P1.
 */

#define pr_fmt(fmt) "agenttx: " fmt

#include <linux/module.h>
#include <linux/printk.h>
#include <linux/sched.h>
#include <linux/slab.h>
#include <linux/tracepoint.h>
#include <linux/workqueue.h>

#include "core.h"

static struct tracepoint *tp_sched_process_exit;
static bool tp_registered;
static struct workqueue_struct *tx_exit_wq;

struct tx_exit_work {
	struct work_struct	work;
	tx_id_t			tx_id;
	u32			tgid;
};

static void tx_exit_worker(struct work_struct *w)
{
	struct tx_exit_work *ew = container_of(w, struct tx_exit_work, work);
	struct tx_ctx *ctx;
	u64 n_eff = 0, n_files = 0;
	enum tx_state st;

	/* Re-resolve by id.  See point 3 above. */
	ctx = tx_ctx_get_by_id(ew->tx_id);
	if (!ctx) {
		pr_debug("exit: tx=%llu already finished\n",
			 (unsigned long long)ew->tx_id);
		goto out;
	}

	spin_lock(&ctx->lock);
	st = ctx->state;
	spin_unlock(&ctx->lock);

	switch (st) {
	case TX_STATE_ACTIVE:
		pr_info("tx=%llu: owner tgid=%u died while ACTIVE -- aborting\n",
			(unsigned long long)ew->tx_id, ew->tgid);
		tx_do_abort(ctx, TX_REASON_PROC_DEATH, &n_eff, &n_files);
		break;

	case TX_STATE_COMMITTING:
		/*
		 * Deliberately nothing.  The supervisor is inside
		 * tx_do_commit() right now, in its own context, holding no
		 * reference we can invalidate.  Let it finish.
		 */
		pr_warn("tx=%llu: owner tgid=%u died during COMMITTING -- leaving the commit to the supervisor\n",
			(unsigned long long)ew->tx_id, ew->tgid);
		break;

	case TX_STATE_DOOMED:
		/*
		 * An irrevocable effect was already emitted, so there is
		 * nothing to undo and abort is not available.  Unlink it so
		 * it does not leak, and say loudly what happened: a doomed
		 * transaction whose owner died is a case a human has to look
		 * at, because the effects are real and the reasoning that
		 * produced them is gone.
		 */
		pr_err("tx=%llu: owner tgid=%u died while DOOMED (worst class %s) -- effects are already emitted and cannot be undone\n",
		       (unsigned long long)ew->tx_id, ew->tgid,
		       tx_class_name(ctx->worst_class));
		tx_ctx_unlink(ctx);
		break;

	default:
		/* ABORTING, or already terminal: somebody else has it. */
		pr_debug("exit: tx=%llu in %s, nothing to do\n",
			 (unsigned long long)ew->tx_id, tx_state_name(st));
		break;
	}

	tx_ctx_put(ctx);
out:
	kfree(ew);
}

/*
 * The tracepoint probe.  Runs with preemption disabled, for every task
 * that dies on the system.  It must therefore be cheap and must not
 * sleep: one hashtable lookup under RCU, and in the overwhelmingly common
 * case (the task was not transacting) it does nothing at all.
 *
 * Only the thread group leader triggers cleanup.  We key contexts by tgid,
 * so a worker thread exiting is not the transaction ending.
 */
static void tx_probe_sched_process_exit(void *data, struct task_struct *p)
{
	struct tx_exit_work *ew;
	struct tx_ctx *ctx;
	u32 tgid;

	if (!p || !thread_group_leader(p))
		return;

	tgid = (u32)task_tgid_nr(p);

	ctx = tx_ctx_get_by_tgid(tgid);
	if (!ctx)
		return;			/* the common case */

	/*
	 * GFP_ATOMIC: we are in atomic context.  If it fails we log and drop
	 * the cleanup -- the transaction then leaks until rmmod, which
	 * tx_ctx_exit() reports as a LEAK.  That is the correct failure:
	 * loud, bounded, and impossible to mistake for success.
	 */
	ew = kmalloc(sizeof(*ew), GFP_ATOMIC);
	if (!ew) {
		pr_err("tx=%llu: no memory to queue exit cleanup for tgid=%u\n",
		       (unsigned long long)ctx->tx_id, tgid);
		tx_ctx_put(ctx);
		return;
	}

	INIT_WORK(&ew->work, tx_exit_worker);
	ew->tx_id = ctx->tx_id;
	ew->tgid  = tgid;
	tx_ctx_put(ctx);

	queue_work(tx_exit_wq, &ew->work);
}

static void tx_tp_lookup(struct tracepoint *tp, void *priv)
{
	if (!tp_sched_process_exit && !strcmp(tp->name, "sched_process_exit"))
		tp_sched_process_exit = tp;
}

int tx_exit_hook_install(void)
{
	int ret;

	/*
	 * An ordered workqueue: cleanups for different transactions do not
	 * interact, but running them one at a time makes the dmesg from a
	 * test that kills ten agents readable, and this is not a throughput
	 * path.
	 */
	tx_exit_wq = alloc_ordered_workqueue("agenttx_exit", WQ_MEM_RECLAIM);
	if (!tx_exit_wq)
		return -ENOMEM;

	for_each_kernel_tracepoint(tx_tp_lookup, NULL);
	if (!tp_sched_process_exit) {
		pr_err("tracepoint sched_process_exit not found; process-death cleanup is DISABLED\n");
		ret = -ENOENT;
		goto err_wq;
	}

	ret = tracepoint_probe_register(tp_sched_process_exit,
					(void *)tx_probe_sched_process_exit,
					NULL);
	if (ret) {
		pr_err("tracepoint_probe_register failed: %d\n", ret);
		goto err_wq;
	}
	tp_registered = true;

	pr_info("process-death cleanup armed on sched_process_exit\n");
	return 0;

err_wq:
	destroy_workqueue(tx_exit_wq);
	tx_exit_wq = NULL;
	return ret;
}

void tx_exit_hook_remove(void)
{
	if (tp_registered) {
		tracepoint_probe_unregister(tp_sched_process_exit,
					    (void *)tx_probe_sched_process_exit,
					    NULL);
		tp_registered = false;
		/*
		 * Mandatory.  Probes are called under RCU; without this a
		 * probe already running can still be executing this module's
		 * code after unregister returns, and rmmod then frees the
		 * text underneath it.
		 */
		tracepoint_synchronize_unregister();
	}

	if (tx_exit_wq) {
		/* Drain before destroy: queued cleanups still hold refs. */
		flush_workqueue(tx_exit_wq);
		destroy_workqueue(tx_exit_wq);
		tx_exit_wq = NULL;
	}
}
