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
# THE CONFOUND, AND HOW IT IS REMOVED.
#
# A bpf_link is refcounted by the fd holding it, so the hooks used to require
# txload to stay running -- which meant the hooks_on arm also measured txload
# draining a ring buffer. It showed up as +108ns on getpid(), a syscall with
# NO LSM hook at all, and it contaminated every other row by the same amount.
#
# `txload --pin` pins the links into TX_PIN_DIR and exits. The hooks stay
# attached with nothing running, so both arms differ by exactly one thing:
# whether the programs are attached.
hooks_down() {
	"$LOADER" --unpin >/dev/null 2>&1
	pkill -INT -f "$LOADER" 2>/dev/null
	sleep 2
}
hooks_up() {
	mount | grep -q "bpf on /sys/fs/bpf" || mount -t bpf bpf /sys/fs/bpf 2>/dev/null
	"$LOADER" --pin --once >/tmp/bench-txload.log 2>&1
	sleep 1
}

header
fail=0

# INTERLEAVED ROUNDS, not "all of one arm then all of the other".
#
# Measuring every hooks_off sample and then every hooks_on sample lets any
# drift over the run -- thermal, host scheduling, page cache -- appear as a
# difference between arms. It did: with the links pinned so nothing was
# running in either arm, getpid() (which has NO LSM hook) came out 103ns
# FASTER with hooks attached. That cannot be causal. The noise between arms
# was the same order as the ~130ns effect being measured.
#
# So alternate off/on/off/on and take the MEDIAN of the per-round deltas.
# Drift affects both arms of a round nearly equally and cancels; a real effect
# survives. The getpid row is still reported, and it is the honest error bar:
# whatever it shows is what this rig cannot resolve.
ROUNDS="${ROUNDS:-7}"
declare -A SUM
declare -A DELTAS

for round in $(seq 1 "$ROUNDS"); do
	say "  round $round/$ROUNDS"
	for arm in hooks_off hooks_on; do
		if [ "$arm" = hooks_off ]; then hooks_down; else hooks_up; fi
		if [ "$arm" = hooks_on ] && ! grep -q 'pinned' /tmp/bench-txload.log 2>/dev/null; then
			say "  txload did not pin; aborting rather than reporting a fake arm"
			fail=1; break 2
		fi
		for op in getpid openat sendto; do
			case "$op" in
			openat) out=$(measure_arm openat --path "$PROBE") ;;
			sendto) out=$(measure_arm sendto --host 127.0.0.1 --port 9) ;;
			*)      out=$(measure_arm "$op") ;;
			esac
			[ -n "$out" ] || { fail=1; continue; }
			read -r n mean sd p50 p99 <<<"$out"
			SUM["$arm:$op:p50"]="${SUM["$arm:$op:p50"]:-} $p50"
			SUM["$arm:$op:mean"]="${SUM["$arm:$op:mean"]:-} $mean"
			SUM["$arm:$op:n"]="$n"
		done
	done
	for op in getpid openat sendto; do
		a=$(echo "${SUM["hooks_off:$op:p50"]}" | awk '{print $NF}')
		b=$(echo "${SUM["hooks_on:$op:p50"]}"  | awk '{print $NF}')
		[ -n "$a" ] && [ -n "$b" ] && DELTAS["$op"]="${DELTAS["$op"]:-} $((b - a))"
	done
done
hooks_down

median() { tr ' ' '\n' <<<"$1" | grep -E '^-?[0-9]+$' | sort -n | awk '{a[NR]=$1} END{ if(NR==0) exit 1; print a[int((NR+1)/2)] }'; }

for op in getpid openat sendto; do
	case "$op" in
	getpid) m="unhooked_syscall"
	        note="getpid() has NO LSM hook. This delta is the NOISE FLOOR of the rig -- the error bar on every other row." ;;
	openat) m="file_open_syscall"; note="lsm/file_open fires, process not transacting" ;;
	sendto) m="socket_sendmsg_syscall"; note="lsm/socket_sendmsg fires, process not transacting" ;;
	esac
	for arm in hooks_off hooks_on; do
		vals="${SUM["$arm:$op:p50"]:-}"
		[ -n "$vals" ] || continue
		med=$(median "$vals") || continue
		spread=$(tr ' ' '\n' <<<"$vals" | grep -E '^[0-9]+$' | sort -n \
		         | awk 'NR==1{lo=$1} {hi=$1} END{print hi-lo}')
		row "$m" "ns" "$arm" "${SUM["$arm:$op:n"]:-0}" "$med" "$spread" \
		    "median of $ROUNDS interleaved rounds; spread=range across rounds; $note$NOTES_SUFFIX"
	done
	d=$(median "${DELTAS["$op"]:-}") || continue
	row "${m}_overhead" "ns" "hooks_on_minus_off" "$ROUNDS" "$d" "0" \
	    "median per-round delta, interleaved; read against unhooked_syscall_overhead$NOTES_SUFFIX"
done

say "done"
exit "$fail"
