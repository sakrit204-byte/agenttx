#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/harness/label.py --- apply and compare effect labels.  Fragment P4-05.

    label.py --in data/gate/*.jsonl --rules --out data/labels.csv
    label.py --interactive --in data/gate/readonly.jsonl --labeller alice \
             --out data/labels-alice.csv
    label.py --agreement data/labels-alice.csv data/labels-bob.csv

WHAT THIS IS FOR
----------------
`docs/taxonomy.md` claims to be a *decision procedure*, not four adjectives.
This file is that claim made executable: --rules applies section 2 literally,
in order, first-match-wins.

That matters for a reason beyond convenience. If a human labeller disagrees
with the rule labeller, exactly one of two things is true: the human made a
mistake, or **the document is ambiguous**. The taxonomy's own protocol
(section 7 item 4) says disagreements are resolved by amending the document,
never by one labeller deferring. Having the document executable is what makes
that check possible at all.

WHAT IT IS NOT
--------------
The rule labeller is NOT a second opinion. It is the document. Reporting
agreement between a human and it measures *how well the document is
written*, not how reliable the labels are.

The kappa that goes in the paper is between **two humans**, and this tool
computes it -- it cannot supply the humans.

Owner: P4.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import sys
from pathlib import Path

CLASSES = ["reversible", "deferrable", "compensable", "irrevocable"]

# The compensable registry.  Shipped, not learned (PROPOSAL.md): `compensable`
# means "a compensation is DECLARED to this system", never "a compensation
# plausibly exists somewhere".  Empty by default, and that is the honest
# default -- nothing has been declared.
REGISTRY: dict[tuple, int] = {}


def rule_label(rec: dict) -> tuple[str, str]:
    """
    docs/taxonomy.md section 2, applied in order, first match wins.
    Returns (class, which rule decided it).
    """
    hook = rec.get("hook") or "none"
    verdict = rec.get("verdict")
    daddr = rec.get("daddr") or ""
    dport = rec.get("dport") or 0

    # Q1: did it leave the machine?
    left = hook in ("socket_sendmsg", "tls_write")

    if not left:
        # Q2: captured by the CoW layer?
        #
        # file_open / inode_unlink / inode_rename inside a transaction are
        # captured -- tests/p2/t02 proves creation, modification AND deletion
        # all roll back. connect() and bprm_check change no state that leaves.
        if hook in ("file_open", "inode_unlink", "inode_rename",
                    "bprm_check", "socket_connect", "none"):
            return "reversible", "Q2 (CoW captures it)"
        # Q3: reconstructible without outside cooperation?  We have no way to
        # assert that from a trace, so fail closed.
        return "irrevocable", "Q3 (not reconstructible from what we hold)"

    # Q4: emitted, or held?
    if verdict == "deferred":
        return "deferrable", "Q4 (the mechanism held it)"

    # Q5: a DECLARED compensation with an open window?
    key = (daddr, dport)
    if key in REGISTRY or (daddr, 0) in REGISTRY:
        return "compensable", "Q5 (declared in the registry)"

    # Loopback never left the machine; taxonomy section 6 item 2 records this
    # as a contested choice, correct only for a single-machine threat model.
    if daddr.startswith("127.") or daddr == "::1":
        return "reversible", "Q1 (loopback; see taxonomy section 6.2)"

    return "irrevocable", "Q5 (no declared compensation)"


def load(paths: list[str]) -> list[dict]:
    recs = []
    for pat in paths:
        for f in sorted(glob.glob(pat)):
            src = Path(f).name
            for i, line in enumerate(open(f), 1):
                if not line.strip():
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                r["_src"] = src
                r["_line"] = i
                recs.append(r)
    return recs


def key_of(r: dict) -> str:
    return f"{r['_src']}:{r['_line']}"


def write_csv(path: str, rows: list[dict], labeller: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", newline="\n") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["key", "src", "line", "hook", "daddr", "dport",
                    "payload_len", "awaits_reply", "label", "labeller",
                    "rule", "notes"])
        for r in rows:
            w.writerow([r["key"], r["src"], r["line"], r["hook"], r["daddr"],
                        r["dport"], r["payload_len"], r["awaits_reply"],
                        r["label"], labeller, r.get("rule", ""),
                        r.get("notes", "")])


def cohen_kappa(a: dict[str, str], b: dict[str, str]) -> tuple[float, int, int, dict]:
    """Cohen's kappa over the keys both labellers covered."""
    shared = sorted(set(a) & set(b))
    n = len(shared)
    if n == 0:
        return float("nan"), 0, 0, {}

    agree = sum(1 for k in shared if a[k] == b[k])
    po = agree / n

    pe = 0.0
    for c in CLASSES:
        pa = sum(1 for k in shared if a[k] == c) / n
        pb = sum(1 for k in shared if b[k] == c) / n
        pe += pa * pb

    kappa = (po - pe) / (1 - pe) if pe < 1 else float("nan")

    # Where they disagree, per class-pair.
    conf: dict = {}
    for k in shared:
        if a[k] != b[k]:
            conf[(a[k], b[k])] = conf.get((a[k], b[k]), 0) + 1
    return kappa, n, agree, conf


def reading(k: float) -> str:
    if k != k:
        return "undefined"
    if k < 0.4:
        return "NOT OPERATIONAL -- do not train on these labels"
    if k < 0.6:
        return "usable, but report the disagreements per class"
    if k < 0.8:
        return "substantial -- the normal target"
    return "suspiciously high -- check the labellers were independent"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", nargs="*", default=[],
                    help="trace .jsonl file(s); globs allowed")
    ap.add_argument("--rules", action="store_true",
                    help="label by docs/taxonomy.md section 2")
    ap.add_argument("--interactive", action="store_true",
                    help="label by hand")
    ap.add_argument("--labeller", default="rules")
    ap.add_argument("--out")
    ap.add_argument("--agreement", nargs=2, metavar=("A.csv", "B.csv"))
    a = ap.parse_args(argv)

    if a.agreement:
        sets = []
        for f in a.agreement:
            m = {}
            who = "?"
            for row in csv.DictReader(open(f, newline="")):
                m[row["key"]] = row["label"]
                who = row["labeller"] or who
            sets.append((who, m, f))

        (na, A, fa), (nb, B, fb) = sets
        k, n, agree, conf = cohen_kappa(A, B)

        print("=" * 62)
        print("  inter-rater agreement (P4-05)")
        print("=" * 62)
        print(f"  A: {na:<20} {len(A):5} labels   {fa}")
        print(f"  B: {nb:<20} {len(B):5} labels   {fb}")
        print(f"  compared on {n} records both covered")
        print()
        print(f"  raw agreement   {100.0 * agree / n if n else 0:.1f}%  ({agree}/{n})")
        print(f"  Cohen's kappa   {k:.3f}   -- {reading(k)}")

        if "rules" in (na, nb):
            print()
            print("  NOTE: one side is the RULE labeller, which is")
            print("  docs/taxonomy.md made executable. This measures how well")
            print("  the DOCUMENT is written, not how reliable the labels are.")
            print("  The kappa for the paper is between two humans.")

        if conf:
            print()
            print("  disagreements (A said -> B said):")
            for (x, y), c in sorted(conf.items(), key=lambda t: -t[1]):
                print(f"    {x:<12} -> {y:<12} {c}")
            print()
            print("  taxonomy.md section 7.4: resolve these by AMENDING the")
            print("  document, never by one labeller deferring. A disagreement")
            print("  is evidence the document is ambiguous.")
        return 0

    if not a.inp:
        ap.error("need --in")

    recs = load(a.inp)
    if not recs:
        print("label: no records", file=sys.stderr)
        return 1

    rows = []
    for r in recs:
        base = {
            "key": key_of(r), "src": r["_src"], "line": r["_line"],
            "hook": r.get("hook"), "daddr": r.get("daddr"),
            "dport": r.get("dport"), "payload_len": r.get("payload_len"),
            "awaits_reply": r.get("awaits_reply"),
        }
        if a.rules:
            lab, why = rule_label(r)
            base["label"] = lab
            base["rule"] = why
        elif a.interactive:
            print(f"\n{base['key']}  {base['hook']}  -> {base['daddr']}:{base['dport']}"
                  f"  {base['payload_len']}B  awaits_reply={base['awaits_reply']}")
            print("  [r]eversible [d]eferrable [c]ompensable [i]rrevocable [?]help [q]uit")
            while True:
                ch = input("  > ").strip().lower()
                if ch == "?":
                    print("  Apply docs/taxonomy.md section 2 IN ORDER.")
                    print("  Q5 asks whether a compensation is DECLARED to this")
                    print("  system -- not whether one plausibly exists.")
                    continue
                if ch == "q":
                    break
                if ch and ch[0] in "rdci":
                    base["label"] = {"r": "reversible", "d": "deferrable",
                                     "c": "compensable", "i": "irrevocable"}[ch[0]]
                    base["notes"] = input("  notes (optional) > ").strip()
                    break
            if "label" not in base:
                break
        else:
            ap.error("need --rules or --interactive")
        rows.append(base)

    dist: dict = {}
    for r in rows:
        dist[r["label"]] = dist.get(r["label"], 0) + 1

    print(f"labelled {len(rows)} record(s) as '{a.labeller}'")
    for c in CLASSES:
        n = dist.get(c, 0)
        print(f"  {c:<14}{n:5}  {100.0 * n / len(rows):5.1f}%")

    if a.rules:
        why: dict = {}
        for r in rows:
            why[r["rule"]] = why.get(r["rule"], 0) + 1
        print("\n  decided by:")
        for k, v in sorted(why.items(), key=lambda t: -t[1]):
            print(f"    {k:<46}{v}")
        print("\n  These are the DOCUMENT's labels, not a human's.")
        print("  P4-05 is not complete until two people have labelled")
        print("  independently and the kappa is reported.")

    if a.out:
        write_csv(a.out, rows, a.labeller)
        print(f"\nwrote {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
