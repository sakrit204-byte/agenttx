// SPDX-License-Identifier: GPL-2.0
/*
 * src/core/kfunc.c --- tx_current_id() as a BPF kfunc.  Fragment P1-10.
 *
 * P3 has been calling the stub since week 3 (it returns 1 unconditionally).
 * This file is the swap that makes the answer real, and by design it is the
 * ONLY thing that has to change for that swap: P3's hooks call the same
 * name with the same signature either way.  If landing this fragment
 * required edits in src/bpf/, the contract was drawn wrong.
 *
 * Requires CONFIG_DEBUG_INFO_BTF_MODULES=y -- the kernel has to be able to
 * see this module's BTF or the verifier cannot resolve the kfunc.  It is in
 * tools/vm/kernel-common.config for exactly this reason, and doctor.sh
 * checks it, because the failure is a verifier error that names the BPF
 * program rather than the missing config symbol.
 *
 * Owner: P1.
 */

#define pr_fmt(fmt) "agenttx: " fmt

#include <linux/bpf.h>
#include <linux/btf.h>
#include <linux/btf_ids.h>
#include <linux/module.h>
#include <linux/printk.h>

#include "core.h"

/*
 * kfuncs are not EXPORT_SYMBOL: they are resolved by the verifier through
 * BTF, and the __bpf_kfunc annotation is what stops the compiler from
 * inlining or eliding a function nothing in C calls.
 */
__bpf_kfunc_start_defs();

/*
 * Returns the current task's transaction id, or 0 (TX_ID_NONE) outside a
 * transaction.  The BPF side treats 0 as "not transacting" and skips the
 * rest of the hook, which is the common case and must stay cheap.
 */
__bpf_kfunc __u64 bpf_tx_current_id(void)
{
	return (__u64)tx_current_id();
}

/*
 * Exposed alongside the id because a hook needs both: an ACTIVE
 * transaction captures effects, a DOOMED one must not pretend it can still
 * defer them.  Returning the state as a plain u32 keeps the BPF side free
 * of any dependency on our enum's storage.
 */
__bpf_kfunc __u32 bpf_tx_current_state(void)
{
	return (__u32)tx_current_state();
}

/*
 * The write side of the contract: a hook that classified an effect raises
 * the transaction's watermark through here.  This is what can move a
 * transaction to DOOMED, so it is the one kfunc with a side effect, and it
 * is why tx_note_class() is careful about ordering.
 */
__bpf_kfunc __s32 bpf_tx_note_class(__u64 tx_id, __u32 klass)
{
	return (__s32)tx_note_class((tx_id_t)tx_id, (enum tx_class)klass);
}

__bpf_kfunc_end_defs();

BTF_KFUNCS_START(tx_kfunc_ids)
BTF_ID_FLAGS(func, bpf_tx_current_id)
BTF_ID_FLAGS(func, bpf_tx_current_state)
BTF_ID_FLAGS(func, bpf_tx_note_class)
BTF_KFUNCS_END(tx_kfunc_ids)

static const struct btf_kfunc_id_set tx_kfunc_set = {
	.owner = THIS_MODULE,
	.set   = &tx_kfunc_ids,
};

int tx_kfunc_register(void)
{
	int ret;

	/*
	 * Registered for BPF_PROG_TYPE_LSM specifically.  P3's hooks are LSM
	 * programs; registering for every program type would let an unrelated
	 * tracing program call into the transaction table, which is a larger
	 * attack surface than this needs.
	 */
	ret = register_btf_kfunc_id_set(BPF_PROG_TYPE_LSM, &tx_kfunc_set);
	if (ret) {
		pr_err("register_btf_kfunc_id_set failed: %d -- is CONFIG_DEBUG_INFO_BTF_MODULES=y?\n",
		       ret);
		return ret;
	}
	pr_info("kfuncs registered for BPF_PROG_TYPE_LSM\n");
	return 0;
}

/*
 * There is no unregister.  The BPF core ties the id set's lifetime to
 * .owner = THIS_MODULE and refuses to unload the module while a program
 * that references one of these kfuncs is loaded, which is the behaviour we
 * want: rmmod with a live hook attached would leave the verifier holding a
 * pointer into freed text.
 */
