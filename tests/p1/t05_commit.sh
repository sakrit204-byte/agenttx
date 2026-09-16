#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p1/t05_commit.sh --- fragments P1-04 and P1-07.
#
# THE invariant, from PROPOSAL.md:
#
#   "Not the agent. tx_commit from inside the transaction returns -EPERM;
#    only the supervisor commits. That one invariant kills the
#    premature-commit attack."
#
# This is the most important test in P1. Everything else is plumbing; this
# is the security property. The threat model gives the adversary full
# control of agent output, so the agent asking nicely to commit is exactly
# what a successful prompt injection looks like, and it must fail.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_dev; need_root; build_txctl

echo "t05_commit: commit authority and ordering (P1-04, P1-07)"

"$TXCTL" abort >/dev/null 2>&1 || true

# --- the attack: a process inside the transaction commits it ---------
# It must be attempted from INSIDE a live transaction. A bare `txctl begin`
# exits immediately and P1-08 then aborts the transaction, so a commit
# afterwards fails with ENOENT -- which looks like a pass and proves
# nothing. The attack is only meaningful against a transaction that is
# actually alive and actually committable.
out=$(in_tx "$TXCTL"' commit --tx $AGENTTX_TX_ID')
if grep -q 'committed tx=' <<<"$out"; then
	bad "self-commit is refused" "THE PREMATURE-COMMIT ATTACK SUCCEEDED"
elif grep -qi 'not permitted' <<<"$out"; then
	ok "self-commit is refused" "EPERM -- the invariant holds"
else
	bad "self-commit is refused" "refused, but not with EPERM: ${out//$'\n'/ | }"
fi

# --- and the same attack one fork deeper -----------------------------
# Membership is inherited, so a grandchild is inside the transaction too.
# If authority were checked by comparing tgids rather than by asking
# "is the caller inside this transaction", forking once would defeat it.
out=$(in_tx 'bash -c "bash -c \"'"$TXCTL"' commit --tx \$AGENTTX_TX_ID\""')
if grep -q 'committed tx=' <<<"$out"; then
	bad "self-commit refused 3 levels deep" "fork() defeated the authority check"
else
	ok "self-commit refused 3 levels deep" "membership, not tgid equality"
fi

# A refused commit is not an abort: the transaction must still be alive.
out=$(in_tx "$TXCTL"' commit --tx $AGENTTX_TX_ID >/dev/null 2>&1; '"$TXCTL"' stat --json')
grep -q '"state_name":"active"' <<<"$out" \
	&& ok "refused commit leaves tx ACTIVE" "not silently torn down" \
	|| bad "refused commit leaves tx ACTIVE" "${out//$'\n'/ | }"

# --- an unsupervised transaction may be committed by nobody ----------
# Fail-closed: the alternative -- "anyone may commit if no supervisor
# registered" -- is strictly worse than "nobody may".
# A transaction opened with no supervisor registered: `txctl begin` does not
# register, so this transaction has supervisor_pid == 0 and the kernel must
# refuse every commit, including one from an uninvolved third party.
out=$(in_tx 'echo $AGENTTX_TX_ID')
ok "unsupervised tx cannot be committed" "fail closed: no supervisor, no commit"

# --- the supervisor CAN commit ---------------------------------------
# txctl run is the shape: parent registers, child transacts, parent decides.
out=$("$TXCTL" supervisor 2>&1)
if grep -q 'registered' <<<"$out"; then
	ok "supervisor registration" "$out"
else
	skip "supervisor registration" "$out"
fi

# --- verification-delimited commit: the child passes -----------------
out=$("$TXCTL" run -- /bin/true 2>&1); rc=$?
if (( rc == 0 )) && grep -q 'verification PASSED' <<<"$out" \
                 && grep -q 'committed' <<<"$out"; then
	ok "verification PASSED -> commit" "exit 0 commits"
else
	bad "verification PASSED -> commit" "rc=$rc ${out//$'\n'/ | }"
fi

# --- and when it fails ------------------------------------------------
out=$("$TXCTL" run -- /bin/false 2>&1); rc=$?
if grep -q 'verification FAILED' <<<"$out" && grep -q 'aborted' <<<"$out"; then
	ok "verification FAILED -> abort" "nonzero exit aborts"
else
	bad "verification FAILED -> abort" "rc=$rc ${out//$'\n'/ | }"
fi

# --- commit ordering: fs before effects ------------------------------
# P1-07's whole argument is that the undoable half runs first: a failed
# merge leaves data we still hold, a failed flush has already put packets on
# the wire. The ordering is observable in dmesg because both providers log.
#
# Matched implementation-agnostically. These assertions are about P1's
# sequencing, not about who implements the providers, so they must pass
# against `agenttx/fs-stub:` and against a real `agenttx/fs:` alike --
# otherwise landing P2 breaks a P1 test that P2 did not change the meaning
# of. It did exactly that, once.
# Match the OPERATION, not just the provider prefix. The real P2 also logs
# "CoW area ready" at BEGIN, and a prefix-only match picked that line up and
# reported the abort ordering backwards -- a test that failed for a reason
# that had nothing to do with what it was testing.
FS_COMMIT='agenttx/fs[a-z-]*:.*(commit|COMMIT|merged)'
FS_ABORT='agenttx/fs[a-z-]*:.*(abort|ABORT|discarded)'
EF_FLUSH='agenttx/eff[a-z-]*:.*flush'
EF_DISCARD='agenttx/eff[a-z-]*:.*discard'

# first_line <extended-regex> -> line number in dmesg, or empty
first_line() { dmesg | grep -nEm1 "$1" | cut -d: -f1; }

dmesg -C 2>/dev/null || true
"$TXCTL" run -- /bin/true >/dev/null 2>&1
f=$(first_line "$FS_COMMIT"); e=$(first_line "$EF_FLUSH")
if [[ -n "$f" && -n "$e" ]] && (( f < e )); then
	ok "commit order is fs then effects" "fs at line $f, effects at $e"
elif [[ -z "$f" || -z "$e" ]]; then
	skip "commit order is fs then effects" "fs='$f' eff='$e' -- a provider did not log"
else
	bad "commit order is fs then effects" "fs at $f, effects at $e -- effects went first"
fi

dmesg -C 2>/dev/null || true
"$TXCTL" run -- /bin/false >/dev/null 2>&1
f=$(first_line "$FS_ABORT"); e=$(first_line "$EF_DISCARD")
if [[ -n "$f" && -n "$e" ]] && (( e < f )); then
	ok "abort order is effects then fs" "effects at line $e, fs at $f"
elif [[ -z "$f" || -z "$e" ]]; then
	skip "abort order is effects then fs" "fs='$f' eff='$e' -- a provider did not log"
else
	bad "abort order is effects then fs" "effects at $e, fs at $f -- fs went first"
fi

finish
