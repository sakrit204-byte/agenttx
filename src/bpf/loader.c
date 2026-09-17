// SPDX-License-Identifier: GPL-2.0
/*
 * src/bpf/loader.c --- load, attach and drain.  Fragment P3-02.
 *
 *   txload            attach the hooks and stream the WAL
 *   txload --stats    attach, print counters every second
 *   txload --jsonl F  also write docs/trace-format.md records to F
 *   txload --once     load, attach, verify, detach. For tests.
 *
 * WHAT MUST BE TRUE BEFORE THIS CAN WORK, and the error each one gives:
 *
 *   agenttx.ko loaded        the kfuncs are resolved through the MODULE's
 *                            BTF. Without it the verifier rejects the
 *                            program naming bpf_tx_current_id.
 *   bpf in the LSM list      otherwise the programs attach successfully and
 *                            never fire -- the worst failure mode, because
 *                            everything looks fine.
 *   CONFIG_DEBUG_INFO_BTF_MODULES=y   or /sys/kernel/btf/agenttx does not
 *                            exist and the kfuncs cannot be found.
 *
 * All three are checked up front, because each one produces a confusing
 * error much later.
 *
 * Owner: P3.
 */

#include <argp.h>
#include <bpf/libbpf.h>
#include <bpf/bpf.h>
#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <net/if.h>
#include <arpa/inet.h>
#include <dirent.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <netinet/in.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include "agenttx.h"
#include "infer.h"
#include "agenttx.skel.h"

/* ------------------------------------------------------------------ */
/* P3-09: flush on commit, discard on abort.                           */
/* ------------------------------------------------------------------ */
/*
 * WHY REPLAY IS IN USERSPACE.
 *
 * Two reasons, and neither is convenience. The suppression state lives in
 * BPF maps, which are managed from userspace; and transmitting a captured
 * datagram from kernel context would mean building sk_buffs by hand for no
 * benefit over a socket. The supervisor is already the process that decides
 * to commit, and it is the natural place to make the decision real.
 *
 * THE ORDERING THAT MATTERS.
 *
 * Suppression must be cleared BEFORE the replay is sent. Our own egress
 * program keys deferred UDP on (daddr, dport, proto) -- the destination-only
 * fallback -- so a replay from a fresh socket matches it just as well as the
 * original did. Replaying first would mean the commit path emitted a packet
 * and then dropped it with its own rule, and commit would silently be a
 * no-op that reported success. Clear, then send.
 */
#define TX_CTL_DIR	"/run/agenttx"

struct deferred {
	struct deferred *next;
	struct tx_wal_rec rec;
};

static struct deferred *deferred_head;
static unsigned long long n_deferred_held;

static void defer_remember(const struct tx_wal_rec *r)
{
	struct deferred *d = calloc(1, sizeof(*d));

	if (!d)
		return;
	d->rec = *r;
	d->next = deferred_head;
	deferred_head = d;
	n_deferred_held++;
}

/* Remove every defer-map entry belonging to @tx. */
static int defer_clear(struct agenttx_bpf *skel, __u64 tx)
{
	int removed = 0;
	int fds[2] = { bpf_map__fd(skel->maps.tx_defer_flows),
		       bpf_map__fd(skel->maps.tx_defer_dests) };
	size_t ksz[2] = { bpf_map__key_size(skel->maps.tx_defer_flows),
			  bpf_map__key_size(skel->maps.tx_defer_dests) };
	int i;

	for (i = 0; i < 2; i++) {
		unsigned char key[64] = {}, next[64] = {};
		int have = 0;
		__u64 val = 0;

		if (fds[i] < 0 || ksz[i] > sizeof(key))
			continue;
		while (bpf_map_get_next_key(fds[i], have ? key : NULL, next) == 0) {
			memcpy(key, next, ksz[i]);
			have = 1;
			if (bpf_map_lookup_elem(fds[i], key, &val) == 0 && val == tx) {
				if (bpf_map_delete_elem(fds[i], key) == 0)
					removed++;
				/* Deleting invalidates iteration position on
				 * some map types; restart to be safe. */
				have = 0;
			}
		}
	}
	return removed;
}

/*
 * Send one captured datagram again, from a fresh socket.
 *
 * The source port will differ from the original -- the socket is new -- and
 * that is correct: a fire-and-forget datagram carries no reply the peer
 * could route back. It is also why clearing the destination-only key first
 * is mandatory rather than tidy.
 */
static int replay_one(const struct tx_wal_rec *r)
{
	struct sockaddr_in to = {};
	int fd, rc;

	if (r->payload_len == 0)
		return -1;			/* nothing captured to replay */

	fd = socket(AF_INET, SOCK_DGRAM, 0);
	if (fd < 0)
		return -1;
	to.sin_family = AF_INET;
	to.sin_port = htons(r->dport);
	to.sin_addr.s_addr = r->daddr_v4;

	rc = sendto(fd, r->payload, r->payload_len, 0,
		    (struct sockaddr *)&to, sizeof(to));
	close(fd);
	return rc < 0 ? -1 : 0;
}

static int cmp_seq(const void *a, const void *b)
{
	const struct tx_wal_rec *x = *(const struct tx_wal_rec **)a;
	const struct tx_wal_rec *y = *(const struct tx_wal_rec **)b;

	return x->seq < y->seq ? -1 : x->seq > y->seq;
}

/*
 * @flush: replay in seq order, then forget. Otherwise: forget.
 *
 * Replay order is the contract's, not ours: include/agenttx.h says seq is
 * "per-tx and gap-free" and "defines replay order". An agent that wrote a
 * log line before a notification meant them in that order, and a commit that
 * reorders them has changed what the transaction did.
 */
static int defer_finish(struct agenttx_bpf *skel, __u64 tx, int flush)
{
	struct deferred **keep = &deferred_head, *d;
	struct tx_wal_rec **batch = NULL;
	int n = 0, cap = 0, sent = 0, i;

	/* Clear suppression FIRST. See the comment at the top of this block. */
	int cleared = defer_clear(skel, tx);

	while ((d = *keep)) {
		if (d->rec.tx_id != tx) {
			keep = &d->next;
			continue;
		}
		*keep = d->next;
		if (flush) {
			if (n == cap) {
				cap = cap ? cap * 2 : 16;
				batch = realloc(batch, cap * sizeof(*batch));
				if (!batch)
					break;
			}
			batch[n++] = &d->rec;
		} else {
			free(d);
		}
		if (n_deferred_held)
			n_deferred_held--;
	}

	if (flush && n) {
		qsort(batch, n, sizeof(*batch), cmp_seq);
		for (i = 0; i < n; i++)
			if (replay_one(batch[i]) == 0)
				sent++;
	}
	free(batch);

	printf("txload: tx=%llu %s -- %d suppression entr%s cleared, "
	       "%d of %d effect(s) %s\n",
	       (unsigned long long)tx, flush ? "FLUSH" : "DISCARD",
	       cleared, cleared == 1 ? "y" : "ies",
	       flush ? sent : 0, n, flush ? "replayed" : "discarded");
	return flush ? sent : n;
}

/*
 * Control channel: a file per request under /run/agenttx.
 *
 * Deliberately the dumbest thing that works. The supervisor is a different
 * process (txctl), the maps are not pinned yet, and a socket protocol here
 * would be a second thing to debug when the first one breaks. When the maps
 * are pinned to TX_PIN_DIR the supervisor can do this itself and the channel
 * disappears entirely.
 */
static void poll_control(struct agenttx_bpf *skel)
{
	DIR *dir = opendir(TX_CTL_DIR);
	struct dirent *e;
	char path[512];

	if (!dir)
		return;
	while ((e = readdir(dir))) {
		unsigned long long tx = 0;
		int flush = -1;

		if (sscanf(e->d_name, "flush-%llu", &tx) == 1)
			flush = 1;
		else if (sscanf(e->d_name, "discard-%llu", &tx) == 1)
			flush = 0;
		if (flush < 0)
			continue;

		defer_finish(skel, (__u64)tx, flush);
		snprintf(path, sizeof(path), "%s/%s", TX_CTL_DIR, e->d_name);
		unlink(path);
		snprintf(path, sizeof(path), "%s/done-%llu", TX_CTL_DIR, tx);
		close(open(path, O_CREAT | O_WRONLY, 0600));
	}
	closedir(dir);
}

static volatile sig_atomic_t stop;
static FILE *jsonl;
static unsigned long long n_records;

static const char *const hook_names[]    = TX_HOOK_NAMES;
static const char *const class_names[]   = TX_CLASS_NAMES;
static const char *const verdict_names[] = TX_VERDICT_NAMES;

static const char *nm(const char *const *t, unsigned n, unsigned i)
{
	return i < n ? t[i] : "?";
}

static void on_sig(int _s) { (void)_s; stop = 1; }

static int quiet_libbpf(enum libbpf_print_level lvl, const char *fmt, va_list ap)
{
	if (lvl == LIBBPF_DEBUG)
		return 0;
	return vfprintf(stderr, fmt, ap);
}

/* --- preflight ---------------------------------------------------- */
static int preflight(void)
{
	FILE *f;
	char buf[512] = {};
	int bad = 0;

	if (access(AGENTTX_DEV_PATH, F_OK) != 0) {
		fprintf(stderr,
			"txload: %s is absent -- agenttx.ko is not loaded.\n"
			"        The kfuncs these programs call live in that module;\n"
			"        without it the verifier cannot resolve them.\n",
			AGENTTX_DEV_PATH);
		bad++;
	}

	f = fopen("/sys/kernel/security/lsm", "r");
	if (f) {
		if (fgets(buf, sizeof(buf), f) && !strstr(buf, "bpf")) {
			fprintf(stderr,
				"txload: `bpf` is not in the active LSM list (%s).\n"
				"        The programs would attach and NEVER FIRE.\n"
				"        Boot with lsm=...,bpf\n", buf);
			bad++;
		}
		fclose(f);
	}

	if (access("/sys/kernel/btf/agenttx", R_OK) != 0) {
		fprintf(stderr,
			"txload: /sys/kernel/btf/agenttx is absent.\n"
			"        Rebuild the kernel with CONFIG_DEBUG_INFO_BTF_MODULES=y,\n"
			"        and make sure pahole was installed when you did.\n");
		bad++;
	}
	return bad;
}

/* --- the WAL ------------------------------------------------------ */
static int on_record(void *ctx, void *data, size_t len)
{
	const struct tx_wal_rec *r = data;
	(void)ctx;

	if (len < sizeof(*r))
		return 0;
	n_records++;

	/* Hold on to anything the hook deferred: commit has to replay it. */
	if (r->verdict == TX_V_DEFERRED)
		defer_remember(r);

	printf("  seq=%-4llu tx=%-3llu %-15s %-12s %-10s",
	       (unsigned long long)r->seq, (unsigned long long)r->tx_id,
	       nm(hook_names, TX_HOOK_MAX, r->hook),
	       nm(class_names, TX_CLASS_MAX, r->klass),
	       nm(verdict_names, TX_V_MAX, r->verdict));
	if (r->daddr_v4) {
		unsigned char *a = (unsigned char *)&r->daddr_v4;

		printf(" -> %u.%u.%u.%u:%u", a[0], a[1], a[2], a[3], r->dport);
	} else if (r->path_hash) {
		printf(" path#%016llx", (unsigned long long)r->path_hash);
	}
	printf("\n");
	fflush(stdout);

	if (jsonl) {
		/* docs/trace-format.md. Payloads are never written out: a real
		 * capture must not carry bytes off the machine that made it. */
		fprintf(jsonl,
			"{\"v\":1,\"seq\":%llu,\"ts_ns\":%llu,\"tx_id\":%llu,"
			"\"pid\":%u,\"tgid\":%u,\"hook\":\"%s\",\"klass\":\"%s\","
			"\"verdict\":\"%s\",\"confidence\":%u,\"path_hash\":%llu,"
			"\"daddr\":\"%u.%u.%u.%u\",\"dport\":%u,\"family\":%u,"
			"\"msg_flags\":%u,\"payload_prefix\":null,"
			"\"label\":null,\"label_source\":\"hook\"}\n",
			(unsigned long long)r->seq, (unsigned long long)r->ts_ns,
			(unsigned long long)r->tx_id, r->pid, r->tgid,
			nm(hook_names, TX_HOOK_MAX, r->hook),
			nm(class_names, TX_CLASS_MAX, r->klass),
			nm(verdict_names, TX_V_MAX, r->verdict),
			r->confidence, (unsigned long long)r->path_hash,
			((unsigned char *)&r->daddr_v4)[0],
			((unsigned char *)&r->daddr_v4)[1],
			((unsigned char *)&r->daddr_v4)[2],
			((unsigned char *)&r->daddr_v4)[3],
			r->dport, r->family, r->payload_trunc);
		fflush(jsonl);
	}
	return 0;
}

static void print_stats(struct agenttx_bpf *skel)
{
	static const char *const names[] = {
		"syscalls seen", "inside a tx", "WAL records", "DROPPED (ring full)",
		"DEFERRED (held back)", "SUPPRESSED (packets dropped)",
		"egress program ran", "egress saw AF_INET",
		"classified by the MODEL", "classified by RULES"
	};
	int fd = bpf_map__fd(skel->maps.tx_stats);
	__u32 k;
	__u64 v;

	printf("\n  counters\n");
	for (k = 0; k < 10; k++) {
		v = 0;
		bpf_map_lookup_elem(fd, &k, &v);
		printf("    %-30s %llu%s\n", names[k], (unsigned long long)v,
		       (k == 3 && v) ? "   <- a dropped record dooms the tx" :
		       (k == 5 && v) ? "   <- these never reached the peer" : "");
	}
}

int main(int argc, char **argv)
{
	struct agenttx_bpf *skel = NULL;
	struct ring_buffer *rb = NULL;
	struct bpf_link *tcx_links[8] = {};
	int n_links = 0;
	int once = 0, stats = 0, err, i, do_pin = 0, do_unpin = 0;
	const char *jpath = NULL, *mpath = NULL;
	int defer_ports[16], n_defer = 0;

	for (i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--once"))        once = 1;
		else if (!strcmp(argv[i], "--stats"))  stats = 1;
		else if (!strcmp(argv[i], "--jsonl") && i + 1 < argc) jpath = argv[++i];

		else if (!strcmp(argv[i], "--model") && i + 1 < argc) mpath = argv[++i];
		else if (!strcmp(argv[i], "--pin"))   do_pin = 1;
		else if (!strcmp(argv[i], "--unpin")) do_unpin = 1;
		else if (!strcmp(argv[i], "--defer-port") && i + 1 < argc &&
			 n_defer < 16) defer_ports[n_defer++] = atoi(argv[++i]);
		else {
			fprintf(stderr,
				"usage: txload [--once] [--stats] [--jsonl F]\n"
				"              [--model FILE] [--defer-port N]...\n"
				"              [--pin]    keep the hooks attached after exit\n"
				"              [--unpin]  detach pinned hooks and quit\n");
			return 2;
		}
	}

	/*
	 * Line-buffer stdout.
	 *
	 * When stdout is a terminal glibc line-buffers it and everything
	 * appears as it happens. When it is redirected to a file -- which is
	 * how every test and the dashboard run this -- it switches to full
	 * buffering, and the startup banner sits in a 4 KiB buffer until the
	 * process exits. A test that greps the log for "attached" then sees
	 * nothing and reports a failure that did not occur.
	 */
	setvbuf(stdout, NULL, _IOLBF, 0);

	if (do_unpin) {
		static const char *names[] = {
			"file_open", "path_unlink", "bprm_check", "socket_connect",
			"socket_sendmsg", "egress0", "egress1", "egress2", "egress3",
		};
		char path[256];
		int n = 0;

		for (i = 0; i < (int)(sizeof(names) / sizeof(*names)); i++) {
			snprintf(path, sizeof(path), "%s/%s", TX_PIN_DIR, names[i]);
			if (unlink(path) == 0)
				n++;
		}
		printf("txload: unpinned %d link(s)\n", n);
		return 0;
	}

	if (preflight())
		return 1;

	libbpf_set_print(quiet_libbpf);
	setrlimit(RLIMIT_MEMLOCK, &(struct rlimit){RLIM_INFINITY, RLIM_INFINITY});

	skel = agenttx_bpf__open();
	if (!skel) {
		fprintf(stderr, "txload: open failed\n");
		return 1;
	}
	/*
	 * A cgroup_skb program has no implicit target, so libbpf cannot
	 * auto-attach it -- it needs a cgroup fd we choose. Turn autoattach
	 * off for it and do it by hand below; leaving it on makes
	 * agenttx_bpf__attach() fail for the whole skeleton.
	 */
	bpf_program__set_autoattach(skel->progs.tx_egress, false);

	if (agenttx_bpf__load(skel)) {
		fprintf(stderr, "txload: load failed: %s\n", strerror(errno));
		fprintf(stderr,
			"        If the message above mentions a kfunc, the module's\n"
			"        BTF could not be read. If it mentions a field, the\n"
			"        vmlinux.h was generated from a different kernel than\n"
			"        the one running -- regenerate it after every\n"
			"        `make vm-kernel`.\n");
		return 1;
	}

	/*
	 * Load the model (P4-10 / P3-11).
	 *
	 * Validated HERE rather than in the hook. The BPF side can only fail
	 * closed -- every effect becomes irrevocable and everything escalates
	 * -- which is safe and tells you nothing. A bad blob should be a
	 * refusal at load time with a reason, not a system that silently
	 * escalates forever.
	 */
	if (mpath) {
		struct tx_model_tree m = {};
		FILE *mf = fopen(mpath, "rb");
		size_t got;
		__u32 zero = 0;

		if (!mf) {
			fprintf(stderr, "txload: cannot open model %s: %s\n",
				mpath, strerror(errno));
			err = 1;
			goto out;
		}
		got = fread(&m, 1, sizeof(m), mf);
		fclose(mf);

		if (got != sizeof(m)) {
			fprintf(stderr,
				"txload: %s is %zu bytes, expected %zu "
				"(sizeof(struct tx_model_tree))\n",
				mpath, got, sizeof(m));
			err = 1;
			goto out;
		}
		if (m.hdr.magic != TX_MODEL_MAGIC) {
			fprintf(stderr, "txload: %s has magic 0x%08x, expected 0x%08x\n",
				mpath, m.hdr.magic, TX_MODEL_MAGIC);
			err = 1;
			goto out;
		}
		if (m.hdr.abi != AGENTTX_ABI_VERSION) {
			fprintf(stderr,
				"txload: model abi %u, this loader expects %u -- "
				"re-export with `make weights`\n",
				m.hdr.abi, AGENTTX_ABI_VERSION);
			err = 1;
			goto out;
		}
		if (m.hdr.kind != TX_MODEL_TREE) {
			fprintf(stderr, "txload: model kind %u is not a tree\n", m.hdr.kind);
			err = 1;
			goto out;
		}
		if (m.hdr.n_features != TX_N_FEATURES ||
		    m.hdr.n_classes != TX_CLASS_MAX) {
			fprintf(stderr,
				"txload: model shape %u features / %u classes, "
				"contract says %u / %u\n",
				m.hdr.n_features, m.hdr.n_classes,
				TX_N_FEATURES, TX_CLASS_MAX);
			err = 1;
			goto out;
		}
		if (bpf_map_update_elem(bpf_map__fd(skel->maps.tx_model),
					&zero, &m, BPF_ANY)) {
			fprintf(stderr, "txload: cannot write the model map: %s\n",
				strerror(errno));
			err = 1;
			goto out;
		}
		printf("txload: model loaded -- %u nodes, trained on %u rows, "
		       "held-out %u.%02u%%\n",
		       m.hdr.n_nodes, m.hdr.trained_rows,
		       m.hdr.accuracy_pct / 100, m.hdr.accuracy_pct % 100);
	} else {
		printf("txload: no model (--model) -- using the static rule table\n");
	}

	/* Operator overrides for the static rule table (P3-06). */
	for (i = 0; i < n_defer; i++) {
		__u32 k = (__u32)defer_ports[i];
		__u8 v = TX_DEFERRABLE;

		if (bpf_map_update_elem(bpf_map__fd(skel->maps.tx_rules),
					&k, &v, BPF_ANY) == 0)
			printf("txload: rule -- port %d is DEFERRABLE\n", defer_ports[i]);
	}

	err = agenttx_bpf__attach(skel);
	if (err) {
		fprintf(stderr, "txload: attach failed: %d (%s)\n", err, strerror(-err));
		goto out;
	}

	/*
	 * The egress half. Without it the LSM hook records a flow as
	 * deferred and the packet leaves anyway -- the system would report
	 * deferral it is not performing, which is the worst thing this
	 * program could do. So a failure here is fatal, not a warning.
	 *
	 * Attached to EVERY interface, because the destination decides which
	 * one a deferred packet leaves by, and attaching to a guess means
	 * silently emitting anything that takes another route. Loopback
	 * matters as much as the uplink: an agent posting to a local service
	 * is still an outbound effect from the transaction's point of view.
	 */
	{
		struct if_nameindex *ifs = if_nameindex(), *p;

		if (!ifs) {
			fprintf(stderr, "txload: if_nameindex: %s\n", strerror(errno));
			err = 1;
			goto out;
		}
		for (p = ifs; p && p->if_index && n_links < 8; p++) {
			struct bpf_link *l =
				bpf_program__attach_tcx(skel->progs.tx_egress,
							p->if_index, NULL);

			if (!l) {
				fprintf(stderr,
					"txload: tcx attach on %s failed: %s\n",
					p->if_name, strerror(errno));
				continue;
			}
			tcx_links[n_links++] = l;
			printf("txload: egress suppression on %s\n", p->if_name);
		}
		if_freenameindex(ifs);
	}
	if (!n_links) {
		fprintf(stderr,
			"txload: could not attach egress suppression anywhere.\n"
			"        Deferred sends would still be emitted, so refusing\n"
			"        to run. Needs CONFIG_NET_XGRESS=y (kernel 6.6+).\n");
		err = 1;
		goto out;
	}

	/*
	 * Pin the links so the hooks OUTLIVE this process.
	 *
	 * A bpf_link is refcounted by the fd that holds it; when txload exits,
	 * the links drop and every program detaches. That has two costs:
	 *
	 *  1. The hooks can only be attached while something is running, so
	 *     any measurement of "what do the hooks cost" also measures that
	 *     process. bench_hooks.sh saw +108ns on getpid() -- a syscall with
	 *     no LSM hook at all -- which is txload draining a ring buffer,
	 *     not hook overhead.
	 *  2. Restarting the WAL reader silently disarms the sandbox.
	 *
	 * Pinning into TX_PIN_DIR (the contract already names it) keeps them
	 * attached with no process alive. Unpin with `rm` on those paths, or
	 * txload --unpin.
	 */
	if (do_pin) {
		char path[256];
		int pinned = 0;

		mkdir(TX_PIN_DIR, 0700);
		struct bpf_link *pins[] = {
			skel->links.tx_file_open, skel->links.tx_path_unlink,
			skel->links.tx_bprm_check, skel->links.tx_socket_connect,
			skel->links.tx_socket_sendmsg,
		};
		static const char *names[] = {
			"file_open", "path_unlink", "bprm_check",
			"socket_connect", "socket_sendmsg",
		};
		for (i = 0; i < 5; i++) {
			if (!pins[i])
				continue;
			snprintf(path, sizeof(path), "%s/%s", TX_PIN_DIR, names[i]);
			unlink(path);
			if (bpf_link__pin(pins[i], path) == 0)
				pinned++;
		}
		for (i = 0; i < n_links; i++) {
			snprintf(path, sizeof(path), "%s/egress%d", TX_PIN_DIR, i);
			unlink(path);
			if (bpf_link__pin(tcx_links[i], path) == 0)
				pinned++;
		}
		printf("txload: pinned %d link(s) under %s -- the hooks now survive "
		       "this process\n", pinned, TX_PIN_DIR);
	}

	printf("txload: 5 LSM hooks + egress suppression attached\n");
	printf("        file_open  path_unlink  bprm_check  socket_connect  socket_sendmsg\n");
	printf("        tcx/egress on %d interface(s)\n", n_links);

	if (once) {
		printf("txload: --once, detaching\n");
		print_stats(skel);
		err = 0;
		goto out;
	}

	if (jpath) {
		jsonl = fopen(jpath, "w");
		if (!jsonl)
			fprintf(stderr, "txload: cannot write %s\n", jpath);
	}

	rb = ring_buffer__new(bpf_map__fd(skel->maps.tx_wal), on_record, NULL, NULL);
	if (!rb) {
		fprintf(stderr, "txload: ring buffer setup failed\n");
		err = 1;
		goto out;
	}

	signal(SIGINT, on_sig);
	signal(SIGTERM, on_sig);
	mkdir(TX_CTL_DIR, 0700);
	printf("txload: streaming the WAL. Ctrl-C to stop.\n");
	printf("        commit/abort control via %s/{flush,discard}-<txid>\n\n",
	       TX_CTL_DIR);

	while (!stop) {
		err = ring_buffer__poll(rb, 1000);
		if (err == -EINTR) { err = 0; break; }
		if (err < 0) { fprintf(stderr, "txload: poll: %d\n", err); break; }
		poll_control(skel);
		if (stats)
			print_stats(skel);
	}

	printf("\ntxload: %llu record(s)\n", n_records);
	print_stats(skel);
	err = 0;

out:
	if (jsonl) fclose(jsonl);
	for (i = 0; i < n_links; i++)
		bpf_link__destroy(tcx_links[i]);
	ring_buffer__free(rb);
	agenttx_bpf__destroy(skel);
	return err ? 1 : 0;
}
