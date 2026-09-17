#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tools/vm/headers-check.sh --- `make headers-check`.  Fragment P1-01's test.
#
# include/agenttx.h is the contract freeze, and it is included from four
# dialects that share no common set of available headers:
#
#   userspace   <linux/types.h> and <sys/ioctl.h> exist
#   kernel      only <linux/*>; libc does not exist
#   BPF         neither; vmlinux.h has already defined the __u* types
#   C++         the harness may grow a C++ consumer
#
# The usual way this breaks is somebody adds an unguarded #include while
# working in one dialect, everything keeps building for them, and the BPF
# build snaps a week later with an error naming a file they never touched.
#
# This script builds the header in every dialect on the host, with no
# kernel tree required, so the check runs in CI and on a laptop.

set -uo pipefail

# No compiler, no contract check.  The guest rootfs deliberately has no
# build-essential -- the module is built on the host and shared in over 9p --
# so this must SKIP rather than FAIL there.  tests/run.sh says a test that
# cannot run here skips with a reason; exit 77 is that reason.
if ! command -v gcc >/dev/null 2>&1 && ! command -v cc >/dev/null 2>&1; then
	echo "headers-check: no C compiler here; skipping (build-tier check)" >&2
	exit 77
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HDR="$REPO/include/agenttx.h"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [[ -t 1 ]]; then R=$'\033[31m'; G=$'\033[32m'; N=$'\033[0m'; else R=; G=; N=; fi
fail=0
ok()  { printf '  %sPASS%s  %s\n' "$G" "$N" "$1"; }
bad() { printf '  %sFAIL%s  %s\n' "$R" "$N" "$1"; fail=$((fail+1)); }

CC=${CC:-gcc}
WARN="-Wall -Wextra -Werror -Wpadded"

echo "headers-check: $HDR"

# --- 1. userspace C, twice in one TU: the include guard must hold ---------
cat > "$TMP/user.c" <<'XEOF'
#include "agenttx.h"
#include "agenttx.h"
int main(void) { return 0; }
XEOF
if $CC $WARN -std=gnu11 -I"$REPO/include" "$TMP/user.c" -o "$TMP/user" 2>"$TMP/err"; then
	ok "userspace C (gnu11, -Wpadded, double include)"
else
	bad "userspace C"; sed 's/^/        /' "$TMP/err"
fi

# --- 2. strict C99: no GNU extensions leaked into the ABI -----------------
cat > "$TMP/c99.c" <<'XEOF'
#include "agenttx.h"
int main(void) { return 0; }
XEOF
if $CC -Wall -Wextra -Werror -std=c99 -I"$REPO/include" "$TMP/c99.c" -o "$TMP/c99" 2>"$TMP/err"; then
	ok "strict C99"
else
	bad "strict C99"; sed 's/^/        /' "$TMP/err"
fi

# --- 3. C++: the harness may grow a C++ consumer, and an enum used as an
#        int is legal in C and not in C++ ---------------------------------
if command -v g++ >/dev/null 2>&1; then
	cp "$TMP/c99.c" "$TMP/cxx.cc"
	if g++ -Wall -Wextra -Werror -std=c++17 -I"$REPO/include" "$TMP/cxx.cc" -o "$TMP/cxx" 2>"$TMP/err"; then
		ok "C++17"
	else
		bad "C++17"; sed 's/^/        /' "$TMP/err"
	fi
fi

# --- 4. BPF dialect.  __TX_BPF__ must make the header include nothing at
#        all: under -ffreestanding with no system include path, any stray
#        #include is a hard error. -----------------------------------------
cat > "$TMP/bpf.c" <<'XEOF'
/* Stand in for vmlinux.h, which supplies these before agenttx.h is read. */
typedef unsigned char      __u8;
typedef signed char        __s8;
typedef unsigned short     __u16;
typedef short              __s16;
typedef unsigned int       __u32;
typedef int                __s32;
typedef unsigned long long __u64;
typedef long long          __s64;

#define __TX_BPF__
#include "agenttx.h"

/* Touch the types the BPF programs actually use, so an unused-struct
   warning cannot hide a broken definition. */
struct tx_wal_rec      _r;
struct tx_features     _f;
struct tx_model_mlp    _m;
struct tx_model_tree   _t;
struct tx_compensable_key _k;
int _classes[TX_CLASS_MAX];
int _feats[TX_N_FEATURES];
XEOF
if $CC -Wall -Wextra -Werror -std=gnu11 -ffreestanding -nostdinc \
       -I"$REPO/include" -c "$TMP/bpf.c" -o "$TMP/bpf.o" 2>"$TMP/err"; then
	ok "BPF dialect (-nostdinc -ffreestanding)"
else
	bad "BPF dialect -- an unguarded #include reached the BPF path"
	sed 's/^/        /' "$TMP/err"
fi

# --- 5. clang too, if present: the BPF build uses clang, not gcc ----------
if command -v clang >/dev/null 2>&1; then
	if clang -Wall -Wextra -Werror -std=gnu11 -ffreestanding -nostdinc \
	         -I"$REPO/include" -c "$TMP/bpf.c" -o "$TMP/bpfc.o" 2>"$TMP/err"; then
		ok "BPF dialect under clang"
	else
		bad "BPF dialect under clang"; sed 's/^/        /' "$TMP/err"
	fi
fi

# --- 6. ABI invariants.  These are the ones a contract-change PR is most
#        likely to break by accident. -------------------------------------
cat > "$TMP/abi.c" <<'XEOF'
#include <stdio.h>
#include "agenttx.h"

#define CHECK(cond, msg) \
	do { if (!(cond)) { printf("        %s\n", msg); rc = 1; } } while (0)

int main(void)
{
	int rc = 0;
	static const char *cls[] = TX_CLASS_NAMES;
	static const char *st[]  = TX_STATE_NAMES;
	static const char *hk[]  = TX_HOOK_NAMES;
	static const char *vd[]  = TX_VERDICT_NAMES;

	/* Name tables must cover their enums exactly, or every log line in
	   the project can print garbage for a value nobody tested. */
	CHECK(sizeof(cls)/sizeof(*cls) == TX_CLASS_MAX, "TX_CLASS_NAMES != TX_CLASS_MAX");
	CHECK(sizeof(st)/sizeof(*st)   == TX_STATE_MAX, "TX_STATE_NAMES != TX_STATE_MAX");
	CHECK(sizeof(hk)/sizeof(*hk)   == TX_HOOK_MAX,  "TX_HOOK_NAMES != TX_HOOK_MAX");
	CHECK(sizeof(vd)/sizeof(*vd)   == TX_V_MAX,     "TX_VERDICT_NAMES != TX_V_MAX");

	/* Severity ascends: src/ takes max() over classes to track the
	   worst effect seen.  Reordering the enum silently inverts that. */
	CHECK(TX_REVERSIBLE < TX_DEFERRABLE &&
	      TX_DEFERRABLE < TX_COMPENSABLE &&
	      TX_COMPENSABLE < TX_IRREVOCABLE,
	      "tx_class is no longer ordered by ascending severity");

	/* The WAL record crosses the kernel/user boundary in a ring buffer.
	   8-byte alignment and a size that is a multiple of 8 keep it
	   readable from both sides without per-field fixups. */
	CHECK(sizeof(struct tx_wal_rec) % 8 == 0, "tx_wal_rec is not 8-byte-multiple sized");
	CHECK(_Alignof(struct tx_wal_rec) == 8,   "tx_wal_rec alignment is not 8");

	/* The model blob is copied into a BPF map value.  Keep it inside the
	   256 KiB single-value limit with room to spare. */
	CHECK(sizeof(struct tx_model_mlp)  < (256u << 10), "MLP blob too large for a BPF map value");
	CHECK(sizeof(struct tx_model_tree) < (256u << 10), "tree blob too large for a BPF map value");
	CHECK(sizeof(struct tx_tree_node) == 6, "tx_tree_node is no longer 6 bytes");

	/* Feature vector width is duplicated in features.py and infer.bpf.c. */
	CHECK(sizeof(struct tx_features) == TX_N_FEATURES, "tx_features is not TX_N_FEATURES bytes");
	CHECK(TX_FEAT_MSG_FLAGS == TX_N_FEATURES - 1, "feature enum does not fill the vector");

	/* ioctl direction bits: BEGIN/COMMIT/ABORT/STAT all read back. */
	CHECK(_IOC_DIR(TX_IOC_BEGIN)  == (_IOC_READ|_IOC_WRITE), "TX_IOC_BEGIN is not _IOWR");
	CHECK(_IOC_DIR(TX_IOC_COMMIT) == (_IOC_READ|_IOC_WRITE), "TX_IOC_COMMIT is not _IOWR");
	CHECK(_IOC_TYPE(TX_IOC_BEGIN) == AGENTTX_IOC_MAGIC, "ioctl magic drifted");

	/* --- wait-for graph (contract change: abi 2) --------------------- */
	{
		static const char *wt[] = TX_WAIT_NAMES;

		CHECK(sizeof(wt)/sizeof(*wt) == TX_WAIT_MAX, "TX_WAIT_NAMES != TX_WAIT_MAX");
	}
	/* The edge struct crosses the ioctl boundary; keep it fixup-free. */
	CHECK(sizeof(struct tx_wait_edge) % 8 == 0, "tx_wait_edge is not 8-byte-multiple sized");
	CHECK(_Alignof(struct tx_wait_edge) == 8,   "tx_wait_edge alignment is not 8");
	CHECK(_IOC_DIR(TX_IOC_WAIT) == (_IOC_READ|_IOC_WRITE), "TX_IOC_WAIT is not _IOWR");
	CHECK(_IOC_TYPE(TX_IOC_WAIT) == AGENTTX_IOC_MAGIC, "TX_IOC_WAIT magic drifted");
	CHECK(_IOC_NR(TX_IOC_WAIT) != _IOC_NR(TX_IOC_SUPERVISOR) &&
	      _IOC_NR(TX_IOC_WAIT) != _IOC_NR(TX_IOC_ABI) &&
	      _IOC_NR(TX_IOC_UNWAIT) != _IOC_NR(TX_IOC_WAIT),
	      "ioctl numbers collide");
	/* TX_REASON_DEADLOCK must be inside the range ioctl.c validates. */
	CHECK(TX_REASON_DEADLOCK < TX_REASON_MAX, "TX_REASON_DEADLOCK outside the enum");

	/* Fail-closed threshold must sit inside the confidence range, or
	   tx_class_final() becomes either a no-op or a permanent deny. */
	CHECK(TX_CONFIDENCE_MIN > 0 && TX_CONFIDENCE_MIN <= 255,
	      "TX_CONFIDENCE_MIN is outside 1..255");

	if (rc == 0)
		printf("        %zu B wal_rec, %zu B mlp, %zu B tree\n",
		       sizeof(struct tx_wal_rec), sizeof(struct tx_model_mlp),
		       sizeof(struct tx_model_tree));
	return rc;
}
XEOF
if $CC $WARN -std=gnu11 -I"$REPO/include" "$TMP/abi.c" -o "$TMP/abi" 2>"$TMP/err"; then
	if "$TMP/abi"; then
		ok "ABI invariants"
	else
		bad "ABI invariants"
	fi
else
	bad "ABI invariant program did not build"; sed 's/^/        /' "$TMP/err"
fi

echo
if (( fail == 0 )); then
	printf '%scontract OK%s\n' "$G" "$N"
else
	printf '%s%d check(s) failed%s -- this is a contract-change PR, get four approvals\n' \
		"$R" "$fail" "$N"
fi
exit "$fail"
