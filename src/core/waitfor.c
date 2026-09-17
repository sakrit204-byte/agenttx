// SPDX-License-Identifier: GPL-2.0
/*
 * src/core/waitfor.c --- the wait-for graph.  Fragments P1-15 .. P1-18.
 *
 * WHY THE KERNEL IS THE ONLY PLACE THIS CAN LIVE.
 *
 * The waits cross process boundaries and no single agent knows about the
 * others. A userspace coordinator could only see the waits it was told
 * about, and the interesting deadlocks are exactly the ones nobody meant to
 * create.
 *
 * WHAT DOES AND DOES NOT DEADLOCK HERE.
 *
 * Data conflicts do not. We use optimistic concurrency control: transactions
 * never wait for data, they proceed on private overlays and one aborts at
 * commit. That makes the whole data path deadlock-free by construction and
 * costs starvation instead (docs/deadlock.md section 2.2).
 *
 * What CAN deadlock is a transaction blocked on something only another
 * transaction can give it, and the mechanism creates precisely such a wait:
 * P3-10 escalates an irrevocable effect to a human approval, and in a
 * multi-agent pipeline the approver may itself be an agent inside a
 * transaction.
 *
 * THE RESULT THIS IMPLEMENTS.
 *
 * Abort is the preemption primitive -- copy-on-write makes rollback cheap,
 * so breaking a cycle is nearly free. EXCEPT that a transaction which
 * emitted an irrevocable effect is DOOMED, and DOOMED -> ABORTING does not
 * exist in the state machine (include/agenttx.h). So a DOOMED transaction is
 * a NON-PREEMPTABLE resource holder, and a cycle whose members are all
 * doomed cannot be broken by the mechanism that breaks every other cycle.
 *
 * That is not a bug to fix. It is what irrevocability means, and the kernel
 * reports it rather than hanging -- a system that deadlocks silently is
 * worse than one that says it has deadlocked.
 *
 * Owner: P1.
 */

#define pr_fmt(fmt) "agenttx: " fmt

#include <linux/ktime.h>
#include <linux/list.h>
#include <linux/module.h>
#include <linux/printk.h>
#include <linux/slab.h>
#include <linux/spinlock.h>
#include <linux/workqueue.h>

#include "core.h"

struct tx_edge {
	struct list_head	node;
	tx_id_t			waiter;
	tx_id_t			holder;
	u8			kind;
	u64			since_ns;
};

static LIST_HEAD(tx_edges);
static DEFINE_SPINLOCK(tx_edges_lock);
static unsigned int tx_n_edges;

static const char * const wait_names[] = TX_WAIT_NAMES;

static const char *wait_name(u8 k)
{
	return k < TX_WAIT_MAX ? wait_names[k] : "?";
}

/* ------------------------------------------------------------------ */
/* Deferred abort.                                                     */
/* ------------------------------------------------------------------ */
/*
 * Victim selection happens with tx_edges_lock held; the abort must not.
 * tx_do_abort() calls into P2 and P3 and both sleep, and sleeping under a
 * spinlock wedges the CPU. Same shape as the process-death path in exit.c,
 * and for the same reason.
 *
 * The work carries a tx_id, never a pointer: between queueing and running,
 * the transaction may have finished on its own.
 */
static struct workqueue_struct *tx_wf_wq;

struct tx_victim_work {
	struct work_struct	work;
	tx_id_t			tx_id;
};

static void tx_victim_worker(struct work_struct *w)
{
	struct tx_victim_work *vw = container_of(w, struct tx_victim_work, work);
	struct tx_ctx *ctx = tx_ctx_get_by_id(vw->tx_id);
	u64 n_eff = 0, n_files = 0;

	if (ctx) {
		pr_warn("tx=%llu: aborting as a deadlock victim\n",
			(unsigned long long)vw->tx_id);
		tx_do_abort(ctx, TX_REASON_DEADLOCK, &n_eff, &n_files);
		tx_ctx_put(ctx);
	}
	tx_wait_drop_all(vw->tx_id);
	kfree(vw);
}

/* ------------------------------------------------------------------ */
/* Detection.                                                          */
/* ------------------------------------------------------------------ */

struct tx_snap {
	tx_id_t	waiter[TX_WAITFOR_MAX_EDGES];
	tx_id_t	holder[TX_WAITFOR_MAX_EDGES];
	u8	kind[TX_WAITFOR_MAX_EDGES];
	unsigned int n;

	tx_id_t	node[TX_WAITFOR_MAX_NODES];
	unsigned int n_nodes;
};

static int snap_node(struct tx_snap *s, tx_id_t id)
{
	unsigned int i;

	for (i = 0; i < s->n_nodes; i++)
		if (s->node[i] == id)
			return (int)i;
	if (s->n_nodes >= TX_WAITFOR_MAX_NODES)
		return -1;
	s->node[s->n_nodes] = id;
	return (int)s->n_nodes++;
}

/* Caller holds tx_edges_lock. */
static void snapshot(struct tx_snap *s)
{
	struct tx_edge *e;

	s->n = 0;
	s->n_nodes = 0;
	list_for_each_entry(e, &tx_edges, node) {
		if (s->n >= TX_WAITFOR_MAX_EDGES)
			break;
		s->waiter[s->n] = e->waiter;
		s->holder[s->n] = e->holder;
		s->kind[s->n] = e->kind;
		s->n++;
		snap_node(s, e->waiter);
		snap_node(s, e->holder);
	}
}

/*
 * Does a path exist from @from back to @to?  If so, that path plus the edge
 * (@to -> @from) is the cycle the caller just closed.
 *
 * WHY THIS IS EDGE-SPECIFIC AND NOT "FIND ANY CYCLE".
 *
 * The first version searched the whole graph for any cycle. That is wrong in
 * a way that only shows up once the graph has been used: an UNRESOLVABLE
 * cycle -- every member DOOMED, so nothing can be aborted -- stays in the
 * graph by definition, and every later insert then rediscovered it and
 * reported -EDEADLK to a caller whose own edge was perfectly fine. One stuck
 * cycle poisoned every subsequent wait in the system.
 *
 * Searching from the new edge answers the question the caller actually
 * asked: "does MY wait deadlock?"  Pre-existing cycles were already reported
 * when they formed.
 *
 * Iterative, not recursive: the kernel stack is 16 KiB and the graph's depth
 * is bounded only by how many transactions a caller chooses to chain, so a
 * recursive DFS is a stack overflow reachable from userspace.
 */
static unsigned int path_back(struct tx_snap *s, tx_id_t from, tx_id_t to,
			      tx_id_t *cycle, unsigned int cap)
{
	u8 seen[TX_WAITFOR_MAX_NODES];
	int stack[TX_WAITFOR_MAX_NODES];
	int iter[TX_WAITFOR_MAX_NODES];
	int start, sp;
	unsigned int i, len;

	memset(seen, 0, sizeof(seen));

	start = snap_node(s, from);
	if (start < 0)
		return 0;

	sp = 0;
	stack[0] = start;
	iter[0] = 0;
	seen[start] = 1;

	while (sp >= 0) {
		int u = stack[sp];
		int pushed = 0;

		for (i = (unsigned int)iter[sp]; i < s->n; i++) {
			int v;

			if (s->waiter[i] != s->node[u])
				continue;
			iter[sp] = (int)i + 1;

			if (s->holder[i] == to) {
				/* Reached the waiter: stack[0..sp] is the path. */
				len = 0;
				if (len < cap)
					cycle[len++] = to;
				for (i = 0; i <= (unsigned int)sp && len < cap; i++)
					cycle[len++] = s->node[stack[i]];
				return len;
			}

			v = snap_node(s, s->holder[i]);
			if (v < 0 || seen[v])
				continue;
			if (sp + 1 >= (int)TX_WAITFOR_MAX_NODES)
				continue;
			seen[v] = 1;
			sp++;
			stack[sp] = v;
			iter[sp] = 0;
			pushed = 1;
			break;
		}
		if (!pushed && i >= s->n)
			sp--;
	}
	return 0;
}

/* ------------------------------------------------------------------ */
/* Recovery.                                                           */
/* ------------------------------------------------------------------ */

/*
 * Pick the transaction to abort.
 *
 * Policy is `least-severe` (docs/deadlock.md section 4): the lowest
 * worst-class first, ties by fewest writes, then lowest id. It is the only
 * policy that knows what the taxonomy is FOR -- a transaction whose worst
 * class is `reversible` costs nothing to abort, one whose worst class is
 * `compensable` costs a compensation.
 *
 * A DOOMED transaction is never a candidate. Not because the policy prefers
 * not to pick it, but because DOOMED -> ABORTING does not exist: asking for
 * it would be asking the state machine for a transition it does not have.
 */
static tx_id_t choose_victim(const tx_id_t *cycle, unsigned int len,
			     unsigned int *n_abortable, unsigned int *n_doomed)
{
	tx_id_t best = TX_ID_NONE;
	unsigned int best_class = TX_CLASS_MAX;
	u64 best_cost = U64_MAX;
	unsigned int i;

	*n_abortable = 0;
	*n_doomed = 0;

	for (i = 0; i < len; i++) {
		struct tx_ctx *ctx = tx_ctx_get_by_id(cycle[i]);
		enum tx_state st;
		enum tx_class wc;
		u64 cost;

		if (!ctx)
			continue;
		spin_lock(&ctx->lock);
		st = ctx->state;
		wc = ctx->worst_class;
		cost = ctx->n_written + ctx->n_deferred;
		spin_unlock(&ctx->lock);

		if (st == TX_STATE_DOOMED)
			(*n_doomed)++;

		if (st == TX_STATE_ACTIVE) {
			(*n_abortable)++;
			if ((unsigned int)wc < best_class ||
			    ((unsigned int)wc == best_class && cost < best_cost)) {
				best_class = wc;
				best_cost = cost;
				best = cycle[i];
			}
		}
		tx_ctx_put(ctx);
	}
	return best;
}

static void report_cycle(const tx_id_t *cycle, unsigned int len,
			 struct tx_snap *s)
{
	char kinds[64] = "";
	unsigned int i, j;

	for (i = 0; i < len; i++)
		for (j = 0; j < s->n; j++)
			if (s->waiter[j] == cycle[i]) {
				unsigned int k;
				bool seen = false;

				for (k = 0; k < len; k++)
					if (s->holder[j] == cycle[k])
						seen = true;
				if (seen && !strstr(kinds, wait_name(s->kind[j]))) {
					strlcat(kinds, wait_name(s->kind[j]),
						sizeof(kinds));
					strlcat(kinds, " ", sizeof(kinds));
				}
			}

	pr_err("DEADLOCK: %u transactions in a cycle [%s]\n", len, kinds);
	for (i = 0; i < len; i++)
		pr_err("  tx=%llu -> tx=%llu\n",
		       (unsigned long long)cycle[i],
		       (unsigned long long)cycle[(i + 1) % len]);
}

/* ------------------------------------------------------------------ */
/* The public interface.                                               */
/* ------------------------------------------------------------------ */

int tx_wait_add(tx_id_t waiter, tx_id_t holder, enum tx_wait_kind kind)
{
	struct tx_snap *s;
	struct tx_edge *e;
	tx_id_t cycle[TX_WAITFOR_MAX_NODES];
	tx_id_t victim;
	unsigned int len, n_abortable = 0, n_doomed = 0;
	int ret = 0;

	if (waiter == TX_ID_NONE || holder == TX_ID_NONE)
		return -EINVAL;
	if (waiter == holder)
		return -EINVAL;		/* a self-edge is always a bug */
	if ((unsigned int)kind >= TX_WAIT_MAX)
		return -EINVAL;

	/*
	 * Both ends must be live transactions.
	 *
	 * An edge naming a transaction that does not exist can never be
	 * satisfied and never be dropped by tx_wait_drop_all() -- nothing will
	 * ever end and clear it. The graph then accumulates permanent garbage,
	 * and a cycle through it is a phantom deadlock reported between parties
	 * that were never there.
	 */
	{
		struct tx_ctx *w = tx_ctx_get_by_id(waiter);
		struct tx_ctx *h = tx_ctx_get_by_id(holder);
		bool ok = w && h;

		tx_ctx_put(w);
		tx_ctx_put(h);
		if (!ok) {
			pr_debug("wait: tx=%llu -> tx=%llu names a transaction that does not exist\n",
				 (unsigned long long)waiter,
				 (unsigned long long)holder);
			return -ESRCH;
		}
	}

	e = kzalloc(sizeof(*e), GFP_KERNEL);
	if (!e)
		return -ENOMEM;
	s = kzalloc(sizeof(*s), GFP_KERNEL);	/* ~13 KiB: too big for the stack */
	if (!s) {
		kfree(e);
		return -ENOMEM;
	}

	e->waiter = waiter;
	e->holder = holder;
	e->kind = (u8)kind;
	e->since_ns = ktime_get_ns();

	spin_lock(&tx_edges_lock);
	if (tx_n_edges >= TX_WAITFOR_MAX_EDGES) {
		spin_unlock(&tx_edges_lock);
		kfree(e);
		kfree(s);
		pr_err("wait-for graph is full (%u edges); refusing to add\n",
		       TX_WAITFOR_MAX_EDGES);
		return -ENOSPC;
	}
	list_add_tail(&e->node, &tx_edges);
	tx_n_edges++;
	snapshot(s);
	spin_unlock(&tx_edges_lock);

	/*
	 * Detect on EVERY insert, not on a timer. A timer leaves a deadlock
	 * undiscovered for up to one period, and the whole claim is that the
	 * kernel notices at the moment the cycle closes. The graph is tens of
	 * nodes, so this is cheap.
	 */
	/* Only the cycle THIS edge closed. See path_back(). */
	len = path_back(s, holder, waiter, cycle, TX_WAITFOR_MAX_NODES);
	if (!len) {
		kfree(s);
		return 0;
	}

	report_cycle(cycle, len, s);
	victim = choose_victim(cycle, len, &n_abortable, &n_doomed);

	if (victim == TX_ID_NONE) {
		/*
		 * THE FINDING (P1-18). Every member emitted an irrevocable
		 * effect, so none can be rolled back, so the mechanism that
		 * breaks every other cycle cannot break this one.
		 */
		pr_err("DEADLOCK IS UNRESOLVABLE: all %u transactions are DOOMED\n",
		       len);
		pr_err("  Abort is the preemption primitive and a DOOMED transaction\n");
		pr_err("  cannot be aborted -- the effects are already emitted.\n");
		pr_err("  Remaining moves are outside this mechanism: deny an\n");
		pr_err("  escalation, or hand the cycle to a human.\n");
		ret = -EDEADLK;
	} else {
		struct tx_victim_work *vw = kzalloc(sizeof(*vw), GFP_KERNEL);

		pr_warn("  victim tx=%llu (policy=least-severe, %u of %u abortable, %u doomed)\n",
			(unsigned long long)victim, n_abortable, len, n_doomed);
		if (n_abortable == 1)
			pr_warn("  choice was FORCED: only one member could be aborted\n");

		if (vw) {
			INIT_WORK(&vw->work, tx_victim_worker);
			vw->tx_id = victim;
			queue_work(tx_wf_wq, &vw->work);
			ret = 1;
		} else {
			pr_err("  out of memory queueing the abort; cycle stands\n");
			ret = -ENOMEM;
		}
	}

	kfree(s);
	return ret;
}
EXPORT_SYMBOL_GPL(tx_wait_add);

int tx_wait_del(tx_id_t waiter, tx_id_t holder)
{
	struct tx_edge *e, *tmp;
	int n = 0;

	spin_lock(&tx_edges_lock);
	list_for_each_entry_safe(e, tmp, &tx_edges, node) {
		if (e->waiter == waiter && e->holder == holder) {
			list_del(&e->node);
			tx_n_edges--;
			kfree(e);
			n++;
		}
	}
	spin_unlock(&tx_edges_lock);
	return n ? 0 : -ENOENT;
}
EXPORT_SYMBOL_GPL(tx_wait_del);

/*
 * Every edge touching @tx_id goes, in both directions.
 *
 * Called when a transaction ends however it ended. Leaving an edge behind
 * would let a finished transaction hold a cycle open forever, and the
 * detector would keep reporting a deadlock between parties one of which no
 * longer exists.
 */
void tx_wait_drop_all(tx_id_t tx_id)
{
	struct tx_edge *e, *tmp;

	spin_lock(&tx_edges_lock);
	list_for_each_entry_safe(e, tmp, &tx_edges, node) {
		if (e->waiter == tx_id || e->holder == tx_id) {
			list_del(&e->node);
			tx_n_edges--;
			kfree(e);
		}
	}
	spin_unlock(&tx_edges_lock);
}
EXPORT_SYMBOL_GPL(tx_wait_drop_all);

unsigned int tx_wait_count(void)
{
	unsigned int n;

	spin_lock(&tx_edges_lock);
	n = tx_n_edges;
	spin_unlock(&tx_edges_lock);
	return n;
}

int tx_waitfor_init(void)
{
	tx_wf_wq = alloc_ordered_workqueue("agenttx_wf", WQ_MEM_RECLAIM);
	return tx_wf_wq ? 0 : -ENOMEM;
}

void tx_waitfor_exit(void)
{
	struct tx_edge *e, *tmp;

	if (tx_wf_wq) {
		flush_workqueue(tx_wf_wq);
		destroy_workqueue(tx_wf_wq);
		tx_wf_wq = NULL;
	}
	spin_lock(&tx_edges_lock);
	list_for_each_entry_safe(e, tmp, &tx_edges, node) {
		list_del(&e->node);
		kfree(e);
	}
	tx_n_edges = 0;
	spin_unlock(&tx_edges_lock);
}
