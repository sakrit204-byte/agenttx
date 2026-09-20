#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/agent/loop.py --- the agentic loop, run inside a transaction.

    loop.py THREAD_DIR TURN [--agent NAME] [--role TEXT]

This is the piece we now own. With Claude Code the loop came for free; a
raw 7B has no harness at all, so think-act-observe is ours to write, and
ours to keep honest.

WHAT A SMALL MODEL DOES THAT A LARGE ONE DOES NOT, and what each costs:

  It never stops.        A 7B will happily read the same file eleven times.
                         Hence a hard step budget and a wall-clock budget,
                         both of which end the turn cleanly rather than
                         leaving a transaction open.

  It loops on failure.   Told "that text appears 3 times", it retries the
                         identical edit. We detect a repeated (tool,
                         arguments) pair and say so in the observation,
                         which is the one nudge that reliably breaks it.

  It forgets the goal.   Long tool outputs push the task out of attention,
                         so the task is restated in the system prompt AND
                         the transcript is trimmed from the middle, never
                         from the front.

  It answers in prose    when it should call a tool. We accept that as the
                         end of the turn rather than nagging, because a
                         7B pushed to "use a tool" invents one.

Every event it emits has the same shape as Claude Code's
--output-format stream-json, so tools/app renders this with no changes.
That was worth designing for: the UI should not know which brain is
answering.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from brain import Brain, BrainError          # noqa: E402
import tools as T                            # noqa: E402

MAX_STEPS = int(os.environ.get("AGENTTX_MAX_STEPS", "24"))
MAX_SECONDS = int(os.environ.get("AGENTTX_MAX_SECONDS", "900"))
KEEP_HEAD = 6          # messages kept verbatim at the start when trimming
KEEP_TAIL = 14         # and at the end

SYSTEM = """You are a coding agent working inside a sandboxed folder.

THE TASK: {task}

{role}
Rules:
- Work only inside the folder. All paths are relative to it.
- Look before you leap: list_dir and read_file before writing or editing.
- Make the change with write_file or edit_file. Do not just describe it.
- One tool call at a time. Wait for the result before the next.
- When the task is genuinely done, reply with a short plain-text summary
  of what you changed and call no more tools.

You do not need permission for anything. Every change you make goes into a
sandbox layer and a human reviews it afterwards.
"""


class Transcript:
    """Append-only events, identical in shape to tx-agent.py's."""

    def __init__(self, path: str, turn: int, agent: str):
        self.path, self.turn, self.agent = path, turn, agent

    def _w(self, rec: dict) -> None:
        rec.setdefault("turn", self.turn)
        rec.setdefault("at", time.time())
        if self.agent:
            rec.setdefault("agent", self.agent)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def text(self, s: str) -> None:
        self._w({"type": "assistant",
                 "message": {"role": "assistant",
                             "content": [{"type": "text", "text": s}]}})

    def tool_use(self, tid: str, name: str, args: dict) -> None:
        self._w({"type": "assistant",
                 "message": {"role": "assistant",
                             "content": [{"type": "tool_use", "id": tid,
                                          "name": name, "input": args}]}})

    def tool_result(self, tid: str, out: str, err: bool) -> None:
        self._w({"type": "user",
                 "message": {"role": "user",
                             "content": [{"type": "tool_result",
                                          "tool_use_id": tid,
                                          "content": out,
                                          "is_error": err}]}})

    def event(self, kind: str, **kw) -> None:
        kw["type"] = kind
        self._w(kw)


def trim(messages: list[dict]) -> list[dict]:
    """
    Keep the conversation inside the model's window.

    Trimmed from the MIDDLE. The front holds the system prompt and the
    first look at the folder, which is what stops the model rediscovering
    the layout every few steps; the end is what it is currently doing.
    Dropping the front -- the obvious ring-buffer approach -- makes a
    small model restart the task from scratch halfway through.
    """
    if len(messages) <= KEEP_HEAD + KEEP_TAIL:
        return messages
    dropped = len(messages) - KEEP_HEAD - KEEP_TAIL
    return (messages[:KEEP_HEAD]
            + [{"role": "user",
                "content": "[%d earlier steps omitted to save space]" % dropped}]
            + messages[-KEEP_TAIL:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("thread_dir")
    ap.add_argument("turn", type=int)
    ap.add_argument("--agent", default="")
    ap.add_argument("--role", default="")
    ap.add_argument("--model", default=None)
    a = ap.parse_args()

    td = a.thread_dir
    events = os.path.join(td, "events.jsonl")
    t = Transcript(events, a.turn, a.agent)

    prompt_path = os.path.join(td, "turn-%d.prompt" % a.turn)
    if a.agent:
        # A swarm member gets its own subtask file if one was written.
        sub = os.path.join(td, "agent-%s.prompt" % a.agent)
        if os.path.exists(sub):
            prompt_path = sub
    try:
        with open(prompt_path, encoding="utf-8") as f:
            task = f.read().strip()
    except OSError as e:
        print("loop: cannot read %s: %s" % (prompt_path, e), file=sys.stderr)
        return 2

    root = os.getcwd()
    t.event("tx_turn_start", prompt=task, cwd=root,
            tx=os.environ.get("AGENTTX_TX", ""))

    brain = Brain(model=a.model) if a.model else Brain()
    ok, detail = brain.available()
    if not ok:
        t.event("tx_error", text=(
            "The local model is not ready: %s\n\n"
            "On the host:\n"
            "    ollama serve\n"
            "    ollama pull %s\n"
            "Nothing here uses a paid API." % (detail, brain.model)))
        t.event("tx_turn_end", exit=69)
        return 69

    messages = [{"role": "system",
                 "content": SYSTEM.format(task=task,
                                          role=(a.role + "\n") if a.role else "")},
                {"role": "user", "content": task}]

    started = time.time()
    steps = 0
    last_call = None
    repeats = 0
    summary = ""

    while steps < MAX_STEPS:
        if time.time() - started > MAX_SECONDS:
            t.event("tx_notice",
                    text="Stopped: this turn hit its %ds time budget."
                         % MAX_SECONDS)
            break
        try:
            msg = brain.chat(trim(messages), tools=T.SCHEMA)
        except BrainError as e:
            t.event("tx_error", text=str(e))
            t.event("tx_turn_end", exit=70)
            return 70

        calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()

        if content:
            t.text(content)
            summary = content
        if not calls:
            break                      # prose with no tool call ends the turn

        messages.append({"role": "assistant", "content": content,
                         "tool_calls": calls})

        for i, c in enumerate(calls):
            fn = c.get("function") or {}
            name = fn.get("name") or "?"
            args = fn.get("arguments")
            tid = "c%d_%d" % (steps, i)
            shown = args if isinstance(args, dict) else {"raw": str(args)[:300]}
            t.tool_use(tid, name, shown)

            sig = json.dumps([name, shown], sort_keys=True)
            out, err = T.call(root, name, args)
            if sig == last_call:
                repeats += 1
                # Say it in the OBSERVATION, where the model is actually
                # looking. A system-prompt warning about repetition is read
                # once and forgotten; this lands at the moment it repeats.
                out += ("\n\n[You just made this exact call again and got "
                        "the same result. Do something different: read the "
                        "file, try another path, or finish and summarise.]")
                if repeats >= 3:
                    t.tool_result(tid, out, True)
                    t.event("tx_notice",
                            text="Stopped: the agent repeated the same call "
                                 "three times without progress.")
                    messages.append({"role": "tool", "content": out})
                    steps = MAX_STEPS
                    break
            else:
                repeats = 0
            last_call = sig

            t.tool_result(tid, out, err)
            messages.append({"role": "tool", "content": out})
        steps += 1

    if steps >= MAX_STEPS:
        t.event("tx_notice",
                text="Stopped: this turn hit its %d-step budget. What it "
                     "changed so far is below and still reviewable."
                     % MAX_STEPS)

    t.event("result", subtype="success", result=summary,
            num_turns=steps, duration_ms=int((time.time() - started) * 1000),
            is_error=False)
    t.event("tx_turn_end", exit=0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
