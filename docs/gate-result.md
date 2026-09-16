# The project gate: measured

**Result: ~3% of an agent's outbound network effects are fire-and-forget.**
`PROPOSAL.md`'s gate says **< 10% → Contribution 1's ceiling is this number;
promote contributions 2 and 5 to the headline.**

This document is the measurement, its method, and every reason it might be
wrong.

---

## 1. The number

Five traces of a real agent (Claude Code 2.1.272), four task shapes, captured
with `strace -f -tt -yy -s 64 -e trace=network,desc`.

| task shape | outbound ops | per syscall | **per logical request** | per connection |
|---|---|---|---|---|
| read & summarise | 69 | 59.4% | **1.8%** | 0.0% |
| read & summarise (run01) | 47 | 70.2% | **4.9%** | 5.3% |
| run shell commands | 45 | 46.7% | **2.6%** | 0.0% |
| write a file | 42 | 47.6% | **2.7%** | 5.6% |
| search the repo | 44 | 45.5% | **2.6%** | 0.0% |
| | | mean 53.9% | **mean 2.9%** | mean 2.2% |

Every trace is fully accounted for: **0 unaccounted lines** in all five.

---

## 2. Why there are three numbers and which one is the answer

The same trace yields 53.9%, 2.9% or 2.2% depending on what you call "one
outbound effect". That is not a rounding difference; it is a **24× spread**,
and the obvious unit is the one that flatters the project.

**Per syscall — wrong, and wrong in our favour.** TLS fragments one logical
request across many `sendto()` calls. A request whose body takes ten writes
and then blocks on the reply scores as nine fire-and-forget plus one
request-response. The payload lengths make it visible: the "fire-and-forget"
sends in the first trace were `1478, 1478, 1478, 1504, 1504, 1510 …` —
MTU-sized TLS records, not independent effects.

**Per connection — too harsh.** A persistent HTTP/2 connection carrying a
blocking request *and* a fire-and-forget ping counts entirely as
request-response.

**Per logical request — the answer, with a stated error direction.** A
logical request is a maximal run of writes on one connection uninterrupted by
a read on that connection; it is request-response if a read follows, and
fire-and-forget if the connection ends without one. This is computable
without parsing TLS.

Where it is wrong: HTTP/2 multiplexing means a reply to request A can end
request B's burst, so it **undercounts** fire-and-forget on multiplexed
connections. The error is in the same direction as the per-connection unit,
though far smaller. **The true value is between 2.9% and some figure below
53.9%, and every honest refinement so far has moved it down, not up.**

---

## 3. Two measurement errors found, both inflating the result

**The tool discarded 34–66% of every trace.** `strace` splits a syscall
across `<unfinished ...>` / `<... resumed>` lines when another thread runs in
between; the first version skipped both halves. On the first four captures
that silently dropped **1,571 inbound operations** — replies we never saw,
making their sends look unanswered. Stitching them back (exact, not
heuristic: every unfinished call has its resumption) moved the figure from
**6.3% to 2.9%**.

**The line accounting was arithmetically wrong.** The first version
double-counted stitched syscalls and reported a *negative* remainder. It was
caught immediately because the accounting is printed and must sum to 100%.
Both errors pushed the number **up**. The check exists because a percentage
that does not add up is a place something can hide.

---

## 4. What this means for the project

`PROPOSAL.md`, on the gate:

> **< 10%** → promote contributions 2 and 5 to the headline.

At 2.9%, **Contribution 1 — kernel-level deferral of fire-and-forget
effects — has a ceiling of about three percent of an agent's outbound
traffic.** The mechanism works (`tests/p3/t07`, `t08`: the send arrives if
and only if the transaction commits). It simply has very little to act on,
because an agent's network traffic is almost entirely request-response with
its model provider — and it blocks on every one of those replies.

This does not kill the project. It relocates it:

* **Contribution 5** (the first head-to-head measurement of agent effect
  behaviour) was already described in `PROPOSAL.md` as "the floor and
  publishable alone". It is now the **ceiling** too, and it is the strongest
  thing here.
* **Contribution 2** (verification-delimited transaction boundaries) is
  untouched: it is about *when* to commit, not about deferral.
* **Contribution 1** should be stated with its measured ceiling attached.
  "Kernel deferral is possible and covers ~3% of agent outbound effects" is a
  true and interesting sentence. "Kernel deferral solves the irreversibility
  problem" is not.
* The **unit-dependence itself** is a contribution. A 24× spread between two
  defensible definitions of "one outbound effect" means any future paper
  reporting this fraction without stating its unit is unfalsifiable.

---

## 5. Threats to this result

Listed in descending order of how much they worry us.

1. **One agent.** Claude Code only. A different agent — one that posts
   webhooks, sends notifications, or writes to a telemetry sink — could have
   a very different profile. This is the biggest gap and the cheapest to
   close.
2. **Task shapes are all "work on a local repo".** None of the four tasks had
   any reason to emit a fire-and-forget effect. That is arguably *typical* of
   coding agents, but it is an assumption, not a finding.
3. **HTTP/2 multiplexing undercounts.** §2. Direction known, magnitude not.
4. **The model API dominates.** 42 of 46 operations in the first trace went
   to one host. The measurement is substantially a measurement of one
   client's behaviour toward one API.
5. **Short sessions.** Each is a single prompt. A long agentic session with
   many tool calls may differ.

Points 1 and 2 are what a serious version of this study fixes, and neither
needs any kernel work.
