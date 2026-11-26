"""Adverse-selection ledger (A7): where do our maker fills get picked off?

Reads twin outputs (``doppel-twin --out``) and attributes maker-fill markouts to the conditions
the fill happened under, one factor at a time:

- ``via``: how the fill was simulated (``trade`` print, book ``cross``, ``inferred`` unprinted volume)
- ``tte``: seconds to window end at the fill (late fills face the most informed flow)
- ``px``: distance of the fill price from 50c (near-certain outcomes vs coin flips)
- ``side``: buy / sell

Markout k = (mid at fill + k s) - fill px for buys, the reverse for sells, in cents, so negative =
adverse. Per level: fills, contracts, contract-weighted markout and its window-clustered standard
error (fills in one window share a price path, so they are not independent), and the markout
dollars the level cost. Read-only on the twin docs; it never changes a genome.

    doppel-toxicity OUT.json [OUT2.json ...] [--k 30] [--out LEDGER.json]
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import defaultdict
from pathlib import Path

import orjson

TTE_EDGES = (30, 60, 120, 240)          # seconds to window end
PX_EDGES = (0.1, 0.25, 0.4)             # |px - 0.5|


def _bucket(x: float, edges: tuple, unit: str) -> str:
    lo = 0
    for e in edges:
        if x < e:
            return f"{unit}{lo}-{e}"
        lo = e
    return f"{unit}>={lo}"


def maker_fills(docs: list[dict]) -> list[dict]:
    """Flatten maker fills with their conditions; one row per fill."""
    rows = []
    for d in docs:
        gid = (d.get("genome") or {}).get("id")
        for w in d.get("windows") or []:
            if not w.get("eligible"):
                continue
            dur = w["end"] - w["start"]
            for f in w.get("fills") or []:
                if f.get("role") != "maker":
                    continue
                rows.append({
                    "genome": gid, "window": f"{gid}:{w['slug']}", "qty": f["qty"], "mo": f.get("markout_c") or {},
                    "via": f.get("via") or "?", "side": f["side"],
                    "tte": _bucket(dur - f["t"], TTE_EDGES, "tte "),
                    "px": _bucket(abs(f["px"] - 0.5), PX_EDGES, "|p-.5| "),
                })
    return rows


def clustered(rows: list[dict], k: str) -> dict:
    """Contract-weighted mean markout with a window-clustered (ratio-estimator) SE."""
    by_w: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    n = 0
    for r in rows:
        m = r["mo"].get(k)
        if m is None:
            continue
        n += 1
        by_w[r["window"]][0] += r["qty"] * m
        by_w[r["window"]][1] += r["qty"]
    W = sum(w for _, w in by_w.values())
    if not W:
        return {"fills": n, "contracts": 0.0, "markout_c": None, "se_c": None, "markout_usd": 0.0, "windows": 0}
    mean = sum(y for y, _ in by_w.values()) / W
    c = len(by_w)
    se = math.sqrt(c / (c - 1) * sum((y - mean * w) ** 2 for y, w in by_w.values())) / W if c > 1 else None
    return {"fills": n, "contracts": round(W, 4), "markout_c": round(mean, 4),
            "se_c": round(se, 4) if se is not None else None,
            "markout_usd": round(mean * W / 100.0, 4), "windows": c}


def ledger(docs: list[dict], ks=("1", "5", "30")) -> dict:
    rows = maker_fills(docs)
    out: dict = {"n_fills": len(rows), "all": {k: clustered(rows, k) for k in ks}, "by": {}}
    for factor in ("via", "tte", "px", "side"):
        levels: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            levels[r[factor]].append(r)
        out["by"][factor] = {lv: {k: clustered(rs, k) for k in ks} for lv, rs in sorted(levels.items())}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-toxicity")
    ap.add_argument("docs", type=Path, nargs="+", help="doppel-twin --out files")
    ap.add_argument("--k", default="30", help="markout horizon (s) to print; all are in --out")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    res = ledger([orjson.loads(p.read_bytes()) for p in a.docs])
    if a.out:
        a.out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2))

    def line(name: str, s: dict) -> str:
        se = f"±{s['se_c']:.2f}" if s["se_c"] is not None else "     "
        mo = f"{s['markout_c']:+.2f}" if s["markout_c"] is not None else "  n/a"
        return f"  {name:18} fills {s['fills']:>5}  ctr {s['contracts']:>9.1f}  mk{a.k}s {mo} {se}c  ${s['markout_usd']:+.2f}"
    print(f"maker fills {res['n_fills']}")
    print(line("ALL", res["all"][a.k]))
    for factor, levels in res["by"].items():
        print(factor)
        for lv, s in levels.items():
            print(line(lv, s[a.k]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
