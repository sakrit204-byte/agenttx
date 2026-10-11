#!/usr/bin/env bash
# t07_occ: commit-time conflict validation (P2-12).
#
# The behaviour this guards is the lost update. Two transactions write
# the same file in separate copy-up layers, neither can see the other,
# and before this fragment existed the second commit silently destroyed
# the first agent's work with no error returned at any point.
#
# The test holds BOTH transactions open at once, because that is the
# only arrangement in which the bug appears: if the second begins after
# the first commits, it reads the committed value and there is nothing
# to lose.
set -uo pipefail
cd "$(dirname "$0")"
. ./lib.sh

need_dev; need_root; need_overlay; need_real_fs

echo "t07_occ: refuse a commit that would overwrite another transaction (P2-12)"

enforce=$(cat /sys/module/agenttx/parameters/occ_enforce 2>/dev/null || echo "?")
if [[ "$enforce" != "Y" ]]; then
	echo "  SKIP  occ_enforce=$enforce; this test needs enforcement on"
	exit 0
fi

fresh_lower
echo "ORIGINAL" > "$LOWER/shared.txt"
echo "ORIGINAL" > "$LOWER/only-a.txt"
echo "ORIGINAL" > "$LOWER/only-b.txt"
chmod -R 777 "$LOWER"

# Two transactions, both open before either commits.
#
# A held session, not `txctl begin`: a transaction is keyed by thread
# group and the exit hook reaps it the moment its owner leaves, so a
# bare begin from a short lived shell is gone before the next command
# runs. `session` runs the work and then waits for a decision, which is
# precisely the state this test needs both transactions to be in.
sa=/tmp/t07-a.out; sb=/tmp/t07-b.out
rm -f "$sa" "$sb"
# Started one after the other, NOT simultaneously. Each session
# registers its own supervisor, and a transaction may only be committed
# by the supervisor that owns it; racing two registrations binds the
# second transaction to the first session's supervisor and its commit
# then fails with EPERM before validation ever runs. Staggering costs a
# second and still leaves both transactions open at the same time,
# which is the condition this test actually needs.
await() {
	for _ in $(seq 1 60); do
		grep -q 'awaiting decision' "$1" 2>/dev/null && return 0
		sleep 0.5
	done
	return 1
}

nohup "$TXCTL" session --lower "$LOWER" -- \
	sh -c "echo FROM-A > $LOWER/shared.txt; echo FROM-A > $LOWER/only-a.txt" \
	> "$sa" 2>&1 &
await "$sa" || { bad "first session started" "$(cat "$sa")"; exit 1; }

nohup "$TXCTL" session --lower "$LOWER" -- \
	sh -c "echo FROM-B > $LOWER/shared.txt; echo FROM-B > $LOWER/only-b.txt" \
	> "$sb" 2>&1 &
await "$sb" || { bad "second session started" "$(cat "$sb")"; exit 1; }

a=$(sed -n 's/^tx=\([0-9]*\) session started.*/\1/p' "$sa" | head -1)
b=$(sed -n 's/^tx=\([0-9]*\) session started.*/\1/p' "$sb" | head -1)
[[ -n "$a" && -n "$b" && "$a" != "$b" ]] \
	&& ok "two transactions open at once" "tx=$a and tx=$b" \
	|| { bad "two transactions open at once" "a='$a' b='$b'"; exit 1; }

check "neither has reached the folder" "ORIGINAL" \
	"$(cat "$LOWER/shared.txt" 2>/dev/null)"

# A commits first and must succeed: nothing committed before it.
#
# The decision goes through the session's own `decide` file, not a bare
# `txctl commit`. A transaction may only be committed by the supervisor
# that owns it, and each session registers itself as its own; an outside
# commit is refused for the wrong reason and would make this test pass
# without testing anything.
decide() { printf '%s' "$2" > "/run/agenttx/session-$1/decide"; }
settled() {
	for _ in $(seq 1 40); do
		grep -qiE 'committed|aborted|discarded|refused|conflict|busy' "$2" \
			&& return 0
		sleep 0.5
	done
	return 1
}

decide "$a" commit
settled "$a" "$sa"
grep -qi 'committed tx=' "$sa" \
	&& ok "first transaction commits" "tx=$a" \
	|| bad "first transaction commits" "$(tr '\n' '|' < "$sa" | tail -c 120)"

check "A's write landed" "FROM-A" "$(cat "$LOWER/shared.txt" 2>/dev/null)"

# B now holds a stale base for shared.txt. THIS is the commit that used
# to silently destroy A's line.
decide "$b" commit
settled "$b" "$sb"
if grep -qiE 'refus|conflict|resource busy|EBUSY' "$sb"; then
	ok "conflicting commit refused" "tx=$b rejected"
else
	bad "conflicting commit refused" "$(tr '\n' '|' < "$sb" | tail -c 160)"
fi

# The decisive assertion: A's work survived.
check "A's write survived B" "FROM-A" "$(cat "$LOWER/shared.txt" 2>/dev/null)"
check "B did not merge its file" "ORIGINAL" "$(cat "$LOWER/only-b.txt" 2>/dev/null)"

dmesg | grep -q "tx=$b CONFLICT" \
	&& ok "kernel named the conflict" "$(dmesg | grep "tx=$b CONFLICT" | tail -1 | cut -c1-60)" \
	|| bad "kernel named the conflict" "no CONFLICT line for tx=$b"

# B is still ACTIVE and can be aborted cleanly: refusing a commit must
# not strand a transaction.
# Refusing a commit must not leave the transaction wedged. Ask the
# kernel, not the session log: whether txload acknowledged the discard
# is a separate concern and it is not running in every tier.
decide "$b" abort
settled "$b" "$sb"
gone=1
for _ in $(seq 1 30); do
	if ! awk -v t="$b" '$1==t{found=1} END{exit !found}' \
		/sys/kernel/debug/agenttx/transactions 2>/dev/null; then
		gone=0; break
	fi
	sleep 0.5
done
[[ $gone -eq 0 ]] \
	&& ok "refused transaction is not stranded" "tx=$b left the table" \
	|| bad "refused transaction is not stranded" "tx=$b still live"

# The whole point, stated once more as a single assertion: after both
# transactions have finished, exactly one agent's line is in the file
# and it is the one that committed first.
check "folder holds the winner only" "FROM-A" \
	"$(cat "$LOWER/shared.txt" 2>/dev/null)"

exit $(( fails > 0 ))
