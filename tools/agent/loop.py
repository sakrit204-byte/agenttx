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
- CHANGING MANY FILES: do not edit them one by one. Write a small script
  and run it with the `run` tool, then check the result. Editing forty
  files individually will run out of steps long before it is finished.
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


def extract_call(text: str):
    """
    Pull a {"name": ..., "arguments": {...}} call out of model prose.

    Returns the call dict, or None if the text is really an answer.

    Deliberately strict about SHAPE and forgiving about WRAPPING: a model
    that meant to answer in prose must not have a stray JSON-looking
    fragment turned into a tool call, but one that fenced its call in
    ```json, or prefixed it with "Let me look:", should still be
    understood.
    """
    import re as _re

    candidates = []
    fence = _re.findall(r"```(?:json|tool_call)?\s*(\{.*?\})\s*```", text, _re.S)
    candidates.extend(fence)
    # Outermost brace span: a call carrying a nested "arguments" object
    # cannot be found with a non-greedy match.
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for c in candidates:
        try:
            o = json.loads(c)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict):
            continue
        # Accept both the bare shape and the OpenAI-ish wrapper some
        # models copy from their training data.
        if "function" in o and isinstance(o["function"], dict):
            o = o["function"]
        name = o.get("name")
        if not isinstance(name, str) or name not in T.DISPATCH:
            continue
        args = o.get("arguments", o.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue
        if not isinstance(args, dict):
            continue
        return {"name": name, "arguments": args}
    return None


def looks_like_narration(text: str) -> bool:
    """
    Is this prose ANNOUNCING the next step rather than reporting the last?

    A small model stops mid-task to write out what it is about to do. One
    real run ended with exactly "2. run: python3 add_spdx.py" -- the
    script written, never executed, the turn reported as complete.

    "Has it changed anything yet" is not enough on its own: writing the
    script IS a change, so that test passed and the turn still ended one
    step short of the point. These two patterns are narrow on purpose --
    a numbered step, or a tool name used as a label -- because the cost of
    a false positive is one wasted call and the cost of a false negative
    is a task that silently stops half-done.
    """
    import re as _re

    t = (text or "").strip()
    if not t:
        return False
    if _re.match(r"^\s*\d+\s*[.)]\s+\S", t):
        return True
    if _re.search(r"\b(run|write_file|edit_file|read_file|list_dir)\s*:\s*\S",
                  t):
        return True
    return False


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
    fail_streak = {}
    empty = 0
    nudged = 0
    did_work = False        # has any tool actually changed something?
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

        # A small model routinely writes the tool call into the prose.
        #
        # Ollama only fills `tool_calls` when the model's template
        # supports tools, and several good 7Bs -- qwen2.5-coder among
        # them -- have no such template. They emit exactly the right JSON
        # in `content` instead:
        #
        #     {"name": "list_dir", "arguments": {"path": "."}}
        #
        # Without this the loop saw prose, concluded the model was done,
        # and ended the turn having changed nothing. Observed on the very
        # first real task: one "assistant" event holding a perfectly
        # formed call, zero tools executed.
        #
        # Refusing to parse it would mean rejecting the model for a
        # formatting detail it got right in substance.
        if not calls and content:
            parsed = extract_call(content)
            if parsed:
                calls = [{"function": parsed}]
                content = ""      # it was a call, not an answer

        if content:
            t.text(content)
            summary = content

        if not calls and not content:
            # NOTHING came back: no text, no tool call.
            #
            # The first version treated this like any other tool-less
            # reply and ended the turn -- so an agent that had silently
            # produced nothing reported success with an empty summary,
            # which is the worst failure this harness can have. Seen on a
            # real task: two directory listings, an empty response, and a
            # cheerful "finished, 2 steps". A blank answer is not an
            # answer; push once, then say plainly that it stalled.
            empty += 1
            if empty <= 2:
                messages.append({
                    "role": "user",
                    "content": ("You returned an empty response. Either "
                                "call a tool to make progress, or give me "
                                "your final summary of what you changed.")})
                continue
            t.event("tx_notice",
                    text=("The agent stopped responding — it returned an "
                          "empty answer three times. Nothing was changed. "
                          "This usually means the task needs breaking into "
                          "smaller steps for a model this size."))
            break

        if not calls:
            # Prose with no tool call normally means "finished". But a
            # small model also stops to NARRATE the next step -- one run
            # ended with the literal text "2. run: python3 add_spdx.py"
            # after writing the script but never running it, and the turn
            # was reported as complete having changed nothing.
            #
            # The test is semantic, not string matching: if nothing in
            # this turn has actually changed anything yet, "I am done" is
            # almost certainly wrong. Nudge once. A genuinely read-only
            # task just repeats its answer and costs one extra call.
            if (not did_work or looks_like_narration(content)) and nudged < 2:
                nudged += 1
                messages.append({
                    "role": "user",
                    "content": ("You have not changed anything yet, so the "
                                "task is not finished. If you described a "
                                "next step, take it now by calling the "
                                "tool. If the task genuinely needs no "
                                "changes, say so explicitly.")})
                continue
            break

        messages.append({"role": "assistant", "content": content,
                         "tool_calls": calls})

        for i, c in enumerate(calls):
            fn = c.get("function") or {}
            name = fn.get("name") or "?"
            args = fn.get("arguments")
            # The agent name is IN the id.
            #
            # Every agent numbers its calls from zero, so in a swarm three
            # of them emit "c0_0" and anything keyed on that id -- the
            # chat's tool cards, any transcript reader -- attaches one
            # agent's result to another's call. It read as drain-low
            # claiming `queue` three times and getting `cache` back.
            tid = "%s_c%d_%d" % (a.agent or "x", steps, i)
            shown = args if isinstance(args, dict) else {"raw": str(args)[:300]}
            t.tool_use(tid, name, shown)

            sig = json.dumps([name, shown], sort_keys=True)
            out, err = T.call(root, name, args)

            # Repeated FAILURE of the same tool, even with different
            # arguments each time.
            #
            # The exact-repeat check below catches a model stuck on one
            # identical call. It does not catch the commoner shape: five
            # attempts at the same idea, each slightly reworded, all
            # failing the same way. Seen for real -- a 7B tried to cram a
            # for-loop into `python3 -c` five times running, got the same
            # SyntaxError each time, and never changed approach. Counting
            # consecutive errors per tool catches that, and the nudge goes
            # in the observation where the model is actually reading.
            if err:
                fail_streak[name] = fail_streak.get(name, 0) + 1
                if fail_streak[name] >= 3:
                    out += ("\n\n[That is %d failures in a row from %s. The "
                            "approach is not working -- change it. If a "
                            "command keeps failing to parse, write the "
                            "script to a file with write_file and then run "
                            "the file.]" % (fail_streak[name], name))
            else:
                fail_streak[name] = 0
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

            if not err and name in ("write_file", "edit_file", "run"):
                did_work = True
            t.tool_result(tid, out, err)
            messages.append({"role": "tool", "content": out})
        steps += 1

    if steps >= MAX_STEPS:
        t.event("tx_notice",
                text="Stopped: this turn hit its %d-step budget. What it "
                     "changed so far is below and still reviewable."
                     % MAX_STEPS)

    t.event("result", subtype="success" if summary else "empty",
            result=summary,
            num_turns=steps, duration_ms=int((time.time() - started) * 1000),
            is_error=False)
    t.event("tx_turn_end", exit=0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
