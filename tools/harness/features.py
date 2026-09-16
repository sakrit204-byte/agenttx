#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/harness/features.py --- trace -> feature vector.  Fragment P4-06.

THE BINDING CONSTRAINT: every feature here must be computable inside a BPF
hook, with integer arithmetic, no string operations and no unbounded loops.

That is not a style preference.  A feature the hook cannot compute produces
a model that scores well in this file and cannot be deployed, and the
failure appears at P3-11 -- week 11 -- when the integration is supposed to
be a small diff.  If you want to add a feature, first write the four lines
of BPF that would compute it.  If you cannot, it does not go in.

The output layout is `struct tx_features` from include/agenttx.h, verbatim:
16 unsigned bytes, in the order of `enum tx_feature_idx`.  Three files share
that layout and all three change in the same contract-change PR:

    tools/harness/features.py    <- this file
    src/policy/infer.bpf.c       the forward pass
    src/bpf/rules.c              the static-rule baseline

Values are u8 by construction, so quantisation is the identity on the input
side and the BPF forward pass never has to scale an input.  Clamping is this
file's job.

Usage:
    python3 features.py --in data/traces/synth.jsonl --out data/traces/features.npz
    python3 features.py --in traces.jsonl --out f.npz --exclude-synth
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# --- enum tx_feature_idx, include/agenttx.h -------------------------------
F_SYSCALL_NR   = 0
F_HOOK_ID      = 1
F_PATH_HASH_B0 = 2
F_PATH_HASH_B1 = 3
F_PATH_DEPTH   = 4
F_PATH_IS_DOT  = 5
F_FD_TYPE      = 6
F_OPEN_FLAGS   = 7
F_DPORT_LO     = 8
F_DPORT_HI     = 9
F_AF           = 10
F_IS_LOOPBACK  = 11
F_TX_DEPTH     = 12
F_NGRAM_0      = 13
F_NGRAM_1      = 14
F_MSG_FLAGS    = 15
N_FEATURES     = 16

FEATURE_NAMES = [
    "syscall_nr", "hook_id", "path_hash_b0", "path_hash_b1",
    "path_depth", "path_is_dot", "fd_type", "open_flags",
    "dport_lo", "dport_hi", "af", "is_loopback",
    "tx_depth", "ngram_0", "ngram_1", "msg_flags",
]
assert len(FEATURE_NAMES) == N_FEATURES

# --- enum tx_hook ---------------------------------------------------------
HOOK_ID = {
    "none": 0, "file_open": 1, "inode_unlink": 2, "inode_rename": 3,
    "socket_connect": 4, "socket_sendmsg": 5, "bprm_check": 6, "tls_write": 7,
}

# --- enum tx_class --------------------------------------------------------
CLASS_ID = {"reversible": 0, "deferrable": 1, "compensable": 2, "irrevocable": 3}
CLASS_NAMES = ["reversible", "deferrable", "compensable", "irrevocable"]

# S_IFMT >> 12, as the hook reads it from the inode.
FD_TYPE = {"none": 0, "fifo": 1, "chr": 2, "dir": 4, "blk": 6,
           "reg": 8, "link": 10, "sock": 12}

AF = {None: 0, "AF_UNIX": 1, "AF_INET": 2, "AF_INET6": 10}

# Packed into one byte.  The hook has these as bits of `flags` already, so
# this is a mask, not a parse.
OPEN_BIT = {"O_RDONLY": 0x00, "O_WRONLY": 0x01, "O_RDWR": 0x02,
            "O_CREAT": 0x04, "O_TRUNC": 0x08, "O_APPEND": 0x10,
            "O_EXCL": 0x20, "O_NOFOLLOW": 0x40}

# MSG_DONTWAIT is the cheap fire-and-forget signal.  Present because it is
# real and computable; weighted-on with caution because an injection can
# simply not set it.  P4-13 tests exactly that evasion.
MSG_BIT = {"MSG_OOB": 0x01, "MSG_PEEK": 0x02, "MSG_DONTROUTE": 0x04,
           "MSG_DONTWAIT": 0x08, "MSG_MORE": 0x10, "MSG_NOSIGNAL": 0x20,
           "MSG_CONFIRM": 0x40, "MSG_EOR": 0x80}


def fnv1a64(s: str) -> int:
    h = 0xCBF29CE484222325
    for b in s.encode("utf-8", "replace"):
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def clamp8(v: int) -> int:
    return 0 if v < 0 else (255 if v > 255 else v)


def extract(rec: dict) -> list[int]:
    """One trace record -> 16 bytes.  Mirror this exactly in infer.bpf.c."""
    f = [0] * N_FEATURES

    # Syscall numbers run past 255 on x86-64 (openat is 257), so the hook
    # folds rather than truncates: & 0xff would collide openat(257) with
    # read(1), which is the single worst collision available in this set.
    nr = int(rec.get("syscall_nr") or 0)
    f[F_SYSCALL_NR] = clamp8((nr & 0xFF) ^ (nr >> 8))

    f[F_HOOK_ID] = HOOK_ID.get(rec.get("hook", "none"), 0)

    ph = rec.get("path_hash")
    if ph:
        h = int(ph, 16) if isinstance(ph, str) else int(ph)
        f[F_PATH_HASH_B0] = h & 0xFF
        f[F_PATH_HASH_B1] = (h >> 8) & 0xFF

    f[F_PATH_DEPTH] = clamp8(int(rec.get("path_depth") or 0))

    # A leading-dot component: ~/.ssh, ~/.aws, .git.  One bounded walk over
    # at most three components in the hook, which is affordable.
    p = rec.get("path") or ""
    f[F_PATH_IS_DOT] = 1 if any(c.startswith(".") for c in p.split("/")[:4] if c) else 0

    f[F_FD_TYPE] = FD_TYPE.get(rec.get("fd_type", "none"), 0)

    of = 0
    for name in rec.get("open_flags") or []:
        of |= OPEN_BIT.get(name, 0)
    f[F_OPEN_FLAGS] = of

    dport = int(rec.get("dport") or 0)
    f[F_DPORT_LO] = dport & 0xFF
    f[F_DPORT_HI] = (dport >> 8) & 0xFF

    f[F_AF] = AF.get(rec.get("family"), 0)

    da = rec.get("daddr") or ""
    f[F_IS_LOOPBACK] = 1 if (da.startswith("127.") or da == "::1") else 0

    # We flatten nested transactions (P1-09), so this is 0 or 1 today. It
    # stays in the vector because removing it later is a contract change
    # and keeping a always-constant byte costs one multiply-accumulate.
    f[F_TX_DEPTH] = 1 if int(rec.get("tx_id") or 0) else 0

    ng = rec.get("ngram") or []
    if ng:
        h = fnv1a64("|".join(ng[-3:]))
        f[F_NGRAM_0] = h & 0xFF
        f[F_NGRAM_1] = (h >> 8) & 0xFF

    mf = 0
    for name in rec.get("msg_flags") or []:
        mf |= MSG_BIT.get(name, 0)
    f[F_MSG_FLAGS] = mf

    return f


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True, help="trace .jsonl")
    ap.add_argument("--out", required=True, help="output .npz")
    ap.add_argument("--exclude-synth", action="store_true",
                    help="drop label_source=synth rows. Use this the moment "
                         "real labelled traces exist -- synthetic labels are "
                         "not evidence (docs/trace-format.md).")
    a = ap.parse_args(argv)

    try:
        import numpy as np
    except ImportError:
        print("features: numpy required -- pip install numpy", file=sys.stderr)
        return 1

    X, y, ff, src = [], [], [], []
    skipped_unlabelled = skipped_synth = 0

    with open(a.inp, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                print("features: %s:%d: %s" % (a.inp, lineno, e), file=sys.stderr)
                return 1

            lab = rec.get("label")
            if lab is None:
                skipped_unlabelled += 1
                continue
            if a.exclude_synth and rec.get("label_source") == "synth":
                skipped_synth += 1
                continue
            if lab not in CLASS_ID:
                print("features: %s:%d: unknown label %r" % (a.inp, lineno, lab),
                      file=sys.stderr)
                return 1

            X.append(extract(rec))
            y.append(CLASS_ID[lab])
            # Carried alongside, not as a feature: awaits_reply is derived
            # from what happened *after* the syscall, so the hook cannot
            # know it at decision time.  Training on it would leak the
            # future and inflate accuracy by exactly the amount that
            # matters.  It is kept only to report the gate split per class.
            ff.append(rec.get("awaits_reply"))
            src.append(rec.get("label_source") or "?")

    if not X:
        print("features: no labelled rows found", file=sys.stderr)
        return 1

    Xa = np.asarray(X, dtype=np.uint8)
    ya = np.asarray(y, dtype=np.uint8)
    ffa = np.asarray([2 if v is None else int(bool(v)) for v in ff], dtype=np.uint8)

    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(a.out, X=Xa, y=ya, awaits_reply=ffa,
                        feature_names=np.asarray(FEATURE_NAMES),
                        class_names=np.asarray(CLASS_NAMES),
                        label_source=np.asarray(src))

    print("features: %d rows x %d features -> %s" % (Xa.shape[0], Xa.shape[1], a.out))
    if skipped_unlabelled:
        print("  skipped %d unlabelled rows" % skipped_unlabelled)
    if skipped_synth:
        print("  skipped %d synthetic-label rows" % skipped_synth)

    print("  class balance:")
    for i, name in enumerate(CLASS_NAMES):
        n = int((ya == i).sum())
        print("    %-12s %6d  %5.1f%%" % (name, n, 100 * n / len(ya)))

    # Constant columns carry no information and are worth knowing about:
    # they are usually a sign that the collector is not filling a field.
    const = [FEATURE_NAMES[i] for i in range(N_FEATURES) if len(set(Xa[:, i].tolist())) == 1]
    if const:
        print("  constant features (no signal, check the collector): %s" % ", ".join(const))

    if len(set(src)) == 1 and src[0] == "synth":
        print("\n  NOTE: every row is synthetic. Downstream accuracy is a")
        print("  pipeline check, not a result.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
