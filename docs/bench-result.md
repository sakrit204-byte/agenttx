# What AgentTx costs

All numbers from the **perf** kernel (KASAN off, lockdep off, `mitigations=off`,
CPUs 2–3 isolated with `nohz_full`), measured on an isolated core inside a
QEMU/KVM guest. That last clause is a real caveat and stating it costs nothing.

`lib_bench.sh` refuses to run on a debug kernel, and `plot.py` refuses to draw
a figure from any row that was: *"a benchmark run under KASAN measures KASAN"*
(`docs/SETUP.md`).

---

## 1. What the hooks cost a process that is NOT transacting

**The deployability number.** The LSM hooks fire for every process on the
machine, and almost none of them are transacting. If this is not small the
mechanism is not deployable however well the rest works.

Median of **7 interleaved rounds**, p50 nanoseconds:

| syscall | hooks off | hooks on | overhead |
|---|---|---|---|
| `getpid` — **no LSM hook** | 248 | 176 | **+0** |
| `openat` — `lsm/file_open` | 449 | 573 | **+132** |
| `sendto` — `lsm/socket_sendmsg` | 1536 | 1537 | **+116** |

**The first row is the error bar.** `getpid` has no hook, so its overhead must
be zero, and the median across rounds *is* zero. That is what makes the other
two rows measurements rather than drift.

Roughly **+120 ns per hooked syscall** — about 29% on an `openat`, 7.5% on a
`sendto`. The cost is one `bpf_tx_current_id()` call: an RCU bucket walk that
returns 0 and a branch.

### Two methodology failures on the way to that table

**The hooks used to require a running process.** A `bpf_link` is refcounted by
the fd that holds it, so `txload` had to stay alive — and then the `hooks_on`
arm also measured `txload` draining a ring buffer. It showed as **+108 ns on
`getpid`**, a syscall with no hook. Fixed by `txload --pin`, which pins the
links into `TX_PIN_DIR` and exits; both arms now have nothing running.

**Measuring one arm fully and then the other let drift look like signal.**
With pinning done, `getpid` came out **103 ns *faster*** with hooks attached —
impossible, and the same order as the effect being measured. Fixed by
alternating the arms and taking the median of per-round deltas: drift hits both
arms of a round and cancels. The noise floor went from ±103 ns to 0.

---

## 2. Transaction lifecycle

| operation | mean | p50 | p99 |
|---|---|---|---|
| `getpid` (baseline syscall) | 186 | 169 | — |
| `TX_IOC_STAT` | 231 | 225 | 350 |
| `TX_IOC_BEGIN` | 69 434 | 66 261 | — |
| begin + abort | 217 843 | 73 964 | — |

`tx_stat` is **+45 ns** over the cheapest real syscall: the ioctl round trip
plus an RCU lookup, and essentially free.

`tx_begin` is **66 µs — 300× more**, and that is not bookkeeping. It is
`tx_fs_begin()` doing five `mkdir()`s to build the copy-on-write area. For a
verification-delimited transaction lasting seconds to minutes this is
irrelevant, and it would matter enormously for per-tool-call transactions —
which is one concrete argument for the transaction length `PROPOSAL.md`
chooses.

The begin+abort **mean is 3× its median** (218 µs vs 74 µs). That
distribution is bimodal and the mean should not be quoted alone.

---

## 3. Copy-on-write

| | mean | p50 | p99 |
|---|---|---|---|
| write 4 KiB, no overlay | 1516 | 1279 | 2562 |
| write 4 KiB, through the overlay | 1519 | 1313 | **9155** |

**Nearly free at the median, 3.6× worse at the tail.** The medians differ by
34 ns; p99 differs by 6.6 µs. The tail *is* the copy-up — a write that has to
duplicate a file before modifying it — so an aggregate that hides p99 hides the
entire cost of CoW.

### Storage amplification

**4096 bytes copied for a 1-byte edit of a 4096-byte file — 4096×.**

That is overlayfs working correctly, and it is the honest cost of the
substrate: there is no partial copy-up. A transaction that touches one byte of
a 100 MB file writes 100 MB.

An earlier version of this measurement reported **16×** because it took the
`du` delta *after* commit — and commit drains the upper layer
(`src/fs/commit.c`), so it was measuring the cleanup. It is now measured while
the transaction is held open.

---

## 4. What is not measured yet

* **Throughput**, only latency. A syscalls/sec figure under load would say
  more about deployability than a microbenchmark.
* **Commit and abort cost** as a function of write-set size. Both are O(files)
  and neither is measured.
* **Deferral overhead** — the WAL write and the egress lookup on a deferred
  send.
* **Anything on real hardware.** Every number here is from inside a KVM guest.
