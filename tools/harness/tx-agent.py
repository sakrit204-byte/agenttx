#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/harness/tx-agent.py --- run ONE turn of an agent thread inside a
transaction, and write the loop out as it happens.

WHY THERE IS NO AGENT LOOP IN THIS FILE.

The obvious way to build a harness is to write the loop yourself: ask the
model what to do, run it, feed back the result, repeat. That loop already
exists, is far better than one we would write, and is sitting in $PATH.
Claude Code plans, calls tools, reads what came back and iterates until the
task is actually done. So this file does not think. It starts that loop,
puts a transaction around it, and turns its output into something the
desktop app can render.

What we add is the part Claude Code cannot do for itself: every file the
agent writes lands in a copy-on-write layer, and none of it is real until a
human presses Keep. That is why --dangerously-skip-permissions is correct
here rather than reckless. The flag is named for a world without this
sandbox, where skipping approval means an agent can do anything to your
machine. Inside a transaction the damage is a directory we can delete.

THE TRANSCRIPT LIVES OUTSIDE THE TRANSACTION, ON PURPOSE.

The thread directory is under /var/lib/agenttx/threads/, never inside the
protected lower directory. If the transcript were inside, pressing Discard
would erase the record of what the agent did along with the changes, and
you would be left with the one thing this project exists to prevent: an
agent that did something you cannot inspect afterwards. Discard throws away
the work. It must not throw away the account of the work.

Usage:
    tx-agent.py THREAD_DIR TURN SESSION_UUID
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time


def now() -> float:
    return time.time()


class Transcript:
    """Append-only event log. One JSON object per line."""

    def __init__(self, path: str, turn: int):
        self.path = path
        self.turn = turn

    def emit(self, kind: str, **fields) -> None:
        rec = {"type": kind, "turn": self.turn, "at": now()}
        rec.update(fields)
        # Open-append-close per record rather than holding the file open.
        # The reader is a separate ssh command that may arrive at any
        # moment, and a half-flushed line would be a parse error it could
        # never recover from. One line, one write, always complete.
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def raw(self, line: str) -> None:
        """Pass a Claude Code event through, tagged with our turn number."""
        line = line.strip()
        if not line:
            return
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            # Not JSON. Claude Code prints the odd human-readable notice on
            # stdout; keep it rather than dropping it, because when
            # something is wrong that notice is usually the reason.
            self.emit("tx_notice", text=line[:4000])
            return
        obj["turn"] = self.turn
        obj["at"] = now()
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())


def claude_bin(thread_dir: str) -> str:
    """
    Which binary to drive. Normally "claude" from $PATH.

    A file named claude-bin in the thread directory overrides it. That is a
    TEST SEAM and it is a file rather than an environment variable on
    purpose: this process is started by `txctl session --as agent`, three
    exec layers and a privilege drop away from whoever wanted to set it, and
    a knob that only works when the plumbing happens to preserve env is a
    knob that fails silently later. A file survives all of it and can be
    read back afterwards to prove which binary actually ran.

    It also lets the agent loop be tested with a scripted transcript, so the
    tests neither need the sandbox agent signed in nor spend anything.
    """
    try:
        with open(os.path.join(thread_dir, "claude-bin"), encoding="utf-8") as f:
            override = f.read().strip()
        if override:
            return override
    except OSError:
        pass
    return "claude"


def build_argv(prompt: str, session_uuid: str, first_turn: bool,
               binary: str = "claude") -> list[str]:
    argv = [
        binary,
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        # stream-json refuses to run without it; the events we render are
        # exactly what --verbose turns on.
        "--verbose",
        "-p", prompt,
    ]
    if first_turn:
        # Choose the id rather than parsing it back out of the init event.
        # Resuming then needs no bookkeeping and cannot pick the wrong
        # conversation if two threads start in the same second.
        argv[1:1] = ["--session-id", session_uuid]
    else:
        argv[1:1] = ["--resume", session_uuid]
    return argv


def child_env() -> dict:
    env = dict(os.environ)
    # NEVER let this turn bill an API key.
    #
    # The operator's standing constraint is that Claude Code's subscription
    # auth is fine and metered API billing is not. Those two live in the
    # same binary and differ by one environment variable, so an
    # ANTHROPIC_API_KEY inherited from anywhere -- a stray export, a
    # /etc/environment line, a helpful shell profile -- would silently move
    # every turn onto a paid meter. Strip it here rather than trusting
    # every environment this ever runs in.
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    thread_dir, turn_s, session_uuid = sys.argv[1], sys.argv[2], sys.argv[3]
    turn = int(turn_s)

    events = os.path.join(thread_dir, "events.jsonl")
    prompt_path = os.path.join(thread_dir, "turn-%d.prompt" % turn)
    try:
        with open(prompt_path, encoding="utf-8") as f:
            prompt = f.read()
    except OSError as e:
        print("tx-agent: cannot read %s: %s" % (prompt_path, e), file=sys.stderr)
        return 2

    t = Transcript(events, turn)
    t.emit("tx_turn_start", prompt=prompt, cwd=os.getcwd(),
           tx=os.environ.get("AGENTTX_TX", ""))

    argv = build_argv(prompt, session_uuid, first_turn=(turn == 1),
                      binary=claude_bin(thread_dir))
    try:
        p = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,   # or Claude Code waits on piped stdin
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,                  # line buffered: the UI streams this
            env=child_env(),
        )
    except FileNotFoundError:
        t.emit("tx_error",
               text="%s is not installed in the sandbox" % argv[0])
        t.emit("tx_turn_end", exit=127)
        return 127

    assert p.stdout is not None
    for line in p.stdout:
        t.raw(line)
    p.wait()

    err = (p.stderr.read() if p.stderr else "") or ""
    if p.returncode != 0:
        # The overwhelmingly likely cause, and the one worth naming
        # precisely, is that nobody has signed the sandbox agent in yet.
        low = err.lower()
        if "login" in low or "authenticat" in low or "not logged in" in low:
            t.emit("tx_error", text=(
                "The sandbox agent is not signed in. In the guest, run:\n"
                "    su - agent\n"
                "    claude setup-token\n"
                "That uses the subscription, not a metered API key."),
                stderr=err[:4000])
        else:
            t.emit("tx_error", text=err.strip()[:4000] or
                   "claude exited %d with no message" % p.returncode)

    t.emit("tx_turn_end", exit=p.returncode)
    return p.returncode


if __name__ == "__main__":
    raise SystemExit(main())
