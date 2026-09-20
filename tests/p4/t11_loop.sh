#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p4/t11_loop.sh --- the agentic loop's own behaviour.
#
# Driven by tests/fixtures/fake_ollama.py rather than a real model,
# because the things worth asserting here are what the loop does when the
# MODEL misbehaves -- repeats itself, never stops, answers in prose -- and
# a real 7B cannot be made to misbehave on cue. Tier 0: no kernel, no GPU,
# no network beyond loopback, nothing spent.

set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
command -v python3 >/dev/null || { echo "needs python3"; exit 77; }

PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
WORK=$(mktemp -d); TD=$(mktemp -d)
SRV=""
cleanup() { [[ -n $SRV ]] && kill $SRV 2>/dev/null; rm -rf "$WORK" "$TD"; }
trap cleanup EXIT

fail=0
ok()  { echo "PASS: $1"; }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

echo "original" > "$WORK/notes.txt"

start_srv() {   # start_srv <script.json>
	[[ -n $SRV ]] && { kill $SRV 2>/dev/null; wait $SRV 2>/dev/null; }
	python3 "$REPO/tests/fixtures/fake_ollama.py" "$PORT" "$1" &
	SRV=$!
	for _ in $(seq 1 40); do
		curl -s --max-time 1 "http://127.0.0.1:$PORT/api/tags" >/dev/null 2>&1 && return 0
		sleep 0.25
	done
	return 1
}

run_loop() {    # run_loop <turn> <prompt>
	: > "$TD/events.jsonl"
	printf '%s' "$2" > "$TD/turn-$1.prompt"
	( cd "$WORK" && AGENTTX_OLLAMA="http://127.0.0.1:$PORT" \
	  AGENTTX_MAX_STEPS="${MAXSTEPS:-24}" \
	  python3 "$REPO/tools/agent/loop.py" "$TD" "$1" >/dev/null 2>&1 )
}

types() { python3 - "$TD/events.jsonl" <<'PY'
import json,sys
for l in open(sys.argv[1]):
    l=l.strip()
    if l:
        try: print(json.loads(l)["type"])
        except Exception: pass
PY
}
grep_ev() { grep -c "$1" "$TD/events.jsonl" 2>/dev/null || echo 0; }

# --- 1. a normal run: tool call, result, summary ------------------------
cat > "$TD/s1.json" <<'JSON'
[{"role":"assistant","content":"","tool_calls":[{"function":{"name":"write_file","arguments":{"path":"notes.txt","content":"rewritten\n"}}}]},
 {"role":"assistant","content":"I rewrote notes.txt.","tool_calls":[]}]
JSON
start_srv "$TD/s1.json" || { echo "fixture would not start"; exit 77; }
run_loop 1 "Rewrite notes.txt"

t=$(types)
for want in tx_turn_start assistant user result tx_turn_end; do
	grep -qx "$want" <<<"$t" && ok "emits '$want'" || bad "missing '$want'"
done
[[ "$(cat "$WORK/notes.txt")" == "rewritten" ]] \
	&& ok "the tool call actually wrote the file" \
	|| bad "the file was not written"
grep -q '"text": "I rewrote notes.txt."' "$TD/events.jsonl" \
	&& ok "the closing summary is in the transcript" \
	|| bad "the closing summary was dropped"

# Prose with no tool call must END the turn, not provoke another round.
n=$(python3 - "$TD/events.jsonl" <<'PY'
import json,sys
n=0
for l in open(sys.argv[1]):
    l=l.strip()
    if not l: continue
    o=json.loads(l)
    if o.get("type")=="assistant":
        for c in o["message"]["content"]:
            if c.get("type")=="tool_use": n+=1
print(n)
PY
)
[[ "$n" == "1" ]] && ok "a prose answer ends the turn (1 tool call, not more)" \
                  || bad "expected exactly 1 tool call, saw $n"

# --- 2. a model that repeats itself must be stopped ---------------------
cat > "$TD/s2.json" <<'JSON'
[{"role":"assistant","content":"","tool_calls":[{"function":{"name":"read_file","arguments":{"path":"nope.txt"}}}]}]
JSON
start_srv "$TD/s2.json"
run_loop 2 "Read a file that is not there, forever"

if grep -q 'repeated the same call' "$TD/events.jsonl"; then
	ok "an agent repeating one failing call is stopped"
else
	bad "a repeating agent was not stopped"
fi
# and it must still close the turn cleanly, or the transaction is orphaned
grep -q '"type": "tx_turn_end"' "$TD/events.jsonl" \
	&& ok "the turn still ends cleanly (no orphaned transaction)" \
	|| bad "the turn never ended -- transaction left open"

# --- 3. the step budget must be enforced --------------------------------
cat > "$TD/s3.json" <<'JSON'
[{"role":"assistant","content":"","tool_calls":[{"function":{"name":"run","arguments":{"command":"date +%N"}}}]}]
JSON
start_srv "$TD/s3.json"
MAXSTEPS=4 run_loop 3 "Loop forever"
if grep -q 'step budget' "$TD/events.jsonl"; then
	ok "the step budget stops a model that never finishes"
else
	bad "the step budget was not enforced"
fi

# --- 4. a tool error must come back as an observation, not a crash ------
cat > "$TD/s4.json" <<'JSON'
[{"role":"assistant","content":"","tool_calls":[{"function":{"name":"edit_file","arguments":{"path":"notes.txt","old":"absent","new":"x"}}}]},
 {"role":"assistant","content":"That text was not there.","tool_calls":[]}]
JSON
start_srv "$TD/s4.json"
run_loop 4 "Edit something that is not there"
if grep -q '"is_error": true' "$TD/events.jsonl"; then
	ok "a failed tool call is fed back as an error observation"
else
	bad "the tool error was not marked as one"
fi
grep -q '"exit": 0' "$TD/events.jsonl" \
	&& ok "a tool error does not fail the whole turn" \
	|| bad "a tool error killed the turn"

# --- 5. an unreachable model must say so, and not hang ------------------
: > "$TD/events.jsonl"
printf 'x' > "$TD/turn-5.prompt"
( cd "$WORK" && AGENTTX_OLLAMA="http://127.0.0.1:1" \
  timeout 60 python3 "$REPO/tools/agent/loop.py" "$TD" 5 >/dev/null 2>&1 )
rc=$?
if [[ $rc -eq 69 ]] && grep -q 'not ready' "$TD/events.jsonl"; then
	ok "an unreachable model is reported clearly, with a non-zero exit"
else
	bad "unreachable model handled badly (rc=$rc)"
fi

exit $fail
