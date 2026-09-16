#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tools/vm/build-rootfs.sh --- build the guest rootfs.  `make vm-rootfs`.
#
# Debootstraps Ubuntu noble into a qcow2 image, wires up a serial console,
# mounts the shared repo at /mnt/agenttx, and installs the userspace the
# harness needs.  Run once; after that you snapshot rather than rebuild.
#
# Needs sudo to loop-mount.  Takes ~5 minutes on a warm apt cache.
#
# Throwaway VM on a loopback network: the root password is `agenttx`.
# Do not reuse it anywhere, and do not expose this VM beyond localhost.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BUILD="$REPO/tools/vm/build"
RAW="$BUILD/rootfs.raw"
QCOW="$BUILD/rootfs.qcow2"
MNT="$BUILD/mnt"
SUITE="${SUITE:-noble}"
SIZE="${SIZE:-6G}"
MIRROR="${MIRROR:-http://archive.ubuntu.com/ubuntu}"

for t in debootstrap qemu-img mkfs.ext4; do
	command -v "$t" >/dev/null 2>&1 || { echo "build-rootfs: need $t" >&2; exit 1; }
done

if [[ -f "$QCOW" ]]; then
	echo "build-rootfs: $QCOW already exists."
	echo "  You almost certainly want a snapshot rollback, not a rebuild:"
	echo "    qemu-img snapshot -a clean $QCOW"
	echo "  Delete the file first if you really want to start over."
	exit 1
fi

mkdir -p "$BUILD" "$MNT"

cleanup() {
	# Unmount in reverse order; ignore failures so a partial run still
	# releases the loop device rather than leaking it until reboot.
	for d in dev/pts dev proc sys; do
		sudo umount -l "$MNT/$d" 2>/dev/null || true
	done
	sudo umount "$MNT" 2>/dev/null || true
	[[ -n "${LOOP:-}" ]] && sudo losetup -d "$LOOP" 2>/dev/null || true
}
trap cleanup EXIT

echo "==> allocating $SIZE image"
truncate -s "$SIZE" "$RAW"
mkfs.ext4 -q -F -L agenttx-root "$RAW"

LOOP=$(sudo losetup --find --show "$RAW")
sudo mount "$LOOP" "$MNT"

echo "==> debootstrap $SUITE (this is the slow part)"
# --components: python3-numpy and the tracing tools live in `universe`, not
# `main`, and debootstrap only pulls `main` by default -- it then fails with
# "Couldn't find these debs" naming the package but not the reason.
#
# python3-pip is deliberately NOT installed: Ubuntu 24.04 marks the system
# python externally-managed (PEP 668), so pip would refuse to install into it
# anyway, and docs/SETUP.md section 2 says to use apt. Carrying a pip that
# cannot be used just makes the image bigger and the failure later.
sudo debootstrap --arch=amd64 \
	--components=main,universe \
	--include=systemd-sysv,udev,openssh-server,python3,python3-numpy,kmod,iproute2,curl,strace,gdb,file,less,vim-tiny,bpftrace,libcap2-bin \
	"$SUITE" "$MNT" "$MIRROR"

echo "==> configuring guest"
sudo mount --bind /dev     "$MNT/dev"
sudo mount --bind /dev/pts "$MNT/dev/pts"
sudo mount -t proc  proc  "$MNT/proc"
sudo mount -t sysfs sysfs "$MNT/sys"

# fstab: the shared repo over 9p, plus bpffs for the pinned maps the BPF
# loader expects at TX_PIN_DIR.
sudo tee "$MNT/etc/fstab" >/dev/null <<'EOF'
/dev/vda     /              ext4    defaults,noatime  0 1
agenttx      /mnt/agenttx   9p      trans=virtio,version=9p2000.L,cache=none,rw,_netdev  0 0
bpffs        /sys/fs/bpf    bpf     defaults          0 0
debugfs      /sys/kernel/debug  debugfs  defaults     0 0
tracefs      /sys/kernel/tracing tracefs defaults     0 0
EOF
sudo mkdir -p "$MNT/mnt/agenttx"

echo "agenttx-guest" | sudo tee "$MNT/etc/hostname" >/dev/null
printf '127.0.0.1 localhost\n127.0.1.1 agenttx-guest\n' | sudo tee "$MNT/etc/hosts" >/dev/null

# Serial console: run-vm.sh puts the console on ttyS0 with -nographic.
sudo chroot "$MNT" systemctl enable serial-getty@ttyS0.service >/dev/null 2>&1 || true

# Root login over the forwarded port, for scp-ing results out.  Loopback
# only; the VM has no route to anything but the QEMU user-mode NAT.
echo 'root:agenttx' | sudo chroot "$MNT" chpasswd
sudo sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' "$MNT/etc/ssh/sshd_config"

# DHCP on the virtio NIC so the guest can reach the QEMU NAT.
sudo tee "$MNT/etc/systemd/network/10-virtio.network" >/dev/null <<'EOF'
[Match]
Name=en*
[Network]
DHCP=yes
EOF
sudo chroot "$MNT" systemctl enable systemd-networkd >/dev/null 2>&1 || true

# A login banner that states the two things people forget.
sudo tee "$MNT/etc/motd" >/dev/null <<'EOF'

  AgentTx guest.  Repo is at /mnt/agenttx (9p, shared with the host).

    insmod /mnt/agenttx/agenttx.ko      load the module
    dmesg -w                            watch it
    cat /sys/kernel/security/lsm        bpf must appear in this list
    rmmod agenttx                       ten cycles must not leak

  Edit on the host. Build on the host. Only load here.

EOF

# Convenience: the test scripts run from the shared mount.
sudo tee "$MNT/root/.bashrc" >/dev/null <<'EOF'
export PS1='agenttx-guest:\w# '
export AGENTTX_REPO=/mnt/agenttx
cd /mnt/agenttx 2>/dev/null || true
EOF

cleanup
trap - EXIT

echo "==> converting to qcow2 (so snapshots work)"
qemu-img convert -f raw -O qcow2 "$RAW" "$QCOW"
rm -f "$RAW"

echo "==> taking the 'clean' snapshot"
qemu-img snapshot -c clean "$QCOW"

cat <<EOF

rootfs: $QCOW  ($(du -h "$QCOW" | cut -f1))

Snapshot 'clean' taken.  WORKFLOW.md section 7 asks you to roll back before
each session, so a corrupted rootfs after a bad rmmod is ten seconds rather
than an evening:

  qemu-img snapshot -a clean $QCOW      roll back
  qemu-img snapshot -c <name> $QCOW     take a new one
  qemu-img snapshot -l $QCOW            list

Now: make vm-boot
EOF
