#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p3/t03_gate.sh --- fragment P3-04, gating on transaction state.
#
# THE PROPERTY: a hook must record syscalls made INSIDE a transaction and
# ignore everything else. The hooks fire for every process on the machine,
# so "ignore everything else" is not an optimisation, it is the difference
# between a usable system and one that logs the entire host.
#
# This is also the test that proves P1's inheritance works from the BPF
# side: the agent is a grandchild of the process that called tx_begin, and
# bpf_tx_current_id() has to find the transaction by walking up to it.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_bpflsm

echo "t03_gate: hooks gate on tx_current_id() (P3-04)"

wal=$(with_wal "
  echo hi > $LOWER/inside.txt
  cat $LOWER/inside.txt >/dev/null
  true")

seen=$(sed -n 's/.*syscalls seen *\([0-9]*\).*/\1/p'  <<<"$wal" | tail -1)
intx=$(sed -n 's/.*inside a tx *\([0-9]*\).*/\1/p'    <<<"$wal" | tail -1)
recs=$(sed -n 's/.*WAL records *\([0-9]*\).*/\1/p'    <<<"$wal" | tail -1)
drop=$(sed -n 's/.*DROPPED (ring full) *\([0-9]*\).*/\1/p' <<<"$wal" | tail -1)

if [[ -n "$seen" ]] && (( seen > 0 )); then
	ok "hooks fired at all" "$seen syscall(s) reached the hooks"
else
	bad "hooks fired at all" "counter says '$seen'"; finish
fi

if (( intx > 0 )); then
	ok "some syscalls were inside a tx" "$intx of $seen"
else
	bad "some syscalls were inside a tx" "the gate never matched -- inheritance broken?"
fi

# The gate must EXCLUDE things, or it is not a gate.
if (( intx < seen )); then
	ok "the gate excludes non-transacting work" "$((seen - intx)) syscall(s) ignored"
else
	bad "the gate excludes non-transacting work" "every syscall counted as in-tx"
fi

check "every in-tx syscall produced a record" "$intx" "$recs"
check "no WAL record was dropped" "0" "$drop"

# The agent is a GRANDCHILD of the tx owner (txctl -> holder -> agent), so
# any record at all proves bpf_tx_current_id() walked the ancestry.
grep -qE 'tx=[0-9]+ +file_open' <<<"$wal" \
	&& ok "inherited membership visible from BPF" "a grandchild's open was attributed" \
	|| bad "inherited membership visible from BPF" "no file_open attributed to a tx"

# seq must be gap-free: P3-09 replays in this order and a hole in the WAL
# can neither be replayed nor discarded.
seqs=$(grep -oE 'seq=[0-9]+' <<<"$wal" | cut -d= -f2 | sort -n | uniq)
n=$(wc -l <<<"$seqs"); lo=$(head -1 <<<"$seqs"); hi=$(tail -1 <<<"$seqs")
if [[ -n "$lo" ]] && (( hi - lo + 1 == n )); then
	ok "seq is gap-free" "$lo..$hi, $n record(s)"
else
	bad "seq is gap-free" "range $lo..$hi but only $n record(s)"
fi

finish
