#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p1/t06_kill.sh --- fragment P1-08, the agent died mid-transaction.
#
# The tracker calls this the hardest correctness bug in the stream. The
# thing it is really testing is that the cleanup path does not use-after-
# free: the tracepoint fires in atomic context, queues work carrying a
# tx_id (never a pointer), and the worker re-resolves it. If that were a
# pointer this test would panic a KASAN kernel, which is precisely why it
# should be run on one.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_dev; need_root; build_txctl

echo "t06_kill: process death mid-transaction (P1-08)"

before=$(dmesg | wc -l)

# --- SIGKILL a process holding an ACTIVE transaction -----------------
# The child opens a transaction and then blocks forever. We kill -9 it, so
# it never runs an exit handler and never calls abort: the ONLY thing that
# can clean up is the kernel.
bash -c "$TXCTL begin >/tmp/killtx.id 2>/dev/null; exec sleep 300" &
child=$!
sleep 1
tx=$(cat /tmp/killtx.id 2>/dev/null)

if [[ "$tx" =~ ^[0-9]+$ ]] && (( tx > 0 )); then
	ok "child opened a transaction" "tx=$tx"
else
	bad "child opened a transaction" "got '$tx'"; kill -9 $child 2>/dev/null; finish
fi

kill -9 $child 2>/dev/null
wait $child 2>/dev/null || true
sleep 2                       # let the workqueue drain

new=$(dmesg | tail -n +$((before + 1)))

if grep -q "tx=$tx: owner tgid=.* died while ACTIVE" <<<"$new"; then
	ok "kernel noticed the death" "sched_process_exit fired"
else
	bad "kernel noticed the death" "no death message for tx=$tx"
fi

if grep -qE "tx=$tx ABORT reason=7" <<<"$new"; then
	ok "transaction auto-aborted" "reason=7 (TX_REASON_PROC_DEATH)"
else
	bad "transaction auto-aborted" "$(grep "tx=$tx" <<<"$new" | head -3)"
fi

# The context must be gone, not merely marked.
if "$TXCTL" stat --tx "$tx" >/dev/null 2>&1; then
	bad "context was reclaimed" "tx=$tx is still resolvable"
else
	ok "context was reclaimed" "ENOENT"
fi

# --- the thing that would actually be a bug --------------------------
if grep -qE 'BUG:|KASAN|use-after-free|WARNING:|Oops' <<<"$new"; then
	bad "no memory error" "$(grep -E 'BUG:|KASAN|WARNING:' <<<"$new" | head -3)"
else
	ok "no memory error" "no KASAN/BUG/WARNING during cleanup"
fi

# --- ten at once: the workqueue must not lose any --------------------
before=$(dmesg | wc -l)
pids=()
for i in $(seq 1 10); do
	bash -c "$TXCTL begin >/dev/null 2>&1; exec sleep 300" &
	pids+=($!)
done
sleep 2
for p in "${pids[@]}"; do kill -9 "$p" 2>/dev/null; done
wait 2>/dev/null || true
sleep 3

new=$(dmesg | tail -n +$((before + 1)))
n=$(grep -c 'died while ACTIVE' <<<"$new" || true)
if (( n == 10 )); then
	ok "10 simultaneous deaths" "all 10 cleaned up"
elif (( n > 0 )); then
	bad "10 simultaneous deaths" "only $n of 10 were cleaned up"
else
	bad "10 simultaneous deaths" "none were cleaned up"
fi

if grep -qE 'BUG:|KASAN|use-after-free' <<<"$new"; then
	bad "no memory error under load" "$(grep -E 'BUG:|KASAN' <<<"$new" | head -3)"
else
	ok "no memory error under load" "clean"
fi

finish
