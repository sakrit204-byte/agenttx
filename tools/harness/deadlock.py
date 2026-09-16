#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/harness/deadlock.py --- the AgentTx concurrency model, runnable.

    python3 tools/harness/deadlock.py --list
    python3 tools/harness/deadlock.py --scenario subagents --explain
    python3 tools/harness/deadlock.py --all --policy least-severe
    python3 tools/harness/deadlock.py --scenario classic --jsonl out.jsonl

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
It is an executable model of the design in docs/deadlock.md: transactions,
a wait-for graph with three edge kinds, cycle detection, and victim
selection under the constraint that a DOOMED transaction cannot be aborted.

It is NOT a measurement of the kernel, because the kernel does not implement
any of this yet. Every event it emits carries `sim: true`, and the summary
says so. A simulator whose output could be mistaken for a measurement is the
same failure docs/STATUS.md section 5 is about, and this project has already
been bitten by it once (docs/journal/p4.md).

What it IS good for: it pins the semantics down before the kernel code is
written, it shows the shapes a real agent system produces, and it runs the
four scenarios a viva should be shown.

Owner: P1 (semantics) with P4 (evaluation).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


# --- mirrors include/agenttx.h -------------------------------------------
class Klass(IntEnum):
    REVERSIBLE = 0
    DEFERRABLE = 1
    COMPENSABLE = 2
    IRREVOCABLE = 3


class State(IntEnum):
    NONE = 0
    ACTIVE = 1
    DOOMED = 2
    COMMITTING = 3
    ABORTING = 4
    DONE = 5
    ABORTED = 6
    FAILED = 7


class Wait(IntEnum):
    DATA = 0        # A's commit would conflict with B's write set
    ESCALATE = 1    # A is asleep in a hook awaiting an approval only B gives
    OUTPUT = 2      # A needs a result B has not committed


WAIT_NAME = {Wait.DATA: "DATA", Wait.ESCALATE: "ESCALATE", Wait.OUTPUT: "OUTPUT"}

# Which edge kinds can be broken by aborting the *waiter*.
# ESCALATE cannot: the waiter is asleep inside an LSM hook and is not
# running any code that could notice an abort. Breaking an ESCALATE edge
# means denying the approval, which is a different action with a different
# authority, and pretending otherwise would make the model lie in the
# direction that flatters it.
BREAKABLE_BY_WAITER_ABORT = {Wait.DATA, Wait.OUTPUT}


@dataclass
class Tx:
    tx_id: int
    agent: str
    state: State = State.ACTIVE
    worst: Klass = Klass.REVERSIBLE
    write_set: set = field(default_factory=set)
    wal: int = 0
    parent: str | None = None
    n_aborts: int = 0

    @property
    def doomed(self) -> bool:
        return self.state == State.DOOMED

    @property
    def abortable(self) -> bool:
        """
        The whole point. include/agenttx.h has no DOOMED -> ABORTING edge,
        so this is not a policy choice the recovery code may override.
        """
        return self.state in (State.ACTIVE,)

    def cost(self) -> int:
        """Work lost if this transaction is aborted."""
        return len(self.write_set) + self.wal


class World:
    def __init__(self, policy: str = "least-severe", seed: int = 1,
                 emit=None) -> None:
        self.txs: dict[int, Tx] = {}
        self.edges: list[tuple[int, int, Wait]] = []
        self.holder: dict[str, int] = {}     # resource path -> tx holding it
        self.policy = policy
        self.rng = random.Random(seed)
        self.seq = 0
        self.events: list[dict] = []
        self._emit = emit
        self.next_id = 1

    # --- event log ------------------------------------------------------
    def ev(self, kind: str, **kw) -> None:
        self.seq += 1
        e = {"sim": True, "seq": self.seq, "kind": kind, **kw}
        self.events.append(e)
        if self._emit:
            self._emit(e)

    # --- transaction lifecycle -----------------------------------------
    def begin(self, agent: str, parent: str | None = None) -> Tx:
        tx = Tx(self.next_id, agent, parent=parent)
        self.next_id += 1
        self.txs[tx.tx_id] = tx
        self.ev("begin", tx=tx.tx_id, agent=agent, parent=parent)
        return tx

    def write(self, tx: Tx, path: str) -> None:
        tx.write_set.add(path)
        self.holder.setdefault(path, tx.tx_id)
        self.ev("write", tx=tx.tx_id, agent=tx.agent, path=path,
                write_set=len(tx.write_set))

    def effect(self, tx: Tx, klass: Klass, what: str) -> None:
        """
        An intercepted outbound effect. IRREVOCABLE dooms the transaction,
        exactly as tx_note_class() does in src/core/state.c -- watermark
        first, then the transition, so a reader can never see DOOMED with a
        watermark that does not justify it.
        """
        if klass > tx.worst:
            tx.worst = klass
        if klass != Klass.IRREVOCABLE:
            tx.wal += 1
        if klass == Klass.IRREVOCABLE and tx.state == State.ACTIVE:
            tx.state = State.DOOMED
            self.ev("doomed", tx=tx.tx_id, agent=tx.agent, what=what,
                    worst=int(tx.worst))
        else:
            self.ev("effect", tx=tx.tx_id, agent=tx.agent, what=what,
                    klass=int(klass), klass_name=klass.name.lower(),
                    wal=tx.wal)

    def wait(self, waiter: Tx, holder: Tx, kind: Wait, why: str) -> list[int]:
        """Register a wait edge and check for a cycle immediately."""
        self.edges.append((waiter.tx_id, holder.tx_id, kind))
        # NOTE the field name: `wait_kind`, not `kind`.  Every event already
        # carries `kind` for the event type, and naming the edge type `kind`
        # too collided with it in ev(**kw).
        self.ev("wait", tx=waiter.tx_id, agent=waiter.agent,
                holder=holder.tx_id, holder_agent=holder.agent,
                wait_kind=int(kind), wait_name=WAIT_NAME[kind], why=why)
        cycle = self.find_cycle()
        if cycle:
            self.resolve(cycle)
        return cycle

    # --- detection ------------------------------------------------------
    def find_cycle(self) -> list[int]:
        """
        Plain DFS with three-colouring. The graph has one node per live
        transaction -- tens, not thousands -- so this runs on every edge
        insertion rather than on a timer. A timer would mean a deadlock is
        undetected for up to one period, and the whole point is that the
        kernel notices immediately.
        """
        adj: dict[int, list[int]] = {}
        for w, h, _k in self.edges:
            adj.setdefault(w, []).append(h)

        WHITE, GREY, BLACK = 0, 1, 2
        colour: dict[int, int] = {}
        stack: list[int] = []

        def dfs(n: int) -> list[int] | None:
            colour[n] = GREY
            stack.append(n)
            for m in adj.get(n, []):
                if m not in self.txs:
                    continue
                c = colour.get(m, WHITE)
                if c == GREY:                       # back edge: cycle
                    return stack[stack.index(m):]
                if c == WHITE:
                    r = dfs(m)
                    if r:
                        return r
            colour[n] = BLACK
            stack.pop()
            return None

        for n in list(adj):
            if colour.get(n, WHITE) == WHITE and n in self.txs:
                r = dfs(n)
                if r:
                    return r
        return []

    # --- recovery -------------------------------------------------------
    def choose_victim(self, cycle: list[int]) -> Tx | None:
        cands = [self.txs[t] for t in cycle if self.txs[t].abortable]
        if not cands:
            return None
        if self.policy == "youngest":
            return max(cands, key=lambda t: t.tx_id)
        if self.policy == "least-work":
            return min(cands, key=lambda t: (t.cost(), t.tx_id))
        if self.policy == "least-severe":
            # Aware of what the taxonomy is FOR: a transaction that emitted
            # nothing costs nothing to abort; one that emitted a compensable
            # effect costs a compensation. Ties broken by work lost, then by
            # how often this agent has already been the victim -- which is
            # the cheap contention manager that stops starvation.
            return min(cands, key=lambda t: (int(t.worst), t.n_aborts,
                                             t.cost(), t.tx_id))
        if self.policy == "random":
            return self.rng.choice(cands)
        raise SystemExit(f"unknown policy {self.policy}")

    def resolve(self, cycle: list[int]) -> None:
        members = [self.txs[t] for t in cycle]
        kinds = [WAIT_NAME[k] for (w, h, k) in self.edges
                 if w in cycle and h in cycle]
        self.ev("deadlock", cycle=cycle,
                agents=[t.agent for t in members],
                states=[t.state.name.lower() for t in members],
                worst=[int(t.worst) for t in members],
                edge_kinds=sorted(set(kinds)),
                n_doomed=sum(1 for t in members if t.doomed))

        victim = self.choose_victim(cycle)
        if victim is None:
            # THE finding. Every member emitted an irrevocable effect, so
            # none can be rolled back, so the mechanism that breaks every
            # other cycle cannot break this one.
            self.ev("unresolvable", cycle=cycle,
                    agents=[t.agent for t in members],
                    reason="every transaction in the cycle is DOOMED; "
                           "DOOMED -> ABORTING does not exist",
                    remaining_moves=[
                        "deny an ESCALATE approval (the effect already "
                        "happened, so denial dooms nothing further)",
                        "escalate the whole cycle to a human",
                    ])
            for t in members:
                t.state = State.FAILED
            return

        forced = sum(1 for t in members if t.abortable) == 1
        self.ev("victim", tx=victim.tx_id, agent=victim.agent,
                policy=self.policy, worst=int(victim.worst),
                cost=victim.cost(), forced=forced,
                n_abortable=sum(1 for t in members if t.abortable),
                n_doomed=sum(1 for t in members if t.doomed))
        self.abort(victim, "deadlock")

    def abort(self, tx: Tx, reason: str) -> None:
        tx.state = State.ABORTED
        tx.n_aborts += 1
        for p in list(self.holder):
            if self.holder[p] == tx.tx_id:
                del self.holder[p]

        # Second-order consequence, and not a nicety: if the victim was the
        # HOLDER of an ESCALATE edge it was the approver, and its waiters are
        # asleep inside an LSM hook waiting for an answer that will now never
        # come.  P3-10's tracker note already fixes the policy --- "Timeout
        # policy = deny" --- so the honest thing is to say that the abort
        # converted a pending approval into a denial, rather than to drop the
        # edge and let the waiter look free.
        stranded = [w for (w, h, k) in self.edges
                    if h == tx.tx_id and k == Wait.ESCALATE]
        if stranded:
            self.ev("stranded", tx=tx.tx_id, agent=tx.agent,
                    waiters=stranded,
                    waiter_agents=[self.txs[t].agent for t in stranded
                                   if t in self.txs],
                    consequence="the approver was the victim; these waiters "
                                "are asleep in a hook awaiting an approval "
                                "nobody can now give",
                    policy="P3-10: timeout is deny — they will be denied, "
                           "not resumed")

        self.edges = [(w, h, k) for (w, h, k) in self.edges
                      if w != tx.tx_id and h != tx.tx_id]
        self.ev("abort", tx=tx.tx_id, agent=tx.agent, reason=reason,
                released=sorted(tx.write_set), wal_dropped=tx.wal)
        tx.write_set.clear()
        tx.wal = 0

    def commit(self, tx: Tx) -> None:
        tx.state = State.DONE
        for p in list(self.holder):
            if self.holder[p] == tx.tx_id:
                del self.holder[p]
        self.ev("commit", tx=tx.tx_id, agent=tx.agent, worst=int(tx.worst))


# =========================================================================
# Scenarios
# =========================================================================
SCENARIOS: dict[str, dict] = {}


def scenario(name: str, blurb: str, teaches: str):
    def deco(fn):
        SCENARIOS[name] = {"fn": fn, "blurb": blurb, "teaches": teaches}
        return fn
    return deco


@scenario("classic",
          "Two agents, circular wait on two files. The textbook case.",
          "All four Coffman conditions hold; abort supplies the missing "
          "preemption and the cycle breaks. This is the easy case and it "
          "should look easy.")
def sc_classic(w: World):
    a = w.begin("agent-A")
    b = w.begin("agent-B")
    w.write(a, "/repo/src/main.c")
    w.write(b, "/repo/src/util.c")
    w.effect(a, Klass.REVERSIBLE, "write main.c")
    w.effect(b, Klass.DEFERRABLE, "POST /webhook build-started")
    # A now needs what B holds, and then B needs what A holds.
    w.wait(a, b, Wait.OUTPUT, "A needs util.c, which B has not committed")
    w.wait(b, a, Wait.OUTPUT, "B needs main.c, which A has not committed")


@scenario("subagents",
          "A parent agent and two subagents; the cycle runs through an "
          "ESCALATE edge.",
          "This is the shape a real agent system produces. Nothing is "
          "misconfigured -- every edge is a legitimate wait -- and the "
          "cycle still forms. The parent is the approver, so the "
          "escalation it must answer is behind the result it is waiting for.")
def sc_subagents(w: World):
    p = w.begin("parent")
    s1 = w.begin("subagent-1", parent="parent")
    s2 = w.begin("subagent-2", parent="parent")

    w.write(p, "/repo/plan.md")
    w.write(s1, "/repo/src/api.c")
    w.write(s2, "/repo/tests/test_api.c")

    w.effect(s1, Klass.DEFERRABLE, "POST /notify api rewritten")
    w.effect(s2, Klass.REVERSIBLE, "write test file")

    # subagent-2 cannot finish until subagent-1's API lands
    w.wait(s2, s1, Wait.OUTPUT, "test needs subagent-1's api.c")
    # subagent-1 hits something irrevocable and blocks on approval
    w.effect(s1, Klass.COMPENSABLE, "charge the build-minutes account")
    w.wait(s1, p, Wait.ESCALATE, "subagent-1 blocked awaiting parent approval")
    # and the parent will not approve until it sees the tests pass
    w.wait(p, s2, Wait.OUTPUT, "parent waits for subagent-2's test result")


@scenario("doomed",
          "One member of the cycle is DOOMED; victim choice is forced.",
          "Recovery is constrained but not defeated. Note the `forced: true` "
          "in the victim event: the policy did not get to choose, it got "
          "told. 'Abort the cheapest' silently degrades to 'abort the only "
          "one', and the cost of recovery goes up without anything failing.")
def sc_doomed(w: World):
    a = w.begin("agent-A")
    b = w.begin("agent-B")
    w.write(a, "/repo/deploy.yaml")
    w.write(b, "/repo/config.json")
    # A emits something the classifier calls irrevocable -> DOOMED
    w.effect(a, Klass.IRREVOCABLE, "POST /api/v1/deploy production")
    w.effect(b, Klass.REVERSIBLE, "write config.json")
    w.wait(a, b, Wait.OUTPUT, "A needs B's config.json")
    w.wait(b, a, Wait.OUTPUT, "B needs A's deploy.yaml")


@scenario("unresolvable",
          "Every member of the cycle is DOOMED. Abort cannot break it.",
          "THE finding. Irrevocability converts a resolvable deadlock into "
          "an unresolvable one. The kernel must report this rather than "
          "hang -- a system that deadlocks silently is worse than one that "
          "says it has deadlocked. Reachable whenever two agents in a "
          "pipeline both emit an irrevocable effect before either finishes.")
def sc_unresolvable(w: World):
    a = w.begin("agent-A")
    b = w.begin("agent-B")
    w.write(a, "/repo/a.txt")
    w.write(b, "/repo/b.txt")
    w.effect(a, Klass.IRREVOCABLE, "POST /payments/capture")
    w.effect(b, Klass.IRREVOCABLE, "sendmail to 400 customers")
    w.wait(a, b, Wait.OUTPUT, "A needs b.txt")
    w.wait(b, a, Wait.OUTPUT, "B needs a.txt")


# =========================================================================
# Presentation
# =========================================================================
C = {"r": "\033[31m", "g": "\033[32m", "y": "\033[33m", "b": "\033[34m",
     "m": "\033[35m", "c": "\033[36m", "d": "\033[2m", "n": "\033[0m",
     "B": "\033[1m"}


def plain() -> None:
    for k in C:
        C[k] = ""


def render(e: dict) -> str:
    k = e["kind"]
    if k == "begin":
        p = f" (subagent of {e['parent']})" if e.get("parent") else ""
        return f"{C['d']}tx {e['tx']}{C['n']}  BEGIN   {C['B']}{e['agent']}{C['n']}{p}"
    if k == "write":
        return f"{C['d']}tx {e['tx']}{C['n']}  write   {e['path']}"
    if k == "effect":
        col = {"reversible": C["g"], "deferrable": C["c"],
               "compensable": C["y"], "irrevocable": C["r"]}[e["klass_name"]]
        return (f"{C['d']}tx {e['tx']}{C['n']}  effect  {col}{e['klass_name']:<12}{C['n']}"
                f"{e['what']}")
    if k == "doomed":
        return (f"{C['d']}tx {e['tx']}{C['n']}  {C['r']}{C['B']}DOOMED{C['n']}  "
                f"{e['what']}\n         {C['r']}irrevocable effect emitted — "
                f"this transaction can no longer be aborted{C['n']}")
    if k == "wait":
        return (f"{C['d']}tx {e['tx']}{C['n']}  {C['y']}WAIT{C['n']}    "
                f"{e['agent']} → {e['holder_agent']} "
                f"{C['d']}[{e['wait_name']}]{C['n']}\n         {C['d']}{e['why']}{C['n']}")
    if k == "deadlock":
        ring = " → ".join(e["agents"]) + " → " + e["agents"][0]
        return (f"\n{C['r']}{C['B']}  ╔══ DEADLOCK ══{C['n']}\n"
                f"{C['r']}  ║{C['n']} cycle      {ring}\n"
                f"{C['r']}  ║{C['n']} tx ids     {e['cycle']}\n"
                f"{C['r']}  ║{C['n']} states     {', '.join(e['states'])}\n"
                f"{C['r']}  ║{C['n']} edges      {', '.join(e['edge_kinds'])}\n"
                f"{C['r']}  ║{C['n']} doomed     {e['n_doomed']} of {len(e['cycle'])}")
    if k == "victim":
        f = (f"\n{C['r']}  ║{C['n']} {C['y']}forced — only one member was abortable; "
             f"the policy did not choose{C['n']}") if e["forced"] else ""
        return (f"{C['r']}  ║{C['n']} {C['g']}victim     {e['agent']} (tx {e['tx']}){C['n']}"
                f"  policy={e['policy']} cost={e['cost']}{f}\n"
                f"{C['r']}  ╚══{C['n']}")
    if k == "unresolvable":
        moves = "\n".join(f"{C['r']}  ║{C['n']}   · {m}" for m in e["remaining_moves"])
        return (f"{C['r']}  ║{C['n']} {C['r']}{C['B']}UNRESOLVABLE{C['n']}\n"
                f"{C['r']}  ║{C['n']} {e['reason']}\n"
                f"{C['r']}  ║{C['n']} remaining moves, none of them the mechanism's:\n"
                f"{moves}\n{C['r']}  ╚══{C['n']}")
    if k == "stranded":
        who = ", ".join(e["waiter_agents"]) or str(e["waiters"])
        return (f"{C['y']}  ⚠ stranded{C['n']}  aborting {e['agent']} left "
                f"{who} awaiting an approval nobody can give\n"
                f"         {C['d']}{e['policy']}{C['n']}")
    if k == "abort":
        rel = ", ".join(e["released"]) or "nothing"
        return (f"{C['d']}tx {e['tx']}{C['n']}  {C['g']}ABORT{C['n']}   {e['agent']} "
                f"— released {rel}; {e['wal_dropped']} WAL record(s) dropped")
    if k == "commit":
        return f"{C['d']}tx {e['tx']}{C['n']}  {C['g']}COMMIT{C['n']}  {e['agent']}"
    return json.dumps(e)


def run_one(name: str, policy: str, explain: bool, quiet: bool,
            seed: int) -> World:
    meta = SCENARIOS[name]
    out = [] if quiet else None
    w = World(policy=policy, seed=seed,
              emit=None if quiet else (lambda e: print("  " + render(e))))
    if not quiet:
        print(f"\n{C['B']}{'═' * 72}{C['n']}")
        print(f"{C['B']}  scenario: {name}{C['n']}   policy={policy}")
        print(f"  {meta['blurb']}")
        print(f"{C['B']}{'═' * 72}{C['n']}")
    meta["fn"](w)
    if explain and not quiet:
        print(f"\n{C['m']}  what this teaches{C['n']}")
        for line in _wrap(meta["teaches"], 68):
            print(f"  {C['d']}{line}{C['n']}")
    return w


def _wrap(s: str, n: int) -> list[str]:
    words, lines, cur = s.split(), [], ""
    for wd in words:
        if len(cur) + len(wd) + 1 > n:
            lines.append(cur); cur = wd
        else:
            cur = f"{cur} {wd}".strip()
    if cur:
        lines.append(cur)
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", choices=sorted(SCENARIOS))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--policy", default="least-severe",
                    choices=["youngest", "least-work", "least-severe", "random"])
    ap.add_argument("--compare-policies", action="store_true",
                    help="run every scenario under every policy")
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--jsonl", help="write the event stream here")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--no-colour", action="store_true")
    a = ap.parse_args(argv)

    if a.no_colour or not sys.stdout.isatty():
        plain()

    if a.list:
        print("scenarios:")
        for n, m in SCENARIOS.items():
            print(f"  {C['B']}{n:<14}{C['n']}{m['blurb']}")
        return 0

    if a.compare_policies:
        print(f"\n{C['B']}victim policy comparison{C['n']}")
        print(f"{'scenario':<15}{'policy':<15}{'victim':<14}{'cost':>5}  outcome")
        print("-" * 68)
        for n in SCENARIOS:
            for p in ("youngest", "least-work", "least-severe", "random"):
                w = run_one(n, p, False, True, a.seed)
                vic = [e for e in w.events if e["kind"] == "victim"]
                un = [e for e in w.events if e["kind"] == "unresolvable"]
                if un:
                    print(f"{n:<15}{p:<15}{'—':<14}{'—':>5}  "
                          f"{C['r']}UNRESOLVABLE{C['n']}")
                elif vic:
                    v = vic[-1]
                    tag = f"{C['y']}forced{C['n']}" if v["forced"] else "chosen"
                    print(f"{n:<15}{p:<15}{v['agent']:<14}{v['cost']:>5}  {tag}")
                else:
                    print(f"{n:<15}{p:<15}{'—':<14}{'—':>5}  no cycle")
        print(f"\n{C['d']}Read the `doomed` rows: every policy picks the same "
              f"victim, because only one was\nabortable. That is the "
              f"constraint DOOMED imposes, visible as an absence of choice.{C['n']}")
        return 0

    names = sorted(SCENARIOS) if (a.all or not a.scenario) else [a.scenario]
    allev = []
    worlds = {}
    for n in names:
        w = run_one(n, a.policy, a.explain, False, a.seed)
        allev.extend(w.events)
        worlds[n] = w

    print(f"\n{C['B']}{'═' * 72}{C['n']}")
    print(f"{C['B']}  summary{C['n']}   policy={a.policy}")
    for n, w in worlds.items():
        dl = sum(1 for e in w.events if e["kind"] == "deadlock")
        un = sum(1 for e in w.events if e["kind"] == "unresolvable")
        vic = [e for e in w.events if e["kind"] == "victim"]
        forced = sum(1 for e in vic if e["forced"])
        status = (f"{C['r']}unresolvable{C['n']}" if un else
                  f"{C['y']}resolved (forced){C['n']}" if forced else
                  f"{C['g']}resolved{C['n']}" if vic else "no cycle")
        print(f"  {n:<15} cycles={dl}  {status}")
    print(f"\n  {C['y']}These are simulated semantics, not kernel measurements.{C['n']}")
    print(f"  {C['d']}Nothing in src/ implements a wait-for graph yet; "
          f"docs/deadlock.md\n  proposes the fragments. Every event carries "
          f"sim:true.{C['n']}")
    print(f"{C['B']}{'═' * 72}{C['n']}")

    if a.jsonl:
        p = Path(a.jsonl)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            for e in allev:
                fh.write(json.dumps(e, separators=(",", ":")) + "\n")
        print(f"\n  wrote {len(allev)} events -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
