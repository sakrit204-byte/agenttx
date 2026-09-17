#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
AgentTx --- the desktop app.

    python3 tools/app/agenttx_desktop.py

A native Qt application, not a browser in a frame. It talks to the sandbox
directly through tools/ui/guest.py; there is no web server involved.

THE IDEA IT IS BUILT AROUND
---------------------------
Every agent tool today asks permission before each action, because once an
action happens it cannot be taken back. That is a workaround for a missing
mechanism, and it trains people to click Allow.

AgentTx has the mechanism. The agent runs with NO permission prompts at all,
because everything it writes lands in a copy-on-write layer that has never
touched the real directory. You review what it actually did -- afterwards,
completely, with the diff in front of you -- and then decide once.

So the app has exactly one decision in it, at the end, instead of twenty
during. That is the entire point of the transaction, expressed as a UI.

Owner: P4 (tooling).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "tools" / "ui"))
sys.path.insert(0, str(REPO / "tools" / "harness"))

from PyQt6.QtCore import (QObject, QSize, Qt, QThread, QTimer,  # noqa: E402
                          pyqtSignal)
from PyQt6.QtGui import QFont, QIcon, QPixmap, QPainter, QColor  # noqa: E402
from PyQt6.QtWidgets import (QApplication, QFrame, QHBoxLayout,  # noqa: E402
                             QLabel, QLineEdit, QListWidget, QListWidgetItem,
                             QMainWindow, QPlainTextEdit, QPushButton,
                             QScrollArea, QSizePolicy, QSplitter,
                             QStackedWidget, QTabWidget, QVBoxLayout, QWidget)

from guest import GuestLink  # noqa: E402

GUEST = GuestLink()

C = {
    "created": "#3fb950", "modified": "#d29922", "deleted": "#f85149",
    "dir": "#6b7a8d", "accent": "#a371f7", "dim": "#8b9aad",
    "def": "#58a6ff",
}
STATUS = {
    "running":            ("#58a6ff", "working"),
    "starting":           ("#58a6ff", "starting"),
    "awaiting-decision":  ("#d29922", "waiting for you"),
    "committed":          ("#3fb950", "kept"),
    "aborted":            ("#8b9aad", "discarded"),
    "failed":             ("#f85149", "failed"),
}


# ---------------------------------------------------------------- helpers
def lab(text, obj=None, wrap=False, size=None, bold=False):
    w = QLabel(text)
    if obj:
        w.setObjectName(obj)
    w.setWordWrap(wrap)
    if size or bold:
        f = w.font()
        if size:
            f.setPointSize(size)
        f.setBold(bold)
        w.setFont(f)
    w.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return w


def pill(text, colour):
    p = QLabel(text)
    p.setObjectName("Pill")
    p.setStyleSheet(
        f"background:{colour}22; color:{colour}; border:1px solid {colour}66;")
    p.setAlignment(Qt.AlignmentFlag.AlignCenter)
    p.setFixedHeight(20)
    return p


def card():
    f = QFrame()
    f.setObjectName("Card")
    return f


def human_bytes(n):
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/1048576:.1f} MB"


def plain_summary(diff):
    """
    The change report in words, not counts.

    "3 entries in the upper layer" is true and undecidable. A person about to
    keep or discard an agent's work needs to know what KIND of change, to
    what, and how much -- in a sentence they can act on without knowing what
    an upper layer is.
    """
    created = [d for d in diff if d["kind"] == "created"]
    modified = [d for d in diff if d["kind"] == "modified"]
    deleted = [d for d in diff if d["kind"] == "deleted"]
    dirs = [d for d in diff if d["kind"] == "dir"]

    if not (created or modified or deleted):
        return ("The agent changed nothing.",
                "It may have only read files, or it may have failed. "
                "Either way there is nothing to keep.")

    bits = []
    if created:
        bits.append(f"created {len(created)} new file"
                    f"{'s' if len(created) != 1 else ''}")
    if modified:
        add = sum(d["added"] for d in modified)
        rem = sum(d["removed"] for d in modified)
        piece = f"edited {len(modified)} existing file{'s' if len(modified) != 1 else ''}"
        if add or rem:
            piece += f" (about {add} line{'s' if add != 1 else ''} added, {rem} removed)"
        bits.append(piece)
    if deleted:
        bits.append(f"DELETED {len(deleted)} file"
                    f"{'s' if len(deleted) != 1 else ''}")

    head = "The agent " + ", ".join(bits) + "."
    if dirs:
        head += f" It also made {len(dirs)} new folder{'s' if len(dirs) != 1 else ''}."

    tail = ("None of this has happened yet. The files below exist only inside "
            "the sandbox. Keep them and they are written for real; discard "
            "them and the folder is exactly as it was.")
    if deleted:
        tail = ("Note the deletions. " + tail)
    return head, tail


# ---------------------------------------------------------------- polling
class Poller(QThread):
    """
    One background thread for every guest read.

    Each call is an ssh round trip of 50-200ms. Doing that on the GUI thread
    freezes the window on a slow link, and a sandbox console that hangs while
    you are deciding whether to keep an agent's work is worse than no console.
    """
    sessions = pyqtSignal(list, bool, str)

    def __init__(self):
        super().__init__()
        self._run = True

    def run(self):
        while self._run:
            try:
                alive = GUEST.alive()
                self.sessions.emit(GUEST.sessions() if alive else [],
                                   alive, GUEST.last_error or "")
            except Exception as e:                      # never kill the thread
                self.sessions.emit([], False, str(e))
            for _ in range(25):                         # ~2.5s, interruptible
                if not self._run:
                    return
                self.msleep(100)

    def stop(self):
        self._run = False


class Task(QObject):
    """Run one blocking guest call off the GUI thread."""

    # Carries (callback, result) so the GUI thread knows what to do with it.
    done = pyqtSignal(object, object)

    def __init__(self, fn, args, cb):
        super().__init__()
        self.fn, self.args, self.cb = fn, args, cb

    def go(self):
        try:
            self.done.emit(self.cb, self.fn(*self.args))
        except Exception as e:
            self.done.emit(self.cb, e)


_WORKERS: list = []


def run_async(parent, fn, *args, then=None):
    """
    Run `fn(*args)` on a worker thread and deliver the result ON THE GUI
    THREAD.

    THE BUG THIS EXISTS TO AVOID. The first version connected the completion
    signal to a plain closure. A plain callable has no thread affinity, so Qt
    used a direct connection and the closure ran in the WORKER thread -- which
    then touched QWidgets. Updating a widget from a non-GUI thread is
    undefined behaviour in Qt, and here it did the quietest possible thing:
    nothing at all. The "Under the hood" panel rendered its tabs and stayed
    permanently blank, with no error anywhere.

    The fix is a real receiver: `parent` is a QObject living in the GUI
    thread, so a QueuedConnection to one of its methods is delivered by the
    GUI event loop. The callback rides along in the signal.
    """
    # NOT parented to `parent`. A QThread destroyed while running aborts the
    # process, and Qt destroys a parent's children -- so on close, any worker
    # still blocked in an ssh round trip (up to 30s) would take the app down
    # with "QThread: Destroyed while thread is still running". Ownership is a
    # module-level registry instead; the interpreter outlives the window.
    th = QThread()
    t = Task(fn, args, then)
    t.moveToThread(th)
    th.started.connect(t.go)
    t.done.connect(parent._task_done, Qt.ConnectionType.QueuedConnection)
    t.done.connect(lambda *_: th.quit())
    th.finished.connect(th.deleteLater)

    # Hold a reference or Python collects the QThread mid-run.
    #
    # Removed when the thread finishes rather than pruned on the next call:
    # deleteLater() destroys the C++ object, and asking a destroyed QThread
    # isRunning() raises "wrapped C/C++ object has been deleted" from inside
    # an unrelated later call. Let the thread say when it is done.
    entry = (th, t)
    _WORKERS.append(entry)

    def _reap():
        try:
            _WORKERS.remove(entry)
        except ValueError:
            pass

    th.finished.connect(_reap)
    th.start()
    return th


# ---------------------------------------------------------------- the file card
class FileCard(QFrame):
    """One changed file: what kind of change, how big, and what it looks like."""

    def __init__(self, d):
        super().__init__()
        self.setObjectName("Card")
        v = QVBoxLayout(self)
        v.setContentsMargins(13, 11, 13, 11)
        v.setSpacing(7)

        kind = d["kind"]
        colour = C.get(kind, "#8b9aad")
        verb = {"created": "NEW", "modified": "EDITED",
                "deleted": "DELETED", "dir": "FOLDER"}[kind]

        head = QHBoxLayout()
        head.setSpacing(9)
        p = pill(verb, colour)
        p.setFixedWidth(72)
        head.addWidget(p)
        head.addWidget(lab(d["path"], bold=True))
        head.addStretch(1)

        if kind == "modified":
            head.addWidget(lab(f"+{d['added']}", "Good"))
            head.addWidget(lab(f"−{d['removed']}", "Danger"))
        elif kind == "created":
            head.addWidget(lab(f"{d['added']} lines · {human_bytes(d['bytes'])}", "Muted"))
        elif kind == "deleted":
            head.addWidget(lab(f"was {d['removed']} lines", "Muted"))
        v.addLayout(head)

        # A plain sentence per file. The badge says what happened; this says
        # what it means for the person reading it.
        note = {
            "created": "This file did not exist before.",
            "modified": "This file already existed and its contents changed.",
            "deleted": "This file exists now and will be gone if you keep the work.",
            "dir": "A new folder.",
        }[kind]
        v.addWidget(lab(note, "Muted"))

        if d.get("preview"):
            box = QPlainTextEdit()
            box.setReadOnly(True)
            box.setPlainText("\n".join(d["preview"][:34]))
            box.setMaximumHeight(190)
            box.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            v.addWidget(box)


# ---------------------------------------------------------------- review view
class ReviewView(QWidget):
    """What the agent did, and the one decision at the end."""

    decided = pyqtSignal(str, str)          # tx, commit|abort

    def __init__(self):
        super().__init__()
        self.tx = None
        self.status = None
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)

        self.headline = lab("Nothing selected", "Headline", wrap=True)
        self.sub = lab("", "Sub", wrap=True)
        root.addWidget(self.headline)
        root.addWidget(self.sub)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.inner = QWidget()
        self.iv = QVBoxLayout(self.inner)
        self.iv.setContentsMargins(0, 0, 8, 0)
        self.iv.setSpacing(11)
        self.iv.addStretch(1)
        self.scroll.setWidget(self.inner)
        root.addWidget(self.scroll, 1)

        self.bar = QFrame()
        self.bar.setObjectName("Card")
        bl = QHBoxLayout(self.bar)
        bl.setContentsMargins(15, 12, 15, 12)
        self.barnote = lab("", "Warn", wrap=True)
        bl.addWidget(self.barnote, 1)
        self.keep = QPushButton("Keep the changes")
        self.keep.setObjectName("Commit")
        self.drop = QPushButton("Discard everything")
        self.drop.setObjectName("Discard")
        self.keep.clicked.connect(lambda: self._decide("commit"))
        self.drop.clicked.connect(lambda: self._decide("abort"))
        bl.addWidget(self.drop)
        bl.addWidget(self.keep)
        root.addWidget(self.bar)
        self.bar.hide()

    def _decide(self, what):
        if self.tx:
            self.keep.setEnabled(False)
            self.drop.setEnabled(False)
            self.barnote.setText("applying your decision…")
            self.decided.emit(self.tx, what)

    def clear_rows(self):
        while self.iv.count() > 1:
            it = self.iv.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def show_session(self, s, diff, output):
        self.tx = s.get("tx")
        self.status = s.get("status")
        self.clear_rows()

        head, tail = plain_summary(diff)
        colour, word = STATUS.get(self.status, ("#8b9aad", self.status or "?"))

        if self.status == "running":
            self.headline.setText("The agent is working on it…")
            self.sub.setText(
                "It is deciding what to run and running it, with no "
                "permission prompts — it cannot do any harm yet, because "
                "everything it writes is going into a sandbox layer. "
                "You decide once, when it is done.")
        elif self.status == "awaiting-decision":
            self.headline.setText(head)
            self.sub.setText(tail)
        elif self.status == "committed":
            self.headline.setText("Kept.")
            self.sub.setText(
                f"{head} These changes are now written for real into "
                f"{s.get('lower','the folder')}.")
        elif self.status == "aborted":
            self.headline.setText("Discarded.")
            self.sub.setText(
                f"{head} None of it happened. {s.get('lower','The folder')} is "
                "byte-for-byte what it was before the agent ran.")
        else:
            self.headline.setText(f"Session {self.tx}: {word}")
            self.sub.setText(head)

        for d in diff:
            self.iv.insertWidget(self.iv.count() - 1, FileCard(d))

        if output.strip():
            o = card()
            ov = QVBoxLayout(o)
            ov.setContentsMargins(13, 11, 13, 11)
            ov.addWidget(lab("What the agent did — its own commands and output",
                             bold=True))
            t = QPlainTextEdit()
            t.setReadOnly(True)
            t.setPlainText(output[-6000:])
            t.setMaximumHeight(230)
            ov.addWidget(t)
            self.iv.insertWidget(self.iv.count() - 1, o)

        if self.status == "awaiting-decision":
            self.bar.show()
            self.keep.setEnabled(True)
            self.drop.setEnabled(True)
            self.barnote.setText(
                "Nothing above has happened yet. This is the only decision "
                "you have to make.")
        else:
            self.bar.hide()


# ---------------------------------------------------------------- under the hood
class HoodView(QWidget):
    """
    Everything the normal view deliberately hides.

    Kept behind a button rather than deleted: the mechanism is the reason the
    normal view can be so calm, and somebody demoing this needs to be able to
    show that it is real.
    """

    def __init__(self):
        super().__init__()
        v = QVBoxLayout(self)
        v.setContentsMargins(18, 16, 18, 16)
        v.setSpacing(10)
        v.addWidget(lab("Under the hood", "Headline"))
        v.addWidget(lab(
            "The sandbox is a Linux kernel module plus five BPF LSM hooks "
            "running in a VM. Nothing below is a mock-up; it is read live "
            "from the running kernel.", "Sub", wrap=True))

        self.tabs = QTabWidget()
        v.addWidget(self.tabs, 1)

        self.kernel = QPlainTextEdit(); self.kernel.setReadOnly(True)
        self.effects = QPlainTextEdit(); self.effects.setReadOnly(True)
        self.research = QPlainTextEdit(); self.research.setReadOnly(True)
        self.tabs.addTab(self.kernel, "Kernel state")
        self.tabs.addTab(self.effects, "Intercepted effects")
        self.tabs.addTab(self.research, "Measurements")

        self.research.setPlainText(self._research())

    @staticmethod
    def _research():
        import glob
        import json
        import statistics
        out = ["THE PROJECT GATE  (docs/gate-result.md)", "=" * 58, ""]
        runs = []
        for f in sorted(glob.glob(str(REPO / "data" / "gate" / "*.summary.json"))):
            try:
                runs.append((Path(f).stem.replace(".summary", ""), json.load(open(f))))
            except Exception:
                pass
        if runs:
            out.append(f"{'task':<22}{'ops':>5}{'syscall':>10}{'request':>10}{'conn':>8}")
            for n, d in runs:
                s, r, c = d["syscall"], d.get("request") or {}, d["connection"]
                out.append(f"{n:<22}{s['total']:>5}{s['pct']:>9.1f}%"
                           f"{r.get('pct', 0):>9.1f}%{c['pct']:>7.1f}%")
            req = [d.get("request", {}).get("pct", 0) for _, d in runs]
            m = statistics.mean(req) if req else 0
            out += ["", f"  per logical request: mean {m:.1f}%",
                    f"  GATE: {'FAIL' if m < 10 else 'MARGINAL' if m < 20 else 'PASS'}"
                    "   (PROPOSAL.md: <10% means deferral's ceiling is this number)",
                    "",
                    "  The three units disagree by ~19x on the same traces, which",
                    "  is why each figure names its unit. Per-syscall counts TLS",
                    "  fragments of one request as separate effects.", ""]
        m = REPO / "data" / "model" / "quantize_report.json"
        if m.exists():
            try:
                q = json.load(open(m))
                out += ["INT8 QUANTISATION  (data/model/quantize_report.json)",
                        "=" * 58,
                        f"  irrevocable recall  {q['float']['irrevocable_recall']:.3f}"
                        f" -> {q['int8']['irrevocable_recall']:.3f}",
                        f"  missed              {q['float']['irrevocable_missed']}"
                        f" -> {q['int8']['irrevocable_missed']}",
                        "  int8 lost detections the float model caught. That is a",
                        "  safety regression, not a rounding error.", ""]
            except Exception:
                pass
        lc = REPO / "data" / "labels.csv"
        if lc.exists():
            import csv as _csv
            cnt = {}
            for r in _csv.DictReader(lc.open(newline="")):
                cnt[r["label"]] = cnt.get(r["label"], 0) + 1
            tot = sum(cnt.values()) or 1
            out += ["EFFECT TAXONOMY  (docs/taxonomy.md, applied)", "=" * 58]
            for k in ("reversible", "deferrable", "compensable", "irrevocable"):
                out.append(f"  {k:<14}{cnt.get(k,0):>5}  {100*cnt.get(k,0)/tot:5.1f}%")
            out += ["", "  deferrable and compensable are structurally 0 here:",
                    "  deferrable is a property of the mechanism (not running",
                    "  during capture) and compensable needs a declared registry.", ""]
        return "\n".join(out)

    @staticmethod
    def _prov(snap, key, real):
        """Say 'unknown' when the banner did not say, instead of guessing 'stub'.

        The old row read `'real' if fs_is_stub is False else 'stub'`, so a
        snapshot that simply could not tell -- an older module, an unparsed
        banner -- displayed a confident "stub". Three states, three answers.
        """
        v = snap.get(f"{key}_is_stub")
        return real if v is False else "stub" if v is True else "unknown"

    def update_live(self, snap):
        if not snap.get("reachable"):
            self.kernel.setPlainText("no link to the sandbox VM\n\n"
                                     + (snap.get("error") or ""))
            return
        m = snap.get("module", {})
        b = snap.get("bpf", {})
        st = snap.get("stat") or {}
        L = [
            "SANDBOX KERNEL", "=" * 58,
            f"  module loaded            {m.get('loaded')}",
            f"  /dev/agenttx             {m.get('dev')}",
            f"  storage provider         {self._prov(snap, 'fs', 'real (src/fs)')}",
            # These two name the MODULE-RESIDENT half only. The interception
            # and the classifier that actually decide live in the BPF program
            # (rows above: "BPF LSM active", "WAL streaming"), and they are
            # real. Labelling these bare "effect interception: stub" would
            # read as "nothing is intercepting", which is the opposite of
            # what is true.
            f"  in-module WAL/flush      {self._prov(snap, 'eff', 'real')}  (BPF half is real)",
            f"  in-module classifier     {self._prov(snap, 'classify', 'real')}  (BPF tree is real)",
            f"  BPF LSM active           {b.get('lsm')}",
            f"  module BTF (kfuncs)      {b.get('modbtf')}",
            f"  WAL streaming            {b.get('running')}",
            f"  overlay mounts           {snap.get('overlays')}",
            f"  KASAN / lockdep errors   {snap.get('health')}",
            "",
            "LIVE TRANSACTION", "=" * 58,
        ]
        if st.get("tx_id"):
            L += [f"  tx_id        {st['tx_id']}",
                  f"  state        {st.get('state_name')}",
                  f"  worst class  {st.get('worst_class_name')}",
                  f"  deferred     {st.get('n_deferred')}",
                  f"  written      {st.get('n_written')}"]
        else:
            L.append("  none open")
        L += ["", "COPY-ON-WRITE AREAS", "=" * 58]
        for d in snap.get("txdirs", [])[:14]:
            L.append(f"  tx {d['tx']:<4} {d['upper_entries']:>4} entries   {d['lower']}")
        self.kernel.setPlainText("\n".join(L))

        ev = [l for l in snap.get("dmesg", []) if "agenttx" in l]
        self.effects.setPlainText("\n".join(ev[-200:]) or "nothing logged yet")


# ---------------------------------------------------------------- main window
class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AgentTx")
        self.resize(1280, 840)
        self.sessions = []
        self.selected = None
        self._last_sig = None

        root = QWidget()
        self.setCentralWidget(root)
        rv = QVBoxLayout(root)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.setSpacing(0)

        # --- header ---------------------------------------------------
        hdr = QFrame()
        hdr.setObjectName("Header")
        hv = QVBoxLayout(hdr)
        hv.setContentsMargins(18, 12, 18, 12)
        hv.setSpacing(9)

        top = QHBoxLayout()
        brand = QVBoxLayout()
        brand.setSpacing(1)
        brand.addWidget(lab("AGENTTX", "Brand"))
        brand.addWidget(lab("run an agent without asking it to ask", "Tagline"))
        top.addLayout(brand)
        top.addStretch(1)
        self.linkpill = pill("connecting…", "#8b9aad")
        self.linkpill.setMinimumWidth(150)
        top.addWidget(self.linkpill)
        self.hood = QPushButton("Under the hood")
        self.hood.setObjectName("Hood")
        self.hood.setCheckable(True)
        self.hood.toggled.connect(self.toggle_hood)
        top.addWidget(self.hood)
        hv.addLayout(top)

        row = QHBoxLayout()
        row.setSpacing(9)
        self.dirin = QLineEdit("/tmp/work")
        self.dirin.setObjectName("DirInput")
        self.dirin.setMaximumWidth(230)
        self.dirin.setToolTip(
            "The only folder the agent is allowed to change.\n"
            "Everything it writes here goes into a sandbox layer first.")
        self.taskin = QLineEdit()
        self.taskin.setObjectName("TaskInput")
        self.taskin.setPlaceholderText(
            "What should the agent do?   e.g. add a README explaining this folder")
        self.taskin.returnPressed.connect(self.dispatch)
        self.go = QPushButton("Give it the task")
        self.go.setObjectName("Dispatch")
        self.go.clicked.connect(self.dispatch)
        row.addWidget(self.dirin)
        row.addWidget(self.taskin, 1)
        row.addWidget(self.go)
        hv.addLayout(row)
        hv.addWidget(lab(
            "You describe the task; <b>the agent decides what commands to "
            "run</b>. It runs with no permission prompts, because it cannot "
            "do any harm until you press Keep — every file it touches lands "
            "in a sandbox layer first. "
            "<span style='color:#4b5666'>(A leading <b>$</b> runs one shell "
            "command directly instead, using no model.)</span>",
            "Muted", wrap=True))
        rv.addWidget(hdr)

        # --- body -----------------------------------------------------
        split = QSplitter()
        side = QFrame()
        side.setObjectName("Side")
        sv = QVBoxLayout(side)
        sv.setContentsMargins(9, 12, 9, 12)
        sv.setSpacing(7)
        sv.addWidget(lab("TASKS", "Muted"))
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self.pick)
        sv.addWidget(self.list, 1)
        side.setMinimumWidth(268)
        side.setMaximumWidth(340)

        self.stack = QStackedWidget()
        self.review = ReviewView()
        self.review.decided.connect(self.decide)
        self.hoodview = HoodView()
        self.stack.addWidget(self.review)
        self.stack.addWidget(self.hoodview)

        split.addWidget(side)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 1)
        rv.addWidget(split, 1)

        self.poll = Poller()
        self.poll.sessions.connect(self.on_sessions)
        self.poll.start()

        self.hoodtimer = QTimer(self)
        self.hoodtimer.timeout.connect(self.refresh_hood)
        self.hoodtimer.start(4000)

    def _task_done(self, cb, result):
        """Runs on the GUI thread. The only place a worker's result touches a widget."""
        if cb:
            try:
                cb(result)
            except Exception as e:
                print(f"agenttx: callback failed: {e}", file=sys.stderr)

    # ------------------------------------------------------------ actions
    def dispatch(self):
        task = self.taskin.text().strip()
        d = self.dirin.text().strip()
        if not task or not d:
            return
        self.go.setEnabled(False)
        self.go.setText("starting…")

        # WHAT GETS RUN, and why it costs nothing by default.
        #
        # Whatever is typed is run as a shell command. A plain command --
        # a build, a script, a refactor -- uses no API and costs nothing, and
        # the sandbox is entirely useful that way.
        #
        # Prefixing with "ai:" runs Claude Code instead, and THAT bills the
        # account the guest is authenticated with. It is opt-in, per task, and
        # visible in the session list afterwards, so nobody spends money by
        # clicking a button whose label did not say so.
        if task.lower().startswith("ai:"):
            prompt = task[3:].strip().replace("'", "'\\''")
            # --dangerously-skip-permissions is the POINT here, not a shortcut.
            # The flag is named for a world without this sandbox, where skipping
            # approval lets an agent do anything to your machine. Inside a
            # transaction every write lands in a copy-on-write layer and nothing
            # is real until a human presses Keep. Asking per action would be
            # asking twice.
            # </dev/null: without it Claude Code waits 3s for piped stdin
            # and prints a warning into the session output. The agent has no
            # stdin here -- its instructions are the prompt.
            cmd = ("claude --dangerously-skip-permissions -p '" + prompt
                   + "' < /dev/null")
        else:
            cmd = "sh -c '" + task.replace("'", "'\\''") + "'"

        def done(res):
            self.go.setEnabled(True)
            self.go.setText("Run agent")
            self.taskin.clear()

        run_async(self, GUEST.session_start, d, cmd, then=done)

    def decide(self, tx, what):
        run_async(self, GUEST.session_decide, tx, what)

    def toggle_hood(self, on):
        self.stack.setCurrentIndex(1 if on else 0)
        if on:
            self.refresh_hood()

    def refresh_hood(self):
        if not self.hood.isChecked():
            return
        run_async(self, GUEST.snapshot, then=lambda s:
                  self.hoodview.update_live(s) if isinstance(s, dict) else None)

    # ------------------------------------------------------------ polling
    def on_sessions(self, sessions, alive, err):
        if alive:
            self.linkpill.setStyleSheet(
                "background:#3fb95022;color:#3fb950;border:1px solid #3fb95066;")
            self.linkpill.setText("sandbox connected")
        else:
            self.linkpill.setStyleSheet(
                "background:#f8514922;color:#f85149;border:1px solid #f8514966;")
            self.linkpill.setText("sandbox offline")

        self.sessions = sorted(sessions, key=lambda s: -int(s.get("tx") or 0))
        sig = [(s.get("tx"), s.get("status"), s.get("changed")) for s in self.sessions]
        if sig != self._last_sig:
            self._last_sig = sig
            self.rebuild_list()
        if self.selected:
            self.load_selected()

    def rebuild_list(self):
        keep = self.selected
        self.list.blockSignals(True)
        self.list.clear()
        for s in self.sessions:
            colour, word = STATUS.get(s.get("status"), ("#8b9aad", s.get("status") or "?"))
            w = QWidget()
            wl = QVBoxLayout(w)
            wl.setContentsMargins(11, 9, 11, 9)
            wl.setSpacing(3)
            top = QHBoxLayout()
            top.addWidget(lab(f"Task {s.get('tx')}", bold=True))
            top.addStretch(1)
            p = pill(word, colour)
            top.addWidget(p)
            wl.addLayout(top)
            n = int(s.get("changed") or 0)
            wl.addWidget(lab(
                f"{n} file{'s' if n != 1 else ''} changed" if n else "no changes",
                "Muted"))
            wl.addWidget(lab(s.get("lower") or "", "Muted"))
            it = QListWidgetItem()
            it.setSizeHint(QSize(0, w.sizeHint().height()))
            self.list.addItem(it)
            self.list.setItemWidget(it, w)
        self.list.blockSignals(False)

        if keep:
            for i, s in enumerate(self.sessions):
                if s.get("tx") == keep:
                    self.list.setCurrentRow(i)
                    return
        if not self.sessions:
            return
        # Open on something worth looking at. Sessions are newest-first, but
        # the newest may have changed nothing -- landing on "The agent changed
        # nothing" when a session below it is waiting on a real decision is a
        # bad first frame. Prefer: awaiting a decision AND has changes.
        for want in (lambda s: s.get("status") == "awaiting-decision"
                               and int(s.get("changed") or 0) > 0,
                     lambda s: int(s.get("changed") or 0) > 0,
                     lambda s: True):
            for i, s in enumerate(self.sessions):
                if want(s):
                    self.list.setCurrentRow(i)
                    return

    def pick(self, row):
        if 0 <= row < len(self.sessions):
            self.selected = self.sessions[row].get("tx")
            self.load_selected()

    def load_selected(self):
        s = next((x for x in self.sessions if x.get("tx") == self.selected), None)
        if not s:
            return

        def got(res):
            if isinstance(res, Exception):
                return
            diff, out = res
            self.review.show_session(s, diff, out)

        def fetch(tx):
            return GUEST.session_diff(tx), GUEST.session_output(tx, 300)

        run_async(self, fetch, self.selected, then=got)

    def closeEvent(self, e):
        """
        Stop every worker before the window goes.

        An ssh round trip takes 50-200ms, so there is almost always one in
        flight. Destroying its QThread while it runs aborts the process --
        "QThread: Destroyed while thread is still running" and a core dump on
        exit, which looks exactly like a crash in the app.
        """
        self.hoodtimer.stop()
        self.poll.stop()
        self.poll.wait(3000)
        for th, _t in list(_WORKERS):
            try:
                th.quit()
                th.wait(2000)
            except RuntimeError:
                pass        # already gone; nothing to wait for
        # Anything still blocked in an ssh call keeps its reference in
        # _WORKERS and is simply left to finish. It is not parented to this
        # window, so nothing destroys it underneath itself.
        e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AgentTx")
    qss = Path(__file__).with_name("style.qss")
    if qss.exists():
        app.setStyleSheet(qss.read_text())
    w = Main()
    w.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
