#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tools/bench/bench_fs.sh --- fragment P2-11.
#
# Two numbers:
#
#   latency          a write straight to the filesystem, vs the same write
#                    through the per-transaction overlay.
#   amplification    bytes landing on disk per byte of logical change. The
#                    tracker asks for this specifically, and it is the honest
#                    cost of copy-on-write: editing one byte of a 1 MB file
#                    copies the whole file up.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
source "$REPO/tools/bench/lib_bench.sh"

STREAM="p2"
FRAGMENT="P2-11"
WARMUP="${WARMUP:-50}"
ITERS="${ITERS:-400}"
SIZE="${SIZE:-4096}"
BIN="${TXBENCH:-$REPO/tools/bench/txbench}"
TXCTL="${TXCTL:-/usr/local/bin/txctl}"
BASE=/tmp/benchfs

[ -x "$BIN" ] || { say "bench_fs: no txbench"; exit 1; }
require_perf_kernel

rm -rf "$BASE"; mkdir -p "$BASE"
header
fail=0

# --- arm 1: plain filesystem -----------------------------------------
say "  baseline write ${SIZE}B"
out=$("$BIN" --op write --path "$BASE/plain.bin" --size "$SIZE" \
      --iters $((WARMUP + ITERS)) 2>/dev/null | drop_warmup "$WARMUP" | stats)
if [ -n "$out" ]; then
	read -r n mean sd p50 p99 <<<"$out"
	row "write_latency" "ns" "no_overlay" "$n" "$mean" "$sd" \
	    "size=$SIZE p50=$p50 p99=$p99$NOTES_SUFFIX"
else
	say "  baseline failed"; fail=1
fi

# --- arm 2: through the per-transaction overlay ----------------------
if [ -x "$TXCTL" ] && [ -c /dev/agenttx ]; then
	say "  overlay write ${SIZE}B (inside a transaction)"
	out=$("$TXCTL" run --lower "$BASE" -- \
	      "$BIN" --op write --path "$BASE/cow.bin" --size "$SIZE" \
	             --iters $((WARMUP + ITERS)) 2>/dev/null \
	      | grep -E '^[0-9]+$' | drop_warmup "$WARMUP" | stats)
	if [ -n "$out" ]; then
		read -r n mean sd p50 p99 <<<"$out"
		row "write_latency" "ns" "overlay" "$n" "$mean" "$sd" \
		    "size=$SIZE p50=$p50 p99=$p99$NOTES_SUFFIX"
	else
		say "  overlay arm produced nothing"; fail=1
	fi
else
	say "  no /dev/agenttx or txctl; skipping the overlay arm"
fi

# --- amplification ----------------------------------------------------
# One byte changed in a file of SIZE bytes. overlayfs copies the whole file
# up, so the ratio is the file size -- which is the point of measuring it.
if [ -x "$TXCTL" ] && [ -c /dev/agenttx ]; then
	rm -rf "$BASE/amp"; mkdir -p "$BASE/amp"
	dd if=/dev/zero of="$BASE/amp/big.bin" bs=1 count="$SIZE" 2>/dev/null
	before=$(du -sb /var/lib/agenttx 2>/dev/null | cut -f1 || echo 0)
	"$TXCTL" run --lower "$BASE/amp" -- \
		sh -c "printf X | dd of=big.bin bs=1 seek=0 conv=notrunc 2>/dev/null; sleep 0.3" \
		>/dev/null 2>&1 || true
	after=$(du -sb /var/lib/agenttx 2>/dev/null | cut -f1 || echo 0)
	delta=$(( after > before ? after - before : 0 ))
	if [ "$delta" -gt 0 ]; then
		row "storage_amplification" "bytes_per_byte" "overlay" "1" \
		    "$delta" "0" "1 logical byte changed in a ${SIZE}B file$NOTES_SUFFIX"
	else
		say "  amplification: no measurable delta (transaction may have been reaped)"
	fi
fi

say "done"
exit "$fail"
