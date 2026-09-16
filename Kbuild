# SPDX-License-Identifier: GPL-2.0
#
# Out-of-tree Kbuild for agenttx.ko.
#
# SHARED FILE -- contract-change PRs only.  Adding your own .o to your own
# stream's list is the one edit that does not need four approvals; changing
# the structure does.
#
# The whole point of the two lists below is WORKFLOW.md Rule 2: with
# STUB=1 the four provider objects come from src/stub/, with STUB=0 they
# come from their owners' directories.  Same symbols, same link, different
# implementation.  Nothing in src/core/ knows which it got.

obj-m += agenttx.o

ccflags-y += -I$(src)/include
ccflags-y += -Wall -Wextra -Wno-unused-parameter
ccflags-y += $(EXTRA_CFLAGS_STUB)

# --- P1: transaction core.  Always real; the core is never stubbed out
#     of the module because the module *is* the core. ------------------
agenttx-y += src/core/main.o
agenttx-y += src/core/ioctl.o
agenttx-y += src/core/ctx.o
agenttx-y += src/core/state.o
agenttx-y += src/core/commit.o
agenttx-y += src/core/exit.o

# ---------------------------------------------------------------------
# Provider selection, PER PROVIDER.
#
# STUB=1 still means "fake everything" and STUB=0 "the real thing", but each
# provider can now be overridden independently:
#
#   make STUB=1                       everything faked (the week-3 default)
#   make STUB=1 STUB_FS=0             P2 real, P3 and P4 still faked
#   make                              everything real (Friday integration)
#
# Why this was needed: the all-or-nothing switch meant the first stream to
# finish could not be tested against a real kernel until ALL FOUR had, which
# is exactly the serialisation WORKFLOW.md Rule 2 exists to prevent. A
# provider that is done should be testable the day it is done.
#
# This is a structural change to a change-controlled file and wants the four
# approvals. The per-stream .o lists below are unchanged.
# ---------------------------------------------------------------------
STUB_FS       ?= $(STUB)
STUB_EFF      ?= $(STUB)
STUB_CLASSIFY ?= $(STUB)

# --- P2: copy-on-write storage --------------------------------------
ifeq ($(STUB_FS),1)
agenttx-y += src/stub/tx_fs_stub.o
else
agenttx-y += src/fs/mount.o
agenttx-y += src/fs/abort.o
agenttx-y += src/fs/commit.o
agenttx-y += src/fs/writeset.o
agenttx-y += src/fs/extmod.o
endif

# --- P3: effect interception ----------------------------------------
# The BPF programs live in src/bpf/ and are loaded from userspace; what
# lands in the module is the kernel-side half that owns the ring and the
# flush path.
ifeq ($(STUB_EFF),1)
agenttx-y += src/stub/tx_eff_stub.o
else
agenttx-y += src/bpf/wal_kern.o
agenttx-y += src/bpf/flush_kern.o
endif

# --- P4: kernel-side inference --------------------------------------
# The forward pass itself is a BPF program (src/policy/infer.bpf.c); this is
# the in-module fallback and the rule-table baseline both hooks call through.
ifeq ($(STUB_CLASSIFY),1)
agenttx-y += src/stub/tx_classify_stub.o
else
agenttx-y += src/policy/classify.o
endif

# tx_core_stub is NOT linked here: src/core/ is present, so tx_current_id()
# is real. The core stub exists for P3's standalone BPF tests, which link it
# on its own.

# --- P1-10: the kfunc that exports tx_current_id() to BPF ------------
# Needs BTF, so it is conditional on the kernel having been built with
# CONFIG_DEBUG_INFO_BTF=y.  tools/vm/doctor.sh checks that.
agenttx-y += src/core/kfunc.o
