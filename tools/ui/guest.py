# SPDX-License-Identifier: GPL-2.0
"""
tools/ui/guest.py --- read live state out of the QEMU guest.

The dashboard runs on the host, where the browser is. The module runs in the
guest, because WORKFLOW.md section 7 says never test on your host kernel. So
everything the dashboard wants to show about a running transaction has to
cross that boundary, and it crosses it over the ssh port run-vm.sh already
forwards.

Every call is read-only and every one of them reports WHY it failed rather
than returning an empty result. A panel that renders 0 when it cannot reach
the guest is indistinguishable from a panel that renders 0 because there is
nothing there, and that is the failure this whole dashboard is written
against.

Owner: P4 (tooling).
"""
from __future__ import annotations

import json
import shlex
import subprocess
import time
from dataclasses import dataclass, field


@dataclass
class GuestLink:
    host: str = "127.0.0.1"
    port: int = 2222
    user: str = "root"
    password: str = "agenttx"
    repo: str = "/mnt/agenttx"
    timeout: int = 10

    last_error: str | None = None
    _ok_at: float = 0.0
    _ok: bool = False

    def _cmd(self, remote: str) -> list[str]:
        return [
            "sshpass", "-p", self.password,
            "ssh", "-p", str(self.port),
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", f"ConnectTimeout={self.timeout}",
            f"{self.user}@{self.host}", remote,
        ]

    def run(self, remote: str, timeout: int | None = None) -> tuple[int, str, str]:
        try:
            p = subprocess.run(self._cmd(remote), capture_output=True, text=True,
                               timeout=timeout or (self.timeout + 20))
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError:
            self.last_error = "sshpass not installed on the host"
            return 127, "", self.last_error
        except subprocess.TimeoutExpired:
            self.last_error = "guest did not answer in time"
            return 124, "", self.last_error

    # --- cheap liveness, cached for a second --------------------------
    def alive(self) -> bool:
        now = time.time()
        if now - self._ok_at < 1.0:
            return self._ok
        rc, _o, err = self.run("true", timeout=6)
        self._ok = rc == 0
        self._ok_at = now
        if not self._ok:
            self.last_error = err.strip() or "ssh to the guest failed"
        return self._ok

    # --- the whole live picture in ONE round trip ---------------------
    # One ssh per poll, not six. Each extra connection is ~100 ms of
    # handshake, and a dashboard that polls every second must not spend
    # most of that second reconnecting.
    SNAP_SH = r"""
set -u
R=%s
echo "---MODULE---"
lsmod 2>/dev/null | awk '$1=="agenttx"{print "loaded "$2" "$3}' || true
[ -c /dev/agenttx ] && echo "dev yes" || echo "dev no"
echo "---MODE---"
dmesg 2>/dev/null | grep -o 'loaded, abi [0-9]*, [a-z]* providers' | tail -1 || true
dmesg 2>/dev/null | grep -c 'agenttx/fs-stub:' | sed 's/^/fsstub /' || true
echo "---STAT---"
"$R/tools/harness/txctl" stat --json 2>/dev/null || echo '{}'
echo "---TXDIRS---"
for d in /var/lib/agenttx/tx-*; do
  [ -d "$d" ] || continue
  id=${d##*/tx-}
  up=$(find "$d/upper" -mindepth 1 2>/dev/null | wc -l)
  lo=$(readlink "$d/lower" 2>/dev/null || echo "-")
  echo "$id $up $lo"
done
echo "---UPPER---"
for d in /var/lib/agenttx/tx-*; do
  [ -d "$d/upper" ] || continue
  id=${d##*/tx-}
  find "$d/upper" -mindepth 1 2>/dev/null | head -60 | while read -r f; do
    rel=${f#"$d/upper/"}
    if [ -d "$f" ]; then k=dir
    elif [ -c "$f" ]; then k=whiteout
    else k=file; fi
    echo "$id $k $rel"
  done
done
echo "---LOWER---"
for d in /var/lib/agenttx/tx-*; do
  L=$(readlink "$d/lower" 2>/dev/null) || continue
  [ -d "$L" ] || continue
  id=${d##*/tx-}
  find "$L" -mindepth 1 2>/dev/null | head -60 | while read -r f; do
    rel=${f#"$L/"}
    [ -d "$f" ] && k=dir || k=file
    echo "$id $k $rel"
  done
done
echo "---MOUNTS---"
grep -c ' overlay ' /proc/mounts 2>/dev/null || echo 0
echo "---DMESG---"
dmesg 2>/dev/null | grep 'agenttx' | tail -40 || true
echo "---BPF---"
grep -q bpf /sys/kernel/security/lsm 2>/dev/null && echo "lsm yes" || echo "lsm no"
[ -r /sys/kernel/btf/agenttx ] && echo "modbtf yes" || echo "modbtf no"
[ -x "$R/src/bpf/txload" ] && echo "loader yes" || echo "loader no"
pgrep -x txload >/dev/null 2>&1 && echo "running yes" || echo "running no"
echo "---HEALTH---"
dmesg 2>/dev/null | grep -cE 'BUG:|KASAN|WARNING:|circular locking' || echo 0
echo "---END---"
""" % "%s"

    def snapshot(self) -> dict:
        out: dict = {
            "reachable": False, "error": None, "module": {}, "stat": None,
            "txdirs": [], "upper": [], "lower": [], "dmesg": [],
            "overlays": 0, "health": 0, "mode": "unknown", "bpf": {},
        }
        if not self.alive():
            out["error"] = self.last_error
            return out
        out["reachable"] = True

        script = self.SNAP_SH % shlex.quote(self.repo)
        rc, so, se = self.run("bash -s", timeout=25) if False else \
            self._run_stdin(script)
        if rc != 0 and not so:
            out["error"] = se.strip() or f"snapshot exited {rc}"
            return out

        sec, cur = {}, None
        for line in so.splitlines():
            if line.startswith("---") and line.endswith("---"):
                cur = line.strip("-")
                sec[cur] = []
                continue
            if cur:
                sec[cur].append(line)

        mod = sec.get("MODULE", [])
        out["module"] = {
            "loaded": any(l.startswith("loaded") for l in mod),
            "dev": any(l == "dev yes" for l in mod),
            "refs": next((l.split()[2] for l in mod
                          if l.startswith("loaded") and len(l.split()) > 2), "0"),
        }
        for l in sec.get("MODE", []):
            if "providers" in l:
                out["mode"] = l.split(",")[-1].strip()
            if l.startswith("fsstub"):
                out["fs_is_stub"] = l.split()[1] != "0"

        try:
            out["stat"] = json.loads("\n".join(sec.get("STAT", [])) or "{}")
        except json.JSONDecodeError:
            out["stat"] = None

        for l in sec.get("TXDIRS", []):
            f = l.split(None, 2)
            if len(f) >= 2:
                out["txdirs"].append({"tx": f[0], "upper_entries": int(f[1]),
                                      "lower": f[2] if len(f) > 2 else "-"})
        for key in ("UPPER", "LOWER"):
            for l in sec.get(key, []):
                f = l.split(None, 2)
                if len(f) == 3:
                    out[key.lower()].append({"tx": f[0], "kind": f[1], "path": f[2]})

        try:
            out["overlays"] = int((sec.get("MOUNTS") or ["0"])[0])
        except ValueError:
            pass
        for l in sec.get("BPF", []):
            k, _, v = l.partition(" ")
            out["bpf"][k] = (v == "yes")
        out["dmesg"] = sec.get("DMESG", [])
        try:
            out["health"] = int((sec.get("HEALTH") or ["0"])[0])
        except ValueError:
            pass
        return out

    def _run_stdin(self, script: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run(self._cmd("bash -s"), input=script,
                               capture_output=True, text=True, timeout=30)
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError:
            return 127, "", "sshpass not installed on the host"
        except subprocess.TimeoutExpired:
            return 124, "", "guest did not answer in time"
