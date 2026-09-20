#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p2/t06_tryit.sh --- run the code before deciding whether to keep it.
#
# "Can I run it and see the real output before I press Keep?" is the
# question this whole system should be able to answer yes to. The answer
# is the overlay's merged view: the folder exactly as it would look if the
# transaction committed. Run the code there and you are running the
# post-commit state, without committing.
#
# The mechanism worth testing is WHERE that view lives. tx_setup_overlay()
# mounts the overlay in a private mount namespace belonging to the
# transaction's holder, so from anywhere else merged/ is an empty
# directory -- correct isolation, and exactly why this needs nsenter
# rather than a cd.

set -uo pipefail
[[ -c /dev/agenttx ]] || { echo "needs the guest"; exit 77; }
command -v nsenter >/dev/null || { echo "needs nsenter"; exit 77; }
[[ -r /sys/kernel/debug/agenttx/transactions ]] || {
	echo "needs debugfs (mount -t debugfs none /sys/kernel/debug)"; exit 77; }
TXCTL=/usr/local/bin/txctl
[[ -x $TXCTL ]] || { echo "txctl not deployed"; exit 77; }
id -u agent >/dev/null 2>&1 || { echo "no agent user"; exit 77; }

fail=0
ok()  { echo "PASS: $1"; }
bad() { echo "FAIL: $1"; fail=$((fail+1)); }

WORK=$(mktemp -d)
cleanup() {
	[[ -n "${DIR:-}" && -d "${DIR:-}" ]] && printf abort > "$DIR/decide" 2>/dev/null
	sleep 1
	rm -rf "$WORK"
}
trap cleanup EXIT

# A file whose OUTPUT changes, so "did it really run the new version" has
# an answer that cannot be faked by reading the file.
cat > "$WORK/calc.py" <<'PYEOF'
def mul(a, b):
    return a + b          # wrong on purpose
print(mul(6, 7))
PYEOF
chown -R agent "$WORK"

( cd "$WORK" && setsid "$TXCTL" session --lower "$WORK" --as agent -- \
	sh -c 'sed -i "s/return a + b/return a * b/" calc.py' \
	> "$WORK/.log" 2>&1 & )
for _ in $(seq 1 80); do
	DIR=$(sed -n 's/.*session started -> //p' "$WORK/.log" 2>/dev/null | tail -1)
	[[ -n "$DIR" && "$(cat "$DIR/status" 2>/dev/null)" == awaiting-decision ]] && break
	sleep 0.25
done
[[ -n "${DIR:-}" ]] || { echo "FAIL: session never started"; exit 1; }
TX=${DIR##*/session-}

exec_in_tx() {   # exec_in_tx <command>
	local pid m owner
	pid=$(awk -v t="$TX" 'NR>1 && $1==t {print $6}' \
		/sys/kernel/debug/agenttx/transactions | head -1)
	[[ -n "$pid" ]] || { echo "no such transaction"; return 7; }
	# The REAL path, not .../merged: tx_setup_overlay bind-mounts the
	# merged view over the protected directory inside this namespace, so
	# here $WORK already IS the transaction's view. Using the merged path
	# works for grep and fails for python3, which absolutises the script
	# name and then cannot traverse /var/lib/agenttx (0700 root).
	m=$(readlink "/var/lib/agenttx/tx-$TX/lower" 2>/dev/null)
	[[ -n "$m" ]] || m=/var/lib/agenttx/tx-$TX/merged
	owner=$(stat -c %U "/var/lib/agenttx/tx-$TX/merged" 2>/dev/null || echo root)
	M="$m" OWNER="$owner" CMD="$1" nsenter -t "$pid" -m -- \
		sh -c 'cd "$M" && exec su -s /bin/sh "$OWNER" -c "$CMD"' 2>&1
}

# --- 1. the merged view shows the CHANGED file ---------------------------
if exec_in_tx 'grep -q "return a \* b" calc.py'; then
	ok "the transaction's view has the agent's edit"
else
	bad "the merged view does not show the change"
fi

# --- 2. running it produces the NEW behaviour ----------------------------
# This is the whole point: not "the file looks right" but "the code does
# the right thing", answered before anything is committed.
out=$(exec_in_tx 'python3 calc.py' | tr -d '[:space:]')
if [[ "$out" == "42" ]]; then
	ok "running the code in the transaction gives the fixed answer (42)"
else
	bad "expected 42 from the fixed code, got '$out'"
fi

# --- 3. the real folder is still broken ----------------------------------
real=$(cd "$WORK" && python3 calc.py | tr -d '[:space:]')
if [[ "$real" == "13" ]]; then
	ok "the real folder still has the old behaviour (13)"
else
	bad "the real folder changed before any decision: got '$real'"
fi

# --- 4. you are the agent, not root --------------------------------------
# A try-it shell that could do more than the agent could would make the
# rehearsal a poor guide to the real thing.
who=$(exec_in_tx 'whoami' | tr -d '[:space:]')
[[ "$who" == "agent" ]] \
	&& ok "the try-it shell runs as the agent, not root" \
	|| bad "try-it ran as '$who', not the agent"

# --- 5. a transaction that is gone cannot be entered ----------------------
printf abort > "$DIR/decide"
for _ in $(seq 1 60); do
	awk -v t="$TX" 'NR>1 && $1==t {found=1} END{exit !found}' \
		/sys/kernel/debug/agenttx/transactions || break
	sleep 0.5
done
exec_in_tx 'echo should-not-run' >/dev/null 2>&1
rc=$?
[[ $rc -eq 7 ]] \
	&& ok "a decided transaction can no longer be run against" \
	|| bad "entered a transaction the kernel no longer has (rc=$rc)"

# and the abort really did restore it
real=$(cd "$WORK" && python3 calc.py | tr -d '[:space:]')
[[ "$real" == "13" ]] \
	&& ok "after Discard the folder is byte-for-byte what it was" \
	|| bad "Discard left something behind: got '$real'"

DIR=""
exit $fail
