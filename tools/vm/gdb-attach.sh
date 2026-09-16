#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tools/vm/gdb-attach.sh --- `make vm-gdb`.
#
# Attaches to a guest booted with `make vm-boot GDB=1`.
#
# Two things here are not obvious and both cost an evening if missed:
#
#  1. add-auto-load-safe-path.  gdb refuses to run the kernel's own python
#     scripts from a directory it does not trust, and says so in one grey
#     line you will scroll past.  Without them lx-symbols, lx-dmesg and
#     lx-ps do not exist.
#
#  2. lx-symbols must be re-run in the guest AFTER each insmod.  A module
#     is linked at load time, so until you tell gdb where its sections
#     landed, a breakpoint on one of your own functions resolves to a
#     stale or absent address and simply never fires.  This is the most
#     common "gdb is broken" report on a kernel project and gdb is not
#     broken.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KBUILD_OUT="${KBUILD_OUT:-$HOME/build/linux}"
KDIR="${KDIR:-$HOME/src/linux}"
PORT="${PORT:-1234}"

VMLINUX="$KBUILD_OUT/vmlinux"
[[ -f "$VMLINUX" ]] || { echo "vm-gdb: no $VMLINUX -- run: make vm-kernel" >&2; exit 1; }

if grep -qx 'CONFIG_RANDOMIZE_BASE=y' "$KBUILD_OUT/.config" 2>/dev/null; then
	echo "vm-gdb: WARNING -- KASLR is enabled in this kernel." >&2
	echo "        Breakpoints will resolve to the wrong addresses and never fire." >&2
	echo "        Set CONFIG_RANDOMIZE_BASE=n and rebuild." >&2
fi

INIT="$(mktemp)"
trap 'rm -f "$INIT"' EXIT

cat > "$INIT" <<EOF
set confirm off
set pagination off
set architecture i386:x86-64
add-auto-load-safe-path $KBUILD_OUT
add-auto-load-safe-path $KDIR
source $KBUILD_OUT/vmlinux-gdb.py
target remote :$PORT

define txhelp
  echo \n
  echo AgentTx gdb cheat sheet\n
  echo   lx-symbols /mnt/agenttx   load module symbols AFTER insmod in the guest\n
  echo   lx-dmesg                  read the guest ring buffer without a console\n
  echo   lx-ps                     list guest tasks\n
  echo   lx-lsmod                  list loaded modules\n
  echo   b tx_ioctl_begin          break in the module (needs lx-symbols first)\n
  echo   p *(struct tx_ctx *)\$rdi  inspect a transaction context\n
  echo   c                         continue\n
  echo \n
end

echo \n
echo attached. run 'txhelp' for the cheat sheet.\n
echo remember: lx-symbols AFTER every insmod, or breakpoints will not fire.\n
echo \n
EOF

exec gdb -q "$VMLINUX" -x "$INIT"
