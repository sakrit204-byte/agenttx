# AgentTx desktop

    agenttx                 # or: python3 tools/app/agenttx_desktop.py

A native Qt app. Not a web page in a window: it talks to the sandbox directly
through `tools/ui/guest.py`, with no server between them.

## What it is for

Every agent tool asks permission before each action, because once an action
happens it cannot be taken back. That is a workaround for a missing
mechanism, and it trains people to click Allow.

AgentTx has the mechanism. The agent runs with **no permission prompts at
all**, because everything it writes goes into a copy-on-write layer that has
never touched the real folder. You review what it actually did — afterwards,
completely, with the diff in front of you — and decide once.

One decision at the end instead of twenty during. That is the transaction,
expressed as a UI.

## Using it

1. **Folder** — the directory the agent may change (a path in the guest).
2. **Task** — what the agent should do.
   Prefix with `$` to run a plain shell command instead, which uses no model.
3. **Run agent.**
4. Read the report: what was created, edited, deleted, with diffs and a
   plain-language summary.
5. **Keep the changes** or **Discard everything.**

`Under the hood` swaps the whole view for the kernel state, the intercepted
effects, and the measurements. The normal view hides all of it on purpose.

## Cost

The agent runs inside the guest VM and uses whatever account that VM's
`claude` is signed into.

* **Sign in with `claude setup-token`** (OAuth). That uses the Claude Code
  subscription and is covered by it.
* **Do NOT set `ANTHROPIC_API_KEY`** in the guest. It switches Claude Code to
  metered API billing, which is a per-token charge.

To sign in:

```bash
sshpass -p agenttx ssh -p 2222 root@127.0.0.1
su - agent
claude setup-token          # follow the prompts
```

Anything prefixed with `$` never contacts a model and costs nothing at all.
The sandbox is fully useful that way.

## Requirements

* the guest VM running (`SERIAL_LOG=/tmp/c.log make vm-boot`)
* `agenttx.ko` loaded in it, built with `STUB_FS=0`
* `python3-pyqt6` on the host

## Two things that will bite you

**Binaries must not run from the 9p share.** `txctl` and `txload` are copied
to `/usr/local/bin` in the guest before every session. Executing them from
`/mnt/agenttx` faults inside `ld-linux` before `main()` — same md5 on both
sides, so it is the execution and not the file, and it fails *intermittently*,
which is worse.

**The agent is unprivileged.** It runs as the `agent` user; only the
supervisor is root. Claude Code refuses `--dangerously-skip-permissions` as
root, which is a sound check and the thing that surfaced this. The CoW area is
handed to that user at mount time, because overlayfs copies up with the
caller's credentials.
