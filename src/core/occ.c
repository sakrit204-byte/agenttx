// SPDX-License-Identifier: GPL-2.0
/*
 * src/core/occ.c --- commit-time validation.  Fragment P2-12.
 *
 * THE PROBLEM THIS FIXES
 * ----------------------
 * Two agents, two transactions, one file. Each writes it in its own
 * copy-up layer and neither can see the other, which is isolation
 * working exactly as designed. Then both commit. The second merge
 * overwrites the first and nobody is told: the first agent's work is
 * gone, no error was returned at any point, and the only trace is that
 * the file contains one agent's line instead of two.
 *
 * Detecting this after the fact in user space, which is what we did
 * before, tells a human that it is ABOUT to happen. It cannot stop it,
 * because by the time the two write sets are compared the kernel has
 * already accepted both commits.
 *
 * WHAT WE DO
 * ----------
 * Optimistic concurrency control, the standard three phase shape:
 * transactions read and write freely (the execute phase), and the
 * kernel checks at commit whether anybody else committed the same
 * object in the meantime (validate), and only then merges (write).
 *
 * The registry below is the "in the meantime" part. Every committed
 * path is recorded against a monotonic sequence number. A transaction
 * records the sequence it began at. At commit, for each path in its
 * write set, we ask: has this path been committed by somebody else
 * since I began? If yes, this commit would be computed from a stale
 * read, and we refuse it.
 *
 * WHY A DIGEST AND NOT THE PATH
 * -----------------------------
 * We store a 64 bit hash of the relative path, never the string. A
 * transaction that writes a million files would otherwise let an
 * unprivileged agent pin an unbounded amount of kernel memory. The
 * cost is a false conflict on a hash collision, which refuses a commit
 * that would have been fine. We take that trade deliberately: the
 * failure direction is a transaction that has to be retried rather
 * than a lost update, and at 64 bits it is not a failure we expect to
 * see.
 *
 * WHAT WE GIVE UP
 * ---------------
 * The registry is bounded (TX_OCC_MAX_ENTRIES). When it is full we
 * evict the oldest entries and raise a watermark. A transaction that
 * began before the watermark cannot be validated, because the evidence
 * it would need was discarded. We do NOT silently pass those: we say
 * so in the log and refuse, because the whole point of this fragment
 * is to stop lost updates and a validation we cannot perform is not a
 * validation that passed. An operator who prefers the old behaviour
 * can turn enforcement off with the occ_enforce module parameter.
 *
 * Owner: P2.
 */

#define pr_fmt(fmt) "agenttx: " fmt

#include <linux/hash.h>
#include <linux/jhash.h>
#include <linux/module.h>
#include <linux/moduleparam.h>
#include <linux/printk.h>
#include <linux/slab.h>
#include <linux/spinlock.h>

#include "core.h"

static bool occ_enforce = true;
module_param(occ_enforce, bool, 0644);
MODULE_PARM_DESC(occ_enforce,
		 "refuse a commit that would overwrite another transaction's committed write (default on)");

/*
 * Sized for the workload this is built for: a handful of agents editing
 * a project tree. 8192 paths is a large refactor and costs 8192 * 24
 * bytes, under 200 KiB, which is a reasonable ceiling for a module that
 * an unprivileged process can cause to allocate.
 */
#define TX_OCC_MAX_ENTRIES	8192
#define TX_OCC_BUCKETS		1024

struct occ_entry {
	struct hlist_node	node;
	struct list_head	age;		/* oldest first, for eviction */
	u64			digest;
	u64			seq;		/* commit that last wrote it  */
	tx_id_t			tx_id;		/* who, for the message       */
};

static struct hlist_head occ_tab[TX_OCC_BUCKETS];
static LIST_HEAD(occ_age);
static DEFINE_SPINLOCK(occ_lock);
static u64 occ_seq;			/* monotonic; 0 means "never"   */
static u64 occ_watermark;		/* below this we have forgotten */
static u32 occ_count;

/* Statistics, for the honest reporting of what this thing actually did. */
static u64 occ_n_validated;
static u64 occ_n_refused;

static inline u32 occ_bucket(u64 digest)
{
	return hash_64(digest, ilog2(TX_OCC_BUCKETS));
}

static u64 occ_digest(const char *rel)
{
	u32 a = jhash(rel, strlen(rel), 0x9e3779b9);
	u32 b = jhash(rel, strlen(rel), 0x85ebca6b);

	return ((u64)a << 32) | b;
}

u64 tx_occ_seq_now(void)
{
	u64 s;

	spin_lock(&occ_lock);
	s = occ_seq;
	spin_unlock(&occ_lock);
	return s;
}

/* --- validation ------------------------------------------------- */

struct occ_check {
	struct tx_ctx	*ctx;
	u64		begin_seq;
	u64		conflicts;
	u64		first_seq;
	tx_id_t		first_tx;
	char		first_path[128];
};

static int occ_check_one(const char *rel, void *arg)
{
	struct occ_check *c = arg;
	u64 digest = occ_digest(rel);
	struct occ_entry *e;

	spin_lock(&occ_lock);
	hlist_for_each_entry(e, &occ_tab[occ_bucket(digest)], node) {
		if (e->digest != digest)
			continue;
		/*
		 * Our own earlier commit is not a conflict with ourselves,
		 * and anything committed before we began is the state we
		 * read, which is exactly what we are allowed to build on.
		 */
		if (e->tx_id == c->ctx->tx_id || e->seq <= c->begin_seq)
			break;
		if (!c->conflicts) {
			c->first_seq = e->seq;
			c->first_tx  = e->tx_id;
			strscpy(c->first_path, rel, sizeof(c->first_path));
		}
		c->conflicts++;
		break;
	}
	spin_unlock(&occ_lock);
	return 0;			/* count them all; do not stop early */
}

int tx_occ_validate(struct tx_ctx *ctx)
{
	struct occ_check c = { .ctx = ctx, .begin_seq = ctx->begin_seq };
	bool forgotten;
	int ret;

	spin_lock(&occ_lock);
	occ_n_validated++;
	forgotten = (occ_watermark && ctx->begin_seq < occ_watermark);
	spin_unlock(&occ_lock);

	if (forgotten) {
		pr_warn("tx=%llu: cannot validate -- the commit registry has wrapped past this transaction's start (began at %llu, records begin at %llu)\n",
			(unsigned long long)ctx->tx_id,
			(unsigned long long)ctx->begin_seq,
			(unsigned long long)occ_watermark);
		if (!occ_enforce)
			return 0;
		spin_lock(&occ_lock);
		occ_n_refused++;
		spin_unlock(&occ_lock);
		return -EBUSY;
	}

	ret = tx_fs_writeset_each(ctx->tx_id, occ_check_one, &c);
	if (ret) {
		pr_warn("tx=%llu: could not read the write set (%d); not validating\n",
			(unsigned long long)ctx->tx_id, ret);
		return 0;
	}

	if (!c.conflicts)
		return 0;

	pr_warn("tx=%llu CONFLICT: %llu path(s) in this write set were committed by another transaction after this one began\n",
		(unsigned long long)ctx->tx_id,
		(unsigned long long)c.conflicts);
	pr_warn("tx=%llu   first: '%s' committed by tx=%llu at seq %llu; this transaction began at seq %llu\n",
		(unsigned long long)ctx->tx_id, c.first_path,
		(unsigned long long)c.first_tx,
		(unsigned long long)c.first_seq,
		(unsigned long long)ctx->begin_seq);

	if (!occ_enforce) {
		pr_warn("tx=%llu   occ_enforce=0: committing anyway, and the earlier write will be lost\n",
			(unsigned long long)ctx->tx_id);
		return 0;
	}

	spin_lock(&occ_lock);
	occ_n_refused++;
	spin_unlock(&occ_lock);
	return -EBUSY;
}

/* --- publish ------------------------------------------------------ */
/*
 * The write set has to be captured BEFORE the merge, not after.
 * tx_fs_commit() moves the upper layer down and leaves it empty, so a
 * walk performed afterwards finds nothing and records nothing -- which
 * is exactly the bug this fragment exists to prevent, reintroduced one
 * level down. We snapshot the digests first, merge, and publish the
 * snapshot only once the merge has actually succeeded.
 */

struct occ_set {
	u64	*d;
	u32	n, cap;
};

static int occ_snap_one(const char *rel, void *arg)
{
	struct occ_set *set = arg;

	if (set->n >= set->cap) {
		u32 cap = set->cap ? set->cap * 2 : 64;
		u64 *grown;

		if (cap > TX_OCC_MAX_ENTRIES)
			cap = TX_OCC_MAX_ENTRIES;
		if (set->n >= cap)
			return 0;	/* full; see tx_occ_snapshot() */
		grown = krealloc_array(set->d, cap, sizeof(*set->d),
				       GFP_KERNEL);
		if (!grown)
			return -ENOMEM;
		set->d = grown;
		set->cap = cap;
	}
	set->d[set->n++] = occ_digest(rel);
	return 0;
}

int tx_occ_snapshot(struct tx_ctx *ctx, struct occ_set **out)
{
	struct occ_set *set;
	int ret;

	*out = NULL;
	set = kzalloc(sizeof(*set), GFP_KERNEL);
	if (!set)
		return -ENOMEM;

	ret = tx_fs_writeset_each(ctx->tx_id, occ_snap_one, set);
	if (ret) {
		kfree(set->d);
		kfree(set);
		return ret;
	}

	if (set->n >= TX_OCC_MAX_ENTRIES)
		pr_warn("tx=%llu: write set exceeds %u paths; only the first %u are recorded and a later transaction may not see the rest\n",
			(unsigned long long)ctx->tx_id,
			TX_OCC_MAX_ENTRIES, set->n);

	*out = set;
	return 0;
}

void tx_occ_set_free(struct occ_set *set)
{
	if (!set)
		return;
	kfree(set->d);
	kfree(set);
}

static int occ_record(struct tx_ctx *ctx, u64 digest)
{
	struct occ_entry *e, *fresh;

	fresh = kzalloc(sizeof(*fresh), GFP_KERNEL);

	spin_lock(&occ_lock);
	hlist_for_each_entry(e, &occ_tab[occ_bucket(digest)], node) {
		if (e->digest != digest)
			continue;
		e->seq   = occ_seq;
		e->tx_id = ctx->tx_id;
		list_move_tail(&e->age, &occ_age);
		spin_unlock(&occ_lock);
		kfree(fresh);
		return 0;
	}


	if (!fresh) {
		spin_unlock(&occ_lock);
		return -ENOMEM;
	}

	while (occ_count >= TX_OCC_MAX_ENTRIES && !list_empty(&occ_age)) {
		struct occ_entry *old = list_first_entry(&occ_age,
							 struct occ_entry, age);

		/*
		 * Forgetting a record raises the watermark: from here on we
		 * can no longer prove anything about a transaction that
		 * began before this point, and tx_occ_validate() refuses
		 * rather than guessing.
		 */
		occ_watermark = max(occ_watermark, old->seq);
		hlist_del(&old->node);
		list_del(&old->age);
		kfree(old);
		occ_count--;
	}

	fresh->digest = digest;
	fresh->seq    = occ_seq;
	fresh->tx_id  = ctx->tx_id;
	hlist_add_head(&fresh->node, &occ_tab[occ_bucket(digest)]);
	list_add_tail(&fresh->age, &occ_age);
	occ_count++;
	spin_unlock(&occ_lock);
	return 0;
}

void tx_occ_publish_set(struct tx_ctx *ctx, struct occ_set *set)
{
	u32 i;

	if (!set)
		return;

	spin_lock(&occ_lock);
	occ_seq++;
	spin_unlock(&occ_lock);

	for (i = 0; i < set->n; i++)
		occ_record(ctx, set->d[i]);
}

/* --- lifecycle ----------------------------------------------------- */

void tx_occ_stats(u64 *validated, u64 *refused, u64 *tracked)
{
	spin_lock(&occ_lock);
	if (validated)
		*validated = occ_n_validated;
	if (refused)
		*refused = occ_n_refused;
	if (tracked)
		*tracked = occ_count;
	spin_unlock(&occ_lock);
}

bool tx_occ_enforcing(void)
{
	return occ_enforce;
}

int tx_occ_init(void)
{
	unsigned int i;

	for (i = 0; i < TX_OCC_BUCKETS; i++)
		INIT_HLIST_HEAD(&occ_tab[i]);
	pr_info("occ: commit validation %s\n",
		occ_enforce ? "enforcing" : "reporting only");
	return 0;
}

void tx_occ_exit(void)
{
	struct occ_entry *e, *t;

	spin_lock(&occ_lock);
	list_for_each_entry_safe(e, t, &occ_age, age) {
		hlist_del(&e->node);
		list_del(&e->age);
		kfree(e);
	}
	occ_count = 0;
	spin_unlock(&occ_lock);
}
