#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tools/bench/bench_hooks.sh --- fragment P3-12.
#
# THE NUMBER THAT MATTERS is not what a hooked syscall costs inside a
# transaction. It is what the hooks cost EVERY OTHER PROCESS ON THE MACHINE,
# which is almost all of them: the LSM hooks fire system-wide and return
# immediately when bpf_tx_current_id() says 0.
#
# So both arms are measured OUTSIDE any transaction, with the programs
# detached and then attached. That difference is the tax AgentTx levies on a
# machine that is not using it, and if it is not small the mechanism is not
# deployable however well the rest works.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$REPO/tools/bench/lib_bench.sh"

STREAM="p3"
FRAGMENT="P3-12"
WARMUP="${WARMUP:-300}"
ITERS="${ITERS:-3000}"
BIN="${TXBENCH:-$REPO/tools/bench/txbench}"
LOADER="${TXLOAD:-/usr/local/bin/txload}"
PROBE=/tmp/txbench-probe

[ -x "$BIN" ] || { say "bench_hooks: no txbench"; exit 1; }
[ -x "$LOADER" ] || { say "bench_hooks: no txload at $LOADER"; exit 1; }
require_perf_kernel
echo probe > "$PROBE"

measure_arm() {   # measure_arm <op> [extra args...]
	local op=$1; shift
	"$BIN" --op "$op" --iters $((WARMUP + ITERS)) "$@" 2>/dev/null \
		| drop_warmup "$WARMUP" | stats
}

hooks_down() { pkill -INT -f "$LOADER" 2>/dev/null; sleep 2; }
hooks_up()   { ( setsid "$LOADER" >/tmp/bench-txload.log 2>&1 & ); sleep 3; }

header
fail=0

# Both arms in ONE run. Comparing an arm captured before a reboot against one
# captured after measures the reboot.
for arm in hooks_off hooks_on; do
	if [ "$arm" = hooks_off ]; then hooks_down; else hooks_up; fi
	if [ "$arm" = hooks_on ] && ! grep -q 'hooks' /tmp/bench-txload.log 2>/dev/null; then
		say "  txload did not attach; aborting rather than reporting a fake arm"
		fail=1
		break
	fi
	for op in getpid openat sendto; do
		say "  $arm / $op"
		case "$op" in
		openat) out=$(measure_arm openat --path "$PROBE") ;;
		sendto) out=$(measure_arm sendto --host 127.0.0.1 --port 9) ;;
		*)      out=$(measure_arm "$op") ;;
		esac
		if [ -z "$out" ]; then say "  $arm/$op failed"; fail=1; continue; fi
		read -r n mean sd p50 p99 <<<"$out"
		case "$op" in
		getpid) m="unhooked_syscall"; note="getpid(), no LSM hook at all" ;;
		openat) m="file_open_syscall"; note="lsm/file_open fires, not transacting" ;;
		sendto) m="socket_sendmsg_syscall"; note="lsm/socket_sendmsg fires, not transacting" ;;
		esac
		row "$m" "ns" "$arm" "$n" "$mean" "$sd" \
		    "p50=$p50 p99=$p99 warmup=$WARMUP $note$NOTES_SUFFIX"
	done
done
hooks_down
say "done"
exit "$fail"
