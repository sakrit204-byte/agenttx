#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p1/t02_ioctl.sh --- fragment P1-04, the user/kernel boundary.
#
# The tracker note is "Check every copy_from_user return. Never deref a user
# pointer."  A shell test cannot inspect the kernel's source, so what it can
# do instead is drive the boundary with input the kernel must REJECT, and
# assert on the specific errno.  A handler that forgot to validate returns
# success or EFAULT; one that validated returns EPROTO or EINVAL.  The
# distinction is the test.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_dev; build_txctl

echo "t02_ioctl: the ioctl ABI (P1-04)"

# --- the version handshake -------------------------------------------
out=$("$TXCTL" abi 2>&1)
if grep -q 'match' <<<"$out"; then ok "ABI handshake" "$out"
else bad "ABI handshake" "$out"; fi

# --- a transaction round-trips ---------------------------------------
# Every assertion about a LIVE transaction runs inside one. A bare
# `txctl begin` returns an id and then immediately exits, and P1-08 aborts
# the transaction of a process that died -- so the id would name something
# already gone. That is not a bug, it is the lifetime rule, and the first
# version of this test got it wrong.
out=$(in_tx 'echo "TXID=$AGENTTX_TX_ID"; '"$TXCTL"' stat --json')

if grep -qE 'TXID=[0-9]+' <<<"$out"; then
	tx=$(sed -n 's/.*TXID=\([0-9]*\).*/\1/p' <<<"$out" | head -1)
	ok "BEGIN returns an id" "tx=$tx"
else
	bad "BEGIN returns an id" "${out//$'\n'/ | }"; finish
fi

check "id is never TX_ID_NONE" "yes" "$( [[ "$tx" != 0 ]] && echo yes || echo no )"

grep -q '"state_name":"active"' <<<"$out" \
	&& ok "STAT inside the tx says active" "" \
	|| bad "STAT inside the tx says active" "${out//$'\n'/ | }"

grep -q "\"tx_id\":$tx," <<<"$out" \
	&& ok "STAT resolves the inherited tx" "a descendant sees its enclosing tx" \
	|| bad "STAT resolves the inherited tx" "${out//$'\n'/ | }"

# --- the agent may abort its own transaction -------------------------
out=$(in_tx "$TXCTL"' abort')
grep -q 'aborted tx=' <<<"$out" \
	&& ok "a process inside the tx may ABORT it" "discarding your own work is safe" \
	|| bad "a process inside the tx may ABORT it" "${out//$'\n'/ | }"

# --- STAT outside a transaction is not an error ----------------------
# The harness polls this constantly; making it -ENOENT would put an error
# in the log on every poll.
if "$TXCTL" stat >/dev/null 2>&1; then
	ok "STAT outside a tx succeeds" "answers 'none' rather than failing"
else
	bad "STAT outside a tx succeeds" "it returned an error"
fi

# --- input the kernel must reject ------------------------------------
# A wrong magic must be ENOTTY, not a wild jump into the switch.
python3 - "$@" <<'PY' >/tmp/ioc.out 2>&1
import fcntl, os, struct, sys, errno
fd = os.open("/dev/agenttx", os.O_RDWR)
res = {}
# wrong magic byte (0xAF instead of 0xAE)
bad_magic = (3 << 30) | (56 << 16) | (0xAF << 8) | 0x04
try:
    fcntl.ioctl(fd, bad_magic, b"\0" * 56); res["magic"] = "ACCEPTED"
except OSError as e: res["magic"] = errno.errorcode.get(e.errno, e.errno)
# right magic, unknown command number
bad_nr = (3 << 30) | (56 << 16) | (0xAE << 8) | 0x7f
try:
    fcntl.ioctl(fd, bad_nr, b"\0" * 56); res["nr"] = "ACCEPTED"
except OSError as e: res["nr"] = errno.errorcode.get(e.errno, e.errno)
# correct command, WRONG abi version in the struct
stat = (3 << 30) | (56 << 16) | (0xAE << 8) | 0x04
arg = struct.pack("<IIQIIQQqII", 999, 0, 0, 0, 0, 0, 0, 0, 0, 0)
try:
    fcntl.ioctl(fd, stat, arg); res["abi"] = "ACCEPTED"
except OSError as e: res["abi"] = errno.errorcode.get(e.errno, e.errno)
os.close(fd)
print(" ".join(f"{k}={v}" for k, v in res.items()))
PY
r=$(cat /tmp/ioc.out)
grep -q 'magic=ENOTTY' <<<"$r" && ok "wrong ioctl magic -> ENOTTY" || bad "wrong ioctl magic -> ENOTTY" "$r"
grep -q 'nr=ENOTTY'    <<<"$r" && ok "unknown command -> ENOTTY"    || bad "unknown command -> ENOTTY" "$r"
grep -q 'abi=EPROTO'   <<<"$r" && ok "wrong ABI version -> EPROTO"  || bad "wrong ABI version -> EPROTO" "$r"

finish
