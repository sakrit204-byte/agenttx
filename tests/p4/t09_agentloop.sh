#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p4/t09_agentloop.sh --- the agent harness: threads, turns, transcripts.
#
# The harness runs Claude Code's own agent loop inside a transaction and
# writes the loop out as an event stream. This asserts the properties the
# desktop app depends on, using tests/fixtures/fake-claude in place of the
# real binary so the test needs no login and spends nothing.
#
# The property that matters most is the last one. A transcript stored inside
# the protected directory would be destroyed by Discard, which would leave
# you with an agent that did something you can no longer inspect -- the exact
# failure this project exists to prevent. So: discarding the WORK must not
# discard the ACCOUNT of the work.

set -uo pipefail

[[ -c /dev/agenttx ]] || { echo "needs the guest"; exit 77; }
REPO=${REPO:-/mnt/agenttx}
TXCTL=/usr/local/bin/txctl
[[ -x $TXCTL ]] || { echo "txctl not deployed"; exit 77; }
command -v python3 >/dev/null || { echo "needs python3"; exit 77; }

TD_ROOT=/var/lib/agenttx-threads
WORK=$(mktemp -d)
TID="t09-$$"
TD="$TD_ROOT/$TID"
fail=0
ok()  { echo "PASS: $1"; }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

cleanup() {
	[[ -n "${sess:-}" ]] && kill -9 "$sess" 2>/dev/null
	rm -rf "$WORK" "$TD"
}
trap cleanup EXIT

install -m755 "$REPO/tools/harness/tx-agent.py" /usr/local/bin/tx-agent.py
install -m755 "$REPO/tests/fixtures/fake-claude" /usr/local/bin/fake-claude
echo "before" > "$WORK/existing.txt"
chown -R agent "$WORK" 2>/dev/null || true

mkdir -p "$TD"
echo /usr/local/bin/fake-claude > "$TD/claude-bin"
date +%s > "$TD/created"
printf 'agent loop test' > "$TD/title"
printf '%s' "$WORK" > "$TD/lower"
: > "$TD/events.jsonl"
SID=$(python3 -c 'import uuid;print(uuid.uuid4())')
printf '%s' "$SID" > "$TD/session"

run_turn() {   # run_turn <n> <prompt>
	printf '%s' "$2" > "$TD/turn-$1.prompt"
	chown -R agent "$TD" 2>/dev/null || true
	( cd "$WORK" && setsid "$TXCTL" session --lower "$WORK" --as agent -- \
		/usr/local/bin/tx-agent.py "$TD" "$1" "$SID" \
		> "$TD/start-$1.log" 2>&1 & echo $! > "$TD/pid-$1" )
	for _ in $(seq 1 120); do
		grep -q 'awaiting decision' "$TD/start-$1.log" 2>/dev/null && return 0
		sleep 0.25
	done
	return 1
}

ev_types() { python3 - "$TD/events.jsonl" <<'PY'
import json,sys
for line in open(sys.argv[1]):
    line=line.strip()
    if line:
        try: print(json.loads(line)["type"])
        except Exception: pass
PY
}

# --- turn 1 ---------------------------------------------------------------
if ! run_turn 1 "Write a NOTES.md describing this folder"; then
	echo "FAIL: turn 1 never reached a decision point"
	sed 's/^/    /' "$TD/start-1.log" 2>/dev/null
	exit 1
fi
TX1=$(sed -n 's/^tx=\([0-9]*\) session started.*/\1/p' "$TD/start-1.log" | tail -1)

types=$(ev_types)
for want in tx_turn_start system assistant user result tx_turn_end; do
	if grep -qx "$want" <<<"$types"; then
		ok "transcript has a '$want' event"
	else
		bad "transcript is missing '$want' (got: $(tr '\n' ' ' <<<"$types"))"
	fi
done

# The loop must be visible as a loop: tool calls AND their results.
ntool=$(python3 - "$TD/events.jsonl" <<'PY'
import json,sys
n=0
for line in open(sys.argv[1]):
    line=line.strip()
    if not line: continue
    try: o=json.loads(line)
    except Exception: continue
    if o.get("type")=="assistant":
        for c in o.get("message",{}).get("content",[]):
            if c.get("type")=="tool_use": n+=1
print(n)
PY
)
(( ntool >= 2 )) && ok "loop shows $ntool tool calls" \
                 || bad "expected >=2 tool calls, saw $ntool"

# The agent's write must be INVISIBLE outside the transaction until kept.
if [[ -e "$WORK/NOTES.md" ]]; then
	bad "NOTES.md is already visible in the real folder before Keep"
else
	ok "the agent's new file is not in the real folder yet"
fi

# --- turn 2 must RESUME, not restart -------------------------------------
sess_dir=/run/agenttx/session-$TX1
printf abort > "$sess_dir/decide" 2>/dev/null
sleep 1

lines_before=$(wc -l < "$TD/events.jsonl")
if ! run_turn 2 "Now make it shorter"; then
	bad "turn 2 never reached a decision point"
else
	TX2=$(sed -n 's/^tx=\([0-9]*\) session started.*/\1/p' "$TD/start-2.log" | tail -1)
	[[ -n "$TX2" && "$TX2" != "$TX1" ]] \
		&& ok "turn 2 got its own transaction ($TX1 -> $TX2)" \
		|| bad "turn 2 reused or lost the transaction (tx1=$TX1 tx2=$TX2)"

	if grep -q -- "--resume" "$TD/start-2.log" 2>/dev/null || \
	   python3 - "$TD/events.jsonl" "$SID" <<'PY'
import json,sys
sid=sys.argv[2]
for line in open(sys.argv[1]):
    line=line.strip()
    if not line: continue
    try: o=json.loads(line)
    except Exception: continue
    if o.get("turn")==2 and o.get("session_id")==sid:
        sys.exit(0)
sys.exit(1)
PY
	then
		ok "turn 2 continued the same conversation ($SID)"
	else
		bad "turn 2 did not resume the thread's session"
	fi
fi

# --- the transcript must survive Discard ---------------------------------
lines_now=$(wc -l < "$TD/events.jsonl")
(( lines_now > lines_before )) \
	&& ok "turn 2 appended to the same transcript ($lines_before -> $lines_now)" \
	|| bad "turn 2 wrote nothing ($lines_before -> $lines_now)"

TX2=${TX2:-}
if [[ -n $TX2 ]]; then
	printf abort > "/run/agenttx/session-$TX2/decide" 2>/dev/null
	sleep 1.5
fi

if [[ -s "$TD/events.jsonl" ]]; then
	ok "the transcript survived Discard ($(wc -l < "$TD/events.jsonl") events)"
else
	bad "Discard destroyed the record of what the agent did"
fi
if [[ "$(cat "$WORK/existing.txt" 2>/dev/null)" == before ]] && \
   [[ ! -e "$WORK/NOTES.md" ]]; then
	ok "Discard left the real folder byte-for-byte unchanged"
else
	bad "Discard did not restore the folder"
fi

exit $fail
