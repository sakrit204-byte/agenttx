#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""
tools/bench/plot.py --- figures from the benchmark CSVs.  Fragment P4-14.

    make figures                      # results/*.csv -> paper/figs/*.png

WHAT IT REFUSES TO DO
---------------------
Plot a row tagged DEBUG-KERNEL. lib_bench.sh marks every row produced on a
KASAN/lockdep kernel, and a figure carries no such marking once it is in a
paper -- an axis label cannot say "this measured KASAN". So those rows are
dropped, loudly, and if that leaves a figure empty the figure is not written.

It also refuses to plot a mean without a spread when n > 1. The CSV contract
makes stddev mandatory for a reason: inside a KVM guest the tail is the
interesting part, and a bar chart of means hides exactly the bimodality that
would tell you something is wrong.

Owner: P4.
"""
from __future__ import annotations

import argparse
import csv
import glob
import sys
from collections import defaultdict
from pathlib import Path

PALETTE = {
    "baseline": "#8b9aad", "no_overlay": "#8b9aad", "hooks_off": "#8b9aad",
    "enforcing": "#a371f7", "overlay": "#58a6ff", "hooks_on": "#d29922",
}
DARK = "#0b0f14"
INK = "#e6edf3"
GRID = "#222c3a"


def load(indir: str) -> tuple[list[dict], int]:
    rows, dropped = [], 0
    for f in sorted(glob.glob(str(Path(indir) / "*.csv"))):
        raw = open(f, newline="").read().splitlines()
        reader = csv.DictReader(raw)
        for ln, r in zip(raw[1:], reader):
            # Belt and braces: check the PARSED notes field and the raw line.
            #
            # A row whose notes contained an unquoted comma used to split into
            # extra columns, putting the DEBUG-KERNEL marker somewhere
            # DictReader files under None -- so the check passed and a figure
            # was drawn from KASAN numbers. lib_bench.sh now quotes the field;
            # this scans the raw text as well, because the consequence of
            # missing it is a wrong number in a paper.
            extra = " ".join(str(v) for v in (r.get(None) or []))
            if "DEBUG-KERNEL" in ((r.get("notes") or "") + extra + ln):
                dropped += 1
                continue
            try:
                r["value"] = float(r["value"])
                r["stddev"] = float(r["stddev"] or 0)
                r["n"] = int(r["n"] or 0)
            except (TypeError, ValueError):
                continue
            rows.append(r)
    return rows, dropped


def style(ax, fig, title, ylabel):
    fig.patch.set_facecolor(DARK)
    ax.set_facecolor(DARK)
    ax.set_title(title, color=INK, fontsize=12, pad=14, loc="left")
    ax.set_ylabel(ylabel, color="#8b9aad", fontsize=10)
    ax.tick_params(colors="#8b9aad", labelsize=9)
    for s in ax.spines.values():
        s.set_color(GRID)
    ax.grid(axis="y", color=GRID, linewidth=0.7, alpha=0.7)
    ax.set_axisbelow(True)


def grouped_bars(rows, metrics, title, ylabel, out, logy=False):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    data = defaultdict(dict)
    errs = defaultdict(dict)
    for r in rows:
        if r["metric"] in metrics:
            data[r["metric"]][r["config"]] = r["value"]
            errs[r["metric"]][r["config"]] = r["stddev"]
    metrics = [m for m in metrics if m in data]
    if not metrics:
        return False

    configs = []
    for m in metrics:
        for c in data[m]:
            if c not in configs:
                configs.append(c)

    fig, ax = plt.subplots(figsize=(8.2, 4.4))
    w = 0.8 / max(len(configs), 1)
    x = np.arange(len(metrics))
    for i, c in enumerate(configs):
        vals = [data[m].get(c, 0) for m in metrics]
        es = [errs[m].get(c, 0) for m in metrics]
        ax.bar(x + i * w - 0.4 + w / 2, vals, w * 0.92,
               yerr=es, capsize=3, label=c,
               color=PALETTE.get(c, "#3fb950"),
               error_kw={"ecolor": "#5d6b7d", "linewidth": 1})
    ax.set_xticks(x)
    ax.set_xticklabels([m.replace("_", " ") for m in metrics], rotation=12,
                       ha="right")
    if logy:
        ax.set_yscale("log")
    leg = ax.legend(frameon=False, fontsize=9)
    for t in leg.get_texts():
        t.set_color("#c3cedb")
    style(ax, fig, title, ylabel)
    fig.tight_layout()
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160, facecolor=DARK)
    plt.close(fig)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--in", dest="inp", default="results")
    ap.add_argument("--out", default="paper/figs")
    a = ap.parse_args(argv)

    try:
        import matplotlib  # noqa: F401
    except ImportError:
        print("plot: matplotlib missing (apt install python3-matplotlib)",
              file=sys.stderr)
        return 1

    # Validate before plotting.
    #
    # P4-11 built an enforcer for exactly this and I plotted without running
    # it. The malformed row it would have caught -- an unquoted comma in
    # `notes` -- hid a DEBUG-KERNEL marker and produced a figure from numbers
    # that measured KASAN. A checker nobody runs is not a checker.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import run as bench_run
        bad = 0
        for f in sorted(glob.glob(str(Path(a.inp) / "*.csv"))):
            if bench_run.main(["--check", f]) != 0:
                bad += 1
        if bad:
            print(f"plot: {bad} CSV file(s) failed the format check; "
                  "refusing to draw from them", file=sys.stderr)
            return 1
    except Exception as e:                     # never let the check itself break plotting
        print(f"plot: could not run the format check ({e}); continuing",
              file=sys.stderr)

    rows, dropped = load(a.inp)
    if dropped:
        print(f"plot: DROPPED {dropped} row(s) measured on a debug kernel.",
              file=sys.stderr)
        print("      A figure cannot carry the caveat that a CSV row can.",
              file=sys.stderr)
    if not rows:
        print("plot: no paper-grade rows. Boot the perf kernel and re-run "
              "`make bench`.", file=sys.stderr)
        return 1

    made = []
    if grouped_bars(rows,
                    ["baseline_syscall", "tx_stat_latency", "tx_begin_latency",
                     "tx_begin_abort_cycle"],
                    "Transaction lifecycle cost (P1-14)", "nanoseconds (log)",
                    f"{a.out}/fig-core-latency.png", logy=True):
        made.append("fig-core-latency.png")

    if grouped_bars(rows,
                    ["unhooked_syscall", "file_open_syscall",
                     "socket_sendmsg_syscall"],
                    "What the LSM hooks cost a process that is NOT transacting (P3-12)",
                    "nanoseconds", f"{a.out}/fig-hook-overhead.png"):
        made.append("fig-hook-overhead.png")

    if grouped_bars(rows, ["write_latency"],
                    "Copy-on-write overhead (P2-11)", "nanoseconds",
                    f"{a.out}/fig-cow-overhead.png"):
        made.append("fig-cow-overhead.png")

    for m in made:
        print(f"  wrote {a.out}/{m}")
    if not made:
        print("plot: nothing to draw from these rows", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
