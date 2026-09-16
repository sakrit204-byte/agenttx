#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p2/t03_commit.sh --- fragment P2-05, merging upper into lower.
#
# Abort is easy; commit is where the subtlety is. Four cases, and each one
# is a different code path in src/fs/commit.c:
#
#   created file      moved down
#   modified file     moved down, replacing
#   deleted file      a WHITEOUT in the upper layer; merging it means
#                     unlinking in the lower layer, NOT copying a device
#                     node down. Getting this wrong is silent -- commit
#                     "succeeds" and the deleted file is still there.
#   new directory     overlayfs marks it opaque; that must NOT be mistaken
#                     for wholesale replacement, or an ordinary `mkdir -p`
#                     inside a transaction fails to merge. It did, once.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_dev; need_root; need_overlay; need_real_fs

echo "t03_commit: merge the upper layer down (P2-05)"

fresh_lower
echo "original" > "$LOWER/keep.txt"
echo "doomed"   > "$LOWER/gone.txt"
mkdir -p "$LOWER/sub" && echo "untouched" > "$LOWER/sub/old.txt"

out=$(tx_run "
  echo created  > $LOWER/new.txt
  echo changed  > $LOWER/keep.txt
  rm -f $LOWER/gone.txt
  mkdir -p $LOWER/sub/deeper && echo nested > $LOWER/sub/deeper/x.txt
  true")

grep -q 'committed tx=' <<<"$out" \
	&& ok "transaction committed" "verification passed -> commit" \
	|| bad "transaction committed" "${out//$'\n'/ | }"

check "created file landed"      "created"   "$(cat "$LOWER/new.txt" 2>/dev/null)"
check "modified file landed"     "changed"   "$(cat "$LOWER/keep.txt" 2>/dev/null)"
check "nested new dir landed"    "nested"    "$(cat "$LOWER/sub/deeper/x.txt" 2>/dev/null)"

# The one that fails silently if whiteouts are mishandled.
[[ ! -e "$LOWER/gone.txt" ]] \
	&& ok "deletion applied to lower" "whiteout merged as an unlink" \
	|| bad "deletion applied to lower" "gone.txt is STILL THERE -- whiteout not applied"

# The one that fails if `opaque` is mistaken for `replaced`.
check "pre-existing sibling survived" "untouched" "$(cat "$LOWER/sub/old.txt" 2>/dev/null)"

# --- commit must not leave the CoW area behind -----------------------
tx=$(sed -n 's/.*committed tx=\([0-9]*\).*/\1/p' <<<"$out" | head -1)
if [[ -n "$tx" && -d "$TXROOT/tx-$tx/upper" ]]; then
	n=$(find "$TXROOT/tx-$tx/upper" -mindepth 1 -not -name work | wc -l)
	if (( n == 0 )); then
		ok "upper layer drained by the merge" "nothing left behind"
	else
		bad "upper layer drained by the merge" "$n entr(y|ies) still in upper/"
	fi
else
	skip "upper layer drained by the merge" "no upper dir"
fi

finish
