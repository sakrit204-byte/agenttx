# The four-class effect taxonomy

Fragment P4-05. This document is the **labelling contract**: it must be
precise enough that two people labelling the same trace independently agree,
and `tests/p4/t04_labels.sh` reports the agreement rather than assuming it.

The four classes are frozen in `include/agenttx.h` and their order is
load-bearing --- severity ascends, so "worst class seen so far" is a `max()`.

---

## 1. The question the taxonomy answers

For one intercepted effect, at the moment it is intercepted:

> **If this transaction aborts, can the system be returned to a state
> indistinguishable — to every party that could have observed it — from one
> in which this effect never occurred?**

Three things in that sentence do work:

* **at the moment it is intercepted** — not later. Classification is a local
  decision with the information the hook has. An effect that a human could
  undo next week is not thereby reversible.
* **indistinguishable to every party** — including parties outside this
  machine. A file restored from a copy-on-write upper layer is
  indistinguishable. An email recalled after delivery is not: the recipient
  saw it.
* **could have observed it** — possibility, not fact. A packet that reached a
  host which happened to discard it is still irrevocable, because whether it
  was read is not knowable from here.

---

## 2. The decision procedure

Apply in order. **The first rule that matches wins.** Do not skip ahead.

```
  Q1. Did the effect leave this machine?
      NO  -> Q2
      YES -> Q4

  Q2. Is the state it changed captured by the transaction's CoW layer?
      YES -> REVERSIBLE
      NO  -> Q3

  Q3. Is the changed state reconstructible from data the transaction still
      holds, without cooperation from anything outside this machine?
      YES -> REVERSIBLE      (and record how, in the notes column)
      NO  -> IRREVOCABLE

  Q4. Was the effect emitted, or is it still held?
      HELD (the mechanism suppressed it) -> DEFERRABLE
      EMITTED                             -> Q5

  Q5. Does a *declared* compensating action exist for this destination in the
      compensable registry, with a cancellation window that has not expired?
      YES -> COMPENSABLE
      NO  -> IRREVOCABLE
```

### Why Q5 asks about a registry and not about the world

PROPOSAL.md is explicit that automatic inference of compensation windows is
**not** claimed: the kernel has no application semantics with which to
discover them. `compensable` therefore means *"someone declared a
compensation for this destination"*, shipped like a CA bundle --- **not**
*"a compensation plausibly exists"*.

A labeller who reasons "well, Stripe has a refund API, so a payment is
compensable" has broken the taxonomy. The question is whether the
compensation is **declared to this system**, because that is the only thing
the kernel can act on. If it is not in the registry, it is `irrevocable`.

This is the single most common labelling disagreement and it is resolved by
rule, not by judgement.

---

## 3. The classes, with their exact meaning

| class | the effect | on abort |
|---|---|---|
| `reversible` | stayed inside the machine and the CoW layer holds it | discarded; nothing observed it |
| `deferrable` | would leave, but was held in the WAL and never emitted | discarded; it never existed |
| `compensable` | left, and a declared compensation can still be invoked | the compensation is invoked; observers saw it and then saw it undone |
| `irrevocable` | left, and nothing can retract it | **abort is unavailable**; the transaction is DOOMED |

`irrevocable` is not "bad". It is "the mechanism has nothing left to offer".
Most of what an agent does that matters is irrevocable, and saying so is the
point.

---

## 4. Worked examples, including the ones that trip people

| effect | class | the rule that decided it |
|---|---|---|
| `write()` to a file in the transaction's workspace | reversible | Q2 — CoW holds it |
| `unlink()` of a file in the workspace | reversible | Q2 — overlayfs whiteout; `tests/p2/t02` proves the file comes back |
| `write()` to `/etc/passwd` outside the overlay | irrevocable | Q2 no, Q3 no — nothing captured it |
| UDP send to a log collector, suppressed | deferrable | Q4 — held |
| UDP send to a log collector, emitted | irrevocable | Q5 — no declared compensation for a syslog sink |
| HTTPS POST the agent blocks on | irrevocable | Q4 emitted, Q5 no. **Not deferrable** — see §5 |
| POST to a destination in the registry, window open | compensable | Q5 yes |
| the same POST after the window expired | irrevocable | Q5 — "has not expired" fails |
| `execve()` of a compiler | reversible | Q1 no, Q2 — its outputs are in the CoW layer |
| `execve()` of `ssh` that then connects out | reversible **for the exec**; the connection is classified separately | Q1 — one effect at a time |
| `connect()` with no data sent | reversible | Q1 no — a handshake reveals only that someone connected, which the transaction cannot un-know but also did not cause as an *effect*. **Contested; see §6.** |
| read of `~/.ssh/id_ed25519` | reversible | Q1 no, Q2 — a read changes nothing. **The read is not the effect; the send that follows is.** |

The last row is the one people get wrong most often, and getting it wrong
inflates the irrevocable count with events that are not effects at all.

---

## 5. `deferrable` is a property of the MECHANISM, not of the effect

An effect is `deferrable` **only if this system actually held it**. It is not
a judgement that the effect "could in principle be delayed".

This matters because it is measurable rather than arguable, and because the
mechanism's reach is narrower than intuition suggests. `src/bpf/agenttx.bpf.c`
does not defer TCP: a suppressed TCP send is retransmitted by the stack and
eventually errors the connection, so the transparency that makes deferral
work is a property of *datagram* semantics (`docs/journal/p3.md`).

So an HTTPS POST is `irrevocable` even though one can imagine deferring it.
Labelling it `deferrable` would be labelling an aspiration.

---

## 6. Known contested cases

Recorded because a taxonomy whose disagreements are undocumented is a
taxonomy that has not been used.

1. **`connect()` alone.** A completed TCP handshake tells the peer something
   happened. We class it `reversible` because no *agent-chosen content*
   crossed the boundary, and because classifying every connect as irrevocable
   would doom essentially every transaction. A labeller who disagrees should
   say so in the notes rather than relabel; §7 is how it gets resolved.
2. **Loopback.** Classed `reversible` — it does not leave the machine. But a
   local service may itself emit externally, in which case the effect is real
   and we have mis-scoped it. Correct for a single-machine threat model;
   wrong the moment the agent talks to a local proxy.
3. **DNS.** A resolver query leaks the name being looked up. We class it
   `reversible` for the same reason as `connect()`. This is the weakest of
   the three.

Each is a place the taxonomy makes a *choice*. The paper should state them.

---

## 7. Labelling protocol

1. **Two labellers, independently.** No discussion before both are done.
2. Each labels every record using §2 only. Where §2 does not decide, label
   `irrevocable` (fail closed) and write why in the notes.
3. `tools/harness/label.py --agreement` reports raw agreement and **Cohen's
   kappa**.
4. Disagreements are resolved by amending §2 or §6 — never by one labeller
   deferring to the other. A disagreement is evidence the *document* is
   ambiguous.
5. The agreement figure is reported in the paper **before** any model is
   trained on the labels. A classifier cannot be more reliable than its
   ground truth, and a kappa below ~0.6 means the labels are not ground truth.

### What agreement would mean

| kappa | reading |
|---|---|
| < 0.4 | the taxonomy is not operational; do not train on it |
| 0.4–0.6 | usable with the disagreements reported per class |
| 0.6–0.8 | substantial; the normal target |
| > 0.8 | suspiciously high — check the labellers were independent |
