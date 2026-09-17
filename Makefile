# SPDX-License-Identifier: GPL-2.0
#
# AgentTx top-level Makefile.
#
# SHARED FILE.  Like include/agenttx.h this is change-controlled: a PR that
# touches it is labelled `contract-change` and needs all four approvals.
# Everything else lives under a single owner's directory.
#
# The two build modes of WORKFLOW.md Rule 2:
#
#   make STUB=1 STUB_FS=0   P2 real, the rest faked (test one stream alone)
#   make STUB=1        every provider faked; the whole system loads and the
#                      milestone demo runs.  This is the week-3 default and
#                      any one person can run it alone.
#   make               the real thing; Friday integration day.
#
# Quick reference:
#   make help          this list
#   make doctor        check the toolchain before you waste an hour
#   make module        build agenttx.ko
#   make bpf           build the BPF objects
#   make headers-check verify include/agenttx.h is self-contained  (P1-01)
#   make txctl         build the userspace ioctl client
#   make test-stub     load the all-stub build and run the smoke tests
#   make tracker       regenerate tracker/master.csv for the weekly review
#   make synth         generate a synthetic trace corpus              (P4-04)
#   make train         train the float baseline classifier            (P4-07)
#   make quantize      int8 quantise and report the accuracy delta    (P4-08)
#   make weights       export the BPF model blob                      (P4-09)
#   make model-kernel  train+export the model the KERNEL runs        (P4-10)
#   make pipeline      synth -> train -> quantize -> weights, end to end
#   make bench         run every bench_*.sh into results/             (P4-11)
#   make vm-kernel     build the guest kernel                         (P*-00)
#   make vm-rootfs     build the guest rootfs
#   make vm-boot       boot the guest under QEMU/KVM
#   make vm-gdb        attach gdb to a running guest

SHELL      := /bin/bash
TOPDIR     := $(CURDIR)
PY         ?= python3

# Where the guest kernel source lives.  Overridable because it must NOT be
# on a 9p/DrvFs mount: a kernel build on /mnt/c is roughly ten times slower
# than the same build on ext4.  See docs/SETUP.md.
KDIR       ?= $(HOME)/src/linux
KBUILD_OUT ?= $(HOME)/build/linux

DATA       := $(TOPDIR)/data
RESULTS    := $(TOPDIR)/results

# STUB=1 selects the faked providers.  It is passed down to Kbuild as a
# plain -D rather than a real Kconfig symbol, because we build out of tree.
STUB       ?= 0
ifeq ($(STUB),1)
  EXTRA_CFLAGS_STUB := -DCONFIG_AGENTTX_STUB=1
  BUILD_MODE := stub
else
  EXTRA_CFLAGS_STUB :=
  BUILD_MODE := real
endif
# Per-provider overrides (see Kbuild). Unset means 'follow STUB'.
STUB_FS       ?= $(STUB)
STUB_EFF      ?= $(STUB)
STUB_CLASSIFY ?= $(STUB)
export EXTRA_CFLAGS_STUB STUB STUB_FS STUB_EFF STUB_CLASSIFY

.PHONY: help
help:
	@sed -n 's/^#   //p' $(MAKEFILE_LIST) | sed -n '1,40p'

# ---------------------------------------------------------------------
# Toolchain sanity.  Run this first, every time you move machines.
# ---------------------------------------------------------------------
.PHONY: doctor
doctor:
	@bash tools/vm/doctor.sh

# ---------------------------------------------------------------------
# Kernel module
# ---------------------------------------------------------------------
.PHONY: module
module:
	@echo "  AGENTTX  module ($(BUILD_MODE) providers)"
	$(MAKE) -C $(KBUILD_OUT) M=$(TOPDIR) modules

.PHONY: modules_install
modules_install:
	$(MAKE) -C $(KBUILD_OUT) M=$(TOPDIR) modules_install

# ---------------------------------------------------------------------
# BPF objects (P3, P4).  Owned by P3's Makefile; this just delegates.
# ---------------------------------------------------------------------
.PHONY: bpf
bpf:
	$(MAKE) -C src/bpf

# ---------------------------------------------------------------------
# Contract check (P1-01).  The header must compile standalone in all three
# of its dialects.  If this breaks, somebody added an unguarded include.
# ---------------------------------------------------------------------
.PHONY: headers-check
headers-check:
	@bash tools/vm/headers-check.sh

# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------
# The userspace client the P1 tests drive, and the base of P4-03's harness.
.PHONY: txctl
txctl: tools/harness/txctl
tools/harness/txctl: tools/harness/txctl.c include/agenttx.h
	$(CC) -Wall -Wextra -Werror -I include -o $@ $<

# The hook's feature encoder, compiled for userspace so the cross-check test
# can compare it against features.py.  Same header, two compilations.
.PHONY: featcheck
featcheck: tools/harness/featcheck
tools/harness/featcheck: tools/harness/featcheck.c src/policy/tx_features.h include/agenttx.h
	$(CC) -Wall -Wextra -Werror -I include -I src/policy -o $@ $<

.PHONY: test-stub
test-stub: txctl featcheck
	$(MAKE) STUB=1 module
	@bash tests/run.sh --stub

.PHONY: test
test:
	@bash tests/run.sh

# ---------------------------------------------------------------------
# Tracker aggregate (WORKFLOW.md section 2).  Generated, never edited,
# gitignored.  Each owner edits only their own CSV.
# ---------------------------------------------------------------------
.PHONY: tracker
tracker:
	@head -1 tracker/P1.csv > tracker/master.csv
	@for f in tracker/P1.csv tracker/P2.csv tracker/P3.csv tracker/P4.csv; do \
		tail -n +2 $$f >> tracker/master.csv; \
	done
	@echo "tracker/master.csv: $$(( $$(wc -l < tracker/master.csv) - 1 )) fragments"
	@$(PY) -c "import csv,collections,sys; \
rows=list(csv.DictReader(open('tracker/master.csv',newline=''))); \
c=collections.Counter(r['status'] for r in rows); \
[print('  %-12s %d' % (k,v)) for k,v in sorted(c.items())]"
# NOTE: a real CSV parser, not awk -F,.  depends_on legitimately contains a
# comma (P1-17 depends on two fragments), and awk counted the quoted field
# as a column break -- which showed up as a fragment whose status was '4'.

# ---------------------------------------------------------------------
# P4 userspace pipeline.  This entire section runs on any machine with
# python3 -- no kernel, no VM, no root.  It is the part of the project
# that can be built and validated before the Linux host exists.
# ---------------------------------------------------------------------
.PHONY: synth
synth:
	$(PY) tools/harness/synth.py --out $(DATA)/traces/synth.jsonl --n 20000 --seed 1

.PHONY: features
features:
	$(PY) tools/harness/features.py --in $(DATA)/traces/synth.jsonl \
		--out $(DATA)/traces/features.npz

.PHONY: train
train: features
	$(PY) tools/harness/train.py --in $(DATA)/traces/features.npz \
		--out $(DATA)/model

.PHONY: quantize
quantize:
	$(PY) tools/harness/quantize.py --model $(DATA)/model \
		--features $(DATA)/traces/features.npz --out $(DATA)/model

# The model the KERNEL actually runs.
#
# Trained with --kernel-only, which zeroes the features an LSM hook cannot
# supply (syscall_nr, ngram_0, ngram_1) so the tree cannot learn to depend on
# them. Without this the tree put 55% of its decision nodes on features the
# hook feeds a constant 0, and every real send classified `reversible`.
.PHONY: model-kernel
model-kernel:
	$(PY) tools/harness/features.py --in $(DATA)/traces/synth.jsonl \
		--out $(DATA)/traces/features_kernel.npz --kernel-only
	$(PY) tools/harness/train.py --in $(DATA)/traces/features_kernel.npz \
		--out $(DATA)/model_kernel
	$(PY) tools/harness/export_weights.py --model $(DATA)/model_kernel \
		--kind tree --out $(DATA)/model/model_tree_kernel.bin
	@echo
	@echo "kernel model -> $(DATA)/model/model_tree_kernel.bin"
	@echo "load with: txload --model data/model/model_tree_kernel.bin"

.PHONY: weights
weights:
	$(PY) tools/harness/export_weights.py --model $(DATA)/model \
		--out $(DATA)/model/model.bin

.PHONY: pipeline
pipeline: synth train quantize weights
	@echo
	@echo "pipeline complete -> $(DATA)/model/model.bin"
	@$(PY) tools/harness/export_weights.py --verify $(DATA)/model/model.bin

# ---------------------------------------------------------------------
# Benchmarks (P4-11).  P1..P3 each ship tools/bench/bench_*.sh emitting
# P4's CSV format.  The runner enforces the format; a bench that does not
# conform is a failure, not a warning.
# ---------------------------------------------------------------------
.PHONY: txbench
txbench: tools/bench/txbench
tools/bench/txbench: tools/bench/txbench.c include/agenttx.h
	$(CC) -O2 -Wall -Wextra -Werror -I include -o $@ $<

.PHONY: bench
bench: txbench
	@mkdir -p $(RESULTS)
	$(PY) tools/bench/run.py --all --out $(RESULTS)

.PHONY: bench-core bench-fs bench-hooks
bench-core:  ; @bash tools/bench/bench_core.sh
bench-fs:    ; @bash tools/bench/bench_fs.sh
bench-hooks: ; @bash tools/bench/bench_hooks.sh

# ---------------------------------------------------------------------
# Concurrency and deadlock (docs/deadlock.md).  Pure userspace model --
# nothing in src/ implements a wait-for graph yet, and every event the
# simulator emits carries sim:true so its output cannot be mistaken for a
# kernel measurement.
# ---------------------------------------------------------------------
.PHONY: deadlock deadlock-compare deadlock-demo
deadlock:
	$(PY) tools/harness/deadlock.py --all --explain

deadlock-compare:
	$(PY) tools/harness/deadlock.py --compare-policies

# What to run in front of an audience: the four scenarios, narrated, then
# the policy table that shows DOOMED removing the choice.
deadlock-demo:
	@$(PY) tools/harness/deadlock.py --all --explain
	@$(PY) tools/harness/deadlock.py --compare-policies
	@mkdir -p $(DATA)/deadlock
	@$(PY) tools/harness/deadlock.py --all --jsonl $(DATA)/deadlock/events.jsonl >/dev/null
	@echo
	@echo "events -> $(DATA)/deadlock/events.jsonl  (the dashboard renders these)"

.PHONY: figures
figures:
	$(PY) tools/bench/plot.py --in $(RESULTS) --out paper/figs

# ---------------------------------------------------------------------
# VM lifecycle.  Identical for all four people (WORKFLOW.md section 7).
# ---------------------------------------------------------------------
.PHONY: vm-kernel vm-rootfs vm-boot vm-gdb vm-clean
vm-kernel: ; @bash tools/vm/build-kernel.sh
vm-rootfs: ; @bash tools/vm/build-rootfs.sh
vm-boot:   ; @bash tools/vm/run-vm.sh
vm-gdb:    ; @bash tools/vm/gdb-attach.sh
vm-clean:  ; rm -rf tools/vm/build

# ---------------------------------------------------------------------
.PHONY: clean
clean:
	-$(MAKE) -C $(KBUILD_OUT) M=$(TOPDIR) clean 2>/dev/null
	-$(MAKE) -C src/bpf clean 2>/dev/null
	rm -f tracker/master.csv
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +

.PHONY: distclean
distclean: clean vm-clean
	rm -rf $(DATA)/traces $(DATA)/model $(RESULTS)
