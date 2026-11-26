"""fair-v1 vs the Polymarket US market price on BTC Up/Down windows.

Market source: OUR OWN PM US WS book tape (``pmus_ws*``; never the cached REST polls, E22), using the
latest book RECEIVED at or before
each decision second (as-of, no look-ahead). Do NOT use ``/v1/price-history`` for this. Its points
are not as-of: stamped prices often match the live book 1-3 minutes later (FINDINGS E21). The
price-history fetch below is kept only to reproduce that finding. At each decision second we score
  - Brier of the market mid ((long + 1 - short) / 2) vs fair-v1
  - a taker rule: buy Up when fair - long > fee(long) + margin, buy Down when
    (1 - fair) - short > fee(short) + margin; settle at the window outcome.
fee(p) = 0.095 * p * (1 - p) per contract (PM US BTC 5m feeCoefficient, E8).
This is a SIGNAL check on display prices, not an execution backtest: displayed size, queue and
latency are not modeled, and minute points are coarse. Positive results only nominate H2 for
the real tape (PM US WS books, never the cached REST polls).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import aiohttp
import numpy as np
import orjson

from ..tape.segment import SegmentWriter, read_segment
from ..tape.sources.base import SSL_CTX, UA
from .brti_import import load_raw
from .fair import fair_up_raw, to_array
from .refprice import load_series

URL = "https://gateway.polymarket.us/v1/price-history"
FEE = 0.095


def slug(start: int) -> str:
    return "cpc-btc-updown-5m-" + time.strftime("%Y-%m-%d-%H%M", time.gmtime(start)) + "z"


def cached(root: Path) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in sorted((root / "pmus_price_history").glob("*/*.dtape.zst")):
        for rec in read_segment(f, verify=True):
            m = rec.get("meta") or {}
            if m.get("status") == 200:
                out[m["slug"]] = orjson.loads(rec["raw"]).get("history") or []
    return out


async def fetch_missing(root: Path, starts: list[int], have: dict, rps: float = 5.0) -> None:
    w = SegmentWriter(root, "pmus_price_history")
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX),
                                     timeout=aiohttp.ClientTimeout(total=20),
                                     headers={"user-agent": UA}) as s:
        for st in starts:
            sl = slug(st)
            if sl in have:
                continue
            q = {"symbol": sl, "timestamp.startTimestamp": st - 60,
                 "timestamp.endTimestamp": st + 360, "fidelity": 1}
            async with s.get(URL, params=q) as r:
                body, status = await r.text(), r.status
            w.append(body, meta={"slug": sl, "status": status, "start": st})
            if status == 200:
                have[sl] = orjson.loads(body).get("history") or []
            await asyncio.sleep(1.0 / rps)
    w.close()


def tape_books(root: Path) -> dict[str, list[tuple[float, float, float]]]:
    """slug -> sorted [(receipt second, best bid, best ask)] from the PM US WS book streams.
    Never REST: `pmus_books` is cached, 8 s stale at the median (FINDINGS E22)."""
    from .pmus_tape import ws_books
    return {k: [(t, b, a) for t, b, _bq, a, _aq in v]
            for k, v in ws_books(root, "cpc-btc-updown-5m").items()}


def asof(rows: list[tuple[float, float, float]], t: float, max_age: float = 3):
    import bisect
    i = bisect.bisect_right(rows, (t, 2.0, 2.0)) - 1
    if i < 0 or t - rows[i][0] > max_age:
        return None
    return rows[i][1], rows[i][2]


def evaluate(root: Path, margin: float = 0.0, taus=(60, 120, 180, 240, 270)) -> dict:
    raw = load_raw(root)
    ser = load_series(root, "crypto:btc")
    t0, r = to_array(raw)
    lo, hi = max(min(raw), min(ser)) + 400, min(max(raw), max(ser)) - 300
    starts = list(range((lo // 300 + 1) * 300, hi, 300))
    books = tape_books(root)
    res: dict = {"windows": 0, "by_tau": {}, "source": "pmus_ws* tape (WS only), as-of receipt <= decision, age <= 3 s"}
    acc = {t: {"m": [], "f": [], "y": [], "pnl": [], "trades": 0} for t in taus}
    for st in starts:
        end = st + 300
        po, pc = ser.get(st - 1), ser.get(end - 1)
        bk = books.get(slug(st))
        if po is None or pc is None or not bk:
            continue
        res["windows"] += 1
        y = 1 if pc >= po else 0
        for tau in taus:
            q = asof(bk, st + tau)
            if q is None:
                continue
            bid, ask = q
            ask_up, ask_dn = ask, 1.0 - bid  # buy Up at the ask; buy Down = sell Up at the bid
            if not (0 < ask_up < 1 and 0 < ask_dn < 1):
                continue
            fv = fair_up_raw(r, t0, po, st + tau, end)
            if fv is None:
                continue
            a = acc[tau]
            a["m"].append((ask_up + 1 - ask_dn) / 2)
            a["f"].append(fv)
            a["y"].append(y)
            if fv - ask_up > FEE * ask_up * (1 - ask_up) + margin:
                a["pnl"].append(y - ask_up - FEE * ask_up * (1 - ask_up))
                a["trades"] += 1
            elif (1 - fv) - ask_dn > FEE * ask_dn * (1 - ask_dn) + margin:
                a["pnl"].append((1 - y) - ask_dn - FEE * ask_dn * (1 - ask_dn))
                a["trades"] += 1
    for tau, a in acc.items():
        if not a["y"]:
            continue
        y = np.array(a["y"])
        m, f = np.clip(np.array(a["m"]), 1e-4, 1 - 1e-4), np.clip(np.array(a["f"]), 1e-4, 1 - 1e-4)
        pnl = np.array(a["pnl"])
        res["by_tau"][str(tau)] = {
            "n": int(len(y)), "brier_market": round(float(np.mean((m - y) ** 2)), 5),
            "brier_fair_v1": round(float(np.mean((f - y) ** 2)), 5),
            "trades": a["trades"],
            "pnl_per_contract": round(float(pnl.mean()), 4) if len(pnl) else None,
            "pnl_t": round(float(pnl.mean() / (pnl.std(ddof=1) / np.sqrt(len(pnl)))), 2) if len(pnl) > 2 and pnl.std() > 0 else None,
            "hit_rate": round(float((pnl > 0).mean()), 3) if len(pnl) else None}
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-vsmarket")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--margin", type=float, default=0.0)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    res = evaluate(a.data, a.margin)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2))
    print(f"windows={res['windows']} margin={a.margin}")
    for tau, v in res["by_tau"].items():
        print(f"  tau={tau:>3}s n={v['n']:>4} brier market={v['brier_market']:.4f} fair_v1={v['brier_fair_v1']:.4f} "
              f"| taker trades={v['trades']:>3} pnl/ct={v['pnl_per_contract']} t={v['pnl_t']} hit={v['hit_rate']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
