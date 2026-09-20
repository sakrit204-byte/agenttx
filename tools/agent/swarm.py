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
import glob
import json
import os
import pwd
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


def handover(path: str, user: str, create: bool = False) -> None:
    """Make `path` writable by the user the agents run as."""
    try:
        if create and not os.path.exists(path):
            open(path, "a").close()
        info = pwd.getpwnam(user)
        os.chown(path, info.pw_uid, info.pw_gid)
    except (OSError, KeyError):
        # Not fatal on its own -- if we are not root, or the user does not
        # exist, the agents may still be able to write. Failing the whole
        # swarm here would turn a permissions warning into an outage.
        pass


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
           agent: str, model: str | None,
           n_agents: int = 1) -> subprocess.Popen:
    argv = [TXCTL, "session", "--lower", lower, "--as", runas, "--",
            "/usr/bin/env", "python3", LOOP, thread_dir, str(turn),
            "--agent", agent]
    if model:
        argv += ["--model", model]
    env = dict(os.environ)
    if n_agents > 1:
        # Makes a three-way claim deadlock deterministic instead of
        # merely likely. See _first_claim_barrier() in tools.py.
        env["AGENTTX_CLAIM_BARRIER"] = str(n_agents)
    return subprocess.Popen(argv, cwd=lower, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            start_new_session=True, env=env)


def read_small(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(200).strip()
    except OSError:
        return ""


def session_of(agent: str, ignore: set) -> str:
    """
    The session directory belonging to `agent`, or "".

    Matched on the recorded command line, which carries --agent NAME.
    Session directories are named by transaction id, and ids restart at 1
    whenever the module reloads, so picking "the newest directory" is
    wrong in exactly the situation that matters -- several agents starting
    at once -- and picking by glob order is worse still, because the glob
    sorts lexically and session-9 comes after session-10.
    """
    needle = "--agent %s" % agent
    for d in glob.glob(os.path.join(SESSION_DIR, "session-*")):
        # Skip directories that already existed before we launched.
        #
        # Session directories are named by transaction id, ids restart at
        # 1 on every module reload, and a finished session leaves its
        # directory behind -- so a previous run's session-5 still has
        # "--agent alpha" in its cmd file and matches perfectly. The first
        # version picked those up: both agents were reported finished
        # 0.4s after launch, having produced no events at all, because it
        # was reading a dead run's status.
        if d in ignore:
            continue
        cmd = read_small(os.path.join(d, "cmd"))
        if needle in cmd and cmd.endswith(agent):
            return d
        if needle in cmd:
            # --agent NAME may be followed by --model, so an exact tail
            # match is not guaranteed; a token match is.
            if agent in cmd.split():
                return d
    return ""


def write_set(tx: str) -> set[str]:
    """Paths this transaction touched, read out of its copy-on-write layer."""
    upper = os.path.join(TX_ROOT, "tx-%s" % tx, "upper")
    out = set()
    for dirpath, _dirs, files in os.walk(upper):
        for f in files:
            full = os.path.join(dirpath, f)
            out.add(os.path.relpath(full, upper))
    return out


CLAIM_DIR = "/run/agenttx-claims"   # see tools.py: NOT inside the 0700 root


def declare_wait(waiter: str, holder: str, kind: str = "data") -> bool:
    """
    Register waiter -> holder in the kernel's wait-for graph.

    Returns False if the kernel refused, which is the interesting case:
    TX_IOC_WAIT runs cycle detection ON INSERT, so a refusal here means
    this edge would close a cycle. The kernel has already chosen a victim
    and scheduled its abort by the time we see the error.
    """
    r = subprocess.run([TXCTL, "wait", "--tx", str(waiter),
                        "--holder", str(holder), "--kind", kind],
                       capture_output=True, text=True, timeout=20)
    # EXIT CODE IS NOT THE ANSWER.
    #
    # Registering an edge that closes a cycle is a SUCCESS as far as
    # txctl is concerned: the edge went in, the kernel detected the
    # cycle, chose a victim and aborted it, all as designed -- so it
    # exits 0 and says "deadlock detected and broken". Reading only the
    # exit code meant the one event worth reporting never reached the
    # transcript, and the deadlock existed solely in dmesg.
    out = (r.stdout or "") + (r.stderr or "")
    broke = "deadlock" in out.lower()
    return (r.returncode == 0 and not broke), out.strip()


def pump_claims(events: str, turn: int, seen: set) -> None:
    """
    Turn blocked agents into wait-for edges, once each.

    An agent that cannot take a claim writes <resource>.want.<tx> naming
    who holds it. It cannot declare the edge itself: /dev/agenttx is
    root-only, deliberately, because anything that can open it can commit
    and abort transactions that are not its own. So the orchestrator --
    which is root, and is already supervising these agents -- does it.
    """
    try:
        names = os.listdir(CLAIM_DIR)
    except OSError:
        return
    for n in names:
        if ".want." not in n:
            continue
        resource, _, waiter = n.partition(".want.")
        try:
            with open(os.path.join(CLAIM_DIR, n)) as f:
                holder = f.read().strip()
        except OSError:
            continue
        if not holder or holder == waiter:
            continue
        key = (waiter, holder)
        if key in seen:
            continue
        seen.add(key)
        ok, detail = declare_wait(waiter, holder)
        emit(events, {"type": "tx_wait_edge", "turn": turn,
                      "waiter": waiter, "holder": holder,
                      "resource": resource, "accepted": ok,
                      "detail": detail})
        if not ok:
            emit(events, {"type": "tx_notice", "turn": turn,
                          "text": ("DEADLOCK — transaction %s waiting on %s "
                                   "for '%s' closed a cycle. Every one of "
                                   "those waits was legitimate; together "
                                   "they cannot all be satisfied. The kernel "
                                   "found it the moment the edge went in, "
                                   "picked the least-severe transaction in "
                                   "the cycle and aborted it. That agent's "
                                   "work is gone; the others carry on.\n\n%s"
                                   % (waiter, holder, resource, detail))})


def release_claims(txs) -> None:
    """Drop claims held by transactions that are over."""
    try:
        names = os.listdir(CLAIM_DIR)
    except OSError:
        return
    alive = {str(t) for t in txs}
    for n in names:
        p = os.path.join(CLAIM_DIR, n)
        if ".want." in n:
            continue
        try:
            with open(p) as f:
                holder = f.read().strip()
        except OSError:
            continue
        if holder and holder not in alive:
            try:
                os.unlink(p)
            except OSError:
                pass


def live_txs() -> set:
    out = set()
    try:
        with open("/sys/kernel/debug/agenttx/transactions") as f:
            for line in f.read().splitlines()[1:]:
                f0 = line.split()
                if f0:
                    out.add(f0[0])
    except OSError:
        pass
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

    # The transcript is shared, and its writers do not share a uid.
    #
    # This orchestrator runs as root; every agent it starts runs as
    # --runas. Whoever creates events.jsonl first owns it, and root
    # creating it makes it unwritable by the agents, which then die with
    # PermissionError before emitting a single event. Seen exactly that
    # way: agents launched, transcript stayed at the orchestrator's five
    # lines, and the swarm reported two agents that had done nothing.
    #
    # Any chown in the launcher happens BEFORE this file exists, so it has
    # to be done here, by the process that creates it.
    handover(events, a.runas, create=True)
    for name in ("turn-%d.prompt" % a.turn,):
        handover(os.path.join(td, name), a.runas)

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

    # Every session directory that exists right now belongs to somebody
    # else, or to a run that is already over. See session_of().
    pre_existing = set(glob.glob(os.path.join(SESSION_DIR, "session-*")))

    # Claims from earlier runs are held by transactions that no longer
    # exist; leaving them makes the first agent wait on a ghost.
    # Sticky, world-writable, like /tmp.
    #
    # /run/agenttx is 0700 root, so an agent cannot create anything under
    # it -- every claim came back PermissionError, and the agents cheerfully
    # reported success anyway. The orchestrator is root and makes this
    # directory once, here. The sticky bit matters: agents may create their
    # own lock files but not remove each other's, which is exactly the
    # property a shared lock directory needs.
    os.makedirs(CLAIM_DIR, exist_ok=True)
    os.chmod(CLAIM_DIR, 0o1777)
    release_claims(live_txs())

    # Each agent gets its own subtask file and its own transaction.
    procs = {}
    for p in parts:
        with open(os.path.join(td, "agent-%s.prompt" % p["name"]), "w",
                  encoding="utf-8") as f:
            f.write(p["task"])
        procs[p["name"]] = launch(td, a.turn, a.lower, a.runas, p["name"],
                                  a.model, len(parts))
        emit(events, {"type": "swarm_agent_start", "turn": a.turn,
                      "agent": p["name"], "task": p["task"]})
        # Stagger slightly: txctl registers a supervisor per session and
        # simultaneous registration is a race we do not need to take here.
        time.sleep(0.4)

    # Wait for every agent to reach its DECISION POINT, not its exit.
    #
    # The first version waited on p.poll(), which never returns: `txctl
    # session` is supposed to hold its transaction open until a human
    # decides, for up to an hour. So the orchestrator sat there until its
    # own timeout while every agent had long since finished working. The
    # test caught this as a 420s timeout.
    #
    # The session is found by its cmd file rather than by reading the
    # child's stdout, because reading stdout to completion is the same
    # blocking mistake in a different shape. txctl records the command
    # line it was started with, and that line carries --agent NAME.
    txs = {}
    deadline = time.time() + 1800
    pending = set(procs)
    seen_edges = set()
    while pending and time.time() < deadline:
        # Blocked agents become wait-for edges, and finished transactions
        # give their claims back. Both have to happen while the agents are
        # still running, which is why this is in the wait loop and not
        # after it.
        pump_claims(events, a.turn, seen_edges)
        release_claims(live_txs())
        for name in sorted(pending):
            d = session_of(name, pre_existing)
            if not d:
                continue
            st = read_small(os.path.join(d, "status"))
            if st in ("awaiting-decision", "committed", "aborted", "failed"):
                txs[name] = os.path.basename(d).split("session-")[-1]
                emit(events, {"type": "swarm_agent_done", "turn": a.turn,
                              "agent": name, "tx": txs[name], "status": st})
                pending.discard(name)
        time.sleep(0.5)

    for name in sorted(pending):
        emit(events, {"type": "tx_notice", "turn": a.turn,
                      "text": "Agent %s did not finish in time." % name})

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
