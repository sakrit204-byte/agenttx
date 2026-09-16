#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p3/t01_load.sh --- fragment P3-02, the BPF LSM toolchain.
#
# Proves the whole chain resolves: clang emitted CO-RE relocations against
# the guest kernel's BTF, the verifier accepted five LSM programs, and the
# kfuncs in agenttx.ko were found through the MODULE's BTF.
#
# The failure this guards against is the silent one. If `bpf` is missing
# from the LSM list the programs attach successfully and never fire --
# everything looks correct and nothing happens. lib.sh checks it up front.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_bpflsm

echo "t01_load: BPF LSM toolchain (P3-02)"

out=$("$TXLOAD" --once 2>&1); rc=$?

check "txload exits cleanly" "0" "$rc"

grep -q '5 LSM hooks' <<<"$out" \
	&& ok "all five LSM programs verified and attached" "" \
	|| bad "all five LSM programs verified and attached" "${out//$'\n'/ | }"

# The egress half is not optional. Without it the LSM hook records a flow as
# deferred and the packet leaves anyway -- the system would report a
# deferral it is not performing. txload refuses to run in that state, and
# this asserts it actually got there.
grep -qE 'tcx/egress on [1-9]' <<<"$out" \
	&& ok "egress suppression attached" "$(grep -o 'tcx/egress on [0-9]* interface(s)' <<<"$out")" \
	|| bad "egress suppression attached" "deferral would silently not happen"

for h in file_open path_unlink bprm_check socket_connect socket_sendmsg; do
	grep -q "$h" <<<"$out" && ok "hook present: $h" "" || bad "hook present: $h" ""
done

# The kfuncs are resolved through the module's BTF, not the linker.
[[ -r /sys/kernel/btf/agenttx ]] \
	&& ok "module BTF readable" "kfuncs resolvable by the verifier" \
	|| bad "module BTF readable" "absent"

# Detaching must not leave anything behind: the programs hold a reference
# on the module, and a leak here means rmmod fails forever after.
if rmmod agenttx 2>/dev/null; then
	ok "module unloads after detach" "no leaked program reference"
	insmod "$REPO/agenttx.ko" 2>/dev/null
else
	bad "module unloads after detach" "rmmod refused -- a BPF program still holds it"
fi

finish
