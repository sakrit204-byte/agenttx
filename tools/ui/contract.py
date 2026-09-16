# SPDX-License-Identifier: GPL-2.0
"""
tools/ui/contract.py --- read the frozen contract, do not restate it.

Every name, class and ioctl number the dashboard shows is parsed out of
include/agenttx.h at startup rather than copied into Python.  This is not
tidiness: a UI that hardcodes "reversible, deferrable, compensable,
irrevocable" keeps rendering confidently after a contract-change PR
reorders them, and the picture is then wrong in a way nobody can see.  If
the header and this file disagree, the dashboard fails to start.

Owner: P4 (tooling).
"""
from __future__ import annotations

import re
import struct
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HEADER = REPO / "include" / "agenttx.h"


def _names(src: str, macro: str) -> list[str]:
    """Pull a TX_*_NAMES brace list out of the header."""
    m = re.search(rf"#define\s+{macro}\s+(.*?)(?=\n#define|\n\n)", src, re.S)
    if not m:
        raise SystemExit(f"contract: {macro} not found in {HEADER}")
    return re.findall(r'"([^"]+)"', m.group(1))


def _define_int(src: str, name: str) -> int:
    m = re.search(rf"#define\s+{name}\s+(\w+)", src)
    if not m:
        raise SystemExit(f"contract: {name} not found in {HEADER}")
    v = m.group(1).rstrip("uU")
    return int(v, 0)


def _enum(src: str, enum_name: str) -> dict[str, int]:
    m = re.search(rf"enum\s+{enum_name}\s*\{{(.*?)\}}", src, re.S)
    if not m:
        raise SystemExit(f"contract: enum {enum_name} not found")
    out, nxt = {}, 0
    for line in m.group(1).splitlines():
        line = re.sub(r"/\*.*?\*/", "", line).strip().rstrip(",")
        if not line:
            continue
        mm = re.match(r"(\w+)\s*(?:=\s*(\w+))?$", line)
        if not mm:
            continue
        nxt = int(mm.group(2), 0) if mm.group(2) else nxt
        out[mm.group(1)] = nxt
        nxt += 1
    return out


class Contract:
    def __init__(self) -> None:
        src = HEADER.read_text()
        self.abi            = _define_int(src, "AGENTTX_ABI_VERSION")
        self.dev_path       = "/dev/agenttx"
        self.confidence_min = _define_int(src, "TX_CONFIDENCE_MIN")
        self.n_features     = _define_int(src, "TX_N_FEATURES")
        self.ioc_magic      = _define_int(src, "AGENTTX_IOC_MAGIC")

        self.class_names   = _names(src, "TX_CLASS_NAMES")
        self.state_names   = _names(src, "TX_STATE_NAMES")
        self.hook_names    = _names(src, "TX_HOOK_NAMES")
        self.verdict_names = _names(src, "TX_VERDICT_NAMES")

        self.classes  = _enum(src, "tx_class")
        self.states   = _enum(src, "tx_state")
        self.features = _enum(src, "tx_feature_idx")

        # The taxonomy ordering is load-bearing (severity ascends, so
        # "worst class so far" is a max()).  Prove it here rather than
        # trusting it: a reordered enum silently inverts every colour and
        # every threshold in the UI.
        expect = ["reversible", "deferrable", "compensable", "irrevocable"]
        if self.class_names != expect:
            raise SystemExit(
                f"contract: taxonomy order changed: {self.class_names}\n"
                "The UI encodes severity as ascending index. Fix the UI, "
                "do not silence this.")

    # --- ioctl numbers, computed the way the C macros compute them -------
    @staticmethod
    def _ioc(direction: int, typ: int, nr: int, size: int) -> int:
        return (direction << 30) | (size << 16) | (typ << 8) | nr

    # struct tx_stat_arg: u32 u32 u64 u32 u32 u64 u64 s64 u32 u32
    STAT_FMT = "<IIQIIQQqII"

    @property
    def stat_size(self) -> int:
        return struct.calcsize(self.STAT_FMT)

    @property
    def IOC_STAT(self) -> int:
        return self._ioc(3, self.ioc_magic, 0x04, self.stat_size)   # _IOWR

    @property
    def IOC_ABI(self) -> int:
        return self._ioc(2, self.ioc_magic, 0x06, 4)                # _IOR


CONTRACT = Contract()
