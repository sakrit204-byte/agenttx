# AgentTx

**AgentTx makes the kernel — not the network interface — the point at which an
AI agent's actions become real.**

---

## The problem

A prompt injection is planted in a dependency's documentation. An agent reads it
during an ordinary task and — with the user's full authority — reads the user's
SSH private key and POSTs it to an external host. The POST is fire-and-forget:
the agent doesn't need the response to continue.

Under every deployed protection this either succeeds (the allowlist happened to
permit the host; the approval prompt showed a shell command whose syscall trace
nobody could predict) or it's blocked by a static rule someone wrote in advance
having anticipated this exact path. Neither is a guarantee.

Filesystem damage, meanwhile, is solved — Claude Code checkpoints and rewinds,
Codex sandboxes with bubblewrap, Landlock and seccomp. We concede all of it. What
remains is the outbound effect, and the field openly concedes tier 3: *actions no
mechanism can take back once a third party has seen them.*

## The observation

That claim is true, and it's stated at the wrong boundary.

```
agent calls sendmsg()
        │
   1. EMISSION       packet leaves the machine
        │            ← the real irreversibility boundary is here
   2. OBSERVATION    third party reads and acts
```

Sagas, compensating transactions and idempotency keys all begin *downstream* of
emission, so they're best-effort by construction. The interval between the
syscall and emission is wholly inside the kernel, and nobody occupies it.

This differs from idempotency keys in kind, not degree: a key requires the
*receiver* to implement it; deferral requires nothing of the receiver.

## What we build

`tx_begin` · `tx_commit` · `tx_abort`. Inside a transaction, writes divert into a
copy-on-write overlay in the agent's private mount namespace, and outbound
effects are captured in a kernel write-ahead log rather than emitted. eBPF LSM
hooks intercept; an `int8`-quantised classifier runs *inside the hook* — integer
arithmetic only, the verifier forbids floats — sorting effects into four classes:

| Class | Handling |
|---|---|
| Reversible | proceed; the CoW layer captures it |
| Deferrable | log it, report success, do not emit |
| Compensable | emit, but record the cancellation window and a commit deadline |
| Irrevocable | transaction becomes non-abortable and terminates; escalate to human approval |

The classifier is **fail-closed**: low confidence escalates rather than permits.

## The policy half — this is what makes the mechanism mean anything

**What is a transaction?** Too short (per tool call) and the guarantee window is
milliseconds. Too long (per session) and you hold an unbounded log across
unbounded agent decisions on an arbitrarily stale snapshot — a regime the STM
irrevocability literature never assumed.

Our answer: **verification-delimited**. The reason to speculate is that safety
can't be decided in advance; the moment evidence arrives — build succeeds, tests
pass, diff reviewed — decide. A transaction spans exactly the interval over which
no evidence exists. Length is workload-determined, not configured, and a session
is a *sequence* of transactions. An irrevocable effect terminates the enclosing
transaction, exactly as in STM.

We don't assert this is optimal — transaction length is an **evaluation axis**.

**Who may commit?** Not the agent. `tx_commit` from inside the transaction
returns `-EPERM`; only the supervisor commits, driven by the verification signal,
a policy rule, or a human. That one invariant kills the premature-commit attack.

**What happens to the agent's reasoning?** Conversation state goes *inside* the
transaction. Abort rolls back files, effects and reasoning together, so no
inference built on a deferred-then-aborted send survives.

**Threat model.** Adversary is a prompt injection with full control of agent
output, no kernel exploit, no control of the supervisor. Covered: premature
commit, namespace escape, classifier evasion, WAL exhaustion, TOCTTOU between
classification and emission, TLS uprobe bypass. Out of scope: kernel exploits,
compromised supervisor, timing side channels.

## What we claim

1. **Kernel-level deferral of fire-and-forget external effects** — a hard
   guarantee where the field offers best-effort compensation.
2. **Verification-delimited transaction boundaries**, with the guarantee-window
   vs. staleness trade-off measured rather than asserted.
3. **In-kernel classification of effect reversibility.**
4. **The four-class taxonomy** plus the first measured distribution of real agent
   traces across it.
5. **The first head-to-head evaluation** of agent rollback mechanisms.

## What we do not claim

The transaction abstraction (TxOS, 2009). Output buffering (Xsyncfs, 2006).
Fixed-point eBPF inference (established). Filesystem rollback for agents (ships
in production). Reversing observed effects (impossible). **Automatic inference of
compensation windows** — the kernel has no application semantics with which to
discover them, so `compensable` is a declarative registry shipped like a CA
bundle, and automatic inference is named as an open problem, not a contribution.

## What the mechanism does *not* reach

In 2026 an agent wiped a production database and its backups in nine seconds. Our
**classifier** wouldn't have caught it — each operation was individually routine
and per-call classification is a local decision. Our **deferral** wouldn't have
held it either — a remote DB mutation is request–response, so the agent blocks on
the reply. What helps there is the **transaction**: staged under a
verification-delimited boundary gated on "tests still pass," the sequence never
commits.

We state this explicitly because a motivating example the mechanism doesn't
address is worse than no example.

## The gate

**Weeks 3–4.** Measure what fraction of an agent's outbound network operations
are fire-and-forget rather than request–response.

- **> 20%** → proceed; that measurement is Figure 1
- **10–20%** → proceed with a narrowed claim
- **< 10%** → promote contributions 2 and 5 to the headline

Four days in month one that de-risks the year.

## Team and scope

| | Component | Kernel depth gained |
|---|---|---|
| P1 | Transaction core | syscall table, `task_struct`, locking |
| P2 | CoW storage | VFS, overlayfs, stackable filesystems |
| P3 | Effect interception | BPF LSM, networking, ring buffers |
| P4 | Policy and evaluation | eBPF verifier, quantisation, measurement |

Four rungs, each independently demonstrable: module + ioctl + overlay-backed
abort → LSM interception with static rules → in-kernel classifier → real syscalls
via kernel patch. If a rung stalls, the previous rung is still a complete system.

## Honest expectations

The engineering outcome is near-certain. The paper is the uncertain part —
realistically a workshop (HotOS, APSys, eBPF@SIGCOMM) or arXiv, not SOSP.
Contribution 5 is the floor and is publishable alone.

---

*See `READING-LIST.md` for the literature, `WORKFLOW.md` for the working
protocol, `tracker/*.csv` for the 51 fragments, and `paper/` for the full
proposal.*
