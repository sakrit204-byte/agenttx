#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/harness/train.py --- float classifier baseline.  Fragment P4-07.

Trains two models against the same features and reports both:

  tree   a depth-limited decision tree.  The verifier-friendly fallback of
         P4-10, and the one most likely to ship: a tree is a bounded walk
         over integer comparisons, which is exactly what eBPF permits.
  mlp    a 16 -> 32 -> 4 MLP.  The headline result if int8 quantisation
         holds up (P4-08) and the verifier accepts the forward pass.

Both are constrained here to what include/agenttx.h can actually represent:
TX_TREE_MAX_DEPTH, TX_TREE_MAX_NODES, TX_MLP_HIDDEN.  Training a model the
blob format cannot hold wastes the export step's time and yours.

WHAT TO READ IN THE OUTPUT
--------------------------
Not the accuracy.  The corpus is ~95% reversible, so predicting
"reversible" unconditionally scores 95% and is worthless and dangerous.

Read these instead:

  irrevocable recall   the safety number.  A missed irrevocable is an
                       effect that left the machine when it should have
                       been escalated.  This is the metric the threat
                       model cares about and the one to put in the paper.
  balanced accuracy    mean per-class recall; immune to the imbalance.
  escalation rate      how often fail-closed fires.  A classifier that
                       escalates everything is perfectly safe and useless;
                       this is the other half of the trade-off.

Usage:
    python3 train.py --in data/traces/features.npz --out data/model
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# include/agenttx.h
TX_TREE_MAX_DEPTH = 16
TX_TREE_MAX_NODES = 512
TX_MLP_HIDDEN = 32
TX_CONFIDENCE_MIN = 178          # ~0.70 in 8-bit fixed point
CLASS_IRREVOCABLE = 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", required=True, help="features .npz")
    ap.add_argument("--out", required=True, help="output model directory")
    ap.add_argument("--test-size", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-depth", type=int, default=TX_TREE_MAX_DEPTH)
    a = ap.parse_args(argv)

    try:
        import numpy as np
        from sklearn.model_selection import train_test_split
        from sklearn.tree import DecisionTreeClassifier
        from sklearn.neural_network import MLPClassifier
        from sklearn.metrics import (classification_report, confusion_matrix,
                                     balanced_accuracy_score, accuracy_score)
        import pickle
    except ImportError as e:
        print("train: %s -- pip install numpy scikit-learn" % e, file=sys.stderr)
        return 1

    d = np.load(a.inp, allow_pickle=True)
    X, y = d["X"], d["y"]
    names = [str(s) for s in d["class_names"]]
    fnames = [str(s) for s in d["feature_names"]]
    srcs = set(str(s) for s in d["label_source"]) if "label_source" in d else set()

    if a.max_depth > TX_TREE_MAX_DEPTH:
        print("train: --max-depth %d exceeds TX_TREE_MAX_DEPTH (%d); the BPF "
              "walk is bounded at that depth and the blob cannot hold more."
              % (a.max_depth, TX_TREE_MAX_DEPTH), file=sys.stderr)
        return 2

    Xtr, Xte, ytr, yte = train_test_split(
        X, y, test_size=a.test_size, random_state=a.seed, stratify=y)

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    report: dict = {"n_train": int(len(ytr)), "n_test": int(len(yte)),
                    "seed": a.seed, "models": {}}

    def evaluate(tag, clf, proba):
        pred = proba.argmax(axis=1)
        conf = proba.max(axis=1)

        # Apply the fail-closed rule from include/agenttx.h exactly as the
        # kernel will: below the confidence floor the answer is discarded
        # and the effect is treated as irrevocable.  Reporting accuracy
        # without this measures a classifier that is not the one deployed.
        gated = pred.copy()
        low = conf < (TX_CONFIDENCE_MIN / 255.0)
        gated[low] = CLASS_IRREVOCABLE

        acc = accuracy_score(yte, gated)
        bacc = balanced_accuracy_score(yte, gated)
        cm = confusion_matrix(yte, gated, labels=list(range(len(names))))

        # Safety metric: of the effects that really were irrevocable, how
        # many did we classify as something we would have let through?
        n_irr = cm[CLASS_IRREVOCABLE].sum()
        irr_recall = cm[CLASS_IRREVOCABLE, CLASS_IRREVOCABLE] / n_irr if n_irr else float("nan")
        missed = int(n_irr - cm[CLASS_IRREVOCABLE, CLASS_IRREVOCABLE])
        esc_rate = float((gated == CLASS_IRREVOCABLE).mean())

        print("\n" + "=" * 66)
        print("%s   (fail-closed gate applied at confidence < %.2f)"
              % (tag, TX_CONFIDENCE_MIN / 255.0))
        print("=" * 66)
        print(classification_report(yte, gated, labels=list(range(len(names))),
                                    target_names=names, zero_division=0, digits=3))
        print("confusion matrix (rows = truth, cols = predicted)")
        print("%-14s %s" % ("", "".join("%12s" % n[:11] for n in names)))
        for i, n in enumerate(names):
            print("%-14s %s" % (n, "".join("%12d" % v for v in cm[i])))

        print()
        print("  accuracy             %.4f   <- ignore this; 95%% is the majority class"
              % acc)
        print("  balanced accuracy    %.4f   <- mean per-class recall" % bacc)
        print("  IRREVOCABLE recall   %.4f   <- THE SAFETY NUMBER (%d missed)"
              % (irr_recall, missed))
        print("  escalation rate      %.4f   <- fraction sent to the human gate"
              % esc_rate)
        if missed:
            print("  ** %d irrevocable effect(s) would have been emitted. **" % missed)

        report["models"][tag] = {
            "accuracy": float(acc),
            "balanced_accuracy": float(bacc),
            "irrevocable_recall": float(irr_recall),
            "irrevocable_missed": missed,
            "escalation_rate": esc_rate,
            "confusion_matrix": cm.tolist(),
        }
        return bacc

    # --- tree -------------------------------------------------------------
    # class_weight="balanced" is not optional at 95/2/1/2.  Without it the
    # tree learns to answer "reversible" and scores 95%.
    tree = DecisionTreeClassifier(
        max_depth=a.max_depth, max_leaf_nodes=TX_TREE_MAX_NODES // 2,
        class_weight="balanced", random_state=a.seed)
    tree.fit(Xtr, ytr)
    n_nodes = tree.tree_.node_count
    print("tree: %d nodes, depth %d (blob limit %d nodes, depth %d)"
          % (n_nodes, tree.get_depth(), TX_TREE_MAX_NODES, TX_TREE_MAX_DEPTH))
    if n_nodes > TX_TREE_MAX_NODES:
        print("train: tree has %d nodes, blob holds %d -- lower --max-depth"
              % (n_nodes, TX_TREE_MAX_NODES), file=sys.stderr)
        return 2
    evaluate("tree", tree, tree.predict_proba(Xte))

    # --- mlp --------------------------------------------------------------
    # MLPClassifier accepts neither class_weight nor sample_weight, so to
    # compare it against a class_weight="balanced" tree we have to balance
    # its input instead -- otherwise the comparison is rigged and the
    # obvious conclusion ("the tree wins") is an artefact of the handicap
    # rather than a finding.  Oversample each minority class with
    # replacement up to the majority count.
    rng = np.random.default_rng(a.seed)
    counts = np.bincount(ytr, minlength=len(names))
    target = counts.max()
    idx = []
    for c in range(len(names)):
        ci = np.flatnonzero(ytr == c)
        if len(ci) == 0:
            continue
        idx.append(ci)
        if len(ci) < target:
            idx.append(rng.choice(ci, target - len(ci), replace=True))
    bal = np.concatenate(idx)
    rng.shuffle(bal)
    print("\nmlp: oversampled %d -> %d rows to balance classes %s -> %s"
          % (len(ytr), len(bal), counts.tolist(),
             np.bincount(ytr[bal], minlength=len(names)).tolist()))

    # Inputs are u8 0..255.  Scale to 0..1 here; the BPF forward pass gets
    # the raw byte and folds the scale into the first-layer shift, so this
    # division must NOT survive into the exported weights.  quantize.py
    # accounts for it.
    mlp = MLPClassifier(hidden_layer_sizes=(TX_MLP_HIDDEN,), activation="relu",
                        solver="adam", max_iter=400, random_state=a.seed,
                        early_stopping=True, n_iter_no_change=20)
    mlp.fit(Xtr[bal] / 255.0, ytr[bal])
    print("mlp: 16 -> %d -> %d, converged in %d iterations"
          % (TX_MLP_HIDDEN, len(names), mlp.n_iter_))
    evaluate("mlp", mlp, mlp.predict_proba(Xte / 255.0))

    # --- feature importance: what is the tree actually keying on? ---------
    print("\n" + "=" * 66)
    print("tree feature importance")
    print("=" * 66)
    imp = sorted(zip(fnames, tree.feature_importances_), key=lambda t: -t[1])
    for n, v in imp:
        if v > 0.0005:
            print("  %-14s %6.3f  %s" % (n, v, "#" * int(v * 50)))
    report["feature_importance"] = {n: float(v) for n, v in imp}

    # --- persist -----------------------------------------------------------
    with open(outdir / "tree.pkl", "wb") as fh:
        pickle.dump(tree, fh)
    with open(outdir / "mlp.pkl", "wb") as fh:
        pickle.dump(mlp, fh)
    np.savez_compressed(outdir / "split.npz", Xte=Xte, yte=yte)
    with open(outdir / "report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    print("\nwrote %s/{tree.pkl,mlp.pkl,split.npz,report.json}" % outdir)

    if srcs == {"synth"}:
        print()
        print("  " + "!" * 62)
        print("  EVERY LABEL IN THIS RUN IS SYNTHETIC.")
        print("  These numbers show the pipeline works. They are not a result,")
        print("  and no figure in the paper may be drawn from them. Re-run")
        print("  against real captures with --exclude-synth at M2.")
        print("  " + "!" * 62)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
