#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p4/t08_deadlock.sh --- the concurrency model (docs/deadlock.md).
#
# Tier 0: pure userspace, no kernel, runs anywhere.
#
# The assertion that matters is the LAST one. A DOOMED transaction cannot be
# aborted -- include/agenttx.h has no DOOMED -> ABORTING edge, so the state
# is unrepresentable rather than merely refused. If victim selection ever
# picks a DOOMED transaction, the recovery path is asking the state machine
# for something that does not exist, and the deadlock story collapses. That
# is this file's version of t05's premature-commit test: everything else
# here is plumbing, that one is the invariant.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
PY="${PY:-python3}"
DL="tools/harness/deadlock.py"

fails=0; n=0
if [[ -t 1 ]]; then R=$'\033[31m'; G=$'\033[32m'; N=$'\033[0m'; else R=; G=; N=; fi
ok()  { n=$((n+1)); printf '  %sPASS%s  %-44s %s\n' "$G" "$N" "$1" "${2-}"; }
bad() { n=$((n+1)); fails=$((fails+1)); printf '  %sFAIL%s  %-44s %s\n' "$R" "$N" "$1" "${2-}"; }

echo "t08_deadlock: wait-for graph, detection and recovery (docs/deadlock.md)"

command -v "$PY" >/dev/null || { echo "  (no python3)"; exit 77; }
[[ -f "$DL" ]] || { echo "  (no $DL)"; exit 77; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

gen() { "$PY" "$DL" --scenario "$1" --policy "${2:-least-severe}" \
        --jsonl "$TMP/$1.jsonl" --no-colour >/dev/null 2>&1; }

for s in classic subagents doomed unresolvable; do
	gen "$s" || { bad "generate $s" "simulator exited nonzero"; continue; }
done

ev() { "$PY" - "$1" "$2" <<'PY'
import json,sys
kinds=[json.loads(l)["kind"] for l in open(sys.argv[1])]
print(kinds.count(sys.argv[2]))
PY
}

# --- every scenario forms exactly one cycle --------------------------
for s in classic subagents doomed unresolvable; do
	c=$(ev "$TMP/$s.jsonl" deadlock)
	[[ "$c" == "1" ]] && ok "$s detects a cycle" "1 deadlock event" \
	                  || bad "$s detects a cycle" "got $c"
done

# --- the resolvable ones are resolved --------------------------------
for s in classic subagents doomed; do
	v=$(ev "$TMP/$s.jsonl" victim); a=$(ev "$TMP/$s.jsonl" abort)
	if [[ "$v" == "1" && "$a" == "1" ]]; then
		ok "$s resolves by abort" "1 victim, 1 abort"
	else
		bad "$s resolves by abort" "victim=$v abort=$a"
	fi
done

# --- subagents: the cycle must run through an ESCALATE edge ----------
# If it does not, the scenario has stopped modelling what it claims to.
if "$PY" - "$TMP/subagents.jsonl" <<'PY'
import json,sys
for l in open(sys.argv[1]):
    e=json.loads(l)
    if e["kind"]=="deadlock" and "ESCALATE" in e["edge_kinds"]:
        sys.exit(0)
sys.exit(1)
PY
then ok "subagents cycle uses an ESCALATE edge" "the shape a real agent system makes"
else bad "subagents cycle uses an ESCALATE edge" "no ESCALATE in the cycle"; fi

# --- doomed: victim choice is forced, not chosen ---------------------
if grep -q '"forced":true' "$TMP/doomed.jsonl"; then
	ok "doomed forces the victim" "policy had no choice"
else
	bad "doomed forces the victim" "forced flag absent"
fi

# --- unresolvable: reported, never victimised ------------------------
u=$(ev "$TMP/unresolvable.jsonl" unresolvable)
v=$(ev "$TMP/unresolvable.jsonl" victim)
[[ "$u" == "1" ]] && ok "all-DOOMED cycle is reported" "1 unresolvable event" \
                  || bad "all-DOOMED cycle is reported" "got $u"
[[ "$v" == "0" ]] && ok "all-DOOMED cycle picks no victim" "abort cannot break it" \
                  || bad "all-DOOMED cycle picks no victim" "it picked $v"

# --- THE INVARIANT ---------------------------------------------------
# Across every scenario AND every policy, a DOOMED transaction is never the
# victim. Checked by replaying the event stream and tracking who is doomed
# at the moment the victim is chosen, rather than by trusting a flag the
# simulator sets about itself.
viol=0
for s in classic subagents doomed unresolvable; do
	for p in youngest least-work least-severe random; do
		"$PY" "$DL" --scenario "$s" --policy "$p" --jsonl "$TMP/x.jsonl" \
			--no-colour >/dev/null 2>&1 || continue
		if ! "$PY" - "$TMP/x.jsonl" <<'PY'
import json,sys
doomed=set()
for l in open(sys.argv[1]):
    e=json.loads(l)
    if e["kind"]=="doomed":
        doomed.add(e["tx"])
    if e["kind"]=="victim" and e["tx"] in doomed:
        print("VICTIMISED A DOOMED TX:", e["tx"]); sys.exit(1)
sys.exit(0)
PY
		then
			bad "no DOOMED tx is ever victimised" "$s/$p"
			viol=1
		fi
	done
done
(( viol == 0 )) && ok "no DOOMED tx is ever victimised" "4 scenarios x 4 policies"

# --- provenance: nothing here may look like a measurement ------------
tot=$(wc -l < "$TMP/classic.jsonl")
sim=$(grep -c '"sim":true' "$TMP/classic.jsonl" || true)
[[ "$tot" == "$sim" ]] && ok "every event is marked sim:true" "$sim of $tot" \
                       || bad "every event is marked sim:true" "$sim of $tot"

echo
if (( fails == 0 )); then echo "t08: PASS ($n assertions)"; else echo "t08: FAIL ($fails of $n)"; fi
exit "$fails"
