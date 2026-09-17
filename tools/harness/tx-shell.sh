#!/bin/sh
# SPDX-License-Identifier: GPL-2.0
#
# tools/harness/tx-shell.sh --- one SHELL turn of a thread, inside a tx.
#
# The no-model path. A leading "$" in the app runs the rest as a shell
# command instead of handing the task to the agent, and this is what runs
# it. It exists for two reasons, and the second is the important one:
#
#   1. It costs nothing. The sandbox is completely useful without a model --
#      run a build, run a migration, run somebody's install script, and
#      decide afterwards whether to keep what it did.
#   2. It is the control arm. When the agent does something surprising, the
#      first question is always "is that the agent or is that the sandbox?"
#      Being able to run the same command by hand, through the same
#      transaction, into the same transcript, answers it in one step.
#
# It writes the same event shapes as tools/harness/tx-agent.py, so the app
# renders both with one code path.
#
# Usage: tx-shell.sh THREAD_DIR TURN

set -u
TD=$1
TURN=$2
EV="$TD/events.jsonl"
CMD_FILE="$TD/turn-$TURN.cmd"

# json_str <string>  --- emit a JSON string literal, correctly escaped.
# Shell quoting cannot do this safely; python3 can, and the guest has it.
json_str() {
	python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))'
}

emit_start() {
	printf '{"type":"tx_turn_start","turn":%s,"at":%s,"shell":true,"prompt":%s}\n' \
		"$TURN" "$(date +%s)" "$(cat "$CMD_FILE" | json_str)" >> "$EV"
}
emit_out() {
	printf '{"type":"tx_output","turn":%s,"at":%s,"text":%s}\n' \
		"$TURN" "$(date +%s)" "$(json_str)" >> "$EV"
}
emit_end() {
	printf '{"type":"tx_turn_end","turn":%s,"at":%s,"exit":%s}\n' \
		"$TURN" "$(date +%s)" "$1" >> "$EV"
}

emit_start

# Run it, capture both streams together -- when a command fails, the reason
# is on stderr and separating them just means the transcript shows the
# failure without the explanation.
out=$(sh -c "$(cat "$CMD_FILE")" 2>&1)
rc=$?

printf '%s' "$out" | emit_out
emit_end "$rc"
exit "$rc"
