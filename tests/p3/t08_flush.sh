#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p3/t08_flush.sh --- fragment P3-09, flush on commit / discard on abort.
#
# THE CLAIM, in one sentence: the same send arrives if and only if the
# transaction commits.
#
# Both halves are asserted against the SAME code path, because either one
# alone is worthless:
#
#   abort  -> never arrives   passes trivially if suppression is broken-on
#   commit -> arrives         passes trivially if suppression never worked
#
# Only the pair distinguishes "deferral" from "we broke the network" and from
# "we did nothing at all". t07 establishes that the path works when nothing
# suppresses it; this one establishes that the transaction outcome is what
# decides.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_bpflsm

PORT="${PORT:-19997}"
RECV=/tmp/agenttx-flush-recv
CTL=/run/agenttx

echo "t08_flush: the send arrives iff the transaction commits (P3-09)"
command -v python3 >/dev/null || { echo "  (no python3)"; exit 77; }

listener() {
	rm -f "$RECV"
	python3 - "$PORT" "$RECV" <<'PY' &
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("127.0.0.1", int(sys.argv[1])))
s.settimeout(25)
with open(sys.argv[2], "a") as fh:
    while True:
        try:
            d, _ = s.recvfrom(2048)
        except socket.timeout:
            break
        fh.write(d.decode(errors="replace") + "\n"); fh.flush()
PY
	LPID=$!; sleep 1
}

# run_tx <verdict-command> <marker>
run_tx() {
	"$TXCTL" run --lower "$LOWER" -- python3 -c "
import socket,sys
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.sendto(b'$2', ('127.0.0.1', $PORT))
sys.exit($1)" 2>&1
}

mkdir -p "$CTL"; rm -f "$CTL"/* 2>/dev/null
rm -rf "$LOWER"; mkdir -p "$LOWER"
listener
wal=$(mktemp)
( setsid "$TXLOAD" --defer-port "$PORT" >"$wal" 2>&1 & )
sleep 3

grep -q 'egress suppression' "$wal" \
	&& ok "suppression is live" "" \
	|| { bad "suppression is live" "$(tail -2 "$wal" | tr '\n' ' ')"; \
	     pkill -INT -f "$TXLOAD"; kill $LPID 2>/dev/null; finish; }

# ---------------------------------------------------------------
# 1. ABORT -- the effect must never exist
# ---------------------------------------------------------------
out=$(run_tx 1 ABORTED-EFFECT)
sleep 2

grep -q 'aborted tx=' <<<"$out" \
	&& ok "abort: transaction aborted" "" \
	|| bad "abort: transaction aborted" "${out//$'\n'/ | }"

grep -q 'discarded' <<<"$out" \
	&& ok "abort: effects discarded" "txload acknowledged the discard" \
	|| bad "abort: effects discarded" "${out//$'\n'/ | }"

if grep -q 'ABORTED-EFFECT' "$RECV" 2>/dev/null; then
	bad "abort: the peer NEVER receives it" "it arrived -- abort emitted an effect"
else
	ok "abort: the peer NEVER receives it" "the send never happened"
fi

# ---------------------------------------------------------------
# 2. COMMIT -- the same effect must arrive, and only now
# ---------------------------------------------------------------
out=$(run_tx 0 COMMITTED-EFFECT)
sleep 3

grep -q 'committed tx=' <<<"$out" \
	&& ok "commit: transaction committed" "" \
	|| bad "commit: transaction committed" "${out//$'\n'/ | }"

grep -q 'replayed' <<<"$out" \
	&& ok "commit: effects replayed" "txload acknowledged the flush" \
	|| bad "commit: effects replayed" "${out//$'\n'/ | }"

if grep -q 'COMMITTED-EFFECT' "$RECV" 2>/dev/null; then
	ok "commit: the peer DOES receive it" "delivered on commit, not before"
else
	bad "commit: the peer DOES receive it" "nothing arrived -- the replay did not happen"
fi

# --- and the aborted one still never showed up -----------------------
# Checked again at the end: a replay that flushed the wrong transaction's
# records would show up here and nowhere else.
if grep -q 'ABORTED-EFFECT' "$RECV" 2>/dev/null; then
	bad "the aborted effect stayed discarded" "it appeared during the commit flush"
else
	ok "the aborted effect stayed discarded" "flush replayed only its own tx"
fi

pkill -INT -f "$TXLOAD" 2>/dev/null; sleep 2
kill $LPID 2>/dev/null; wait $LPID 2>/dev/null

grep -qE 'FLUSH -- .* replayed' "$wal" \
	&& ok "txload logged the flush" "$(grep -oE 'tx=[0-9]+ FLUSH[^,]*, [0-9]+ of [0-9]+ effect\(s\) replayed' "$wal" | tail -1)" \
	|| bad "txload logged the flush" "$(grep -E 'FLUSH|DISCARD' "$wal" | tail -2 | tr '\n' ' ')"

rm -f "$wal"
finish
