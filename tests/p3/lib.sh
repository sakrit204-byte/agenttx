# SPDX-License-Identifier: GPL-2.0
# tests/p3/lib.sh --- shared harness for P3's tier-2 tests.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TXCTL="${TXCTL:-$REPO/tools/harness/txctl}"
TXLOAD="${TXLOAD:-$REPO/src/bpf/txload}"
LOWER="${LOWER:-/tmp/agenttx-p3}"

fails=0; _t=0
if [[ -t 1 ]]; then R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'
else R=; G=; Y=; N=; fi
ok()   { _t=$((_t+1)); printf '  %sPASS%s  %-44s %s\n' "$G" "$N" "$1" "${2-}"; }
bad()  { _t=$((_t+1)); fails=$((fails+1)); printf '  %sFAIL%s  %-44s %s\n' "$R" "$N" "$1" "${2-}"; }
skip() { printf '  %sSKIP%s  %-44s %s\n' "$Y" "$N" "$1" "${2-}"; }
check(){ if [[ "$2" == "$3" ]]; then ok "$1" "$3"; else bad "$1" "expected '$2', got '$3'"; fi; }

need_bpflsm() {
	grep -q bpf /sys/kernel/security/lsm 2>/dev/null || {
		echo "  (bpf is not an active LSM -- boot with lsm=...,bpf)" >&2; exit 77; }
	[[ -c /dev/agenttx ]] || { echo "  (agenttx.ko not loaded)" >&2; exit 77; }
	[[ -x "$TXLOAD" ]]    || { echo "  (no $TXLOAD -- make -C src/bpf)" >&2; exit 77; }
	[[ -r /sys/kernel/btf/agenttx ]] || {
		echo "  (no module BTF -- CONFIG_DEBUG_INFO_BTF_MODULES=y?)" >&2; exit 77; }
}

# Run CMD inside a transaction while txload streams the WAL.
# Returns the WAL text on stdout.
with_wal() {
	local script=$1 out
	out=$(mktemp)
	rm -rf "$LOWER"; mkdir -p "$LOWER"
	( setsid "$TXLOAD" >"$out" 2>&1 & )
	sleep 2
	"$TXCTL" run --lower "$LOWER" -- bash -c "$script" >/dev/null 2>&1
	sleep 1
	pkill -INT -f "$TXLOAD" 2>/dev/null
	sleep 1
	cat "$out"
	rm -f "$out"
}

finish() {
	echo
	if (( fails == 0 )); then echo "${0##*/}: PASS ($_t assertions)"
	else echo "${0##*/}: FAIL ($fails of $_t assertions)"; fi
	exit "$fails"
}
