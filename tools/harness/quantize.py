#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/harness/quantize.py --- int8 quantisation.  Fragment P4-08.

"Report accuracy lost.  This is half of the headline figure."

The eBPF verifier forbids floating point outright, so the forward pass that
runs inside the LSM hook is integer arithmetic and nothing else.  This file
converts the float model into that form and, critically, *simulates the
integer pass exactly as the kernel will execute it* so the accuracy delta
reported here is the delta you actually ship.

A quantiser that reports the delta between float and "float rounded to
int8" is measuring the wrong thing: it omits the accumulator widths, the
shifts and the saturation, which is where the error actually comes from.
The reference implementation below is therefore written the way BPF will
be written -- int32 accumulators, explicit shifts, no division -- and
src/policy/infer.bpf.c must reproduce it bit for bit.  tests/p4/t07_infer.sh
checks that by running both over the same vectors.

THE ARITHMETIC
--------------
Float model:  z1 = W1f . (x/255) + b1f ; h = relu(z1) ; z2 = W2f . h + b2f

Symmetric per-tensor quantisation, W1f ~ s1 * W1q with s1 = max|W1f|/127:

    acc1[j]   = sum_i x[i] * W1q[j][i]          int32, x is the RAW u8
    h_int[j]  = max(0, acc1[j] + b1q[j]) >> h_shift
    acc2[c]   = sum_j h_int[j] * W2q[c][j] + b2q[c]
    class     = argmax_c acc2[c]

with b1q = round(b1f * 255 / s1) and b2q = round(b2f * alpha / s2), where
alpha = (255 / s1) / 2^h_shift is the scale h_int carries.

argmax is invariant under a positive scale, so no rescaling is needed
before the decision -- which is the whole reason this fits in a hook.

Confidence comes from the top-1/top-2 margin through a fixed-point sigmoid
LUT, because the fail-closed rule in include/agenttx.h needs a 0..255
number and a softmax needs exp().

Usage:
    python3 quantize.py --model data/model --features data/traces/features.npz --out data/model
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TX_MLP_HIDDEN = 32
TX_CLASS_MAX = 4
TX_N_FEATURES = 16
TX_CONFIDENCE_MIN = 178
CLASS_IRREVOCABLE = 3

# Fixed-point sigmoid LUT.  64 entries over a margin range of +-8.0 in the
# logit-ish units the margin comes out in.  The BPF side indexes this with
# a shift and a clamp; no division, no exp.
SIGMOID_LUT_N = 64
SIGMOID_LUT_RANGE = 8.0


def build_sigmoid_lut():
    import numpy as np
    xs = np.linspace(0.0, SIGMOID_LUT_RANGE, SIGMOID_LUT_N)
    # 2*sigmoid(x)-1 maps a non-negative margin onto 0..1, so a zero margin
    # is zero confidence and a large margin saturates.  Scaled to 0..255.
    v = (2.0 / (1.0 + np.exp(-xs)) - 1.0) * 255.0
    return np.clip(np.rint(v), 0, 255).astype(np.uint8)


def quantize_tensor(W, bits=8):
    """Symmetric per-tensor quantisation to int8.  Returns (Wq, scale)."""
    import numpy as np
    qmax = (1 << (bits - 1)) - 1          # 127
    amax = float(np.abs(W).max())
    if amax == 0.0:
        return np.zeros_like(W, dtype=np.int8), 1.0
    scale = amax / qmax
    Wq = np.clip(np.rint(W / scale), -qmax, qmax).astype(np.int8)
    return Wq, scale


def forward_int(X, W1q, b1q, h_shift, W2q, b2q, lut):
    """The integer forward pass, written the way the BPF program will be.

    X is the RAW u8 feature matrix -- no scaling, because a hook receives
    raw bytes and cannot divide.  Returns (class, confidence 0..255).
    """
    import numpy as np
    x = X.astype(np.int32)

    # Layer 1.  int32 accumulator.  Bound check: 16 features * 255 * 127 =
    # 518160, so int32 has ~4000x headroom here and cannot overflow.
    acc1 = x @ W1q.astype(np.int32).T + b1q.astype(np.int32)
    h = np.maximum(acc1, 0) >> h_shift

    # Layer 2.
    acc2 = h @ W2q.astype(np.int32).T + b2q.astype(np.int32)

    cls = acc2.argmax(axis=1)

    # Confidence from the top-1/top-2 margin.  Scale-invariance of argmax
    # does not extend to the margin, so normalise by the per-row magnitude
    # with a shift rather than a divide -- the BPF side does the same.
    srt = np.sort(acc2, axis=1)
    margin = srt[:, -1] - srt[:, -2]
    mag = np.maximum(np.abs(acc2).max(axis=1), 1)
    # margin/mag in [0,1]; index the LUT over [0, SIGMOID_LUT_RANGE].
    idx = np.clip((margin * SIGMOID_LUT_N) // mag, 0, SIGMOID_LUT_N - 1)
    conf = lut[idx.astype(np.int32)]
    return cls, conf


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True, help="model dir from train.py")
    ap.add_argument("--features", required=True, help="features .npz")
    ap.add_argument("--out", required=True, help="output dir")
    ap.add_argument("--h-shift", type=int, default=None,
                    help="layer-1 output shift; auto-chosen if omitted")
    a = ap.parse_args(argv)

    try:
        import numpy as np
        import pickle
        from sklearn.metrics import balanced_accuracy_score, confusion_matrix
    except ImportError as e:
        print("quantize: %s -- pip install numpy scikit-learn" % e, file=sys.stderr)
        return 1

    mdir = Path(a.model)
    with open(mdir / "mlp.pkl", "rb") as fh:
        mlp = pickle.load(fh)

    d = np.load(a.features, allow_pickle=True)
    names = [str(s) for s in d["class_names"]]
    sp = np.load(mdir / "split.npz")
    Xte, yte = sp["Xte"], sp["yte"]

    W1f = mlp.coefs_[0].T.astype(np.float64)      # (hidden, features)
    b1f = mlp.intercepts_[0].astype(np.float64)
    W2f = mlp.coefs_[1].T.astype(np.float64)      # (classes, hidden)
    b2f = mlp.intercepts_[1].astype(np.float64)

    if W1f.shape != (TX_MLP_HIDDEN, TX_N_FEATURES):
        print("quantize: layer 1 is %s, blob expects (%d, %d)"
              % (W1f.shape, TX_MLP_HIDDEN, TX_N_FEATURES), file=sys.stderr)
        return 2
    if W2f.shape != (TX_CLASS_MAX, TX_MLP_HIDDEN):
        print("quantize: layer 2 is %s, blob expects (%d, %d)"
              % (W2f.shape, TX_CLASS_MAX, TX_MLP_HIDDEN), file=sys.stderr)
        return 2

    W1q, s1 = quantize_tensor(W1f)
    W2q, s2 = quantize_tensor(W2f)

    # b1 lives in the accumulator's units: acc1 ~ (255/s1) * z1_pre.
    b1q = np.clip(np.rint(b1f * 255.0 / s1), -2**31, 2**31 - 1).astype(np.int32)

    # Choose h_shift so the hidden activations land around 2^11.  Too small
    # and layer 2 can overflow int32; too large and the hidden layer
    # quantises to a handful of distinct values and the model collapses.
    if a.h_shift is None:
        probe = Xte.astype(np.int32) @ W1q.astype(np.int32).T + b1q
        hmax = int(max(np.maximum(probe, 0).max(), 1))
        h_shift = max(0, int(np.ceil(np.log2(hmax / 2048.0))))
    else:
        h_shift = a.h_shift

    alpha = (255.0 / s1) / float(1 << h_shift)
    b2q = np.clip(np.rint(b2f * alpha / s2), -2**31, 2**31 - 1).astype(np.int32)

    # Overflow check for layer 2, stated rather than hoped for.
    probe = Xte.astype(np.int32) @ W1q.astype(np.int32).T + b1q
    h_probe = np.maximum(probe, 0) >> h_shift
    worst = int(np.abs(h_probe).max()) * 127 * TX_MLP_HIDDEN + int(np.abs(b2q).max())
    if worst >= 2**31:
        print("quantize: layer-2 accumulator can overflow int32 (worst %d); "
              "raise --h-shift" % worst, file=sys.stderr)
        return 2

    lut = build_sigmoid_lut()

    # --- float reference, with the same fail-closed gate ------------------
    pf = mlp.predict_proba(Xte / 255.0)
    cls_f = pf.argmax(axis=1)
    conf_f = (pf.max(axis=1) * 255).astype(np.int32)
    gated_f = np.where(conf_f < TX_CONFIDENCE_MIN, CLASS_IRREVOCABLE, cls_f)

    # --- integer, exactly as the hook will run it -------------------------
    cls_i, conf_i = forward_int(Xte, W1q, b1q, h_shift, W2q, b2q, lut)
    gated_i = np.where(conf_i < TX_CONFIDENCE_MIN, CLASS_IRREVOCABLE, cls_i)

    def stats(g):
        cm = confusion_matrix(yte, g, labels=list(range(TX_CLASS_MAX)))
        n_irr = cm[CLASS_IRREVOCABLE].sum()
        return (balanced_accuracy_score(yte, g),
                cm[CLASS_IRREVOCABLE, CLASS_IRREVOCABLE] / n_irr if n_irr else float("nan"),
                int(n_irr - cm[CLASS_IRREVOCABLE, CLASS_IRREVOCABLE]),
                float((g == CLASS_IRREVOCABLE).mean()))

    bf, rf, mf, ef = stats(gated_f)
    bi, ri, mi, ei = stats(gated_i)
    agree = float((gated_f == gated_i).mean())

    print("=" * 68)
    print("int8 quantisation  (h_shift=%d, s1=%.6g, s2=%.6g)" % (h_shift, s1, s2))
    print("=" * 68)
    print("  %-22s %12s %12s %10s" % ("", "float32", "int8", "delta"))
    print("  %-22s %12.4f %12.4f %+10.4f" % ("balanced accuracy", bf, bi, bi - bf))
    print("  %-22s %12.4f %12.4f %+10.4f" % ("irrevocable recall", rf, ri, ri - rf))
    print("  %-22s %12d %12d %+10d" % ("irrevocable missed", mf, mi, mi - mf))
    print("  %-22s %12.4f %12.4f %+10.4f" % ("escalation rate", ef, ei, ei - ef))
    print()
    print("  decision agreement     %.4f   (%d of %d rows differ)"
          % (agree, int((gated_f != gated_i).sum()), len(yte)))
    print()
    print("  model size             %d B float32 -> %d B int8  (%.1fx smaller)"
          % (W1f.nbytes + b1f.nbytes + W2f.nbytes + b2f.nbytes,
             W1q.nbytes + W2q.nbytes + b1q.nbytes + b2q.nbytes,
             (W1f.nbytes + b1f.nbytes + W2f.nbytes + b2f.nbytes) /
             max(W1q.nbytes + W2q.nbytes + b1q.nbytes + b2q.nbytes, 1)))

    if mi > mf:
        print()
        print("  ** quantisation LOST %d irrevocable detection(s). **" % (mi - mf))
        print("  That is a safety regression, not a rounding error. Either")
        print("  raise TX_CONFIDENCE_MIN, or ship the tree (P4-10's fallback).")

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(outdir / "mlp_int8.npz",
                        W1q=W1q, b1q=b1q, W2q=W2q, b2q=b2q,
                        h_shift=np.int32(h_shift), lut=lut,
                        s1=np.float64(s1), s2=np.float64(s2))

    rep = {
        "h_shift": h_shift, "s1": s1, "s2": s2,
        "float": {"balanced_accuracy": bf, "irrevocable_recall": rf,
                  "irrevocable_missed": mf, "escalation_rate": ef},
        "int8": {"balanced_accuracy": bi, "irrevocable_recall": ri,
                 "irrevocable_missed": mi, "escalation_rate": ei},
        "decision_agreement": agree,
    }
    with open(outdir / "quantize_report.json", "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=2)

    print("\nwrote %s/{mlp_int8.npz,quantize_report.json}" % outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
