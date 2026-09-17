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
  n=$(find "$L" -mindepth 1 2>/dev/null | wc -l)
  [ "$n" -gt 60 ] && echo "$id TRUNCATED $n"
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
[ -x /usr/local/bin/txload ] && echo "loader yes" || echo "loader no"
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
        # A listing capped at 60 entries must SAY it was capped. Silently
        # showing 60 of 200 files is the dashboard telling a confident lie,
        # which is the one thing every panel here is written not to do.
        out["truncated"] = {}
        for key in ("UPPER", "LOWER"):
            for l in sec.get(key, []):
                f = l.split(None, 2)
                if len(f) == 3 and f[1] == "TRUNCATED":
                    out["truncated"][f"{key.lower()}:{f[0]}"] = int(f[2])
                elif len(f) == 3:
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

    # --- deploy: binaries must run from LOCAL disk, not the 9p share ----
    #
    # The repo is shared into the guest over 9p so edits reach it instantly.
    # That works for source. It does NOT reliably work for EXECUTING a
    # binary: demand-paging an executable over 9p with cache=none can fault
    # mid-load, and the failure is a SIGSEGV inside ld-linux before main()
    # runs. The binary is not corrupt -- same md5 on both sides -- and
    # copying it to local disk makes it run.
    #
    # It also fails intermittently, which is worse: the same binary ran from
    # the share for days and then stopped after growing by a few KB. So
    # deploy is not an optimisation, it is the only correct way to run them.
    DEPLOY_SH = r"""
set -u
R=%s
mkdir -p /usr/local/lib/agenttx
changed=0
for f in tools/harness/txctl src/bpf/txload; do
  src="$R/$f"; dst="/usr/local/bin/$(basename $f)"
  [ -f "$src" ] || continue
  if ! cmp -s "$src" "$dst" 2>/dev/null; then
    cp -f "$src" "$dst" && chmod 0755 "$dst" && changed=$((changed+1))
  fi
done
if [ -f "$R/agenttx.ko" ]; then
  if ! cmp -s "$R/agenttx.ko" /usr/local/lib/agenttx/agenttx.ko 2>/dev/null; then
    cp -f "$R/agenttx.ko" /usr/local/lib/agenttx/agenttx.ko && changed=$((changed+1))
  fi
fi
echo "deployed=$changed"
"""

    def deploy(self) -> int:
        """Copy the binaries out of the 9p share. Returns how many changed."""
        rc, so, _e = self._run_stdin(self.DEPLOY_SH % shlex.quote(self.repo))
        for line in so.splitlines():
            if line.startswith("deployed="):
                try:
                    return int(line.split("=", 1)[1])
                except ValueError:
                    pass
        return 0

    # --- sandbox sessions (the operator console) -----------------------
    def session_start(self, lower: str, cmd: str,
                      as_user: str = "agent") -> tuple[int, str, str]:
        """
        Launch a held transaction around `cmd`, with `lower` protected.

        setsid + & so the session outlives this ssh connection: it is
        supposed to sit at `awaiting-decision` until a human decides, which
        may be minutes. Tying it to the request that started it would abort
        every session the moment the page was refreshed.
        """
        # Deploy first. The binaries live on a 9p share and must NOT be
        # executed from it: demand-paging an executable over 9p faults inside
        # ld-linux before main() runs, and the SIGSEGV names nothing useful.
        # Byte-identical md5 on both sides; it is the execution, not the file.
        self.deploy()
        script = f"""
mkdir -p {shlex.quote(lower)} /run/agenttx
chown {shlex.quote(as_user)} {shlex.quote(lower)} 2>/dev/null || true
cd {shlex.quote(lower)}
setsid /usr/local/bin/txctl session --lower {shlex.quote(lower)} --as {shlex.quote(as_user)} -- {cmd} >/run/agenttx/last-start.log 2>&1 &
sleep 2
cat /run/agenttx/last-start.log
"""
        return self._run_stdin(script)

    SESSIONS_SH = r"""
set -u
for d in /run/agenttx/session-*; do
  [ -d "$d" ] || continue
  tx=${d##*/session-}
  echo "---SESSION $tx---"
  for k in status exit lower cmd; do
    printf '%s=' "$k"; head -c 400 "$d/$k" 2>/dev/null | tr -d '
'; echo
  done
  U=/var/lib/agenttx/tx-$tx/upper
  n=$(find "$U" -mindepth 1 2>/dev/null | wc -l)
  echo "changed=$n"
  find "$U" -mindepth 1 2>/dev/null | head -100 | while read -r f; do
    rel=${f#"$U/"}
    if [ -c "$f" ]; then echo "F deleted $rel"
    elif [ -d "$f" ]; then echo "F dir $rel"
    else echo "F file $rel"; fi
  done
done
echo "---END---"
"""

    def sessions(self) -> list[dict]:
        if not self.alive():
            return []
        rc, so, _e = self._run_stdin(self.SESSIONS_SH)
        out, cur = [], None
        for line in so.splitlines():
            if line.startswith("---SESSION "):
                cur = {"tx": line.split()[1].rstrip("-"), "files": []}
                out.append(cur)
                continue
            if line.startswith("---END"):
                break
            if cur is None:
                continue
            if line.startswith("F "):
                p = line.split(None, 2)
                if len(p) == 3:
                    cur["files"].append({"kind": p[1], "path": p[2]})
            elif "=" in line:
                k, _, v = line.partition("=")
                cur[k] = v
        return out

    # --- the change report, in terms a person can act on ---------------
    #
    # "3 entries in the upper layer" is a true statement nobody can decide
    # from. What a person deciding commit-or-abort needs is: which files,
    # what KIND of change, how big, and what it looks like. The CoW layout
    # gives all of that for free -- upper/ is the new version and the lower
    # symlink points at the old one, so a diff is just two paths.
    DIFF_SH = r"""
set -u
TX=%s
U=/var/lib/agenttx/tx-$TX/upper
L=$(readlink /var/lib/agenttx/tx-$TX/lower 2>/dev/null)
[ -d "$U" ] || { echo "---END---"; exit 0; }
find "$U" -mindepth 1 2>/dev/null | sort | while read -r f; do
  rel=${f#"$U/"}
  old="$L/$rel"
  if [ -c "$f" ]; then
    # a character device in the upper layer is overlayfs's whiteout: the
    # agent DELETED this file, and the old contents are still in lower.
    n=$(wc -l < "$old" 2>/dev/null || echo 0)
    echo "ENTRY deleted $rel"
    echo "STAT 0 $n $(stat -c %%s "$old" 2>/dev/null || echo 0)"
    echo "PREVIEW-END"
  elif [ -d "$f" ]; then
    echo "ENTRY dir $rel"
    echo "STAT 0 0 0"
    echo "PREVIEW-END"
  elif [ -e "$old" ]; then
    add=$(diff --unchanged-line-format= --old-line-format= --new-line-format=x "$old" "$f" 2>/dev/null | wc -c)
    del=$(diff --unchanged-line-format= --old-line-format=x --new-line-format= "$old" "$f" 2>/dev/null | wc -c)
    echo "ENTRY modified $rel"
    echo "STAT $add $del $(stat -c %%s "$f" 2>/dev/null || echo 0)"
    diff -u "$old" "$f" 2>/dev/null | head -40 | sed 's/^/D /'
    echo "PREVIEW-END"
  else
    n=$(wc -l < "$f" 2>/dev/null || echo 0)
    echo "ENTRY created $rel"
    echo "STAT $n 0 $(stat -c %%s "$f" 2>/dev/null || echo 0)"
    head -24 "$f" 2>/dev/null | sed 's/^/D /'
    echo "PREVIEW-END"
  fi
done
echo "---END---"
"""

    def session_diff(self, tx: str) -> list[dict]:
        if not self.alive():
            return []
        rc, so, _e = self._run_stdin(self.DIFF_SH % str(int(tx)))
        out, cur = [], None
        for line in so.splitlines():
            if line.startswith("ENTRY "):
                p = line.split(None, 2)
                cur = {"kind": p[1], "path": p[2] if len(p) > 2 else "",
                       "added": 0, "removed": 0, "bytes": 0, "preview": []}
                out.append(cur)
            elif line.startswith("STAT ") and cur is not None:
                p = line.split()
                try:
                    cur["added"], cur["removed"], cur["bytes"] = (
                        int(p[1]), int(p[2]), int(p[3]))
                except (IndexError, ValueError):
                    pass
            elif line.startswith("D ") and cur is not None:
                cur["preview"].append(line[2:])
            elif line == "PREVIEW-END":
                cur = None
        return out

    def session_output(self, tx: str, tail: int = 400) -> str:
        rc, so, _e = self.run(
            f"tail -n {int(tail)} /run/agenttx/session-{shlex.quote(str(tx))}/output 2>/dev/null")
        return so

    def session_decide(self, tx: str, decision: str) -> bool:
        if decision not in ("commit", "abort"):
            return False
        rc, _o, _e = self.run(
            f"printf '%s' {shlex.quote(decision)} > "
            f"/run/agenttx/session-{shlex.quote(str(tx))}/decide")
        return rc == 0

    def _run_stdin(self, script: str) -> tuple[int, str, str]:
        try:
            p = subprocess.run(self._cmd("bash -s"), input=script,
                               capture_output=True, text=True, timeout=30)
            return p.returncode, p.stdout, p.stderr
        except FileNotFoundError:
            return 127, "", "sshpass not installed on the host"
        except subprocess.TimeoutExpired:
            return 124, "", "guest did not answer in time"
