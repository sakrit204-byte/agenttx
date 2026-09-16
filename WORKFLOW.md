# AgentTx — Parallel Working Protocol

Four people, four kernel components, zero blocking. This document is the rulebook.

---

## 1. The three de-serialization rules

### Rule 1 — Contracts freeze in Week 2

`include/agenttx.h` is written and merged in Week 2, approved by all four.
After that, **the dependency graph between people is empty.** Everyone depends on
the header, nobody depends on anybody's `.c` file.

Changing a frozen contract requires a PR labelled `contract-change` with all four
approvals. Expect two or three of these over the semester; that is normal. Ten is
a sign the design is wrong.

### Rule 2 — Every provider ships a stub on Day 1

Before writing a real implementation, each owner writes the fake one:

```
src/stub/tx_fs_stub.c     (P2 writes)  -> logs and returns 0
src/stub/tx_eff_stub.c    (P3 writes)  -> logs and returns 0
src/stub/tx_classify_stub.c (P4 writes) -> always returns TX_REVERSIBLE
src/stub/tx_core_stub.c   (P1 writes)  -> tx_current_id() returns 1
```

Build with `CONFIG_AGENTTX_STUB=y` and the whole system compiles, loads, and boots
with all four components faked. **Any one person can run the entire system alone
from Week 3.** This is what makes the fragments parallel instead of sequential.

### Rule 3 — A fragment that cannot be tested against stubs is cut wrong

Every row in the tracker has a `test_cmd` and a `parallel_safe` column.
`parallel_safe = no` is not allowed to survive planning. If you write one, you must
either re-cut the fragment or add a stub that removes the dependency.
This is the self-audit that keeps chunks from creeping back in.

---

## 2. Repo layout — files never collide

```
include/agenttx.h          SHARED. contract-change PRs only.
src/core/                  P1 only
src/fs/                    P2 only
src/bpf/                   P3 only
src/policy/                P4 only  (kernel-side inference)
src/stub/                  each owner writes their own stub file
tools/harness/             P4 only
tools/bench/               P4 owns the runner; P1-P3 add their own bench_*.sh
tests/p1/ p2/ p3/ p4/      each owner only
tracker/P1.csv .. P4.csv   each owner edits ONLY their own file
docs/journal/              one markdown file per person, panics + fixes
```

**Nobody edits another person's directory.** You file an issue against the owner.
The only shared files are `include/agenttx.h` and `Makefile`, and both are
change-controlled.

Per-person tracker CSVs are deliberate: if all four edit one sheet you will spend
the semester resolving merge conflicts in a spreadsheet. `make tracker` concatenates
them into `tracker/master.csv` for the weekly review — it is generated, never edited,
and is gitignored.

---

## 3. Git protocol

**Branches:** `p<n>/<fragment-id>-<slug>` — e.g. `p3/P3-06-effect-wal`.
One branch per fragment. Merged and deleted the same week it opens.
A branch older than 7 days means the fragment was too big — split it.

**Commits:** prefix with the fragment ID. `P3-06: add ringbuf WAL for deferred effects`.
This makes the tracker and git history the same document.

**PR rules:**
- Never push to `main`.
- A PR must state: what it does, which fragment, how the reviewer runs the test,
  and what it looks like when it fails.
- Update your CSV row in the same PR. Status changes are part of the diff.

---

## 4. Review rotation — everyone reads every subsystem

Reviewer assignment rotates by phase, so by the end each of you has reviewed
kernel code in all three subsystems you didn't write. This is the mechanism for
building depth across the whole project rather than in your own silo.

| Phase | P1 reviewed by | P2 reviewed by | P3 reviewed by | P4 reviewed by |
|-------|---------------|---------------|---------------|---------------|
| 1 (wk 3-5)   | P2 | P3 | P4 | P1 |
| 2 (wk 6-9)   | P3 | P4 | P1 | P2 |
| 3 (wk 10-12) | P4 | P1 | P2 | P3 |

Two approvals to merge: the assigned reviewer, plus anyone else.

---

## 5. What "inspect their part" means for kernel code

A review that says "looks good" is worthless here. Every PR gets checked against
this list, and the reviewer states the result in the PR:

1. **Does it build clean?** No new warnings. `make W=1`.
2. **Does the module load and unload repeatedly?** `insmod; rmmod` ten times. A
   refcount or allocation leak shows up here and nowhere else.
3. **KASAN clean?** Boot the KASAN kernel, run the fragment's test. Use-after-free
   in kernel code is silent until it isn't.
4. **Every error path frees what it allocated.** Read the `goto` ladder line by line.
   This is where 80% of kernel bugs in student projects live.
5. **Locking:** what lock protects this data, is it held on every access, can it
   sleep here? Run `lockdep`.
6. **User pointers:** every `copy_from_user`/`copy_to_user` return value checked?
   Never dereference a userspace pointer directly.
7. **Does the test actually fail if you break the code?** Reviewer sabotages one
   line and re-runs. A test that passes on broken code is worse than no test.

Reviewers, be adversarial. You are not being nice to your teammate; you are the
reason their code doesn't panic during the viva demo.

---

## 6. Weekly rhythm

- **Mon** — 20 min standup: what fragment, what's blocked, any contract change needed.
- **Wed** — review day. All open PRs reviewed within 24h. No PR waits over a weekend.
- **Fri** — **integration day.** Everything on `main`, `CONFIG_AGENTTX_STUB=n`, all
  four real components loaded together, current milestone demo runs end to end.
  If it doesn't run, that is Monday's top priority for everyone.
- Every panic you hit goes in `docs/journal/p<n>.md`: symptom, cause, fix, 3 lines.
  This becomes the report's implementation-challenges section and stops the other
  three repeating your mistake.

---

## 7. Environment — identical for all four

Same kernel version, same QEMU invocation, same rootfs image, checked into the repo
as `tools/vm/`. "Works on my machine" must be impossible.

- Linux 6.12+ built from source, `CONFIG_BPF_LSM=y`, `CONFIG_DEBUG_INFO_BTF=y`,
  `CONFIG_KASAN=y` in the debug config, boot param `lsm=...,bpf`.
- QEMU/KVM guest with gdb on `:1234`. Never test on your host kernel.
- Snapshot the VM disk image before each session — a corrupted rootfs is then a
  10-second rollback instead of an evening.
