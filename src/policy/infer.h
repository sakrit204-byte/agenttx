/* SPDX-License-Identifier: GPL-2.0 */
/*
 * src/policy/infer.h --- the in-kernel forward pass.  Fragment P4-10.
 *
 * WHY THE TREE AND NOT THE MLP.
 *
 * P4-10 lists the quantised decision tree as the *fallback* if the verifier
 * rejects the MLP. On our own data it should be the primary, and
 * docs/STATUS.md already said so before a line of this was written:
 *
 *              balanced acc   irrevocable recall   escalation
 *   tree           0.574            0.978            0.055
 *   int8 MLP       0.588            0.889            0.152
 *
 * The MLP is marginally better on balanced accuracy and materially worse on
 * the only metric with a safety meaning: it misses 10 irrevocable effects
 * the float model caught, and escalates three times as often. For a
 * fail-closed classifier, "misses irrevocable effects" is not a tuning
 * question.
 *
 * It is also what eBPF wants. A tree walk is a bounded sequence of integer
 * comparisons with no arithmetic the verifier has to reason about -- no
 * accumulator width, no shift, no saturation. The MLP needs all of that and
 * buys nothing here.
 *
 * VERIFIER CONSTRAINTS, and how each is met:
 *
 *   no unbounded loops    the walk is `for (d = 0; d < TX_TREE_MAX_DEPTH;
 *                         d++)` with a compile-time constant bound, so it
 *                         unrolls. No bpf_loop() needed at depth 16.
 *   no out-of-range index every array index is masked to its bound before
 *                         use, not merely checked -- the verifier tracks the
 *                         mask, and an `if (i < N)` guard on a value it
 *                         cannot bound is rejected.
 *   no floats             there are none; this is why the tree was
 *                         quantised in the first place.
 *
 * Owner: P4.
 */

#ifndef _AGENTTX_INFER_H
#define _AGENTTX_INFER_H

#include "agenttx.h"
#include "tx_features.h"

/*
 * Walk @tree with @f.  Writes 0..255 into *conf and returns the class.
 *
 * NEVER FAILS, by contract. include/agenttx.h is explicit: "an error path
 * inside a classifier is an unclassified effect, and an unclassified effect
 * is TX_IRREVOCABLE." So every way out of this function that is not a leaf
 * returns TX_IRREVOCABLE with confidence 0 -- which tx_class_final() then
 * turns into TX_IRREVOCABLE again, because 0 < TX_CONFIDENCE_MIN. Two
 * independent reasons for the same safe answer.
 */
__tx_inline enum tx_class tx_tree_classify(const struct tx_model_tree *tree,
					   const struct tx_features *f,
					   __u8 *conf)
{
	__u16 idx = 0;
	__u16 n_nodes;
	int d;

	*conf = 0;

	if (!tree || !f)
		return TX_IRREVOCABLE;
	if (tree->hdr.magic != TX_MODEL_MAGIC)
		return TX_IRREVOCABLE;
	if (tree->hdr.kind != TX_MODEL_TREE)
		return TX_IRREVOCABLE;
	if (tree->hdr.n_features != TX_N_FEATURES)
		return TX_IRREVOCABLE;
	if (tree->hdr.n_classes != TX_CLASS_MAX)
		return TX_IRREVOCABLE;

	n_nodes = tree->hdr.n_nodes;
	if (n_nodes == 0 || n_nodes > TX_TREE_MAX_NODES)
		return TX_IRREVOCABLE;

	for (d = 0; d < (int)TX_TREE_MAX_DEPTH; d++) {
		const struct tx_tree_node *nd;
		__u8 fi, thr, fv;

		/*
		 * Mask, do not merely compare. The verifier needs the index
		 * bounded by construction; a runtime `if (idx < n_nodes)` on a
		 * value it cannot statically bound is rejected. The mask is
		 * safe because TX_TREE_MAX_NODES is a power of two.
		 */
		nd = &tree->node[idx & (TX_TREE_MAX_NODES - 1)];

		if (nd->left == TX_TREE_LEAF) {
			__u8 cls = nd->thresh;

			/* A leaf carries class in thresh and confidence in
			 * feat. A class outside the enum is a corrupt blob,
			 * and corrupt means irrevocable. */
			if (cls >= TX_CLASS_MAX)
				return TX_IRREVOCABLE;
			*conf = nd->feat;
			return (enum tx_class)cls;
		}

		fi  = nd->feat;
		thr = nd->thresh;
		if (fi >= TX_N_FEATURES)
			return TX_IRREVOCABLE;

		fv = f->f[fi & (TX_N_FEATURES - 1)];
		idx = (fv <= thr) ? nd->left : nd->right;

		if (idx >= n_nodes)
			return TX_IRREVOCABLE;	/* blob points off the end */
	}

	/*
	 * Ran out of depth without reaching a leaf. The exporter asserts the
	 * tree fits in TX_TREE_MAX_DEPTH, so this means the blob and the
	 * contract disagree -- fail closed rather than guess.
	 */
	return TX_IRREVOCABLE;
}

#endif /* _AGENTTX_INFER_H */
