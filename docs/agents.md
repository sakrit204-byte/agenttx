# The agent harness

    You describe a task.  One or more agents do it.  Every change each of
    them makes lands in its own kernel transaction, and nothing is real
    until you say so.

This document is about the half above the kernel: where the loop lives,
what the model is, and why N agents is the configuration that makes the
rest of this project necessary.

## The shape of it

```
HOST                                  GUEST (the kernel under test)
┌──────────────────┐                  ┌────────────────────────────────┐
│ Ollama + 7B      │◄── HTTP ─────────│  agent 1 ─┐                    │
│ qwen2.5-coder    │    10.0.2.2      │  agent 2 ─┼── one transaction  │
│ (the brain)      │      :11434      │  agent N ─┘   each             │
└──────────────────┘                  │        │                       │
┌──────────────────┐                  │   /dev/agenttx                 │
│ Desktop app      │◄── ssh ──────────│   overlayfs CoW layers         │
│ chat + swarm +   │                  │   write sets, wait-for graph,  │
│ live kernel view │                  │   deadlock detection           │
└──────────────────┘                  └────────────────────────────────┘
```

The model is on the host and the agents are in the guest. That split is
deliberate: the guest has 6G and four vCPUs and is already running the
kernel being measured, and an OOM inside the sandbox looks exactly like a
transaction bug. Keeping the model out means a slow or memory-hungry model
can never be mistaken for a kernel problem.

Nothing bills. It is a local model on local hardware, and
`tools/agent/brain.py` strips `ANTHROPIC_API_KEY` and
`ANTHROPIC_AUTH_TOKEN` from the environment of everything it starts.

## Who owns the loop

`tools/agent/loop.py`. Think, act, observe, repeat — written here because a
raw 7B has no harness of its own.

The old path (`tools/harness/tx-agent.py`, driving Claude Code) still
exists and is worth keeping. It is the reference arm: when the local model
does something strange, the first question is always *is the harness wrong
or is the model small*, and running the same task through a strong model
answers it in one step.

Both emit the same event shapes, so the desktop app renders either without
knowing which one ran.

### What a small model does, and what it costs

| It does this | Which costs | So the loop |
|---|---|---|
| never stops | an open transaction forever | hard step + wall-clock budgets, each ending the turn cleanly |
| repeats a failing call | the whole turn, silently | detects a repeated (tool, arguments) pair and says so **in the observation** |
| forgets the goal | a restart halfway through | trims the transcript from the **middle**, never the front |
| answers in prose | nothing, if you let it | treats that as the end of the turn |

The repeat notice goes in the observation rather than the system prompt on
purpose. A prompt-level warning about repetition is read once and
forgotten; the observation lands at the exact moment the model repeats
itself, and it is the one nudge that reliably breaks the loop.

Trimming from the middle matters more than it sounds. The obvious ring
buffer drops the front — which is the system prompt and the first look at
the folder — and a small model then rediscovers the layout every few
steps, or restarts the task outright.

## Why several agents is the interesting case

One agent never stressed anything this project built. Its write set
intersects nobody's, the wait-for graph has one node, and the deadlock
detector never sees a cycle outside a unit test.

Three agents on one folder make all of it load-bearing. Each gets its own
transaction, so each has a private copy-on-write layer and none of them can
see the others. Two agents told to edit the same file therefore produce a
textbook lost update: whoever commits second silently destroys the first
one's work.

So the swarm is not a feature bolted onto the sandbox. It is the first
workload that makes the sandbox's hard parts necessary.

Because each agent holds its own transaction, **keep and discard are
per-agent answers**. When two collide you keep the one you wanted and drop
the other, without re-running anything.

### What is and is not built

| | Where | Status |
|---|---|---|
| Per-agent transactions | kernel | working |
| Conflict **detection** | userspace, `swarm.py` | working |
| Conflict **prevention** at commit | kernel (P2-12, OCC) | proposed, not built |
| Wait-for graph + deadlock | kernel, `waitfor.c` | working |
| Live view of both | debugfs | working |

The distinction in rows 2 and 3 is the one to be careful about. Detection
compares upper layers after the agents stop: it can tell a human that two
transactions overlap, and it cannot refuse a commit. Commit-time write-set
intersection in the kernel is a different and stronger claim, and it is not
written yet.

## Watching it happen

    /sys/kernel/debug/agenttx/transactions   one line per live transaction
    /sys/kernel/debug/agenttx/waitfor        one line per live wait-for edge

Read-only, root-only, optional — if debugfs is not mounted the module works
exactly as before.

These hold **current state**, which is the whole point. The first version
of the live panel counted matching lines in dmesg, and dmesg is history: an
edge added and then released still counts, so the number only ever goes up.
A panel claiming three wait-for edges when there are none is worse than one
that says nothing.

## Running it

Pick "3 agents at once" in the app and give it a task. Or:

    python3 tools/agent/swarm.py THREAD_DIR TURN --lower DIR --agents 3

A leading `$` in the app runs a plain shell command instead — no model, no
cost, same transaction and same transcript. That is also the control arm:
when an agent does something surprising, running the same command by hand
through the same machinery answers "agent or sandbox?" in one step.

## Tests

| | |
|---|---|
| `tests/p4/t10_agent_tools.sh` | the tools, and the mistakes a 7B actually makes |
| `tests/p4/t11_loop.sh` | the loop, against a model scripted to misbehave |
| `tests/p4/t12_swarm.sh` | N agents, N transactions, and the collision |

All three use scripted fixtures rather than a real model. That is not
laziness: the behaviour worth asserting is what happens when the *model*
goes wrong, and a real 7B cannot be made to repeat itself or collide on
cue. They need no GPU, no login, and spend nothing.
