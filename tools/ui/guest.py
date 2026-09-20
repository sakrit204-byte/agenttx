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
dmesg 2>/dev/null | grep -oE 'loaded, abi [0-9]+, fs=[a-z]+ eff=[a-z]+ classify=[a-z]+' | tail -1 || true
dmesg 2>/dev/null | grep -c 'agenttx/fs-stub:' | sed 's/^/fsstub /' || true
echo "---STAT---"
"$R/tools/harness/txctl" stat --json 2>/dev/null || echo '{}'
echo "---TXDIRS---"
# Pick the newest N transaction directories ONCE and reuse the list below.
#
# THE BUG THIS FIXES. The three loops that follow each expanded
# /var/lib/agenttx/tx-* and ran find(1) per directory. The module creates one
# skeleton directory per transaction ever opened and never unlinks it -- abort
# and commit drain upper/ but leave the shell -- so this box had accumulated
# 13,704 of them. Three full walks over 54,869 entries took longer than the
# 30s ssh timeout, so snapshot() returned its zero-value dict and the "Under
# the hood" Kernel state tab rendered permanently blank. The panel only ever
# shows 14 rows; walking all of them was never useful.
TXD=$(ls -1dt /var/lib/agenttx/tx-* 2>/dev/null | head -20)
for d in $TXD; do
  [ -d "$d" ] || continue
  id=${d##*/tx-}
  up=$(find "$d/upper" -mindepth 1 2>/dev/null | wc -l)
  lo=$(readlink "$d/lower" 2>/dev/null || echo "-")
  echo "$id $up $lo"
done
echo "---UPPER---"
for d in $TXD; do
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
for d in $TXD; do
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
echo "---LIVE---"
# The handful of numbers the always-on strip shows while agents work.
# Counted here rather than in four separate ssh calls: this script is
# already one round trip and the strip refreshes about once a second.
# Read LIVE state out of debugfs, not out of dmesg.
#
# Counting log lines reports history, not state: an edge that was added
# and released still counts, so the number only ever goes up, and a panel
# claiming "3 wait-for edges" when there are none is worse than one that
# says nothing. /sys/kernel/debug/agenttx/{transactions,waitfor} are the
# current contents of the table and the graph. Deadlocks stay a dmesg
# count because that one genuinely IS cumulative -- a broken cycle is an
# event that happened, not a thing that is.
DBG=/sys/kernel/debug/agenttx
# `grep -c` PRINTS 0 and EXITS 1 when nothing matches, so the obvious
# `grep -c . || echo 0` emits the count twice -- once from grep, once from
# the fallback -- and the stray bare "0" shifted every line the parser read
# after it.
printf 'opentx='; { tail -n +2 "$DBG/transactions" 2>/dev/null || true; } | grep -c . | head -1
printf 'upperfiles='; find /var/lib/agenttx -mindepth 3 -path '*/upper/*' -type f 2>/dev/null | wc -l
printf 'wfg='; { tail -n +2 "$DBG/waitfor" 2>/dev/null || true; } | grep -c . | head -1
printf 'deadlocks='; dmesg 2>/dev/null | grep -c 'aborting as a deadlock victim' | head -1
# Ask the model host what it is serving.
#
# This line MUST stay inside ---LIVE---. The first version sat after the
# ---WFG--- marker, so the parser read "brain=..." as a wait-for edge and
# the panel showed no model while one was plainly loaded and answering.
#
# python3 rather than sed: the sed worked when typed at a shell and
# produced nothing from inside this script, and a JSON read that cannot be
# broken by quoting is worth the extra process.
printf 'brain='
python3 - <<'TXBRAIN_EOF' 2>/dev/null || echo
import json, urllib.request
try:
    with urllib.request.urlopen("http://10.0.2.2:11434/api/tags", timeout=3) as r:
        m = json.load(r).get("models") or []
    print(m[0].get("name", "") if m else "no model pulled")
except Exception:
    print("")
TXBRAIN_EOF
echo "---WFG---"
tail -n +2 "$DBG/waitfor" 2>/dev/null | head -40 || true
echo "---TXLIVE---"
tail -n +2 "$DBG/transactions" 2>/dev/null | head -40 || true
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
            # `reachable` means "the snapshot came back", not "the host
            # pinged". Leaving it True here rendered a panel of None rows
            # with the real error nowhere on screen.
            out["reachable"] = False
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
            # "loaded, abi 2, fs=real eff=stub classify=stub"
            if "fs=" in l:
                out["mode"] = l.split(",", 2)[-1].strip()
                for part in out["mode"].split():
                    k, _, v = part.partition("=")
                    if k in ("fs", "eff", "classify"):
                        out[f"{k}_is_stub"] = (v == "stub")
            if l.startswith("fsstub") and "fs_is_stub" not in out:
                # Fallback for a module built before the banner named each
                # provider. Counts over the whole dmesg ring, so it mixes
                # module loads -- the banner is authoritative when present.
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
        for l in sec.get("LIVE", []):
            k, _, v = l.partition("=")
            v = v.strip()
            if k in ("opentx", "upperfiles", "wfg", "deadlocks"):
                try:
                    out[{"opentx": "open_tx", "upperfiles": "upper_files",
                         "wfg": "wfg_edges", "deadlocks": "deadlocks"}[k]] = int(v or 0)
                except ValueError:
                    pass
            elif k == "brain":
                out["brain"] = v or "no local model"
        out["wfg"] = []
        for l in sec.get("WFG", []):
            f = l.split()
            if len(f) >= 4:
                out["wfg"].append({"waiter": f[0], "holder": f[1],
                                   "kind": f[2], "age_ms": f[3]})
        out["txlive"] = []
        for l in sec.get("TXLIVE", []):
            f = l.split()
            if len(f) >= 6:
                out["txlive"].append({"tx": f[0], "state": f[1],
                                      "worst": f[2], "deferred": f[3],
                                      "written": f[4], "pid": f[5]})
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
mkdir -p /usr/local/lib/agenttx /usr/local/lib/agenttx/agent
changed=0
# The agent package is plain Python, so it can be copied wholesale. It is
# still copied OUT of the 9p share rather than run from it: executing from
# 9p faults inside ld-linux for binaries, and keeping one rule for
# everything is cheaper than remembering which files are exempt.
for f in "$R"/tools/agent/*.py; do
  [ -f "$f" ] || continue
  dst="/usr/local/lib/agenttx/agent/$(basename "$f")"
  if ! cmp -s "$f" "$dst" 2>/dev/null; then
    cp -f "$f" "$dst" && chmod 0755 "$dst" && changed=$((changed+1))
  fi
done
for f in tools/harness/txctl src/bpf/txload tools/harness/tx-agent.py \
         tools/harness/tx-shell.sh; do
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
  # Heartbeat. `txctl session` holds the transaction open waiting for a
  # human, and if the window that was going to decide simply closes, it used
  # to sit out its full one-hour window with /dev/agenttx open -- which pins
  # the module, so rmmod fails with "Module agenttx is in use" and the reason
  # is three directories away. Touching this file every poll lets txctl tell
  # "nobody is watching any more" from "nobody has decided yet".
  touch "$d/watch" 2>/dev/null || true
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
    diff -u "$old" "$f" 2>/dev/null | head -40 | awk '{print "D " $0}' 
    echo "PREVIEW-END"
  else
    n=$(wc -l < "$f" 2>/dev/null || echo 0)
    echo "ENTRY created $rel"
    echo "STAT $n 0 $(stat -c %%s "$f" 2>/dev/null || echo 0)"
    head -24 "$f" 2>/dev/null | awk '{print "D " $0}' 
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


# ======================================================================
# Agent threads
# ======================================================================
#
# A THREAD is a conversation; a TRANSACTION is one turn's sandbox. They are
# deliberately not the same object.
#
# The first version of this app made them the same: one task, one
# transaction, one shell command, decide, done. That is a demo, not a
# harness. Real work is a conversation -- "add a README", then "actually
# make it shorter", then "now mention the licence" -- and each of those
# steps needs its own commit-or-discard decision while the agent keeps the
# context of everything before it.
#
# So a thread holds the Claude Code session id and the full transcript, and
# each turn opens a fresh transaction around a fresh `claude --resume`.
# Discarding turn 3 does not unwind the conversation; it unwinds turn 3's
# writes. That is the behaviour a person actually wants when they look at a
# diff and say "no, not like that" -- keep talking, drop the edit.
#
# Thread state lives in /var/lib/agenttx/threads/, which is NOT inside any
# protected lower directory. See the header of tools/harness/tx-agent.py:
# Discard must throw away the work, never the account of the work.

# NOT under /var/lib/agenttx.
#
# That directory is the transaction root and it is mode 0700 root-owned on
# purpose: it holds every transaction's upper layer, so the sandboxed user
# must not be able to walk it and read what some other transaction wrote.
# The first version put threads inside it and the agent could not even
# traverse to its own transcript. The fix is emphatically NOT to relax the
# tx root -- that trades a broken feature for a sandbox escape. Threads get
# their own root, world-traversable, with each thread directory owned by
# the agent that writes it.
THREADS_DIR = "/var/lib/agenttx-threads"


class AgentThreads:
    """Thread lifecycle, on top of a GuestLink."""

    def __init__(self, link: "GuestLink"):
        self.g = link

    # --- listing ------------------------------------------------------
    LIST_SH = r"""
set -u
for d in %s/*; do
  [ -d "$d" ] || continue
  echo "---THREAD ${d##*/}---"
  printf 'title='; head -c 300 "$d/title" 2>/dev/null | tr -d '\n'; echo
  printf 'lower='; head -c 300 "$d/lower" 2>/dev/null | tr -d '\n'; echo
  printf 'sid=';   head -c 80  "$d/session" 2>/dev/null | tr -d '\n'; echo
  printf 'turns='; cat "$d/turns" 2>/dev/null || echo 0
  printf 'created='; cat "$d/created" 2>/dev/null || echo 0
  printf 'lines='; wc -l < "$d/events.jsonl" 2>/dev/null || echo 0
  # The transaction for the most recent turn, if it is still open.
  printf 'tx='; cat "$d/tx" 2>/dev/null || echo ""
  echo
done
echo "---END---"
"""

    def list(self) -> list[dict]:
        if not self.g.alive():
            return []
        _rc, so, _e = self.g._run_stdin(self.LIST_SH % THREADS_DIR)
        out: list[dict] = []
        cur: dict | None = None
        for line in so.splitlines():
            if line.startswith("---THREAD "):
                cur = {"id": line[len("---THREAD "):].rstrip("-")}
                out.append(cur)
                continue
            if line.startswith("---END"):
                break
            if cur is not None and "=" in line:
                k, _, v = line.partition("=")
                cur[k] = v.strip()
        for t in out:
            for k in ("turns", "lines", "created"):
                try:
                    t[k] = int(t.get(k) or 0)
                except ValueError:
                    t[k] = 0
        out.sort(key=lambda t: -t.get("created", 0))
        return out

    # --- reading a transcript ----------------------------------------
    def events(self, thread_id: str, since: int = 0) -> tuple[list[dict], int]:
        """
        Return (new events, new line count) for `thread_id`.

        Incremental by line number rather than re-sending the whole
        transcript every poll. A long agent turn is a few hundred events and
        the app polls about once a second; re-reading all of it would put
        the transcript size into the poll cost, which is the kind of thing
        that works beautifully in a demo and falls over on the day it
        matters.
        """
        if not self.g.alive():
            return [], since
        path = "%s/%s/events.jsonl" % (THREADS_DIR, shlex.quote(thread_id))
        rc, so, _e = self.g.run(
            "tail -n +%d %s 2>/dev/null" % (int(since) + 1, path), timeout=20)
        if rc != 0:
            return [], since
        evs, n = [], since
        for line in so.splitlines():
            n += 1
            line = line.strip()
            if not line:
                continue
            try:
                evs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return evs, n

    # --- starting a turn ----------------------------------------------
    START_SH = r"""
set -u
TD=%(td)s
mkdir -p "$TD" /run/agenttx
chmod 0755 "$(dirname "$TD")" 2>/dev/null || true
LOWER=%(lower)s

# First turn sets up the thread; later turns only bump the counter.
if [ ! -f "$TD/created" ]; then
  date +%%s > "$TD/created"
  printf '%%s' %(title)s > "$TD/title"
  printf '%%s' "$LOWER" > "$TD/lower"
  printf '%%s' %(sid)s  > "$TD/session"
  echo 0 > "$TD/turns"
  : > "$TD/events.jsonl"
fi

# --- who the agent runs as, and whose folder this is ------------------
#
# This used to be one line: chown the protected folder to `agent`. Two
# things were wrong with that.
#
# 1. It is a real, permanent change to the user's files, made BEFORE any
#    decision. The whole promise of this system is that nothing happens
#    until you press Keep, and quietly taking ownership of somebody's
#    directory is something happening.
# 2. It did not even work. chown on the DIRECTORY lets the agent create
#    and delete entries, but the files inside keep their old owner, so
#    editing an existing root-owned file failed with "Permission denied"
#    -- while creating and deleting succeeded. A harness whose main job is
#    editing existing files silently could not edit existing files.
#
# So: run the agent as whoever already owns the folder, which needs no
# chown and gives exactly the access that user already had. Only claim
# ownership of a directory we created ourselves. If the folder belongs to
# root we cannot run there (Claude Code refuses
# --dangerously-skip-permissions as root, deliberately), so we fall back to
# the sandbox user and say plainly what will not work.
if [ -d "$LOWER" ]; then PRE=1; else PRE=0; mkdir -p "$LOWER"; fi
OWNER=$(stat -c %%U "$LOWER" 2>/dev/null || echo root)
if [ "$OWNER" != root ] && id -u "$OWNER" >/dev/null 2>&1; then
  RUNAS=$OWNER
else
  RUNAS=%(user)s
  [ "$PRE" = 0 ] && chown "$RUNAS" "$LOWER" 2>/dev/null || true
fi

# Warn about what this user cannot touch, in the transcript, before the
# agent starts -- not as a "Permission denied" three tool calls in.
# find's -writable tests the CURRENT user, so it has to run as RUNAS.
BLOCKED=$(su "$RUNAS" -s /bin/sh -c \
  "find \"$LOWER\" -maxdepth 3 -type f ! -writable 2>/dev/null | head -12" \
  2>/dev/null)
if [ -n "$BLOCKED" ]; then
  python3 - "$TD/events.jsonl" "$RUNAS" <<'TXWARN_EOF' "$BLOCKED"
import json, sys, time
ev, runas, blocked = sys.argv[1], sys.argv[2], sys.argv[3]
files = [b for b in blocked.splitlines() if b.strip()]
msg = ("Heads up: the agent runs as '%%s', and these files are not writable "
       "by it, so it can read them but any edit will fail:\n  %%s\n\n"
       "Fix with:  sudo chown -R %%s <folder>" %%
       (runas, "\n  ".join(files[:12]), runas))
with open(ev, "a") as f:
    f.write(json.dumps({"type": "tx_notice", "at": time.time(),
                        "text": msg}) + "\n")
TXWARN_EOF
fi

TURN=$(( $(cat "$TD/turns" 2>/dev/null || echo 0) + 1 ))
echo "$TURN" > "$TD/turns"
cat > "$TD/turn-$TURN.prompt" <<'TXPROMPT_EOF'
%(prompt)s
TXPROMPT_EOF
SID=$(cat "$TD/session")

# The agent must be able to write its own transcript.
chown -R "$RUNAS" "$TD" 2>/dev/null || true

cd "$LOWER"
setsid %(runner)s > /run/agenttx/last-start.log 2>&1 &
sleep 2
# Record which transaction this turn got, so the UI can put the review
# surface next to the right message instead of guessing from the newest.
TX=$(sed -n 's/^tx=\([0-9]*\) session started.*/\1/p' /run/agenttx/last-start.log | tail -1)
printf '%%s' "$TX" > "$TD/tx"
echo "turn=$TURN"
echo "tx=$TX"
cat /run/agenttx/last-start.log
"""

    # Which brain runs the turn.
    #
    # "local" is the default and the one that matters: a 7B served by
    # Ollama on the host, reached across QEMU's user network. It costs
    # nothing, runs offline, and is the configuration the system is meant
    # to be evaluated in. "claude" stays because it is the useful
    # reference point -- when the local model does something odd, the
    # question is always whether the harness is wrong or the model is
    # small, and being able to run the identical task through a strong
    # model answers it. "swarm" runs N agents, each in its own
    # transaction, against the same folder.
    AGENT_DIR = "/usr/local/lib/agenttx/agent"

    def _runner(self, mode: str, td_q: str, session_uuid: str,
                lower_q: str, agents: int, model: str | None) -> str:
        base = ('/usr/local/bin/txctl session --lower "$LOWER" '
                '--as "$RUNAS" -- ')
        m = (" --model %s" % shlex.quote(model)) if model else ""
        if mode == "claude":
            return base + '/usr/local/bin/tx-agent.py "$TD" "$TURN" %s' % (
                shlex.quote(session_uuid))
        if mode == "swarm":
            # The orchestrator is NOT itself inside a transaction: it
            # writes no files, it starts the agents that do. Wrapping it in
            # one would add an empty transaction to every swarm run and
            # make the agent count in the UI wrong by one.
            return ('/usr/bin/env python3 %s/swarm.py "$TD" "$TURN" '
                    '--lower "$LOWER" --agents %d --runas "$RUNAS"%s'
                    % (self.AGENT_DIR, int(agents), m))
        return base + '/usr/bin/env python3 %s/loop.py "$TD" "$TURN"%s' % (
            self.AGENT_DIR, m)

    def start_turn(self, thread_id: str, lower: str, prompt: str,
                   title: str, session_uuid: str,
                   as_user: str = "agent", mode: str = "local",
                   agents: int = 3, model: str | None = None
                   ) -> tuple[int, str, str]:
        self.g.deploy()
        td = "%s/%s" % (THREADS_DIR, thread_id)
        script = self.START_SH % {
            "td": shlex.quote(td),
            "lower": shlex.quote(lower),
            "user": shlex.quote(as_user),
            "title": shlex.quote(title),
            "sid": shlex.quote(session_uuid),
            "prompt": prompt,
            "runner": self._runner(mode, shlex.quote(td), session_uuid,
                                   shlex.quote(lower), agents, model),
        }
        return self.g._run_stdin(script)

    # --- a plain shell turn, no model, no cost ------------------------
    SHELL_SH = r"""
set -u
TD=%(td)s
mkdir -p "$TD" /run/agenttx
chmod 0755 "$(dirname "$TD")" 2>/dev/null || true
LOWER=%(lower)s
if [ ! -f "$TD/created" ]; then
  date +%%s > "$TD/created"
  printf '%%s' %(title)s > "$TD/title"
  printf '%%s' "$LOWER" > "$TD/lower"
  printf '%%s' %(sid)s  > "$TD/session"
  echo 0 > "$TD/turns"
  : > "$TD/events.jsonl"
fi

# --- who the agent runs as, and whose folder this is ------------------
#
# This used to be one line: chown the protected folder to `agent`. Two
# things were wrong with that.
#
# 1. It is a real, permanent change to the user's files, made BEFORE any
#    decision. The whole promise of this system is that nothing happens
#    until you press Keep, and quietly taking ownership of somebody's
#    directory is something happening.
# 2. It did not even work. chown on the DIRECTORY lets the agent create
#    and delete entries, but the files inside keep their old owner, so
#    editing an existing root-owned file failed with "Permission denied"
#    -- while creating and deleting succeeded. A harness whose main job is
#    editing existing files silently could not edit existing files.
#
# So: run the agent as whoever already owns the folder, which needs no
# chown and gives exactly the access that user already had. Only claim
# ownership of a directory we created ourselves. If the folder belongs to
# root we cannot run there (Claude Code refuses
# --dangerously-skip-permissions as root, deliberately), so we fall back to
# the sandbox user and say plainly what will not work.
if [ -d "$LOWER" ]; then PRE=1; else PRE=0; mkdir -p "$LOWER"; fi
OWNER=$(stat -c %%U "$LOWER" 2>/dev/null || echo root)
if [ "$OWNER" != root ] && id -u "$OWNER" >/dev/null 2>&1; then
  RUNAS=$OWNER
else
  RUNAS=%(user)s
  [ "$PRE" = 0 ] && chown "$RUNAS" "$LOWER" 2>/dev/null || true
fi

# Warn about what this user cannot touch, in the transcript, before the
# agent starts -- not as a "Permission denied" three tool calls in.
# find's -writable tests the CURRENT user, so it has to run as RUNAS.
BLOCKED=$(su "$RUNAS" -s /bin/sh -c \
  "find \"$LOWER\" -maxdepth 3 -type f ! -writable 2>/dev/null | head -12" \
  2>/dev/null)
if [ -n "$BLOCKED" ]; then
  python3 - "$TD/events.jsonl" "$RUNAS" <<'TXWARN_EOF' "$BLOCKED"
import json, sys, time
ev, runas, blocked = sys.argv[1], sys.argv[2], sys.argv[3]
files = [b for b in blocked.splitlines() if b.strip()]
msg = ("Heads up: the agent runs as '%%s', and these files are not writable "
       "by it, so it can read them but any edit will fail:\n  %%s\n\n"
       "Fix with:  sudo chown -R %%s <folder>" %%
       (runas, "\n  ".join(files[:12]), runas))
with open(ev, "a") as f:
    f.write(json.dumps({"type": "tx_notice", "at": time.time(),
                        "text": msg}) + "\n")
TXWARN_EOF
fi

TURN=$(( $(cat "$TD/turns" 2>/dev/null || echo 0) + 1 ))
echo "$TURN" > "$TD/turns"
cat > "$TD/turn-$TURN.cmd" <<'TXCMD_EOF'
%(cmd)s
TXCMD_EOF
chown -R "$RUNAS" "$TD" 2>/dev/null || true
cd "$LOWER"
setsid /usr/local/bin/txctl session --lower "$LOWER" --as "$RUNAS" -- \
    /usr/local/bin/tx-shell.sh "$TD" "$TURN" \
    > /run/agenttx/last-start.log 2>&1 &
sleep 2
TX=$(sed -n 's/^tx=\([0-9]*\) session started.*/\1/p' /run/agenttx/last-start.log | tail -1)
printf '%%s' "$TX" > "$TD/tx"
echo "turn=$TURN"
echo "tx=$TX"
cat /run/agenttx/last-start.log
"""

    def start_shell(self, thread_id: str, lower: str, cmd: str, title: str,
                    as_user: str = "agent") -> tuple[int, str, str]:
        self.g.deploy()
        script = self.SHELL_SH % {
            "td": shlex.quote("%s/%s" % (THREADS_DIR, thread_id)),
            "lower": shlex.quote(lower),
            "user": shlex.quote(as_user),
            "title": shlex.quote(title),
            "sid": shlex.quote("-"),
            "cmd": cmd,
        }
        return self.g._run_stdin(script)
