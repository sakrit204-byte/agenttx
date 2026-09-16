#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p1/t03_ctx.sh --- fragment P1-05 (context table) and P1-09 (nesting).
#
# P1-09's decision is that we FLATTEN: a BEGIN inside a live transaction
# joins the existing one rather than opening a second.  The paper has to
# justify that choice, so the test has to pin it down -- if flattening ever
# silently becomes true nesting, this fails.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_dev; build_txctl

echo "t03_ctx: context table and nesting (P1-05, P1-09)"

"$TXCTL" abort >/dev/null 2>&1 || true     # start clean

# --- flattening -------------------------------------------------------
# P1-09 decided we FLATTEN, and once membership is inherited, "inside a
# transaction" includes descendants. So a subprocess that calls BEGIN must
# JOIN the enclosing transaction rather than open a nested sibling.
out=$(in_tx 'echo "OUTER=$AGENTTX_TX_ID"; echo "INNER=$('"$TXCTL"' begin)"')
outer=$(sed -n 's/.*OUTER=\([0-9]*\).*/\1/p' <<<"$out" | head -1)
inner=$(sed -n 's/.*INNER=\([0-9]*\).*/\1/p' <<<"$out" | head -1)
if [[ -n "$outer" && "$outer" == "$inner" ]]; then
	ok "nested BEGIN flattens" "descendant joined tx=$outer"
else
	bad "nested BEGIN flattens" "outer='$outer' inner='$inner' -- that is nesting"
fi

# --- a descendant is inside its ancestor's transaction ---------------
# Without this, an agent's subprocesses escape the sandbox entirely: the
# hooks would see no transaction and let every syscall through.
out=$(in_tx 'bash -c "bash -c \"'"$TXCTL"' stat --json\""')
grep -q '"state_name":"active"' <<<"$out" \
	&& ok "membership is inherited 3 levels down" "grandchild sees the tx" \
	|| bad "membership is inherited 3 levels down" "${out//$'\n'/ | }"

# --- separate process trees get separate transactions ----------------
a=$(in_tx 'echo $AGENTTX_TX_ID')
b=$(in_tx 'echo $AGENTTX_TX_ID')
ida=$(grep -oE '^[0-9]+$' <<<"$a" | head -1)
idb=$(grep -oE '^[0-9]+$' <<<"$b" | head -1)
if [[ -n "$ida" && -n "$idb" && "$ida" != "$idb" ]]; then
	ok "distinct trees get distinct txs" "tx=$ida and tx=$idb"
else
	bad "distinct trees get distinct txs" "got '$ida' and '$idb'"
fi
if [[ -n "$ida" && -n "$idb" ]] && (( idb > ida )); then
	ok "ids are monotonic" "$ida -> $idb"
else
	bad "ids are monotonic" "$ida -> $idb"
fi

# --- a transaction does not outlive its owner ------------------------
# P1-08 in one assertion: the id above named a real transaction while the
# holder lived, and names nothing now.
if "$TXCTL" stat --tx "$ida" >/dev/null 2>&1; then
	bad "tx dies with its owner" "tx=$ida is still resolvable after the holder exited"
else
	ok "tx dies with its owner" "ENOENT once the holder is gone (P1-08)"
fi

# --- an id that was never issued -------------------------------------
if "$TXCTL" stat --tx 999999 >/dev/null 2>&1; then
	bad "unknown tx id is rejected" "STAT on a nonexistent id succeeded"
else
	ok "unknown tx id is rejected" "ENOENT"
fi

finish
