// SPDX-License-Identifier: GPL-2.0
/*
 * tools/harness/txctl.c --- the userspace side of /dev/agenttx.
 *
 * Drives every ioctl in the contract, and is what the tests in tests/p1 exercise.
 *
 *   txctl abi
 *   txctl supervisor
 *   txctl begin  [--flags N] [--timeout MS]      -> prints the tx id
 *   txctl stat   [--tx ID] [--json]
 *   txctl commit [--tx ID] [--reason N]
 *   txctl abort  [--tx ID] [--reason N]
 *   txctl run    [--flags N] -- CMD [ARGS...]
 *
 * `run` is the interesting one.  PROPOSAL.md argues transactions should be
 * VERIFICATION-DELIMITED: a transaction spans exactly the interval over
 * which no evidence exists, and the moment evidence arrives -- the build
 * succeeded, the tests passed -- you decide.  `run` is that, literally:
 *
 *     begin  ->  fork/exec CMD  ->  wait  ->  exit 0 ? commit : abort
 *
 * and because the committing process is txctl rather than CMD, it is also
 * a working demonstration of the "who may commit" invariant: the child
 * cannot commit itself even if it is entirely under an attacker's control.
 *
 * Owner: P4 (harness), against P1's ABI.
 */

#define _GNU_SOURCE
#include <errno.h>
#include <sched.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <limits.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/wait.h>
#include <unistd.h>

#include "agenttx.h"

/* Must match the module's `tx_root` parameter (src/fs/mount.c). */
static const char *tx_root(void)
{
	const char *e = getenv("AGENTTX_ROOT");

	return (e && *e) ? e : "/var/lib/agenttx";
}

/*
 * Put the agent inside a per-transaction copy-on-write overlay, in ITS OWN
 * mount namespace.
 *
 * This is the userspace half of P2, and it is userspace for a concrete
 * reason rather than convenience: `do_add_mount()` is not exported to
 * modules, so no module can graft a mount into a namespace however much of
 * the fs_context API it is permitted to call. The kernel owns the part that
 * is actually transactional -- discard, merge, write-set -- and that is all
 * in src/fs/. This is the same split container runtimes use.
 *
 * The `lower` symlink is how the kernel learns which directory is under
 * protection: `struct tx_begin_arg` carries no path and adding one is a
 * contract-change PR needing four approvals.
 *
 * Called in the HOLDER, after tx_begin and before exec, so the namespace
 * and the transaction have the same lifetime.
 */
static int tx_setup_overlay(tx_id_t tx, const char *lower)
{
	char base[256], upper[320], work[320], merged[320], link[320];
	char opts[2048];
	char real[512];
	char *resolved;

	/*
	 * realpath(x, NULL) so the resolved length is discovered rather than
	 * assumed, then copied into a buffer whose bound the COMPILER can
	 * see. A runtime length check alone is invisible to
	 * -Wformat-truncation, and the warning is right to insist: a
	 * silently truncated `lowerdir=` would mount the overlay over the
	 * wrong directory and look like it worked.
	 */
	resolved = realpath(lower, NULL);
	if (!resolved) {
		fprintf(stderr, "txctl: %s: %s\n", lower, strerror(errno));
		return -1;
	}
	if (strlen(resolved) >= sizeof(real)) {
		fprintf(stderr, "txctl: lower path too long (%zu bytes)\n",
			strlen(resolved));
		free(resolved);
		return -1;
	}
	snprintf(real, sizeof(real), "%s", resolved);
	free(resolved);
	/*
	 * Bound the path before it reaches the options string. The overlay
	 * mount options are a single comma-separated buffer and the kernel
	 * caps it at one page; a silently truncated `lowerdir=` would mount
	 * an overlay over the WRONG directory, which is the worst available
	 * failure here -- it would look like it worked.
	 */
	snprintf(base,   sizeof(base),   "%s/tx-%llu", tx_root(), (unsigned long long)tx);
	snprintf(upper,  sizeof(upper),  "%s/upper",  base);
	snprintf(work,   sizeof(work),   "%s/work",   base);
	snprintf(merged, sizeof(merged), "%s/merged", base);
	snprintf(link,   sizeof(link),   "%s/lower",  base);

	/* The kernel created base/{upper,work,merged} during tx_fs_begin. */
	unlink(link);
	if (symlink(real, link) < 0) {
		fprintf(stderr, "txctl: symlink %s -> %s: %s\n",
			link, real, strerror(errno));
		return -1;
	}

	if (unshare(CLONE_NEWNS) < 0) {
		fprintf(stderr, "txctl: unshare(CLONE_NEWNS): %s\n", strerror(errno));
		return -1;
	}
	/*
	 * Make the new namespace's mounts private. Without this the overlay
	 * propagates back to the parent namespace and the "private" in
	 * "private mount namespace" is not true -- the isolation would look
	 * right in a demo and be absent in fact.
	 */
	if (mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) < 0) {
		fprintf(stderr, "txctl: make-rprivate: %s\n", strerror(errno));
		return -1;
	}

	snprintf(opts, sizeof(opts),
		 "lowerdir=%s,upperdir=%s,workdir=%s", real, upper, work);
	if (mount("overlay", merged, "overlay", 0, opts) < 0) {
		fprintf(stderr, "txctl: mount overlay on %s: %s\n",
			merged, strerror(errno));
		fprintf(stderr, "       opts: %s\n", opts);
		return -1;
	}

	/*
	 * Bind the merged view OVER the protected directory, so the agent
	 * sees its work at the path it expects. Everything it writes to
	 * `real` now lands in upper/ and the lower layer is untouched until
	 * commit merges it down.
	 */
	if (mount(merged, real, NULL, MS_BIND, NULL) < 0) {
		fprintf(stderr, "txctl: bind %s over %s: %s\n",
			merged, real, strerror(errno));
		return -1;
	}
	return 0;
}

static const char *const class_names[] = TX_CLASS_NAMES;
static const char *const state_names[] = TX_STATE_NAMES;

static int dev_open(void)
{
	int fd = open(AGENTTX_DEV_PATH, O_RDWR);

	if (fd < 0) {
		fprintf(stderr, "txctl: open %s: %s\n",
			AGENTTX_DEV_PATH, strerror(errno));
		if (errno == ENOENT)
			fprintf(stderr, "       is agenttx.ko loaded?  insmod agenttx.ko\n");
		if (errno == EACCES)
			fprintf(stderr, "       the node is mode 0600; run as root\n");
	}
	return fd;
}

static const char *name_of(const char *const *tbl, unsigned n, unsigned i)
{
	return i < n ? tbl[i] : "?";
}

/* ------------------------------------------------------------------ */

static int cmd_abi(int fd)
{
	__u32 v = 0;

	if (ioctl(fd, TX_IOC_ABI, &v) < 0) {
		perror("TX_IOC_ABI");
		return 1;
	}
	printf("module abi %u, header abi %u -- %s\n", v, AGENTTX_ABI_VERSION,
	       v == AGENTTX_ABI_VERSION ? "match" : "MISMATCH");
	return v == AGENTTX_ABI_VERSION ? 0 : 1;
}

static int cmd_supervisor(int fd)
{
	struct tx_supervisor_arg a;

	memset(&a, 0, sizeof(a));
	a.abi = AGENTTX_ABI_VERSION;
	if (ioctl(fd, TX_IOC_SUPERVISOR, &a) < 0) {
		fprintf(stderr, "txctl: TX_IOC_SUPERVISOR: %s\n", strerror(errno));
		if (errno == EPERM)
			fprintf(stderr, "       registering as supervisor needs CAP_SYS_ADMIN\n");
		return 1;
	}
	printf("supervisor registered (pid %d)\n", (int)getpid());
	return 0;
}

static int do_begin(int fd, __u32 flags, __u32 timeout_ms, tx_id_t *out)
{
	struct tx_begin_arg a;

	memset(&a, 0, sizeof(a));
	a.abi = AGENTTX_ABI_VERSION;
	a.flags = flags;
	a.timeout_ms = timeout_ms;
	if (ioctl(fd, TX_IOC_BEGIN, &a) < 0) {
		fprintf(stderr, "txctl: TX_IOC_BEGIN: %s\n", strerror(errno));
		return -1;
	}
	if (out)
		*out = a.tx_id;
	return 0;
}

static int do_end(int fd, int commit, tx_id_t tx, __u32 reason, int quiet)
{
	struct tx_end_arg a;

	memset(&a, 0, sizeof(a));
	a.abi = AGENTTX_ABI_VERSION;
	a.tx_id = tx;
	a.reason = reason;
	if (ioctl(fd, commit ? TX_IOC_COMMIT : TX_IOC_ABORT, &a) < 0) {
		fprintf(stderr, "txctl: TX_IOC_%s: %s\n",
			commit ? "COMMIT" : "ABORT", strerror(errno));
		if (errno == EPERM && commit)
			fprintf(stderr,
				"       only the supervisor may commit -- this is the\n"
				"       premature-commit invariant, working as designed\n");
		if (errno == EPERM && !commit)
			fprintf(stderr,
				"       the transaction is DOOMED: an irrevocable effect\n"
				"       was emitted, so abort is no longer available\n");
		return -1;
	}
	if (!quiet)
		printf("%s tx=%llu effects=%llu files=%llu\n",
		       commit ? "committed" : "aborted",
		       (unsigned long long)a.tx_id,
		       (unsigned long long)a.n_effects,
		       (unsigned long long)a.n_files);
	return 0;
}

static int cmd_stat(int fd, tx_id_t tx, int as_json)
{
	struct tx_stat_arg a;

	memset(&a, 0, sizeof(a));
	a.abi = AGENTTX_ABI_VERSION;
	a.tx_id = tx;
	if (ioctl(fd, TX_IOC_STAT, &a) < 0) {
		fprintf(stderr, "txctl: TX_IOC_STAT: %s\n", strerror(errno));
		return 1;
	}
	if (as_json) {
		printf("{\"tx_id\":%llu,\"state\":%u,\"state_name\":\"%s\","
		       "\"worst_class\":%u,\"worst_class_name\":\"%s\","
		       "\"n_deferred\":%llu,\"n_written\":%llu,"
		       "\"deadline_ns\":%lld,\"owner_pid\":%u}\n",
		       (unsigned long long)a.tx_id, a.state,
		       name_of(state_names, TX_STATE_MAX, a.state),
		       a.worst_class,
		       name_of(class_names, TX_CLASS_MAX, a.worst_class),
		       (unsigned long long)a.n_deferred,
		       (unsigned long long)a.n_written,
		       (long long)a.deadline_ns, a.owner_pid);
	} else {
		printf("tx_id       %llu\n", (unsigned long long)a.tx_id);
		printf("state       %s (%u)\n",
		       name_of(state_names, TX_STATE_MAX, a.state), a.state);
		printf("worst class %s (%u)\n",
		       name_of(class_names, TX_CLASS_MAX, a.worst_class), a.worst_class);
		printf("deferred    %llu\n", (unsigned long long)a.n_deferred);
		printf("written     %llu\n", (unsigned long long)a.n_written);
		printf("owner pid   %u\n", a.owner_pid);
	}
	return 0;
}

/*
 * Verification-delimited transaction, end to end.
 *
 * THE PROCESS SHAPE, and why it is three processes rather than two:
 *
 *     txctl            supervisor. Registers, decides, commits.
 *      └── holder      calls tx_begin. OWNS the transaction.
 *           └── CMD    the agent. Inside the transaction by inheritance.
 *
 * Two processes does not work, and finding out why is what tests/p1/t02
 * and t03 were for:
 *
 *  - If txctl calls tx_begin itself, txctl is inside its own transaction
 *    and the kernel refuses its commit -- correctly, because "nobody
 *    inside the transaction may commit it" is the whole invariant.
 *  - If CMD calls tx_begin, the transaction dies the moment CMD exits:
 *    P1-08 aborts a transaction whose owner died, and CMD must exit
 *    before its exit status can be used as the verification signal.
 *
 * So the owner has to be a process that is neither the decider nor the
 * agent, and that outlives the agent. The holder does nothing but hold.
 * It is four lines of code and it is the difference between the model
 * working and not.
 *
 * CMD is inside the transaction without asking: membership is inherited by
 * descendants (src/core/ctx.c), so every syscall CMD makes is covered.
 */
static int cmd_run(int fd, __u32 flags, __u32 timeout_ms,
		   const char *lower, char **argv)
{
	int to_holder[2], to_parent[2];
	tx_id_t tx = TX_ID_NONE;
	pid_t holder;
	int status = 0, verified = 0;
	char done = 1;

	if (pipe(to_holder) < 0 || pipe(to_parent) < 0) {
		perror("pipe");
		return 1;
	}

	/*
	 * Claim the supervisor role BEFORE the holder opens the transaction.
	 *
	 * Ordering matters: tx_begin records the registered supervisor onto
	 * the new transaction, so registering afterwards leaves the
	 * transaction with supervisor_pid == 0 and the kernel then refuses
	 * every commit with "no supervisor registered". That is fail-closed
	 * and correct -- an unsupervised transaction that anyone may commit
	 * is strictly worse than one nobody may -- but it means `txctl run`
	 * could never commit anything until it asked for the role.
	 * tests/p1/t04 found this.
	 */
	if (cmd_supervisor(fd) != 0) {
		fprintf(stderr, "txctl run: could not become supervisor; "
				"the commit will be refused\n");
		return 1;
	}

	holder = fork();
	if (holder < 0) {
		perror("fork");
		return 1;
	}

	if (holder == 0) {
		/* ---- the holder: owns the transaction, does nothing else ---- */
		pid_t kid;
		int st = 0;
		char go;

		close(to_holder[1]);
		close(to_parent[0]);

		if (do_begin(fd, flags, timeout_ms, &tx) < 0)
			_exit(70);
		if (write(to_parent[1], &tx, sizeof(tx)) != (ssize_t)sizeof(tx))
			_exit(71);

		if (lower && tx_setup_overlay(tx, lower) < 0)
			_exit(76);

		kid = fork();
		if (kid < 0)
			_exit(72);
		if (kid == 0) {
			char buf[32];

			/* So a nested txctl can name the transaction it is in. */
			snprintf(buf, sizeof(buf), "%llu", (unsigned long long)tx);
			setenv("AGENTTX_TX_ID", buf, 1);
			execvp(argv[0], argv);
			fprintf(stderr, "txctl: exec %s: %s\n", argv[0], strerror(errno));
			_exit(127);
		}
		if (waitpid(kid, &st, 0) < 0)
			_exit(73);
		if (write(to_parent[1], &st, sizeof(st)) != (ssize_t)sizeof(st))
			_exit(74);

		/*
		 * Stay alive until the supervisor has decided.  Exiting here
		 * would trip the process-death hook and abort the transaction
		 * out from under the commit that is about to happen.
		 */
		if (read(to_holder[0], &go, 1) != 1)
			_exit(75);
		_exit(0);
	}

	/* ---- the supervisor ---- */
	close(to_holder[0]);
	close(to_parent[1]);

	if (read(to_parent[0], &tx, sizeof(tx)) != (ssize_t)sizeof(tx)) {
		fprintf(stderr, "txctl: holder did not report a transaction id\n");
		return 1;
	}
	printf("tx=%llu BEGIN (holder pid %d) -> %s\n",
	       (unsigned long long)tx, (int)holder, argv[0]);
	fflush(stdout);

	if (read(to_parent[0], &status, sizeof(status)) != (ssize_t)sizeof(status)) {
		fprintf(stderr, "txctl: holder did not report an exit status\n");
		do_end(fd, 0, tx, TX_REASON_UNSPEC, 0);
		status = -1;
	} else if (WIFEXITED(status) && WEXITSTATUS(status) == 0) {
		printf("tx=%llu verification PASSED -> commit\n",
		       (unsigned long long)tx);
		verified = do_end(fd, 1, tx, TX_REASON_VERIFIED, 0) == 0;
	} else {
		if (WIFSIGNALED(status))
			printf("tx=%llu child killed by signal %d -> abort\n",
			       (unsigned long long)tx, WTERMSIG(status));
		else
			printf("tx=%llu verification FAILED (exit %d) -> abort\n",
			       (unsigned long long)tx, WEXITSTATUS(status));
		do_end(fd, 0, tx, TX_REASON_VERIFY_FAIL, 0);
	}

	/* Release the holder now that the transaction has been decided. */
	if (write(to_holder[1], &done, 1) != 1)
		perror("txctl: releasing the holder");
	close(to_holder[1]);
	waitpid(holder, NULL, 0);

	return verified ? 0 : 1;
}

static void usage(void)
{
	fputs(
"usage: txctl <command> [options]\n"
"  abi                                 compare module and header ABI\n"
"  supervisor                          claim the supervisor role (CAP_SYS_ADMIN)\n"
"  begin  [--flags N] [--timeout MS]   open a transaction, print its id\n"
"  stat   [--tx ID] [--json]           report state\n"
"  commit [--tx ID] [--reason N]       supervisor only\n"
"  abort  [--tx ID] [--reason N]\n"
"  run    [--flags N] [--lower DIR] -- CMD [ARGS]\n"
"                                      verification-delimited transaction;\n"
"                                      --lower puts CMD inside a CoW overlay\n",
	      stderr);
}

int main(int argc, char **argv)
{
	__u32 flags = TX_F_DEFER_NET | TX_F_COW_FS | TX_F_ESCALATE;
	const char *lower = NULL;
	__u32 timeout_ms = 0, reason = TX_REASON_UNSPEC;
	tx_id_t tx = TX_ID_NONE;
	int as_json = 0, i, fd, rc;
	const char *cmd;

	if (argc < 2) {
		usage();
		return 2;
	}
	cmd = argv[1];

	for (i = 2; i < argc; i++) {
		if (!strcmp(argv[i], "--flags") && i + 1 < argc)
			flags = (__u32)strtoul(argv[++i], NULL, 0);
		else if (!strcmp(argv[i], "--timeout") && i + 1 < argc)
			timeout_ms = (__u32)strtoul(argv[++i], NULL, 0);
		else if (!strcmp(argv[i], "--tx") && i + 1 < argc)
			tx = (tx_id_t)strtoull(argv[++i], NULL, 0);
		else if (!strcmp(argv[i], "--reason") && i + 1 < argc)
			reason = (__u32)strtoul(argv[++i], NULL, 0);
		else if (!strcmp(argv[i], "--lower") && i + 1 < argc)
			lower = argv[++i];
		else if (!strcmp(argv[i], "--json"))
			as_json = 1;
		else if (!strcmp(argv[i], "--"))
			break;
		else {
			fprintf(stderr, "txctl: unknown option %s\n", argv[i]);
			return 2;
		}
	}

	fd = dev_open();
	if (fd < 0)
		return 1;

	if (!strcmp(cmd, "abi")) {
		rc = cmd_abi(fd);
	} else if (!strcmp(cmd, "supervisor")) {
		rc = cmd_supervisor(fd);
	} else if (!strcmp(cmd, "begin")) {
		rc = do_begin(fd, flags, timeout_ms, &tx) < 0 ? 1 : 0;
		if (!rc)
			printf("%llu\n", (unsigned long long)tx);
	} else if (!strcmp(cmd, "stat")) {
		rc = cmd_stat(fd, tx, as_json);
	} else if (!strcmp(cmd, "commit")) {
		rc = do_end(fd, 1, tx, reason, 0) < 0 ? 1 : 0;
	} else if (!strcmp(cmd, "abort")) {
		rc = do_end(fd, 0, tx, reason, 0) < 0 ? 1 : 0;
	} else if (!strcmp(cmd, "run")) {
		if (i >= argc || strcmp(argv[i], "--")) {
			fprintf(stderr, "txctl run: need -- CMD\n");
			rc = 2;
		} else {
			rc = cmd_run(fd, flags, timeout_ms, lower, &argv[i + 1]);
		}
	} else {
		usage();
		rc = 2;
	}

	close(fd);
	return rc;
}
