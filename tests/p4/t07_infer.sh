#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-2.0
#
# tests/p4/t07_infer.sh --- fragment P4-10, the in-kernel forward pass.
#
# Tier 0: no kernel needed. This tests the two things that fail SILENTLY.
#
#   1. TRAIN/SERVE ENCODING SKEW. features.py encodes open_flags and
#      msg_flags into its own bit layout, which is not the kernel's. 6 of 8
#      open flags and 4 of 8 message flags differ. A hook that masked raw
#      kernel values would feed the model a different number than training
#      saw, for the same event, and nothing would fail -- the model would
#      just be wrong.
#
#      The worst case is exact: kernel MSG_DONTWAIT is 0x40, which is
#      features.py's MSG_CONFIRM. MSG_DONTWAIT is the feature
#      docs/trace-format.md calls "the f-and-f signal".
#
#   2. TRAIN/SERVE AVAILABILITY SKEW. A model may split on features the hook
#      cannot supply at all. Our first tree put 33 of 60 decision nodes (55%)
#      on syscall_nr and the syscall n-gram, neither of which an LSM hook
#      has. Every real send then classified `reversible`, because the walk
#      went through zeros to a fixed leaf. It looked like a working
#      classifier.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
PY="${PY:-python3}"
FC=tools/harness/featcheck

fails=0; n=0
if [[ -t 1 ]]; then R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'; else R=; G=; Y=; N=; fi
ok()   { n=$((n+1)); printf '  %sPASS%s  %-46s %s\n' "$G" "$N" "$1" "${2-}"; }
bad()  { n=$((n+1)); fails=$((fails+1)); printf '  %sFAIL%s  %-46s %s\n' "$R" "$N" "$1" "${2-}"; }
skip() { printf '  %sSKIP%s  %-46s %s\n' "$Y" "$N" "$1" "${2-}"; }

echo "t07_infer: feature parity and model shape (P4-10)"

if [[ ! -x "$FC" ]]; then
	cc -Wall -Wextra -I include -I src/policy -o "$FC" tools/harness/featcheck.c 2>/dev/null \
		|| { echo "  (cannot build featcheck)"; exit 77; }
fi

# --- 1. encoding parity, every single kernel bit ---------------------
"$PY" - "$FC" <<'PYEOF' > /tmp/featparity.out 2>&1
import subprocess, sys
sys.path.insert(0, "tools/harness")
import features as F
fc = sys.argv[1]

def c_side(kind, val):
    return int(subprocess.run([fc, kind, hex(val)], capture_output=True,
                              text=True).stdout.strip() or -1)

KERNEL_OPEN = {"O_WRONLY":0x1,"O_RDWR":0x2,"O_CREAT":0x40,"O_TRUNC":0x200,
               "O_APPEND":0x400,"O_EXCL":0x80,"O_NOFOLLOW":0x20000}
KERNEL_MSG  = {"MSG_OOB":0x1,"MSG_PEEK":0x2,"MSG_DONTROUTE":0x4,
               "MSG_DONTWAIT":0x40,"MSG_MORE":0x8000,"MSG_NOSIGNAL":0x4000,
               "MSG_CONFIRM":0x800,"MSG_EOR":0x80}

bad = 0
for name, kv in KERNEL_OPEN.items():
    want = F.OPEN_BIT[name]; got = c_side("open", kv)
    if want != got:
        print(f"MISMATCH open {name}: kernel 0x{kv:x} -> C {got}, features.py {want}"); bad += 1
for name, kv in KERNEL_MSG.items():
    want = F.MSG_BIT[name]; got = c_side("msg", kv)
    if want != got:
        print(f"MISMATCH msg {name}: kernel 0x{kv:x} -> C {got}, features.py {want}"); bad += 1

# combinations, not just single bits
import itertools
for combo in itertools.combinations(KERNEL_MSG.items(), 3):
    kv = 0; want = 0
    for nm, v in combo:
        kv |= v; want |= F.MSG_BIT[nm]
    got = c_side("msg", kv)
    if got != want:
        print(f"MISMATCH msg combo {[c[0] for c in combo]}: C {got}, py {want}"); bad += 1

print(f"OK {bad}")
PYEOF
res=$(tail -1 /tmp/featparity.out)
if [[ "$res" == "OK 0" ]]; then
	ok "flag encoding matches features.py" "all single bits + 56 combinations"
else
	bad "flag encoding matches features.py" "$(head -3 /tmp/featparity.out | tr '\n' ' ')"
fi

# --- 2. the specific inversion that would have happened --------------
dw=$("$FC" msg 0x40)
py_dontwait=$("$PY" -c "import sys;sys.path.insert(0,'tools/harness');import features;print(features.MSG_BIT['MSG_DONTWAIT'])")
py_confirm=$("$PY" -c "import sys;sys.path.insert(0,'tools/harness');import features;print(features.MSG_BIT['MSG_CONFIRM'])")
if [[ "$dw" == "$py_dontwait" ]]; then
	ok "kernel MSG_DONTWAIT maps to MSG_DONTWAIT" "not $py_confirm (MSG_CONFIRM), which raw masking gives"
else
	bad "kernel MSG_DONTWAIT maps to MSG_DONTWAIT" "got $dw, want $py_dontwait"
fi

# --- 2b. THE WHOLE VECTOR, not just the helpers ----------------------
# Sabotage-found: asserting only on `featcheck msg` tests tx_msg_flags_feat()
# in isolation, so replacing the CALL to it inside tx_features_extract() with
# a raw mask passed cleanly. The helper was right and the vector was wrong.
# Compare all 16 bytes against features.py for representative events.
"$PY" - "$FC" <<'PYEOF' > /tmp/featvec.out 2>&1
import subprocess, sys
sys.path.insert(0, "tools/harness")
import features as F
fc = sys.argv[1]

# (record for features.py, scalars for the C side) -- the SAME event twice.
CASES = [
  ("sendmsg + DONTWAIT to :443",
   {"syscall_nr":0,"hook":"socket_sendmsg","path_hash":None,"path_depth":0,
    "path":None,"fd_type":"sock","open_flags":[],"dport":443,"family":"AF_INET",
    "daddr":"8.8.8.8","tx_id":1,"ngram":[],"msg_flags":["MSG_DONTWAIT"]},
   [0,5,0,0,0,0,12,0,443,2,0,1,0x40]),
  ("sendmsg loopback, MORE|NOSIGNAL",
   {"syscall_nr":0,"hook":"socket_sendmsg","path_hash":None,"path_depth":0,
    "path":None,"fd_type":"sock","open_flags":[],"dport":9999,"family":"AF_INET",
    "daddr":"127.0.0.1","tx_id":1,"ngram":[],"msg_flags":["MSG_MORE","MSG_NOSIGNAL"]},
   [0,5,0,0,0,0,12,0,9999,2,1,1,0x8000|0x4000]),
  ("file_open O_CREAT|O_TRUNC|O_WRONLY",
   {"syscall_nr":0,"hook":"file_open","path_hash":None,"path_depth":3,
    "path":"/a/b/c","fd_type":"reg","open_flags":["O_CREAT","O_TRUNC","O_WRONLY"],
    "dport":0,"family":None,"daddr":None,"tx_id":1,"ngram":[],"msg_flags":[]},
   [0,1,0,0,3,0,8,0x40|0x200|0x1,0,0,0,1,0]),
  ("unlink a dotfile",
   {"syscall_nr":0,"hook":"inode_unlink","path_hash":None,"path_depth":2,
    "path":"/home/.ssh/id_ed25519","fd_type":"reg","open_flags":[],
    "dport":0,"family":None,"daddr":None,"tx_id":1,"ngram":[],"msg_flags":[]},
   [0,2,0,0,2,1,8,0,0,0,0,1,0]),
]

bad = 0
for name, rec, sc in CASES:
    want = F.blind(F.extract(rec))
    syscall_nr,hook,ph,pd,isdot,fdt,of,dport,fam,lb,intx,mf = (
        sc[0],sc[1],0,sc[4],sc[5],sc[6],sc[7],sc[8],sc[9],sc[10],sc[11],sc[12])
    out = subprocess.run([fc,"vec",str(syscall_nr),str(hook),"0",str(pd),str(isdot),
                          str(fdt),str(of),str(dport),str(fam),str(lb),str(intx),str(mf)],
                         capture_output=True, text=True).stdout.split()
    got = [int(x) for x in out] if out else []
    if got != want:
        print(f"MISMATCH {name}")
        print(f"  features.py: {want}")
        print(f"  tx_features.h: {got}")
        bad += 1
print(f"OK {bad}")
PYEOF
res=$(tail -1 /tmp/featvec.out)
if [[ "$res" == "OK 0" ]]; then
	ok "the full 16-byte vector matches features.py" "4 representative events"
else
	bad "the full 16-byte vector matches features.py" "$(head -4 /tmp/featvec.out | tr '\n' ' ')"
fi

# --- 3. the model must not split on features the hook cannot supply --
for blob in data/model/model_tree_kernel.bin data/model_k/model_tree.bin; do
	[[ -f "$blob" ]] || continue
	out=$("$PY" - "$blob" <<'PYEOF'
import struct, sys, collections
b=open(sys.argv[1],'rb').read()
hdr='<IHBBBBHiII'; off=struct.calcsize(hdr); nn=struct.unpack_from(hdr,b,0)[6]
NAMES=["syscall_nr","hook_id","path_hash_b0","path_hash_b1","path_depth","path_is_dot",
       "fd_type","open_flags","dport_lo","dport_hi","af","is_loopback","tx_depth",
       "ngram_0","ngram_1","msg_flags"]
BLIND={"syscall_nr","ngram_0","ngram_1"}
use=collections.Counter(); tot=0; depth_ok=True
for i in range(nn):
    feat,thr,l,r=struct.unpack_from('<BBHH',b,off+6*i)
    if l!=0xffff: tot+=1; use[NAMES[feat]]+=1
bad=sum(v for k,v in use.items() if k in BLIND)
print(f"{bad} {tot}")
PYEOF
)
	blind=${out%% *}; total=${out##* }
	if [[ "$blind" == "0" ]]; then
		ok "model uses only supplyable features" "$(basename "$blob"): 0 of $total nodes blind"
	else
		bad "model uses only supplyable features" "$(basename "$blob"): $blind of $total nodes split on syscall_nr/ngram"
	fi
	break
done

# --- 4. the tree must fit the verifier's bounds ----------------------
blob=data/model/model_tree_kernel.bin
[[ -f "$blob" ]] || blob=data/model_k/model_tree.bin
if [[ -f "$blob" ]]; then
	"$PY" - "$blob" > /tmp/treebound.out <<'PYEOF'
import struct, sys
b=open(sys.argv[1],'rb').read()
hdr='<IHBBBBHiII'; off=struct.calcsize(hdr)
magic,abi,kind,nf,nc,_p,nn,osh,rows,acc=struct.unpack_from(hdr,b,0)
nodes=[struct.unpack_from('<BBHH',b,off+6*i) for i in range(nn)]
LEAF=0xffff
def depth(i,d=0,seen=frozenset()):
    if i in seen or d>64: return 99
    f,t,l,r=nodes[i]
    if l==LEAF: return d
    return max(depth(l,d+1,seen|{i}), depth(r,d+1,seen|{i}))
mx=depth(0)
oob=sum(1 for f,t,l,r in nodes if l!=LEAF and (l>=nn or r>=nn or f>=16))
print(f"{mx} {nn} {oob} {magic:#x} {nf} {nc}")
PYEOF
	read -r mx nn oob magic nf nc < /tmp/treebound.out
	(( mx <= 16 )) && ok "tree fits TX_TREE_MAX_DEPTH" "depth $mx of 16" \
	               || bad "tree fits TX_TREE_MAX_DEPTH" "depth $mx > 16; the walk would fail closed"
	(( nn <= 512 )) && ok "tree fits TX_TREE_MAX_NODES" "$nn of 512" \
	                || bad "tree fits TX_TREE_MAX_NODES" "$nn > 512"
	[[ "$oob" == "0" ]] && ok "no node index or feature out of range" "" \
	                    || bad "no node index or feature out of range" "$oob bad node(s)"
	[[ "$magic" == "0x54584d44" ]] && ok "blob magic is TXMD" "" || bad "blob magic is TXMD" "$magic"
	[[ "$nf" == "16" && "$nc" == "4" ]] && ok "shape matches the contract" "$nf features, $nc classes" \
	                                    || bad "shape matches the contract" "$nf/$nc"
else
	skip "tree bounds" "no kernel-only model blob; run: make model-kernel"
fi

echo
if (( fails == 0 )); then echo "t07: PASS ($n assertions)"; else echo "t07: FAIL ($fails of $n)"; fi
exit "$fails"
