# AgentTx — environment walkthrough

Everything needed to go from this Windows machine to four people building kernel
code against an identical target. Read section 0 before you install anything: the
conclusion is that **you probably do not need to install Linux.**

---

## 0. The host decision

AgentTx needs three things from its host:

1. A Linux userspace that can **build a 6.12 kernel** (gcc, flex/bison, pahole).
2. A **KVM-capable** hypervisor so the QEMU guest runs at native speed — a kernel
   dev loop under TCG emulation is unusable.
3. A **BPF toolchain** (clang ≥ 14, libbpf, bpftool) to compile CO-RE programs.

It does **not** need the host kernel to be the target kernel. WORKFLOW.md §7 is
explicit: *never test on your host kernel*. The target is always a QEMU guest
running the kernel you built. That decoupling is what makes the host choice much
less constrained than it looks.

### The three options

| | Build host | Runs KVM | Cost to set up | Verdict |
|---|---|---|---|---|
| **WSL2 Ubuntu** | yes | **yes, already** | ~20 min | **start here** |
| VM (VirtualBox/VMware) | yes | nested only, slow | ~1 hr | avoid |
| Dual-boot / bare metal | yes | yes, fastest | ~2 hr + repartition | the fallback |

### What is already on this machine

Checked, not assumed:

```
Ubuntu 24.04.4 LTS on WSL2, kernel 6.6.87.2-microsoft-standard-WSL2
/dev/kvm            present          <- nested virtualisation is ON
CPU                 vmx              <- hardware virt available
cores / RAM / disk  12 / 7.6 GiB / 952 GiB free
gcc 13.3, make 4.3, git 2.43, python3 3.12, flex, bison   present
clang, bpftool, qemu-system-x86_64                        MISSING
```

`/dev/kvm` existing inside WSL2 is the whole ballgame. It means QEMU/KVM works
there, which means WSL2 is a complete AgentTx host. The three missing packages
are one `apt install`.

**So: use WSL2.** Keep dual-boot as the fallback for two specific situations —
if you need to `perf` the host kernel for the benchmark numbers, or if WSL2's
memory ceiling starts hurting during `-j12` kernel builds. Neither is likely
before week 8.

### Which distribution

**Ubuntu 24.04 LTS.** Decided; do not relitigate it in week 6.

The decision is easy because **the host distribution barely matters**. The target
is a QEMU guest running a kernel you built from kernel.org source with your own
config, and even `bpftool` is built from that tree rather than taken from the
distro (§4) — precisely so the distro cannot influence it. The host only has to
supply gcc, clang, qemu and pahole.

That leaves the real criteria, and they all point the same way: four people need
*identical* environments (§7 of WORKFLOW.md makes "works on my machine" a bug),
it is already installed and working here, and most kernel and BPF documentation
assumes Debian/Ubuntu — which matters at 1am when someone is searching a verifier
error message.

Verified against this machine's package index:

| | AgentTx needs | Ubuntu 24.04 provides |
|---|---|---|
| `dwarves` (pahole) | ≥ 1.24 for 6.12 BTF | 1.25 |
| clang | ≥ 14 for CO-RE | 18 |
| gcc | any modern | 13.2 |
| qemu-system-x86 | KVM | 8.2.2 |

The alternatives, honestly:

* **Fedora 40+** is genuinely the best distro for BPF work — much upstream BPF
  development happens there and its LLVM and pahole are the newest. That
  advantage is worth close to nothing here, because we build our own kernel and
  our own bpftool. Not worth four people learning `dnf`.
* **Debian 12** ships pahole 1.24, exactly at the boundary. It works, but Ubuntu's
  1.25 has margin, and pahole is the one package whose failure mode is silent:
  no BTF, no error, and every BPF program then fails to load with a message that
  blames the program.
* **Arch** — a rolling toolchain that can change under you mid-semester is a risk
  with no compensating upside on a fixed deadline.
* **Avoid** CentOS Stream and RHEL (toolchain too old) and any non-LTS Ubuntu.

If WSL2 later proves insufficient and you want bare metal, **Ubuntu 24.04 Desktop
is the same distribution**: every script, config fragment and document in this
repo carries over unchanged. Choosing it now keeps the dual-boot option free.

**Flavours (Kubuntu, Xubuntu, Ubuntu MATE) are all fine.** They are the same
archive with a different desktop: identical package versions, `apt`, and kernel,
and `/etc/os-release` still reports `ID=ubuntu`. Nothing in `tools/` keys on the
distribution or the desktop, and the guest rootfs is debootstrapped from the
Ubuntu base whichever flavour the host runs, so the guest is identical either
way. Kubuntu's KDE idles lighter than GNOME, which is worth a little if you are
giving QEMU 8 GB on a 16 GB machine.

Note this only arises for a bare-metal install: WSL2 has no desktop, so there is
no Kubuntu-on-WSL — there, it is simply "Ubuntu".

### The one WSL2 rule that will bite you

**Never put the kernel source tree, the build output, or the VM images on
`/mnt/c`.** DrvFs/9p makes `/mnt/c` roughly an order of magnitude slower than
ext4 for the many-small-files access pattern a kernel build is made of. A build
that takes 6 minutes on `~` takes over an hour on `/mnt/c`, and `make -j12`
spends all of it in I/O wait.

Everything lives under `$HOME` inside WSL. The Windows-side copy of the repo is
for editing in an IDE and for git; it is not a build directory.

---

## 1. Tune WSL2

Create `C:\Users\ACER\.wslconfig` (Windows side, not inside WSL):

```ini
[wsl2]
memory=12GB
processors=12
swap=8GB
nestedVirtualization=true
```

Then from PowerShell:

```powershell
wsl --shutdown
```

`memory=12GB` leaves the Windows host 4 GB of a 16 GB machine. Adjust down if
Windows starts swapping — a kernel build with `-j12` wants roughly 1 GB per job
at peak, and the linker peak is the moment it will OOM if you got this wrong.
`nestedVirtualization=true` is already the Windows 11 default, but stating it
explicitly means a Windows update cannot silently take `/dev/kvm` away.

Verify after restart:

```bash
wsl -d Ubuntu -e bash -lc 'nproc; free -h | head -2; ls -l /dev/kvm'
```

If `/dev/kvm` is missing, add yourself to the `kvm` group and re-check:

```bash
sudo usermod -aG kvm "$USER"   # then: wsl --shutdown, reopen
```

---

## 2. Host packages

One command, inside WSL. Split into groups so you know what each is for.

```bash
sudo apt update && sudo apt install -y \
  build-essential bc kmod cpio rsync file zstd \
  flex bison libssl-dev libelf-dev libncurses-dev dwarves \
  clang llvm lld libbpf-dev libcap-dev pkg-config \
  qemu-system-x86 qemu-utils \
  debootstrap \
  gdb python3-dev python3-pip python3-venv python3-numpy python3-sklearn \
  git ccache
```

| Group | Why |
|---|---|
| `build-essential bc kmod cpio rsync zstd` | kernel build machinery |
| `flex bison libssl-dev libelf-dev` | kconfig, module signing, ELF |
| **`dwarves`** | provides `pahole`, which generates BTF. **Without it `CONFIG_DEBUG_INFO_BTF=y` silently fails and no BPF LSM program will load.** This is the single most common wasted evening. |
| `clang llvm lld libbpf-dev` | CO-RE BPF compilation (gcc cannot target BPF usefully) |
| `qemu-system-x86 qemu-utils` | the guest, and `qemu-img` for snapshots |
| `debootstrap` | builds the guest rootfs |
| `gdb` | attaches to the guest over `:1234` |
| `python3-numpy`, `python3-sklearn` | P4's pipeline (`make pipeline`). Install from apt, not pip: Ubuntu 24.04 marks the system Python externally-managed (PEP 668) and `python3 -m pip` is not even present |
| `ccache` | halves incremental kernel rebuild time |

`bpftool` is deliberately **not** from apt. Ubuntu's `linux-tools-*` builds
against Ubuntu's kernel, and a version skew between `bpftool` and your 6.12
guest produces confusing BTF errors. Build it from the kernel source you are
about to fetch (section 4 does this).

Check `pahole` is new enough — 6.12 needs ≥ 1.24:

```bash
pahole --version   # Ubuntu 24.04 ships 1.25. Good.
```

---

## 3. Get the repo into WSL

The Windows copy is the authoring copy. Clone it into ext4:

```bash
cd /mnt/c/Users/ACER/Desktop/os_project/ideas
git init && git add -A && git commit -m "AgentTx: scaffold"

cd ~ && git clone /mnt/c/Users/ACER/Desktop/os_project/ideas agenttx
cd ~/agenttx
```

From then on `~/agenttx` is where you build and `git push`/`pull` moves work
between the two. When the team gets a shared remote, both copies point at it and
the local path clone goes away.

`.gitattributes` forces `eol=lf` on every source file. This is not cosmetic: a
CRLF that survives into a `.sh` produces `bad interpreter: /bin/bash^M`, and one
that survives into a `.bpf.c` produces a clang error that names the wrong line.

---

## 4. Build the guest kernel

```bash
mkdir -p ~/src && cd ~/src
git clone --depth 1 --branch v6.12 \
  https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git
cd linux
```

A shallow clone is ~1.5 GB and takes a few minutes. Drop `--depth 1` only if you
intend to `git bisect` upstream, which you should not need.

### Config

Two configs, and the difference matters:

* **debug** — KASAN, lockdep, all the debug objects. This is what you develop on.
  It is 2–4× slower. Every review item in WORKFLOW.md §5 (KASAN clean, lockdep
  clean) is checked here.
* **perf** — none of that. **Every number in the paper comes from this kernel.**
  A benchmark run under KASAN measures KASAN.

Both are generated from a fragment checked into the repo, so all four of you get
byte-identical configs:

```bash
cd ~/agenttx
make vm-kernel                 # debug config, the default
make vm-kernel CONFIG=perf     # the benchmarking kernel
```

`tools/vm/build-kernel.sh` does the work; `tools/vm/kernel-debug.config` and
`kernel-perf.config` are the fragments. The non-negotiable symbols:

```
CONFIG_BPF_LSM=y                  the whole of P3 depends on this
CONFIG_DEBUG_INFO_BTF=y           CO-RE; needs pahole installed
CONFIG_DEBUG_INFO_BTF_MODULES=y   so P1-10's kfunc is visible to BPF
CONFIG_LSM="...,bpf"              bpf must be in the list or hooks never fire
CONFIG_OVERLAY_FS=y               P2's entire stream
CONFIG_USER_NS=y                  per-transaction mount namespaces
CONFIG_MODULE_UNLOAD=y            insmod/rmmod x10 is a review requirement
CONFIG_GDB_SCRIPTS=y              lx-symbols, lx-dmesg
CONFIG_RANDOMIZE_BASE=n           KASLR makes gdb breakpoints miss
```

Verify before you boot — `doctor.sh` does this automatically, but by hand:

```bash
grep -E 'BPF_LSM|DEBUG_INFO_BTF|LSM=' ~/build/linux/.config
```

### Build

```bash
make vm-kernel     # ~6-12 min cold on 12 cores, ~40 s incremental with ccache
```

Then build the matching `bpftool`:

```bash
cd ~/src/linux/tools/bpf/bpftool && make -j12 && sudo make install
bpftool version    # must report the same libbpf your kernel expects
```

---

## 5. Build the guest rootfs

```bash
cd ~/agenttx && make vm-rootfs
```

`tools/vm/build-rootfs.sh` debootstraps Ubuntu noble into a 6 GB raw image,
installs python3 and the trace tooling the harness needs, enables a serial
getty, and sets the root password to `agenttx` (it is a throwaway VM on a
loopback network; do not reuse the password anywhere).

Building it needs `sudo` for loop-mounting. It takes ~5 minutes and you do it
once. After that the image is snapshotted, not rebuilt.

**Snapshot it immediately.** WORKFLOW.md §7 asks for a snapshot before each
session, and a corrupted rootfs after a bad `rmmod` is then a 10-second rollback
instead of an evening:

```bash
qemu-img snapshot -c clean tools/vm/build/rootfs.qcow2   # take
qemu-img snapshot -a clean tools/vm/build/rootfs.qcow2   # roll back
qemu-img snapshot -l tools/vm/build/rootfs.qcow2         # list
```

---

## 6. Boot, and the fast iteration loop

```bash
make vm-boot
```

The guest boots to a serial console in your terminal. Quit with `Ctrl-a x`.

The important part of the QEMU invocation is that **the repo is shared into the
guest over 9p, not copied into the image**:

```
-virtfs local,path=$REPO,mount_tag=agenttx,security_model=none
```

In the guest:

```bash
mount -t 9p -o trans=virtio,version=9p2000.L agenttx /mnt/agenttx
```

(the rootfs `/etc/fstab` does this at boot). So the loop is:

```
  edit on Windows  ->  git push/pull, or just edit in ~/agenttx
  make module      ->  on the WSL host, fast, ext4
  in the guest:    ->  insmod /mnt/agenttx/agenttx.ko
```

No image rebuild, no reboot, no file copying. A code change reaches a running
kernel in about fifteen seconds. Protect this loop; every minute you add to it
gets multiplied by four people times a semester.

### gdb

Boot with the guest stopped at the first instruction and a gdb stub on `:1234`:

```bash
make vm-boot GDB=1      # adds -s -S
```

In a second terminal:

```bash
make vm-gdb
```

which is `gdb ~/build/linux/vmlinux -ex 'target remote :1234'` plus the
`add-auto-load-safe-path` that makes `lx-symbols`, `lx-dmesg` and `lx-ps` work.

To break inside your *module* you must tell gdb where it was loaded, because the
module is linked at load time:

```gdb
(gdb) lx-symbols /mnt/agenttx        # after insmod, in the guest
(gdb) break tx_ioctl_begin
```

`lx-symbols` is why `CONFIG_GDB_SCRIPTS=y` is in the config fragment.

---

## 7. Verify the BPF LSM is actually live

This is the gate for P3's entire stream, and it fails silently if you get it
wrong. In the guest:

```bash
# 1. bpf must appear in the active LSM list
cat /sys/kernel/security/lsm
#    expect: ...,landlock,lockdown,yama,integrity,bpf

# 2. BTF must exist
ls -l /sys/kernel/btf/vmlinux

# 3. a trivial LSM program must attach
bpftool prog list | head
```

If `bpf` is missing from the LSM list the boot parameter did not take. If
`/sys/kernel/btf/vmlinux` is missing, `pahole` was not installed when you built
the kernel — install `dwarves` and rebuild. `CONFIG_DEBUG_INFO_BTF=y` does not
fail the build when pahole is absent; it just quietly produces no BTF, and every
CO-RE program then fails to load with an error that blames the program.

Generate `vmlinux.h` for CO-RE once per kernel build:

```bash
bpftool btf dump file /sys/kernel/btf/vmlinux format c > src/bpf/vmlinux.h
```

(`src/bpf/Makefile` does this automatically; it is gitignored because it is
kernel-specific and 3 MB.)

---

## 8. Check everything at once

```bash
make doctor
```

`tools/vm/doctor.sh` checks all of the above and prints a pass/fail table. Run it
on each of the four machines in week 1 and paste the output into
`docs/journal/p<n>.md`. "Works on my machine" must be impossible (WORKFLOW.md
§7), and the way you enforce that is by making the environment itself testable.

---

## 9. The bring-up order

Do these in order. Each rung stands alone if the next one stalls.

| Week | Everyone | Then |
|---|---|---|
| 1 | sections 1–8 above; `make doctor` green; hello-LKM loads | `P*-00` done |
| 2 | review and merge `include/agenttx.h` | **contract freeze** |
| 2 | each owner writes their stub; `make test-stub` passes | Rule 2 satisfied |
| 3+ | `make STUB=1` — anyone can run the whole system alone | fragments go parallel |

Fragment `P*-00` is one week of bootcamp for all four people and nobody advances
until everyone has panicked a kernel and recovered it. That is not padding: the
first panic during integration week is a much worse place to learn what a
`RIP: 0010:` line means.

---

## 10. The week 3–4 gate needs none of this

Worth noticing, because it changes the schedule. The project gate is:

> measure what fraction of an agent's outbound network operations are
> fire-and-forget rather than request–response. >20% → proceed.

That measurement does not need the custom kernel, the module, or BPF LSM. It
needs a real agent, running under `strace -f -e trace=network`, and a script to
classify each `sendmsg`/`sendto` by whether a `recvmsg` on the same fd follows
before the next write. That runs **today**, on stock WSL2, on stock Ubuntu.

Do it in week 1 while the kernel builds. If the number comes back under 10% the
whole project's headline claim changes, and you would much rather learn that in
week 1 than week 4.

`tools/harness/measure_gate.py` is the skeleton for it.

---

## 11. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `bad interpreter: /bin/bash^M` | CRLF crossed from Windows | `.gitattributes` is in place; re-clone, or `dos2unix` the file |
| Kernel build takes an hour | source tree on `/mnt/c` | move to `~`; never build on DrvFs |
| `libbpf: failed to find BTF` | `dwarves` absent at kernel build time | `apt install dwarves`, rebuild kernel |
| LSM program loads but never fires | `bpf` not in the boot `lsm=` list | check `/sys/kernel/security/lsm` |
| gdb breakpoints never hit | KASLR | `CONFIG_RANDOMIZE_BASE=n` **and** `nokaslr` on the cmdline |
| `lx-symbols` finds no module symbols | gdb refuses to auto-load | `add-auto-load-safe-path ~/build/linux` |
| QEMU is glacial | KVM not in use | check `/dev/kvm`, confirm `-enable-kvm -cpu host` |
| `insmod: Invalid module format` | module built against a different kernel | rebuild module after every kernel rebuild |
| Benchmarks are wildly slow/noisy | benchmarking the KASAN kernel | `make vm-kernel CONFIG=perf` |
| OOM during `make -j12` | `.wslconfig` memory too low | lower to `-j8`, or raise `memory=` |
| Guest sees a stale `agenttx.ko` | 9p caching | `-virtfs ...,cache=none` (already set) |

---

## 12. What is already done, before Linux

Everything below is authored and validated on Windows and migrates by `git
clone`. See `docs/STATUS.md` for exactly which fragment each file closes.

- `include/agenttx.h` — the contract freeze (P1-01), compile-tested
- the four Day-1 stubs (P1-02, P2-01, P3-01, P4-01)
- `Makefile`, `Kbuild`, `.gitattributes`, `.gitignore`
- `tools/vm/*` — kernel config fragments, build, boot, gdb and doctor scripts
- `docs/trace-format.md` and the whole P4 userspace ML pipeline (P4-02 → P4-09),
  which runs to completion on Windows with nothing but `python3`
- `tools/bench/` — the CSV contract P1–P3 must emit into

What genuinely cannot be done before Linux: anything that compiles against
kernel headers, anything that loads a BPF program, and anything that needs a
running guest. That is the `src/core/`, `src/fs/`, `src/bpf/` and `src/policy/`
implementation bodies — which is the point of the semester, and correctly the
part you do on the real host.
