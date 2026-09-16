#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p1/t01_load.sh --- fragment P1-03.
#
# The tracker note is "insmod/rmmod x10 must not leak", so that is literally
# what this does.  Ten cycles is not superstition: a refcount or allocation
# leak in module_init/module_exit is invisible in one cycle and obvious in
# ten, and it is the cheapest possible check for the error-ladder bugs that
# WORKFLOW.md section 5 item 4 says are 80% of student kernel bugs.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_root

echo "t01_load: module lifecycle (P1-03)"

[[ -f "$KO" ]] || { echo "  (no $KO -- run: make STUB=1 module)" >&2; exit 77; }

# Start from a known state.
rmmod agenttx 2>/dev/null || true

dmesg_start

# --- ten load/unload cycles ------------------------------------------
cycles=10
leaked=0
for ((i = 1; i <= cycles; i++)); do
	if ! insmod "$KO" 2>/tmp/ins.err; then
		bad "cycle $i insmod" "$(cat /tmp/ins.err)"
		break
	fi
	if [[ ! -c /dev/agenttx ]]; then
		bad "cycle $i device node" "/dev/agenttx did not appear"
		rmmod agenttx 2>/dev/null || true
		break
	fi
	if ! rmmod agenttx 2>/tmp/rm.err; then
		bad "cycle $i rmmod" "$(cat /tmp/rm.err)"
		break
	fi
	if [[ -e /dev/agenttx ]]; then
		bad "cycle $i device cleanup" "/dev/agenttx survived rmmod"
		break
	fi
done
(( fails == 0 )) && ok "insmod/rmmod x$cycles" "no errors, node created and removed each time"

# --- did the module report a leak? -----------------------------------
# tx_ctx_exit() prints "LEAK at unload" for anything left in the table.
# Catching our own accusation is the point: the module is instrumented to
# tell on itself, and a test that ignored that would be worthless.
if dmesg_since | grep -q 'LEAK at unload'; then
	bad "no transaction leaked" "$(dmesg_since | grep 'LEAK at unload' | head -2)"
else
	ok "no transaction leaked" "module reported none"
fi

# --- and did anything else go wrong? ---------------------------------
if dmesg_since | grep -qE 'BUG:|WARNING:|Oops|general protection'; then
	bad "clean dmesg" "$(dmesg_since | grep -E 'BUG:|WARNING:|Oops' | head -3)"
else
	ok "clean dmesg" "no BUG/WARNING/Oops"
fi

# --- load it once more and leave it for the other tests --------------
insmod "$KO" 2>/dev/null && ok "final load" "module left loaded for t02+" \
	|| bad "final load" "could not reload"

finish
