#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# start.sh --- bring AgentTx up from cold, and prove it works.
#
#   bash ~/Desktop/os_project/ideas/start.sh
#
# Safe to run twice. Every step checks before it acts, so re-running it
# after something died is also how you repair it.
#
# It ends by actually opening a transaction, writing a file inside it and
# throwing it away. A script that says "ready" without having transacted
# once is reporting that commands exited 0, which is not the same thing --
# the module can be loaded, the hooks attached, and the agent still unable
# to reach its model.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KBUILD_OUT="${KBUILD_OUT:-$HOME/build/linux-perf}"
SSH_PORT="${SSH_PORT:-2222}"
GUEST_PW="${GUEST_PW:-agenttx}"
WORK="${WORK:-/tmp/work}"
OLLAMA_PORT="${OLLAMA_PORT:-11434}"

if [[ -t 1 ]]; then
	R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; D=$'\033[2m'; N=$'\033[0m'
else
	R=; G=; Y=; D=; N=
fi
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$1"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$1"; }
die()  { printf '  %s✗%s %s\n' "$R" "$N" "$1"; [[ $# -gt 1 ]] && printf '    %s\n' "$2"; exit 1; }
step() { printf '\n%s%s%s\n' "$D" "$1" "$N"; }

gssh() {
	sshpass -p "$GUEST_PW" ssh -p "$SSH_PORT" \
		-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
		-o LogLevel=ERROR -o ConnectTimeout=5 root@127.0.0.1 "$@"
}

# --------------------------------------------------------------- host
step "1/6  host"

for t in qemu-system-x86_64 sshpass python3 ssh; do
	command -v "$t" >/dev/null || die "$t is not installed" \
		"sudo apt install -y qemu-system-x86 sshpass openssh-client python3"
done
ok "tools present"

[[ -r /dev/kvm && -w /dev/kvm ]] || die "/dev/kvm is not accessible" \
	"sudo usermod -aG kvm \$USER, then log out and back in"
ok "KVM available"

python3 -c 'import PyQt6' 2>/dev/null \
	&& ok "PyQt6 present (the desktop app will run)" \
	|| warn "PyQt6 missing — the CLI works, the app will not. pip install --user PyQt6"

[[ -f "$KBUILD_OUT/arch/x86/boot/bzImage" ]] \
	|| die "no kernel at $KBUILD_OUT" "make vm-kernel"
[[ -f "$REPO/tools/vm/build/rootfs.qcow2" ]] \
	|| die "no rootfs image" "make vm-rootfs"
ok "kernel and rootfs present"

# The module MUST be built against the kernel the guest boots, or insmod
# fails with "Invalid module format" -- same version string, different
# struct module size. This has cost real debugging time.
if [[ ! -f "$REPO/agenttx.ko" ]] ||
   [[ "$REPO/agenttx.ko" -ot "$KBUILD_OUT/arch/x86/boot/bzImage" ]]; then
	warn "building the module against $KBUILD_OUT"
	make -C "$REPO" module STUB=1 STUB_FS=0 KBUILD_OUT="$KBUILD_OUT" \
		>/tmp/agenttx-build.log 2>&1 \
		|| die "module build failed" "see /tmp/agenttx-build.log"
fi
ok "module built"

# --------------------------------------------------------------- model
step "2/6  model"

if ! ss -tln 2>/dev/null | grep -q ":$OLLAMA_PORT"; then
	warn "ollama is not listening — starting it"
	sudo systemctl start ollama 2>/dev/null || true
	for _ in $(seq 1 20); do
		ss -tln 2>/dev/null | grep -q ":$OLLAMA_PORT" && break
		sleep 1
	done
fi
if ss -tln 2>/dev/null | grep -q ":$OLLAMA_PORT"; then
	# It must listen on more than loopback, or the guest cannot reach it.
	if ss -tln 2>/dev/null | grep ":$OLLAMA_PORT" | grep -qE '0\.0\.0\.0|\*:'; then
		ok "ollama listening on all interfaces"
	else
		warn "ollama is bound to loopback only; the guest will not reach it"
		printf '    %s\n' "sudo systemctl edit ollama   ->   Environment=\"OLLAMA_HOST=0.0.0.0:$OLLAMA_PORT\""
	fi
	models=$(ollama list 2>/dev/null | tail -n +2 | awk '{print $1}' | paste -sd' ')
	[[ -n "${models:-}" ]] && ok "models: $models" \
		|| warn "no model pulled — ollama pull qwen2.5:7b"
else
	warn "ollama is down; local-model tasks will not run (the \$ shell path still will)"
fi

# --------------------------------------------------------------- guest
step "3/6  sandbox VM"

if gssh true 2>/dev/null; then
	ok "already up"
else
	printf '    booting…'
	SERIAL_LOG=/tmp/agenttx-vm.log KBUILD_OUT="$KBUILD_OUT" \
		setsid nohup bash "$REPO/tools/vm/run-vm.sh" >/tmp/agenttx-vm-boot.log 2>&1 &
	for _ in $(seq 1 60); do
		gssh true 2>/dev/null && break
		printf '.'; sleep 2
	done
	printf '\n'
	gssh true 2>/dev/null || die "the guest did not come up" \
		"see /tmp/agenttx-vm.log and /tmp/agenttx-vm-boot.log"
	ok "booted"
fi

# --------------------------------------------------------------- stack
step "4/6  kernel module and hooks"

python3 - "$REPO" <<'PY' || exit 1
import sys, os
sys.path.insert(0, os.path.join(sys.argv[1], "tools", "ui"))
from guest import GuestLink
g = GuestLink()
n = g.deploy()
print("  ✓ binaries deployed (%d changed)" % n)
PY

out=$(gssh 'lsmod | grep -q "^agenttx" || insmod /mnt/agenttx/agenttx.ko 2>&1' 2>&1)
if grep -qi "invalid module format" <<<"$out"; then
	die "the module does not match the running guest kernel" \
	    "make module STUB=1 STUB_FS=0 KBUILD_OUT=$KBUILD_OUT, then re-run this"
fi
gssh 'lsmod | grep -q "^agenttx"' || die "the module did not load" "$out"
ok "module loaded: $(gssh 'dmesg | grep -o "loaded, abi.*" | tail -1')"

gssh 'mountpoint -q /sys/kernel/debug || mount -t debugfs none /sys/kernel/debug'
gssh 'test -r /sys/kernel/debug/agenttx/transactions' \
	&& ok "live state readable (/sys/kernel/debug/agenttx)" \
	|| warn "debugfs view unavailable — the UI will show fewer details"

# --infra-port: the agent's own call to its model must NOT be deferred as
# if it were an effect, or the agent cannot think inside its sandbox.
gssh "pgrep -x txload >/dev/null || (setsid /usr/local/bin/txload --pin --infra-port $OLLAMA_PORT >/var/log/txload.log 2>&1 &)"
sleep 3
gssh 'pgrep -x txload >/dev/null' \
	&& ok "BPF hooks attached (model port $OLLAMA_PORT exempt)" \
	|| warn "txload is not running — effect interception is off"

gssh "mkdir -p $WORK && chown -R agent $WORK" 2>/dev/null
ok "work folder $WORK owned by the agent"

# --------------------------------------------------------------- proof
step "5/6  proving a transaction actually works"

gssh "rm -rf $WORK/.selftest && mkdir -p $WORK/.selftest && echo before > $WORK/.selftest/f.txt && chown -R agent $WORK/.selftest"
gssh "cd $WORK/.selftest && setsid /usr/local/bin/txctl session --lower $WORK/.selftest --as agent -- sh -c 'echo after > f.txt' >/tmp/selftest.log 2>&1 &" 
for _ in $(seq 1 40); do
	grep -q 'awaiting decision' /dev/null 2>/dev/null
	gssh 'grep -q "awaiting decision" /tmp/selftest.log 2>/dev/null' && break
	sleep 0.5
done
tx=$(gssh 'sed -n "s/^tx=\([0-9]*\) session started.*/\1/p" /tmp/selftest.log | tail -1')
if [[ -n "${tx:-}" ]]; then
	real=$(gssh "cat $WORK/.selftest/f.txt")
	inside=$(gssh "cat /var/lib/agenttx/tx-$tx/upper/f.txt 2>/dev/null")
	[[ "$real" == "before" ]] \
		&& ok "the write is held back (your folder still says '$real')" \
		|| warn "the real folder already says '$real' — isolation is NOT working"
	[[ "$inside" == "after" ]] \
		&& ok "and it is waiting in the layer (says '$inside')" \
		|| warn "the layer does not hold the change"
	gssh "printf abort > /run/agenttx/session-$tx/decide"; sleep 2
	[[ "$(gssh "cat $WORK/.selftest/f.txt")" == "before" ]] \
		&& ok "discard restored it byte for byte" \
		|| warn "discard did not restore the folder"
else
	warn "could not run the self-test transaction — see /tmp/selftest.log in the guest"
fi
gssh "rm -rf $WORK/.selftest"

# model reachability FROM INSIDE a transaction, which is the case that
# actually matters and the one that silently broke before.
if ss -tln 2>/dev/null | grep -q ":$OLLAMA_PORT"; then
	code=$(gssh "cd $WORK && timeout 30 /usr/local/bin/txctl run --lower $WORK --as agent -- sh -c 'curl -s -o /dev/null -w %{http_code} --max-time 10 http://10.0.2.2:$OLLAMA_PORT/api/tags' 2>/dev/null | grep -oE '^[0-9]{3}' | head -1")
	[[ "${code:-}" == "200" ]] \
		&& ok "the agent can reach its model from inside a transaction" \
		|| warn "the agent CANNOT reach the model inside a transaction (got '${code:-nothing}') — check txload --infra-port $OLLAMA_PORT"
fi

# --------------------------------------------------------------- done
step "6/6  ready"

command -v agenttx >/dev/null || {
	mkdir -p "$HOME/.local/bin"
	printf '#!/usr/bin/env bash\nexec python3 %s/tools/agenttx "$@"\n' "$REPO" \
		> "$HOME/.local/bin/agenttx"
	chmod +x "$HOME/.local/bin/agenttx"
	warn "installed agenttx to ~/.local/bin (add it to PATH if it is not there)"
}

cat <<EOF

  ${G}AgentTx is up.${N}

  agenttx status              what is running
  agenttx task "..."          give the agent a task
  agenttx task "..." -n 3     three agents at once
  agenttx sh "..."            one shell command, no model, no cost
  agenttx layers              live transactions and the wait-for graph
  agenttx open TX [FILE]      open the pending change in your editor
  agenttx keep TX | drop TX   decide
  agenttx                     the desktop app

  agenttx down                stop everything
EOF
