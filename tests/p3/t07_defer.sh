#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p3/t07_defer.sh --- fragment P3-08.  THE HEADLINE CONTRIBUTION.
#
# The claim: inside a transaction, a fire-and-forget send is held back. The
# agent's sendto() returns success. Nothing reaches the peer. On abort it
# never existed; on commit it is replayed (P3-09).
#
# WHY THERE IS A CONTROL CASE, and why it runs FIRST.
#
# "The listener received nothing" passes if deferral works. It ALSO passes
# if the network is broken, if the listener never started, if the port was
# wrong, or if UDP was blocked for some unrelated reason. A test that cannot
# distinguish "suppressed" from "never worked" is the exact failure
# docs/STATUS.md section 6 is about.
#
# So the control runs first, OUTSIDE any transaction, and must RECEIVE the
# packet. Only then does "inside a transaction, nothing arrives" mean
# anything at all.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_bpflsm

PORT="${PORT:-19999}"
RECV=/tmp/agenttx-udp-recv

echo "t07_defer: hold back a fire-and-forget send (P3-08)"

command -v python3 >/dev/null || { echo "  (no python3 in the guest)"; exit 77; }

# A UDP listener that appends whatever it receives.
listener() {
	rm -f "$RECV"
	python3 - "$PORT" "$RECV" <<'PY' &
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("127.0.0.1", int(sys.argv[1])))
s.settimeout(12)
with open(sys.argv[2], "a") as fh:
    while True:
        try:
            d, _ = s.recvfrom(2048)
        except socket.timeout:
            break
        fh.write(d.decode(errors="replace") + "\n")
        fh.flush()
PY
	LPID=$!
	sleep 1
}

send_udp() {   # send_udp <text>
	python3 - "$PORT" "$1" <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
n = s.sendto(sys.argv[2].encode(), ("127.0.0.1", int(sys.argv[1])))
print("SENT_BYTES=%d" % n)
PY
}

# ---------------------------------------------------------------
# CONTROL: no transaction, no hooks. The packet MUST arrive.
# ---------------------------------------------------------------
listener
out=$(send_udp CONTROL-PACKET 2>&1)
sleep 1
if grep -q 'SENT_BYTES=14' <<<"$out"; then
	ok "control: sendto() reports success" "14 bytes"
else
	bad "control: sendto() reports success" "$out"
fi
if grep -q 'CONTROL-PACKET' "$RECV" 2>/dev/null; then
	ok "control: the packet ARRIVES" "the path works when nothing suppresses it"
else
	bad "control: the packet ARRIVES" "nothing received -- the rest of this test would be meaningless"
	kill $LPID 2>/dev/null; finish
fi
kill $LPID 2>/dev/null; wait $LPID 2>/dev/null

# ---------------------------------------------------------------
# THE TEST: same send, inside a transaction, with the hooks up.
# ---------------------------------------------------------------
# 127/8 classifies as REVERSIBLE by the built-in rule (loopback is not an
# outbound effect), so the destination port is forced DEFERRABLE via the
# operator override -- which is also what exercises P3-06's rule table.
listener
wal=$(mktemp)
rm -rf "$LOWER"; mkdir -p "$LOWER"
( setsid "$TXLOAD" --defer-port "$PORT" >"$wal" 2>&1 & )
sleep 3

if ! grep -q 'egress suppression attached' "$wal"; then
	bad "egress program attached" "$(tail -3 "$wal" | tr '\n' ' ')"
	pkill -INT -f "$TXLOAD"; kill $LPID 2>/dev/null; finish
fi
ok "egress program attached" "tcx/egress is live"

send_out=$("$TXCTL" run --lower "$LOWER" -- bash -c "
$(declare -f send_udp)
PORT=$PORT
send_udp DEFERRED-PACKET
false" 2>&1)
sleep 2
pkill -INT -f "$TXLOAD" 2>/dev/null; sleep 2
kill $LPID 2>/dev/null; wait $LPID 2>/dev/null

# --- 1. userspace was told it succeeded ------------------------------
if grep -q 'SENT_BYTES=15' <<<"$send_out"; then
	ok "the agent's sendto() reported SUCCESS" "15 bytes, no error"
else
	bad "the agent's sendto() reported SUCCESS" "${send_out//$'\n'/ | }"
fi

# --- 2. and nothing reached the peer ---------------------------------
if grep -q 'DEFERRED-PACKET' "$RECV" 2>/dev/null; then
	bad "the packet did NOT reach the peer" "it arrived -- deferral did not happen"
else
	ok "the packet did NOT reach the peer" "held back while the control arrived"
fi

# --- 3. the WAL says so ----------------------------------------------
grep -qE "socket_sendmsg +deferrable +deferred" "$wal" \
	&& ok "WAL records it as deferred" "class=deferrable verdict=deferred" \
	|| bad "WAL records it as deferred" "$(grep socket_sendmsg "$wal" | tail -2 | tr '\n' ' ')"

# --- 4. the counters agree -------------------------------------------
defr=$(sed -n 's/.*DEFERRED (held back) *\([0-9]*\).*/\1/p'        "$wal" | tail -1)
supp=$(sed -n 's/.*SUPPRESSED (packets dropped) *\([0-9]*\).*/\1/p' "$wal" | tail -1)
[[ -n "$defr" ]] && (( defr > 0 )) \
	&& ok "LSM hook deferred the flow" "$defr send(s)" \
	|| bad "LSM hook deferred the flow" "counter '$defr'"
[[ -n "$supp" ]] && (( supp > 0 )) \
	&& ok "egress actually dropped packets" "$supp packet(s) never left" \
	|| bad "egress actually dropped packets" "counter '$supp' -- the flow was recorded but not suppressed"

# --- 5. and the transaction aborted, so it never existed -------------
grep -q 'aborted tx=' <<<"$send_out" \
	&& ok "transaction aborted" "the deferred send is discarded, not replayed" \
	|| bad "transaction aborted" "${send_out//$'\n'/ | }"

rm -f "$wal"
finish
