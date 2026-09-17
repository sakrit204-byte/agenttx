#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tools/bench/bench_core.sh --- fragment P1-14.
#
# What a transaction costs to open, inspect and close, against a syscall that
# does nothing. getpid() is the baseline because it is the cheapest real
# syscall: it measures the user/kernel boundary and essentially nothing else,
# so (tx_begin - getpid) is AgentTx's work rather than Linux's.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$REPO/tools/bench/lib_bench.sh"

STREAM="p1"
FRAGMENT="P1-14"
WARMUP="${WARMUP:-300}"
ITERS="${ITERS:-3000}"
BIN="${TXBENCH:-$REPO/tools/bench/txbench}"

[ -x "$BIN" ] || { say "bench_core: no txbench (make txbench)"; exit 1; }
[ -c /dev/agenttx ] || { say "bench_core: /dev/agenttx absent"; exit 1; }
require_perf_kernel

header
fail=0
for op in getpid stat begin cycle; do
	say "  measuring $op (warmup $WARMUP, iters $ITERS)"
	if ! out=$("$BIN" --op "$op" --iters $((WARMUP + ITERS)) 2>/dev/null \
	           | drop_warmup "$WARMUP" | stats); then
		say "  $op: measurement failed"
		fail=1
		continue
	fi
	read -r n mean sd p50 p99 <<<"$out"
	case "$op" in
	getpid) metric="baseline_syscall";  note="getpid(), no AgentTx involvement" ;;
	stat)   metric="tx_stat_latency";   note="ioctl round trip, no state change" ;;
	begin)  metric="tx_begin_latency";  note="ctx alloc + hash insert + provider begin" ;;
	cycle)  metric="tx_begin_abort_cycle"; note="the whole lifecycle" ;;
	esac
	row "$metric" "ns" "enforcing" "$n" "$mean" "$sd" \
	    "p50=$p50 p99=$p99 warmup=$WARMUP $note$NOTES_SUFFIX"
done
say "done"
exit "$fail"
