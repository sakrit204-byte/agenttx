#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/ui/server.py --- the AgentTx harness dashboard.

    python3 tools/ui/server.py            # then open http://127.0.0.1:8420

WHAT IT SHOWS, AND WHERE EACH NUMBER COMES FROM
-----------------------------------------------
Nothing on this dashboard is invented.  Every panel names its source, and a
panel whose source is missing says so rather than rendering a plausible
zero -- a dashboard that cannot distinguish "no effects" from "not
connected" is the same failure STATUS.md section 6 is about, drawn in
colour.

    pipeline    data/model/report.json, quantize_report.json, model.bin
    gate        data/gate/*.jsonl                       (measured, week 1)
    live tx     ioctl(TX_IOC_STAT) on /dev/agenttx      (needs the module)
    events      dmesg lines matching `agenttx:`         (needs the module)
    replay      a recorded trace streamed through the classifier, so the
                flow is visible before any kernel code exists

Stdlib only: http.server plus Server-Sent Events.  No pip, no websockets,
no build step.  It has to run on four machines and in a QEMU guest, and
every dependency is a thing that can be missing on one of them.

Owner: P4 (tooling).
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import queue
import re
import shlex
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contract import CONTRACT as C, REPO            # noqa: E402

# The concurrency model (docs/deadlock.md).  Imported rather than shelled
# out to, so the dashboard and `make deadlock` can never drift apart: there
# is one implementation of the semantics and both render the same events.
sys.path.insert(0, str(REPO / "tools" / "harness"))
try:
    import deadlock as DL                           # noqa: E402
    DL_ERR = None
except Exception as _e:                             # pragma: no cover
    DL, DL_ERR = None, str(_e)

from guest import GuestLink                         # noqa: E402

GUEST = GuestLink()

STATIC = Path(__file__).resolve().parent / "static"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ---------------------------------------------------------------------
# Event hub: one queue per connected browser.
# ---------------------------------------------------------------------
class Hub:
    def __init__(self, backlog: int = 400) -> None:
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._backlog: list[dict] = []
        self._max = backlog

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            for ev in self._backlog:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    break
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, ev: dict) -> None:
        ev.setdefault("t", now_iso())
        with self._lock:
            self._backlog.append(ev)
            if len(self._backlog) > self._max:
                self._backlog.pop(0)
            dead = []
            for q in self._subs:
                try:
                    q.put_nowait(ev)
                except queue.Full:
                    dead.append(q)          # a browser that stopped reading
            for q in dead:
                self._subs.remove(q)


HUB = Hub()


# ---------------------------------------------------------------------
# /dev/agenttx
# ---------------------------------------------------------------------
class TxDevice:
    """TX_IOC_STAT against the real module, or a clear 'absent'."""

    def __init__(self, path: str = "/dev/agenttx") -> None:
        self.path = path
        self.fd: int | None = None
        self.error: str | None = None
        self.abi: int | None = None
        self.open()

    def open(self) -> None:
        if self.fd is not None:
            return
        if not os.path.exists(self.path):
            self.error = f"{self.path} does not exist -- module not loaded"
            return
        try:
            self.fd = os.open(self.path, os.O_RDWR)
        except PermissionError:
            self.error = f"{self.path} exists but is not readable (mode 0600; run as root)"
            return
        except OSError as e:
            self.error = f"{self.path}: {e}"
            return
        try:
            buf = fcntl.ioctl(self.fd, C.IOC_ABI, struct.pack("<I", 0))
            self.abi = struct.unpack("<I", buf)[0]
            if self.abi != C.abi:
                self.error = (f"ABI mismatch: module says {self.abi}, "
                              f"header says {C.abi}")
            else:
                self.error = None
        except OSError as e:
            self.error = f"TX_IOC_ABI failed: {e}"

    @property
    def present(self) -> bool:
        return self.fd is not None and self.error is None

    def stat(self, tx_id: int = 0) -> dict | None:
        if not self.present:
            return None
        packed = struct.pack(C.STAT_FMT, C.abi, 0, tx_id, 0, 0, 0, 0, 0, 0, 0)
        try:
            out = fcntl.ioctl(self.fd, C.IOC_STAT, packed)
        except OSError:
            return None
        (abi, _p, txid, state, worst, ndef, nwr, dl, pid, _p2) = \
            struct.unpack(C.STAT_FMT, out)
        return {
            "tx_id": txid,
            "state": state,
            "state_name": C.state_names[state] if state < len(C.state_names) else "?",
            "worst_class": worst,
            "worst_class_name": C.class_names[worst] if worst < len(C.class_names) else "?",
            "n_deferred": ndef,
            "n_written": nwr,
            "deadline_ns": dl,
            "owner_pid": pid,
        }


# ---------------------------------------------------------------------
# dmesg -> events
# ---------------------------------------------------------------------
class DmesgSource(threading.Thread):
    """
    Follows the kernel log and turns `agenttx:` lines into dashboard events.

    The module's pr_info() lines are already structured (tx=%llu VERB ...),
    so the parsing here is deliberately thin: the kernel is the source of
    truth about what happened, and inventing structure in the UI that the
    kernel did not emit would let the picture drift from the system.
    """
    LINE = re.compile(r"agenttx(?:/\w+)?: (?P<body>.*)$")
    TXLINE = re.compile(
        r"tx=(?P<tx>\d+)\s+(?P<verb>BEGIN|COMMIT|ABORT|DOOMED)\b(?P<rest>.*)")

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.status = "starting"

    def run(self) -> None:
        # kernel.dmesg_restrict=1 is the Ubuntu default, so an unprivileged
        # dmesg --follow exits immediately.  Try direct first (works in the
        # QEMU guest, where we are root), then a non-interactive sudo.  If
        # neither works we say so -- an empty log panel that cannot explain
        # itself is worse than no panel.
        p = None
        for cmd in (["dmesg", "--follow", "--color=never"],
                    ["sudo", "-n", "dmesg", "--follow", "--color=never"]):
            try:
                cand = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
            except FileNotFoundError:
                continue
            time.sleep(0.4)
            if cand.poll() is None:          # still alive: it works
                p = cand
                break
            cand.wait()
        if p is None:
            self.status = ("no kernel log access -- "
                           "sysctl kernel.dmesg_restrict=0, or run as root")
            return
        if p.stdout is None:
            self.status = "dmesg produced no output"
            return
        self.status = "following"
        for raw in p.stdout:
            m = self.LINE.search(raw)
            if not m:
                continue
            body = m.group("body").strip()
            ev = {"kind": "kmsg", "text": body, "raw": raw.rstrip()}
            tm = self.TXLINE.search(body)
            if tm:
                ev["kind"] = "tx"
                ev["tx_id"] = int(tm.group("tx"))
                ev["verb"] = tm.group("verb")
                ev["detail"] = tm.group("rest").strip()
            elif "LEAK" in body:
                ev["kind"] = "leak"
            elif "refused" in body or "PARTIAL COMMIT" in body:
                ev["kind"] = "alarm"
            HUB.publish(ev)
        self.status = "dmesg exited"


# ---------------------------------------------------------------------
# Replay: make the flow visible with no kernel at all.
# ---------------------------------------------------------------------
class ReplaySource(threading.Thread):
    """
    Streams a recorded trace through the four-class taxonomy so the
    pipeline animates today.

    IMPORTANT, and surfaced in the UI itself: replay classifications come
    from the same rule table synth.py uses, not from the in-kernel model.
    They show the SHAPE of the system, never its accuracy.  The banner in
    the UI says so on every replay event, for the same reason train.py
    prints a ten-line banner: a demo that looks like a measurement will
    eventually be quoted as one.
    """

    def __init__(self, path: Path, rate: float = 6.0, loop: bool = True) -> None:
        super().__init__(daemon=True)
        self.path, self.rate, self.loop = path, rate, loop
        self.enabled = threading.Event()
        self.status = "idle"

    @staticmethod
    def classify(rec: dict) -> tuple[int, int, int]:
        """(class, confidence, verdict) -- the static-rule baseline, P3-06."""
        hook = rec.get("hook", "")
        dport = rec.get("dport") or 0
        awaits = rec.get("awaits_reply")
        daddr = rec.get("daddr") or ""
        loopback = daddr.startswith("127.") or daddr == "::1"

        if hook in ("file_open", "inode_rename"):
            return C.classes["TX_REVERSIBLE"], 240, 1        # captured
        if hook == "inode_unlink":
            return C.classes["TX_REVERSIBLE"], 210, 1
        if hook == "bprm_check":
            return C.classes["TX_IRREVOCABLE"], 200, 4       # escalated
        if hook in ("socket_sendmsg", "socket_connect", "tls_write"):
            if loopback:
                return C.classes["TX_REVERSIBLE"], 230, 0
            if awaits is True:
                # blocks on a reply: deferral is not available
                return C.classes["TX_COMPENSABLE"], 190, 3   # emitted
            if awaits is False:
                return C.classes["TX_DEFERRABLE"], 205, 2    # deferred
            return C.classes["TX_IRREVOCABLE"], 120, 4       # undetermined -> fail closed
        return C.classes["TX_REVERSIBLE"], 180, 0

    def run(self) -> None:
        while True:
            self.enabled.wait()
            if not self.path.exists():
                self.status = f"no trace at {self.path}"
                time.sleep(2)
                continue
            recs = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]
            self.status = f"replaying {len(recs)} records from {self.path.name}"
            seq = 0
            for rec in recs:
                if not self.enabled.is_set():
                    break
                klass, conf, verdict = self.classify(rec)
                # The fail-closed rule, applied exactly as tx_class_final()
                # applies it in the kernel.  Same threshold, same direction.
                final = klass if conf >= C.confidence_min else C.classes["TX_IRREVOCABLE"]
                seq += 1
                HUB.publish({
                    "kind": "effect",
                    "synthetic": True,
                    "seq": seq,
                    "hook": rec.get("hook"),
                    "syscall": rec.get("syscall"),
                    "daddr": rec.get("daddr"),
                    "dport": rec.get("dport"),
                    "payload_len": rec.get("payload_len"),
                    "awaits_reply": rec.get("awaits_reply"),
                    "klass": final,
                    "klass_name": C.class_names[final],
                    "raw_klass": klass,
                    "confidence": conf,
                    "escalated_by_threshold": final != klass,
                    "verdict": verdict,
                    "verdict_name": C.verdict_names[verdict],
                })
                time.sleep(1.0 / max(self.rate, 0.1))
            if not self.loop:
                self.enabled.clear()
                self.status = "replay finished"


# ---------------------------------------------------------------------
# Snapshot: everything the page needs on load.
# ---------------------------------------------------------------------
def read_json(p: Path):
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def gate_files() -> list[Path]:
    d = REPO / "data" / "gate"
    return sorted(d.glob("*.jsonl")) if d.is_dir() else []


def gate_summary() -> dict:
    """
    Read the summary measure_gate.py wrote.  Do NOT recompute it here.

    An earlier version of this function derived the connection-level figure
    from the JSONL records and got 50% where the trace says 5.6%, because
    the records contain only OUTBOUND operations -- whether a connection
    ever read a byte back is simply not in them.  The tool that parsed the
    trace is the only thing that knows, so it computes both figures and
    writes them out, and the dashboard displays what it was told.

    A run with no summary is reported as such rather than estimated.
    """
    out = {"runs": [], "available": False, "unsummarised": []}
    d = REPO / "data" / "gate"
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.jsonl")):
        sm = f.with_suffix("").with_suffix(".summary.json")
        if not sm.exists():
            sm = f.parent / (f.name.replace(".jsonl", ".summary.json"))
        data = read_json(sm)
        if not data:
            out["unsummarised"].append(f.name)
            continue
        out["runs"].append({
            "file": f.name,
            "total": data["syscall"]["total"],
            "lines_parsed": data.get("lines_parsed"),
            "lines_total": data.get("lines_total"),
            "syscall": data["syscall"],
            "connection": data["connection"],
            # The unit that means "one effect" (docs/gate-result.md). Absent
            # from summaries written before it existed, so the panel must
            # cope with None rather than render a confident 0.
            "request": data.get("request"),
            "stitched": data.get("lines_stitched", 0),
            "unaccounted": data.get("lines_unaccounted"),
            "verdict": data.get("verdict"),
            "trustworthy": data.get("trustworthy", False),
        })
        out["available"] = True
    return out


def snapshot(dev: TxDevice, dmesg: DmesgSource, replay: ReplaySource) -> dict:
    model_meta = read_json(REPO / "data" / "model" / "model.bin.meta.json")
    blob = REPO / "data" / "model" / "model.bin"
    return {
        "t": now_iso(),
        "contract": {
            "abi": C.abi,
            "confidence_min": C.confidence_min,
            "confidence_min_pct": round(100 * C.confidence_min / 255, 1),
            "n_features": C.n_features,
            "class_names": C.class_names,
            "state_names": C.state_names,
            "hook_names": C.hook_names,
            "verdict_names": C.verdict_names,
            "feature_names": [k.replace("TX_FEAT_", "").lower()
                              for k in C.features if k != "TX_N_FEATURES"],
        },
        "device": {
            "present": dev.present,
            "path": dev.path,
            "abi": dev.abi,
            "error": dev.error,
            "stat": dev.stat() if dev.present else None,
        },
        "sources": {
            "dmesg": dmesg.status,
            "replay": replay.status,
            "replay_on": replay.enabled.is_set(),
        },
        "train": read_json(REPO / "data" / "model" / "report.json"),
        "quantize": read_json(REPO / "data" / "model" / "quantize_report.json"),
        "model": {
            "meta": model_meta,
            "blob_bytes": blob.stat().st_size if blob.exists() else None,
        },
        "gate": gate_summary(),
    }


# ---------------------------------------------------------------------
# Demos: run the real thing in the guest and report before/during/after.
#
# These are not animations of a story. Each one drives the actual module
# through ssh and reports what the filesystem actually contained at three
# moments. If the kernel is broken the demo shows it broken.
# ---------------------------------------------------------------------
DEMO_LOWER = "/tmp/agenttx-demo"

DEMO_SETUP = r"""
rm -rf %(L)s && mkdir -p %(L)s/sub
printf 'original\n' > %(L)s/keep.txt
printf 'doomed\n'   > %(L)s/delete_me.txt
printf 'untouched\n' > %(L)s/sub/old.txt
"""

DEMO_AGENT = r"""
printf 'created\n' > %(L)s/new.txt
printf 'changed\n' > %(L)s/keep.txt
rm -f %(L)s/delete_me.txt
mkdir -p %(L)s/sub/deeper && printf 'nested\n' > %(L)s/sub/deeper/x.txt
echo '===AGENT-VIEW==='
cd %(L)s && find . -mindepth 1 | sort
echo '===UPPER-LAYER==='
U=/var/lib/agenttx/tx-$AGENTTX_TX_ID/upper
find "$U" -mindepth 1 2>/dev/null | sort | while read -r f; do
  rel=${f#"$U/"}
  if [ -c "$f" ]; then echo "whiteout $rel"
  elif [ -d "$f" ]; then echo "dir $rel"
  else echo "file $rel"; fi
done
echo '===LOWER-UNDERNEATH==='
find /var/lib/agenttx/tx-$AGENTTX_TX_ID/lower/ -mindepth 1 2>/dev/null |   sed "s|/var/lib/agenttx/tx-$AGENTTX_TX_ID/lower||" | sort
echo '===END-AGENT==='
%(VERDICT)s
"""

DEMOS = {
    "abort":  {"verdict": "false",
               "title": "verification FAILS -> abort",
               "blurb": "The agent writes, modifies and deletes. Verification "
                        "fails, so none of it ever happened."},
    "commit": {"verdict": "true",
               "title": "verification PASSES -> commit",
               "blurb": "The same writes, but the tests pass, so the "
                        "supervisor commits and the upper layer merges down."},
}


def _tree(lines: list[str], typed: bool = False) -> list[dict]:
    """Parse a find(1) listing.  `typed` lines are `<kind> <path>`."""
    out = []
    for l in lines:
        l = l.strip()
        if not l or l == ".":
            continue
        if typed and " " in l:
            kind, _, pth = l.partition(" ")
            out.append({"kind": kind, "path": pth.lstrip("./")})
        else:
            out.append({"kind": "file", "path": l.lstrip("./")})
    return out


def run_demo(name: str) -> dict:
    d = DEMOS[name]
    L = DEMO_LOWER
    if not GUEST.alive():
        return {"error": GUEST.last_error or "guest unreachable", "name": name}

    subst = {"L": L, "VERDICT": d["verdict"]}
    script = (DEMO_SETUP % subst) + f"""
echo '===BEFORE==='
cd {L} && find . -mindepth 1 | sort
echo '===RUN==='
{GUEST.repo}/tools/harness/txctl run --lower {L} -- bash -c {shlex.quote(DEMO_AGENT % subst)} 2>&1
echo '===AFTER==='
cd {L} && find . -mindepth 1 | sort
echo '===CONTENT==='
for f in keep.txt new.txt delete_me.txt sub/old.txt sub/deeper/x.txt; do
  if [ -f "{L}/$f" ]; then printf '%s = %s\n' "$f" "$(cat {L}/$f)"
  else printf '%s = <absent>\n' "$f"; fi
done
echo '===KMSG==='
dmesg | grep agenttx | tail -14   # a TAIL, and the panel says so
echo '===DONE==='
"""
    rc, so, se = GUEST._run_stdin(script)
    sec, cur = {}, None
    for line in so.splitlines():
        if line.startswith("===") and line.endswith("==="):
            cur = line.strip("=")
            sec[cur] = []
            continue
        if cur is not None:
            sec[cur].append(line)

    run = sec.get("RUN", [])
    content = {}
    for l in sec.get("CONTENT", []):
        if " = " in l:
            k, _, v = l.partition(" = ")
            content[k.strip()] = v.strip()

    return {
        "name": name,
        "title": d["title"],
        "blurb": d["blurb"],
        "rc": rc,
        "error": (se.strip() or None) if rc not in (0, 1) else None,
        "before": _tree(sec.get("BEFORE", [])),
        "agent_view": _tree(sec.get("AGENT-VIEW", [])),
        "upper": _tree(sec.get("UPPER-LAYER", []), typed=True),
        "lower_underneath": _tree(sec.get("LOWER-UNDERNEATH", [])),
        "after": _tree(sec.get("AFTER", [])),
        "content": content,
        "log": [l for l in run if l.strip()],
        "kmsg": sec.get("KMSG", []),
    }


# ---------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "agenttx-dash"

    def log_message(self, fmt, *args):      # quiet; the dashboard is the log
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                    # noqa: N802
        path = self.path.split("?", 1)[0]
        srv = self.server                                # type: ignore[attr-defined]

        if path == "/":
            f = STATIC / "index.html"
            if not f.exists():
                self._send(500, b"static/index.html missing", "text/plain")
                return
            self._send(200, f.read_bytes(), "text/html; charset=utf-8")
            return

        if path.startswith("/static/"):
            name = path[len("/static/"):]
            f = (STATIC / name).resolve()
            if not str(f).startswith(str(STATIC.resolve())) or not f.exists():
                self._send(404, b"not found", "text/plain")
                return
            ctype = {"js": "application/javascript", "css": "text/css"}.get(
                f.suffix.lstrip("."), "application/octet-stream")
            self._send(200, f.read_bytes(), ctype + "; charset=utf-8")
            return

        if path == "/api/snapshot":
            srv.dev.open()
            body = json.dumps(snapshot(srv.dev, srv.dmesg, srv.replay)).encode()
            self._send(200, body, "application/json")
            return

        if path == "/api/labels":
            import csv as _csv
            out = {"available": False, "counts": {}, "rules": {}, "total": 0}
            f = REPO / "data" / "labels.csv"
            if f.exists():
                for r in _csv.DictReader(f.open(newline="")):
                    lab = r.get("label") or "?"
                    out["counts"][lab] = out["counts"].get(lab, 0) + 1
                    why = r.get("rule") or "?"
                    out["rules"][why] = out["rules"].get(why, 0) + 1
                    out["total"] += 1
                out["available"] = out["total"] > 0
            self._send(200, json.dumps(out).encode(), "application/json")
            return

        if path == "/api/fragments":
            rows, counts = [], {}
            m = REPO / "tracker" / "master.csv"
            if m.exists():
                import csv as _csv
                for r in _csv.DictReader(m.open(newline="")):
                    rows.append({"id": r.get("id"), "title": r.get("title"),
                                 "status": r.get("status"),
                                 "stream": r.get("stream")})
                    counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
            self._send(200, json.dumps({"rows": rows, "counts": counts}).encode(),
                       "application/json")
            return

        if path == "/api/live":
            self._send(200, json.dumps(GUEST.snapshot()).encode(),
                       "application/json")
            return

        if path.startswith("/api/demo"):
            q = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    k, _, v = kv.partition("=")
                    q[k] = v
            name = q.get("name", "abort")
            if name not in DEMOS:
                self._send(404, b'{"error":"no such demo"}', "application/json")
                return
            self._send(200, json.dumps(run_demo(name)).encode(),
                       "application/json")
            return

        if path.startswith("/api/deadlock"):
            if DL is None:
                self._send(500, json.dumps({"error": DL_ERR}).encode(),
                           "application/json")
                return
            q = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    k, _, v = kv.partition("=")
                    q[k] = v
            name = q.get("scenario", "subagents")
            policy = q.get("policy", "least-severe")
            if name not in DL.SCENARIOS:
                self._send(404, b'{"error":"no such scenario"}',
                           "application/json")
                return
            w = DL.World(policy=policy, seed=1)
            DL.SCENARIOS[name]["fn"](w)
            body = json.dumps({
                "scenario": name,
                "policy": policy,
                "blurb": DL.SCENARIOS[name]["blurb"],
                "teaches": DL.SCENARIOS[name]["teaches"],
                "scenarios": {k: v["blurb"] for k, v in DL.SCENARIOS.items()},
                "policies": ["youngest", "least-work", "least-severe", "random"],
                "events": w.events,
                "txs": [{"tx_id": t.tx_id, "agent": t.agent,
                         "state": t.state.name.lower(),
                         "worst": int(t.worst), "parent": t.parent,
                         "doomed": t.doomed, "abortable": t.abortable,
                         "cost": t.cost()}
                        for t in w.txs.values()],
            }).encode()
            self._send(200, body, "application/json")
            return

        if path == "/api/replay/on":
            srv.replay.enabled.set()
            self._send(200, b'{"ok":true}', "application/json")
            return

        if path == "/api/replay/off":
            srv.replay.enabled.clear()
            self._send(200, b'{"ok":true}', "application/json")
            return

        if path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            q = HUB.subscribe()
            try:
                while True:
                    try:
                        ev = q.get(timeout=15)
                        payload = json.dumps(ev)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")   # keep proxies happy
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                HUB.unsubscribe(q)
            return

        self._send(404, b"not found", "text/plain")


def poller(dev: TxDevice, period: float = 1.0):
    """Publish a live tx stat whenever it changes. Cheap, and it is the only
    thing that can show a transaction sitting in COMMITTING."""
    last = None
    while True:
        dev.open()
        st = dev.stat() if dev.present else None
        if st != last:
            HUB.publish({"kind": "stat", "stat": st, "present": dev.present})
            last = st
        time.sleep(period)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8420)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--replay", default=str(REPO / "data" / "gate" / "run01-claude-code.jsonl"))
    ap.add_argument("--replay-rate", type=float, default=6.0)
    ap.add_argument("--no-dmesg", action="store_true")
    args = ap.parse_args()

    dev = TxDevice()
    dmesg = DmesgSource()
    if not args.no_dmesg:
        dmesg.start()
    else:
        dmesg.status = "disabled (--no-dmesg)"
    replay = ReplaySource(Path(args.replay), args.replay_rate)
    replay.start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.dev, httpd.dmesg, httpd.replay = dev, dmesg, replay   # type: ignore[attr-defined]

    threading.Thread(target=poller, args=(dev,), daemon=True).start()

    print(f"AgentTx dashboard  http://{args.host}:{args.port}")
    print(f"  device   {dev.path}: {'present' if dev.present else dev.error}")
    print(f"  dmesg    {dmesg.status}")
    print(f"  replay   {args.replay}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
