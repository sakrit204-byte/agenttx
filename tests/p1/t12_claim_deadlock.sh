#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p1/t12_claim_deadlock.sh --- three transactions, one cycle.
#
# Everything in the data path is deadlock-free by construction: agents
# work in private copy-on-write layers, never wait for each other's data,
# and conflicts are settled at commit (docs/deadlock.md §2.2). No number
# of agents editing files can produce a cycle, and for a long time nothing
# in this repo could produce one outside a unit test.
#
# Exclusive claims are what change that. Some effects cannot be done
# speculatively by two parties and reconciled afterwards -- a deploy slot,
# an outbound send -- so they need exclusive access, which means waiting,
# which means a wait-for graph, which means cycles. §2.3 calls this the
# deadlock AgentTx can actually suffer.
#
# This builds one deliberately: three transactions, three resources, each
# taking them in a different order. No model involved -- the cycle is a
# property of the ordering, and a language model would only make it
# non-deterministic.

set -uo pipefail
[[ -c /dev/agenttx ]] || { echo "needs the guest"; exit 77; }
[[ -r /sys/kernel/debug/agenttx/transactions ]] || {
	echo "needs debugfs"; exit 77; }
TXCTL=/usr/local/bin/txctl
[[ -x $TXCTL ]] || { echo "txctl not deployed"; exit 77; }

fail=0
ok()  { echo "PASS: $1"; }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

D=$(mktemp -d)
cleanup() {
	for s in /run/agenttx/session-*; do
		[[ -d $s ]] && printf abort > "$s/decide" 2>/dev/null
	done
	sleep 1
	for p in $(pgrep -x txctl 2>/dev/null); do kill -9 "$p" 2>/dev/null; done
	rm -rf "$D" "$D".claims
	exit $fail
}
trap cleanup EXIT

mkdir -p "$D"/a "$D"/b "$D"/c
dmesg -C >/dev/null 2>&1 || true

# Three held transactions. `sleep` keeps each one open and holding its
# layer, which is what a blocked agent looks like from the kernel's side.
for n in a b c; do
	( cd "$D/$n" && setsid "$TXCTL" session --lower "$D/$n" -- sleep 300 \
		> "$D/$n.log" 2>&1 & )
done
for _ in $(seq 1 60); do
	# grep -h across the files, then count lines. The obvious
	# `grep -hc ... | paste -sd+ | bc` needs bc, which this guest does
	# not have, and every iteration printed a "command not found".
	n=$(grep -h 'session started' "$D"/*.log 2>/dev/null | grep -c .)
	[[ "${n:-0}" -ge 3 ]] && break
	sleep 0.5
done
read -r TA TB TC <<<"$(sed -n 's/^tx=\([0-9]*\) session started.*/\1/p' \
	"$D"/a.log "$D"/b.log "$D"/c.log | paste -sd' ')"
[[ -n "${TC:-}" ]] || { echo "FAIL: could not start three transactions"; exit 1; }
echo "  transactions: $TA $TB $TC"

# --- the cycle, one edge at a time ---------------------------------------
"$TXCTL" wait --tx "$TA" --holder "$TB" --kind data >/dev/null 2>&1 \
	&& ok "edge 1 accepted ($TA waits on $TB)" \
	|| bad "edge 1 refused"
"$TXCTL" wait --tx "$TB" --holder "$TC" --kind data >/dev/null 2>&1 \
	&& ok "edge 2 accepted ($TB waits on $TC)" \
	|| bad "edge 2 refused"

# Nothing should have fired yet: a chain is not a cycle.
if dmesg | grep -qi 'DEADLOCK'; then
	bad "a deadlock was reported before the cycle was closed"
else
	ok "a chain of waits is not a deadlock"
fi

out=$("$TXCTL" wait --tx "$TC" --holder "$TA" --kind data 2>&1)
if grep -qi deadlock <<<"$out"; then
	ok "closing the cycle is reported as a deadlock"
else
	bad "the cycle-closing edge said: ${out:-(nothing)}"
fi

# --- the kernel broke it -------------------------------------------------
for _ in $(seq 1 40); do
	dmesg | grep -qi 'aborting as a deadlock victim' && break
	sleep 0.25
done
victim=$(dmesg | sed -n 's/.*victim tx=\([0-9]*\).*/\1/p' | tail -1)
if [[ -n "$victim" ]]; then
	ok "the kernel chose a victim (tx=$victim)"
else
	bad "no victim was chosen"
fi
dmesg | grep -q 'DEADLOCK: 3 transactions in a cycle' \
	&& ok "it saw all three transactions in the cycle" \
	|| bad "the cycle length was not reported as 3"

# The victim is gone; the survivors are not.
sleep 1
live=$(tail -n +2 /sys/kernel/debug/agenttx/transactions | awk '{print $1}')
if grep -qx "$victim" <<<"$live"; then
	bad "the victim is still open"
else
	ok "the victim's transaction is gone"
fi
# Count OUR transactions only. Counting every live transaction makes the
# test fail because of somebody else's leftover session, which is a true
# statement about the machine and says nothing about deadlock recovery.
mine=0
for t in "$TA" "$TB" "$TC"; do
	grep -qx "$t" <<<"$live" && mine=$((mine + 1))
done
[[ "$mine" -eq 2 ]] \
	&& ok "the other two of our transactions survived" \
	|| bad "expected 2 of our 3 to survive, found $mine"

# Aborted for the right reason, and nothing of its work survived.
dmesg | grep -q "tx=$victim ABORT reason=8" \
	&& ok "the victim was aborted with reason=DEADLOCK (8)" \
	|| bad "the victim was aborted for the wrong reason"

# --- and the graph no longer contains the cycle --------------------------
# OUR edges only. The graph is shared, and tests/p1/t11_waitfor.sh runs
# before this one and leaves edges of its own behind -- counting all of
# them made this fail for a reason that has nothing to do with the cycle
# it just broke. Same mistake as the survivor count above.
ours=0
while read -r w h _rest; do
	for t in "$TA" "$TB" "$TC"; do
		[[ "$w" == "$t" ]] && for u in "$TA" "$TB" "$TC"; do
			[[ "$h" == "$u" ]] && ours=$((ours + 1))
		done
	done
done < <(tail -n +2 /sys/kernel/debug/agenttx/waitfor)
[[ "$ours" -lt 3 ]] \
	&& ok "our cycle is no longer in the graph ($ours of our edges left)" \
	|| bad "all three of our edges are still there"
