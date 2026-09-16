#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p4/t06_sabotage.sh --- does t06_weights.sh actually detect anything?
#
# WORKFLOW.md section 5, item 7:
#
#   "Does the test actually fail if you break the code?  Reviewer sabotages
#    one line and re-runs.  A test that passes on broken code is worse than
#    no test."
#
# This automates that for the P4-09 blob seam, because the answer turned out
# to be "no" the first two times it was asked:
#
#   - the original test compared a C reader against a second Python reader.
#     Both used the correct struct layout, so a wrongly-encoded blob was
#     misread identically by both and they agreed.  Fixed by comparing
#     against the exporter's declared intent instead.
#   - the intent then carried plain SUMS of the weight arrays.  A sum does
#     not change when you permute the elements, so transposing w1 -- 481
#     changed bytes, every weight in the wrong place -- still passed.
#     Fixed by using FNV-1a walked in struct index order.
#
# Note the NO-OP check below.  Two of the three sabotages originally tried
# produced byte-identical blobs: swapping two same-width header fields in a
# struct format string packs the same bytes, and round-tripping small int32
# values through int16 loses nothing.  Demanding a failure from an unchanged
# file would have "proved" the test worked while proving nothing at all.  So
# each sabotage is only held to account if it actually changed the blob.

set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

PY="${PY:-python3}"
EW="tools/harness/export_weights.py"
REF="data/model/model.bin"
SAB="data/model/_sabotage.bin"
BAK="data/model/_export_weights.bak"

if [[ ! -f "$REF" ]]; then
	echo "t06_sabotage: no reference blob -- run: make pipeline" >&2
	exit 77
fi

cp "$EW" "$BAK"
restore() {
	cp "$BAK" "$EW"
	rm -f "$BAK" "$SAB" "$SAB.meta.json"
}
trap restore EXIT

echo "t06_sabotage: breaking the exporter on purpose"
echo

pass=0 miss=0 noop=0

# ---------------------------------------------------------------------
# CONTROL CASE.  Run the UNMODIFIED exporter first and require that it
# reproduces the reference blob byte for byte.
#
# Without this the harness reports a confident false PASS: if the exporter
# cannot run at all -- $PY missing numpy is the way this actually happened
# -- then every sabotage produces no blob, every one is scored "caught by
# export_weights.py itself", and the harness announces that ten breakages
# were detected when in truth nothing was ever exported.  A test harness
# that cannot tell "the bug was caught" from "nothing ran" is exactly the
# failure it exists to prevent.
# ---------------------------------------------------------------------
rm -f "$SAB" "$SAB.meta.json"
if ! "$PY" "$EW" --model data/model --out "$SAB" --kind mlp >/dev/null 2>&1 \
   || [[ ! -f "$SAB" ]]; then
	echo "t06_sabotage: CONTROL FAILED -- the unmodified exporter produced no blob." >&2
	echo "  Every sabotage would be scored as 'caught' and the harness would" >&2
	echo "  report a false PASS. Diagnose with:" >&2
	echo "    $PY $EW --model data/model --out /tmp/x.bin --kind mlp" >&2
	echo "  (usually: this python has no numpy -- apt install python3-numpy)" >&2
	# 77 = skip. The exporter being unrunnable here is an environment
	# limitation, not a defect in the code under test, and a runner that
	# shows red for a missing optional package teaches people to ignore
	# it. A STALE REFERENCE below is a different matter and does fail.
	exit 77
fi
if ! cmp -s "$SAB" "$REF"; then
	echo "t06_sabotage: CONTROL FAILED -- unmodified exporter does not reproduce" >&2
	echo "  $REF. The reference is stale; re-run: make pipeline" >&2
	exit 1
fi
echo "  control: unmodified exporter reproduces the reference blob"
echo

# sabotage <description> <python-expression-editing-s>
sabotage() {
	local desc=$1 edit=$2

	cp "$BAK" "$EW"
	if ! "$PY" -c "
import pathlib, sys
p = pathlib.Path('$EW'); s = p.read_text(encoding='utf-8')
before = s
$edit
if s == before:
    sys.exit('edit matched nothing')
p.write_text(s, encoding='utf-8', newline='\n')
" 2>/dev/null; then
		printf '  %-42s %s\n' "$desc" "SKIP (edit did not apply)"
		return
	fi

	rm -f "$SAB" "$SAB.meta.json"
	"$PY" "$EW" --model data/model --out "$SAB" --kind mlp >/dev/null 2>&1

	if [[ ! -f "$SAB" ]]; then
		# The exporter's own size assertions caught it. That counts:
		# the bad blob never reaches the loader.
		printf '  %-42s %s\n' "$desc" "caught by export_weights.py itself"
		pass=$((pass+1))
		return
	fi

	if cmp -s "$SAB" "$REF"; then
		printf '  %-42s %s\n' "$desc" "no-op (byte-identical blob)"
		noop=$((noop+1))
		return
	fi

	if bash tests/p4/t06_weights.sh "$SAB" /dev/null >/dev/null 2>&1; then
		printf '  %-42s %s\n' "$desc" "*** MISSED ***"
		miss=$((miss+1))
	else
		printf '  %-42s %s\n' "$desc" "detected"
		pass=$((pass+1))
	fi
}

sabotage "w1 transposed (row/col swap)" \
	"s = s.replace('W1q.astype(\"<i1\").tobytes(order=\"C\")', 'W1q.astype(\"<i1\").tobytes(order=\"F\")')"

sabotage "w2 transposed" \
	"s = s.replace('W2q.astype(\"<i1\").tobytes(order=\"C\")', 'W2q.astype(\"<i1\").tobytes(order=\"F\")')"

sabotage "b1 and w2 written in swapped order" \
	"s = s.replace('''    blob += b1q.astype(\"<i4\").tobytes(order=\"C\")
    blob += W2q.astype(\"<i1\").tobytes(order=\"C\")''', '''    blob += W2q.astype(\"<i1\").tobytes(order=\"C\")
    blob += b1q.astype(\"<i4\").tobytes(order=\"C\")''')"

sabotage "b1 written big-endian" \
	"s = s.replace('b1q.astype(\"<i4\")', 'b1q.astype(\">i4\")')"

sabotage "h_shift off by one" \
	"s = s.replace('blob += struct.pack(\"<i\", h_shift)', 'blob += struct.pack(\"<i\", h_shift + 1)')"

sabotage "trained_rows dropped from header" \
	"s = s.replace('pack_hdr(TX_MODEL_MLP, 0, 0, trained_rows, acc_pct)', 'pack_hdr(TX_MODEL_MLP, 0, 0, 0, acc_pct)')"

sabotage "n_features mis-stated as 8" \
	"s = s.replace('TX_N_FEATURES, TX_CLASS_MAX, 0, n_nodes,', '8, TX_CLASS_MAX, 0, n_nodes,')"

sabotage "one weight byte flipped" \
	"s = s.replace('    blob += W1q.astype(\"<i1\").tobytes(order=\"C\")', '''    _w = bytearray(W1q.astype(\"<i1\").tobytes(order=\"C\"))
    _w[7] = (_w[7] + 1) & 0xff
    blob += bytes(_w)''')"

sabotage "header field order swapped (same widths)" \
	"s = s.replace('HDR_FMT = \"<IHBBBBHiII\"', 'HDR_FMT = \"<IHBBBBHIiI\"')"

sabotage "b1 round-tripped through int16" \
	"s = s.replace('b1q.astype(\"<i4\")', 'b1q.astype(\"<i2\").astype(\"<i4\")')"

echo
echo "  detected $pass, missed $miss, no-ops $noop"
if (( miss )); then
	echo
	echo "t06_sabotage: FAIL -- t06_weights.sh passed on $miss broken blob(s)."
	echo "  A test that passes on broken code is worse than no test."
	exit 1
fi
echo "t06_sabotage: PASS"
