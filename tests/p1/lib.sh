# SPDX-License-Identifier: GPL-2.0
# tests/p1/lib.sh --- shared harness for P1's tier-1 tests.
#
# Sourced, never executed.  Every test reports one line per assertion and
# exits with the number of failures, which is what tests/run.sh counts.
#
# A note on what these tests are allowed to assume: they run in the guest
# against the STUB build, so P2/P3/P4 always succeed.  That is deliberate
# (WORKFLOW.md Rule 2) and it bounds what can be tested here -- a test that
# needs a real overlay belongs in tests/p2.  If one of these passes against
# stubs and fails against the real build, the test was measuring the stub.

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TXCTL="${TXCTL:-$REPO/tools/harness/txctl}"
KO="${KO:-$REPO/agenttx.ko}"

fails=0
_t=0

if [[ -t 1 ]]; then R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'
else R=; G=; Y=; N=; fi

ok()   { _t=$((_t+1)); printf '  %sPASS%s  %-40s %s\n' "$G" "$N" "$1" "${2-}"; }
bad()  { _t=$((_t+1)); fails=$((fails+1))
         printf '  %sFAIL%s  %-40s %s\n' "$R" "$N" "$1" "${2-}"; }
skip() { printf '  %sSKIP%s  %-40s %s\n' "$Y" "$N" "$1" "${2-}"; }

# check <label> <expected> <actual>
check() {
	if [[ "$2" == "$3" ]]; then ok "$1" "$3"
	else bad "$1" "expected '$2', got '$3'"; fi
}

# expect_fail <label> <expected-errno-name> -- cmd...
# Asserts the command fails AND that its stderr names the right errno, so a
# test cannot pass because the command failed for an unrelated reason.
expect_fail() {
	local label=$1 errno=$2; shift 3   # drop the literal --
	local out rc
	out=$("$@" 2>&1); rc=$?
	if (( rc == 0 )); then
		bad "$label" "command SUCCEEDED; it must not"
	elif ! grep -qi "$errno" <<<"$out"; then
		bad "$label" "failed, but not with $errno: ${out//$'\n'/ | }"
	else
		ok "$label" "$errno"
	fi
}

# Run a shell snippet INSIDE a live transaction.
#
# This is not a convenience wrapper, it is the only correct way to test the
# thing. A transaction is owned by the process that opened it and dies with
# it (P1-08), and membership is inherited by descendants -- so assertions
# about a live transaction have to run as a descendant of its opener, while
# the opener is still alive. `txctl run` builds exactly that shape:
#
#     txctl (supervisor) -> holder (owner) -> our snippet
#
# The snippet sees $AGENTTX_TX_ID. Its exit status is the verification
# signal, so `in_tx 'false'` aborts and `in_tx 'true'` commits -- which is
# also how the commit and abort paths get exercised.
in_tx() { "$TXCTL" run -- bash -c "$1" 2>&1; }

need_dev() {
	if [[ ! -c /dev/agenttx ]]; then
		echo "  (no /dev/agenttx -- load the module first)" >&2
		exit 77
	fi
}

need_root() {
	if (( EUID != 0 )); then
		echo "  (needs root)" >&2
		exit 77
	fi
}

build_txctl() {
	if [[ ! -x "$TXCTL" ]]; then
		cc -Wall -Wextra -I "$REPO/include" -o "$TXCTL" \
		   "$REPO/tools/harness/txctl.c" || {
			echo "  (cannot build txctl)" >&2; exit 77; }
	fi
}

dmesg_mark() { echo "===AGENTTX-TEST-MARK-$$-$1==="; dmesg -n 8 2>/dev/null || true; }

# Everything the module printed since the test started.
dmesg_since() { dmesg | sed -n "/AGENTTX-TEST-START-$$/,\$p"; }
dmesg_start() { echo "AGENTTX-TEST-START-$$" > /dev/kmsg 2>/dev/null || true; }

finish() {
	echo
	if (( fails == 0 )); then
		echo "${0##*/}: PASS ($_t assertions)"
	else
		echo "${0##*/}: FAIL ($fails of $_t assertions)"
	fi
	exit "$fails"
}
