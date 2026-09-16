/* SPDX-License-Identifier: GPL-2.0 */
/*
 * src/policy/tx_features.h --- the feature vector, computed once.
 *
 * NAMED tx_features.h, NOT features.h. <features.h> is a glibc header,
 * pulled in by libc-header-start.h from essentially every libc include. The
 * moment src/policy/ is on the include path, a file called features.h here
 * shadows it, and <stdio.h> starts parsing our C as if it were glibc's
 * feature-test macros. The error blames sys/ioctl.h. Do not rename it back.
 *
 * ONE IMPLEMENTATION, THREE COMPILATIONS.
 *
 *   src/bpf/agenttx.bpf.c   the hook, at classification time
 *   tools/harness/featcheck userspace, for the cross-check test
 *   (tools/harness/features.py is the fourth, in Python, and
 *    tests/p4/t07_infer.sh pins the two together)
 *
 * `features.py` says "Mirror this exactly in infer.bpf.c." Mirroring by hand
 * in a second language is how train/serve skew happens, so the C side exists
 * exactly once, here, and is compiled into both places.
 *
 * ============================================================
 * THE TRAP THIS HEADER EXISTS TO CLOSE
 * ============================================================
 *
 * features.py encodes open_flags and msg_flags into ITS OWN bit layout, and
 * its comment claims "The hook has these as bits of `flags` already, so this
 * is a mask, not a parse." That is false. Measured on this kernel:
 *
 *     flag            kernel    features.py
 *     O_CREAT         0x40      0x04
 *     O_TRUNC         0x200     0x08
 *     O_APPEND        0x400     0x10
 *     O_EXCL          0x80      0x20
 *     O_NOFOLLOW      0x20000   0x40
 *     MSG_DONTWAIT    0x40      0x08
 *     MSG_MORE        0x8000    0x10
 *     MSG_NOSIGNAL    0x4000    0x20
 *     MSG_CONFIRM     0x800     0x40
 *
 * 6 of 8 open flags and 4 of 8 message flags differ. A hook that masked the
 * raw kernel value -- which is what that comment instructs -- would report
 * MSG_CONFIRM every time the agent set MSG_DONTWAIT, because kernel
 * MSG_DONTWAIT (0x40) is features.py's MSG_CONFIRM (0x40).
 *
 * MSG_DONTWAIT is the feature docs/trace-format.md calls "the f-and-f
 * signal". It would have been systematically inverted, the model would have
 * trained on it happily, and nothing would have failed. So the remap is
 * explicit below and the test compares against features.py on real records.
 */

#ifndef _AGENTTX_TX_FEATURES_H
#define _AGENTTX_TX_FEATURES_H

#include "agenttx.h"

/* features.py's own encoding.  NOT the kernel's.  See above. */
#define TXF_O_WRONLY	0x01u
#define TXF_O_RDWR	0x02u
#define TXF_O_CREAT	0x04u
#define TXF_O_TRUNC	0x08u
#define TXF_O_APPEND	0x10u
#define TXF_O_EXCL	0x20u
#define TXF_O_NOFOLLOW	0x40u

#define TXF_MSG_OOB	0x01u
#define TXF_MSG_PEEK	0x02u
#define TXF_MSG_DONTROUTE 0x04u
#define TXF_MSG_DONTWAIT 0x08u
#define TXF_MSG_MORE	0x10u
#define TXF_MSG_NOSIGNAL 0x20u
#define TXF_MSG_CONFIRM	0x40u
#define TXF_MSG_EOR	0x80u

/* The kernel's values, restated because vmlinux.h carries types not defines
 * and because being explicit is the entire point of this file. */
#define TXK_O_WRONLY	0x1u
#define TXK_O_RDWR	0x2u
#define TXK_O_CREAT	0x40u
#define TXK_O_TRUNC	0x200u
#define TXK_O_APPEND	0x400u
#define TXK_O_EXCL	0x80u
#define TXK_O_NOFOLLOW	0x20000u

#define TXK_MSG_OOB	0x1u
#define TXK_MSG_PEEK	0x2u
#define TXK_MSG_DONTROUTE 0x4u
#define TXK_MSG_DONTWAIT 0x40u
#define TXK_MSG_MORE	0x8000u
#define TXK_MSG_NOSIGNAL 0x4000u
#define TXK_MSG_CONFIRM	0x800u
#define TXK_MSG_EOR	0x80u

#ifndef __tx_inline
# ifdef __TX_BPF__
#  define __tx_inline static __always_inline
# else
#  define __tx_inline static inline
# endif
#endif

__tx_inline __u8 tx_clamp8(__s64 v)
{
	if (v < 0)
		return 0;
	if (v > 255)
		return 255;
	return (__u8)v;
}

/* kernel open flags -> features.py's byte */
__tx_inline __u8 tx_open_flags_feat(__u32 k)
{
	__u8 f = 0;

	if (k & TXK_O_WRONLY)	f |= TXF_O_WRONLY;
	if (k & TXK_O_RDWR)	f |= TXF_O_RDWR;
	if (k & TXK_O_CREAT)	f |= TXF_O_CREAT;
	if (k & TXK_O_TRUNC)	f |= TXF_O_TRUNC;
	if (k & TXK_O_APPEND)	f |= TXF_O_APPEND;
	if (k & TXK_O_EXCL)	f |= TXF_O_EXCL;
	if (k & TXK_O_NOFOLLOW)	f |= TXF_O_NOFOLLOW;
	return f;
}

/* kernel msg flags -> features.py's byte */
__tx_inline __u8 tx_msg_flags_feat(__u32 k)
{
	__u8 f = 0;

	if (k & TXK_MSG_OOB)		f |= TXF_MSG_OOB;
	if (k & TXK_MSG_PEEK)		f |= TXF_MSG_PEEK;
	if (k & TXK_MSG_DONTROUTE)	f |= TXF_MSG_DONTROUTE;
	if (k & TXK_MSG_DONTWAIT)	f |= TXF_MSG_DONTWAIT;
	if (k & TXK_MSG_MORE)		f |= TXF_MSG_MORE;
	if (k & TXK_MSG_NOSIGNAL)	f |= TXF_MSG_NOSIGNAL;
	if (k & TXK_MSG_CONFIRM)	f |= TXF_MSG_CONFIRM;
	if (k & TXK_MSG_EOR)		f |= TXF_MSG_EOR;
	return f;
}

/*
 * Everything the extraction needs, already reduced to scalars.
 *
 * Deliberately not kernel structs: the hook reads `struct file` and
 * `struct sock`, the checker reads JSON, and the moment this function knows
 * which one it is talking to it stops being testable outside the kernel.
 */
struct tx_feat_in {
	__u32	syscall_nr;
	__u8	hook;			/* enum tx_hook                     */
	__u64	path_hash;		/* 0 when not a path                */
	__u32	path_depth;
	__u8	path_is_dot;
	__u8	fd_type;		/* S_IFMT >> 12                     */
	__u32	open_flags_kernel;	/* raw; remapped below              */
	__u16	dport;			/* host byte order                  */
	__u16	family;			/* AF_*                             */
	__u8	is_loopback;
	__u8	in_tx;
	__u64	ngram_hash;		/* 0 when unavailable               */
	__u32	msg_flags_kernel;	/* raw; remapped below              */
};

__tx_inline void tx_features_extract(const struct tx_feat_in *in,
				     struct tx_features *out)
{
	__u32 nr = in->syscall_nr;
	int i;

	for (i = 0; i < TX_N_FEATURES; i++)
		out->f[i] = 0;

	/*
	 * Syscall numbers run past 255 on x86-64 (openat is 257), so fold
	 * rather than truncate: & 0xff alone would collide openat(257) with
	 * read(1), the single worst collision available in this set.
	 */
	out->f[TX_FEAT_SYSCALL_NR]  = tx_clamp8((nr & 0xFF) ^ (nr >> 8));
	out->f[TX_FEAT_HOOK_ID]     = in->hook;
	out->f[TX_FEAT_PATH_HASH_B0] = (__u8)(in->path_hash & 0xFF);
	out->f[TX_FEAT_PATH_HASH_B1] = (__u8)((in->path_hash >> 8) & 0xFF);
	out->f[TX_FEAT_PATH_DEPTH]  = tx_clamp8(in->path_depth);
	out->f[TX_FEAT_PATH_IS_DOT] = in->path_is_dot ? 1 : 0;
	out->f[TX_FEAT_FD_TYPE]     = in->fd_type;
	out->f[TX_FEAT_OPEN_FLAGS]  = tx_open_flags_feat(in->open_flags_kernel);
	out->f[TX_FEAT_DPORT_LO]    = (__u8)(in->dport & 0xFF);
	out->f[TX_FEAT_DPORT_HI]    = (__u8)((in->dport >> 8) & 0xFF);
	out->f[TX_FEAT_AF]          = (__u8)in->family;
	out->f[TX_FEAT_IS_LOOPBACK] = in->is_loopback ? 1 : 0;
	out->f[TX_FEAT_TX_DEPTH]    = in->in_tx ? 1 : 0;
	out->f[TX_FEAT_NGRAM_0]     = (__u8)(in->ngram_hash & 0xFF);
	out->f[TX_FEAT_NGRAM_1]     = (__u8)((in->ngram_hash >> 8) & 0xFF);
	out->f[TX_FEAT_MSG_FLAGS]   = tx_msg_flags_feat(in->msg_flags_kernel);
}

#endif /* _AGENTTX_TX_FEATURES_H */
