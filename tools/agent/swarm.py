#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/agent/swarm.py --- split a task across N agents, one transaction each.

    swarm.py THREAD_DIR TURN --lower DIR --agents N

WHY A SWARM IS THE INTERESTING CASE, AND NOT JUST A BIGGER ONE.

A single agent editing a folder never stresses anything this project
built. It takes a transaction, it writes, a human commits. The write-set
tracking has nothing to intersect with, the wait-for graph has one node,
and the deadlock detector has never had a cycle to find outside a test.

Put three agents on the same folder and all of that becomes load-bearing:

  - Two agents edit the same file. Each holds a private copy-on-write
    layer, so neither sees the other, and whichever commits second
    silently destroys the first one's work. That is the classic lost
    update, and it is what commit-time write-set intersection (P2-12) is
    for. This file detects it; the kernel should eventually refuse it.

  - Agent A needs a file agent B has not finished writing. That is a real
    wait-for edge between real transactions, which is what TX_IOC_WAIT
    exists to record and what the in-kernel detector exists to break.

So the swarm is not a feature bolted onto the sandbox. It is the first
workload that makes the sandbox's hard parts necessary, which is also
what makes it worth writing about.

WHAT THIS FILE DOES NOT CLAIM. Conflict detection here is in USERSPACE, by
comparing the upper layers after the agents stop. That is honest and
demonstrable, and it is strictly weaker than doing it in the kernel at
commit time: it cannot stop a commit, it can only tell a human that two
of them overlap. Saying otherwise would overstate what is built.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from brain import Brain, BrainError          # noqa: E402

TXCTL = "/usr/local/bin/txctl"
LOOP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "loop.py")
SESSION_DIR = "/run/agenttx"
TX_ROOT = "/var/lib/agenttx"

SPLIT_PROMPT = """Split this job into {n} independent parts that different
people could do at the same time without talking to each other.

JOB: {task}

FILES IN THE FOLDER:
{listing}

Answer with ONLY a JSON array of {n} objects, nothing else:
[{{"name": "short-slug", "task": "what this person does, in one or two sentences"}}]

Each part must name the specific files it will touch. Parts that edit the
same file are allowed but say so."""


def emit(events: str, rec: dict) -> None:
    rec.setdefault("at", time.time())
    with open(events, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def decompose(brain: Brain, task: str, listing: str, n: int) -> list[dict]:
    """
    Ask the model for N subtasks. Fall back to a trivial split.

    A 7B asked for strict JSON returns it most of the time and returns it
    wrapped in prose or a code fence the rest of the time, so the parse is
    forgiving. If it is unusable we do NOT fail the run: N agents all
    given the same task still produces a real concurrent workload, which
    is what the transaction machinery is here to handle. A swarm that
    refuses to start because the planner phrased itself badly would be a
    worse system than one that starts slightly redundantly.
    """
    try:
        msg = brain.chat([{"role": "user",
                           "content": SPLIT_PROMPT.format(n=n, task=task,
                                                          listing=listing)}],
                         temperature=0.2)
        body = (msg.get("content") or "").strip()
    except BrainError:
        body = ""

    m = re.search(r"\[.*\]", body, re.S)
    if m:
        try:
            parts = json.loads(m.group(0))
            out = []
            for i, p in enumerate(parts[:n]):
                if isinstance(p, dict) and p.get("task"):
                    slug = re.sub(r"[^a-z0-9-]", "-",
                                  str(p.get("name") or "agent%d" % (i + 1)).lower())
                    out.append({"name": slug.strip("-")[:24] or "agent%d" % (i + 1),
                                "task": str(p["task"])})
            if out:
                return out
        except json.JSONDecodeError:
            pass
    return [{"name": "agent%d" % (i + 1), "task": task} for i in range(n)]


def launch(thread_dir: str, turn: int, lower: str, runas: str,
           agent: str, model: str | None) -> subprocess.Popen:
    argv = [TXCTL, "session", "--lower", lower, "--as", runas, "--",
            "/usr/bin/env", "python3", LOOP, thread_dir, str(turn),
            "--agent", agent]
    if model:
        argv += ["--model", model]
    return subprocess.Popen(argv, cwd=lower, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)


def tx_of(proc_out: str) -> str:
    m = re.search(r"tx=(\d+) session started", proc_out)
    return m.group(1) if m else ""


def write_set(tx: str) -> set[str]:
    """Paths this transaction touched, read out of its copy-on-write layer."""
    upper = os.path.join(TX_ROOT, "tx-%s" % tx, "upper")
    out = set()
    for dirpath, _dirs, files in os.walk(upper):
        for f in files:
            full = os.path.join(dirpath, f)
            out.add(os.path.relpath(full, upper))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("thread_dir")
    ap.add_argument("turn", type=int)
    ap.add_argument("--lower", required=True)
    ap.add_argument("--agents", type=int, default=3)
    ap.add_argument("--runas", default="agent")
    ap.add_argument("--model", default=None)
    a = ap.parse_args()

    td = a.thread_dir
    events = os.path.join(td, "events.jsonl")
    with open(os.path.join(td, "turn-%d.prompt" % a.turn), encoding="utf-8") as f:
        task = f.read().strip()

    brain = Brain(model=a.model) if a.model else Brain()
    ok, detail = brain.available()
    if not ok:
        emit(events, {"type": "tx_error", "turn": a.turn,
                      "text": "The local model is not ready: %s" % detail})
        emit(events, {"type": "tx_turn_end", "turn": a.turn, "exit": 69})
        return 69

    try:
        listing = "\n".join(sorted(os.listdir(a.lower))[:60]) or "(empty)"
    except OSError:
        listing = "(unreadable)"

    emit(events, {"type": "tx_turn_start", "turn": a.turn, "prompt": task,
                  "cwd": a.lower, "swarm": a.agents})
    emit(events, {"type": "tx_notice", "turn": a.turn,
                  "text": "Planning how to split this across %d agents…" % a.agents})

    parts = decompose(brain, task, listing, a.agents)
    emit(events, {"type": "swarm_plan", "turn": a.turn,
                  "parts": parts})

    # Each agent gets its own subtask file and its own transaction.
    procs = {}
    for p in parts:
        with open(os.path.join(td, "agent-%s.prompt" % p["name"]), "w",
                  encoding="utf-8") as f:
            f.write(p["task"])
        procs[p["name"]] = launch(td, a.turn, a.lower, a.runas, p["name"],
                                  a.model)
        emit(events, {"type": "swarm_agent_start", "turn": a.turn,
                      "agent": p["name"], "task": p["task"]})
        # Stagger slightly: txctl registers a supervisor per session and
        # simultaneous registration is a race we do not need to take here.
        time.sleep(0.4)

    # Wait for every agent to reach its decision point.
    txs = {}
    deadline = time.time() + 1800
    while procs and time.time() < deadline:
        for name in list(procs):
            p = procs[name]
            if p.poll() is None:
                continue
            out = p.stdout.read() if p.stdout else ""
            txs[name] = tx_of(out)
            emit(events, {"type": "swarm_agent_done", "turn": a.turn,
                          "agent": name, "tx": txs[name]})
            del procs[name]
        time.sleep(0.5)

    # --- the point of the whole exercise ------------------------------
    #
    # Two agents that wrote the same path are a lost update waiting to
    # happen: each has its own copy-on-write layer, neither saw the
    # other, and whoever commits second wins silently.
    sets = {n: write_set(tx) for n, tx in txs.items() if tx}
    conflicts = []
    names = sorted(sets)
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            both = sorted(sets[x] & sets[y])
            if both:
                conflicts.append({"a": x, "b": y, "paths": both[:20]})

    emit(events, {"type": "swarm_result", "turn": a.turn,
                  "agents": [{"name": n, "tx": txs.get(n, ""),
                              "files": sorted(sets.get(n, ()))[:40]}
                             for n in sorted(txs)],
                  "conflicts": conflicts})
    if conflicts:
        lines = ["%s and %s both wrote: %s" % (c["a"], c["b"],
                                               ", ".join(c["paths"]))
                 for c in conflicts]
        emit(events, {"type": "tx_notice", "turn": a.turn,
                      "text": ("CONFLICT — these agents changed the same "
                               "files in separate transactions:\n  "
                               + "\n  ".join(lines)
                               + "\n\nNeither saw the other's version. If you "
                                 "keep both, the one you keep second wins and "
                                 "the first one's work is gone. Keep one, or "
                                 "keep one and re-run the other.")})

    emit(events, {"type": "tx_turn_end", "turn": a.turn, "exit": 0})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
