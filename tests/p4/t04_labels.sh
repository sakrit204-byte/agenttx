#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p4/t04_labels.sh --- fragment P4-05, the taxonomy and its labels.
#
# Tier 0.  What this can and cannot establish:
#
#   CAN    that docs/taxonomy.md section 2 is executable, deterministic, and
#          applies first-match-wins; that the agreement machinery computes
#          Cohen's kappa correctly; and that two of the four classes are
#          structurally unreachable on traces captured without the mechanism.
#
#   CANNOT that the labels are correct. That needs two humans, and the
#          tracker note for P4-05 says so: "Two people label independently;
#          report agreement." A kappa between a human and the rule labeller
#          measures how well the DOCUMENT is written, not how reliable the
#          labels are.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
PY="${PY:-python3}"
L=tools/harness/label.py

fails=0; n=0
if [[ -t 1 ]]; then R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'; else R=; G=; Y=; N=; fi
ok()   { n=$((n+1)); printf '  %sPASS%s  %-48s %s\n' "$G" "$N" "$1" "${2-}"; }
bad()  { n=$((n+1)); fails=$((fails+1)); printf '  %sFAIL%s  %-48s %s\n' "$R" "$N" "$1" "${2-}"; }
skip() { printf '  %sSKIP%s  %-48s %s\n' "$Y" "$N" "$1" "${2-}"; }

echo "t04_labels: the taxonomy is a decision procedure (P4-05)"
[[ -f docs/taxonomy.md ]] || { echo "  (no docs/taxonomy.md)"; exit 77; }
ls data/gate/*.jsonl >/dev/null 2>&1 || { echo "  (no trace records)"; exit 77; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

# --- 1. the document runs -------------------------------------------
"$PY" "$L" --in 'data/gate/*.jsonl' --rules --out "$TMP/a.csv" >"$TMP/a.out" 2>&1 \
	&& ok "taxonomy section 2 is executable" "$(grep -oE 'labelled [0-9]+ record' "$TMP/a.out")" \
	|| { bad "taxonomy section 2 is executable" "$(tail -2 "$TMP/a.out")"; exit "$fails"; }

# --- 2. it is deterministic -----------------------------------------
# A decision procedure that gives different answers on the same input is not
# a decision procedure.
"$PY" "$L" --in 'data/gate/*.jsonl' --rules --out "$TMP/b.csv" >/dev/null 2>&1
if cmp -s "$TMP/a.csv" "$TMP/b.csv"; then
	ok "labelling is deterministic" "byte-identical across runs"
else
	bad "labelling is deterministic" "two runs differ"
fi

# --- 3. every label is in the frozen enum ---------------------------
badlab=$(awk -F, 'NR>1 && $9!="" && $9!~/^(reversible|deferrable|compensable|irrevocable)$/{print $9}' "$TMP/a.csv" | sort -u | head -3)
[[ -z "$badlab" ]] && ok "every label is in enum tx_class" "" \
                   || bad "every label is in enum tx_class" "$badlab"

# --- 4. every record names the rule that decided it -----------------
# The point of an executable document is that a disagreement can be traced to
# a clause. A label with no rule is a label nobody can argue with.
norule=$(awk -F, 'NR>1 && $11==""' "$TMP/a.csv" | wc -l)
[[ "$norule" == "0" ]] && ok "every label cites the clause that decided it" "" \
                       || bad "every label cites the clause that decided it" "$norule without one"

# --- 5. the structural consequence, asserted rather than assumed ----
# taxonomy.md section 5: `deferrable` is a property of the MECHANISM, not of
# the effect. These traces were captured with strace, with no mechanism
# running, so NOTHING was held and `deferrable` must be 0. And the registry
# is empty, so `compensable` must be 0. If either is non-zero the rule
# labeller is inventing labels the document does not license.
defn=$(awk -F, 'NR>1 && $9=="deferrable"' "$TMP/a.csv" | wc -l)
comn=$(awk -F, 'NR>1 && $9=="compensable"' "$TMP/a.csv" | wc -l)
[[ "$defn" == "0" ]] && ok "no 'deferrable' without the mechanism" "taxonomy section 5" \
                     || bad "no 'deferrable' without the mechanism" "$defn labelled deferrable"
[[ "$comn" == "0" ]] && ok "no 'compensable' with an empty registry" "taxonomy section 2, Q5" \
                     || bad "no 'compensable' with an empty registry" "$comn labelled compensable"

# --- 6. the agreement machinery must actually work ------------------
# Perturb a known fraction of labels and check kappa moves the right way.
# A kappa function that returns a plausible number regardless is worse than
# none, because it would certify labels nobody checked.
"$PY" - "$TMP/a.csv" "$TMP/perturbed.csv" <<'PYEOF'
import csv, sys, random
random.seed(7)
rows=list(csv.DictReader(open(sys.argv[1], newline="")))
CL=["reversible","deferrable","compensable","irrevocable"]
n=0
for i,r in enumerate(rows):
    r["labeller"]="perturbed"
    if i % 4 == 0:                       # change 25%
        r["label"]=random.choice([c for c in CL if c!=r["label"]]); n+=1
with open(sys.argv[2],"w",newline="\n") as fh:
    w=csv.DictWriter(fh,fieldnames=rows[0].keys(),lineterminator="\n")
    w.writeheader(); w.writerows(rows)
PYEOF

selfk=$("$PY" "$L" --agreement "$TMP/a.csv" "$TMP/b.csv" 2>/dev/null | grep -oE "kappa +[0-9.-]+" | grep -oE '[0-9.-]+$')
pertk=$("$PY" "$L" --agreement "$TMP/a.csv" "$TMP/perturbed.csv" 2>/dev/null | grep -oE "kappa +[0-9.-]+" | grep -oE '[0-9.-]+$')

if [[ "$selfk" == "1.000" ]]; then
	ok "kappa is 1.000 for identical label sets" ""
else
	bad "kappa is 1.000 for identical label sets" "got '$selfk'"
fi
if [[ -n "$pertk" ]] && "$PY" -c "import sys;sys.exit(0 if float('$pertk')<0.8 else 1)"; then
	ok "kappa drops when 25% of labels are perturbed" "$pertk"
else
	bad "kappa drops when 25% of labels are perturbed" "got '$pertk' -- the metric is not measuring anything"
fi

# --- 7. and it must SAY that a rules-vs-human kappa is not the result
out=$("$PY" "$L" --agreement "$TMP/a.csv" "$TMP/perturbed.csv" 2>&1)
grep -q 'RULE labeller' <<<"$out" \
	&& ok "warns when one side is the rule labeller" "not a human agreement figure" \
	|| bad "warns when one side is the rule labeller" "silently reports it as agreement"

skip "two-human kappa" "P4-05 needs two people; this tool cannot supply them"

echo
if (( fails == 0 )); then echo "t04: PASS ($n assertions)"; else echo "t04: FAIL ($fails of $n)"; fi
exit "$fails"
