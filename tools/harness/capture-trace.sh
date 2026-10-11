#!/usr/bin/env bash
# Capture an agent's syscall trace in the shape measure_gate.py expects.
#
# The paper's weakest claim rests on five traces of ONE agent, and four
# of those five are no longer in the archive. This script exists so the
# base can be rebuilt and widened: same flags, same naming, one file per
# task shape, compressed on the way out because a raw trace is tens of
# megabytes.
#
# The flags are not negotiable and the tool says why:
#
#   -f    follow threads; an agent is never single threaded, and the
#         replies arrive on threads other than the sender's.
#   -tt   microsecond timestamps; the reply window is a time window.
#   -yy   annotate descriptors with their endpoints. WITHOUT THIS a
#         socket cannot be told from a pipe and the whole measurement is
#         impossible, which is the one failure that looks like success:
#         the tool reports zero outbound operations rather than an error.
#   -s 128 enough payload to see what a send carried.
#   -e trace=network,read,write,close  reads and writes included, because
#         a reply consumed by read() on a socket is what makes a send
#         request-response rather than fire and forget.
#
# usage: capture-trace.sh <label> -- <command...>
set -uo pipefail

if [ $# -lt 3 ] || [ "$2" != "--" ]; then
	echo "usage: $0 <label> -- <command...>" >&2
	echo "   eg: $0 write-a-file -- python3 loop.py /var/lib/x 1" >&2
	exit 2
fi
label="$1"; shift 2

out="${TRACE_DIR:-$HOME/agenttx-private-traces}"
mkdir -p "$out"
raw="$out/$label.strace"

echo "==> capturing '$label'"
strace -f -tt -yy -s 128 -e trace=network,read,write,close \
       -o "$raw" "$@"
rc=$?

if [ ! -s "$raw" ]; then
	echo "capture-trace: $raw is empty; nothing was traced" >&2
	exit 1
fi

# Refuse to keep a trace that cannot answer the question. A capture
# without -yy annotations parses cleanly and yields zero outbound
# operations, which reads as "this agent used no network" instead of
# "this trace is unusable". Catching it here costs one grep.
if ! grep -qE '<(TCP|UDP|UNIX|socket):' "$raw"; then
	echo "capture-trace: no descriptor annotations in $raw." >&2
	echo "               The trace cannot distinguish a socket from a" >&2
	echo "               pipe, so it is useless for the gate. Keeping" >&2
	echo "               it as $raw.unusable rather than silently" >&2
	echo "               adding a zero to the corpus." >&2
	mv "$raw" "$raw.unusable"
	exit 1
fi

gzip -f "$raw"
echo "==> $raw.gz  ($(du -h "$raw.gz" | cut -f1), agent exit $rc)"
echo
python3 "$(dirname "$0")/measure_gate.py" --strace "$raw.gz" \
        --summary "$out/$label.summary.txt"
