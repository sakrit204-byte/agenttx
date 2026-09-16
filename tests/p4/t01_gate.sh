#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p4/t01_gate.sh --- regression test for measure_gate.py.
#
# The gate measurement decides whether Contribution 1 survives (PROPOSAL.md
# weeks 3-4), so a parser bug here does not produce a wrong log line, it
# produces a wrong project decision.  The fixture has a hand-checked ground
# truth and this asserts against it.
#
# Ground truth in tests/p4/fixtures/gate_sample.strace, counted by hand:
#
#   request-response (a reply is consumed on the same fd)      5
#     npmjs GET x2, github GET, stripe POST, pypi GET
#   fire-and-forget  (closed, or the thread moved on, no read) 6
#     telemetry POST x2, slack POST, paste POST x2, sendgrid POST
#   undetermined                                               0
#   excluded: one AF_UNIX journal write -- a local socket is not an
#     outbound external effect, so it must not appear in the denominator.
#
# Two bugs this has already caught, both of which produced confident and
# wrong output rather than an error:
#
#   1. `[^>]*` in the fd-annotation regex truncated at the '>' inside the
#      '->' of a connected socket, so every reported destination was the
#      LOCAL endpoint. The gate fraction was right; every per-host figure,
#      and anything built on it such as the compensable registry, was not.
#   2. Taking the first address in the annotation rather than the one after
#      the arrow -- same symptom, second cause.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

PY="${PY:-python3}"
FIX="tests/p4/fixtures/gate_sample.strace"
# Temp dir lives inside the repo, not /tmp.  Two reasons, both real:
# the guest sees the repo over 9p but has its own /tmp, and on a Windows
# authoring box the interpreter may not share a namespace with this shell.
TMP="$(mktemp -d "$REPO/.t01.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

[[ -f "$FIX" ]] || { echo "t01: missing fixture $FIX" >&2; exit 1; }

# The interpreter must share a filesystem namespace with this shell.  It
# does not when a Windows python.exe is driven from WSL: it resolves
# "/mnt/c/..." as a Windows path, writes somewhere else entirely, reports
# success, and every later check fails against a file that was never
# created.  Probe once and SKIP loudly rather than emit a wall of
# misleading failures.
if ! "$PY" -c 'import sys; open(sys.argv[1], "w").write("ok")' "$TMP/probe" 	2>/dev/null || [[ ! -f "$TMP/probe" ]]; then
	echo "t01: SKIP -- \$PY ($PY) cannot write to $TMP" >&2
	echo "     It does not share this shell's filesystem namespace." >&2
	echo "     Run with a native interpreter: PY=python3 bash $0" >&2
	exit 77
fi
rm -f "$TMP/probe"

fail=0
expect() {
	local what=$1 want=$2 got=$3
	# Strip CR before comparing. Without this a value that prints as
	# "11" compares unequal to "11" while the failure message shows
	# the two as identical -- the least debuggable failure there is.
	# Note $'\r' (ANSI-C quoting); $"\r" is locale
	# translation, which is a no-op here and would check nothing.
	got=${got%$'\r'}
	if [[ "$got" == "$want" ]]; then
		printf '  PASS  %-34s %s\n' "$what" "$got"
	else
		printf '  FAIL  %-34s expected %s, got %s\n' "$what" "$want" "$got"
		fail=1
	fi
}

"$PY" tools/harness/measure_gate.py --strace "$FIX" > "$TMP/out" 2>&1 || {
	echo "t01: measure_gate.py exited non-zero"; sed 's/^/    /' "$TMP/out"; exit 1; }

echo "t01_gate: measure_gate.py against a hand-counted fixture"

num() { grep -E "^  $1 " "$TMP/out" | tr -d '\r' | awk '{print $2}'; }

expect "outbound socket operations" 11 \
	"$(grep 'outbound socket operations:' "$TMP/out" | tr -d '\r' | awk '{print $4}')"
expect "fire-and-forget"    6 "$(num 'fire-and-forget')"
expect "request-response"   5 "$(num 'request-response')"
expect "undetermined"       0 "$(num 'undetermined')"

# The AF_UNIX journal write must not be counted: 11, not 12.
expect "AF_UNIX excluded by default" 11 \
	"$(grep 'outbound socket operations:' "$TMP/out" | tr -d '\r' | awk '{print $4}')"

# Destinations must be PEERS, not local endpoints.  The fixture's local
# side is always 10.0.0.5:41xxx, so its presence is the bug signature.
if grep -qE '^    10\.0\.0\.5:' "$TMP/out"; then
	echo "  FAIL  destinations are peers            local endpoint 10.0.0.5 reported as a destination"
	fail=1
else
	echo "  PASS  destinations are peers            no local endpoints in the report"
fi

for peer in "104.16.20.35:443" "35.186.224.25:443" "45.33.32.156:8080"; do
	if grep -qF "$peer" "$TMP/out"; then
		printf '  PASS  %-34s present\n' "peer $peer"
	else
		printf '  FAIL  %-34s missing from the report\n' "peer $peer"
		fail=1
	fi
done

expect "gate verdict" "PASS" \
	"$(grep -o 'GATE: [A-Z]*' "$TMP/out" | tr -d '\r' | awk '{print $2}')"

# --jsonl must emit docs/trace-format.md records, with payloads stripped.
if ! "$PY" tools/harness/measure_gate.py --strace "$FIX" --jsonl "$TMP/g.jsonl" 	>"$TMP/j.err" 2>&1 || [[ ! -s "$TMP/g.jsonl" ]]; then
	echo "  FAIL  --jsonl produced no output:"
	sed 's/^/    /' "$TMP/j.err"
	fail=1
fi
expect "jsonl record count" 11 "$(wc -l < "$TMP/g.jsonl" | tr -d ' \r')"
expect "jsonl payloads redacted" 0 \
	"$("$PY" -c '
import json, sys
n = 0
for line in open(sys.argv[1], encoding="utf-8"):
    if json.loads(line).get("payload_prefix") is not None:
        n += 1
print(n)' "$TMP/g.jsonl")"
expect "jsonl awaits_reply populated" 11 \
	"$("$PY" -c '
import json, sys
n = 0
for line in open(sys.argv[1], encoding="utf-8"):
    if json.loads(line).get("awaits_reply") is not None:
        n += 1
print(n)' "$TMP/g.jsonl")"

# The jsonl must feed the rest of the pipeline unchanged -- that is the
# whole reason the gate capture and the training corpus share a format.
if "$PY" tools/harness/features.py --in "$TMP/g.jsonl" --out "$TMP/g.npz" \
	>"$TMP/feat" 2>&1; then
	echo "  PASS  features.py accepts gate output  (unlabelled rows skipped)"
elif grep -q "no labelled rows" "$TMP/feat"; then
	echo "  PASS  features.py accepts gate output  (all rows unlabelled, as expected)"
elif grep -q "numpy required" "$TMP/feat"; then
	echo "  SKIP  features.py hand-off             (no numpy in $PY)"
else
	echo "  FAIL  features.py rejected gate output:"
	sed 's/^/    /' "$TMP/feat"
	fail=1
fi

echo
if (( fail )); then echo "t01: FAIL"; exit 1; fi
echo "t01: PASS"
