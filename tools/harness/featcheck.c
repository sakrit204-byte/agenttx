// SPDX-License-Identifier: GPL-2.0
/*
 * tools/harness/featcheck.c --- compile the hook's feature encoder in
 * userspace so tests/p4/t07_infer.sh can compare it against features.py.
 *
 * src/policy/tx_features.h is the ONE implementation; the BPF program and
 * this binary are two compilations of it. That is what makes the comparison
 * meaningful: if this agrees with features.py, the hook agrees with
 * features.py, because it is the same code.
 *
 *   featcheck open <kernel-open-flags>   -> the feature byte
 *   featcheck msg  <kernel-msg-flags>    -> the feature byte
 *   featcheck vec  <16 scalars>          -> the whole vector
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "tx_features.h"

int main(int argc, char **argv)
{
	if (argc >= 3 && !strcmp(argv[1], "open")) {
		printf("%u\n", tx_open_flags_feat((unsigned)strtoul(argv[2], NULL, 0)));
		return 0;
	}
	if (argc >= 3 && !strcmp(argv[1], "msg")) {
		printf("%u\n", tx_msg_flags_feat((unsigned)strtoul(argv[2], NULL, 0)));
		return 0;
	}
	if (argc >= 2 && !strcmp(argv[1], "vec")) {
		struct tx_feat_in in = {};
		struct tx_features out;
		int i;

		if (argc < 14) {
			fprintf(stderr, "featcheck vec needs 12 scalars\n");
			return 2;
		}
		in.syscall_nr        = (unsigned)strtoul(argv[2], NULL, 0);
		in.hook              = (unsigned char)strtoul(argv[3], NULL, 0);
		in.path_hash         = strtoull(argv[4], NULL, 0);
		in.path_depth        = (unsigned)strtoul(argv[5], NULL, 0);
		in.path_is_dot       = (unsigned char)strtoul(argv[6], NULL, 0);
		in.fd_type           = (unsigned char)strtoul(argv[7], NULL, 0);
		in.open_flags_kernel = (unsigned)strtoul(argv[8], NULL, 0);
		in.dport             = (unsigned short)strtoul(argv[9], NULL, 0);
		in.family            = (unsigned short)strtoul(argv[10], NULL, 0);
		in.is_loopback       = (unsigned char)strtoul(argv[11], NULL, 0);
		in.in_tx             = (unsigned char)strtoul(argv[12], NULL, 0);
		in.msg_flags_kernel  = (unsigned)strtoul(argv[13], NULL, 0);

		tx_features_extract(&in, &out);
		for (i = 0; i < TX_N_FEATURES; i++)
			printf("%u%s", out.f[i], i + 1 == TX_N_FEATURES ? "\n" : " ");
		return 0;
	}
	fprintf(stderr, "usage: featcheck open|msg <flags> | vec <12 scalars>\n");
	return 2;
}
