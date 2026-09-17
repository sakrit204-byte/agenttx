#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/run.sh --- run every test that can run here.
#
#   bash tests/run.sh            everything the current host supports
#   bash tests/run.sh --stub     the all-stub build's smoke tests
#   bash tests/run.sh p4         one stream only
#
# Tests are tiered by what they need, and a test that cannot run here
# SKIPS rather than fails.  The distinction matters: on a Windows or WSL
# authoring box the host-only tier is all that can run, and a runner that
# reported red there would train everyone to ignore it.
#
#   tier 0  host only        no kernel, no root, no VM.  Runs anywhere.
#   tier 1  guest            needs the AgentTx kernel and /dev/agenttx.
#   tier 2  guest + BPF LSM  needs bpf in /sys/kernel/security/lsm.
#
# Exit code is the number of FAILURES.  Skips are never failures.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

if [[ -t 1 ]]; then
	R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; D=$'\033[2m'; N=$'\033[0m'
else
	R=; G=; Y=; D=; N=
fi

STUB_ONLY=0
ONLY=""
for arg in "$@"; do
	case "$arg" in
	--stub) STUB_ONLY=1 ;;
	p1|p2|p3|p4) ONLY="$arg" ;;
	*) echo "usage: run.sh [--stub] [p1|p2|p3|p4]" >&2; exit 2 ;;
	esac
done

pass=0 fail=0 skip=0
failed_names=()

# --- what can this host do? ------------------------------------------
have_guest=0
have_bpflsm=0
[[ -c /dev/agenttx ]] && have_guest=1
grep -q bpf /sys/kernel/security/lsm 2>/dev/null && have_bpflsm=1

printf '%sAgentTx test run%s  %s\n' "$D" "$N" "$(uname -sr)"
printf '  tier 0 host        yes\n'
printf '  tier 1 guest       %s\n' "$( ((have_guest)) && echo yes || echo "no (/dev/agenttx absent)" )"
printf '  tier 2 bpf lsm     %s\n' "$( ((have_bpflsm)) && echo yes || echo "no (bpf not in /sys/kernel/security/lsm)" )"
echo

# run <tier> <path> [args...]
run_test() {
	local tier=$1 path=$2; shift 2
	local name="${path#tests/}"

	if [[ -n "$ONLY" && "$path" != tests/"$ONLY"/* ]]; then
		return
	fi
	if [[ ! -f "$path" ]]; then
		printf '  %sSKIP%s  %-34s not written yet\n' "$Y" "$N" "$name"
		skip=$((skip+1)); return
	fi
	if (( tier >= 1 && !have_guest )); then
		printf '  %sSKIP%s  %-34s needs the guest\n' "$Y" "$N" "$name"
		skip=$((skip+1)); return
	fi
	if (( tier >= 2 && !have_bpflsm )); then
		printf '  %sSKIP%s  %-34s needs BPF LSM\n' "$Y" "$N" "$name"
		skip=$((skip+1)); return
	fi

	local out rc
	out="$(bash "$path" "$@" 2>&1)"; rc=$?
	case $rc in
	0)  printf '  %sPASS%s  %s\n' "$G" "$N" "$name"; pass=$((pass+1)) ;;
	77) printf '  %sSKIP%s  %-34s %s\n' "$Y" "$N" "$name" \
	        "$(echo "$out" | tail -1)"; skip=$((skip+1)) ;;
	*)  printf '  %sFAIL%s  %s\n' "$R" "$N" "$name"
	    echo "$out" | sed 's/^/        /'
	    fail=$((fail+1)); failed_names+=("$name") ;;
	esac
}

# --- tier 0: contract and the P4 userspace pipeline -------------------
echo "tier 0 -- host only"
run_test 0 tools/vm/headers-check.sh
run_test 0 tests/p4/t01_gate.sh
run_test 0 tests/p4/t06_weights.sh
run_test 0 tests/p4/t06_sabotage.sh
run_test 0 tests/p4/t04_labels.sh
run_test 0 tests/p4/t07_infer.sh
run_test 0 tests/p4/t08_deadlock.sh

# --- tier 1: module loaded in the guest -------------------------------
echo
echo "tier 1 -- guest, module loaded"
run_test 1 tests/p1/t01_load.sh
run_test 1 tests/p1/t02_ioctl.sh
run_test 1 tests/p1/t03_ctx.sh
run_test 1 tests/p1/t04_state.sh
run_test 1 tests/p1/t05_commit.sh
run_test 1 tests/p1/t06_kill.sh
run_test 1 tests/p1/t08_kfunc.sh
run_test 1 tests/p1/t11_waitfor.sh
run_test 1 tests/p2/t02_abort.sh
run_test 1 tests/p2/t03_commit.sh
run_test 1 tests/p2/t04_watchdog.sh
# Tier 1, not tier 0: the agent harness needs a real transaction underneath.
# It uses tests/fixtures/fake-claude, so it needs no login and spends nothing.
run_test 1 tests/p4/t09_agentloop.sh

# --- tier 2: BPF LSM attached ------------------------------------------
echo
echo "tier 2 -- guest with BPF LSM"
run_test 2 tests/p3/t01_load.sh
run_test 2 tests/p3/t03_gate.sh
run_test 2 tests/p3/t06_wal.sh
run_test 2 tests/p3/t07_defer.sh
run_test 2 tests/p3/t08_flush.sh


echo
printf '  %d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"
if (( fail )); then
	printf '\n%sfailures:%s\n' "$R" "$N"
	printf '  %s\n' "${failed_names[@]}"
fi
if (( STUB_ONLY )) && (( fail == 0 )); then
	echo
	echo "  Note: this was the all-stub build. Every provider was faked, so a"
	echo "  green run means the wiring is right, not that anything works."
fi
exit "$fail"
