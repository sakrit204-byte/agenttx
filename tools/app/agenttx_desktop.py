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

import json
import os
import sys
import time
import uuid
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
                             QComboBox, QStackedWidget, QTabWidget,
                             QTextEdit, QVBoxLayout, QWidget)

from guest import AgentThreads, GuestLink  # noqa: E402

GUEST = GuestLink()
THREADS = AgentThreads(GUEST)

C = {
    "created": "#3fb950", "modified": "#d29922", "deleted": "#f85149",
    "dir": "#6b7a8d", "accent": "#a371f7", "dim": "#8b9aad",
    "def": "#58a6ff",
}
STATUS = {
    "running":            ("#58a6ff", "working"),
    "starting":           ("#58a6ff", "starting"),
    "awaiting-decision":  ("#d29922", "decide"),
    "committed":          ("#3fb950", "kept"),
    "aborted":            ("#8b9aad", "discarded"),
    "failed":             ("#f85149", "failed"),
    "closed":             ("#6b7a8d", "done"),
}


# ---------------------------------------------------------------- helpers
def lab(text, obj=None, wrap=False, size=None, bold=False, selectable=True):
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
    # Selectable text CONSUMES mouse events.
    #
    # The task list is built from QLabels inside a QListWidget item
    # widget, and a selectable QLabel swallows the press before it ever
    # reaches the item -- so clicking a task in the sidebar did nothing at
    # all and the only way to change conversation was to wait for the
    # poller to pick a different one. Selecting text matters in the
    # transcript, where people copy an agent's explanation; it is useless
    # on a three-line list row.
    w.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse if selectable
        else Qt.TextInteractionFlag.NoTextInteraction)
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
    tick = pyqtSignal(list, list, bool, str)    # threads, sessions, alive, err

    def __init__(self):
        super().__init__()
        self._run = True

    def run(self):
        while self._run:
            try:
                alive = GUEST.alive()
                self.tick.emit(THREADS.list() if alive else [],
                               GUEST.sessions() if alive else [],
                               alive, GUEST.last_error or "")
            except Exception as e:                      # never kill the thread
                self.tick.emit([], [], False, str(e))
            # A conversation streams, so this is the frame rate of the whole
            # app. 2.5s made an agent look hung mid-turn.
            for _ in range(10):                         # ~1s, interruptible
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
# ---------------------------------------------------------------- under the hood
# ------------------------------------------------------- the layer picture
#
# What a copy-on-write transaction IS, drawn rather than described.
#
# The hood used to be a wall of monospace. It was accurate and almost
# nobody could read it, which defeats the purpose: this panel exists so a
# person can SEE that the agent's changes are not in their folder yet. The
# thing that makes that obvious is the shape of the stack -- your real
# files underneath, one private layer per agent stacked on top, nothing
# flowing down until you say so.
#
# Hover a layer for what that agent did; click it for the actual lines,
# coloured the way a diff is always coloured.


class FileChip(QFrame):
    """One changed file inside a layer."""

    KIND = {
        "created":  ("#3fb950", "new"),
        "modified": ("#d29922", "edited"),
        "deleted":  ("#f85149", "deleted"),
        "dir":      ("#6b7a8d", "folder"),
    }

    def __init__(self, d):
        super().__init__()
        self.setObjectName("Chip")
        colour, word = self.KIND.get(d.get("kind"), ("#8b9aad", d.get("kind")))
        self.setStyleSheet(
            "#Chip { background: %s18; border: 1px solid %s55;"
            " border-radius: 6px; }" % (colour, colour))
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 4, 8, 4)
        h.setSpacing(7)
        dot = QLabel("●")
        dot.setStyleSheet("color: %s; font-size: 9px; background: transparent;"
                          % colour)
        # Without a fixed width the dot's label expands and leaves a wide
        # blank gap before the filename, which reads as a rendering fault.
        dot.setFixedWidth(9)
        h.addWidget(dot)
        name = lab(d.get("path", "?"), selectable=False)
        name.setStyleSheet(
            "color: #e6edf3; font-size: 11.5px; background: transparent;")
        h.addWidget(name, 1)
        a, r = int(d.get("added") or 0), int(d.get("removed") or 0)
        if a or r:
            counts = lab("+%d −%d" % (a, r), selectable=False)
            counts.setStyleSheet(
                "color: #8b9aad; font-size: 10.5px; background: transparent;"
                " font-family: 'DejaVu Sans Mono', monospace;")
            h.addWidget(counts)
        else:
            w = lab(word, selectable=False)
            w.setStyleSheet("color: %s; font-size: 10.5px;"
                            " background: transparent;" % colour)
            h.addWidget(w)


class TxCard(QFrame):
    """One transaction: one agent's private layer."""

    clicked = pyqtSignal(str)
    hovered = pyqtSignal(str)

    def __init__(self, tx, agent, state, diff, lower):
        super().__init__()
        self.tx = str(tx)
        self.setObjectName("TxCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(240)
        v = QVBoxLayout(self)
        v.setContentsMargins(13, 11, 13, 11)
        v.setSpacing(7)

        head = QHBoxLayout()
        head.setSpacing(7)
        who = lab(agent or ("transaction %s" % tx), bold=True, selectable=False)
        who.setStyleSheet("color: #e6edf3; font-size: 12.5px;")
        head.addWidget(who)
        head.addStretch(1)
        colour = {"active": "#58a6ff", "committing": "#d29922",
                  "doomed": "#f85149"}.get(state, "#8b9aad")
        head.addWidget(pill(state or "open", colour))
        v.addLayout(head)

        v.addWidget(lab("layer %s · on top of %s" % (tx, lower or "?"),
                        "Muted", selectable=False))

        if diff:
            for d in diff[:8]:
                v.addWidget(FileChip(d))
            if len(diff) > 8:
                v.addWidget(lab("+%d more" % (len(diff) - 8), "Muted",
                                selectable=False))
        else:
            v.addWidget(lab("nothing changed yet", "Muted", selectable=False))

        v.addWidget(lab("click to see the lines", "Muted", selectable=False))

    def enterEvent(self, e):
        self.setProperty("hover", True)
        self.style().unpolish(self); self.style().polish(self)
        self.hovered.emit(self.tx)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.setProperty("hover", False)
        self.style().unpolish(self); self.style().polish(self)
        super().leaveEvent(e)

    def mousePressEvent(self, e):
        self.clicked.emit(self.tx)
        super().mousePressEvent(e)


class DiffView(QTextEdit):
    """
    A diff, coloured the way diffs are always coloured.

    QTextEdit rather than QPlainTextEdit: the plain widget cannot render
    HTML at all, and the colouring is the entire point of this pane.
    """

    def __init__(self):
        super().__init__()
        self.setReadOnly(True)
        self.setObjectName("DiffView")
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

    def show_diff(self, tx, diff):
        if not diff:
            self.setPlainText("Transaction %s has changed nothing." % tx)
            return
        # Built as HTML rather than by walking a QTextCursor: this is
        # redrawn on every poll while agents work, and per-line cursor
        # formatting on a few hundred lines is visibly slow.
        out = []
        for d in diff:
            kind = d.get("kind")
            head = {"created": "new file", "modified": "edited",
                    "deleted": "deleted", "dir": "new folder"}.get(kind, kind)
            out.append(
                "<div style='color:#e6edf3;font-weight:600;margin-top:10px'>"
                "%s &nbsp;<span style='color:#8b9aad;font-weight:400'>(%s)"
                "</span></div>" % (esc(d.get("path", "?")), head))
            if kind == "deleted":
                out.append("<div style='color:#f85149'>"
                           "this file is removed in this layer</div>")
            for line in (d.get("preview") or [])[:200]:
                out.append(diff_line_html(line, kind))
        self.setHtml(
            "<body style=\"font-family:'JetBrains Mono','DejaVu Sans Mono',"
            "monospace;font-size:11.5px;background:#0a0e14\">"
            + "".join(out) + "</body>")


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def diff_line_html(line, kind):
    """
    Colour one line the way git does.

    A created file has no diff markers -- every line of it is new -- so it
    is coloured entirely as an addition. Without that special case a brand
    new file rendered as flat grey text, which is the exact moment somebody
    most wants to see "all of this is new".
    """
    # Qt's rich text does NOT understand 8-digit hex (#RRGGBBAA).
    #
    # The first version tinted these lines with #3fb95012 and #f8514912,
    # which Qt could not parse, so added and removed lines came out the
    # same muddy colour and the one thing this pane exists to show -- what
    # went in and what came out -- was unreadable. These are flat, opaque
    # colours picked to sit on the #0a0e14 background.
    ADD_BG, DEL_BG = "#0d2417", "#2b1417"
    ADD_FG, DEL_FG = "#56d364", "#ff7b72"

    body = esc(line) or "&nbsp;"
    row = ("<div style='color:%s;background-color:%s;"
           "white-space:pre'>%s</div>")
    if kind == "created":
        return row % (ADD_FG, ADD_BG, "+ " + body)
    if line.startswith("+++") or line.startswith("---"):
        return "<div style='color:#6b7a8d;white-space:pre'>%s</div>" % body
    if line.startswith("@@"):
        return ("<div style='color:#79c0ff;margin-top:8px;"
                "white-space:pre'>%s</div>" % body)
    if line.startswith("+"):
        return row % (ADD_FG, ADD_BG, body)
    if line.startswith("-"):
        return row % (DEL_FG, DEL_BG, body)
    return "<div style='color:#8b9aad;white-space:pre'>%s</div>" % body


class LayerStack(QWidget):
    """The picture: your folder underneath, the agents' layers on top."""

    show_tx = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._sig = None
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(10)

        self.caption = lab("", "Sub", wrap=True)
        v.addWidget(self.caption)

        # --- the layers the agents write into -------------------------
        self.layerbox = QFrame()
        self.layerbox.setObjectName("LayerBox")
        lb = QVBoxLayout(self.layerbox)
        lb.setContentsMargins(13, 11, 13, 13)
        lb.setSpacing(8)
        lb.addWidget(lab("SANDBOX LAYERS — one per agent, private to it",
                         "Muted", selectable=False))
        self.cards = QHBoxLayout()
        self.cards.setSpacing(10)
        lb.addLayout(self.cards)
        v.addWidget(self.layerbox)

        self.arrow = lab("nothing flows down until you press Keep",
                         "Muted", selectable=False)
        self.arrow.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(self.arrow)

        # --- the real folder ------------------------------------------
        self.realbox = QFrame()
        self.realbox.setObjectName("RealBox")
        rb = QVBoxLayout(self.realbox)
        rb.setContentsMargins(13, 11, 13, 11)
        rb.setSpacing(5)
        rb.addWidget(lab("YOUR REAL FOLDER — untouched", "Muted",
                         selectable=False))
        self.realfiles = lab("", selectable=False)
        self.realfiles.setWordWrap(True)
        self.realfiles.setStyleSheet("color:#9fb0c3;font-size:11.5px;")
        rb.addWidget(self.realfiles)
        v.addWidget(self.realbox)

        self.hint = lab("", "Muted", wrap=True)
        v.addWidget(self.hint)
        v.addStretch(1)

    def clear_cards(self):
        while self.cards.count():
            it = self.cards.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def update(self, snap, diffs, agents):
        live = snap.get("txlive") or []
        sig = (tuple(sorted(t["tx"] for t in live)),
               tuple(sorted((k, len(v or [])) for k, v in diffs.items())))
        if sig == self._sig:
            return
        self._sig = sig
        self.clear_cards()

        if not live:
            self.caption.setText(
                "No transaction is open. When an agent runs, a private "
                "layer appears here for it.")
            self.cards.addWidget(lab("— no open layers —", "Muted",
                                     selectable=False))
        else:
            n = len(live)
            self.caption.setText(
                "%d layer%s open. Each agent writes into its own, so none "
                "of them can see or overwrite another's work, and none of "
                "it has reached your folder."
                % (n, "s are" if n != 1 else " is"))
            lowers = {d["tx"]: d.get("lower") for d in (snap.get("txdirs") or [])}
            for t in live:
                tx = t["tx"]
                c = TxCard(tx, agents.get(str(tx)), t.get("state"),
                           diffs.get(str(tx)), lowers.get(tx))
                c.clicked.connect(self.show_tx.emit)
                self.cards.addWidget(c)
            self.cards.addStretch(1)

        # What is actually in the folder underneath. The snapshot already
        # walks it for the LOWER section; a placeholder string here was
        # the one part of this picture that was not read from the system.
        names = sorted({e.get("path") for e in (snap.get("lower") or [])
                        if e.get("path")})
        if names:
            shown = "   ".join(names[:18])
            if len(names) > 18:
                shown += "   +%d more" % (len(names) - 18)
            self.realfiles.setText(shown)
        else:
            self.realfiles.setText(
                "(empty, or nothing readable underneath)")
        self.hint.setText(
            "Hover a layer to see what that agent did. Click it for the "
            "actual lines, with additions in green and removals in red.")


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

        # The picture first. The text tabs stay, because when something is
        # wrong the monospace dump is what you actually need -- but it is
        # not what somebody should meet first.
        self.stackpage = QWidget()
        sp = QVBoxLayout(self.stackpage)
        sp.setContentsMargins(0, 0, 0, 0)
        sp.setSpacing(8)
        self.stack_view = LayerStack()
        self.diffview = DiffView()
        self.diffview.setMinimumHeight(220)
        self.diffhead = lab("Click a layer above to see its lines", "Muted",
                            selectable=False)
        self.stack_view.show_tx.connect(self._show_tx)
        sp.addWidget(self.stack_view, 1)
        sp.addWidget(self.diffhead)
        sp.addWidget(self.diffview, 1)

        self.kernel = QPlainTextEdit(); self.kernel.setReadOnly(True)
        self.effects = QPlainTextEdit(); self.effects.setReadOnly(True)
        self.research = QPlainTextEdit(); self.research.setReadOnly(True)
        self.tabs.addTab(self.stackpage, "Live layers")
        self.tabs.addTab(self.kernel, "Kernel state")
        self.tabs.addTab(self.effects, "Intercepted effects")
        self.tabs.addTab(self.research, "Measurements")
        self._diffs = {}
        self._agents = {}
        self._open_tx = None

        self.research.setPlainText(self._research())

    def _show_tx(self, tx):
        self._open_tx = tx
        who = self._agents.get(str(tx))
        self.diffhead.setText(
            "Layer %s%s — green is added, red is removed. None of it is in "
            "your folder yet." % (tx, (" · " + who) if who else ""))
        self.diffview.show_diff(tx, self._diffs.get(str(tx)))

    def set_layers(self, snap, diffs, agents):
        self._diffs = diffs
        self._agents = agents
        self.stack_view.update(snap, diffs, agents)
        if self._open_tx:
            self.diffview.show_diff(self._open_tx,
                                    diffs.get(str(self._open_tx)))

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
        # --- what the kernel says right now, not what it once logged ---
        #
        # These two come from /sys/kernel/debug/agenttx/{transactions,
        # waitfor}, which are the live table and the live graph. The
        # earlier version counted dmesg lines, which only ever goes up:
        # an edge added and released still counted, so the panel would
        # claim wait-for edges existed long after they were gone.
        L += ["", "LIVE TRANSACTIONS  (/sys/kernel/debug/agenttx)", "=" * 58]
        live = snap.get("txlive") or []
        if live:
            L.append("  %-6s %-12s %-12s %8s %8s %8s"
                     % ("tx", "state", "worst", "deferred", "written", "pid"))
            for t in live[:14]:
                L.append("  %-6s %-12s %-12s %8s %8s %8s"
                         % (t["tx"], t["state"], t["worst"],
                            t["deferred"], t["written"], t["pid"]))
        else:
            L.append("  no transaction open")

        L += ["", "WAIT-FOR GRAPH", "=" * 58]
        wfg = snap.get("wfg") or []
        if wfg:
            for e in wfg[:20]:
                L.append("  tx %s  --waiting on-->  tx %s   (%s, %s ms)"
                         % (e["waiter"], e["holder"], e["kind"], e["age_ms"]))
            L.append("")
            L.append("  A cycle here is a deadlock. The kernel finds it when")
            L.append("  the edge is added, picks the least-severe victim, and")
            L.append("  aborts it -- abort IS the preemption primitive.")
        else:
            L.append("  no transaction is waiting on another")
            L.append("")
            L.append("  This is the normal state with one agent. Run a task")
            L.append("  with several agents at once to make edges appear.")

        L += ["", "COPY-ON-WRITE AREAS", "=" * 58]
        for d in snap.get("txdirs", [])[:14]:
            L.append(f"  tx {d['tx']:<4} {d['upper_entries']:>4} entries   {d['lower']}")
        self.kernel.setPlainText("\n".join(L))

        ev = [l for l in snap.get("dmesg", []) if "agenttx" in l]
        self.effects.setPlainText("\n".join(ev[-200:]) or "nothing logged yet")


# ---------------------------------------------------------------- main window
# ---------------------------------------------------------------- the chat
#
# A thread is a conversation and the app renders it as one. This is the part
# that was missing: clicking a task used to show a diff and a blob of raw
# output, which tells you WHAT changed but never HOW the agent got there --
# what it decided to try, what came back, what it did about it. That is the
# thing you actually need when the answer is wrong, and it is what every
# other harness shows you.
#
# The events come from tools/harness/tx-agent.py, which is Claude Code's own
# --output-format stream-json, so what is drawn here is the real loop and not
# a summary of it.


class ToolCard(QFrame):
    """One tool call, with its result folded underneath."""

    # Tool calls are the bulk of an agent turn and most of them are boring --
    # a read, a grep, a file that went where it was supposed to. Showing all
    # of them expanded turns the conversation into a wall of JSON and buries
    # the two lines of reasoning that matter. Collapsed by default, one click
    # to open, and a failure opens itself.

    def __init__(self, name: str, detail: str):
        super().__init__()
        self.setObjectName("Tool")
        v = QVBoxLayout(self)
        v.setContentsMargins(12, 9, 12, 9)
        v.setSpacing(6)

        head = QHBoxLayout()
        head.setSpacing(8)
        self.toggle = QPushButton("▸")
        self.toggle.setObjectName("Twisty")
        self.toggle.setFixedWidth(20)
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(self._toggled)
        head.addWidget(self.toggle)
        head.addWidget(lab(name, "ToolName", bold=True))
        self.detail = lab(detail, "Muted")
        self.detail.setWordWrap(False)
        head.addWidget(self.detail, 1)
        self.state = pill("running", C["def"])
        head.addWidget(self.state)
        v.addLayout(head)

        self.body = QPlainTextEdit()
        self.body.setReadOnly(True)
        self.body.setObjectName("ToolBody")
        self.body.setMaximumHeight(220)
        self.body.hide()
        v.addWidget(self.body)
        self._text = ""

    def _toggled(self, on):
        self.toggle.setText("▾" if on else "▸")
        self.body.setVisible(on and bool(self._text))

    def set_input(self, text: str):
        self._text = text
        self.body.setPlainText(text)

    def set_result(self, text: str, is_error: bool):
        self._text = (self._text + "\n\n--- result ---\n" + text).strip()
        self.body.setPlainText(self._text[-8000:])
        self.state.setText("failed" if is_error else "done")
        colour = C["deleted"] if is_error else C["created"]
        self.state.setStyleSheet(
            f"background:{colour}22;color:{colour};border:1px solid {colour}66;")
        if is_error:
            # A failure is never the boring case.
            self.toggle.setChecked(True)


class Bubble(QFrame):
    """One message. `who` is 'you', 'agent', or 'problem'."""

    def __init__(self, who: str, text: str, agent: str = ""):
        super().__init__()
        self.setObjectName({"you": "YouMsg", "agent": "AgentMsg",
                            "notice": "NoteMsg",
                            "conflict": "ConflictMsg"}.get(who, "ErrMsg"))
        v = QVBoxLayout(self)
        v.setContentsMargins(14, 11, 14, 11)
        v.setSpacing(5)
        tag = {"you": "YOU", "agent": "AGENT", "problem": "PROBLEM",
               "notice": "SANDBOX", "conflict": "CONFLICT"}[who]
        # With a swarm running, "AGENT" alone is useless -- three of them
        # are talking and the whole question is which one did what.
        if agent:
            tag = agent.upper()
        v.addWidget(lab(tag, "Who"))
        self.body = lab(text, wrap=True)
        # Selectable: people copy an agent's explanation into commit
        # messages and bug reports constantly.
        self.body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.body)

    def append(self, text: str):
        self.body.setText(self.body.text() + text)


class TurnDivider(QFrame):
    """Marks where one turn ends and its transaction is waiting."""

    def __init__(self, turn: int, tx: str):
        super().__init__()
        self.setObjectName("Divider")
        h = QHBoxLayout(self)
        h.setContentsMargins(2, 4, 2, 4)
        h.setSpacing(8)
        h.addWidget(lab(f"turn {turn}", "Muted"))
        if tx:
            h.addWidget(lab(f"· transaction {tx}", "Muted"))
        h.addStretch(1)


class ChatView(QWidget):
    decided = pyqtSignal(str, str)          # tx, commit|abort
    submitted = pyqtSignal(str)             # follow-up text

    def __init__(self):
        super().__init__()
        self.thread_id = None
        self._review = None                 # the change-report block, if any
        self._review_sig = None
        self._decide_rows = {}              # tx -> (discard, keep, note)
        self._is_swarm = False              # did this turn fan out?
        self._tools = {}                    # tool_use_id -> ToolCard
        self._last_bubble = None
        self._seen_turns = set()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        head = QFrame()
        head.setObjectName("ChatHead")
        hv = QVBoxLayout(head)
        hv.setContentsMargins(20, 13, 20, 13)
        hv.setSpacing(2)
        self.title = lab("No task open", "Headline")
        self.where = lab("", "Muted")
        hv.addWidget(self.title)
        hv.addWidget(self.where)
        root.addWidget(head)

        # Shown when no conversation is open. This is where the folder is
        # chosen, because that is the one decision a new task genuinely
        # needs and asking for it here means the header does not have to.
        self.welcome = QFrame()
        self.welcome.setObjectName("Welcome")
        wv = QVBoxLayout(self.welcome)
        wv.setContentsMargins(40, 40, 40, 40)
        wv.setSpacing(12)
        wv.addStretch(1)
        wv.addWidget(lab("What should the agent do?", "Headline"))
        wv.addWidget(lab(
            "Describe it below and press Enter. The agent decides what to "
            "run and keeps going until it is done — with no permission "
            "prompts, because nothing it writes is real until you press "
            "Keep.", "Sub", wrap=True))
        fr = QHBoxLayout()
        fr.setSpacing(9)
        fr.addWidget(lab("Folder it may change", "Muted", selectable=False))
        self.dirin = QLineEdit("/tmp/work")
        self.dirin.setObjectName("DirInput")
        self.dirin.setMaximumWidth(280)
        self.dirin.setToolTip(
            "The only folder the agent is allowed to change.\n"
            "Everything it writes here goes into a sandbox layer first.")
        fr.addWidget(self.dirin)
        fr.addStretch(1)
        wv.addLayout(fr)
        wv.addWidget(lab(
            "A leading <b>$</b> runs one shell command instead, using no "
            "model and costing nothing.", "Muted", wrap=True))
        wv.addStretch(2)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setObjectName("ChatScroll")
        self.inner = QWidget()
        self.col = QVBoxLayout(self.inner)
        self.col.setContentsMargins(20, 16, 20, 16)
        self.col.setSpacing(10)
        self.col.addStretch(1)
        self.scroll.setWidget(self.inner)

        # welcome OR transcript, never both
        self.pane = QStackedWidget()
        self.pane.addWidget(self.welcome)
        self.pane.addWidget(self.scroll)
        root.addWidget(self.pane, 1)

        # --- the decision, when there is one --------------------------
        self.bar = QFrame()
        self.bar.setObjectName("DecideBar")
        bl = QHBoxLayout(self.bar)
        bl.setContentsMargins(18, 12, 18, 12)
        bl.setSpacing(10)
        self.barnote = lab("", "Warn", wrap=True)
        bl.addWidget(self.barnote, 1)
        self.drop = QPushButton("Discard")
        self.drop.setObjectName("Discard")
        self.keep = QPushButton("Keep the changes")
        self.keep.setObjectName("Commit")
        self.drop.clicked.connect(lambda: self._decide("abort"))
        self.keep.clicked.connect(lambda: self._decide("commit"))
        bl.addWidget(self.drop)
        bl.addWidget(self.keep)
        root.addWidget(self.bar)
        self.bar.hide()
        self.tx = None

        # --- live kernel strip ----------------------------------------
        #
        # The "Under the hood" panel is a separate screen you have to go
        # and look at, which means during the one moment it is interesting
        # -- while agents are actually running -- nobody is looking at it.
        # This is the always-on version: the few numbers that change while
        # work happens, on the same screen as the work.
        self.strip = QFrame()
        self.strip.setObjectName("LiveStrip")
        sl = QHBoxLayout(self.strip)
        sl.setContentsMargins(16, 6, 16, 6)
        sl.setSpacing(16)
        self.live = {}
        for key, label in (("tx", "open transactions"),
                           ("files", "sandboxed writes"),
                           ("wfg", "wait-for edges"),
                           ("dead", "deadlocks broken"),
                           ("model", "brain")):
            sl.addWidget(lab(label, "Muted"))
            w = lab("—", "LiveVal")
            self.live[key] = w
            sl.addWidget(w)
            sl.addSpacing(4)
        sl.addStretch(1)
        root.addWidget(self.strip)

        # --- composer -------------------------------------------------
        comp = QFrame()
        comp.setObjectName("Composer")
        cv = QHBoxLayout(comp)
        cv.setContentsMargins(16, 12, 16, 14)
        cv.setSpacing(10)
        self.input = QPlainTextEdit()
        self.input.setObjectName("ChatInput")
        self.input.setPlaceholderText(
            "Reply to the agent…    ($ runs a shell command instead)")
        self.input.setFixedHeight(64)
        self.input.installEventFilter(self)
        self.send = QPushButton("Send")
        self.send.setObjectName("Dispatch")
        self.send.clicked.connect(self._submit)
        cv.addWidget(self.input, 1)
        cv.addWidget(self.send)
        root.addWidget(comp)

    # Enter sends, Shift+Enter makes a newline. This is what every chat
    # client does and fingers already know it; a Send-button-only composer
    # is a small papercut repeated all day.
    def eventFilter(self, obj, ev):
        if obj is self.input and ev.type() == ev.Type.KeyPress:
            if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and \
               not (ev.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self._submit()
                return True
        return super().eventFilter(obj, ev)

    def _submit(self):
        text = self.input.toPlainText().strip()
        if text:
            self.input.clear()
            self.submitted.emit(text)

    def _decide_one(self, tx, what):
        """A single agent's transaction, from the swarm result card."""
        row = self._decide_rows.get(str(tx))
        if row:
            d, k, note = row
            d.setEnabled(False)
            k.setEnabled(False)
            note.setText("applying…")
        self.decided.emit(tx, what)

    def sync_decisions(self, sessions):
        """
        Keep the per-agent buttons honest about what still exists.

        A transaction only waits for a person while its session is alive.
        Close the app on a review screen and the watchdog reaps it 90s
        later; reopen the app and the swarm card is still sitting there
        offering Keep and Discard for transactions the kernel has already
        forgotten. Pressing them wrote a decision into a directory nobody
        was reading, and the app gave no sign either way -- which is
        exactly how this was reported: "why cant i press this".
        """
        live = {str(x.get("tx")): (x.get("status") or "")
                for x in (sessions or [])}
        for tx, (d, k, note) in self._decide_rows.items():
            st = live.get(tx)
            if st == "awaiting-decision":
                if not note.text() == "applying…":
                    d.setEnabled(True)
                    k.setEnabled(True)
                    note.setText("")
            elif st in ("committed", "aborted"):
                d.setEnabled(False)
                k.setEnabled(False)
                note.setText("kept" if st == "committed" else "discarded")
            else:
                d.setEnabled(False)
                k.setEnabled(False)
                note.setText("expired — no longer open")

    def _decide(self, what):
        if self.tx:
            self.keep.setEnabled(False)
            self.drop.setEnabled(False)
            self.barnote.setText("applying your decision…")
            self.decided.emit(self.tx, what)

    # --- rendering ----------------------------------------------------
    def add(self, w):
        self.col.insertWidget(self.col.count() - 1, w)

    def reset(self, thread):
        self.thread_id = thread.get("id") if thread else None
        self._review = None
        self._review_sig = None
        self._decide_rows = {}
        self._is_swarm = False
        self._tools.clear()
        self._last_bubble = None
        self._seen_turns.clear()
        while self.col.count() > 1:
            it = self.col.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if thread:
            self.pane.setCurrentIndex(1)
            self.title.setText(thread.get("title") or "Task")
            self.where.setText(
                f"agent may only change {thread.get('lower','?')}  ·  "
                f"{thread.get('turns',0)} turn"
                f"{'s' if thread.get('turns',0) != 1 else ''}")
            self.input.setPlaceholderText(
                "Reply to the agent…    ($ runs a shell command instead)")
        else:
            self.pane.setCurrentIndex(0)
            self.title.setText("New chat")
            self.where.setText("")
            self.bar.hide()
            self.input.setPlaceholderText(
                "Describe the task…    ($ runs a shell command instead)")
        self.input.setFocus()

    def at_bottom(self) -> bool:
        b = self.scroll.verticalScrollBar()
        return b.value() >= b.maximum() - 80

    def scroll_end(self):
        b = self.scroll.verticalScrollBar()
        b.setValue(b.maximum())

    def append_events(self, evs):
        """Render new transcript events. Incremental: never rebuilds."""
        stick = self.at_bottom()
        for e in evs:
            try:
                self._one(e)
            except Exception as ex:           # a bad event must not blank the UI
                print(f"agenttx: cannot render {e.get('type')}: {ex}",
                      file=sys.stderr)
        if stick:
            QTimer.singleShot(0, self.scroll_end)

    def _one(self, e):
        kind = e.get("type")
        turn = e.get("turn")

        if kind == "tx_turn_start":
            if turn not in self._seen_turns:
                self._seen_turns.add(turn)
                if len(self._seen_turns) > 1:
                    self.add(TurnDivider(turn, e.get("tx") or ""))
            prompt = (e.get("prompt") or "").strip()
            if e.get("shell"):
                prompt = "$ " + prompt
            self.add(Bubble("you", prompt))
            self._last_bubble = None
            return

        if kind == "assistant":
            for c in (e.get("message", {}) or {}).get("content", []) or []:
                if c.get("type") == "text":
                    txt = (c.get("text") or "").strip()
                    if txt:
                        self.add(Bubble("agent", txt, e.get("agent") or ""))
                        self._last_bubble = None
                elif c.get("type") == "tool_use":
                    who = e.get("agent")
                    card_ = ToolCard((("%s · " % who) if who else "")
                                     + (c.get("name") or "tool"),
                                     self._summarise(c.get("name"),
                                                     c.get("input") or {}))
                    card_.set_input(json.dumps(c.get("input") or {},
                                               indent=2)[:8000])
                    self._tools[c.get("id")] = card_
                    self.add(card_)
            return

        if kind == "user":
            for c in (e.get("message", {}) or {}).get("content", []) or []:
                if c.get("type") != "tool_result":
                    continue
                card_ = self._tools.get(c.get("tool_use_id"))
                body = c.get("content")
                if isinstance(body, list):
                    body = "\n".join(
                        b.get("text", "") for b in body if isinstance(b, dict))
                if card_:
                    card_.set_result(str(body or "")[:8000],
                                     bool(c.get("is_error")))
            return

        if kind == "tx_output":
            txt = (e.get("text") or "").strip()
            if txt:
                card_ = ToolCard("shell", "the command you typed")
                card_.set_result(txt, False)
                card_.toggle.setChecked(True)
                self.add(card_)
            return

        if kind in ("tx_error", "tx_notice"):
            # A notice is not a problem. "Planning how to split this across
            # 3 agents…" was rendering in the red PROBLEM style, which
            # tells the reader something went wrong at the exact moment
            # nothing has. Errors stay red; notices are information, and
            # a conflict warning is its own thing again -- it is the one
            # the person has to act on.
            text = (e.get("text") or "").strip()
            if kind == "tx_error":
                who = "problem"
            elif text.startswith("CONFLICT"):
                who = "conflict"
            else:
                who = "notice"
            self.add(Bubble(who, text))
            return

        if kind == "swarm_plan":
            box = QFrame(); box.setObjectName("Plan")
            v = QVBoxLayout(box); v.setContentsMargins(14, 11, 14, 11)
            v.setSpacing(6)
            parts = e.get("parts") or []
            v.addWidget(lab("Split across %d agents" % len(parts), bold=True))
            v.addWidget(lab(
                "Each one gets its own transaction on the same folder, so "
                "they cannot overwrite each other until you decide.",
                "Muted", wrap=True))
            for p in parts:
                v.addWidget(lab("<b>%s</b> — %s" % (p.get("name", "?"),
                                                    p.get("task", "")),
                                wrap=True))
            self.add(box)
            return

        if kind == "swarm_agent_start":
            self.add(lab("▸ %s started" % e.get("agent", "?"), "Muted"))
            return

        if kind == "swarm_agent_done":
            self.add(lab("✓ %s finished (transaction %s)"
                         % (e.get("agent", "?"), e.get("tx") or "?"), "Muted"))
            return

        if kind == "swarm_result":
            self._is_swarm = True
            box = QFrame()
            conflicts = e.get("conflicts") or []
            box.setObjectName("Conflict" if conflicts else "Plan")
            v = QVBoxLayout(box); v.setContentsMargins(14, 11, 14, 11)
            v.setSpacing(6)
            agents = e.get("agents") or []
            if conflicts:
                v.addWidget(lab("%d conflict%s between agents"
                                % (len(conflicts),
                                   "s" if len(conflicts) != 1 else ""),
                                bold=True))
                v.addWidget(lab(
                    "These agents changed the same file in separate "
                    "transactions. Neither saw the other's version, so if "
                    "you keep both, whichever you keep second wins and the "
                    "first one's work is gone.", "Muted", wrap=True))
                for c in conflicts:
                    v.addWidget(lab("<b>%s</b> and <b>%s</b> both wrote: %s"
                                    % (c.get("a"), c.get("b"),
                                       ", ".join(c.get("paths") or [])),
                                    wrap=True))
            else:
                v.addWidget(lab("%d agents finished, no overlapping files"
                                % len(agents), bold=True))
            # One decision PER AGENT.
            #
            # Each agent holds its own transaction, so "keep" and
            # "discard" are per-agent answers, not one answer for the
            # run. That is the entire reason they were given separate
            # transactions: when two of them collide you keep the one you
            # wanted and drop the other, without re-running anything.
            # A single bar at the bottom cannot express that.
            for ag in agents:
                row = QHBoxLayout()
                row.setSpacing(8)
                files = ", ".join(ag.get("files") or []) or "nothing"
                row.addWidget(lab("<b>%s</b> (tx %s): %s"
                                  % (ag.get("name"), ag.get("tx") or "?",
                                     files), wrap=True), 1)
                tx = ag.get("tx")
                if tx and ag.get("files"):
                    d = QPushButton("Discard")
                    d.setObjectName("Discard")
                    k = QPushButton("Keep")
                    k.setObjectName("Commit")
                    d.clicked.connect(
                        lambda _=False, t=tx: self._decide_one(t, "abort"))
                    k.clicked.connect(
                        lambda _=False, t=tx: self._decide_one(t, "commit"))
                    row.addWidget(d)
                    row.addWidget(k)
                    note = lab("", "Muted", selectable=False)
                    row.addWidget(note)
                    # Remember them so a transaction that goes away can
                    # take its own buttons with it. A button that looks
                    # live and does nothing is worse than no button: it
                    # was pressed, nothing happened, and the app said
                    # nothing about why.
                    self._decide_rows[str(tx)] = (d, k, note)
                holder = QWidget()
                holder.setLayout(row)
                v.addWidget(holder)
            if conflicts:
                v.addWidget(lab(
                    "Keeping both is the one choice that loses work: "
                    "whichever you keep second overwrites the first.",
                    "Warn", wrap=True))
            self.add(box)
            return

        if kind == "result":
            n = e.get("num_turns")
            ms = e.get("duration_ms")
            bits = []
            if n is not None:
                bits.append(f"{n} step{'s' if n != 1 else ''}")
            if ms:
                bits.append(f"{ms/1000:.1f}s")
            if bits:
                self.add(lab("finished — " + ", ".join(bits), "Muted"))
            return

    @staticmethod
    def _summarise(name, inp):
        """One line saying what this call is about, in the caller's terms."""
        if not isinstance(inp, dict):
            return ""
        for key in ("file_path", "path", "pattern", "command", "url"):
            if inp.get(key):
                return str(inp[key])[:120]
        if inp.get("description"):
            return str(inp["description"])[:120]
        return ""

    def update_live(self, snap):
        """Numbers that move while the agents work."""
        if not snap.get("reachable"):
            for w in self.live.values():
                w.setText("—")
            self.live["model"].setText("sandbox offline")
            return
        st = snap.get("stat") or {}
        self.live["tx"].setText(str(snap.get("open_tx", "0")))
        self.live["files"].setText(str(snap.get("upper_files", st.get("n_written", 0))))
        self.live["wfg"].setText(str(snap.get("wfg_edges", 0)))
        self.live["dead"].setText(str(snap.get("deadlocks", 0)))
        self.live["model"].setText(snap.get("brain") or "—")

    def set_review(self, tx, status, diff, swarm=False):
        """
        Put the change report at the end of the conversation.

        Not on a separate screen. The question "do I keep this?" is asked
        about what the agent just said it did, and the honest answer usually
        depends on both halves -- the explanation and the actual diff. Making
        someone switch views to compare them is how a review becomes a
        rubber stamp.
        """
        self.tx = tx
        if swarm:
            # Several transactions are open; each is decided on its own
            # row in the swarm card. One bar deciding an arbitrary one of
            # them would be worse than no bar.
            self.bar.hide()
            return
        if status != "awaiting-decision" or not diff:
            self.bar.hide()
            if self._review is not None and status in ("committed", "aborted"):
                self._review.setEnabled(False)
            return

        sig = (tx, json.dumps(diff, sort_keys=True))
        if sig != self._review_sig:
            self._review_sig = sig
            if self._review is not None:
                self._review.deleteLater()
            self._review = self._build_review(diff)
            self.add(self._review)
            QTimer.singleShot(0, self.scroll_end)

        head, _tail = plain_summary(diff)
        self.bar.show()
        self.keep.setEnabled(True)
        self.drop.setEnabled(True)
        self.barnote.setText(
            head + "  Nothing has touched the real folder yet.")

    def _build_review(self, diff):
        box = QFrame()
        box.setObjectName("Review")
        v = QVBoxLayout(box)
        v.setContentsMargins(15, 13, 15, 13)
        v.setSpacing(9)
        head, tail = plain_summary(diff)
        v.addWidget(lab("What this turn would change", bold=True))
        v.addWidget(lab(head + " " + tail, "Sub", wrap=True))
        for d in diff:
            v.addWidget(FileCard(d))
        return box


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AgentTx")
        self.resize(1320, 860)
        self.threads = []
        self.sessions = []
        self.selected = None            # thread id
        self._since = {}                # thread id -> transcript lines read
        self._rendered = None           # thread id currently drawn
        self._fetching = set()          # thread ids with a fetch in flight
        self._want_new = False          # user asked for an empty chat
        self._live_tx = None            # tx ids the kernel actually has
        self._agent_of = {}             # tx -> agent name, from swarm events
        self._last_sig = None
        self._busy = False

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

        # ONE button, not a form.
        #
        # The header used to carry a folder field, a task field and a Start
        # button, which asked for three decisions before anything could
        # happen and duplicated the composer at the bottom. A chat app has
        # one place you type. This is the only thing the header still needs
        # to offer: somewhere new to type.
        self.newchat = QPushButton("＋  New chat")
        self.newchat.setObjectName("NewChat")
        self.newchat.clicked.connect(self.new_chat)
        top.addWidget(self.newchat)

        self.mode = QComboBox()
        self.mode.setObjectName("Mode")
        self.mode.addItem("1 agent", ("local", 1))
        self.mode.addItem("2 agents at once", ("swarm", 2))
        self.mode.addItem("3 agents at once", ("swarm", 3))
        self.mode.addItem("5 agents at once", ("swarm", 5))
        self.mode.setToolTip(
            "Each agent gets its OWN transaction on the same folder.\n"
            "If two of them change the same file, you are told before\n"
            "you keep anything.")
        top.addWidget(self.mode)

        self.linkpill = pill("connecting…", "#8b9aad")
        self.linkpill.setMinimumWidth(150)
        top.addWidget(self.linkpill)
        self.hood = QPushButton("Under the hood")
        self.hood.setObjectName("Hood")
        self.hood.setCheckable(True)
        self.hood.toggled.connect(self.toggle_hood)
        top.addWidget(self.hood)
        hv.addLayout(top)

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
        self.chat = ChatView()
        self.chat.decided.connect(self.decide)
        self.chat.submitted.connect(self.on_submit)
        self.hoodview = HoodView()
        self.stack.addWidget(self.chat)
        self.stack.addWidget(self.hoodview)

        split.addWidget(side)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 1)
        rv.addWidget(split, 1)

        self.poll = Poller()
        self.poll.tick.connect(self.on_tick)
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
    #
    # A task is a CONVERSATION, not a command. `new_task` opens one;
    # `follow_up` continues the one already open, carrying everything the
    # agent learned in the earlier turns. Each turn still gets its own
    # transaction, so "keep turn 1, throw away turn 2" is a thing you can
    # actually do.
    def new_chat(self):
        """Open an empty conversation. Nothing starts until you type."""
        # Without this the next poll (1 Hz) runs rebuild_list, sees no
        # selection, helpfully selects the newest thread, and the empty
        # chat you just asked for vanishes about a second after you asked
        # for it.
        self._want_new = True
        self.selected = None
        self._rendered = None
        self.list.blockSignals(True)
        self.list.setCurrentRow(-1)
        self.list.blockSignals(False)
        self.chat.reset(None)

    def on_submit(self, text):
        """
        The composer is the only place you type.

        With a conversation open this continues it; with none open it
        starts one. The header used to have its own task field, so there
        were two boxes doing the same job and the answer to "where do I
        type" depended on what was on screen.
        """
        if self._busy:
            return
        t = self._thread(self.selected)
        if t:
            self._start(t["id"], t.get("lower") or self.chat.dirin.text().strip(),
                        text, t.get("title") or "Task", sid=t.get("sid"))
            return
        d = self.chat.dirin.text().strip()
        if not d:
            return
        tid = "t%d-%s" % (int(time.time()), uuid.uuid4().hex[:6])
        title = text[:70] + ("…" if len(text) > 70 else "")
        self._want_new = False
        self.selected = tid
        self._since[tid] = 0
        self._rendered = None
        self._start(tid, d, text, title)

    def _start(self, tid, lower, text, title, sid=None):
        self._busy = True
        self.chat.send.setEnabled(False)
        self.chat.send.setText("…")

        # A leading "$" is the no-model path: run exactly this command,
        # bill nothing, and still get the transaction and the transcript.
        # It is also the control arm -- see tools/harness/tx-shell.sh.
        shell = text.startswith("$")
        payload = text[1:].strip() if shell else text

        def done(_res):
            self._busy = False
            self.chat.send.setEnabled(True)
            self.chat.send.setText("Send")

        if shell:
            run_async(self, THREADS.start_shell, tid, lower, payload, title,
                      then=done)
            return
        mode, n = self.mode.currentData() or ("local", 1)
        run_async(self, THREADS.start_turn, tid, lower, payload, title,
                  sid or str(uuid.uuid4()), "agent", mode, n, then=done)

    def decide(self, tx, what):
        run_async(self, GUEST.session_decide, tx, what)

    def toggle_hood(self, on):
        self.stack.setCurrentIndex(1 if on else 0)
        if on:
            self.refresh_hood()

    def refresh_hood(self):
        if not self.hood.isChecked():
            return

        def got(snap):
            if not isinstance(snap, dict):
                return
            self.hoodview.update_live(snap)
            live = [t["tx"] for t in (snap.get("txlive") or [])]
            if not live:
                self.hoodview.set_layers(snap, {}, self._agent_of)
                return

            # One round trip for every open layer's diff. Bounded by the
            # number of agents (at most five), and only while the hood is
            # actually open -- this is the panel somebody is staring at, so
            # it is the one place the extra calls are worth it.
            def gotdiffs(res):
                if isinstance(res, Exception):
                    return
                self.hoodview.set_layers(snap, res, self._agent_of)

            def fetch(txs):
                return {str(t): GUEST.session_diff(t) for t in txs}
            run_async(self, fetch, live[:6], then=gotdiffs)

        run_async(self, GUEST.snapshot, then=got)

    # ------------------------------------------------------------ polling
    def _thread(self, tid):
        return next((t for t in self.threads if t.get("id") == tid), None)

    def on_tick(self, threads, sessions, alive, err):
        if alive:
            self.linkpill.setStyleSheet(
                "background:#3fb95022;color:#3fb950;border:1px solid #3fb95066;")
            self.linkpill.setText("sandbox connected")
        else:
            self.linkpill.setStyleSheet(
                "background:#f8514922;color:#f85149;border:1px solid #f8514966;")
            self.linkpill.setText("sandbox offline")

        self.threads = threads
        self.sessions = sessions
        # The strip is live whether or not the hood panel is open, which
        # is the entire point of it.
        def livesnap(sn):
            if not isinstance(sn, dict):
                return
            self.chat.update_live(sn)
            if sn.get("reachable"):
                self._live_tx = {str(t.get("tx"))
                                 for t in (sn.get("txlive") or [])}
        run_async(self, GUEST.snapshot, then=livesnap)
        sig = [(t.get("id"), t.get("turns"), t.get("lines"), t.get("tx"))
               for t in threads]
        if sig != self._last_sig:
            self._last_sig = sig
            self.rebuild_list()
        self.chat.sync_decisions(sessions)
        self.refresh_chat()

    def refresh_chat(self):
        t = self._thread(self.selected)
        if not t:
            return
        if self._rendered != t["id"]:
            # Switched threads: redraw from the top of the transcript. This
            # is the bug the whole rewrite started from -- clicking a task
            # used to change the diff and leave the conversation behind.
            self._rendered = t["id"]
            self._since[t["id"]] = 0
            self._fetching.discard(t["id"])
            self.chat.reset(t)

        # ONE transcript fetch in flight at a time.
        #
        # The fetch is an ssh round trip and the poll is 1 Hz, so on a slow
        # link a second poll fires before the first returns. `since` has not
        # been advanced yet, so the second request asks for the same lines
        # and the conversation renders every message twice. Seen exactly
        # that way: a freshly opened thread showed its prompt and its tool
        # call duplicated, and only settled once the requests stopped
        # overlapping.
        tid = t["id"]
        since = self._since.get(tid, 0)
        if t.get("lines", 0) > since and tid not in self._fetching:
            self._fetching.add(tid)

            def got(res, tid=tid):
                self._fetching.discard(tid)
                if isinstance(res, Exception) or not isinstance(res, tuple):
                    return
                evs, n = res
                # Another thread may have been selected while this was in
                # flight; those events belong to a conversation that is no
                # longer on screen.
                if self._rendered != tid:
                    return
                self._since[tid] = n
                # A swarm names its agents as they finish; the layer
                # picture is much easier to read with "add-license" on a
                # card than "transaction 23".
                for e in evs:
                    if e.get("type") == "swarm_agent_done" and e.get("tx"):
                        self._agent_of[str(e["tx"])] = e.get("agent") or ""
                self.chat.append_events(evs)
            run_async(self, THREADS.events, tid, since, then=got)

        # The decision belongs to the newest turn's transaction.
        tx = t.get("tx")
        s = next((x for x in self.sessions if x.get("tx") == tx), None)
        if not s:
            self.chat.set_review(tx, None, [])
            return
        if s.get("status") != "awaiting-decision":
            self.chat.set_review(tx, s.get("status"), [])
            return

        # Is THIS conversation a swarm?
        #
        # Counting every awaiting-decision session in the system was wrong
        # in the ordinary case: sessions from older threads linger in
        # /run/agenttx, so a single-agent task saw "more than one waiting"
        # and hid its own Keep/Discard bar. The transcript already knows --
        # a swarm turn emits swarm_result and a single agent does not.
        swarm = self.chat._is_swarm

        def gotdiff(diff):
            if isinstance(diff, Exception):
                return
            self.chat.set_review(tx, "awaiting-decision", diff, swarm)
        run_async(self, GUEST.session_diff, tx, then=gotdiff)

    def rebuild_list(self):
        keep = self.selected
        self.list.blockSignals(True)
        self.list.clear()
        for t in self.threads:
            tx = t.get("tx")
            s = next((x for x in self.sessions if x.get("tx") == tx), None)
            # No live session for this thread's last transaction.
            #
            # Usually that just means the decision was made and txctl exited.
            # It also happens after a guest reboot: sessions live in
            # /run/agenttx, which is tmpfs, while threads live on disk -- so
            # every past conversation comes back with nothing to decide. A
            # bare "—" made that look like a broken row. There is genuinely
            # nothing pending, so say that.
            status = (s or {}).get("status") or "closed"
            # A status file saying "awaiting-decision" only means somebody
            # wrote that line. If the kernel has no such transaction the
            # session is gone -- killed, reaped by the watchdog, or lost
            # with a reboot -- and showing "decide" invites a click that
            # cannot do anything.
            if status == "awaiting-decision" and self._live_tx is not None \
                    and str(tx) not in self._live_tx:
                status = "closed"
            colour, word = STATUS.get(status, ("#8b9aad", status))
            w = QWidget()
            wl = QVBoxLayout(w)
            wl.setContentsMargins(11, 9, 11, 9)
            wl.setSpacing(3)
            top = QHBoxLayout()
            title = t.get("title") or "Task"
            # Elide here rather than letting the layout squeeze the pill:
            # a long task title otherwise pushed the status off the edge
            # and it rendered as "o decision pendin".
            if len(title) > 26:
                title = title[:25] + "…"
            lb = lab(title, bold=True, selectable=False)
            lb.setWordWrap(False)
            top.addWidget(lb)
            top.addStretch(1)
            pl = pill(word, colour)
            pl.setMinimumWidth(58)
            top.addWidget(pl)
            wl.addLayout(top)
            n = t.get("turns", 0)
            wl.addWidget(lab(f"{n} turn{'s' if n != 1 else ''}", "Muted",
                             selectable=False))
            wl.addWidget(lab(t.get("lower") or "", "Muted", selectable=False))
            w.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            it = QListWidgetItem()
            it.setSizeHint(QSize(0, w.sizeHint().height()))
            self.list.addItem(it)
            self.list.setItemWidget(it, w)
        self.list.blockSignals(False)

        for i, t in enumerate(self.threads):
            if t.get("id") == keep:
                self.list.setCurrentRow(i)
                return
        if self.threads and not self._want_new:
            # Only auto-open on first load. Never steal a conversation the
            # person deliberately left.
            self.list.setCurrentRow(0)

    def pick(self, row):
        if 0 <= row < len(self.threads):
            self._want_new = False
            self.selected = self.threads[row].get("id")
            self.refresh_chat()

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
