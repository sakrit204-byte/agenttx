#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/bench/run.py --- benchmark runner and format enforcer.  P4-11.

"P1-P3 deliver bench_*.sh conforming to your CSV format.  Enforce it."

So this does enforce it.  A bench script whose output does not conform is a
FAILURE, not a warning: four people emitting four nearly-compatible CSV
dialects at week 12 is how a results section gets written by hand at 3am
with a calculator.

THE FORMAT
----------
Each tools/bench/bench_*.sh writes CSV to stdout with exactly this header:

    stream,fragment,metric,unit,config,n,value,stddev,notes

  stream     p1 | p2 | p3 | p4
  fragment   the tracker id that owns the measurement, e.g. P3-12
  metric     short snake_case name, stable across runs; it is the join key
  unit       ns | us | ms | pct | bytes | ops_per_sec | ratio | count
  config     the arm of the comparison: baseline | hooks_off | log_only |
             enforcing | stub | real | int8 | float32 ...
  n          number of samples behind this row
  value      float, in `unit`
  stddev     float, same unit; empty only if n == 1
  notes      free text, no commas

ONE ROW PER (metric, config).  The runner refuses duplicates, because a
duplicate silently halves or doubles a mean depending on how you aggregate,
and nobody notices until a reviewer does.

WHY stddev IS MANDATORY
-----------------------
A single number from a single run inside a KVM guest is not a measurement.
If a bench cannot produce a stddev it is measuring once, and measuring once
is what produces the 3% overhead figure that turns out to be 30%.

WHY THE KERNEL PROFILE IS RECORDED
----------------------------------
Every row is stamped with the guest kernel's KASAN state.  A benchmark run
on the debug kernel measures KASAN's shadow-memory checks, not AgentTx --
by more than the effect being reported.  The runner refuses to write a
results file from a KASAN kernel unless --allow-debug-kernel is given, and
stamps the file either way.

Usage:
    python3 run.py --all --out results/
    python3 run.py --check results/bench_hooks.csv      # format only
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

HEADER = ["stream", "fragment", "metric", "unit", "config",
          "n", "value", "stddev", "notes"]

STREAMS = {"p1", "p2", "p3", "p4"}
UNITS = {"ns", "us", "ms", "pct", "bytes", "ops_per_sec", "ratio", "count"}


class FormatError(Exception):
    pass


def validate(text: str, source: str) -> list[dict]:
    """Parse and validate one bench script's CSV. Raises FormatError."""
    rdr = csv.reader(io.StringIO(text))
    try:
        head = next(rdr)
    except StopIteration:
        raise FormatError("%s: empty output (the bench printed nothing)" % source)

    head = [h.strip() for h in head]
    if head != HEADER:
        raise FormatError(
            "%s: header is\n    %s\nexpected\n    %s"
            % (source, ",".join(head), ",".join(HEADER)))

    rows, seen = [], {}
    for i, raw in enumerate(rdr, start=2):
        if not raw or all(not c.strip() for c in raw):
            continue
        if len(raw) != len(HEADER):
            raise FormatError("%s:%d: %d fields, expected %d (a comma in `notes`?)"
                              % (source, i, len(raw), len(HEADER)))
        r = dict(zip(HEADER, (c.strip() for c in raw)))

        if r["stream"] not in STREAMS:
            raise FormatError("%s:%d: stream %r not in %s"
                              % (source, i, r["stream"], sorted(STREAMS)))
        if r["unit"] not in UNITS:
            raise FormatError("%s:%d: unit %r not in %s"
                              % (source, i, r["unit"], sorted(UNITS)))
        if not r["metric"] or " " in r["metric"]:
            raise FormatError("%s:%d: metric %r must be non-empty snake_case"
                              % (source, i, r["metric"]))
        if not r["config"]:
            raise FormatError("%s:%d: config is empty; name the arm being measured"
                              % (source, i))
        try:
            n = int(r["n"])
            val = float(r["value"])
        except ValueError:
            raise FormatError("%s:%d: n=%r value=%r are not numeric"
                              % (source, i, r["n"], r["value"]))
        if n < 1:
            raise FormatError("%s:%d: n=%d" % (source, i, n))
        if n > 1 and not r["stddev"]:
            raise FormatError(
                "%s:%d: n=%d but stddev is empty.\n"
                "    A mean without a spread is not a measurement; if the\n"
                "    bench cannot compute one it is sampling once."
                % (source, i, n))
        if r["stddev"]:
            try:
                float(r["stddev"])
            except ValueError:
                raise FormatError("%s:%d: stddev=%r is not numeric"
                                  % (source, i, r["stddev"]))

        key = (r["metric"], r["config"])
        if key in seen:
            raise FormatError(
                "%s:%d: duplicate row for metric=%s config=%s (first at line %d).\n"
                "    One row per (metric, config). Aggregate inside the bench."
                % (source, i, key[0], key[1], seen[key]))
        seen[key] = i
        r["_value"] = val
        rows.append(r)

    if not rows:
        raise FormatError("%s: header only, no data rows" % source)
    return rows


def kernel_profile() -> tuple[str, bool]:
    """('debug'|'perf'|'unknown', is_kasan). Read from the running kernel."""
    for path in ("/proc/config.gz", "/boot/config-" + platform.release()):
        try:
            if path.endswith(".gz"):
                import gzip
                with gzip.open(path, "rt") as fh:
                    cfg = fh.read()
            else:
                cfg = Path(path).read_text(errors="replace")
        except OSError:
            continue
        kasan = "\nCONFIG_KASAN=y" in cfg
        return ("debug" if kasan else "perf"), kasan
    return "unknown", False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true", help="run every bench_*.sh")
    ap.add_argument("--bench", action="append", default=[],
                    help="run just this bench script (repeatable)")
    ap.add_argument("--out", default="results", help="output directory")
    ap.add_argument("--check", help="validate an existing CSV and exit")
    ap.add_argument("--allow-debug-kernel", action="store_true",
                    help="permit results from a KASAN kernel (they are not "
                         "publishable; the file is stamped either way)")
    a = ap.parse_args(argv)

    here = Path(__file__).resolve().parent

    if a.check:
        try:
            rows = validate(Path(a.check).read_text(encoding="utf-8"), a.check)
        except FormatError as e:
            print("FORMAT ERROR\n  %s" % e, file=sys.stderr)
            return 1
        print("%s: OK, %d rows" % (a.check, len(rows)))
        return 0

    profile, kasan = kernel_profile()
    print("bench: kernel profile = %s%s" % (profile, "  (KASAN ON)" if kasan else ""))
    if kasan and not a.allow_debug_kernel:
        print("\nbench: refusing to record results from a KASAN kernel.",
              file=sys.stderr)
        print("  Overhead measured here is KASAN's shadow-memory checking, not",
              file=sys.stderr)
        print("  AgentTx, and by a larger margin than the effect being reported.",
              file=sys.stderr)
        print("  Rebuild and reboot:  make vm-kernel CONFIG=perf", file=sys.stderr)
        print("  Or pass --allow-debug-kernel for a throwaway sanity run.",
              file=sys.stderr)
        return 2

    scripts = [here / b for b in a.bench]
    if a.all or not scripts:
        scripts = sorted(here.glob("bench_*.sh"))
    if not scripts:
        print("bench: no bench_*.sh found in %s" % here, file=sys.stderr)
        print("  P1-P3 each own one; see tracker P1-14, P2-11, P3-12.",
              file=sys.stderr)
        return 1

    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")

    all_rows, bad = [], 0
    for s in scripts:
        if not s.exists():
            print("  MISSING  %s" % s.name)
            bad += 1
            continue
        print("  running  %s" % s.name)
        try:
            proc = subprocess.run(["bash", str(s)], capture_output=True,
                                  text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            print("  TIMEOUT  %s (30 min)" % s.name)
            bad += 1
            continue
        if proc.returncode != 0:
            print("  FAILED   %s (exit %d)" % (s.name, proc.returncode))
            print(("\n".join(proc.stderr.splitlines()[-15:])).rstrip())
            bad += 1
            continue
        try:
            rows = validate(proc.stdout, s.name)
        except FormatError as e:
            print("  BAD CSV  %s" % s.name)
            print("    %s" % str(e).replace("\n", "\n    "))
            bad += 1
            continue
        for r in rows:
            r["kernel_profile"] = profile
            r["source"] = s.name
        all_rows.extend(rows)
        print("           %d rows OK" % len(rows))

    if not all_rows:
        print("\nbench: no valid rows collected", file=sys.stderr)
        return 1

    outf = outdir / ("bench_%s.csv" % stamp)
    with open(outf, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(HEADER + ["kernel_profile", "source"])
        for r in all_rows:
            w.writerow([r[k] for k in HEADER] + [r["kernel_profile"], r["source"]])

    print("\nwrote %d rows -> %s" % (len(all_rows), outf))
    if kasan:
        print("  STAMPED kernel_profile=debug -- NOT publishable.")

    # A quick per-metric view, so a wrong order of magnitude is visible now
    # rather than when the figure is drawn.
    print("\n  %-28s %-14s %12s %10s" % ("metric", "config", "value", "unit"))
    for r in sorted(all_rows, key=lambda r: (r["metric"], r["config"])):
        print("  %-28s %-14s %12.4g %10s"
              % (r["metric"][:28], r["config"][:14], r["_value"], r["unit"]))

    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
