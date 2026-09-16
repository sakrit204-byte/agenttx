# SPDX-License-Identifier: GPL-2.0
# tests/p2/lib.sh --- shared harness for P2's tier-1 tests.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TXCTL="${TXCTL:-$REPO/tools/harness/txctl}"
TXROOT="${AGENTTX_ROOT:-/var/lib/agenttx}"
LOWER="${LOWER:-/tmp/agenttx-p2}"

fails=0; _t=0
if [[ -t 1 ]]; then R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'
else R=; G=; Y=; N=; fi
ok()   { _t=$((_t+1)); printf '  %sPASS%s  %-44s %s\n' "$G" "$N" "$1" "${2-}"; }
bad()  { _t=$((_t+1)); fails=$((fails+1)); printf '  %sFAIL%s  %-44s %s\n' "$R" "$N" "$1" "${2-}"; }
skip() { printf '  %sSKIP%s  %-44s %s\n' "$Y" "$N" "$1" "${2-}"; }
check(){ if [[ "$2" == "$3" ]]; then ok "$1" "$3"; else bad "$1" "expected '$2', got '$3'"; fi; }

need_dev()  { [[ -c /dev/agenttx ]] || { echo "  (no /dev/agenttx)" >&2; exit 77; }; }
need_root() { (( EUID == 0 )) || { echo "  (needs root)" >&2; exit 77; }; }
need_overlay() {
	grep -qw overlay /proc/filesystems || { echo "  (no overlayfs)" >&2; exit 77; }
}
# The stub build satisfies every P2 symbol by logging and returning 0, so a
# P2 test run against it passes while testing nothing. Detect and skip:
# WORKFLOW.md Rule 2's stubs must never be mistaken for an implementation.
need_real_fs() {
	if dmesg | grep -q 'agenttx/fs-stub:'; then
		echo "  (stub P2 is linked -- build with STUB_FS=0)" >&2
		exit 77
	fi
}

# Run CMD inside a transaction whose CoW lower layer is $LOWER.
tx_run() { "$TXCTL" run --lower "$LOWER" -- bash -c "$1" 2>&1; }

fresh_lower() { rm -rf "$LOWER"; mkdir -p "$LOWER"; }

finish() {
	echo
	if (( fails == 0 )); then echo "${0##*/}: PASS ($_t assertions)"
	else echo "${0##*/}: FAIL ($fails of $_t assertions)"; fi
	exit "$fails"
}
