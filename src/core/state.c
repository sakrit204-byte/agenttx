// SPDX-License-Identifier: GPL-2.0
/*
 * src/core/state.c --- the transaction state machine.  Fragment P1-06.
 *
 * The tracker note for this fragment says "enumerate illegal transitions
 * and assert on them", and that is the entire design: there is exactly one
 * writer of ctx->state in the whole module, and it consults a table.
 *
 * The table below is the one drawn in include/agenttx.h.  If the two ever
 * disagree the header wins -- it is the frozen contract and three other
 * people compile against it -- so the check at the bottom of this file
 * exists to make the disagreement loud at module load rather than subtle
 * at 2am.
 *
 *   NONE       -> ACTIVE        tx_begin
 *   ACTIVE     -> COMMITTING    supervisor tx_commit
 *   ACTIVE     -> ABORTING      tx_abort, verification failure, deadline
 *   ACTIVE     -> DOOMED        an irrevocable effect was emitted
 *   DOOMED     -> COMMITTING    the only exit from DOOMED; abort is gone
 *   COMMITTING -> DONE | FAILED
 *   ABORTING   -> ABORTED
 *
 * The edge that carries the semantics is the one that is ABSENT:
 * DOOMED -> ABORTING. Once an irrevocable effect has been emitted the
 * transaction can no longer be undone, so abort must not merely fail --
 * it must be unrepresentable. That is the STM irrevocability rule, and it
 * is enforced here by the omission of one bit.
 *
 * Owner: P1.
 */

#define pr_fmt(fmt) "agenttx: " fmt

#include <linux/bug.h>
#include <linux/printk.h>

#include "core.h"

#define S(x)	(1u << (TX_STATE_##x))

/*
 * legal[from] is the set of states reachable from `from`.  A zero entry
 * means terminal.
 */
static const unsigned int legal[TX_STATE_MAX] = {
	[TX_STATE_NONE]		= S(ACTIVE),
	[TX_STATE_ACTIVE]	= S(COMMITTING) | S(ABORTING) | S(DOOMED),
	[TX_STATE_DOOMED]	= S(COMMITTING),	/* NOT ABORTING */
	[TX_STATE_COMMITTING]	= S(DONE) | S(FAILED),
	[TX_STATE_ABORTING]	= S(ABORTED),
	[TX_STATE_DONE]		= 0,
	[TX_STATE_ABORTED]	= 0,
	[TX_STATE_FAILED]	= 0,
};

static const char * const state_names[] = TX_STATE_NAMES;
static const char * const class_names[] = TX_CLASS_NAMES;

const char *tx_state_name(enum tx_state s)
{
	if ((unsigned int)s >= TX_STATE_MAX)
		return "?";
	return state_names[s];
}

const char *tx_class_name(enum tx_class c)
{
	if ((unsigned int)c >= TX_CLASS_MAX)
		return "?";
	return class_names[c];
}

bool tx_state_is_legal(enum tx_state from, enum tx_state to)
{
	if ((unsigned int)from >= TX_STATE_MAX ||
	    (unsigned int)to   >= TX_STATE_MAX)
		return false;
	return (legal[from] & (1u << to)) != 0;
}

bool tx_state_is_terminal(enum tx_state s)
{
	if ((unsigned int)s >= TX_STATE_MAX)
		return true;
	return legal[s] == 0;
}

/*
 * Caller must hold ctx->lock.
 *
 * An illegal transition is never bad input -- ioctl.c validates userspace
 * before it gets here -- so it is a bug in us.  WARN, refuse, and leave
 * the state alone: a state machine that half-applies an illegal edge is
 * worse than one that rejects it, because the next transition then looks
 * legal and the real cause is three steps back.
 */
int tx_state_set(struct tx_ctx *ctx, enum tx_state to)
{
	enum tx_state from;

	lockdep_assert_held(&ctx->lock);

	from = ctx->state;
	if (from == to)
		return 0;

	if (!tx_state_is_legal(from, to)) {
		WARN_ONCE(1, pr_fmt_tx "illegal transition tx=%llu %s -> %s\n",
			  (unsigned long long)ctx->tx_id,
			  tx_state_name(from), tx_state_name(to));
		return -EINVAL;
	}

	ctx->state = to;
	pr_debug("tx=%llu %s -> %s\n", (unsigned long long)ctx->tx_id,
		 tx_state_name(from), tx_state_name(to));
	return 0;
}

/*
 * tx_note_class() is part of the frozen contract: P3 calls it from the
 * effect path to raise the transaction's watermark.  The watermark is
 * monotonic -- max(), never assignment -- which is why enum tx_class is
 * ordered by ascending severity in the header, and why that ordering is
 * marked load-bearing there.
 *
 * TX_IRREVOCABLE is the one class with a side effect on the state machine:
 * it dooms the transaction. Note the order of operations. We raise the
 * watermark first and transition second, so a concurrent reader can never
 * observe DOOMED with a watermark that does not justify it.
 */
int tx_note_class(tx_id_t tx_id, enum tx_class klass)
{
	struct tx_ctx *ctx;
	int ret = 0;

	if ((unsigned int)klass >= TX_CLASS_MAX)
		return -EINVAL;

	ctx = tx_ctx_get_by_id(tx_id);
	if (!ctx)
		return -ENOENT;

	spin_lock(&ctx->lock);

	if (klass > ctx->worst_class)
		ctx->worst_class = klass;

	if (klass == TX_IRREVOCABLE && ctx->state == TX_STATE_ACTIVE) {
		ret = tx_state_set(ctx, TX_STATE_DOOMED);
		if (!ret)
			pr_info("tx=%llu DOOMED by an irrevocable effect; abort is no longer available\n",
				(unsigned long long)tx_id);
	}

	spin_unlock(&ctx->lock);
	tx_ctx_put(ctx);
	return ret;
}
EXPORT_SYMBOL_GPL(tx_note_class);

/*
 * Called once from module init.  Two things are checked, and both of them
 * are things a reviewer cannot see by reading:
 *
 *  1. every state has a name -- otherwise a panic message says "?"
 *  2. the table is self-consistent: no edge leaves the enum, and the only
 *     states with no outgoing edge are the three we call terminal.
 */
int tx_state_selfcheck(void)
{
	unsigned int i;
	int bad = 0;

	BUILD_BUG_ON(ARRAY_SIZE(state_names) != TX_STATE_MAX);
	BUILD_BUG_ON(ARRAY_SIZE(class_names) != TX_CLASS_MAX);

	for (i = 0; i < TX_STATE_MAX; i++) {
		if (legal[i] & ~((1u << TX_STATE_MAX) - 1)) {
			pr_err("selfcheck: state %u has an edge outside the enum\n", i);
			bad++;
		}
	}

	if (tx_state_is_legal(TX_STATE_DOOMED, TX_STATE_ABORTING)) {
		pr_err("selfcheck: DOOMED -> ABORTING is reachable; irrevocability is broken\n");
		bad++;
	}
	if (!tx_state_is_terminal(TX_STATE_DONE) ||
	    !tx_state_is_terminal(TX_STATE_ABORTED) ||
	    !tx_state_is_terminal(TX_STATE_FAILED)) {
		pr_err("selfcheck: a terminal state has an outgoing edge\n");
		bad++;
	}

	return bad ? -EINVAL : 0;
}
