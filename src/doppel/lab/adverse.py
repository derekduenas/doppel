"""Fill-conditional edge (Astra review #1): what a PM US maker's fill is worth, versus what fair value
says it is worth.

For every public PM US BTC trade print (WS streams only; the aggressor is ``taker.side``):
  - taker SELL hits a bid at p   -> the maker BOUGHT YES at p: maker pnl = outcome - p; model edge = fair - p
  - taker BUY lifts an ask at p  -> the maker SOLD YES at p:   maker pnl = p - outcome; model edge = p - fair
fair = fair-v1 from raw BRTI known BEFORE the print (decision second = floor(print receive time) - 1),
ptb / outcome from the published settlement series (A(S-1), A(E-1)).

Bucketed by time to close and by model edge. ``adverse_c`` = realised maker pnl - model edge, per
contract (cents, quantity-weighted). A model that is right UNCONDITIONALLY can still be wrong
CONDITIONAL ON BEING FILLED; this measures exactly that gap on the venue we will quote on.
Prints are a lower bound of venue volume (FINDINGS: 28-85% coverage); this is a study of the
fills that ARE observed, not a twin verdict.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
import orjson

from ..tape.segment import read_segment
from .brti_import import load_raw
from .fair import fair_up_raw, to_array
from .pmus_tape import WS_STREAMS
from .refprice import load_series

LEN = {"5m": 300, "15m": 900}
TTE_BUCKETS = ((0, 30), (30, 60), (60, 120), (120, 240), (240, 900))
EDGE_BUCKETS = ((-100, -5), (-5, -2), (-2, 0), (0, 2), (2, 5), (5, 100))


def _ts(s: str) -> float:
    base, _, frac = s.rstrip("Z").partition(".")
    return datetime.fromisoformat(base + "+00:00").timestamp() + (float("0." + frac) if frac else 0.0)


def iter_prints(root: Path, hz: str = "5m", asset: str = "btc"):
    pref = ("cpc-btc" if asset == "btc" else f"cpc{asset}-{asset}") + f"-updown-{hz}-"
    seen = set()
    for st in WS_STREAMS:
        for f in sorted((root / st).glob("*/*.dtape.zst")):
            for rec in read_segment(f, verify=True):
                raw = rec["raw"]
                if not raw or '"trade"' not in raw:
                    continue
                t = orjson.loads(raw).get("trade") or {}
                slug = t.get("marketSlug", "")
                if not slug.startswith(pref):
                    continue
                key = t.get("id") or (slug, t.get("tradeTime"), t["price"]["value"], t["quantity"]["value"])
                if key in seen:
                    continue
                seen.add(key)
                start = int(datetime.strptime(slug[-16:-1], "%Y-%m-%d-%H%M").replace(
                    tzinfo=__import__("datetime").timezone.utc).timestamp())
                yield {"slug": slug, "start": start, "end": start + LEN[hz],
                       "recv": rec["tw"] / 1e9, "price": float(t["price"]["value"]),
                       "qty": float(t["quantity"]["value"]),
                       "taker_side": (t.get("taker") or {}).get("side")}


def study(root: Path, hz: str = "5m") -> dict:
    raw = load_raw(root)
    ser = load_series(root, "crypto:btc")
    t0, r = to_array(raw)
    cells: dict = defaultdict(lambda: {"q": 0.0, "pnl": 0.0, "edge": 0.0, "n": 0})
    total = {"q": 0.0, "pnl": 0.0, "edge": 0.0, "n": 0}
    skipped = defaultdict(int)
    for p in iter_prints(root, hz):
        po, pc = ser.get(p["start"] - 1), ser.get(p["end"] - 1)
        if po is None or pc is None:
            skipped["unsettled"] += 1
            continue
        d = int(p["recv"]) - 1
        if not (p["start"] <= d < p["end"]):
            skipped["outside_window"] += 1
            continue
        fv = fair_up_raw(r, t0, po, d, p["end"])
        if fv is None:
            skipped["no_fair"] += 1
            continue
        y = 1.0 if pc >= po else 0.0
        side = p["taker_side"]
        if side == "ORDER_SIDE_SELL":      # maker bought YES
            pnl, edge = y - p["price"], fv - p["price"]
        elif side == "ORDER_SIDE_BUY":     # maker sold YES
            pnl, edge = p["price"] - y, p["price"] - fv
        else:
            skipped["no_side"] += 1
            continue
        tte = p["end"] - d
        tb = next(b for b in TTE_BUCKETS if b[0] <= tte < b[1]) if tte < 900 else TTE_BUCKETS[-1]
        ec = 100 * edge
        eb = next((b for b in EDGE_BUCKETS if b[0] <= ec < b[1]), EDGE_BUCKETS[-1])
        for key in ((tb, eb), (tb, "all"), ("all", eb)):
            c = cells[key]
            c["q"] += p["qty"]; c["pnl"] += p["qty"] * pnl; c["edge"] += p["qty"] * edge; c["n"] += 1
        total["q"] += p["qty"]; total["pnl"] += p["qty"] * pnl; total["edge"] += p["qty"] * edge; total["n"] += 1

    def fmt(c):
        if not c["q"]:
            return None
        return {"n": c["n"], "contracts": round(c["q"], 1),
                "maker_pnl_c": round(100 * c["pnl"] / c["q"], 3),
                "model_edge_c": round(100 * c["edge"] / c["q"], 3),
                "adverse_c": round(100 * (c["pnl"] - c["edge"]) / c["q"], 3)}
    out = {"hz": hz, "built_tw": time.time_ns(), "total": fmt(total), "skipped": dict(skipped),
           "by_tte": {f"{a}-{b}s": fmt(cells[((a, b), "all")]) for a, b in TTE_BUCKETS},
           "by_model_edge": {f"{a}..{b}c": fmt(cells[("all", (a, b))]) for a, b in EDGE_BUCKETS},
           "grid": {f"{tb[0]}-{tb[1]}s|{eb[0]}..{eb[1]}c": fmt(v) for (tb, eb), v in cells.items()
                    if isinstance(tb, tuple) and isinstance(eb, tuple) and v["q"]}}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-adverse")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--hz", default="5m", choices=list(LEN))
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    res = study(a.data, a.hz)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2))
    print("total", res["total"], "skipped", res["skipped"])
    for k in ("by_tte", "by_model_edge"):
        print(k)
        for b, v in res[k].items():
            print(f"  {b:>12} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
