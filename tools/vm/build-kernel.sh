#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tools/vm/build-kernel.sh --- build the guest kernel.  `make vm-kernel`.
#
#   make vm-kernel                 DEBUG profile (KASAN, lockdep)  <- develop here
#   make vm-kernel CONFIG=perf     PERF profile  (neither)         <- measure here
#
# Both profiles are generated from checked-in fragments, so all four of you
# get byte-identical configs.  WORKFLOW.md section 7: "works on my machine"
# must be impossible.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KDIR="${KDIR:-$HOME/src/linux}"
KBUILD_OUT="${KBUILD_OUT:-$HOME/build/linux}"
PROFILE="${CONFIG:-debug}"
JOBS="${JOBS:-$(nproc)}"

case "$PROFILE" in
debug|perf) ;;
*) echo "build-kernel: CONFIG must be 'debug' or 'perf', got '$PROFILE'" >&2; exit 2 ;;
esac

if [[ ! -d "$KDIR" ]]; then
	cat >&2 <<EOF
build-kernel: no kernel source at $KDIR

  mkdir -p ~/src && cd ~/src
  git clone --depth 1 --branch v6.12 \\
    https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git

Or set KDIR to where yours lives.  See docs/SETUP.md section 4.
EOF
	exit 1
fi

case "$KDIR" in
/mnt/*)
	echo "build-kernel: refusing to build on $KDIR" >&2
	echo "  A DrvFs/9p mount is ~10x slower for this workload; the build will" >&2
	echo "  take an hour instead of six minutes.  Move the tree to \$HOME." >&2
	echo "  Override with ALLOW_SLOW_FS=1 if you really mean it." >&2
	[[ "${ALLOW_SLOW_FS:-0}" = 1 ]] || exit 1
	;;
esac

# pahole absent turns CONFIG_DEBUG_INFO_BTF=y into a no-op *without failing
# the build*.  Every CO-RE program then fails to load with an error that
# blames the program.  Catch it here rather than in week 4.
if ! command -v pahole >/dev/null 2>&1; then
	echo "build-kernel: pahole not found -- apt install dwarves" >&2
	echo "  Without it the kernel builds fine and produces no BTF, and every" >&2
	echo "  BPF LSM program in P3 and P4 fails to load." >&2
	exit 1
fi

mkdir -p "$KBUILD_OUT"
export KBUILD_OUTPUT="$KBUILD_OUT"

command -v ccache >/dev/null 2>&1 && export KBUILD_BUILD_TIMESTAMP='' CC="ccache gcc"

echo "==> base config (defconfig + kvm_guest.config)"
make -C "$KDIR" -s defconfig
make -C "$KDIR" -s kvm_guest.config

echo "==> merging AgentTx fragments: common + $PROFILE"
"$KDIR/scripts/kconfig/merge_config.sh" -m -O "$KBUILD_OUT" \
	"$KBUILD_OUT/.config" \
	"$REPO/tools/vm/kernel-common.config" \
	"$REPO/tools/vm/kernel-$PROFILE.config"

make -C "$KDIR" -s olddefconfig

# merge_config.sh warns about conflicts on stderr and carries on, so a
# dependency it could not satisfy silently becomes =n.  Re-assert every
# symbol we cannot do without, after olddefconfig has had its say.
echo "==> verifying required symbols survived olddefconfig"
required=(
	CONFIG_BPF_LSM
	CONFIG_DEBUG_INFO_BTF
	CONFIG_DEBUG_INFO_BTF_MODULES
	CONFIG_OVERLAY_FS
	CONFIG_USER_NS
	CONFIG_MODULES
	CONFIG_MODULE_UNLOAD
	CONFIG_9P_FS
	CONFIG_NET_9P_VIRTIO
	CONFIG_GDB_SCRIPTS
	CONFIG_KPROBES
	CONFIG_UPROBES
	CONFIG_CGROUP_BPF
)
missing=0
for sym in "${required[@]}"; do
	grep -qx "$sym=y" "$KBUILD_OUT/.config" || { echo "  MISSING: $sym"; missing=1; }
done
grep -q '^CONFIG_LSM=.*bpf' "$KBUILD_OUT/.config" \
	|| { echo "  MISSING: bpf in CONFIG_LSM"; missing=1; }

if [[ "$PROFILE" = debug ]]; then
	grep -qx 'CONFIG_KASAN=y' "$KBUILD_OUT/.config" \
		|| { echo "  MISSING: CONFIG_KASAN in the debug profile"; missing=1; }
else
	! grep -qx 'CONFIG_KASAN=y' "$KBUILD_OUT/.config" \
		|| { echo "  KASAN is ON in the perf profile -- measurements would be invalid"; missing=1; }
fi

if (( missing )); then
	echo "build-kernel: config verification failed; not building." >&2
	echo "  Something in the fragment could not be satisfied by this kernel version." >&2
	exit 1
fi

echo "==> building with -j$JOBS ($PROFILE profile)"
time make -C "$KDIR" -j"$JOBS" bzImage modules

echo
echo "kernel:  $KBUILD_OUT/arch/x86/boot/bzImage"
echo "vmlinux: $KBUILD_OUT/vmlinux   (give this one to gdb)"
echo "profile: $PROFILE"
[[ "$PROFILE" = debug ]] && \
	echo "NOTE:    KASAN is on. Do not take benchmark numbers from this kernel."
