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
#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

#include "agenttx.h"
#include "agenttx.skel.h"

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
		"egress program ran", "egress saw AF_INET"
	};
	int fd = bpf_map__fd(skel->maps.tx_stats);
	__u32 k;
	__u64 v;

	printf("\n  counters\n");
	for (k = 0; k < 8; k++) {
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
	int once = 0, stats = 0, err, i;
	const char *jpath = NULL;
	int defer_ports[16], n_defer = 0;

	for (i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--once"))        once = 1;
		else if (!strcmp(argv[i], "--stats"))  stats = 1;
		else if (!strcmp(argv[i], "--jsonl") && i + 1 < argc) jpath = argv[++i];

		else if (!strcmp(argv[i], "--defer-port") && i + 1 < argc &&
			 n_defer < 16) defer_ports[n_defer++] = atoi(argv[++i]);
		else {
			fprintf(stderr,
				"usage: txload [--once] [--stats] [--jsonl F]\n"
				"              [--defer-port N]...\n");
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
	printf("txload: streaming the WAL. Ctrl-C to stop.\n\n");

	while (!stop) {
		err = ring_buffer__poll(rb, 1000);
		if (err == -EINTR) { err = 0; break; }
		if (err < 0) { fprintf(stderr, "txload: poll: %d\n", err); break; }
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
