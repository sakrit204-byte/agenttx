# P4 journal --- the in-kernel classifier

## 2026-09-16 --- P4-10 and P3-11: the model runs, and the first one was meaningless

The verifier accepts the quantised decision-tree walk inline in all five LSM
programs. `txload --model` loads and validates the blob. Live, on real sends:

```
socket_sendmsg  reversible   emitted    -> 127.0.0.1:9999
socket_sendmsg  irrevocable  escalated  -> 8.8.8.8:443
```

Getting there found two failures that would have shipped silently.

---

## 1. Train/serve ENCODING skew: MSG_DONTWAIT would have been inverted

`features.py` encodes `open_flags` and `msg_flags` into **its own** bit
layout, and its comment said:

> The hook has these as bits of `flags` already, so this is a mask, not a parse.

That is false. Measured on this kernel:

| flag | kernel | features.py |
|---|---|---|
| O_CREAT | 0x40 | 0x04 |
| O_TRUNC | 0x200 | 0x08 |
| O_APPEND | 0x400 | 0x10 |
| O_EXCL | 0x80 | 0x20 |
| O_NOFOLLOW | 0x20000 | 0x40 |
| **MSG_DONTWAIT** | **0x40** | **0x08** |
| MSG_MORE | 0x8000 | 0x10 |
| MSG_NOSIGNAL | 0x4000 | 0x20 |
| MSG_CONFIRM | 0x800 | 0x40 |

6 of 8 open flags and 4 of 8 message flags differ. Following that comment,
the hook would have reported **MSG_CONFIRM every time the agent set
MSG_DONTWAIT**, because kernel `MSG_DONTWAIT` (0x40) is `features.py`'s
`MSG_CONFIRM` (0x40).

`MSG_DONTWAIT` is the feature `docs/trace-format.md` calls *"the f-and-f
signal"* --- the one the whole deferral argument leans on. It would have been
systematically inverted, the model would have trained happily, and nothing
would have failed.

Fixed by `src/policy/tx_features.h`, which does the remap explicitly and is
the **one** implementation: the BPF program and `tools/harness/featcheck`
are two compilations of the same header, so they cannot drift.

---

## 2. Train/serve AVAILABILITY skew: 55% of the tree was unreachable

The first tree had 121 nodes. Its top two split features were the syscall
n-gram:

```
ngram_1        16 nodes
ngram_0        15 nodes
path_hash_b0    7
...
syscall_nr      2
```

**33 of 60 internal nodes (55%) split on features an LSM hook cannot
supply:**

* `syscall_nr` --- LSM hooks run below syscall dispatch; the number is not
  in scope.
* `ngram_0/1` --- needs per-task syscall history the hook does not keep. And
  worse, `features.py` hashes syscall **name strings**; even with history, a
  kernel-side hash of hook ids is a different number, so the thresholds would
  be meaningless. Not *missing* --- **unmatchable**.

The consequence is not graceful degradation. The walk went through zeros to a
fixed leaf, and **every real send classified `reversible`** --- the least
severe class, which emits. Swapping the model in for the static rule table
made the system *worse* on live traffic, while looking like it worked.

### The fix, and what it cost

`features.py --kernel-only` zeroes those three so the tree cannot split on
them. Retrained:

| | 121-node tree | 23-node tree |
|---|---|---|
| nodes / depth | 121 / 14 | **23 / 6** |
| blind decision nodes | 33 of 60 (55%) | **0 of 11** |
| balanced accuracy | 0.574 | 0.495 |
| **irrevocable recall** | 0.978 | **1.000** |

Balanced accuracy fell. **Irrevocable recall rose to 1.000** --- the only
metric with a safety meaning. The features the kernel cannot see were
carrying accuracy the kernel could never have realised, and removing them
made the model smaller, faithful, and safer.

It is also conservative: precision on `irrevocable` is 0.286, so it escalates
far more than it must. That is the correct direction for a fail-closed
classifier and a real cost to measure later.

---

## 3. A test that could not catch its own bug

`tests/p4/t07_infer.sh` first asserted on `featcheck msg <flags>`, which calls
`tx_msg_flags_feat()` directly. Sabotaging the **call site** --- replacing
`tx_msg_flags_feat(...)` inside `tx_features_extract()` with a raw
`& 0xff` --- **passed cleanly.** The helper was right; the vector was wrong;
the test only looked at the helper.

Now it compares all 16 bytes against `features.py` for four representative
events. Re-sabotaged: caught, `8` vs `64` on the last byte --- exactly the
`MSG_DONTWAIT` → `MSG_CONFIRM` inversion.

Three sabotages, three catches:

| sabotage | caught by |
|---|---|
| mask raw kernel flags in the vector | whole-vector comparison |
| swap two bits in the remap | flag encoding parity |
| load the 55%-blind model | supplyable-features audit |

---

## 4. A naming trap worth one line

The header was first called `src/policy/features.h`. **`<features.h>` is a
glibc header**, pulled in by `libc-header-start.h` from essentially every
libc include. With `src/policy` on the include path it shadowed glibc's, and
`<stdio.h>` began parsing our C as feature-test macros. The error blamed
`sys/ioctl.h`. Renamed to `tx_features.h`.

---

## What this means for the claim

Contribution 3 is "in-kernel classification of effect reversibility". The
mechanism is real: the verifier accepts it, it runs in the hook, it
classifies, and `P3-11` was a one-block diff because both classifiers go
through the same funnel.

**The decisions are not yet evidence.** The model is trained on synthetic
labels whose generating function it has recovered, and every record carries
`label_source="synth"`. What P4-10 establishes is that the kernel *can* run
the classifier and that the feature pipeline is faithful end to end --- not
that the classifier is right. P4-05 (real labels) is what makes it a result.
