// SPDX-License-Identifier: GPL-2.0
/*
 * tools/bench/txbench.c --- the measurement engine.
 *
 * Prints ONE LATENCY IN NANOSECONDS PER LINE on stdout, nothing else. The
 * bench_*.sh scripts do the statistics and emit the CSV; this only measures.
 *
 *   txbench --op begin      --iters N     ioctl(TX_IOC_BEGIN) round trip
 *   txbench --op stat       --iters N     ioctl(TX_IOC_STAT)  round trip
 *   txbench --op cycle      --iters N     begin+abort, the whole lifecycle
 *   txbench --op getpid     --iters N     a syscall with no AgentTx hook
 *   txbench --op openat --path P          file_open: hooked
 *   txbench --op sendto --host H --port P socket_sendmsg: hooked, classified
 *   txbench --op write  --path P --size N write() into whatever is mounted
 *
 * WHY A SEPARATE BINARY. A shell loop cannot resolve a syscall: the fork and
 * the `date` cost orders of magnitude more than the thing being measured. The
 * first version of this measured bash.
 *
 * WHAT IT DOES NOT DO. It does not decide what is comparable. Measuring
 * `getpid` here and `sendto` there and subtracting is the caller's mistake to
 * make; the scripts measure every arm in one run for exactly that reason.
 */
#define _GNU_SOURCE
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <netinet/in.h>
#include <time.h>
#include <unistd.h>

#include "agenttx.h"

static inline unsigned long long now_ns(void)
{
	struct timespec t;

	clock_gettime(CLOCK_MONOTONIC, &t);
	return (unsigned long long)t.tv_sec * 1000000000ULL + t.tv_nsec;
}

static int dev = -1;

static int op_begin(tx_id_t *out)
{
	struct tx_begin_arg a;

	memset(&a, 0, sizeof(a));
	a.abi = AGENTTX_ABI_VERSION;
	a.flags = TX_F_DEFER_NET | TX_F_COW_FS;
	if (ioctl(dev, TX_IOC_BEGIN, &a) < 0)
		return -1;
	if (out)
		*out = a.tx_id;
	return 0;
}

static int op_abort(tx_id_t tx)
{
	struct tx_end_arg a;

	memset(&a, 0, sizeof(a));
	a.abi = AGENTTX_ABI_VERSION;
	a.tx_id = tx;
	a.reason = TX_REASON_UNSPEC;
	return ioctl(dev, TX_IOC_ABORT, &a) < 0 ? -1 : 0;
}

static int op_stat(void)
{
	struct tx_stat_arg a;

	memset(&a, 0, sizeof(a));
	a.abi = AGENTTX_ABI_VERSION;
	return ioctl(dev, TX_IOC_STAT, &a) < 0 ? -1 : 0;
}

int main(int argc, char **argv)
{
	const char *op = NULL, *path = "/tmp/txbench.tmp", *host = "127.0.0.1";
	int iters = 1000, port = 9, size = 4096, i, need_dev = 0;
	char *buf = NULL;
	int sock = -1;
	struct sockaddr_in to;

	for (i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--op") && i + 1 < argc)         op = argv[++i];
		else if (!strcmp(argv[i], "--iters") && i + 1 < argc) iters = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--path") && i + 1 < argc)  path = argv[++i];
		else if (!strcmp(argv[i], "--host") && i + 1 < argc)  host = argv[++i];
		else if (!strcmp(argv[i], "--port") && i + 1 < argc)  port = atoi(argv[++i]);
		else if (!strcmp(argv[i], "--size") && i + 1 < argc)  size = atoi(argv[++i]);
		else { fprintf(stderr, "txbench: bad arg %s\n", argv[i]); return 2; }
	}
	if (!op) { fprintf(stderr, "txbench: --op is required\n"); return 2; }

	need_dev = !strcmp(op, "begin") || !strcmp(op, "stat") || !strcmp(op, "cycle");
	if (need_dev) {
		dev = open(AGENTTX_DEV_PATH, O_RDWR);
		if (dev < 0) {
			fprintf(stderr, "txbench: %s: %s\n", AGENTTX_DEV_PATH,
				strerror(errno));
			return 1;
		}
	}

	if (!strcmp(op, "sendto")) {
		sock = socket(AF_INET, SOCK_DGRAM, 0);
		if (sock < 0) { perror("socket"); return 1; }
		memset(&to, 0, sizeof(to));
		to.sin_family = AF_INET;
		to.sin_port = htons(port);
		inet_pton(AF_INET, host, &to.sin_addr);
	}
	if (!strcmp(op, "write")) {
		buf = calloc(1, size);
		if (!buf) return 1;
	}

	for (i = 0; i < iters; i++) {
		unsigned long long t0, t1;
		tx_id_t tx = TX_ID_NONE;
		int rc = 0;

		if (!strcmp(op, "begin")) {
			t0 = now_ns(); rc = op_begin(&tx); t1 = now_ns();
			if (!rc) op_abort(tx);          /* outside the timed region */
		} else if (!strcmp(op, "stat")) {
			t0 = now_ns(); rc = op_stat(); t1 = now_ns();
		} else if (!strcmp(op, "cycle")) {
			t0 = now_ns();
			rc = op_begin(&tx);
			if (!rc) rc = op_abort(tx);
			t1 = now_ns();
		} else if (!strcmp(op, "getpid")) {
			t0 = now_ns(); (void)getpid(); t1 = now_ns();
		} else if (!strcmp(op, "openat")) {
			int fd;
			t0 = now_ns(); fd = open(path, O_RDONLY); t1 = now_ns();
			if (fd < 0) rc = -1; else close(fd);
		} else if (!strcmp(op, "sendto")) {
			t0 = now_ns();
			rc = sendto(sock, "x", 1, 0, (struct sockaddr *)&to,
				    sizeof(to)) < 0 ? -1 : 0;
			t1 = now_ns();
		} else if (!strcmp(op, "write")) {
			int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, 0600);
			if (fd < 0) { rc = -1; t0 = t1 = now_ns(); }
			else {
				t0 = now_ns();
				rc = write(fd, buf, size) < 0 ? -1 : 0;
				t1 = now_ns();
				close(fd);
			}
		} else {
			fprintf(stderr, "txbench: unknown op %s\n", op);
			return 2;
		}

		if (rc) {
			/*
			 * A failed operation is not a slow one. Emitting its
			 * timing would silently mix "the ioctl was refused" into
			 * a latency distribution, and the mean would move for a
			 * reason nobody could see.
			 */
			fprintf(stderr, "txbench: %s failed at iter %d: %s\n",
				op, i, strerror(errno));
			return 1;
		}
		printf("%llu\n", t1 - t0);
	}

	if (sock >= 0) close(sock);
	if (dev >= 0) close(dev);
	free(buf);
	return 0;
}
