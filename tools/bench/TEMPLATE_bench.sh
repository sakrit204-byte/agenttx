#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tools/bench/TEMPLATE_bench.sh --- copy this to write yours.
#
# P1 -> bench_core.sh   (P1-14)
# P2 -> bench_fs.sh     (P2-11)
# P3 -> bench_hooks.sh  (P3-12)
#
# Named TEMPLATE_bench.sh, not bench_TEMPLATE.sh, so `run.py --all` does
# not pick it up.
#
# CONTRACT (tools/bench/run.py enforces all of it):
#   - CSV on stdout, exactly the header below, nothing else on stdout
#   - diagnostics go to stderr
#   - one row per (metric, config)
#   - stddev mandatory whenever n > 1
#   - exit non-zero if the measurement did not actually happen
#
# THE THREE RULES THAT DECIDE WHETHER YOUR NUMBERS MEAN ANYTHING
#
# 1. Measure every arm in the same run.  Comparing a baseline captured on
#    Tuesday against enforcing captured on Thursday measures the two days,
#    not the mechanism: different boot, different cache state, possibly a
#    different kernel.
#
# 2. Discard warm-up.  The first iterations pay for cold caches, a cold
#    BPF JIT and demand paging. Report steady state and say how many
#    iterations you dropped.
#
# 3. Report spread, not just a mean.  Inside a KVM guest the tail is the
#    interesting part; a mean alone hides a bimodal distribution, and a
#    bimodal distribution usually means something is wrong.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# CHANGE THESE TWO FIRST.  They are set to P1's values so that a fresh
# copy of this template already passes `run.py --check`; leaving them is a
# mislabelled row, not a validation error, so nothing will tell you.
STREAM="p1"          # p1 | p2 | p3 | p4
FRAGMENT="P1-14"     # the tracker id that owns this measurement

WARMUP="${WARMUP:-200}"
ITERS="${ITERS:-2000}"

# Everything informational goes to stderr; stdout is CSV and only CSV.
say() { echo "$@" >&2; }

# measure <config> -> prints "n mean stddev" on stdout
#
# Replace the body with the real thing.  Keep the shape: the caller needs
# n, mean and stddev, and the warm-up must be discarded inside here.
measure() {
	local config=$1
	say "  measuring $config (warmup $WARMUP, iters $ITERS)"

	# --- replace this block ------------------------------------------
	# e.g.  ./txbench --op begin --config "$config" --iters "$ITERS"
	# which should print one latency in ns per line.
	local samples
	samples=$(
		for _ in $(seq 1 $((WARMUP + ITERS))); do
			echo $(( (RANDOM % 50) + 100 ))
		done
	)
	# -----------------------------------------------------------------

	echo "$samples" | tail -n +$((WARMUP + 1)) | awk '
		{ x[NR] = $1; s += $1 }
		END {
			if (NR == 0) exit 1
			m = s / NR
			for (i = 1; i <= NR; i++) v += (x[i] - m) ^ 2
			printf "%d %.4f %.4f\n", NR, m, (NR > 1 ? sqrt(v / (NR - 1)) : 0)
		}'
}

row() {  # row <metric> <unit> <config> <n> <value> <stddev> <notes>
	printf '%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
		"$STREAM" "$FRAGMENT" "$1" "$2" "$3" "$4" "$5" "$6" "$7"
}

# Header first, on stdout.
echo "stream,fragment,metric,unit,config,n,value,stddev,notes"

fail=0
for config in baseline enforcing; do
	if ! read -r n mean sd < <(measure "$config"); then
		say "  $config: measurement failed"
		fail=1
		continue
	fi
	row "tx_begin_latency" "ns" "$config" "$n" "$mean" "$sd" \
		"warmup=$WARMUP discarded"
done

# A derived row is fine, but derive it here rather than in the plotting
# script: the arms were measured in this run and only this script knows
# they are comparable.
say "done"
exit "$fail"
