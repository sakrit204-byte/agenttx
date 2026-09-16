#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tools/vm/doctor.sh --- is this machine able to build and run AgentTx?
#
# Run it in week 1 on all four machines and paste the output into your
# docs/journal/p<n>.md.  WORKFLOW.md section 7 says "works on my machine"
# must be impossible; the way you make it impossible is to make the
# environment itself testable.
#
# Exit status is the number of hard failures, so it works in CI.
# Warnings do not fail: they are things you can proceed without today and
# will need by a named week.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KDIR="${KDIR:-$HOME/src/linux}"
KBUILD_OUT="${KBUILD_OUT:-$HOME/build/linux}"

fail=0
warn=0

if [[ -t 1 ]]; then
	R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; D=$'\033[2m'; N=$'\033[0m'
else
	R=; G=; Y=; D=; N=
fi

section() { printf '\n%s== %s ==%s\n' "$D" "$1" "$N"; }
ok()      { printf '  %sPASS%s  %-28s %s\n' "$G" "$N" "$1" "${2-}"; }
bad()     { printf '  %sFAIL%s  %-28s %s\n' "$R" "$N" "$1" "${2-}"; fail=$((fail+1)); }
soft()    { printf '  %sWARN%s  %-28s %s\n' "$Y" "$N" "$1" "${2-}"; warn=$((warn+1)); }

# need <name> <command> [note-on-failure]
need() {
	local name=$1 cmd=$2 note=${3-}
	if command -v "$cmd" >/dev/null 2>&1; then
		ok "$name" "$(command -v "$cmd")"
	else
		bad "$name" "${note:-not found: apt install it (docs/SETUP.md section 2)}"
	fi
}

want() {
	local name=$1 cmd=$2 note=${3-}
	if command -v "$cmd" >/dev/null 2>&1; then
		ok "$name" "$(command -v "$cmd")"
	else
		soft "$name" "$note"
	fi
}

printf '%sAgentTx doctor%s   %s\n' "$D" "$N" "$(date -Is 2>/dev/null || date)"
printf '%srepo%s %s\n' "$D" "$N" "$REPO"

# ---------------------------------------------------------------------
section "host"
# ---------------------------------------------------------------------
printf '  %sinfo%s  %-28s %s\n' "$D" "$N" "kernel" "$(uname -sr)"
printf '  %sinfo%s  %-28s %s cores, %s RAM\n' "$D" "$N" "capacity" \
	"$(nproc)" "$(free -h 2>/dev/null | awk '/^Mem:/{print $2}')"

if grep -qi microsoft /proc/version 2>/dev/null; then
	printf '  %sinfo%s  %-28s %s\n' "$D" "$N" "environment" \
		"WSL2 -- see docs/SETUP.md section 0"
fi

if [[ -c /dev/kvm ]]; then
	if [[ -r /dev/kvm && -w /dev/kvm ]]; then
		ok "/dev/kvm" "readable and writable"
	else
		bad "/dev/kvm" "present but not accessible: sudo usermod -aG kvm \$USER, then re-login"
	fi
else
	bad "/dev/kvm" "absent: enable nested virtualisation (docs/SETUP.md section 1)"
fi

if (( $(nproc) < 4 )); then
	soft "cores" "$(nproc) cores makes a kernel build painful; 8+ recommended"
fi

mem_gb=$(awk '/MemTotal/{printf "%d", $2/1048576}' /proc/meminfo 2>/dev/null || echo 0)
if (( mem_gb < 8 )); then
	soft "memory" "${mem_gb}GB: raise memory= in .wslconfig, or build with -j4"
fi

# ---------------------------------------------------------------------
section "toolchain: kernel build"
# ---------------------------------------------------------------------
need "gcc"          gcc
need "make"         make
need "flex"         flex
need "bison"        bison
need "bc"           bc
need "rsync"        rsync
need "cpio"         cpio
want "ccache"       ccache   "not required, but halves incremental rebuilds"

# pahole is the one that fails silently and costs an evening.
if command -v pahole >/dev/null 2>&1; then
	pv=$(pahole --version 2>/dev/null | tr -dc '0-9.')
	pmaj=${pv%%.*}; prest=${pv#*.}; pmin=${prest%%.*}
	if (( pmaj > 1 || (pmaj == 1 && pmin >= 24) )); then
		ok "pahole (dwarves)" "v$pv"
	else
		bad "pahole (dwarves)" "v$pv is too old for 6.12; need >= 1.24"
	fi
else
	bad "pahole (dwarves)" "ABSENT -- CONFIG_DEBUG_INFO_BTF will silently produce no BTF"
fi

for lib in libssl-dev libelf-dev; do
	if dpkg -s "$lib" >/dev/null 2>&1; then
		ok "$lib" "installed"
	else
		bad "$lib" "apt install $lib"
	fi
done

# ---------------------------------------------------------------------
section "toolchain: BPF"
# ---------------------------------------------------------------------
if command -v clang >/dev/null 2>&1; then
	cv=$(clang --version | head -1 | grep -oE '[0-9]+' | head -1)
	if (( cv >= 14 )); then
		ok "clang" "v$cv"
	else
		bad "clang" "v$cv is too old for CO-RE; need >= 14"
	fi
	if clang -print-targets 2>/dev/null | grep -q bpf; then
		ok "clang bpf target" "available"
	else
		bad "clang bpf target" "this clang cannot emit BPF: apt install llvm"
	fi
else
	bad "clang" "apt install clang llvm"
fi

if command -v bpftool >/dev/null 2>&1; then
	ok "bpftool" "$(bpftool version 2>/dev/null | head -1)"
	if [[ -f /sys/kernel/btf/vmlinux ]]; then
		soft "bpftool provenance" "check it was built from ~/src/linux, not apt (docs/SETUP.md section 4)"
	fi
else
	soft "bpftool" "build from \$KDIR/tools/bpf/bpftool -- needed by week 4 (P3-02)"
fi

# ---------------------------------------------------------------------
section "toolchain: VM and debug"
# ---------------------------------------------------------------------
want "qemu-system-x86_64" qemu-system-x86_64 "needed by week 1 (P*-00)"
want "qemu-img"           qemu-img           "needed for VM snapshots"
want "gdb"                gdb                "needed by week 1 (P*-00)"
want "debootstrap"        debootstrap        "needed once, to build the rootfs"

if command -v qemu-system-x86_64 >/dev/null 2>&1; then
	if qemu-system-x86_64 -accel help 2>/dev/null | grep -q kvm; then
		ok "qemu kvm accel" "supported"
	else
		bad "qemu kvm accel" "this qemu has no KVM; the dev loop will be unusably slow"
	fi
fi

# ---------------------------------------------------------------------
section "toolchain: userspace pipeline (P4)"
# ---------------------------------------------------------------------
need "python3" python3
if command -v python3 >/dev/null 2>&1; then
	pyv=$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')
	pymaj=${pyv%%.*}; pymin=${pyv#*.}
	if (( pymaj > 3 || (pymaj == 3 && pymin >= 9) )); then
		ok "python version" "$pyv"
	else
		bad "python version" "$pyv; need >= 3.9"
	fi
	for m in numpy sklearn; do
		if python3 -c "import $m" >/dev/null 2>&1; then
			ok "python: $m" "$(python3 -c "import $m;print($m.__version__)")"
		else
			soft "python: $m" "pip install $m -- needed for P4-07 onward"
		fi
	done
fi

# ---------------------------------------------------------------------
section "kernel source and build"
# ---------------------------------------------------------------------
if [[ -d "$KDIR" ]]; then
	kver=$(make -C "$KDIR" -s kernelversion 2>/dev/null || echo "?")
	kmaj=${kver%%.*}; krest=${kver#*.}; kmin=${krest%%.*}
	if [[ "$kmaj" =~ ^[0-9]+$ ]] && (( kmaj > 6 || (kmaj == 6 && kmin >= 12) )); then
		ok "kernel source" "$KDIR (v$kver)"
	else
		bad "kernel source" "$KDIR is v$kver; AgentTx needs >= 6.12"
	fi

	case "$KDIR" in
	/mnt/*) bad "kernel source location" \
		"$KDIR is on a DrvFs mount -- builds there are ~10x slower. Move to \$HOME." ;;
	esac
else
	soft "kernel source" "$KDIR absent -- docs/SETUP.md section 4"
fi

if [[ -f "$KBUILD_OUT/.config" ]]; then
	ok "kernel .config" "$KBUILD_OUT/.config"
	# The symbols that fail silently if merge_config dropped them.
	for sym in CONFIG_BPF_LSM CONFIG_DEBUG_INFO_BTF CONFIG_DEBUG_INFO_BTF_MODULES \
	           CONFIG_OVERLAY_FS CONFIG_USER_NS CONFIG_MODULE_UNLOAD CONFIG_9P_FS; do
		if grep -qx "$sym=y" "$KBUILD_OUT/.config"; then
			ok "  $sym" "y"
		else
			bad "  $sym" "not =y -- rebuild: make vm-kernel"
		fi
	done
	if grep -q '^CONFIG_LSM=.*bpf' "$KBUILD_OUT/.config"; then
		ok "  CONFIG_LSM" "includes bpf"
	else
		bad "  CONFIG_LSM" "does not include bpf -- LSM hooks will never fire"
	fi
	if grep -qx 'CONFIG_RANDOMIZE_BASE=y' "$KBUILD_OUT/.config"; then
		soft "  CONFIG_RANDOMIZE_BASE" "KASLR on: gdb breakpoints will miss"
	fi
	if grep -qx 'CONFIG_KASAN=y' "$KBUILD_OUT/.config"; then
		printf '  %sinfo%s  %-28s %s\n' "$D" "$N" "build profile" \
			"DEBUG (KASAN on) -- do not benchmark on this kernel"
	else
		printf '  %sinfo%s  %-28s %s\n' "$D" "$N" "build profile" \
			"PERF (KASAN off) -- benchmark-grade"
	fi
else
	soft "kernel .config" "not built yet -- make vm-kernel"
fi

if [[ -f "$KBUILD_OUT/vmlinux" ]]; then
	ok "vmlinux" "$(du -h "$KBUILD_OUT/vmlinux" | cut -f1)"
else
	soft "vmlinux" "not built yet -- make vm-kernel"
fi

# ---------------------------------------------------------------------
section "repo"
# ---------------------------------------------------------------------
case "$REPO" in
/mnt/*) soft "repo location" \
	"$REPO is on DrvFs. Fine for editing, slow for building: clone to \$HOME (SETUP section 3)." ;;
*)      ok "repo location" "on a native filesystem" ;;
esac

# A CRLF that reaches a .sh produces `bad interpreter: /bin/bash^M`, which
# names neither the file nor the real cause.  Catch it here instead.
crlf=$(find "$REPO" -type f \( -name '*.sh' -o -name '*.c' -o -name '*.h' -o -name '*.py' \) \
	-not -path '*/.git/*' -exec grep -lI $'\r' {} + 2>/dev/null | head -5)
if [[ -n "$crlf" ]]; then
	bad "line endings" "CRLF found -- run: dos2unix $(echo "$crlf" | tr '\n' ' ')"
else
	ok "line endings" "LF throughout"
fi

[[ -f "$REPO/include/agenttx.h" ]] && ok "contract header" "present" \
	|| bad "contract header" "include/agenttx.h missing"

# ---------------------------------------------------------------------
printf '\n'
if (( fail == 0 && warn == 0 )); then
	printf '%sready.%s everything green.\n' "$G" "$N"
elif (( fail == 0 )); then
	printf '%sready to start.%s %d warning(s) -- each names the week it becomes blocking.\n' \
		"$G" "$N" "$warn"
else
	printf '%s%d failure(s)%s, %d warning(s). Fix the failures before you write code.\n' \
		"$R" "$fail" "$N" "$warn"
fi
exit "$fail"
