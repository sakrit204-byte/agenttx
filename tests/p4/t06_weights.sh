#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p4/t06_weights.sh --- test for fragment P4-09 (export weights blob).
#
# The point of this test is the SEAM, not the model.  export_weights.py
# packs bytes in Python; src/bpf/ reads them in C through `struct
# tx_model_mlp`.  Neither language checks the other, so a field written to
# the wrong offset produces a blob the loader parses into plausible-looking
# garbage -- wrong weights, no error, and a classifier that is simply worse
# for reasons nobody can find.
#
# TWO DESIGN DECISIONS, both arrived at by getting them wrong first:
#
# 1. Compare C against the exporter's DECLARED INTENT (the .meta.json
#    sidecar, computed from the source arrays before packing), not against
#    a second Python reader.  Two readers sharing the same struct
#    definition misread a wrongly-encoded blob in exactly the same way:
#    they agree with each other and with nothing real.
#
# 2. Use position-SENSITIVE digests, not sums.  A sum is invariant under
#    permutation, so a transposed w1 -- 481 changed bytes, every weight in
#    the wrong place -- yields an identical checksum and this test would
#    report PASS.  FNV-1a walked in struct index order catches it.
#
# Reviewer, per WORKFLOW.md section 5 item 7: sabotage it and re-run.
# tests/p4/t06_sabotage.sh does that for you and asserts each sabotage is
# detected.  Note that some plausible-looking edits are no-ops -- swapping
# two same-width header fields packs identical bytes -- so the harness
# checks the bytes actually changed before demanding a failure.  A test
# that "fails" on an unchanged file has proved nothing.

set -uo pipefail

if ! command -v gcc >/dev/null 2>&1 && ! command -v cc >/dev/null 2>&1; then
	echo "t06: no C compiler here; skipping (this test compiles a C reader)" >&2
	exit 77
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

PY="${PY:-python3}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

BLOB="${1:-data/model/model.bin}"
TREE_BLOB="${2:-data/model/model_tree.bin}"

if [[ ! -f "$BLOB" ]]; then
	echo "t06: no blob at $BLOB -- run: make pipeline" >&2
	exit 77   # skip, not fail: the pipeline has not been run yet
fi

cat > "$TMP/reader.c" <<'XEOF'
/* Reads a model blob through the real struct definitions and prints what C
   sees.  Compared against the exporter's intent sidecar by the shell above. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "agenttx.h"

#define FNV_OFF   14695981039346656037ULL
#define FNV_PRIME 1099511628211ULL

static unsigned long long fnv_u8(unsigned long long h, unsigned char b)
{
	return (h ^ (unsigned long long)b) * FNV_PRIME;
}

/* int32 fields are hashed little-endian, matching how they sit in the blob. */
static unsigned long long fnv_i32(unsigned long long h, int v)
{
	unsigned int u = (unsigned int)v;
	int k;

	for (k = 0; k < 4; k++)
		h = fnv_u8(h, (unsigned char)((u >> (8 * k)) & 0xffu));
	return h;
}

static long slurp(const char *path, void *buf, long cap)
{
	FILE *f = fopen(path, "rb");
	long n;

	if (!f) { perror(path); return -1; }
	n = (long)fread(buf, 1, (size_t)cap, f);
	fclose(f);
	return n;
}

int main(int argc, char **argv)
{
	static union { struct tx_model_mlp mlp; struct tx_model_tree tree; } m;
	const struct tx_model_hdr *h;
	long n;

	if (argc < 2) { fprintf(stderr, "usage: reader <blob>\n"); return 2; }
	memset(&m, 0, sizeof(m));

	n = slurp(argv[1], &m, (long)sizeof(m));
	if (n < (long)sizeof(struct tx_model_hdr)) {
		fprintf(stderr, "short read: %ld\n", n);
		return 1;
	}
	h = &m.mlp.hdr;

	printf("file_bytes=%ld\n", n);
	printf("magic=0x%08x\n", h->magic);
	printf("abi=%u\n", h->abi);
	printf("kind=%u\n", h->kind);
	printf("n_features=%u\n", h->n_features);
	printf("n_classes=%u\n", h->n_classes);
	printf("n_nodes=%u\n", h->n_nodes);
	printf("trained_rows=%u\n", h->trained_rows);
	printf("accuracy_pct=%u\n", h->accuracy_pct);
	printf("sizeof_hdr=%zu\n",  sizeof(struct tx_model_hdr));
	printf("sizeof_mlp=%zu\n",  sizeof(struct tx_model_mlp));
	printf("sizeof_tree=%zu\n", sizeof(struct tx_model_tree));
	printf("sizeof_node=%zu\n", sizeof(struct tx_tree_node));

	if (h->kind == TX_MODEL_MLP) {
		unsigned long long d;
		long i, j;

		printf("h_shift=%d\n", m.mlp.h_shift);

		d = FNV_OFF;
		for (i = 0; i < (long)TX_MLP_HIDDEN; i++)
			for (j = 0; j < (long)TX_N_FEATURES; j++)
				d = fnv_u8(d, (unsigned char)m.mlp.w1[i][j]);
		printf("fnv_w1=%llu\n", d);

		d = FNV_OFF;
		for (i = 0; i < (long)TX_MLP_HIDDEN; i++)
			d = fnv_i32(d, m.mlp.b1[i]);
		printf("fnv_b1=%llu\n", d);

		d = FNV_OFF;
		for (i = 0; i < (long)TX_CLASS_MAX; i++)
			for (j = 0; j < (long)TX_MLP_HIDDEN; j++)
				d = fnv_u8(d, (unsigned char)m.mlp.w2[i][j]);
		printf("fnv_w2=%llu\n", d);

		d = FNV_OFF;
		for (i = 0; i < (long)TX_CLASS_MAX; i++)
			d = fnv_i32(d, m.mlp.b2[i]);
		printf("fnv_b2=%llu\n", d);
	} else if (h->kind == TX_MODEL_TREE) {
		unsigned long long d = FNV_OFF;
		unsigned i, leaves = 0, bad = 0;

		for (i = 0; i < h->n_nodes; i++) {
			const struct tx_tree_node *nd = &m.tree.node[i];

			if (nd->left == TX_TREE_LEAF) {
				leaves++;
				if (nd->thresh >= TX_CLASS_MAX)
					bad++;
			} else if (nd->left >= h->n_nodes ||
				   nd->right >= h->n_nodes ||
				   nd->feat >= TX_N_FEATURES) {
				bad++;
			}
			d = fnv_u8(d, nd->feat);
			d = fnv_u8(d, nd->thresh);
			d = fnv_u8(d, (unsigned char)(nd->left & 0xffu));
			d = fnv_u8(d, (unsigned char)(nd->left >> 8));
			d = fnv_u8(d, (unsigned char)(nd->right & 0xffu));
			d = fnv_u8(d, (unsigned char)(nd->right >> 8));
		}
		printf("leaves=%u\n", leaves);
		printf("bad_nodes=%u\n", bad);
		printf("fnv_nodes=%llu\n", d);
	}
	return 0;
}
XEOF

CC="${CC:-gcc}"
if ! $CC -Wall -Wextra -Werror -std=gnu11 -I include "$TMP/reader.c" -o "$TMP/reader" 2>"$TMP/cc.err"; then
	echo "t06: FAIL -- the C reader did not build"
	sed 's/^/    /' "$TMP/cc.err"
	exit 1
fi

fail=0
check_one() {
	local blob=$1 kind=$2 meta
	[[ -f "$blob" ]] || { echo "  skip $kind: $blob absent"; return 0; }

	meta="$blob.meta.json"
	if [[ ! -f "$meta" ]]; then
		echo "  FAIL $kind: no intent sidecar at $meta (re-run export_weights.py)"
		fail=1; return
	fi

	if ! "$TMP/reader" "$blob" > "$TMP/c.raw"; then
		echo "  FAIL $kind: the C reader errored"
		fail=1; return
	fi

	# tr -d '\r' on BOTH sides. An interpreter that emits CRLF
	# otherwise produces a diff whose two halves are visibly identical
	# and still unequal -- a failure nobody can read.
	"$PY" -c 'import json,sys
for k, v in sorted(json.load(open(sys.argv[1])).items()):
    print("%s=%s" % (k, v))' "$meta" | tr -d '\r' | sort > "$TMP/py.out"

	# The reader prints a superset (fields meaningless for this kind);
	# compare only the keys the exporter actually declared.
	grep -f <(cut -d= -f1 "$TMP/py.out" | sed 's/^/^/; s/$/=/') "$TMP/c.raw" \
		| tr -d '\r' | sort > "$TMP/c.out"

	if diff -u "$TMP/py.out" "$TMP/c.out" > "$TMP/diff"; then
		echo "  PASS $kind ($(wc -l < "$TMP/c.out") declared fields match what C reads)"
	else
		echo "  FAIL $kind -- C reads values the exporter did not intend to write:"
		sed 's/^/    /' "$TMP/diff"
		fail=1
	fi
}

echo "t06_weights: exporter intent vs. what C actually reads"
check_one "$BLOB" "mlp"
check_one "$TREE_BLOB" "tree"

if (( fail )); then
	echo "t06: FAIL"
	echo "     export_weights.py and include/agenttx.h have diverged."
	echo "     This is a contract-change PR: four approvals, and update BOTH sides."
	exit 1
fi
echo "t06: PASS"
