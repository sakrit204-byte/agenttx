// SPDX-License-Identifier: GPL-2.0
/*
 * src/bpf/agenttx.bpf.c --- the LSM hooks.  Fragments P3-03, P3-04, P3-05.
 *
 * Every hook here follows the same three steps, in this order, and the
 * order is the whole performance story:
 *
 *   1. am I inside a transaction?   bpf_tx_current_id()
 *   2. if not, return immediately.  <- the overwhelmingly common case
 *   3. only then do any work.
 *
 * Step 2 is what makes this affordable. These hooks fire on every matching
 * syscall made by every process on the system, and almost none of them are
 * transacting. One kfunc call that walks a hash bucket under RCU and
 * returns 0 is the entire cost for everything else on the machine.
 *
 * THE VERIFIER CONSTRAINTS THAT SHAPE THIS FILE
 *   - no floats, no unbounded loops, no unchecked pointer arithmetic
 *   - every pointer read from kernel memory goes through bpf_probe_read or
 *     a CO-RE accessor; dereferencing directly is rejected
 *   - string handling is essentially unavailable, which is why the feature
 *     vector in include/agenttx.h is hashes and bytes rather than paths
 *
 * Owner: P3.
 */

#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_endian.h>

#include "agenttx.h"
#include "tx_features.h"
#include "infer.h"

/*
 * vmlinux.h carries every TYPE the kernel knows and none of its #defines --
 * BTF describes types, not preprocessor symbols. So the handful of
 * constants used below have to be restated. Keep this list short: each
 * entry is a value that could drift from the kernel's without anything
 * noticing.
 */
#ifndef AF_INET
#define AF_INET		2
#endif
#ifndef AF_INET6
#define AF_INET6	10
#endif
#ifndef IPPROTO_TCP
#define IPPROTO_TCP	6
#endif
#ifndef IPPROTO_UDP
#define IPPROTO_UDP	17
#endif
#ifndef MSG_DONTWAIT
#define MSG_DONTWAIT	0x40
#endif

char LICENSE[] SEC("license") = "GPL";

/* ------------------------------------------------------------------ */
/* P1's kfuncs (src/core/kfunc.c).                                     */
/* ------------------------------------------------------------------ */
/*
 * Resolved by the verifier through the module's BTF, not by the linker.
 * If CONFIG_DEBUG_INFO_BTF_MODULES=n, or agenttx.ko is not loaded, these
 * fail to resolve and the program is rejected with a message that names
 * the kfunc -- which is at least honest about what is missing.
 */
extern __u64 bpf_tx_current_id(void) __ksym;
extern __u32 bpf_tx_current_state(void) __ksym;
extern __s32 bpf_tx_note_class(__u64 tx_id, __u32 klass) __ksym;

/* ------------------------------------------------------------------ */
/* The WAL ring (P3-07) and counters.                                  */
/* ------------------------------------------------------------------ */

struct {
	__uint(type, BPF_MAP_TYPE_RINGBUF);
	__uint(max_entries, TX_WAL_RING_BYTES);
} tx_wal SEC(".maps");

/*
 * Per-transaction sequence numbers. The contract says seq is "per-tx and
 * gap-free", because P3-09's flush has to replay in issue order and a WAL
 * with a hole in it can neither be replayed nor discarded.
 */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 1024);
	__type(key, __u64);		/* tx_id */
	__type(value, __u64);		/* next seq */
} tx_seq SEC(".maps");

/* ------------------------------------------------------------------ */
/* P3-08: DEFERRAL.                                                    */
/* ------------------------------------------------------------------ */
/*
 * An LSM hook cannot defer on its own. security_socket_sendmsg() is
 * allow-or-deny and has no third answer: return 0 and the packet leaves,
 * return an errno and userspace sees the failure. Deferral needs the
 * syscall to report success AND the emission to be suppressed, which are
 * two different interception points.
 *
 * So the LSM hook records the FLOW it wants suppressed, returns 0, and the
 * agent's sendmsg() returns the byte count -- success, as far as it can
 * tell. A cgroup egress program then drops the packets belonging to that
 * flow. Nothing reaches the peer, and nothing told the agent so.
 *
 * WHY A 4-TUPLE AND NOT A SOCKET COOKIE. The obvious key is the socket
 * cookie, and it does not work: sk->sk_cookie is assigned lazily by
 * sock_gen_cookie(), so the LSM hook -- which runs FIRST -- usually reads 0
 * and has nothing to key on. The 4-tuple is visible from both sides
 * (sk->__sk_common in the hook, __sk_buff in the egress program) and needs
 * nothing to have happened first.
 */
struct tx_flow {
	__u32	saddr;
	__u32	daddr;
	__u16	sport;		/* host byte order, both sides */
	__u16	dport;		/* host byte order, both sides */
	__u8	proto;
	__u8	_pad[3];
};

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 4096);
	__type(key, struct tx_flow);
	__type(value, __u64);		/* tx_id holding it */
} tx_defer_flows SEC(".maps");

/*
 * Destination-only fallback.
 *
 * AN UNCONNECTED UDP SOCKET HAS NO SOURCE PORT WHEN THE LSM HOOK RUNS.
 * sendto() auto-binds during the send, i.e. AFTER security_socket_sendmsg
 * returns, so the hook reads skc_num == 0 while the egress program later
 * sees the real ephemeral port. The full 4-tuple can never match for
 * exactly the socket type that is most likely to be fire-and-forget --
 * which is the case this whole fragment exists for.
 *
 * So a send whose source port is not yet assigned is recorded by
 * (daddr, dport, proto) alone, and egress consults this map second.
 *
 * THE COST, stated plainly: this key cannot distinguish two processes
 * sending to the same destination. While a transaction defers to
 * 10.0.0.1:514, another process's packets to 10.0.0.1:514 are dropped too.
 * The fix is not a better key -- it is to attach the egress program to the
 * AGENT'S OWN CGROUP rather than the root cgroup, so it never sees anyone
 * else's traffic. txload takes --cgroup for that; the tests use the root
 * cgroup because the guest has no per-agent cgroup yet. Recorded in
 * docs/journal/p3.md as the follow-up.
 */
struct tx_dest {
	__u32	daddr;
	__u16	dport;
	__u8	proto;
	__u8	_pad;
};

struct {
	__uint(type, BPF_MAP_TYPE_LRU_HASH);
	__uint(max_entries, 1024);
	__type(key, struct tx_dest);
	__type(value, __u64);
} tx_defer_dests SEC(".maps");

/*
 * Per-destination override, written by the loader. Absent means "use the
 * built-in rule". This is P3-06's static rule table in its smallest useful
 * form: the interface the model replaces at P3-11.
 */
struct {
	__uint(type, BPF_MAP_TYPE_HASH);
	__uint(max_entries, 256);
	__type(key, __u32);		/* destination port */
	__type(value, __u8);		/* enum tx_class */
} tx_rules SEC(".maps");

/*
 * The model (P4-10). One array slot holding the whole blob; the loader
 * writes it from data/model/model_tree.bin.
 *
 * An EMPTY slot is not an error. The magic check inside tx_tree_classify()
 * fails, which returns TX_IRREVOCABLE, which would escalate every effect --
 * safe, but useless. So the hook checks whether a model is loaded and falls
 * back to the static rule table (P3-06) when it is not. That keeps "no model
 * yet" and "model says irrevocable" as different states rather than one.
 */
struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, 1);
	__type(key, __u32);
	__type(value, struct tx_model_tree);
} tx_model SEC(".maps");

/* Observability that survives a full ring: counters never drop. */
enum {
	TX_STAT_SEEN = 0,
	TX_STAT_IN_TX,
	TX_STAT_EMITTED,
	TX_STAT_DROPPED,	/* ring was full -- the fail-closed case */
	TX_STAT_DEFERRED,	/* sends held back rather than emitted        */
	TX_STAT_SUPPRESSED,	/* packets the egress program actually dropped */
	TX_STAT_EGRESS_SEEN,	/* diagnostic: did the egress program run at all */
	TX_STAT_EGRESS_INET,	/* diagnostic: ...and did it see an AF_INET skb */
	TX_STAT_MODEL,		/* classified by the in-kernel model            */
	TX_STAT_RULES,		/* classified by the static rule table          */
	TX_STAT_MAX,
};

struct {
	__uint(type, BPF_MAP_TYPE_ARRAY);
	__uint(max_entries, TX_STAT_MAX);
	__type(key, __u32);
	__type(value, __u64);
} tx_stats SEC(".maps");

static __always_inline void bump(__u32 which)
{
	__u64 *v = bpf_map_lookup_elem(&tx_stats, &which);

	if (v)
		__sync_fetch_and_add(v, 1);
}

static __always_inline __u64 next_seq(__u64 tx)
{
	__u64 init = 1, *v;

	v = bpf_map_lookup_elem(&tx_seq, &tx);
	if (!v) {
		bpf_map_update_elem(&tx_seq, &tx, &init, BPF_ANY);
		return 0;
	}
	return __sync_fetch_and_add(v, 1);
}

/*
 * Capture a bounded prefix of what is being sent.
 *
 * P3-09 replays deferred effects on commit, and it cannot replay bytes
 * nobody kept. The hook is the only place they are visible: after it
 * returns, the data is copied into the socket's send queue and the user
 * buffer may be reused or freed.
 *
 * `struct iov_iter` has two shapes that matter here. A plain send()/
 * sendto() with one buffer is ITER_UBUF and the pointer is in
 * __ubuf_iovec; a writev-style send is ITER_IOVEC and the pointer is in
 * the first iovec. Both are USER addresses, so bpf_probe_read_user --
 * probe_read_kernel on a user pointer reads garbage or fails, and the
 * failure is silent.
 *
 * Only the first segment of a multi-segment send is captured. A partial
 * capture is recorded as such (payload_trunc) rather than presented as
 * complete: replaying a prefix and calling it the message would be worse
 * than admitting we only have a prefix.
 */
static __always_inline __u32 capture_payload(struct msghdr *msg, __u8 *dst,
					     __u32 cap, __u32 *wanted)
{
	__u8 iter_type = 0;
	void *base = NULL;
	__u64 len = 0;
	__u32 n;

	iter_type = BPF_CORE_READ(msg, msg_iter.iter_type);

	if (iter_type == 0 /* ITER_UBUF */) {
		base = BPF_CORE_READ(msg, msg_iter.__ubuf_iovec.iov_base);
		len  = BPF_CORE_READ(msg, msg_iter.__ubuf_iovec.iov_len);
	} else if (iter_type == 1 /* ITER_IOVEC */) {
		const struct iovec *iov = BPF_CORE_READ(msg, msg_iter.__iov);

		if (iov) {
			base = BPF_CORE_READ(iov, iov_base);
			len  = BPF_CORE_READ(iov, iov_len);
		}
	} else {
		/* bvec/kvec/xarray: kernel-internal, not an agent's write(). */
		*wanted = 0;
		return 0;
	}

	if (!base || !len) {
		*wanted = 0;
		return 0;
	}

	*wanted = (__u32)(len > 0xffffffffULL ? 0xffffffffU : len);

	n = (__u32)len;
	if (n > cap)
		n = cap;
	/* The verifier needs a constant upper bound it can prove. */
	if (n > TX_WAL_PAYLOAD_MAX)
		n = TX_WAL_PAYLOAD_MAX;
	if (n == 0)
		return 0;

	if (bpf_probe_read_user(dst, n, base) != 0)
		return 0;
	return n;
}

/*
 * Emit one WAL record.  Returns 0 on success.
 *
 * A full ring is NOT silently tolerated. include/agenttx.h states the
 * policy: overflow dooms the transaction, because a WAL with a hole in it
 * can neither be replayed on commit nor discarded on abort. We count the
 * drop here and the supervisor acts on it; the alternative -- carrying on
 * with a truncated log -- is the one outcome that is worse than failing.
 */
static __always_inline int wal_emit(__u64 tx, __u8 hook, __u8 klass,
				    __u8 verdict, __u8 conf, __u32 nr,
				    __u64 path_hash, __u32 daddr, __u16 dport,
				    __u16 family, __u32 msg_flags,
				    struct msghdr *msg)
{
	struct tx_wal_rec *r;
	__u64 pidtgid;

	r = bpf_ringbuf_reserve(&tx_wal, sizeof(*r), 0);
	if (!r) {
		bump(TX_STAT_DROPPED);
		return -1;
	}

	pidtgid = bpf_get_current_pid_tgid();

	r->seq		= next_seq(tx);
	r->tx_id	= tx;
	r->ts_ns	= bpf_ktime_get_ns();
	r->pid		= (__u32)pidtgid;
	r->tgid		= (__u32)(pidtgid >> 32);
	r->hook		= hook;
	r->klass	= klass;
	r->verdict	= verdict;
	r->confidence	= conf;
	r->syscall_nr	= nr;
	r->path_hash	= path_hash;
	r->daddr_v4	= daddr;
	r->dport	= dport;
	r->family	= family;
	/*
	 * NOTE: msg_flags has nowhere to go.
	 *
	 * struct tx_wal_rec carries no field for it, yet the feature vector
	 * (enum tx_feature_idx) has TX_FEAT_MSG_FLAGS and calls MSG_DONTWAIT
	 * "the f-and-f signal". So the classifier is specified to use a value
	 * the WAL cannot record, and a model trained on captures would be
	 * missing the feature its own contract names.
	 *
	 * An earlier version of this file smuggled it through payload_trunc,
	 * which is worse than the gap: it silently corrupted a field that now
	 * means something. Recorded in docs/journal/p3.md as a
	 * contract-change candidate; msg_flags is still used for the in-kernel
	 * decision below, which is where it actually matters.
	 */
	(void)msg_flags;
	r->payload_len	= 0;
	r->payload_trunc = 0;
	if (msg) {
		__u32 wanted = 0;
		__u32 got = capture_payload(msg, r->payload,
					    TX_WAL_PAYLOAD_MAX, &wanted);

		r->payload_len = got;
		r->payload_trunc = wanted > got ? wanted - got : 0;
	}

	bpf_ringbuf_submit(r, 0);
	bump(TX_STAT_EMITTED);
	return 0;
}

/* FNV-1a over a bounded prefix.  Bounded because the verifier requires it
 * and because an unbounded string walk in a hook is a latency bug. */
#define TX_HASH_MAX	64

static __always_inline __u64 hash_prefix(const void *p, int max)
{
	__u64 h = 0xcbf29ce484222325ULL;
	char buf[TX_HASH_MAX] = {};
	int i;

	if (max > TX_HASH_MAX)
		max = TX_HASH_MAX;
	if (bpf_probe_read_kernel_str(buf, max, p) < 0)
		return 0;

	for (i = 0; i < TX_HASH_MAX; i++) {
		if (!buf[i])
			break;
		h ^= (__u64)(__u8)buf[i];
		h *= 0x100000001b3ULL;
	}
	return h;
}

/* ------------------------------------------------------------------ */
/* P3-03 / P3-04: the first hook, gated on transaction state.          */
/* ------------------------------------------------------------------ */

SEC("lsm/file_open")
int BPF_PROG(tx_file_open, struct file *file)
{
	__u64 tx;
	__u64 ph = 0;
	const unsigned char *name;

	bump(TX_STAT_SEEN);

	tx = bpf_tx_current_id();
	if (!tx)
		return 0;		/* not transacting: the common path */
	bump(TX_STAT_IN_TX);

	name = BPF_CORE_READ(file, f_path.dentry, d_name.name);
	if (name)
		ph = hash_prefix(name, TX_HASH_MAX);

	/*
	 * Log only. A file open inside a transaction is reversible by
	 * construction -- the CoW layer captures whatever the agent does with
	 * it -- so the verdict is ALLOW and the class is REVERSIBLE. This
	 * hook exists to prove the toolchain end to end, which is exactly
	 * what P3-03 is for.
	 */
	wal_emit(tx, TX_HOOK_FILE_OPEN, TX_REVERSIBLE, TX_V_ALLOW,
		 255, 0, ph, 0, 0, 0, 0, NULL);
	return 0;
}

/* ------------------------------------------------------------------ */
/* P3-05: deletion and execution.                                      */
/* ------------------------------------------------------------------ */

SEC("lsm/path_unlink")
int BPF_PROG(tx_path_unlink, const struct path *dir, struct dentry *dentry)
{
	__u64 tx = bpf_tx_current_id();
	__u64 ph = 0;
	const unsigned char *name;

	if (!tx)
		return 0;
	bump(TX_STAT_IN_TX);

	name = BPF_CORE_READ(dentry, d_name.name);
	if (name)
		ph = hash_prefix(name, TX_HASH_MAX);

	/*
	 * A delete inside a transaction is reversible: overlayfs records it
	 * as a whiteout in the upper layer and abort discards the whiteout,
	 * which brings the file back. tests/p2/t02 asserts exactly that, so
	 * this class is a measured claim rather than an assumption.
	 */
	wal_emit(tx, TX_HOOK_INODE_UNLINK, TX_REVERSIBLE, TX_V_CAPTURED,
		 255, 0, ph, 0, 0, 0, 0, NULL);
	return 0;
}

SEC("lsm/bprm_check_security")
int BPF_PROG(tx_bprm_check, struct linux_binprm *bprm)
{
	__u64 tx = bpf_tx_current_id();
	__u64 ph = 0;
	const char *fn;

	if (!tx)
		return 0;
	bump(TX_STAT_IN_TX);

	fn = BPF_CORE_READ(bprm, filename);
	if (fn)
		ph = hash_prefix(fn, TX_HASH_MAX);

	/*
	 * exec() is recorded, not blocked. The agent running a compiler is
	 * the normal case and the whole point of the sandbox is that its
	 * effects are contained rather than forbidden.
	 */
	wal_emit(tx, TX_HOOK_BPRM_CHECK, TX_REVERSIBLE, TX_V_ALLOW,
		 255, 0, ph, 0, 0, 0, 0, NULL);
	return 0;
}

/* ------------------------------------------------------------------ */
/* P3-05: the outbound path -- the reason this project exists.         */
/* ------------------------------------------------------------------ */

SEC("lsm/socket_connect")
int BPF_PROG(tx_socket_connect, struct socket *sock, struct sockaddr *address,
	     int addrlen)
{
	__u64 tx = bpf_tx_current_id();
	__u16 family = 0, dport = 0;
	__u32 daddr = 0;

	if (!tx)
		return 0;
	bump(TX_STAT_IN_TX);

	bpf_probe_read_kernel(&family, sizeof(family), &address->sa_family);
	if (family == AF_INET) {
		struct sockaddr_in *in = (struct sockaddr_in *)address;

		bpf_probe_read_kernel(&daddr, sizeof(daddr),
				      &in->sin_addr.s_addr);
		bpf_probe_read_kernel(&dport, sizeof(dport), &in->sin_port);
		dport = bpf_ntohs(dport);
	}

	/*
	 * A connect is not itself an outbound effect -- nothing has been said
	 * to the peer yet. Recorded so the WAL can correlate later sends
	 * against the destination, which is what the compensable registry is
	 * keyed on.
	 */
	wal_emit(tx, TX_HOOK_SOCKET_CONNECT, TX_REVERSIBLE, TX_V_ALLOW,
		 255, 0, 0, daddr, dport, family, 0, NULL);
	return 0;
}

/*
 * The static rule table -- P3-06's baseline, and the interface P3-11's
 * model replaces. Deliberately small and deliberately explicit: this is the
 * thing the classifier has to BEAT, so it must be honest rather than weak.
 *
 * The TCP rule is grounded in our own measurement rather than in intuition.
 * docs/journal/p4.md: on a real agent trace, 18 of 19 connections read a
 * reply back. Deferring a request-response send does not delay an effect,
 * it deadlocks the agent -- it is waiting for an answer that our own
 * suppression is preventing. So TCP is emitted unless it is explicitly
 * flagged fire-and-forget by MSG_DONTWAIT.
 */
static __always_inline __u8 classify(__u32 daddr, __u16 dport, __u8 proto,
				     __u32 msg_flags, __u8 *conf)
{
	__u32 k = dport;
	__u8 *override;

	/* An operator override always wins: the registry is shipped, not
	 * learned (PROPOSAL.md, on the compensable registry). */
	override = bpf_map_lookup_elem(&tx_rules, &k);
	if (override) {
		*conf = 255;
		return *override;
	}

	/* Loopback never leaves the machine, so it is not an outbound
	 * effect at all. 127.0.0.0/8 in network byte order. */
	if ((daddr & 0xff) == 127) {
		*conf = 250;
		return TX_REVERSIBLE;
	}

	if (proto == IPPROTO_UDP) {
		/* No reply is expected by construction. This is the case the
		 * whole mechanism is for. */
		*conf = 230;
		return TX_DEFERRABLE;
	}

	if (msg_flags & MSG_DONTWAIT) {
		*conf = 200;
		return TX_DEFERRABLE;
	}

	/* Everything else: assume the agent is waiting for an answer.
	 * Emitting is the safe error here -- a wrongly-emitted effect is a
	 * missed opportunity, a wrongly-deferred one is a hang. */
	*conf = 190;
	return TX_COMPENSABLE;
}

SEC("lsm/socket_sendmsg")
int BPF_PROG(tx_socket_sendmsg, struct socket *sock, struct msghdr *msg,
	     int size)
{
	__u64 tx = bpf_tx_current_id();
	__u16 family = 0, dport = 0, sport = 0;
	__u32 daddr = 0, saddr = 0, mflags = 0;
	__u8 klass, conf = 0, proto = 0, verdict;
	struct sock *sk;

	if (!tx)
		return 0;
	bump(TX_STAT_IN_TX);

	sk = BPF_CORE_READ(sock, sk);
	if (!sk)
		return 0;

	family = BPF_CORE_READ(sk, __sk_common.skc_family);
	if (family != AF_INET)
		return 0;		/* v6 and unix are out of scope for now */

	daddr  = BPF_CORE_READ(sk, __sk_common.skc_daddr);
	saddr  = BPF_CORE_READ(sk, __sk_common.skc_rcv_saddr);
	dport  = bpf_ntohs(BPF_CORE_READ(sk, __sk_common.skc_dport));
	sport  = BPF_CORE_READ(sk, __sk_common.skc_num);   /* already host order */
	proto  = BPF_CORE_READ(sk, sk_protocol);
	mflags = BPF_CORE_READ(msg, msg_flags);

	/*
	 * An unconnected UDP sendto() leaves skc_daddr at 0 -- the
	 * destination is in msg_name, not on the socket. Read it from there
	 * or every such send classifies against address 0.0.0.0.
	 */
	if (!daddr) {
		struct sockaddr_in *sin = (struct sockaddr_in *)BPF_CORE_READ(msg, msg_name);

		if (sin) {
			bpf_probe_read_kernel(&daddr, sizeof(daddr), &sin->sin_addr.s_addr);
			bpf_probe_read_kernel(&dport, sizeof(dport), &sin->sin_port);
			dport = bpf_ntohs(dport);
		}
	}

	/*
	 * P3-11: the model replaces the static rules, and the swap is this
	 * small because both go through the same funnel. If landing the
	 * classifier had needed changes elsewhere in this hook, the contract
	 * between P3 and P4 was drawn wrong.
	 */
	{
		__u32 zero = 0;
		struct tx_model_tree *m = bpf_map_lookup_elem(&tx_model, &zero);
		__u32 k = dport;
		__u8 *override = bpf_map_lookup_elem(&tx_rules, &k);

		if (override) {
			/* The compensable registry is shipped, not learned
			 * (PROPOSAL.md). An operator override outranks the
			 * model by design. */
			klass = *override;
			conf = 255;
			bump(TX_STAT_RULES);
		} else if (m && m->hdr.magic == TX_MODEL_MAGIC) {
			struct tx_feat_in fi = {};
			struct tx_features fv;

			/*
			 * NOT the syscall number -- an LSM hook runs below
			 * syscall dispatch and cannot see it. An earlier
			 * version passed `size` here, which fed the message
			 * byte count into a feature trained on syscall
			 * numbers. Left at 0, and the model is trained with
			 * --kernel-only so it cannot split on it.
			 */
			fi.syscall_nr	     = 0;
			fi.hook		     = TX_HOOK_SOCKET_SENDMSG;
			fi.dport	     = dport;
			fi.family	     = family;
			fi.is_loopback	     = ((daddr & 0xff) == 127);
			fi.in_tx	     = 1;
			fi.fd_type	     = 12;	/* S_IFSOCK >> 12 */
			fi.msg_flags_kernel  = mflags;

			tx_features_extract(&fi, &fv);
			klass = (__u8)tx_tree_classify(m, &fv, &conf);
			bump(TX_STAT_MODEL);
		} else {
			klass = classify(daddr, dport, proto, mflags, &conf);
			bump(TX_STAT_RULES);
		}
	}

	/*
	 * The fail-closed rule, applied exactly as tx_class_final() applies
	 * it in src/core. Same threshold, same direction, one place in each
	 * half of the system.
	 */
	if (conf < TX_CONFIDENCE_MIN)
		klass = TX_IRREVOCABLE;

	if (klass == TX_DEFERRABLE) {
		struct tx_flow f = {
			.saddr = saddr, .daddr = daddr,
			.sport = sport, .dport = dport,
			.proto = proto,
		};

		/*
		 * Record BEFORE returning. The packet is built and transmitted
		 * after this hook returns, so the egress program must already
		 * know to drop it -- there is no second chance.
		 */
		if (sport) {
			bpf_map_update_elem(&tx_defer_flows, &f, &tx, BPF_ANY);
		} else {
			/* Source port not assigned yet: see tx_defer_dests. */
			struct tx_dest d = {
				.daddr = daddr, .dport = dport, .proto = proto,
			};

			bpf_map_update_elem(&tx_defer_dests, &d, &tx, BPF_ANY);
		}
		bump(TX_STAT_DEFERRED);
		verdict = TX_V_DEFERRED;
	} else if (klass == TX_IRREVOCABLE) {
		/*
		 * Dooms the transaction through P1's kfunc: abort is no longer
		 * available once an irrevocable effect is on its way out. We
		 * still return 0 -- escalation to a human is P3-10, and
		 * denying here would be a policy this fragment has not earned.
		 */
		bpf_tx_note_class(tx, TX_IRREVOCABLE);
		verdict = TX_V_ESCALATED;
	} else {
		verdict = TX_V_EMITTED;
	}

	wal_emit(tx, TX_HOOK_SOCKET_SENDMSG, klass, verdict, conf,
		 (__u32)size, 0, daddr, dport, family, mflags, msg);

	/*
	 * ALWAYS 0. This is the line the whole project turns on: userspace is
	 * told the send succeeded. For a deferred flow that is a statement the
	 * kernel is about to make true or false depending on whether the
	 * transaction commits.
	 */
	return 0;
}

/* ------------------------------------------------------------------ */
/* P3-08, the other half: suppress the emission.                       */
/* ------------------------------------------------------------------ */
/*
 * Runs after the syscall has already returned success. Returning 0 here
 * drops the packet; 1 lets it through.
 *
 * This program is attached to the cgroup the agent runs in, so it sees only
 * that subtree's traffic -- and it does nothing at all unless the flow is in
 * tx_defer_flows, which only the LSM hook writes and only inside a
 * transaction.
 */
/*
 * WHY tcx/egress AND NOT cgroup_skb/egress.
 *
 * Both can drop a packet. Only one does it TRANSPARENTLY, and transparency
 * is the entire property this fragment claims.
 *
 * cgroup_skb/egress returning 0 makes __cgroup_bpf_run_filter_skb() return
 * -EPERM, which propagates up ip_output -> udp_send_skb -> udp_sendmsg and
 * out to userspace. Measured, not guessed: the agent's sendto() raised
 * `PermissionError: [Errno 1] Operation not permitted`. The packet was
 * indeed suppressed -- and the agent was told, which defeats the point.
 *
 * tc drops the packet inside __dev_queue_xmit, which returns NET_XMIT_DROP;
 * net_xmit_errno() maps that to -ENOBUFS, and net/ipv4/udp.c:986 converts
 * -ENOBUFS to err = 0 for any socket without IP_RECVERR. So udp_sendmsg
 * returns the byte count and the sender sees success.
 *
 * That asymmetry is not incidental. The kernel already has a notion of
 * "the packet went away and the sender does not need to know" -- it is how
 * a full transmit queue behaves -- and deferral is exactly that, made
 * deliberate. The mechanism works because UDP is already allowed to lose
 * packets silently.
 *
 * The honest consequence, stated here because it bounds the contribution:
 * this transparency is a property of DATAGRAM semantics. A TCP send
 * suppressed this way is retransmitted by the stack and eventually errors
 * the connection, so TCP deferral needs a different mechanism and a bounded
 * window. The rule table therefore does not defer TCP (see classify()), and
 * our own gate measurement says most agent TCP traffic is request-response
 * anyway.
 */
#ifndef TC_ACT_OK
#define TC_ACT_OK	0
#endif
#ifndef TC_ACT_SHOT
#define TC_ACT_SHOT	2
#endif

SEC("tcx/egress")
int tx_egress(struct __sk_buff *skb)
{
	struct tx_flow f = {};
	struct tx_dest d = {};
	struct iphdr ip;
	__u64 *tx;
	__u32 l4off;

	bump(TX_STAT_EGRESS_SEEN);

	/*
	 * Read relative to the NETWORK header. At tc level skb->data starts
	 * at L2 on most devices and at L3 on others, and hardcoding either
	 * one silently parses garbage on the interfaces it gets wrong.
	 */
	if (bpf_skb_load_bytes_relative(skb, 0, &ip, sizeof(ip),
					BPF_HDR_START_NET) < 0)
		return TC_ACT_OK;
	if (ip.version != 4)
		return TC_ACT_OK;
	bump(TX_STAT_EGRESS_INET);

	l4off = (__u32)ip.ihl * 4;
	if (l4off < sizeof(struct iphdr) || l4off > 60)
		return TC_ACT_OK;

	f.saddr = ip.saddr;
	f.daddr = ip.daddr;
	f.proto = ip.protocol;

	if (ip.protocol == IPPROTO_UDP) {
		struct udphdr uh;

		if (bpf_skb_load_bytes_relative(skb, l4off, &uh, sizeof(uh),
						BPF_HDR_START_NET) < 0)
			return TC_ACT_OK;
		f.sport = bpf_ntohs(uh.source);
		f.dport = bpf_ntohs(uh.dest);
	} else if (ip.protocol == IPPROTO_TCP) {
		struct tcphdr th;

		if (bpf_skb_load_bytes_relative(skb, l4off, &th, sizeof(th),
						BPF_HDR_START_NET) < 0)
			return TC_ACT_OK;
		f.sport = bpf_ntohs(th.source);
		f.dport = bpf_ntohs(th.dest);
	} else {
		return TC_ACT_OK;
	}

	/* Exact flow first: a connected socket gave the hook a source port. */
	tx = bpf_map_lookup_elem(&tx_defer_flows, &f);
	if (!tx) {
		/* Then the destination-only key, for the unconnected case
		 * where no source port existed when the hook ran. */
		d.daddr = f.daddr;
		d.dport = f.dport;
		d.proto = f.proto;
		tx = bpf_map_lookup_elem(&tx_defer_dests, &d);
	}
	if (!tx)
		return TC_ACT_OK;

	bump(TX_STAT_SUPPRESSED);
	return TC_ACT_SHOT;		/* dropped; the sender is not told */
}
