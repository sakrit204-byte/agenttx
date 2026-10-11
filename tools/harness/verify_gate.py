#!/usr/bin/env python3
"""
An INDEPENDENT check on measure_gate.py, written against the strace
format rather than against that tool's source.

Reproducing a number by running the same code again only shows the code
is deterministic. This re-derives it: its own stitcher, its own burst
classifier, its own counting. Where the two disagree, the disagreement
is the output anyone actually wants.

It also renders each connection as a string of sends and receives, so a
reader can count request boundaries by eye and trust neither tool:

    OiOOiiiiiiiiiiiiiiOiiiiiiiOiiiiiiiiiiiOiiiiO
                                                ^ a trailing send,
                                                  never answered

Run it on every trace in a corpus before quoting a figure from it.
"""

import argparse
import collections
import gzip
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure_gate as G                                    # noqa: E402

UNFIN = re.compile(r"^(\d+)\s+(.*?)<unfinished \.\.\.>\s*$")
# Deliberately loose about what sits between the pid and the marker.
# The one operation our first attempt missed was a sendmmsg whose
# resumption strace renders in a shape a tighter pattern rejects, and
# missing a single operation moved the headline of a 41 request trace
# by two and a half points.
RESUM = re.compile(r"^(\d+)\s+.*?<\.\.\. \w+ resumed>(.*)$")


def events_of(path, drop_dns=False, include_unix=False):
    opener = gzip.open if str(path).endswith(".gz") else open
    held, conns = {}, collections.OrderedDict()
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if " --- SIG" in line or "+++" in line:
                continue
            mu = UNFIN.match(line)
            if mu:
                # Keep the WHOLE head, pid included. Capturing only the
                # part after the pid produced a rejoined line that the
                # main pattern rejects for having no pid, which silently
                # dropped every split call. It cost one operation here
                # and would cost more on a busier trace.
                held[int(mu.group(1))] = line.split("<unfinished", 1)[0]
                continue
            mr = RESUM.match(line)
            if mr:
                head = held.pop(int(mr.group(1)), None)
                if head is None:
                    continue
                line = head.rstrip() + mr.group(2)
            mo = G.LINE.match(line)
            if not mo:
                continue
            fd, annot = G.fd_of(mo.group("args"))
            if fd is None or not G.is_socket(annot):
                continue
            if not include_unix and annot.startswith("UNIX"):
                continue
            ret = mo.group("ret")
            if ret == "?" or int(ret) < 0:
                continue
            _ip, port = G.peer_addr(annot)
            if drop_dns and port == 53:
                continue
            key = (int(mo.group("pid")), annot)
            if mo.group("call") in G.OUTBOUND:
                conns.setdefault(key, []).append("O")
            elif mo.group("call") in G.INBOUND:
                conns.setdefault(key, []).append("i")
    return conns


def count(conns):
    ops = req = unanswered = 0
    for evs in conns.values():
        seq = "".join(evs)
        if "O" not in seq:
            continue
        trailing = 1 if seq.endswith("O") else 0
        req += len(re.findall(r"O+i", seq)) + trailing
        unanswered += trailing
        ops += seq.count("O")
    return ops, req, unanswered


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trace")
    ap.add_argument("--show", action="store_true",
                    help="print every connection as a send/receive string")
    a = ap.parse_args()

    conns = events_of(a.trace)
    ops, req, un = count(conns)

    print("independent re-derivation of %s" % a.trace)
    print("  outbound socket operations : %d" % ops)
    print("  logical requests           : %d" % req)
    print("  never answered             : %d" % un)
    print("  per logical request        : %.2f%%"
          % (100.0 * un / req if req else 0.0))

    nd_ops, nd_req, nd_un = count(events_of(a.trace, drop_dns=True))
    print("  excluding DNS              : %.2f%%  (%d ops, %d requests)"
          % (100.0 * nd_un / nd_req if nd_req else 0.0, nd_ops, nd_req))

    # With a sample this small, one operation is a sensitivity to state
    # rather than a rounding concern to bury.
    if req:
        print("  one operation is worth     : %.2f points" % (100.0 / req))

    if a.show:
        print("\n  connections. O is a send, i a receive.")
        for (pid, annot), evs in conns.items():
            seq = "".join(evs)
            if "O" not in seq:
                continue
            print("    pid %-6d %-34s %s" % (pid, annot[:34], seq[:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
