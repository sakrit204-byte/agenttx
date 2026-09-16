# AgentTx — Annotated Reading List

Ordered by what you need first. Tiers 1 and 2 must be read before the Week 2
interface freeze; everything else is background you can pick up as the relevant
workstream needs it.

Assignment: one person per Tier 1 paper, present back at the Week 1 meeting.

---

## Tier 1 — Read first. These decide whether the project has a contribution left.

These are recent, they overlap us heavily, and if one of them already defers
network effects in the kernel, we reposition immediately.

| Paper | Why | Owner |
|---|---|---|
| **Fault-Tolerant Sandboxing for AI Coding Agents: A Transactional Approach to Safe Autonomous Execution** — [arXiv:2512.12806](https://arxiv.org/abs/2512.12806) | Closest prior work. Policy interception + transactional FS snapshots. Reports 100% interception, 100% rollback, 14.5% overhead. This is our rungs 1–2, already published. | P1 |
| **DeltaBox: Scaling Stateful AI Agents with Millisecond-Level Sandbox Checkpoint/Rollback** — [arXiv:2605.22781](https://arxiv.org/abs/2605.22781) | DeltaState / DeltaFS, OverlayFS-inspired change-based checkpointing. Overlaps P2 almost exactly. Their millisecond figure is our target. | P2 |
| **ActPlane** — in-kernel agent policy enforcement via eBPF + IFC DSL | Overlaps P3. **Citation incomplete — find the primary paper.** Referenced in secondary sources only. | P3 |
| **CRAB** — eBPF classification of per-turn OS effects to align checkpoints | Overlaps our effect-classification contribution directly. **Citation incomplete — find the primary paper.** | P4 |

> If ActPlane or CRAB turns out to defer external effects in the kernel,
> contribution 1 is gone and we pivot the paper onto the comparative evaluation.
> Better to learn this in Week 1 than Week 12.

---

## Tier 2 — The foundations we are building on. Cite these; they are our lineage.

| Paper | Why it matters to us |
|---|---|
| **Operating System Transactions** — Porter, Hofmann, Rossbach, Benn, Witchel. SOSP '09. [PDF](https://www.cs.utexas.edu/~witchel/pubs/porter09sosp.pdf) · [project](https://www.cs.unc.edu/~porter/txos/) | TxOS. Our direct ancestor: `sys_xbegin/xend/xabort` in Linux 2.6.22, ACID across FS + process + credentials + signals, ~8,600 LoC, 1–2× overhead. Never merged. Read the isolation and conflict-detection sections carefully. |
| **Operating Systems Should Provide Transactions** — Porter & Witchel. HotOS XII, 2009. [link](https://cs.utexas.edu/users/witchel/pubs/porter09hotos.html) | The position-paper version. Short. Steal the argument structure for our intro. |
| **Speculative Execution in a Distributed File System** — Nightingale, Chen, Flinn. SOSP '05, Best Paper. [PDF](https://www.cs.cmu.edu/~dga/15-849/papers/speculator-sosp2005.pdf) | Speculator. Kernel speculation with checkpoint/rollback and causal dependency tracking across IPC. The reason we can say speculation at the OS layer is a known-good idea. |
| **Rethink the Sync** — Nightingale, Veeraraghavan, Chen, Flinn. OSDI '06, Best Paper. [PDF](https://www.usenix.org/legacy/event/osdi06/tech/nightingale/nightingale.pdf) | **The single most important paper for P3.** External synchrony: buffer externally visible output until the state it depends on is durable. Our effect WAL is this mechanism with a different trigger. Do not write P3-07/P3-08 before reading it. |
| **TxFS: Leveraging File-System Crash Consistency to Provide ACID Transactions** — Hu et al. USENIX ATC '18, Best Paper. [PDF](https://www.usenix.org/system/files/conference/atc18/atc18-hu.pdf) · [code](https://github.com/ut-osa/txfs) | Shows transactions can be built cheaply by reusing the ext4 journal — 5,200 LoC vs TxOS's sprawl. Relevant to P2's "do we need our own FS?" decision. |

---

## Tier 3 — In-kernel ML. P4's core references.

| Paper | Why |
|---|---|
| **Improving Storage Systems Using Machine Learning (KML)** — Akgun, Zadok et al. ACM TOS 2023. [PDF](https://www.fsl.cs.stonybrook.edu/docs/kml/kml-tos23.pdf) · [arXiv:2111.11554](https://arxiv.org/abs/2111.11554) · [code](https://github.com/sbu-fsl/kernel-ml) | Proof that in-kernel NNs are practical: <4 KB kernel memory, <0.2% CPU, 2.3–15× I/O gains. Our "is this even feasible" citation. |
| **Dynamic Fixed-point Values in eBPF: a Case for Fully In-kernel Anomaly Detection** — [ACM DL](https://dl.acm.org/doi/fullHtml/10.1145/3674213.3674219) | **The technique P4-10 implements.** Fixed-point arithmetic under the verifier. Read before writing a line of `infer.bpf.c`. |
| **Practicality of in-kernel/user-space packet processing with lightweight NN and decision tree** — [ScienceDirect](https://www.sciencedirect.com/science/article/pii/S1389128624000203) | int8 quantisation, weights in BPF maps, per-layer tail calls, ~84% lower inference latency than user space. The engineering recipe. |
| **When eBPF Meets Machine Learning: On-the-fly OS Kernel Compartmentalization** — [arXiv:2401.05641](https://arxiv.org/abs/2401.05641) | Adjacent application of the same idea. |

---

## Tier 4 — Agent security context. Motivation and framing.

| Source | Why |
|---|---|
| **Defeating Prompt Injections by Design (CaMeL)** — DeepMind. [arXiv:2503.18813](https://arxiv.org/abs/2503.18813) · [Willison's explainer](https://simonwillison.net/2025/Apr/11/camel/) | The strongest *preventive* defence. We should position AgentTx as complementary, not competing — CaMeL constrains what the agent decides, we constrain what its syscalls can do. |
| **AgentSight: System-Level Observability for AI Agents Using eBPF** — [arXiv:2508.02736](https://arxiv.org/abs/2508.02736) | eBPF for agents at <3% overhead. Our overhead target and a template for the eval section. |
| **ACRFence: Preventing Semantic Rollback Attacks in Agent Checkpoint-Restore** — [arXiv:2603.20625](https://arxiv.org/pdf/2603.20625) | Attacks *against rollback itself*. Read before claiming our rollback is safe. Feeds the limitations section. |
| **Cordon: Semantic Transactions for Tool-Using LLM Agents** — [arXiv:2606.17573](https://arxiv.org/html/2606.17573v1) | Transactions at the tool-call boundary. The contrast that justifies working at the syscall boundary instead. |
| **Always-On Agents: A Survey of Persistent Memory, State, and Governance** — [arXiv:2606.30306](https://arxiv.org/pdf/2606.30306) | Survey. Best citation mine in the list — read its references. |
| **Making `syscall` a Privilege not a Right** — [arXiv:2406.07429](https://arxiv.org/pdf/2406.07429) | Adjacent take on restricting syscall access. |
| **eBPF for AI Agent Enforcement (ARMO)** — [blog](https://www.armosec.io/blog/ebpf-based-ai-agent-enforcement/) | Non-archival but the clearest statement of why Tetragon/Falco/KubeArmor assume determinism that agents violate. Good motivating quote. |
| **Agent Rollback and Checkpoint Patterns** — [reference](https://www.digitalapplied.com/blog/agent-rollback-checkpoint-patterns-2026-engineering-reference) | Non-archival. Documents the three reversibility tiers used in practice — checkpoints, sagas, idempotency keys — all at the application layer. This *is* the gap we target. |
| **awesome-agent-runtime-security** — [GitHub](https://github.com/bureado/awesome-agent-runtime-security) | Living link collection. Check monthly; this field moves fast. |

---

## Tier 5 — Implementation manuals. Read when your fragment needs them.

| Source | For whom |
|---|---|
| **FiST: A Language for Stackable File Systems** — Zadok & Nieh, USENIX ATC 2000. [PDF](https://www.filesystems.org/docs/fist-lang/fist.pdf) · [site](https://www.filesystems.org/) | P2, fragment P2-10. Wrapfs templates are the canonical starting point for a stackable FS. eCryptfs and Unionfs both derive from them. |
| **A Stackable File System Interface for Linux** — Zadok & Badulescu. [PDF](https://www.filesystems.org/docs/linux-stacking/linux.pdf) | P2, same. |
| Linux `Documentation/filesystems/overlayfs.rst` (in-tree) | P2. Whiteouts, `metacopy`, `redirect_dir`, and whether copy-up uses reflink on your filesystem. |
| Linux `Documentation/bpf/prog_lsm.rst` (in-tree) | P3. BPF LSM attachment, sleepable hooks. |
| Linux `Documentation/bpf/kfuncs.rst` (in-tree) | P1 fragment P1-10, P3 fragment P3-04. Exporting module functions to BPF. |
| `arch/x86/entry/syscalls/syscall_64.tbl` + `include/linux/syscalls.h` | P1 fragment P1-11. |
| **eunomia eBPF tutorials** — [site](https://eunomia.dev/GPTtrace/) | P3, P4. Practical eBPF, gentler than kernel docs. |

---

## Standing instruction

This field is producing relevant papers monthly. Re-run the searches at the
start of each phase (Weeks 3, 6, 10, 13) on: *agent sandbox transaction
rollback*, *eBPF LSM agent enforcement*, *in-kernel inference eBPF*, *deferred
external effects speculation*. Log anything new in this file with a one-line
note on whether it threatens a contribution.
