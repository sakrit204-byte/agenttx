#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tools/vm/run-vm.sh --- boot the guest.  `make vm-boot`.
#
#   make vm-boot              normal boot, serial console in this terminal
#   make vm-boot GDB=1        stop at the first instruction, gdb stub on :1234
#   make vm-boot PROFILE=bench  pin CPUs and isolate for measurement runs
#   SERIAL_LOG=/tmp/c.log make vm-boot   headless; drive it over ssh
#
# Quit with Ctrl-a x.
#
# The single most important line in this file is the -virtfs one.  The repo
# is SHARED into the guest over 9p, never copied into the image, so the
# edit-build-insmod loop is about fifteen seconds and needs no reboot and no
# image rebuild.  Four people times a semester multiplies every second you
# add to that loop.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KBUILD_OUT="${KBUILD_OUT:-$HOME/build/linux}"
BUILD="$REPO/tools/vm/build"
KERNEL="$KBUILD_OUT/arch/x86/boot/bzImage"
DISK="$BUILD/rootfs.qcow2"

# 6G, not 4G: the guest now hosts a Node-based agent as well as the kernel
# under test, and an OOM inside the sandbox looks exactly like a transaction
# bug. The host has ~9G available; leaving it 3G plus cache is the trade.
MEM="${MEM:-6G}"
CPUS="${CPUS:-4}"
SSH_PORT="${SSH_PORT:-2222}"
GDB="${GDB:-0}"
PROFILE="${PROFILE:-dev}"

[[ -f "$KERNEL" ]] || { echo "run-vm: no kernel at $KERNEL -- run: make vm-kernel" >&2; exit 1; }
[[ -f "$DISK"   ]] || { echo "run-vm: no rootfs at $DISK -- run: make vm-rootfs" >&2; exit 1; }

if [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
	echo "run-vm: /dev/kvm is not accessible." >&2
	echo "  Without KVM this boots under TCG emulation and is unusably slow." >&2
	echo "  Fix: sudo usermod -aG kvm \$USER   (then log out and back in)" >&2
	echo "  Override with ALLOW_TCG=1 if you understand the cost." >&2
	[[ "${ALLOW_TCG:-0}" = 1 ]] || exit 1
	ACCEL=(-accel tcg)
else
	ACCEL=(-enable-kvm -cpu host)
fi

# The LSM order is asserted on the command line as well as in the config.
# A stale image plus a fresh kernel disagreeing about this is a silent
# failure: your BPF LSM programs attach and never fire.
LSM="landlock,lockdown,yama,integrity,bpf"

CMDLINE="root=/dev/vda rw console=ttyS0,115200 earlyprintk=serial"
CMDLINE+=" nokaslr lsm=$LSM"
CMDLINE+=" panic=0 oops=panic"   # stop on the first oops; keep the dmesg readable

if [[ "$PROFILE" = bench ]]; then
	# Measurement runs: keep the timer interrupt off the CPUs under test.
	# Document in the paper that numbers are taken inside a KVM guest --
	# it is a real caveat and stating it costs nothing.
	CMDLINE+=" isolcpus=2,3 nohz_full=2,3 rcu_nocbs=2,3 mitigations=off"
fi

# Non-interactive boot.  `make vm-boot` attaches the serial console to this
# terminal, which needs a tty; a test runner, a CI job or a background
# session has none, and QEMU then fails or eats the console.  SERIAL_LOG
# sends the console to a file instead and detaches stdio, so the guest can
# be driven entirely over ssh on $SSH_PORT.
SERIAL_LOG="${SERIAL_LOG:-}"
if [[ -n "$SERIAL_LOG" ]]; then
	SERIALOPT=(-serial "file:$SERIAL_LOG" -monitor none -display none)
else
	SERIALOPT=(-nographic -serial mon:stdio)
fi

GDBOPT=()
if [[ "$GDB" = 1 ]]; then
	GDBOPT=(-s -S)
	echo "run-vm: stopped at the first instruction. In another terminal: make vm-gdb"
fi

echo "run-vm: $CPUS vCPU, $MEM, profile=$PROFILE"
echo "run-vm: repo shared at /mnt/agenttx inside the guest"
echo "run-vm: ssh -p $SSH_PORT root@localhost   (password: agenttx)"
echo "run-vm: quit with Ctrl-a x"
echo

exec qemu-system-x86_64 \
	"${ACCEL[@]}" \
	-smp "$CPUS" -m "$MEM" \
	-kernel "$KERNEL" \
	-append "$CMDLINE" \
	-drive file="$DISK",format=qcow2,if=virtio,cache=writeback \
	-netdev user,id=n0,hostfwd=tcp::"$SSH_PORT"-:22 \
	-device virtio-net-pci,netdev=n0 \
	-virtfs local,path="$REPO",mount_tag=agenttx,security_model=none,id=agenttx \
	"${SERIALOPT[@]}" \
	-no-reboot \
	"${GDBOPT[@]}"
