# SPDX-License-Identifier: GPL-2.0
#
# src/bpf/Makefile --- P3's BPF objects and loader.  Fragment P3-02.
#
#   make -C src/bpf            build everything
#   make -C src/bpf vmlinux.h  regenerate the CO-RE type header
#   make -C src/bpf clean
#
# TWO THINGS THAT SILENTLY PRODUCE A BROKEN BUILD
#
# 1. vmlinux.h must describe the GUEST kernel (6.12), not the host kernel.
#    They are different versions here, and a CO-RE program compiled against
#    the wrong one fails to load with a relocation error that names a field
#    rather than the mismatch.  So it is generated from $(KBUILD_OUT)/vmlinux
#    -- the kernel we actually built and boot -- and never from
#    /sys/kernel/btf/vmlinux on the host.
#
# 2. -g is not optional.  Without it clang emits no BTF for the program and
#    the verifier rejects it for reasons that mention neither BTF nor -g.
#
# vmlinux.h is gitignored: it is 3 MB and specific to one kernel build.
# Regenerate it after every `make vm-kernel`.

CLANG      ?= clang
BPFTOOL    ?= bpftool
KBUILD_OUT ?= $(HOME)/build/linux
ARCH       := x86
REPO       := ../..

BPF_CFLAGS := -g -O2 -target bpf -D__TARGET_ARCH_$(ARCH) -D__TX_BPF__ \
              -I. -I$(REPO)/include -Wall -Wno-unused-function

# Userspace loader.  Links libbpf dynamically so the same binary runs in the
# guest, which has the runtime but no headers and no compiler.
LOADER_CFLAGS := -g -O2 -Wall -Wextra -I. -I$(REPO)/include
LOADER_LDLIBS := -lbpf -lelf -lz

BPF_SRCS := agenttx.bpf.c
BPF_OBJS := $(BPF_SRCS:.bpf.c=.bpf.o)
SKELS    := $(BPF_SRCS:.bpf.c=.skel.h)

.PHONY: all clean
all: txload

vmlinux.h:
	@echo "  BTF     $@  (from $(KBUILD_OUT)/vmlinux -- the GUEST kernel)"
	@test -f $(KBUILD_OUT)/vmlinux || { \
		echo "src/bpf: no $(KBUILD_OUT)/vmlinux -- run: make vm-kernel" >&2; \
		exit 1; }
	@$(BPFTOOL) btf dump file $(KBUILD_OUT)/vmlinux format c > $@

# NOTE: no `bpftool gen object` step. It is for linking SEVERAL bpf objects
# into one, which we do not do -- and invoking it as `gen object $@ $@`
# reads and writes the same path, truncating the object to zero bytes. The
# resulting error ("failed to get ELF header") names the file rather than
# the in-place write, which cost a build cycle to see.
%.bpf.o: %.bpf.c vmlinux.h $(REPO)/include/agenttx.h
	@echo "  BPF     $@"
	@$(CLANG) $(BPF_CFLAGS) -c $< -o $@

%.skel.h: %.bpf.o
	@echo "  SKEL    $@"
	@$(BPFTOOL) gen skeleton $< > $@

txload: loader.c $(SKELS) $(REPO)/include/agenttx.h
	@echo "  CC      $@"
	@$(CC) $(LOADER_CFLAGS) -o $@ loader.c $(LOADER_LDLIBS)

clean:
	rm -f $(BPF_OBJS) $(SKELS) txload

# vmlinux.h is deliberately NOT removed by clean: regenerating it needs a
# built kernel, and `make clean` should not create a dependency on that.
.PHONY: distclean
distclean: clean
	rm -f vmlinux.h
