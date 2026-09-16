#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p2/t02_abort.sh --- fragment P2-04.  THE M1 DEMO PATH.
#
#     echo hi > f  ;  tx_abort  ;  the file is gone
#
# and the two harder cases the one-liner hides: a MODIFIED file must come
# back with its original contents, and a DELETED file must come back at all.
# The deletion case is the one that exercises overlayfs whiteouts, and it is
# the one a naive "just delete the upper layer" implementation still gets
# right -- which is exactly why it belongs here, as the thing that proves
# the CoW substrate is doing the work rather than the test being generous.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_dev; need_root; need_overlay; need_real_fs

echo "t02_abort: discard the upper layer (P2-04) -- M1 demo path"

fresh_lower
echo "original content" > "$LOWER/keep.txt"
echo "delete me"        > "$LOWER/gone.txt"
mkdir -p "$LOWER/sub" && echo "deep" > "$LOWER/sub/deep.txt"
before=$(find "$LOWER" -type f | sort | md5sum)

out=$(tx_run "
  echo created  > $LOWER/new.txt
  echo MODIFIED > $LOWER/keep.txt
  rm -f $LOWER/gone.txt
  mkdir -p $LOWER/sub/deeper && echo x > $LOWER/sub/deeper/x.txt
  false")

grep -q 'aborted tx=' <<<"$out" \
	&& ok "transaction aborted" "verification failed -> abort" \
	|| bad "transaction aborted" "${out//$'\n'/ | }"

# --- the headline ----------------------------------------------------
[[ ! -e "$LOWER/new.txt" ]] \
	&& ok "created file is gone" "echo hi > f; abort; gone" \
	|| bad "created file is gone" "$LOWER/new.txt survived the abort"

[[ ! -e "$LOWER/sub/deeper" ]] \
	&& ok "created directory is gone" "nested creation rolled back" \
	|| bad "created directory is gone" "$LOWER/sub/deeper survived"

# --- modification rolled back ----------------------------------------
got=$(cat "$LOWER/keep.txt" 2>/dev/null)
check "modified file restored" "original content" "$got"

# --- deletion rolled back (the whiteout case) ------------------------
if [[ -f "$LOWER/gone.txt" ]]; then
	check "deleted file restored" "delete me" "$(cat "$LOWER/gone.txt")"
else
	bad "deleted file restored" "$LOWER/gone.txt did not come back"
fi

# --- pre-existing content untouched throughout -----------------------
check "untouched file intact" "deep" "$(cat "$LOWER/sub/deep.txt" 2>/dev/null)"

after=$(find "$LOWER" -type f | sort | md5sum)
check "tree is byte-identical to before" "$before" "$after"

# --- and the upper layer itself is empty -----------------------------
tx=$(sed -n 's/.*aborted tx=\([0-9]*\).*/\1/p' <<<"$out" | head -1)
if [[ -n "$tx" && -d "$TXROOT/tx-$tx/upper" ]]; then
	n=$(find "$TXROOT/tx-$tx/upper" -mindepth 1 | wc -l)
	check "upper layer discarded" "0" "$n"
else
	skip "upper layer discarded" "no upper dir for tx=$tx"
fi

finish
