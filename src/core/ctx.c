// SPDX-License-Identifier: GPL-2.0
/*
 * src/core/ctx.c --- transaction context table.  Fragment P1-05.
 *
 * Keyed by tgid, deliberately NOT by a pointer hung off task_struct.  The
 * tracker says why: a task_struct field costs a kernel rebuild per
 * iteration, and the whole of phase 1 wants a fifteen-second edit-to-insmod
 * loop.  P1-12 promotes this to task_struct once the semantics are settled
 * and the rebuild is worth paying for.
 *
 * LOCKING, stated once and enforced everywhere below:
 *
 *   lookup      RCU.  tx_current_id() is called from BPF LSM hooks on every
 *               intercepted syscall, in contexts that may not sleep and
 *               must not contend.  A spinlock here would serialise every
 *               hook in the system on one cache line.
 *   mutation    tx_table_lock, a plain spinlock.  Create/unlink only.
 *   lifetime    refcount_t + kfree_rcu.  A hook that resolved us under
 *               rcu_read_lock() can still be dereferencing us after we have
 *               left the table, so the free waits for a grace period.
 *
 * Owner: P1.
 */

#define pr_fmt(fmt) "agenttx: " fmt

#include <linux/err.h>
#include <linux/hashtable.h>
#include <linux/ktime.h>
#include <linux/module.h>
#include <linux/printk.h>
#include <linux/sched.h>
#include <linux/slab.h>

#include "core.h"

static DEFINE_HASHTABLE(tx_table, TX_HASH_BITS);
static DEFINE_SPINLOCK(tx_table_lock);
static atomic_t tx_live_count = ATOMIC_INIT(0);

int tx_ctx_init(void)
{
	hash_init(tx_table);
	atomic_set(&tx_live_count, 0);
	return 0;
}

/*
 * Module teardown.  Anything still in the table at rmmod time is a leak on
 * somebody's error path -- a transaction whose owner is gone without the
 * exit hook having cleaned it up.  We free it so rmmod cannot corrupt
 * memory, and we shout, because silence here would hide exactly the bug
 * that WORKFLOW.md section 5 item 2 ("insmod/rmmod x10") exists to find.
 */
void tx_ctx_exit(void)
{
	struct tx_ctx *ctx;
	struct hlist_node *tmp;
	unsigned int bkt, leaked = 0;

	spin_lock(&tx_table_lock);
	hash_for_each_safe(tx_table, bkt, tmp, ctx, node) {
		hash_del_rcu(&ctx->node);
		leaked++;
		pr_err("LEAK at unload: tx=%llu tgid=%u state=%s\n",
		       (unsigned long long)ctx->tx_id, ctx->tgid,
		       tx_state_name(ctx->state));
		kfree_rcu(ctx, rcu);
	}
	spin_unlock(&tx_table_lock);

	if (leaked)
		pr_err("%u transaction(s) leaked -- the exit hook did not run for them\n",
		       leaked);

	/* Wait for any hook still inside rcu_read_lock() to leave. */
	synchronize_rcu();
	atomic_set(&tx_live_count, 0);
}

/*
 * Caller must hold tx_table_lock or rcu_read_lock().  Does NOT take a
 * reference -- the two public getters below do that, because taking it is
 * the part that is easy to forget and easy to get wrong.
 */
static struct tx_ctx *__lookup_tgid(u32 tgid)
{
	struct tx_ctx *ctx;

	hash_for_each_possible_rcu(tx_table, ctx, node, tgid) {
		if (ctx->tgid == tgid)
			return ctx;
	}
	return NULL;
}

/*
 * Transaction membership is INHERITED BY DESCENDANTS.
 *
 * This was not in the original design and it is not decoration.  The table
 * is keyed by tgid, so without inheritance a process that calls tx_begin
 * and then forks has a child which is NOT inside the transaction: the
 * child's syscalls miss every hook, and the sandbox has a hole exactly the
 * shape of "the agent ran a subprocess".  Since the whole model is
 * "supervisor wraps agent, agent does work", the agent is always a
 * descendant, and without this walk rung 1 cannot express the model at all.
 *
 * tests/p1/t02 and t03 found this by failing.  They were written assuming
 * a transaction outlives its opener and is visible to siblings; it does
 * not and it is not.  The tests were wrong about the lifetime and right
 * that something was missing.
 *
 * The walk is bounded.  An unbounded parent chain in a hook that runs on
 * every syscall is a denial of service: a process tree 10 000 deep would
 * make every syscall on the system walk 10 000 pointers.  TX_ANCESTOR_MAX
 * caps it, and a transacting process further up than that simply does not
 * cover this descendant -- fail-open on membership, which is the safe
 * direction here because a task we fail to find is treated as "not
 * transacting", and a non-transacting task is not granted anything.
 *
 * P1-12 replaces all of this with a pointer in task_struct copied at
 * copy_process(), which is O(1) and exact.  This is the version that does
 * not need a kernel rebuild per iteration.
 */
#define TX_ANCESTOR_MAX	16

/* Caller must hold rcu_read_lock(). */
static struct tx_ctx *__lookup_inherited(struct task_struct *start)
{
	struct task_struct *t = start;
	struct tx_ctx *ctx;
	unsigned int depth;

	for (depth = 0; t && depth < TX_ANCESTOR_MAX; depth++) {
		ctx = __lookup_tgid((u32)task_tgid_nr(t));
		if (ctx)
			return ctx;
		if (is_global_init(t))
			break;
		t = rcu_dereference(t->real_parent);
	}
	return NULL;
}


int tx_ctx_create(u32 tgid, u32 owner_pid, u32 flags, u32 timeout_ms,
		  struct tx_ctx **out)
{
	struct tx_ctx *ctx;

	if (flags & ~TX_F_ALL)
		return -EINVAL;

	ctx = kzalloc(sizeof(*ctx), GFP_KERNEL);
	if (!ctx)
		return -ENOMEM;

	ctx->tx_id	= (tx_id_t)atomic64_inc_return(&tx_next_id);
	ctx->tgid	= tgid;
	ctx->owner_pid	= owner_pid;
	ctx->flags	= flags;
	ctx->state	= TX_STATE_NONE;
	ctx->worst_class = TX_REVERSIBLE;
	ctx->deadline_ns = timeout_ms
			 ? (s64)ktime_get_ns() + (s64)timeout_ms * NSEC_PER_MSEC
			 : -1;
	spin_lock_init(&ctx->lock);
	refcount_set(&ctx->refcount, 1);		/* the table's reference */

	spin_lock(&tx_table_lock);
	/*
	 * We flatten nested transactions (P1-09), and flattening is the
	 * caller's decision to make.  Reporting -EEXIST rather than quietly
	 * returning the outer context means ioctl.c has to say in one place
	 * what a nested BEGIN means, and the answer is visible in a test.
	 */
	if (__lookup_tgid(tgid)) {
		spin_unlock(&tx_table_lock);
		kfree(ctx);
		return -EEXIST;
	}
	hash_add_rcu(tx_table, &ctx->node, tgid);
	atomic_inc(&tx_live_count);
	spin_unlock(&tx_table_lock);

	pr_debug("create tx=%llu tgid=%u flags=0x%x\n",
		 (unsigned long long)ctx->tx_id, tgid, flags);

	if (out) {
		refcount_inc(&ctx->refcount);		/* the caller's reference */
		*out = ctx;
	}
	return 0;
}

struct tx_ctx *tx_ctx_get_by_tgid(u32 tgid)
{
	struct tx_ctx *ctx;

	rcu_read_lock();
	ctx = __lookup_tgid(tgid);
	/*
	 * refcount_inc_not_zero, not refcount_inc: we may have found a
	 * context that is already on its way out, whose last reference was
	 * dropped between the lookup and here.  Treating that as "not found"
	 * is correct; resurrecting it is a use-after-free.
	 */
	if (ctx && !refcount_inc_not_zero(&ctx->refcount))
		ctx = NULL;
	rcu_read_unlock();
	return ctx;
}

/*
 * "My transaction" for a task that may be a descendant of the opener.
 * TX_ID_NONE in a STAT or ABORT argument means this, and it has to mean
 * this: an agent's subprocess asking "am I in a transaction" must get yes.
 */
struct tx_ctx *tx_ctx_get_inherited(void)
{
	struct tx_ctx *ctx;

	rcu_read_lock();
	ctx = __lookup_inherited(current);
	if (ctx && !refcount_inc_not_zero(&ctx->refcount))
		ctx = NULL;
	rcu_read_unlock();
	return ctx;
}

struct tx_ctx *tx_ctx_get_by_id(tx_id_t tx_id)
{
	struct tx_ctx *ctx, *found = NULL;
	unsigned int bkt;

	rcu_read_lock();
	hash_for_each_rcu(tx_table, bkt, ctx, node) {
		if (ctx->tx_id == tx_id) {
			if (refcount_inc_not_zero(&ctx->refcount))
				found = ctx;
			break;
		}
	}
	rcu_read_unlock();
	return found;
}

void tx_ctx_put(struct tx_ctx *ctx)
{
	if (!ctx)
		return;
	if (refcount_dec_and_test(&ctx->refcount)) {
		pr_debug("free tx=%llu\n", (unsigned long long)ctx->tx_id);
		kfree_rcu(ctx, rcu);
	}
}

void tx_ctx_unlink(struct tx_ctx *ctx)
{
	bool unlinked = false;

	if (!ctx)
		return;

	spin_lock(&tx_table_lock);
	/*
	 * hash_del_rcu() on an already-unhashed node corrupts the list, so
	 * the check is not defensive programming -- it is the difference
	 * between a double abort being harmless and being a panic.
	 */
	if (!hlist_unhashed(&ctx->node)) {
		hash_del_rcu(&ctx->node);
		atomic_dec(&tx_live_count);
		unlinked = true;
	}
	spin_unlock(&tx_table_lock);

	if (unlinked)
		tx_ctx_put(ctx);		/* drop the table's reference */
}

/*
 * Snapshot-and-visit rather than calling @fn under the lock: the exit hook
 * uses this to abort transactions, and abort calls into P2 and P3, both of
 * which sleep.  Calling a sleeping function under a spinlock is the single
 * most common way to wedge a kernel, and CONFIG_DEBUG_ATOMIC_SLEEP exists
 * to catch it -- but only if you run the debug kernel, so do not rely on
 * that alone.
 */
void tx_ctx_for_each(tx_ctx_visit_fn fn, void *arg)
{
	struct tx_ctx **snap;
	struct tx_ctx *ctx;
	unsigned int bkt, n = 0, cap;

	cap = (unsigned int)atomic_read(&tx_live_count) + 8;
	snap = kcalloc(cap, sizeof(*snap), GFP_KERNEL);
	if (!snap) {
		pr_err("for_each: out of memory, skipping sweep\n");
		return;
	}

	rcu_read_lock();
	hash_for_each_rcu(tx_table, bkt, ctx, node) {
		if (n >= cap)
			break;
		if (refcount_inc_not_zero(&ctx->refcount))
			snap[n++] = ctx;
	}
	rcu_read_unlock();

	while (n--) {
		fn(snap[n], arg);
		tx_ctx_put(snap[n]);
	}
	kfree(snap);
}

unsigned int tx_ctx_count(void)
{
	return (unsigned int)atomic_read(&tx_live_count);
}

/* ---------------------------------------------------------------- */
/* The identity half of the frozen contract.                         */
/* ---------------------------------------------------------------- */

/*
 * tx_current_id() is THE hot path of the whole system: P3's LSM hooks call
 * it on every intercepted syscall to decide whether they are inside a
 * transaction at all, and the answer is "no" almost every time.  RCU read
 * side only, no refcount traffic -- we read the id out while still inside
 * rcu_read_lock() rather than taking a reference we would immediately drop.
 *
 * It must be safe from non-sleepable context.  It is.
 */
tx_id_t tx_current_id(void)
{
	struct tx_ctx *ctx;
	tx_id_t id = TX_ID_NONE;

	rcu_read_lock();
	ctx = __lookup_inherited(current);
	if (ctx)
		id = ctx->tx_id;
	rcu_read_unlock();
	return id;
}
EXPORT_SYMBOL_GPL(tx_current_id);

enum tx_state tx_current_state(void)
{
	struct tx_ctx *ctx;
	enum tx_state st = TX_STATE_NONE;

	rcu_read_lock();
	ctx = __lookup_inherited(current);
	if (ctx)
		st = READ_ONCE(ctx->state);
	rcu_read_unlock();
	return st;
}
EXPORT_SYMBOL_GPL(tx_current_state);

/*
 * Is the calling task INSIDE @ctx -- as owner or as any descendant?
 *
 * This exists because inheritance opened a privilege escalation the moment
 * it was added.  tx_check_commit_authority() refuses a commit from the
 * transacting process by comparing tgids; with descendants inside the
 * transaction, an agent could simply fork, and the child's tgid differs
 * from the owner's, so the comparison would pass and the child would
 * commit.  That is the premature-commit attack with one extra line of
 * attacker code.
 *
 * So authority is asked as a membership question, not an equality one:
 * nobody inside the transaction may commit it, at any depth.
 */
bool tx_ctx_covers_current(const struct tx_ctx *ctx)
{
	struct task_struct *t = current;
	struct tx_ctx *found;
	unsigned int depth;
	bool inside = false;

	if (!ctx)
		return false;

	rcu_read_lock();
	for (depth = 0; t && depth < TX_ANCESTOR_MAX; depth++) {
		found = __lookup_tgid((u32)task_tgid_nr(t));
		if (found) {
			inside = (found->tx_id == ctx->tx_id);
			break;
		}
		if (is_global_init(t))
			break;
		t = rcu_dereference(t->real_parent);
	}
	rcu_read_unlock();
	return inside;
}
EXPORT_SYMBOL_GPL(tx_ctx_covers_current);

/*
 * The id of the transaction this task is inside, WITHOUT the ancestor walk.
 * Used by the exit hook: only the process that opened a transaction ends it
 * by dying.  A descendant exiting is just a subprocess finishing, and
 * treating that as the end of the transaction would abort the agent's work
 * every time it ran `ls`.
 */
tx_id_t tx_owned_id(void)
{
	struct tx_ctx *ctx;
	tx_id_t id = TX_ID_NONE;

	rcu_read_lock();
	ctx = __lookup_tgid((u32)task_tgid_nr(current));
	if (ctx)
		id = ctx->tx_id;
	rcu_read_unlock();
	return id;
}
EXPORT_SYMBOL_GPL(tx_owned_id);
