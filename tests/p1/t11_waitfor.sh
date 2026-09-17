#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p1/t11_waitfor.sh --- fragments P1-15 .. P1-18, in the kernel.
#
# docs/deadlock.md modelled all of this in Python. This tests the real thing:
# a wait-for graph in agenttx.ko, cycle detection inside the ioctl, and
# victim selection that REFUSES to pick a DOOMED transaction.
#
# The assertion that matters is the last group. A transaction which emitted an
# irrevocable effect cannot be aborted -- DOOMED -> ABORTING does not exist in
# include/agenttx.h -- so it cannot be a deadlock victim, and a cycle whose
# members are all doomed cannot be broken by the mechanism that breaks every
# other cycle. If the kernel ever "resolves" one of those, it resolved it by
# doing something the state machine forbids.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_dev; need_root

TXCTL="${TXCTL:-/usr/local/bin/txctl}"
[ -x "$TXCTL" ] || TXCTL="$REPO/tools/harness/txctl"
TXLOAD="${TXLOAD:-/usr/local/bin/txload}"

echo "t11_waitfor: wait-for graph, detection and recovery (P1-15..P1-18)"

# Start from a clean graph. A cycle left behind by an earlier run -- in
# particular an UNRESOLVABLE one, which by definition never goes away -- is
# still in the kernel's edge list, and stale edges made this test report
# EDEADLK for edges that were perfectly fine.
pkill -f "txctl session" 2>/dev/null; sleep 1
rm -rf /run/agenttx/session-* 2>/dev/null

# two held transactions; `txctl session` parks each at awaiting-decision
start_two() {   # start_two <prefix> <command>
	rm -rf /run/agenttx/session-* 2>/dev/null
	id agent >/dev/null 2>&1 || useradd -m -s /bin/bash agent
	for d in A B; do
		rm -rf "/tmp/$1$d"; mkdir -p "/tmp/$1$d"; chown agent "/tmp/$1$d"
		( setsid "$TXCTL" session --lower "/tmp/$1$d" --as agent \
		  -- sh -c "$2" >/dev/null 2>&1 & )
		sleep 3
	done
	TXA=$(cat /run/agenttx/session-*/tx 2>/dev/null | head -1)
	TXB=$(cat /run/agenttx/session-*/tx 2>/dev/null | tail -1)
}
finish_all() {
	for d in /run/agenttx/session-*; do
		[ -d "$d" ] && printf abort > "$d/decide" 2>/dev/null
	done
	sleep 3
	# Belt and braces. A parked session holds /dev/agenttx open, which pins
	# the module -- so a leftover one makes the NEXT run's rmmod fail and
	# the next run then silently tests the old module.
	pkill -f "txctl session" 2>/dev/null
	sleep 1
}

# --- 1. the ABI moved, and it must say so ----------------------------
out=$("$TXCTL" abi 2>&1)
grep -q 'abi 2' <<<"$out" \
	&& ok "contract is at abi 2" "the wait-for graph is a contract change" \
	|| bad "contract is at abi 2" "$out"

# --- 2. a plain edge, no cycle ---------------------------------------
dmesg -C 2>/dev/null
start_two wf 'echo x > f.txt'
if [[ -z "${TXA:-}" || -z "${TXB:-}" || "$TXA" == "$TXB" ]]; then
	bad "two live transactions" "got '$TXA' and '$TXB'"; finish_all; finish
fi
ok "two live transactions" "tx=$TXA and tx=$TXB"

out=$("$TXCTL" wait --tx "$TXA" --holder "$TXB" --kind output 2>&1)
grep -q 'wait registered' <<<"$out" \
	&& ok "an edge that closes no cycle is accepted" "" \
	|| bad "an edge that closes no cycle is accepted" "$out"

# --- 3. a self-edge is always a bug ----------------------------------
if "$TXCTL" wait --tx "$TXA" --holder "$TXA" >/dev/null 2>&1; then
	bad "self-edge is rejected" "it was accepted"
else
	ok "self-edge is rejected" "EINVAL"
fi

# --- 4. closing the cycle is detected IN THE IOCTL -------------------
out=$("$TXCTL" wait --tx "$TXB" --holder "$TXA" --kind escalate 2>&1); rc=$?
grep -q 'deadlock detected and broken' <<<"$out" \
	&& ok "cycle detected when the edge closes it" "not on a timer" \
	|| bad "cycle detected when the edge closes it" "rc=$rc $out"

sleep 3
new=$(dmesg)
grep -q 'DEADLOCK: 2 transactions in a cycle' <<<"$new" \
	&& ok "kernel reported the cycle" "$(grep -o 'cycle \[.*\]' <<<"$new" | head -1)" \
	|| bad "kernel reported the cycle" "no report in dmesg"

grep -qE 'victim tx=[0-9]+ \(policy=least-severe' <<<"$new" \
	&& ok "a victim was chosen by policy" "$(grep -oE 'victim tx=[0-9]+.*abortable[^)]*' <<<"$new" | head -1)" \
	|| bad "a victim was chosen by policy" ""

grep -q 'ABORT reason=8' <<<"$new" \
	&& ok "victim aborted with TX_REASON_DEADLOCK" "reason=8" \
	|| bad "victim aborted with TX_REASON_DEADLOCK" "$(grep -o 'ABORT reason=[0-9]*' <<<"$new" | head -1)"

# The abort must be REAL: the CoW layer is discarded, not just relabelled.
grep -qE 'ABORT discarded [0-9]+ upper-layer' <<<"$new" \
	&& ok "the victim's writes were actually discarded" "CoW layer dropped" \
	|| bad "the victim's writes were actually discarded" "no discard logged"

grep -qE 'BUG:|KASAN|WARNING:' <<<"$new" \
	&& bad "no memory error during detection" "$(grep -E 'BUG:|WARNING:' <<<"$new" | head -1)" \
	|| ok "no memory error during detection" ""
finish_all

# --- 5. THE FINDING: a cycle of DOOMED transactions cannot be broken -
if [[ -x "$TXLOAD" ]] && grep -q bpf /sys/kernel/security/lsm 2>/dev/null; then
	mount | grep -q "bpf on /sys/fs/bpf" || mount -t bpf bpf /sys/fs/bpf 2>/dev/null
	MODEL="$REPO/data/model/model_tree_kernel.bin"
	[ -f "$MODEL" ] && "$TXLOAD" --pin --once --model "$MODEL" >/dev/null 2>&1 \
	                || "$TXLOAD" --pin --once >/dev/null 2>&1
	dmesg -C 2>/dev/null

	# An outbound send to an external address classifies irrevocable, and
	# tx_note_class() then dooms the transaction. That is the only way to
	# reach DOOMED -- it cannot be forced from userspace, by design.
	start_two dm 'python3 -c "
import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.sendto(b\"x\",(\"8.8.8.8\",1234))"'

	da=$("$TXCTL" stat --tx "$TXA" --json 2>/dev/null | grep -c '"state_name":"doomed"')
	db=$("$TXCTL" stat --tx "$TXB" --json 2>/dev/null | grep -c '"state_name":"doomed"')
	if [[ "$da" == "1" && "$db" == "1" ]]; then
		ok "an irrevocable effect DOOMED both transactions" "via the in-kernel classifier"

		"$TXCTL" wait --tx "$TXA" --holder "$TXB" --kind output >/dev/null 2>&1
		out=$("$TXCTL" wait --tx "$TXB" --holder "$TXA" --kind escalate 2>&1); rc=$?

		[[ "$rc" == "3" ]] \
			&& ok "unresolvable cycle returns EDEADLK" "userspace is told it cannot be broken" \
			|| bad "unresolvable cycle returns EDEADLK" "rc=$rc"

		grep -q 'CANNOT be broken' <<<"$out" \
			&& ok "and says why" "every member is DOOMED" \
			|| bad "and says why" "$out"

		sleep 2
		new=$(dmesg)
		grep -q 'DEADLOCK IS UNRESOLVABLE' <<<"$new" \
			&& ok "kernel reports rather than hangs" "$(grep -o 'UNRESOLVABLE.*' <<<"$new" | head -1)" \
			|| bad "kernel reports rather than hangs" ""

		# THE INVARIANT. No DOOMED transaction may be aborted, ever.
		grep -qE 'victim tx=' <<<"$new" \
			&& bad "no DOOMED transaction was victimised" "a victim was chosen from an all-doomed cycle" \
			|| ok "no DOOMED transaction was victimised" "abort is unavailable and stayed unavailable"
		grep -q 'reason=8' <<<"$new" \
			&& bad "no deadlock abort happened" "something was aborted" \
			|| ok "no deadlock abort happened" "nothing could be"
	else
		skip "all-DOOMED cycle" "could not doom both transactions (doomed: $da/$db)"
	fi
	finish_all
	"$TXLOAD" --unpin >/dev/null 2>&1
else
	skip "all-DOOMED cycle" "needs txload and bpf in the LSM list"
fi

# --- 6. a finished transaction leaves no edges behind ----------------
if "$TXCTL" wait --tx 999998 --holder 999999 >/dev/null 2>&1; then
	bad "edges on dead transactions are rejected" "accepted an edge between nonexistent txs"
else
	ok "edges on dead transactions are rejected" "or harmlessly dropped"
fi

finish
