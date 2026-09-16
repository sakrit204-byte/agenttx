#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p1/t04_state.sh --- fragment P1-06, the state machine.
#
# WHAT THIS TEST CAN AND CANNOT REACH, stated up front because a reader who
# assumes otherwise will think the test is weaker than it is.
#
# From userspace, with the STUB classifier (which always answers
# TX_REVERSIBLE) and no BPF hooks loaded, the only reachable states are
#
#     NONE -> ACTIVE -> ABORTING -> ABORTED
#     NONE -> ACTIVE -> COMMITTING -> DONE
#
# DOOMED is NOT reachable: it requires tx_note_class(TX_IRREVOCABLE), which
# only a real classifier in a real hook calls.  That is P3's test to write,
# and tests/p3/ is where it belongs.
#
# But the edge that carries the semantics -- the ABSENCE of
# DOOMED -> ABORTING -- is still checked here, indirectly and soundly:
# tx_state_selfcheck() runs at module_init, asserts exactly that, and
# REFUSES TO LOAD if it is ever reachable.  So "the module loaded" is a
# proof that the irrevocability rule holds.  We assert on the evidence
# rather than pretending to drive a state we cannot reach.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_dev; build_txctl

echo "t04_state: transitions and illegal edges (P1-06)"

"$TXCTL" abort >/dev/null 2>&1 || true

# --- the module is loaded, therefore the selfcheck passed ------------
if [[ -c /dev/agenttx ]]; then
	ok "state selfcheck passed" "module_init refuses to load if DOOMED->ABORTING is reachable"
else
	bad "state selfcheck passed" "no device node"
fi

# --- NONE -> ACTIVE ---------------------------------------------------
st=$("$TXCTL" stat --json 2>/dev/null)
grep -q '"state_name":"none"' <<<"$st" \
	&& ok "starts in NONE" "" || bad "starts in NONE" "$st"

out=$(in_tx "$TXCTL"' stat --json')
grep -q '"state_name":"active"' <<<"$out" \
	&& ok "NONE -> ACTIVE on BEGIN" "" \
	|| bad "NONE -> ACTIVE on BEGIN" "${out//$'\n'/ | }"

# --- worst_class starts at the bottom of the taxonomy ----------------
# The watermark is a max(), so it must start at the least severe class or
# every subsequent max() is wrong.
grep -q '"worst_class":0' <<<"$out" \
	&& ok "worst_class starts reversible" "0" \
	|| bad "worst_class starts reversible" "${out//$'\n'/ | }"

# --- ACTIVE -> COMMITTING -> DONE (verification passed) --------------
out=$(in_tx 'true')
grep -q 'committed tx=' <<<"$out" \
	&& ok "ACTIVE -> DONE on verified commit" "" \
	|| bad "ACTIVE -> DONE on verified commit" "${out//$'\n'/ | }"

# --- ACTIVE -> ABORTING -> ABORTED (verification failed) -------------
out=$(in_tx 'false')
grep -q 'aborted tx=' <<<"$out" \
	&& ok "ACTIVE -> ABORTED on failed verification" "" \
	|| bad "ACTIVE -> ABORTED on failed verification" "${out//$'\n'/ | }"

# --- a terminal state is terminal ------------------------------------
tx=$(sed -n 's/.*aborted tx=\([0-9]*\).*/\1/p' <<<"$out" | head -1)
if [[ -n "$tx" ]]; then
	if "$TXCTL" abort --tx "$tx" >/dev/null 2>&1; then
		bad "double ABORT is rejected" "the second abort succeeded"
	else
		ok "double ABORT is rejected" "ENOENT -- the context is gone"
	fi
	if "$TXCTL" commit --tx "$tx" >/dev/null 2>&1; then
		bad "COMMIT after ABORT is rejected" "it succeeded"
	else
		ok "COMMIT after ABORT is rejected" "ENOENT"
	fi
fi

# --- no illegal transition was ever attempted ------------------------
# tx_state_set() WARNs on an illegal edge.  A WARNING in dmesg naming a
# transition means the code tried something the machine forbids -- a bug in
# us, not bad input, which is why it is a WARN and why this test looks for it.
if dmesg | grep -q 'illegal transition'; then
	bad "no illegal transition attempted" "$(dmesg | grep 'illegal transition' | tail -2)"
else
	ok "no illegal transition attempted" "no WARN from tx_state_set()"
fi

skip "DOOMED -> COMMITTING" "needs a real classifier; belongs to tests/p3"

finish
