/* SPDX-License-Identifier: GPL-2.0 */
/*
 * include/agenttx.h --- the AgentTx contract.
 *
 * Fragment P1-01.  THIS FILE IS THE CONTRACT FREEZE (WORKFLOW.md, Rule 1).
 *
 * After the week-2 merge the dependency graph between P1..P4 is empty:
 * everyone depends on this header, nobody depends on anybody's .c file.
 * Changing anything here requires a PR labelled `contract-change` carrying
 * all four approvals.  Two or three such PRs over the semester is normal.
 * Ten means the design is wrong.
 *
 * The header is included from four very different places, so it is layered:
 *
 *   1. everywhere            types, enums, the four-class taxonomy
 *   2. userspace + kernel    the ioctl ABI and the WAL record
 *   3. __KERNEL__ only       the provider interfaces P1 calls
 *   4. BPF (__TX_BPF__)      no kernel headers; vmlinux.h supplies the types
 *
 * A BPF translation unit must define __TX_BPF__ before including this.
 */

#ifndef _AGENTTX_H
#define _AGENTTX_H

/* ------------------------------------------------------------------ */
/* Layer 0: type sourcing                                              */
/* ------------------------------------------------------------------ */

#if defined(__TX_BPF__)
  /* vmlinux.h has already supplied __u8 ... __s64.  Include nothing. */
#elif defined(__KERNEL__)
# include <linux/types.h>
# include <linux/ioctl.h>
#else
# include <linux/types.h>
# include <sys/ioctl.h>
#endif

/*
 * ABI version.  Bumped by any contract-change PR.  The supervisor refuses
 * to talk to a module whose version it does not recognise, and the BPF
 * loader refuses a weight blob whose version it does not recognise.  This
 * is what stops a half-migrated tree failing in a way nobody can read.
 */
#define AGENTTX_ABI_VERSION	1u

#define AGENTTX_DEV_NAME	"agenttx"
#define AGENTTX_DEV_PATH	"/dev/" AGENTTX_DEV_NAME

/* ------------------------------------------------------------------ */
/* Layer 1: the four-class taxonomy (PROPOSAL.md; paper section 3.3)    */
/* ------------------------------------------------------------------ */

/*
 * The ordering is deliberate and load-bearing: severity ascends, so a
 * transaction's "worst class seen so far" is a plain max().  Do not
 * reorder without reading every max() and >= in src/.
 */
enum tx_class {
	TX_REVERSIBLE	= 0,	/* proceed; the CoW layer captures it           */
	TX_DEFERRABLE	= 1,	/* record in the WAL, report success, no emit   */
	TX_COMPENSABLE	= 2,	/* emit; record cancel window + commit deadline */
	TX_IRREVOCABLE	= 3,	/* tx becomes non-abortable; escalate to human  */
	TX_CLASS_MAX	= 4
};

/*
 * Fail-closed threshold.  Confidence is 0..255.  A classifier result below
 * this is *not* the model's answer -- it is TX_IRREVOCABLE.  Every call site
 * that reads a class must apply this; there is a helper in layer 3 so that
 * the rule lives in exactly one place.
 */
#define TX_CONFIDENCE_MIN	178u	/* ~0.70 in 8-bit fixed point */

/* ------------------------------------------------------------------ */
/* Transaction lifecycle                                               */
/* ------------------------------------------------------------------ */

/*
 * Legal transitions, and only these (P1-06 asserts on the rest):
 *
 *   NONE       -> ACTIVE        tx_begin
 *   ACTIVE     -> COMMITTING    supervisor tx_commit
 *   ACTIVE     -> ABORTING      tx_abort, verification failure, deadline
 *   ACTIVE     -> DOOMED        an irrevocable effect was emitted
 *   DOOMED     -> COMMITTING    the only exit from DOOMED; abort is gone
 *   COMMITTING -> DONE | FAILED
 *   ABORTING   -> ABORTED
 *
 * DOOMED is the STM irrevocability state: the transaction may still finish,
 * it may no longer be undone.
 */
enum tx_state {
	TX_STATE_NONE		= 0,
	TX_STATE_ACTIVE		= 1,
	TX_STATE_DOOMED		= 2,
	TX_STATE_COMMITTING	= 3,
	TX_STATE_ABORTING	= 4,
	TX_STATE_DONE		= 5,
	TX_STATE_ABORTED	= 6,
	TX_STATE_FAILED		= 7,
	TX_STATE_MAX		= 8
};

/* tx_begin flags */
#define TX_F_DEFER_NET		(1u << 0)  /* buffer fire-and-forget sendmsg    */
#define TX_F_COW_FS		(1u << 1)  /* mount the per-tx overlay          */
#define TX_F_ESCALATE		(1u << 2)  /* irrevocable -> human gate         */
#define TX_F_DRY_RUN		(1u << 3)  /* classify + log, never enforce     */
#define TX_F_TRACE		(1u << 4)  /* emit every decision to the WAL    */
#define TX_F_ALL		0x1fu

/* The measurement build (the week 3-4 gate) runs DRY_RUN|TRACE and nothing else. */
#define TX_F_MEASURE		(TX_F_DRY_RUN | TX_F_TRACE)

typedef __u64 tx_id_t;
#define TX_ID_NONE	((tx_id_t)0)

/* ------------------------------------------------------------------ */
/* Layer 2a: the ioctl ABI  (P1-04)                                    */
/* ------------------------------------------------------------------ */

/*
 * Rung 1..3 use ioctls on /dev/agenttx.  Rung 4 (P1-11) promotes BEGIN /
 * COMMIT / ABORT to real syscalls; the argument structs below are reused
 * verbatim as the syscall arguments, so the promotion is not an ABI change.
 */
#define AGENTTX_IOC_MAGIC	0xAE

struct tx_begin_arg {
	__u32	abi;		/* in:  AGENTTX_ABI_VERSION                   */
	__u32	flags;		/* in:  TX_F_*                                */
	__u32	timeout_ms;	/* in:  0 = none; else auto-abort deadline     */
	__u32	_pad;
	tx_id_t	tx_id;		/* out: the new transaction id                */
};

struct tx_end_arg {
	__u32	abi;		/* in:  AGENTTX_ABI_VERSION                   */
	__u32	_pad;
	tx_id_t	tx_id;		/* in:  which transaction                     */
	__u32	reason;		/* in:  enum tx_reason                        */
	__u32	_pad2;
	__u64	n_effects;	/* out: WAL records flushed or discarded      */
	__u64	n_files;	/* out: upper-layer inodes merged or dropped  */
};

enum tx_reason {
	TX_REASON_UNSPEC	= 0,
	TX_REASON_VERIFIED	= 1,	/* build/tests passed -- the normal commit */
	TX_REASON_HUMAN		= 2,	/* a person approved it                   */
	TX_REASON_POLICY	= 3,	/* a policy rule fired                    */
	TX_REASON_VERIFY_FAIL	= 4,	/* tests failed -- the normal abort       */
	TX_REASON_DEADLINE	= 5,	/* a compensable window expired           */
	TX_REASON_ESCALATE_DENY	= 6,	/* human gate said no, or timed out       */
	TX_REASON_PROC_DEATH	= 7	/* the agent died mid-transaction (P1-08) */
};

struct tx_stat_arg {
	__u32	abi;
	__u32	_pad;
	tx_id_t	tx_id;		/* in: TX_ID_NONE = "the caller's own"       */
	__u32	state;		/* out: enum tx_state                        */
	__u32	worst_class;	/* out: enum tx_class seen so far            */
	__u64	n_deferred;	/* out: effects sitting in the WAL           */
	__u64	n_written;	/* out: inodes in the upper layer            */
	__s64	deadline_ns;	/* out: commit deadline, -1 if none          */
	__u32	owner_pid;	/* out                                       */
	__u32	_pad2;
};

/*
 * Supervisor registration.  See "who may commit" in the proposal: the
 * transacting process may not commit itself.  A process claims the
 * supervisor role once, and thereafter it -- and only it -- may COMMIT.
 * TX_IOC_COMMIT from inside the transaction returns -EPERM.  That single
 * invariant is what kills the premature-commit attack.
 */
struct tx_supervisor_arg {
	__u32	abi;
	__u32	flags;
	__u32	agent_pid;	/* in: the process it supervises (0 = any child) */
	__u32	_pad;
};

#define TX_IOC_BEGIN		_IOWR(AGENTTX_IOC_MAGIC, 0x01, struct tx_begin_arg)
#define TX_IOC_COMMIT		_IOWR(AGENTTX_IOC_MAGIC, 0x02, struct tx_end_arg)
#define TX_IOC_ABORT		_IOWR(AGENTTX_IOC_MAGIC, 0x03, struct tx_end_arg)
#define TX_IOC_STAT		_IOWR(AGENTTX_IOC_MAGIC, 0x04, struct tx_stat_arg)
#define TX_IOC_SUPERVISOR	_IOW (AGENTTX_IOC_MAGIC, 0x05, struct tx_supervisor_arg)
#define TX_IOC_ABI		_IOR (AGENTTX_IOC_MAGIC, 0x06, __u32)

/* ------------------------------------------------------------------ */
/* Layer 2b: the effect WAL record (P3-07)                             */
/* ------------------------------------------------------------------ */

/*
 * Written by a BPF program into a bpf_ringbuf; read by the supervisor and
 * by P4's trace collector.  Fixed size, naturally aligned, no pointers --
 * it crosses the kernel/user boundary and it must be replayable in issue
 * order after the emitting process is gone.
 *
 * seq is the replay order: per-transaction and gap-free.  P3-09's flush
 * must reproduce it exactly; the test for that is an interleaved
 * multi-threaded write.
 */
enum tx_hook {
	TX_HOOK_NONE		= 0,
	TX_HOOK_FILE_OPEN	= 1,
	TX_HOOK_INODE_UNLINK	= 2,
	TX_HOOK_INODE_RENAME	= 3,
	TX_HOOK_SOCKET_CONNECT	= 4,
	TX_HOOK_SOCKET_SENDMSG	= 5,
	TX_HOOK_BPRM_CHECK	= 6,
	TX_HOOK_TLS_WRITE	= 7,	/* uprobe on the TLS library write path */
	TX_HOOK_MAX		= 8
};

enum tx_verdict {
	TX_V_ALLOW	= 0,	/* passed through untouched                      */
	TX_V_CAPTURED	= 1,	/* reversible; the CoW layer holds it            */
	TX_V_DEFERRED	= 2,	/* held in the WAL; userspace was told "success" */
	TX_V_EMITTED	= 3,	/* compensable; out the door, window recorded    */
	TX_V_ESCALATED	= 4,	/* irrevocable; parked on the human gate         */
	TX_V_DENIED	= 5,	/* -EPERM returned to the caller                 */
	TX_V_MAX	= 6
};

#define TX_WAL_PAYLOAD_MAX	256

struct tx_wal_rec {
	__u64	seq;		/* per-tx, gap-free, defines replay order    */
	tx_id_t	tx_id;
	__u64	ts_ns;		/* bpf_ktime_get_ns()                        */
	__u32	pid;
	__u32	tgid;
	__u8	hook;		/* enum tx_hook                              */
	__u8	klass;		/* enum tx_class                             */
	__u8	verdict;	/* enum tx_verdict                           */
	__u8	confidence;	/* 0..255, as returned by the classifier     */
	__u32	syscall_nr;
	__u64	path_hash;	/* fnv1a of the path prefix, 0 if not a path */
	__u32	daddr_v4;	/* network byte order, 0 if not a socket     */
	__u16	dport;		/* host byte order                           */
	__u16	family;		/* AF_INET / AF_INET6 / AF_UNIX              */
	__u32	payload_len;	/* bytes actually captured below             */
	__u32	payload_trunc;	/* bytes the caller wanted but we dropped    */
	__u8	payload[TX_WAL_PAYLOAD_MAX];
};

/*
 * WAL sizing.  P3-07 asks what happens at a million effects: the ring is
 * finite, so the honest answer is a policy, not a bigger buffer.  Overflow
 * is fail-closed -- the transaction is doomed, not silently truncated --
 * because a WAL with a hole in it can neither be replayed nor discarded.
 */
#define TX_WAL_RING_BYTES	(4u << 20)	/* 4 MiB per ring              */
#define TX_WAL_MAX_RECS		16384u		/* per transaction, then doom  */

/* ------------------------------------------------------------------ */
/* Layer 2c: the classifier feature vector  (P4-06)                    */
/* ------------------------------------------------------------------ */

/*
 * Every feature here must be computable inside a BPF hook with integer
 * arithmetic and no loops over strings.  If a proposed feature needs a
 * strcmp it does not go in this struct -- it goes in the userspace
 * labeller and gets distilled into a hash.
 *
 * This layout is shared verbatim by:
 *   tools/harness/features.py   (extraction, training)
 *   src/policy/infer.bpf.c      (the forward pass)
 *   src/bpf/rules.c             (the static-rule baseline)
 * If you change it, all three change in the same contract-change PR.
 */
#define TX_N_FEATURES	16

enum tx_feature_idx {
	TX_FEAT_SYSCALL_NR	= 0,	/* raw syscall number, clamped            */
	TX_FEAT_HOOK_ID		= 1,	/* enum tx_hook                           */
	TX_FEAT_PATH_HASH_B0	= 2,	/* low byte of the path-prefix hash       */
	TX_FEAT_PATH_HASH_B1	= 3,
	TX_FEAT_PATH_DEPTH	= 4,	/* number of '/' in the path              */
	TX_FEAT_PATH_IS_DOT	= 5,	/* a leading-dot component is present     */
	TX_FEAT_FD_TYPE		= 6,	/* S_IFMT >> 12                           */
	TX_FEAT_OPEN_FLAGS	= 7,	/* O_WRONLY|O_CREAT|O_TRUNC|O_APPEND      */
	TX_FEAT_DPORT_LO	= 8,	/* destination port, low byte             */
	TX_FEAT_DPORT_HI	= 9,	/* destination port, high byte            */
	TX_FEAT_AF		= 10,	/* address family                         */
	TX_FEAT_IS_LOOPBACK	= 11,	/* destination is 127/8 or ::1            */
	TX_FEAT_TX_DEPTH	= 12,	/* nesting depth (we flatten: 0 or 1)     */
	TX_FEAT_NGRAM_0		= 13,	/* hashed last-3-syscall n-gram, byte 0   */
	TX_FEAT_NGRAM_1		= 14,
	TX_FEAT_MSG_FLAGS	= 15	/* MSG_DONTWAIT etc -- the f-and-f signal */
};

/*
 * Feature values are u8 by construction, so quantisation is the identity
 * on the input side and the BPF forward pass never has to scale an input.
 * Extraction is responsible for the clamping.
 */
struct tx_features {
	__u8	f[TX_N_FEATURES];
};

/* ------------------------------------------------------------------ */
/* Layer 2d: the model blob  (P4-09 exports it, P3-11 loads it)        */
/* ------------------------------------------------------------------ */

#define TX_MODEL_MAGIC		0x54584d44u	/* "TXMD" */

enum tx_model_kind {
	TX_MODEL_TREE	= 1,	/* quantised decision tree -- verifier-friendly */
	TX_MODEL_MLP	= 2	/* int8 2-layer MLP -- the headline result      */
};

#define TX_MLP_HIDDEN		32u	/* fixed: the verifier wants a constant bound */
#define TX_TREE_MAX_NODES	512u
#define TX_TREE_MAX_DEPTH	16u	/* bpf_loop() trip count                      */

struct tx_model_hdr {
	__u32	magic;		/* TX_MODEL_MAGIC                        */
	__u16	abi;		/* AGENTTX_ABI_VERSION                   */
	__u8	kind;		/* enum tx_model_kind                    */
	__u8	n_features;	/* must equal TX_N_FEATURES              */
	__u8	n_classes;	/* must equal TX_CLASS_MAX               */
	__u8	_pad;
	__u16	n_nodes;	/* tree only                             */
	__s32	out_shift;	/* >> applied to logits before argmax    */
	__u32	trained_rows;	/* provenance: rows in the training set  */
	__u32	accuracy_pct;	/* provenance: held-out accuracy x100    */
};

/*
 * Decision-tree node.  A leaf has left == TX_TREE_LEAF; then thresh
 * carries the class and feat carries the confidence.  Packing them this
 * way keeps a node at 6 bytes, which keeps the whole tree inside one BPF
 * map value and inside L1.
 */
#define TX_TREE_LEAF		0xffffu

struct tx_tree_node {
	__u8	feat;		/* feature index, or confidence if a leaf */
	__u8	thresh;		/* split point, or class if a leaf        */
	__u16	left;		/* taken when f[feat] <= thresh           */
	__u16	right;		/* taken when f[feat] >  thresh           */
};

struct tx_model_tree {
	struct tx_model_hdr	hdr;
	struct tx_tree_node	node[TX_TREE_MAX_NODES];
};

struct tx_model_mlp {
	struct tx_model_hdr	hdr;
	__s8	w1[TX_MLP_HIDDEN][TX_N_FEATURES];
	__s32	b1[TX_MLP_HIDDEN];
	__s8	w2[TX_CLASS_MAX][TX_MLP_HIDDEN];
	__s32	b2[TX_CLASS_MAX];
	__s32	h_shift;	/* >> after layer 1, before the ReLU */
};

/* Pinned paths the loader and the exporter agree on. */
#define TX_PIN_DIR		"/sys/fs/bpf/agenttx"
#define TX_PIN_MODEL		TX_PIN_DIR "/model"
#define TX_PIN_WAL		TX_PIN_DIR "/wal"
#define TX_PIN_TXSTATE		TX_PIN_DIR "/txstate"
#define TX_PIN_REGISTRY		TX_PIN_DIR "/compensable"

/* ------------------------------------------------------------------ */
/* Layer 2e: the compensable registry  (paper section 3.4)             */
/* ------------------------------------------------------------------ */

/*
 * Shipped like a CA bundle, not learned.  The kernel consults it; it does
 * not infer it.  Key is (daddr, dport, path_hash); a zero field wildcards.
 */
struct tx_compensable_key {
	__u32	daddr_v4;
	__u16	dport;
	__u16	_pad;
	__u64	path_hash;
};

struct tx_compensable_val {
	__u32	window_ms;	/* how long the cancellation stays open */
	__u32	flags;
};

/* ------------------------------------------------------------------ */
/* Layer 3: kernel-internal provider interfaces                        */
/* ------------------------------------------------------------------ */
/*
 * These are the four seams of WORKFLOW.md Rule 2.  Each has a stub in
 * src/stub/ that logs and succeeds, so any one person can run the whole
 * system alone from week 3.  Build with CONFIG_AGENTTX_STUB=y to get the
 * stubs and =n to get the real implementations.  The signatures are
 * identical either way -- that is the entire point.
 */
#ifdef __KERNEL__

/* --- P2 provides: copy-on-write storage ------------------------------ */
int  tx_fs_begin(tx_id_t tx_id, const char *workdir);
int  tx_fs_commit(tx_id_t tx_id, __u64 *n_files);
int  tx_fs_abort(tx_id_t tx_id, __u64 *n_files);
int  tx_fs_writeset_count(tx_id_t tx_id, __u64 *n);
/* Returns 1 if the lower layer changed underneath us since begin (P2-08). */
int  tx_fs_extmod_check(tx_id_t tx_id);

/* --- P3 provides: effect interception and the WAL -------------------- */
int  tx_eff_begin(tx_id_t tx_id, __u32 flags);
int  tx_eff_flush(tx_id_t tx_id, __u64 *n_effects);	/* replay in seq order */
int  tx_eff_discard(tx_id_t tx_id, __u64 *n_effects);
int  tx_eff_count(tx_id_t tx_id, __u64 *n);

/* --- P4 provides: classification ------------------------------------- */
/*
 * Returns an enum tx_class and writes 0..255 into *confidence.  It must
 * never fail: an error path inside a classifier is an unclassified effect,
 * and an unclassified effect is TX_IRREVOCABLE.  Callers do not check a
 * return code, they apply tx_class_final() below.
 */
enum tx_class tx_classify(const struct tx_features *f, __u8 *confidence);

/* --- P1 provides: transaction identity -------------------------------- */
/*
 * tx_current_id() is exported as a BPF kfunc by P1-10 so the hooks can gate
 * on it.  Until that lands, P3 links the stub, which returns 1.  Returns
 * TX_ID_NONE if the current task is not inside a transaction.
 */
tx_id_t tx_current_id(void);
enum tx_state tx_current_state(void);

/* Raise the transaction's worst-class watermark; may move it to DOOMED. */
int  tx_note_class(tx_id_t tx_id, enum tx_class klass);

/*
 * The fail-closed rule, in exactly one place.  Every consumer of a
 * classifier result calls this and nothing else.  P3-06's static-rule
 * baseline and P4-10's model go through the same funnel, which is why
 * P3-11 is supposed to be a small diff.  If it is not, the contract was
 * wrong.
 */
static inline enum tx_class
tx_class_final(enum tx_class klass, __u8 confidence)
{
	if (confidence < TX_CONFIDENCE_MIN)
		return TX_IRREVOCABLE;
	if ((unsigned int)klass >= TX_CLASS_MAX)
		return TX_IRREVOCABLE;
	return klass;
}

#endif /* __KERNEL__ */

/* ------------------------------------------------------------------ */
/* Layer 4: shared string tables (debug output, harness, tests)        */
/* ------------------------------------------------------------------ */

#define TX_CLASS_NAMES   { "reversible", "deferrable", "compensable", "irrevocable" }
#define TX_STATE_NAMES   { "none", "active", "doomed", "committing", \
			   "aborting", "done", "aborted", "failed" }
#define TX_HOOK_NAMES    { "none", "file_open", "inode_unlink", "inode_rename", \
			   "socket_connect", "socket_sendmsg", "bprm_check", "tls_write" }
#define TX_VERDICT_NAMES { "allow", "captured", "deferred", "emitted", \
			   "escalated", "denied" }

#endif /* _AGENTTX_H */
