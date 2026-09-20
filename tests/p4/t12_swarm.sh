#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p4/t12_swarm.sh --- N agents, N transactions, one folder.
#
# This is the case the whole transaction machinery exists for, and until
# the swarm existed nothing in this repo produced it. One agent has
# nothing to conflict with: its write set intersects nobody's, the
# wait-for graph has one node, and the deadlock detector never sees a
# cycle outside a unit test.
#
# Two agents told to write the same file, in separate transactions, are a
# lost update waiting to happen. Each holds a private copy-on-write layer,
# so neither can see the other, and whichever commits second silently
# destroys the first one's work. The test asserts that (a) they really do
# get separate transactions, (b) neither reaches the real folder before a
# decision, and (c) the overlap is DETECTED and reported rather than
# quietly resolved by whoever finished last.
#
# Driven by tests/fixtures/fake_ollama.py: a real model cannot be made to
# collide on cue, and the collision is the entire point.

set -uo pipefail
[[ -c /dev/agenttx ]] || { echo "needs the guest"; exit 77; }
REPO=${REPO:-/mnt/agenttx}
[[ -x /usr/local/bin/txctl ]] || { echo "txctl not deployed"; exit 77; }
command -v python3 >/dev/null || { echo "needs python3"; exit 77; }
id -u agent >/dev/null 2>&1 || { echo "no agent user"; exit 77; }

fail=0
ok()  { echo "PASS: $1"; }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
WORK=$(mktemp -d); TD=/var/lib/agenttx-threads/t12-$$
SRV=""
cleanup() {
	[[ -n $SRV ]] && kill $SRV 2>/dev/null
	for d in /run/agenttx/session-*; do
		[[ -d $d ]] && printf abort > "$d/decide" 2>/dev/null
	done
	# WAIT for them to actually go.
	#
	# Aborting is asynchronous: txctl still has to end the transaction
	# and exit, and until it does it holds /dev/agenttx and pins the
	# module. A fixed `sleep 1` was not enough for two agents, and the
	# next test to run happened to be tests/p3/t01_load.sh, whose last
	# assertion is that the module unloads -- so this test's mess was
	# reported as that test's failure. A test has to leave the system as
	# it found it, or it makes the next one a liar.
	for _ in $(seq 1 60); do
		pgrep -x txctl >/dev/null 2>&1 || break
		sleep 0.5
	done
	for p in $(pgrep -x txctl 2>/dev/null); do kill -9 "$p" 2>/dev/null; done
	sleep 1
	rm -rf "$WORK" "$TD"
}
trap cleanup EXIT

mkdir -p "$TD"
echo "original" > "$WORK/shared.txt"
chown -R agent "$WORK" "$TD"

# Every agent: write shared.txt, then summarise. Identical behaviour for
# all of them, which is what guarantees the collision.
cat > "$TD/script.json" <<'JSON'
[{"_mode":"content","_when":"plan","role":"assistant",
  "content":"[{\"name\":\"alpha\",\"task\":\"Update shared.txt\"},{\"name\":\"beta\",\"task\":\"Update shared.txt too\"}]"},
 {"_when":"first","role":"assistant","content":"",
  "tool_calls":[{"function":{"name":"write_file","arguments":{"path":"shared.txt","content":"rewritten by an agent\n"}}}]},
 {"_when":"after_tool","role":"assistant","content":"Updated shared.txt.","tool_calls":[]}]
JSON

python3 "$REPO/tests/fixtures/fake_ollama.py" "$PORT" "$TD/script.json" &
SRV=$!
for _ in $(seq 1 40); do
	curl -s --max-time 1 "http://127.0.0.1:$PORT/api/tags" >/dev/null 2>&1 && break
	sleep 0.25
done
curl -s --max-time 2 "http://127.0.0.1:$PORT/api/tags" >/dev/null 2>&1 \
	|| { echo "fixture would not start"; exit 77; }

printf 'Update the shared file' > "$TD/turn-1.prompt"
chown -R agent "$TD"

AGENTTX_OLLAMA="http://127.0.0.1:$PORT" timeout 420 python3 \
	"$REPO/tools/agent/swarm.py" "$TD" 1 --lower "$WORK" --agents 2 \
	--runas agent > "$TD/swarm.log" 2>&1
rc=$?
[[ $rc -eq 0 ]] || { echo "FAIL: swarm exited $rc"; sed 's/^/    /' "$TD/swarm.log" | tail -12; exit 1; }

ev="$TD/events.jsonl"
q() { python3 - "$ev" "$1" <<'PY'
import json,sys
want=sys.argv[2]
for l in open(sys.argv[1]):
    l=l.strip()
    if not l: continue
    try: o=json.loads(l)
    except Exception: continue
    if o.get("type")==want: print(json.dumps(o))
PY
}

# --- each agent gets its OWN transaction --------------------------------
txs=$(q swarm_agent_done | python3 -c '
import json,sys
out=[json.loads(l)["tx"] for l in sys.stdin if l.strip()]
print(" ".join(t for t in out if t))')
n=$(wc -w <<<"$txs")
uniq=$(tr " " "\n" <<<"$txs" | sort -u | grep -c .)
if [[ "$n" == "2" && "$uniq" == "2" ]]; then
	ok "2 agents got 2 distinct transactions ($txs)"
else
	bad "expected 2 distinct transactions, got n=$n uniq=$uniq ($txs)"
fi

# --- nothing reached the real folder ------------------------------------
if [[ "$(cat "$WORK/shared.txt")" == "original" ]]; then
	ok "neither agent's write reached the real folder"
else
	bad "an agent's write escaped its transaction: $(cat "$WORK/shared.txt")"
fi

# --- the collision is DETECTED ------------------------------------------
res=$(q swarm_result | tail -1)
if [[ -z "$res" ]]; then
	bad "no swarm_result event"
else
	nconf=$(python3 -c '
import json,sys
print(len(json.loads(sys.argv[1]).get("conflicts") or []))' "$res")
	if [[ "$nconf" -ge 1 ]]; then
		ok "the overlapping write was detected ($nconf conflict)"
	else
		bad "two agents wrote shared.txt and no conflict was reported"
	fi
	if python3 -c '
import json,sys
c=json.loads(sys.argv[1]).get("conflicts") or []
sys.exit(0 if any("shared.txt" in (x.get("paths") or []) for x in c) else 1)' "$res"; then
		ok "the conflict names the file they collided on"
	else
		bad "the conflict does not name shared.txt"
	fi
fi

# The human-readable warning matters as much as the structured event: the
# whole point is that a person is about to choose, and "keep both" is the
# one choice that silently loses work.
if q tx_notice | grep -q 'CONFLICT'; then
	ok "the person is warned in plain words before deciding"
else
	bad "no plain-language conflict warning"
fi

exit $fail
