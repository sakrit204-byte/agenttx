#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/harness/export_weights.py --- model blob for the BPF map.  P4-09.

"Versioned blob format -- P3 loads it at attach time."

This is the seam where Python and C must agree byte for byte.  The layouts
below are `struct tx_model_mlp` and `struct tx_model_tree` from
include/agenttx.h, and the sizes are asserted, not assumed: if a
contract-change PR alters a field, this file fails loudly here rather than
producing a blob the loader misreads into plausible-looking garbage.

    struct tx_model_hdr    24 B
    struct tx_model_mlp   812 B   hdr + w1[32][16]i8 + b1[32]i32
                                      + w2[4][32]i8 + b2[4]i32 + h_shift
    struct tx_model_tree 3096 B   hdr + node[512] x 6 B

Both fit comfortably inside a single BPF map value.

The sigmoid LUT is deliberately NOT in the blob: it is a property of the
fixed-point arithmetic, not of the model, so it is emitted as a C header
and compiled into infer.bpf.c.  Shipping it per-model would invite two
models with two different LUTs and no way to tell them apart.

Usage:
    python3 export_weights.py --model data/model --out data/model/model.bin
    python3 export_weights.py --model data/model --out t.bin --kind tree
    python3 export_weights.py --verify data/model/model.bin
    python3 export_weights.py --model data/model --emit-lut src/policy/sigmoid_lut.h
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

# --- include/agenttx.h ----------------------------------------------------
TX_MODEL_MAGIC = 0x54584D44          # "TXMD"
AGENTTX_ABI_VERSION = 1
TX_MODEL_TREE, TX_MODEL_MLP = 1, 2
TX_MLP_HIDDEN = 32
TX_N_FEATURES = 16
TX_CLASS_MAX = 4
TX_TREE_MAX_NODES = 512
TX_TREE_MAX_DEPTH = 16
TX_TREE_LEAF = 0xFFFF

HDR_FMT = "<IHBBBBHiII"
HDR_SIZE = 24
MLP_SIZE = 812
TREE_SIZE = 3096
NODE_FMT = "<BBHH"
NODE_SIZE = 6

# Asserted against the C side by tools/vm/headers-check.sh, which prints
# the real sizeof() values.  Keep both in sync or the loader misreads.
assert struct.calcsize(HDR_FMT) == HDR_SIZE, struct.calcsize(HDR_FMT)
assert struct.calcsize(NODE_FMT) == NODE_SIZE, struct.calcsize(NODE_FMT)

CLASS_NAMES = ["reversible", "deferrable", "compensable", "irrevocable"]

FNV_OFF, FNV_PRIME, FNV_MASK = 14695981039346656037, 1099511628211, (1 << 64) - 1


def fnv_bytes(data, h=FNV_OFF):
    """FNV-1a over a byte sequence.  Position-SENSITIVE, unlike a sum.

    tests/p4/t06_weights.sh compares this against the same walk done in C.
    A sum would be invariant under permutation and would not notice a
    transposed weight matrix -- every weight in the wrong place, identical
    checksum.  This does.
    """
    for b in data:
        h = ((h ^ b) * FNV_PRIME) & FNV_MASK
    return h


def fnv_i8_array(arr):
    """int8 array, walked in C index order (row-major)."""
    return fnv_bytes(arr.astype("<i1").tobytes(order="C"))


def fnv_i32_array(arr):
    """int32 array, little-endian, walked in C index order."""
    return fnv_bytes(arr.astype("<i4").tobytes(order="C"))



def pack_hdr(kind, n_nodes, out_shift, trained_rows, accuracy_pct):
    return struct.pack(HDR_FMT, TX_MODEL_MAGIC, AGENTTX_ABI_VERSION, kind,
                       TX_N_FEATURES, TX_CLASS_MAX, 0, n_nodes,
                       out_shift, trained_rows, accuracy_pct)


def export_mlp(mdir: Path, trained_rows: int, acc_pct: int, meta: dict) -> bytes:
    import numpy as np
    q = np.load(mdir / "mlp_int8.npz")
    W1q, b1q, W2q, b2q = q["W1q"], q["b1q"], q["W2q"], q["b2q"]
    h_shift = int(q["h_shift"])

    # Declared INTENT, computed from the source arrays and not from the
    # bytes we are about to write.  tests/p4/t06_weights.sh checks a C
    # reader against this, so a field written to the wrong offset is
    # caught even when both readers agree with each other -- which they
    # will, since both use the same (correct) struct definition.
    meta.update(
        kind=TX_MODEL_MLP, n_nodes=0, h_shift=h_shift,
        trained_rows=trained_rows, accuracy_pct=acc_pct,
        fnv_w1=fnv_i8_array(W1q), fnv_w2=fnv_i8_array(W2q),
        fnv_b1=fnv_i32_array(b1q), fnv_b2=fnv_i32_array(b2q),
    )

    if W1q.shape != (TX_MLP_HIDDEN, TX_N_FEATURES):
        raise SystemExit("export: w1 is %s, blob expects (%d,%d)"
                         % (W1q.shape, TX_MLP_HIDDEN, TX_N_FEATURES))
    if W2q.shape != (TX_CLASS_MAX, TX_MLP_HIDDEN):
        raise SystemExit("export: w2 is %s, blob expects (%d,%d)"
                         % (W2q.shape, TX_CLASS_MAX, TX_MLP_HIDDEN))

    blob = bytearray()
    blob += pack_hdr(TX_MODEL_MLP, 0, 0, trained_rows, acc_pct)
    # C row-major order matches numpy's default; .tobytes() is the layout.
    blob += W1q.astype("<i1").tobytes(order="C")
    blob += b1q.astype("<i4").tobytes(order="C")
    blob += W2q.astype("<i1").tobytes(order="C")
    blob += b2q.astype("<i4").tobytes(order="C")
    blob += struct.pack("<i", h_shift)

    if len(blob) != MLP_SIZE:
        raise SystemExit("export: built %d B, struct tx_model_mlp is %d B"
                         % (len(blob), MLP_SIZE))
    return bytes(blob)


def export_tree(mdir: Path, trained_rows: int, acc_pct: int, meta: dict) -> bytes:
    import numpy as np
    import pickle
    with open(mdir / "tree.pkl", "rb") as fh:
        clf = pickle.load(fh)
    t = clf.tree_

    n = int(t.node_count)
    if n > TX_TREE_MAX_NODES:
        raise SystemExit("export: %d nodes, blob holds %d -- retrain with a "
                         "lower --max-depth" % (n, TX_TREE_MAX_NODES))
    if clf.get_depth() > TX_TREE_MAX_DEPTH:
        raise SystemExit("export: depth %d, the bpf_loop() bound is %d"
                         % (clf.get_depth(), TX_TREE_MAX_DEPTH))

    nodes = []
    for i in range(n):
        left, right = int(t.children_left[i]), int(t.children_right[i])
        if left == -1:                       # leaf
            counts = t.value[i][0]
            total = float(counts.sum()) or 1.0
            klass = int(counts.argmax())
            # Leaf confidence is the winning class's share of the samples
            # that reached it.  This is what tx_class_final() gates on, so
            # an impure leaf correctly escalates rather than guessing.
            conf = int(round(255.0 * counts.max() / total))
            nodes.append((min(conf, 255), klass, TX_TREE_LEAF, TX_TREE_LEAF))
        else:
            # sklearn splits on `X[feature] <= threshold` going left.
            # Features are integers, so floor() is exact, not an
            # approximation: a threshold of 127.5 means "<= 127".
            thr = int(np.floor(float(t.threshold[i])))
            thr = 0 if thr < 0 else (255 if thr > 255 else thr)
            feat = int(t.feature[i])
            if not 0 <= feat < TX_N_FEATURES:
                raise SystemExit("export: node %d splits on feature %d" % (i, feat))
            nodes.append((feat, thr, left, right))

    # Declared intent, from `nodes`, not from the packed bytes.  See the
    # comment in export_mlp().
    meta.update(
        kind=TX_MODEL_TREE, n_nodes=n,
        trained_rows=trained_rows, accuracy_pct=acc_pct,
        leaves=sum(1 for nd in nodes if nd[2] == TX_TREE_LEAF),
        bad_nodes=0,
        fnv_nodes=fnv_bytes(b"".join(struct.pack(NODE_FMT, *nd) for nd in nodes)),
    )

    blob = bytearray()
    blob += pack_hdr(TX_MODEL_TREE, n, 0, trained_rows, acc_pct)
    for feat, thr, left, right in nodes:
        blob += struct.pack(NODE_FMT, feat, thr, left, right)
    blob += b"\0" * ((TX_TREE_MAX_NODES - n) * NODE_SIZE)

    if len(blob) != TREE_SIZE:
        raise SystemExit("export: built %d B, struct tx_model_tree is %d B"
                         % (len(blob), TREE_SIZE))
    return bytes(blob)


def verify(path: Path) -> int:
    raw = path.read_bytes()
    if len(raw) < HDR_SIZE:
        print("verify: %s is %d B, shorter than the header" % (path, len(raw)),
              file=sys.stderr)
        return 1

    magic, abi, kind, nfeat, nclass, _pad, nnodes, out_shift, rows, acc = \
        struct.unpack(HDR_FMT, raw[:HDR_SIZE])

    ok = True

    def chk(cond, msg):
        nonlocal ok
        print("  %s  %s" % ("ok  " if cond else "FAIL", msg))
        ok = ok and cond

    print("verify: %s (%d B)" % (path, len(raw)))
    chk(magic == TX_MODEL_MAGIC, "magic 0x%08x (TXMD)" % magic)
    chk(abi == AGENTTX_ABI_VERSION,
        "abi %d (loader expects %d)" % (abi, AGENTTX_ABI_VERSION))
    chk(kind in (TX_MODEL_TREE, TX_MODEL_MLP),
        "kind %d (%s)" % (kind, {1: "tree", 2: "mlp"}.get(kind, "?")))
    chk(nfeat == TX_N_FEATURES, "n_features %d" % nfeat)
    chk(nclass == TX_CLASS_MAX, "n_classes %d" % nclass)

    if kind == TX_MODEL_MLP:
        chk(len(raw) == MLP_SIZE, "size %d == sizeof(struct tx_model_mlp)" % len(raw))
        if len(raw) == MLP_SIZE:
            h_shift = struct.unpack("<i", raw[808:812])[0]
            chk(0 <= h_shift < 31, "h_shift %d" % h_shift)
    else:
        chk(len(raw) == TREE_SIZE, "size %d == sizeof(struct tx_model_tree)" % len(raw))
        chk(0 < nnodes <= TX_TREE_MAX_NODES, "n_nodes %d" % nnodes)
        if len(raw) == TREE_SIZE:
            # Every non-leaf child index must be in range, or the bounded
            # walk in BPF reads outside the node array.  The verifier will
            # reject an unbounded index anyway; this catches the bug here,
            # where the error message is readable.
            bad = 0
            leaves = 0
            for i in range(nnodes):
                off = HDR_SIZE + i * NODE_SIZE
                f, th, l, r = struct.unpack(NODE_FMT, raw[off:off + NODE_SIZE])
                if l == TX_TREE_LEAF:
                    leaves += 1
                    if th >= TX_CLASS_MAX:
                        bad += 1
                else:
                    if not (0 < l < nnodes and 0 < r < nnodes) or f >= TX_N_FEATURES:
                        bad += 1
            chk(bad == 0, "%d nodes (%d leaves), all indices in range" % (nnodes, leaves))

    print("  info  trained on %d rows, held-out accuracy %.2f%%" % (rows, acc / 100.0))
    print("  %s" % ("blob OK" if ok else "BLOB REJECTED"))
    return 0 if ok else 1


def emit_lut(path: Path) -> None:
    from quantize import build_sigmoid_lut, SIGMOID_LUT_N
    lut = build_sigmoid_lut()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("/* SPDX-License-Identifier: GPL-2.0 */\n")
        fh.write("/* GENERATED by tools/harness/export_weights.py --emit-lut."
                 "  Do not edit. */\n")
        fh.write("/*\n"
                 " * Fixed-point sigmoid over the top-1/top-2 margin.  The eBPF\n"
                 " * verifier forbids floating point, so confidence comes from this\n"
                 " * table rather than a softmax.  It is a property of the\n"
                 " * arithmetic, not of the model, which is why it is compiled in\n"
                 " * rather than shipped in the weight blob.\n"
                 " */\n")
        fh.write("#ifndef _TX_SIGMOID_LUT_H\n#define _TX_SIGMOID_LUT_H\n\n")
        fh.write("#define TX_SIGMOID_LUT_N %d\n\n" % SIGMOID_LUT_N)
        fh.write("static const __u8 tx_sigmoid_lut[TX_SIGMOID_LUT_N] = {\n")
        for i in range(0, len(lut), 8):
            fh.write("\t" + " ".join("%3d," % v for v in lut[i:i + 8]) + "\n")
        fh.write("};\n\n#endif /* _TX_SIGMOID_LUT_H */\n")
    print("emit-lut: %d entries -> %s" % (len(lut), path))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", help="model dir from train.py / quantize.py")
    ap.add_argument("--out", help="output blob path")
    ap.add_argument("--kind", choices=("mlp", "tree"), default="mlp")
    ap.add_argument("--verify", help="validate an existing blob and exit")
    ap.add_argument("--emit-lut", help="write the sigmoid LUT C header and exit")
    a = ap.parse_args(argv)

    if a.verify:
        return verify(Path(a.verify))

    if a.emit_lut:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        emit_lut(Path(a.emit_lut))
        return 0

    if not (a.model and a.out):
        ap.error("--model and --out are required unless --verify/--emit-lut")

    mdir = Path(a.model)
    rows, acc_pct = 0, 0
    rep = mdir / "report.json"
    if rep.exists():
        import json
        r = json.loads(rep.read_text(encoding="utf-8"))
        rows = int(r.get("n_train", 0))
        # Provenance carries BALANCED accuracy, not raw accuracy: raw
        # accuracy on a 95%-majority corpus would stamp a meaningless 96 on
        # every blob and make two very different models look identical.
        m = r.get("models", {}).get(a.kind, {})
        acc_pct = int(round(100 * 100 * m.get("balanced_accuracy", 0.0)))

    meta: dict = {"magic": "0x%08x" % TX_MODEL_MAGIC, "abi": AGENTTX_ABI_VERSION,
                  "n_features": TX_N_FEATURES, "n_classes": TX_CLASS_MAX,
                  "sizeof_hdr": HDR_SIZE, "sizeof_mlp": MLP_SIZE,
                  "sizeof_tree": TREE_SIZE, "sizeof_node": NODE_SIZE}

    blob = export_mlp(mdir, rows, acc_pct, meta) if a.kind == "mlp" \
        else export_tree(mdir, rows, acc_pct, meta)
    meta["file_bytes"] = len(blob)

    outp = Path(a.out)
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_bytes(blob)

    # The intent sidecar.  Not consumed by the kernel -- it exists so the
    # blob's meaning is recorded independently of its encoding, which is
    # what makes tests/p4/t06_weights.sh able to detect an encoding bug.
    import json as _json
    meta_path = outp.with_suffix(outp.suffix + ".meta.json")
    meta_path.write_text(_json.dumps(meta, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")

    print("export: %s model, %d B -> %s" % (a.kind, len(blob), outp))
    print("        intent sidecar -> %s" % meta_path)
    print("        load with: bpftool map update pinned %s ..." % "/sys/fs/bpf/agenttx/model")
    return verify(outp)


if __name__ == "__main__":
    raise SystemExit(main())
