# Concurrency, conflict and deadlock in AgentTx

Status: **implemented in the kernel** as of 2026-09-17 --- `src/core/waitfor.c`,
contract abi 2, `tests/p1/t11_waitfor.sh` (17 assertions, KASAN+lockdep clean).
`tools/harness/deadlock.py` remains as the model: it explores victim policies
across scenarios far faster than booting a VM, and every event it emits still
carries `sim: true`.

---

## 1. Why this document exists

The 51 fragments assume one agent. `tracker/P2.csv` mentions multi-agent
concurrency once, in P2-07's note --- *"Only needed for the multi-agent
stretch. Cut first if behind."* --- and the word *deadlock* appears nowhere
in the design.

That was defensible while an agent was a single process. It is not any more:
agent systems spawn **subagents**, subagents are separate processes with
separate transactions, and the moment two transactions can wait on each
other the system has a liveness problem that the kernel is the only thing
positioned to see.

---

## 2. Where a cycle can actually form

Four candidates. Only two survive scrutiny.

### 2.1 Kernel lock-order inversion — real, but not a contribution

`ctx->lock`, `tx_table_lock`, and P2/P3's internal locks. Ordinary kernel
engineering; `CONFIG_PROVE_LOCKING` in the debug profile is the answer and
`src/core/commit.c` already documents its rule (never hold `ctx->lock`
across a provider call, because providers sleep). Out of scope here.

### 2.2 Write-write conflict between transactions — **not** deadlock

Two agents write the same file. Under *pessimistic* locking this deadlocks.
Under **optimistic concurrency control** it cannot: transactions never wait
for data, they proceed on their private overlay and the conflict is detected
at commit, where one of them aborts.

**Decision: AgentTx uses OCC.** This matches STM, matches TxOS, and it makes
the entire data path deadlock-free by construction. The cost is livelock and
starvation, not deadlock, and the mitigation is a contention manager (§5).

This is why P2-06 (write-set) and P2-07 (read-set) stop being a "stretch":
they are what commit-time conflict detection reads.

### 2.3 Escalation — **the real one**

P3-10 escalates an irrevocable effect to human approval, and it does so by
**blocking in a sleepable LSM hook**. A blocked transaction holds:

* its CoW upper layer,
* its WAL records,
* and, in a multi-agent pipeline, an output another agent is waiting for.

Now put two agents in a pipeline --- the normal shape of a subagent system:

```
  parent  --waits for--> subagent's verified output
  subagent --waits for--> approval of an irrevocable effect
  approver is the parent (a supervisor agent, not a human)
```

That is a cycle, and every edge in it is a legitimate wait. Nothing is
misconfigured. This is the deadlock AgentTx can actually suffer, and it is a
direct consequence of the mechanism's own design.

### 2.4 Supervisor inversion

`tx_commit` is supervisor-only. If the supervisor is itself transacting and
its commit depends on an agent whose commit depends on the supervisor, the
same cycle appears one level up. Same detection, same resolution.

---

## 3. The wait-for graph

Nodes are transactions. An edge `A -> B` means *A cannot progress until B
finishes*. Three edge kinds, and the distinction matters for recovery:

| edge | meaning | breakable by aborting the waiter? |
|---|---|---|
| `DATA` | A's commit conflicts with B's write-set | yes --- OCC aborts A anyway |
| `ESCALATE` | A is blocked on an approval only B can give | no --- A is asleep in a hook |
| `OUTPUT` | A waits on a file/result B has not committed | yes |

A cycle in this graph is a deadlock. Detection is plain DFS with colouring;
the graph is tiny (one node per live transaction, tens at most), so the
detector runs on every new wait edge rather than on a timer.

---

## 4. Recovery, and the result that falls out of it

Recovery is **victim selection followed by abort**. Abort is cheap here in a
way it is not in a database: the CoW upper layer is discarded and the WAL is
dropped, so an aborted transaction costs only the work it had done.

**But `DOOMED` transactions cannot be aborted.**

A transaction that emitted an irrevocable effect is in `TX_STATE_DOOMED`,
and `DOOMED -> ABORTING` is not merely refused --- it is absent from the
transition table in `include/agenttx.h`, so it is unrepresentable. That is
the STM irrevocability rule and it is correct.

Its consequence for deadlock is the finding:

> **A DOOMED transaction is a non-preemptable resource holder. A wait-for
> cycle in which every member is DOOMED cannot be broken by the mechanism
> that breaks every other cycle.**

Three cases, in increasing severity:

1. **No member DOOMED** --- pick a victim by policy, abort, cycle breaks.
   Cheap and invisible.
2. **Some members DOOMED** --- the victim must be chosen from the
   non-DOOMED members. The policy is *constrained*, not defeated. A cycle
   with one abortable member still resolves, but the choice is forced, so
   "abort the cheapest" degrades to "abort the only one".
3. **All members DOOMED** --- unresolvable by abort. The only remaining
   moves are outside the mechanism: break an `ESCALATE` edge by denying the
   approval (which dooms nothing further, since the effect already
   happened), or hand it to a human. The kernel must **report** this rather
   than hang, and reporting it is the honest deliverable.

Case 3 is not hypothetical: it is reachable whenever two agents in a
pipeline both emit an irrevocable effect before either finishes, which is
exactly what a pipeline of agents with real side effects does.

### Victim selection policies

Implemented and compared by the simulator:

| policy | picks | rationale |
|---|---|---|
| `youngest` | highest tx id | least work lost; the classic DBMS default |
| `least-work` | fewest write-set entries + WAL records | minimises wasted effort directly |
| `least-severe` | lowest `worst_class` | prefers losing a transaction that emitted nothing over one that emitted a compensable effect |
| `random` | uniform | the control. A policy that cannot beat random is not a policy |

`least-severe` is the one to argue for in the paper, because it is the only
one that is aware of what the taxonomy is *for*: a transaction whose worst
class is `reversible` costs nothing to abort, and one whose worst class is
`compensable` costs a compensation.

---

## 5. Starvation, which OCC buys in exchange for deadlock

OCC removes deadlock and introduces the possibility that one transaction
aborts forever while others commit. The standard answer is a contention
manager; the standard cheap one is to make a repeatedly-aborted transaction
progressively harder to victimise. The simulator tracks `n_aborts` per
logical agent and the policies can consult it, so the trade is measurable
rather than asserted.

This is a real evaluation axis and it belongs next to the transaction-length
axis PROPOSAL.md already names.

---

## 6. Fragments --- DONE

| id | what | where |
|---|---|---|
| P1-15 | wait-for graph, three edge kinds | `src/core/waitfor.c` |
| P1-16 | cycle detection on edge insert | `path_back()`, iterative DFS |
| P1-17 | victim selection; refuses DOOMED | `choose_victim()` |
| P1-18 | reports the all-DOOMED cycle | `-EDEADLK` to userspace |

### What changed between the model and the kernel

**Detection is edge-specific, not "find any cycle".** The model searched the
whole graph. In the kernel that is wrong in a way that only appears once the
graph has been used: an *unresolvable* cycle stays in the graph by definition
--- nothing can be aborted to break it --- so every later insert rediscovered
it and returned `-EDEADLK` to callers whose own edge was fine. One stuck cycle
poisoned every subsequent wait. `path_back()` answers the question the caller
actually asked: *does MY wait deadlock?*

**The DFS is iterative.** The kernel stack is 16 KiB and the graph's depth is
bounded only by how many transactions a caller chooses to chain, so a
recursive DFS is a stack overflow reachable from userspace.

**Both ends must be live transactions.** An edge naming a transaction that
does not exist can never be satisfied and never be dropped, so the graph
accumulates permanent garbage and a cycle through it is a phantom deadlock
between parties that were never there. `-ESRCH`.

**The abort is deferred to a workqueue.** Victim selection happens under
`tx_edges_lock`; `tx_do_abort()` calls into P2 and P3 and both sleep. Same
shape as the process-death path, for the same reason.

### Measured, in the kernel

```
DEADLOCK: 2 transactions in a cycle [escalate output ]
  tx=1 -> tx=2
  tx=2 -> tx=1
  victim tx=2 (policy=least-severe, 2 of 2 abortable, 0 doomed)
  tx=2 ABORT reason=8 files_dropped=1 effects_dropped=3
```

and, with both transactions doomed by the **in-kernel classifier** calling an
outbound send irrevocable:

```
DEADLOCK IS UNRESOLVABLE: all 2 transactions are DOOMED
  Abort is the preemption primitive and a DOOMED transaction
  cannot be aborted -- the effects are already emitted.
```

`-EDEADLK` reaches userspace, because a caller about to sleep on that wait
must be told that sleeping would hang.

---

## 7. Still proposed

These are **proposals**, not merged rows. The write-set fragments already
exist and only change priority; the rest are new.

| id | stream | title | depends on |
|---|---|---|---|
| P2-06 | fs | Write-set tracking (**promote from stretch**) | P2-03 |
| P2-07 | fs | Read-set tracking (**promote from stretch**) | P2-06 |
| P1-15 | core | Wait-for graph: nodes, three edge kinds, registration | P1-06 |
| P1-16 | core | Cycle detection on edge insert (DFS, coloured) | P1-15 |
| P1-17 | core | Victim selection + abort; refuse to victimise DOOMED | P1-16, P1-08 |
| P1-18 | core | Report the all-DOOMED cycle rather than hanging | P1-17 |
| P2-12 | fs | Commit-time write-set intersection (OCC validation) | P2-06 |
| P4-15 | policy | Deadlock corpus + victim-policy comparison | P1-17 |

### Contract delta this would need

A `contract-change` PR (four approvals, WORKFLOW.md Rule 1) adding:

```c
enum tx_wait_kind { TX_WAIT_DATA = 0, TX_WAIT_ESCALATE = 1, TX_WAIT_OUTPUT = 2 };

struct tx_wait_edge {
        tx_id_t waiter;
        tx_id_t holder;
        __u8    kind;           /* enum tx_wait_kind      */
        __u8    _pad[3];
        __u64   since_ns;
};

#define TX_IOC_WAIT   _IOW (AGENTTX_IOC_MAGIC, 0x07, struct tx_wait_edge)
#define TX_IOC_UNWAIT _IOW (AGENTTX_IOC_MAGIC, 0x08, struct tx_wait_edge)
```

plus `TX_REASON_DEADLOCK` in `enum tx_reason`. **`include/agenttx.h` has not
been touched** --- it is frozen, and proposing the delta here is the process.

---

## 7. What the demo shows

`tools/harness/deadlock.py` runs four scenarios end to end and emits the
trace format, so the dashboard renders the wait-for graph live:

1. `classic` --- two agents, circular wait, resolved by abort
2. `subagents` --- a parent and two subagents; the cycle runs through an
   escalation edge, which is the shape a real agent system produces
3. `doomed` --- one cycle member is DOOMED, so victim choice is forced
4. `unresolvable` --- every member DOOMED; the mechanism reports rather
   than resolves

Run `python3 tools/harness/deadlock.py --list` for the scenarios and
`--scenario X --explain` for a narrated walkthrough suitable for a viva.
