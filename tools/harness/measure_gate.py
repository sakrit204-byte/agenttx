#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/harness/measure_gate.py --- the week 3-4 project gate.

PROPOSAL.md:

    Measure what fraction of an agent's outbound network operations are
    fire-and-forget rather than request-response.
      > 20%    proceed; that measurement is Figure 1
      10-20%   proceed with a narrowed claim
      < 10%    promote contributions 2 and 5 to the headline

    Four days in month one that de-risks the year.

THIS NEEDS NO KERNEL WORK.  No custom kernel, no module, no BPF, no VM.  It
needs a real agent, `strace`, and this file.  Run it in week 1 while the
kernel builds: if the number comes back under 10% the project's headline
claim changes, and you would much rather learn that in week 1 than week 4.

Deferral is possible only where the agent does not block on a reply.  So
this fraction is a hard ceiling on how much Contribution 1 can ever be
worth, and nobody has measured it.

METHOD
------
An outbound operation AWAITS A REPLY if, on the same file descriptor, the
same thread performs a read/recv before it performs its next outbound
write on any descriptor, within a time window (default 30s).

Three outcomes, and all three are reported:

    fire-and-forget   no reply consumed        -> deferrable
    request-response  reply consumed           -> must NOT be deferred
    undetermined      window closed, thread exited, trace truncated

`undetermined` is never folded into fire-and-forget.  Doing so would
inflate the headline number in our own favour, and a gate result stated
without its undetermined rate is not a measurement.

USAGE
-----
    # 1. capture (any Linux, WSL included)
    strace -f -tt -yy -s 128 -e trace=network,read,write,close \\
           -o agent.strace  <your agent command>

    # 2. measure
    python3 measure_gate.py --strace agent.strace

    # 3. optional: emit docs/trace-format.md records for the rest of the
    #    pipeline, so the gate capture doubles as training data
    python3 measure_gate.py --strace agent.strace --jsonl out.jsonl

`-yy` is what makes the output useful: it annotates each fd with the socket
it refers to, so the destination is recoverable without guessing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 1234  12:34:56.789012 sendto(3<TCP:[127.0.0.1:5432]>, "..."..., 5, 0, NULL, 0) = 5
LINE = re.compile(
    r"^(?P<pid>\d+)\s+"
    r"(?:(?P<h>\d+):(?P<m>\d+):(?P<s>\d+(?:\.\d+)?)\s+)?"
    r"(?P<call>\w+)\((?P<args>.*?)\)\s*=\s*(?P<ret>-?\d+|\?)"
)
UNFINISHED = re.compile(r"^(?P<pid>\d+)\s+.*<unfinished \.\.\.>$")
RESUMED = re.compile(r"^(?P<pid>\d+)\s+.*resumed>")

# fd annotation from -yy:  3<TCP:[10.0.0.1:443]>, 3<socket:[12345]>, or a
# connected pair 3<TCP:[10.0.0.5:41000->104.16.20.35:443]>.
#
# The annotation body may itself contain '>' -- that is exactly what the
# '->' in a connected socket is -- so a naive [^>]* truncates at "41000-"
# and every destination in the report comes out as the LOCAL endpoint.
# Match lazily instead and require the closing '>' to be followed by a
# real argument separator.
FD_ANNOT = re.compile(r"^(?P<fd>\d+)(?:<(?P<annot>.*?)>(?=\s*[,)]|\s|$))?")
# strace -yy renders a connected socket as TCP:[LOCAL->PEER], e.g.
#   TCP:[10.0.0.5:41000->104.16.20.35:443]
# Taking the first address in that string yields the LOCAL endpoint, which
# is not what any consumer wants: the compensable registry, the per-host
# breakdown and the dport feature are all about the PEER.  So split on the
# arrow and keep the right-hand side, falling back to the sole address when
# the socket is unconnected and strace printed only one.
ADDR = re.compile(r"(?P<ip>\d+\.\d+\.\d+\.\d+):(?P<port>\d+)")


def peer_addr(annot: str):
    """(ip, port) of the REMOTE end, or (None, 0)."""
    if not annot:
        return None, 0
    side = annot.split("->")[-1]          # right of the arrow, or the whole
    mo = ADDR.search(side)
    if not mo:
        return None, 0
    return mo.group("ip"), int(mo.group("port"))

OUTBOUND = {"sendto", "sendmsg", "sendmmsg", "write", "writev", "send"}
INBOUND = {"recvfrom", "recvmsg", "recvmmsg", "read", "readv", "recv"}
CONNECTS = {"connect"}

SYSCALL_NR = {"sendmsg": 46, "sendto": 44, "write": 1, "sendmmsg": 307,
              "writev": 20, "send": 44, "connect": 42}


def parse_ts(mo) -> float | None:
    if not mo.group("h"):
        return None
    return int(mo.group("h")) * 3600 + int(mo.group("m")) * 60 + float(mo.group("s"))


def fd_of(args: str):
    mo = FD_ANNOT.match(args.strip())
    if not mo:
        return None, None
    return int(mo.group("fd")), mo.group("annot") or ""


def is_socket(annot: str) -> bool:
    a = annot or ""
    return a.startswith(("TCP", "UDP", "UNIX", "socket:", "TCPv6", "UDPv6"))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--strace", required=True, help="strace -f -tt -yy output")
    ap.add_argument("--window", type=float, default=30.0,
                    help="seconds a reply may arrive in (default 30)")
    ap.add_argument("--jsonl", help="also emit docs/trace-format.md records here")
    ap.add_argument("--summary", help="write both gate figures as JSON here")
    ap.add_argument("--include-unix", action="store_true",
                    help="count AF_UNIX sockets too (default: INET only, since "
                         "a local socket is not an outbound external effect)")
    a = ap.parse_args(argv)

    # events[(pid, fd)] -> list of (idx, ts, direction)
    sends: list[dict] = []
    # per (pid, fd): pending send indices awaiting a verdict
    pending: dict[tuple, list[int]] = {}
    # per (pid, connection-annotation): the connection-level view.
    # A logical request is fragmented by TLS across many sendto() calls, so
    # the per-syscall fraction counts one effect many times.  This is the
    # second, coarser unit, and both are reported.  See docs/journal/p4.md.
    conn_stats: dict[tuple, dict] = {}

    n_lines = n_parsed = 0
    with open(a.strace, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            n_lines += 1
            line = line.rstrip("\n")
            if UNFINISHED.match(line) or RESUMED.search(line):
                # Interleaved multi-threaded output. Skipping these slightly
                # undercounts; it never invents a fire-and-forget, which is
                # the direction an honest error should go.
                continue
            mo = LINE.match(line)
            if not mo:
                continue
            n_parsed += 1
            pid = int(mo.group("pid"))
            call = mo.group("call")
            ts = parse_ts(mo)
            args = mo.group("args")
            ret = mo.group("ret")
            fd, annot = fd_of(args)
            if fd is None:
                continue

            if call in CONNECTS:
                pending.setdefault((pid, fd), [])
                continue

            if call in OUTBOUND:
                if not is_socket(annot):
                    continue
                if not a.include_unix and annot.startswith("UNIX"):
                    continue
                if ret == "?" or int(ret) < 0:
                    continue
                cs = conn_stats.setdefault(
                    (pid, annot), {"out": 0, "in": 0, "obytes": 0, "ibytes": 0})
                cs["out"] += 1
                cs["obytes"] += int(ret)

                idx = len(sends)
                dip, dport = peer_addr(annot)
                sends.append({
                    "idx": idx, "pid": pid, "fd": fd, "ts": ts, "call": call,
                    "annot": annot,
                    "daddr": dip,
                    "dport": dport,
                    "bytes": int(ret),
                    "verdict": None,
                })
                # Any send by this thread that is still unresolved is now
                # fire-and-forget: the thread moved on without reading.
                for k, lst in list(pending.items()):
                    if k[0] != pid:
                        continue
                    for j in lst:
                        if sends[j]["verdict"] is None:
                            sends[j]["verdict"] = False
                    pending[k] = []
                pending.setdefault((pid, fd), []).append(idx)

            elif call in INBOUND:
                if not is_socket(annot):
                    continue
                if not (not a.include_unix and annot.startswith("UNIX")):
                    if ret != "?" and int(ret) >= 0:
                        cs = conn_stats.setdefault(
                            (pid, annot),
                            {"out": 0, "in": 0, "obytes": 0, "ibytes": 0})
                        cs["in"] += 1
                        cs["ibytes"] += int(ret)
                lst = pending.get((pid, fd))
                if not lst:
                    continue
                for j in lst:
                    s = sends[j]
                    if s["verdict"] is not None:
                        continue
                    if ts is not None and s["ts"] is not None and \
                            (ts - s["ts"]) > a.window:
                        continue          # too late; stays undetermined
                    s["verdict"] = True   # a reply was consumed
                pending[(pid, fd)] = []

            elif call == "close":
                # Closing without a read is a definite fire-and-forget.
                for j in pending.pop((pid, fd), []):
                    if sends[j]["verdict"] is None:
                        sends[j]["verdict"] = False

    total = len(sends)
    if total == 0:
        print("gate: no outbound socket operations found in %s" % a.strace,
              file=sys.stderr)
        print("      Did you pass -f -tt -yy to strace? Without -yy the fd",
              file=sys.stderr)
        print("      annotations are absent and sockets cannot be identified.",
              file=sys.stderr)
        return 1

    ff = sum(1 for s in sends if s["verdict"] is False)
    rr = sum(1 for s in sends if s["verdict"] is True)
    un = total - ff - rr

    pct_ff = 100.0 * ff / total
    pct_rr = 100.0 * rr / total
    pct_un = 100.0 * un / total

    print("=" * 62)
    print("AgentTx project gate -- fire-and-forget fraction")
    print("=" * 62)
    print("  parsed %d of %d strace lines" % (n_parsed, n_lines))
    print("  outbound socket operations: %d" % total)
    print()
    print("  fire-and-forget    %6d   %5.1f%%   <- deferrable" % (ff, pct_ff))
    print("  request-response   %6d   %5.1f%%   <- must not be deferred" % (rr, pct_rr))
    print("  undetermined       %6d   %5.1f%%   <- neither; report it" % (un, pct_un))
    print()

    # The gate is on fire-and-forget as a share of everything observed.
    # Reporting it as a share of (ff + rr) -- i.e. excluding undetermined --
    # would flatter the result, so print both and say which is the gate.
    if ff + rr:
        print("  (excluding undetermined: %.1f%% fire-and-forget -- NOT the gate"
              % (100.0 * ff / (ff + rr)))
        print("   figure; stated only so the two are not confused)")
        print()

    if pct_ff > 20:
        print("  GATE: PASS (>20%). Proceed. This measurement is Figure 1.")
    elif pct_ff >= 10:
        print("  GATE: MARGINAL (10-20%). Proceed with a narrowed claim.")
    else:
        print("  GATE: FAIL (<10%). Promote contributions 2 and 5 to the")
        print("        headline; Contribution 1's ceiling is this number.")

    # ---------------------------------------------------------------
    # The SECOND unit.  Everything above counts syscalls; TLS fragments a
    # single logical request across many sendto() calls, so that count
    # treats one effect as many and inflates fire-and-forget in our own
    # favour.  This is the coarse bound in the other direction: a whole
    # connection is request-response if it ever read a byte back.
    #
    # Neither is the publishable figure.  The honest unit is the logical
    # request, which needs request framing we do not parse.  Reporting the
    # bracket is the most that is currently true.  docs/journal/p4.md.
    # ---------------------------------------------------------------
    conns_out = {k: v for k, v in conn_stats.items() if v["out"] > 0}
    n_conn = len(conns_out)
    conn_ff = sum(1 for v in conns_out.values() if v["in"] == 0)
    pct_conn_ff = 100.0 * conn_ff / n_conn if n_conn else 0.0
    ops_on_ff = sum(v["out"] for v in conns_out.values() if v["in"] == 0)
    pct_ops_ff = 100.0 * ops_on_ff / total if total else 0.0

    print()
    print("=" * 62)
    print("  SECOND UNIT -- per connection, not per syscall")
    print("=" * 62)
    print("  connections with outbound traffic: %d" % n_conn)
    print("  never read a byte back            %6d   %5.1f%%" % (conn_ff, pct_conn_ff))
    print("  outbound ops on those connections %6d   %5.1f%%" % (ops_on_ff, pct_ops_ff))
    print()
    if n_conn:
        spread = pct_ff / pct_conn_ff if pct_conn_ff > 0.01 else float("inf")
        print("  per-syscall says %.1f%%, per-connection says %.1f%%" % (pct_ff, pct_conn_ff))
        if spread != spread or spread == float("inf"):
            print("  The two units disagree completely. Do not quote either.")
        elif spread > 1.5 or spread < 0.67:
            print("  The two disagree by %.0fx. The true fire-and-forget fraction is" % spread)
            print("  bracketed between them and NEITHER is publishable on its own.")
            print("  The unit that would settle it is the logical request; this")
            print("  tool does not model request framing. See docs/journal/p4.md.")
        else:
            print("  The two units agree to within 1.5x, which is the strongest")
            print("  statement this method supports.")

    if pct_un > 20:
        print()
        print("  WARNING: %.1f%% undetermined is too high to draw a conclusion." % pct_un)
        print("  Usual causes: trace truncated before the agent exited, or")
        print("  --window too short for this workload. Re-capture.")

    # Top destinations: useful for building the compensable registry.
    by_dest: dict = {}
    for s in sends:
        k = (s["daddr"], s["dport"])
        d = by_dest.setdefault(k, [0, 0, 0])
        d[0] += 1
        if s["verdict"] is False:
            d[1] += 1
        elif s["verdict"] is True:
            d[2] += 1
    print()
    print("  top destinations")
    print("    %-24s %7s %7s %7s" % ("dest", "total", "f-and-f", "req-rsp"))
    for (ip, port), (t, f, r) in sorted(by_dest.items(), key=lambda kv: -kv[1][0])[:12]:
        print("    %-24s %7d %7d %7d" % ("%s:%s" % (ip or "?", port), t, f, r))

    if a.jsonl:
        outp = Path(a.jsonl)
        outp.parent.mkdir(parents=True, exist_ok=True)
        with open(outp, "w", encoding="utf-8", newline="\n") as fh:
            for i, s in enumerate(sends, 1):
                fh.write(json.dumps({
                    "v": 1, "seq": i,
                    "ts_ns": int((s["ts"] or 0) * 1e9),
                    "tx_id": 1, "pid": s["pid"], "tgid": s["pid"],
                    "hook": "socket_sendmsg", "syscall": s["call"],
                    "syscall_nr": SYSCALL_NR.get(s["call"], 0),
                    "path": None, "path_hash": None, "path_depth": 0,
                    "fd_type": "sock", "open_flags": [],
                    # The connection this operation belongs to.  Added
                    # because the fire-and-forget fraction is only
                    # meaningful per CONNECTION: a single logical HTTP
                    # request is fragmented by TLS across many sendto()
                    # calls, and counting each fragment as an independent
                    # effect inflates the headline in our own favour.
                    # docs/journal/p4.md has the measurement that showed it.
                    "fd": s["fd"], "conn": s["annot"],
                    "family": "AF_INET", "daddr": s["daddr"], "dport": s["dport"],
                    "msg_flags": [], "payload_len": s["bytes"],
                    # Redaction (docs/trace-format.md): real captures never
                    # carry payload bytes out of the machine that made them.
                    "payload_prefix": None,
                    "awaits_reply": s["verdict"],
                    "ngram": [], "label": None, "label_source": "rule",
                }, separators=(",", ":")) + "\n")
        print()
        print("  wrote %d records -> %s" % (total, outp))
        print("  (payloads stripped; label with tools/harness/ before training)")
    if a.summary:
        sp = Path(a.summary)
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps({
            "v": 1,
            "strace": str(a.strace),
            "lines_total": n_lines,
            "lines_parsed": n_parsed,
            "syscall": {
                "total": total, "ff": ff, "rr": rr, "undetermined": un,
                "pct": pct_ff,
            },
            "connection": {
                "total": n_conn, "ff": conn_ff, "pct": pct_conn_ff,
                "ops_on_ff": ops_on_ff, "ops_pct": pct_ops_ff,
            },
            "verdict": ("PASS" if pct_ff > 20 else
                        "MARGINAL" if pct_ff >= 10 else "FAIL"),
            "trustworthy": bool(
                n_conn and pct_conn_ff > 0.01 and
                0.67 <= (pct_ff / pct_conn_ff) <= 1.5),
        }, indent=2) + "\n", encoding="utf-8")
        print()
        print("  summary -> %s" % sp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
