#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p1/t08_kfunc.sh --- fragment P1-10, tx_current_id() as a BPF kfunc.
#
# What this can honestly check today, and what it cannot.
#
# CAN: the module carries BTF, the kernel parsed it, and the three kfunc
# symbols are present in it. That is the whole failure mode P1-10 is
# exposed to -- CONFIG_DEBUG_INFO_BTF_MODULES off, or pahole missing at
# kernel build time, produces a module whose kfuncs the verifier cannot
# resolve, and the error then blames the BPF program rather than the
# missing config.
#
# CANNOT: that a BPF LSM program actually calls them and gets the right
# answer. That needs a loaded LSM program, which is P3-04, and the test for
# it belongs in tests/p3. Claiming it here would be claiming P3's work.
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_dev

echo "t08_kfunc: BPF kfunc export (P1-10)"

# --- the kernel must have BTF at all ---------------------------------
if [[ -r /sys/kernel/btf/vmlinux ]]; then
	ok "vmlinux BTF present" "$(stat -c %s /sys/kernel/btf/vmlinux) bytes"
else
	bad "vmlinux BTF present" "no /sys/kernel/btf/vmlinux -- was pahole installed at kernel build time?"
	finish
fi

# --- and it must have parsed OUR module's BTF ------------------------
# This is the one that fails silently: without
# CONFIG_DEBUG_INFO_BTF_MODULES the module loads fine and simply has no
# entry here, and every P3 program that references a kfunc then fails to
# load with a message that names the program.
if [[ -r /sys/kernel/btf/agenttx ]]; then
	ok "module BTF present" "/sys/kernel/btf/agenttx, $(stat -c %s /sys/kernel/btf/agenttx) bytes"
else
	bad "module BTF present" "no /sys/kernel/btf/agenttx -- CONFIG_DEBUG_INFO_BTF_MODULES=y?"
fi

# --- the three kfuncs must appear in it ------------------------------
for fn in bpf_tx_current_id bpf_tx_current_state bpf_tx_note_class; do
	if grep -qa "$fn" /sys/kernel/btf/agenttx 2>/dev/null; then
		ok "kfunc in module BTF" "$fn"
	else
		bad "kfunc in module BTF" "$fn absent"
	fi
done

# --- the module said so at load time ---------------------------------
if dmesg | grep -q 'kfuncs registered for BPF_PROG_TYPE_LSM'; then
	ok "registered for BPF_PROG_TYPE_LSM" "not for every program type"
elif dmesg | grep -q 'kfuncs unavailable'; then
	bad "registered for BPF_PROG_TYPE_LSM" "registration failed at load"
else
	skip "registered for BPF_PROG_TYPE_LSM" "no message in the current dmesg"
fi

# --- bpf must be an active LSM, or P3 can never fire -----------------
if grep -q bpf /sys/kernel/security/lsm 2>/dev/null; then
	ok "bpf is an active LSM" "$(cat /sys/kernel/security/lsm)"
else
	bad "bpf is an active LSM" "hooks would attach and silently never fire"
fi

skip "a BPF program calls the kfunc" "needs an LSM program; that is P3-04 / tests/p3"

finish
