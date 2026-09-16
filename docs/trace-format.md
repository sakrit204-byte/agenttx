# AgentTx trace format

Fragment **P4-02**. Spec first: everyone else's logs conform to this.

One JSON object per line (JSONL), one object per intercepted operation. UTF-8,
`\n` terminated, no trailing comma, no wrapping array. A trace file is append-only
and streamable, so a 4 GB capture can be processed without loading it.

Four producers must emit this identically:

| Producer | Fragment | Notes |
|---|---|---|
| `src/bpf/wal.c` via the ring buffer | P3-07 | the real thing; a `struct tx_wal_rec` rendered to JSON |
| `tools/harness/trace.bt` | P4-02 | bpftrace, for measurement before the hooks exist |
| `tools/harness/synth.py` | P4-04 | synthetic, so training does not wait on P3 |
| `tools/harness/measure_gate.py` | — | `strace` parsing, for the week 3–4 gate |

If they diverge, the model trains on one distribution and runs on another, and
the accuracy number in the paper is measuring the divergence.

---

## Record

```json
{
  "v": 1,
  "seq": 41,
  "ts_ns": 1723472819000041000,
  "tx_id": 1,
  "pid": 8812,
  "tgid": 8812,
  "hook": "socket_sendmsg",
  "syscall": "sendmsg",
  "syscall_nr": 46,
  "path": null,
  "path_hash": "0x8f1bbcdc4b9e3a1d",
  "path_depth": 0,
  "fd_type": "sock",
  "open_flags": [],
  "family": "AF_INET",
  "daddr": "93.184.216.34",
  "dport": 443,
  "msg_flags": ["MSG_NOSIGNAL"],
  "payload_len": 412,
  "payload_prefix": "POST /collect HTTP/1.1\r\nHost: telemetry.example\r\n",
  "awaits_reply": false,
  "ngram": ["openat", "read", "sendmsg"],
  "label": "deferrable",
  "label_source": "rule"
}
```

### Fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `v` | int | yes | format version; this document is `1` |
| `seq` | int | yes | per-transaction, gap-free. **Defines replay order.** A gap means the WAL overflowed and the transaction is doomed — it is not a rounding error |
| `ts_ns` | int | yes | `bpf_ktime_get_ns()`, monotonic, **not** wall clock |
| `tx_id` | int | yes | `0` = outside any transaction |
| `pid`, `tgid` | int | yes | |
| `hook` | enum | yes | one of `enum tx_hook` (see below) |
| `syscall` | string | yes | name, for humans |
| `syscall_nr` | int | yes | x86-64 number, for the model |
| `path` | string\|null | path hooks | absolute, resolved. **May contain secrets** — see redaction |
| `path_hash` | hex string | path hooks | FNV-1a-64 of the path's first three components |
| `path_depth` | int | path hooks | count of `/` |
| `fd_type` | enum | yes | `reg` `dir` `sock` `fifo` `chr` `blk` `link` `none` |
| `open_flags` | [string] | `file_open` | `O_RDONLY` `O_WRONLY` `O_RDWR` `O_CREAT` `O_TRUNC` `O_APPEND` |
| `family` | enum | socket hooks | `AF_INET` `AF_INET6` `AF_UNIX` |
| `daddr` | string\|null | socket hooks | dotted quad or v6 literal |
| `dport` | int | socket hooks | host byte order |
| `msg_flags` | [string] | `socket_sendmsg` | `MSG_DONTWAIT` `MSG_NOSIGNAL` `MSG_MORE` `MSG_OOB` |
| `payload_len` | int | `socket_sendmsg` | bytes the caller asked to send |
| `payload_prefix` | string\|null | `socket_sendmsg` | first ≤256 bytes, post-TLS-uprobe where available |
| **`awaits_reply`** | bool\|null | socket hooks | **the gate measurement.** See below |
| `ngram` | [string] | yes | the previous three syscall names, oldest first |
| `label` | enum\|null | training only | `reversible` `deferrable` `compensable` `irrevocable` |
| `label_source` | enum | if labelled | `rule` `human` `synth` |

### `hook` values

`file_open` `inode_unlink` `inode_rename` `socket_connect` `socket_sendmsg`
`bprm_check` `tls_write` — matching `enum tx_hook` in `include/agenttx.h`.

---

## `awaits_reply` — the one field the project turns on

The gate (PROPOSAL.md, weeks 3–4) is: *what fraction of an agent's outbound
network operations are fire-and-forget rather than request–response?* `>20%`
proceeds, `<10%` demotes contribution 1.

Deferral is only possible when the agent does not block on a reply. So this
field is not a convenience — it is Figure 1.

**Definition.** An outbound operation *awaits a reply* if, on the same file
descriptor, a `recvmsg`/`recvfrom`/`read` by the same thread occurs before that
thread performs its next outbound write on any descriptor, within a 30-second
window.

**Encoding.**

* `false` — fire-and-forget. Deferrable, subject to classification.
* `true` — request–response. **Must not be deferred**; deferring it deadlocks
  the agent. TxOS §3.1.5 makes this the programmer's problem; we have no
  programmer, so it is the classifier's.
* `null` — undetermined (the window closed, the thread exited, the trace was
  truncated). Counted separately and **never** silently folded into `false`;
  that would inflate the headline number in our own favour.

Report all three counts. A gate result stated without its `null` rate is not a
measurement.

---

## Redaction

`path` and `payload_prefix` carry real user data — the motivating example in
PROPOSAL.md is literally an SSH private key being exfiltrated. Traces captured
from real agent runs are **not** committed to the repo.

* Committed corpora carry `path_hash` and drop `path`.
* `payload_prefix` is dropped unless the run used a synthetic workspace.
* `tools/harness/redact.py` performs the strip and is run before any trace
  leaves a laptop.

The model never sees `path`, only `path_hash` — which is also the constraint the
kernel is under, since a BPF hook cannot do string comparison anyway. Training on
a feature the hook cannot compute would produce an accuracy number the deployed
classifier could never reproduce.

---

## Labels

`label` is ground truth, produced two ways (P4-05):

1. **`rule`** — a deterministic labeller over the trace fields. Cheap, covers the
   bulk, and is *also the honest baseline*: if the tree matches the rules and the
   rules were what generated the labels, the model has learned nothing. Report
   accuracy against **human** labels separately, and say so.
2. **`human`** — two people label independently and **inter-annotator agreement
   is reported** (Cohen's κ). The taxonomy is a claimed contribution; a taxonomy
   two people cannot apply consistently is not one.

`synth` marks labels that came from the generator's own intent
(`tools/harness/synth.py`), which are ground truth by construction and are
therefore **excluded from any reported accuracy figure**. They exist to unblock
the pipeline before real traces exist, not to produce results.

## Connection identity (`fd`, `conn`)

Socket records carry two extra fields:

| field | meaning |
|---|---|
| `fd`   | the file descriptor the operation used, as the tracer saw it |
| `conn` | the full endpoint annotation, `TCP:[local:port->remote:port]` |

They exist because **the fire-and-forget fraction is only meaningful per
connection.** TLS fragments one logical HTTP request across many `sendto()`
calls; counting each fragment as an independent outbound effect inflates the
fraction in the project's own favour. `conn` is the unambiguous key --- `fd`
alone is reused after `close()`, and `(daddr, dport)` alone merges every
connection to the same host.

A consumer computing the gate figure MUST group on `conn` and report the
per-syscall figure alongside it, never instead of it. See `docs/journal/p4.md`.
