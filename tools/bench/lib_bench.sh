# SPDX-License-Identifier: GPL-2.0
# tools/bench/lib_bench.sh --- shared statistics for the bench_*.sh scripts.
#
# Sourced, never executed. stdout belongs to CSV; everything here that talks
# writes to stderr.

say() { echo "$@" >&2; }

# stats <<< "one number per line"  ->  "n mean stddev p50 p99"
#
# Reports the tail as well as the mean. Inside a KVM guest the tail is the
# interesting part: a mean alone hides a bimodal distribution, and a bimodal
# distribution here usually means something is wrong rather than slow.
stats() {
	# NOTE: no asort(). The guest has mawk, where asort() does not exist --
	# it is a gawk extension. The host has gawk, so a version using it
	# works on the host and fails only in the guest, which is the worst
	# place to discover it. Percentiles come from sort(1) instead.
	local data n mean sd p50 p99 line
	data=$(cat)
	[ -n "$data" ] || return 1

	line=$(printf '%s\n' "$data" | awk '
		{ x[NR] = $1; s += $1 }
		END {
			if (NR == 0) exit 1
			m = s / NR
			for (i = 1; i <= NR; i++) v += (x[i] - m) ^ 2
			printf "%d %.2f %.2f", NR, m, (NR > 1 ? sqrt(v / (NR - 1)) : 0)
		}') || return 1
	read -r n mean sd <<<"$line"
	[ "${n:-0}" -gt 0 ] || return 1

	p50=$(printf '%s\n' "$data" | sort -n | awk -v n="$n" \
		'NR == int(n * 0.50) + 1 { print; exit }')
	p99=$(printf '%s\n' "$data" | sort -n | awk -v n="$n" \
		'NR == int(n * 0.99) + 1 { print; exit }')
	echo "$n $mean $sd ${p50:-0} ${p99:-0}"
}

# drop_warmup <n>  --- discard the first n samples on stdin
#
# Not optional. txbench's first sample is routinely 15-20x the steady state:
# cold caches, cold BPF JIT, demand paging of the binary itself.
drop_warmup() { tail -n +$(( $1 + 1 )); }

# row <metric> <unit> <config> <n> <value> <stddev> <notes>
#
# `notes` is QUOTED, and that is not cosmetic. A note reading
# "getpid(), no AgentTx involvement DEBUG-KERNEL-NOT-PAPER-GRADE" contains a
# comma; unquoted it splits into two columns, the DEBUG-KERNEL marker lands
# in a column no reader looks at, and plot.py cheerfully draws a figure from
# numbers that measured KASAN. That happened. The guard was defeated by
# punctuation.
#
# Embedded double quotes are doubled, per RFC 4180.
row() {
	local notes=${7//\"/\"\"}
	printf '%s,%s,%s,%s,%s,%s,%s,%s,"%s"\n' \
		"$STREAM" "$FRAGMENT" "$1" "$2" "$3" "$4" "$5" "$6" "$notes"
}

header() { echo "stream,fragment,metric,unit,config,n,value,stddev,notes"; }

# Refuse to produce paper numbers from the debug kernel.
#
# docs/SETUP.md: "Every number in the paper comes from this kernel [perf].
# A benchmark run under KASAN measures KASAN." KASAN alone is 2-4x, and a
# reader cannot tell from the CSV which kernel produced it -- so the check
# belongs here, where it can refuse.
require_perf_kernel() {
	local bad=""
	grep -qE 'kasan|KASAN' /proc/cmdline 2>/dev/null && bad="cmdline"
	if [ -r /sys/kernel/debug/kmemleak ] || \
	   dmesg 2>/dev/null | grep -qiE 'kasan: kernelAddressSanitizer|KASAN init'; then
		bad="KASAN"
	fi
	if dmesg 2>/dev/null | grep -qi 'lockdep: '; then
		bad="${bad:+$bad, }lockdep"
	fi
	if [ -n "$bad" ]; then
		if [ "${ALLOW_DEBUG_KERNEL:-0}" = 1 ]; then
			say "  WARNING: debug kernel detected ($bad)."
			say "  ALLOW_DEBUG_KERNEL=1 is set, so continuing."
			say "  These numbers measure $bad. Do not put them in the paper."
			NOTES_SUFFIX=" DEBUG-KERNEL($bad)-NOT-PAPER-GRADE"
		else
			say "bench: this is a DEBUG kernel ($bad)."
			say "  A benchmark run under KASAN measures KASAN (docs/SETUP.md)."
			say "  Build and boot the perf kernel:"
			say "    make vm-kernel CONFIG=perf"
			say "  Override with ALLOW_DEBUG_KERNEL=1 if you know why."
			exit 3
		fi
	fi
}
NOTES_SUFFIX=""
