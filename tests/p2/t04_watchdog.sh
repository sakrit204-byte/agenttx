#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p2/t04_watchdog.sh --- an unwatched session must fail closed.
#
# `txctl session` holds a transaction open waiting for a human to decide.
# Twice now a desktop window was closed on the review screen and the session
# sat out its full one-hour window with /dev/agenttx open, which pins the
# module: rmmod fails with "Module agenttx is in use", and the second time
# that happened the next test silently ran against the OLD module.
#
# The UI touches <session>/watch about once a second. This asserts that when
# those touches STOP, the session aborts -- and, just as important, that a
# session nobody ever watched keeps its full window, because a person at a
# terminal is a legitimate decider who leaves no heartbeat.

set -uo pipefail

[[ -c /dev/agenttx ]] || { echo "needs the guest"; exit 77; }
TXCTL=${TXCTL:-/mnt/agenttx/tools/harness/txctl}
[[ -x $TXCTL ]] || { echo "txctl not built"; exit 77; }

GRACE=3
LOWER=$(mktemp -d); echo original > "$LOWER/f.txt"
trap 'rm -rf "$LOWER"; kill %1 2>/dev/null' EXIT
fail=0

# --- 1. watched, then abandoned -> aborts ---------------------------------
AGENTTX_WATCH_GRACE_S=$GRACE "$TXCTL" session --lower "$LOWER" \
	-- sh -c 'echo changed > f.txt' >"$LOWER/.out" 2>&1 &
sess=$!

# Wait for it to reach the decision point.
#
# Take the directory from txctl's own "session started -> DIR" line. Picking
# the newest /run/agenttx/session-* glob entry instead looks obvious and is
# wrong: the glob sorts lexically, so session-9 sorts after session-10 and
# the test touched a stale directory's watch file. The session under test
# then never saw a watcher, kept its full window, and the watchdog looked
# broken when it was the test that was.
dir=""
for _ in $(seq 1 100); do
	dir=$(sed -n 's/.*session started -> //p' "$LOWER/.out" 2>/dev/null | tail -1)
	[[ -n $dir && -d $dir && \
	   "$(cat "$dir/status" 2>/dev/null)" == awaiting-decision ]] && break
	sleep 0.1
done
if [[ ! -d $dir ]]; then
	echo "FAIL: session never reached a decision point"; exit 1
fi

# Be the watcher for a moment, then stop -- the closed-window case.
for _ in 1 2 3; do touch "$dir/watch"; sleep 0.3; done

waited=0
while kill -0 $sess 2>/dev/null && (( waited < 200 )); do
	sleep 0.1; waited=$((waited + 1))
done

if kill -0 $sess 2>/dev/null; then
	echo "FAIL: abandoned session still holding after $((waited / 10))s"
	kill -9 $sess 2>/dev/null; fail=$((fail + 1))
else
	wait $sess 2>/dev/null
	if grep -q 'no watcher' "$LOWER/.out"; then
		echo "PASS: abandoned session aborted on the watchdog"
	else
		echo "FAIL: session ended, but not via the watchdog:"
		sed 's/^/    /' "$LOWER/.out"; fail=$((fail + 1))
	fi
	# Fail closed means the lower directory is untouched.
	if [[ "$(cat "$LOWER/f.txt")" == original ]]; then
		echo "PASS: lower layer untouched (failed closed)"
	else
		echo "FAIL: watchdog abort still published the change"; fail=$((fail + 1))
	fi
fi

# --- 2. never watched -> keeps its window --------------------------------
# No touch at all. It must still be waiting well after the grace period,
# or every plain terminal session would abort out from under its operator.
AGENTTX_WATCH_GRACE_S=$GRACE "$TXCTL" session --lower "$LOWER" \
	-- sh -c 'echo x > g.txt' >"$LOWER/.out2" 2>&1 &
sess2=$!
sleep $(( GRACE * 3 ))
if kill -0 $sess2 2>/dev/null; then
	echo "PASS: unwatched session kept its window"
	kill -9 $sess2 2>/dev/null; wait $sess2 2>/dev/null
else
	echo "FAIL: a session nobody ever watched aborted anyway"
	sed 's/^/    /' "$LOWER/.out2"; fail=$((fail + 1))
fi

exit $fail
