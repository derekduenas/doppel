"""W5 cross-venue: PM US BTC 15m vs Kalshi KXBTC15M.

Hypothesis: both settle on the same CF Benchmarks BRTI 60 s average against the same kind of opening
reference on the same 15-minute grid, so they are the SAME contract on two US venues. If so, buying
YES on one and NO on the other locks $1 per pair, and any price gap beyond both fees is riskless.

1. ``equivalence``: per window compare PM US priceToBeat / settlementPrice / outcome with Kalshi
   floor_strike / result (public APIs; cached to tape stream ``xvenue_meta``).
2. ``spread_scan``: per second (as-of our receive time), using our recorded books, compute
     A = kalshi_yes_bid - pmus_yes_ask - fees   (buy YES on PM US + buy NO on Kalshi)
     B = pmus_yes_bid - kalshi_yes_ask - fees   (buy YES on Kalshi + sell YES on PM US)
   with size = min(top-of-book depths). Fees: PM US taker coef*p*(1-p) per contract (cent half-even,
   approximated per contract here); Kalshi taker ceil-to-cent of 0.07*C*p*(1-p) per order (we
   charge per-contract 0.07*p*(1-p) rounded up at the order size actually available).
   PM US books come from the WS streams only (REST is cached, E22). Kalshi books are REST polls whose
   freshness is not yet verified (a Kalshi WS recorder is the fix).
   This is TAKER-TAKER arbitrage on displayed quotes. It is optimistic about simultaneity (two legs, no
   leg risk modelled) and pessimistic about price (no maker improvement). It is a measurement, not a P&L.
"""

from __future__ import annotations

import argparse
import bisect
import math
import sys
import time
from datetime import datetime
from pathlib import Path

import orjson
import urllib.request

from ..tape.segment import read_segment
from ..tape.sources.base import SSL_CTX, UA

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
PMUS = "https://gateway.polymarket.us/v1"
PMUS_COEF = 0.095  # gateway feeCoefficient; docs: API takers 0.0695 (+70% rebate to 2026-12-31). Override: --pmus-coef
PMUS_REBATE = 0.0  # fraction of the PM US taker fee rebated (API promo: 0.70)
KALSHI_COEF = 0.07


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"user-agent": UA})
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=20) as r:
        return orjson.loads(r.read())


def _iso(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def pmus_fee(p: float, n: float = 1.0, *, coef: float = PMUS_COEF, rebate: float = PMUS_REBATE) -> float:
    """Dollars for n contracts at price p, cent half-even (Python round = banker's)."""
    from ..twin.engine import fee_usd
    return round(fee_usd(coef, p, n) * (1 - rebate), 4)


def kalshi_fee(p: float, n: float = 1.0) -> float:
    """Dollars for an n-contract taker order at p: ceil to the cent of 0.07*n*p*(1-p)."""
    return math.ceil(KALSHI_COEF * n * p * (1 - p) * 100 - 1e-9) / 100


def load_books(root: Path, stream: str, key_filter: str) -> dict[str, list]:
    """key -> sorted [(recv_s_float, raw dict)] for REST book polls whose key contains key_filter."""
    out: dict[str, list] = {}
    for f in sorted((root / stream).glob("*/*.dtape.zst")):
        for rec in read_segment(f, verify=True):
            m = rec.get("meta") or {}
            if m.get("status") != 200 or key_filter not in m.get("key", ""):
                continue
            out.setdefault(m["key"], []).append((rec["tw"] / 1e9, orjson.loads(rec["raw"])))
    for v in out.values():
        v.sort(key=lambda x: x[0])
    return out


def pmus_top(d: dict):
    md = d.get("marketData") or {}
    b, o = md.get("bids") or [], md.get("offers") or []
    if not b or not o:
        return None
    return (float(b[0]["px"]["value"]), float(b[0]["qty"]), float(o[0]["px"]["value"]), float(o[0]["qty"]))


def kalshi_top(d: dict):
    ob = d.get("orderbook_fp") or d.get("orderbook") or {}
    yes = ob.get("yes_dollars") or []
    no = ob.get("no_dollars") or []
    if not yes or not no:
        return None
    yb = max(yes, key=lambda x: float(x[0]))
    nb = max(no, key=lambda x: float(x[0]))
    return (float(yb[0]), float(yb[1]), round(1.0 - float(nb[0]), 4), float(nb[1]))  # yes bid, qty, yes ask, qty


def kalshi_meta(tickers: list[str], cache: Path) -> dict[str, dict]:
    meta = orjson.loads(cache.read_bytes()) if cache.exists() else {}
    for t in tickers:
        if t in meta and meta[t].get("result") not in (None, ""):
            continue
        try:
            m = _get(f"{KALSHI}/markets/{t}")["market"]
        except Exception:  # noqa: BLE001
            continue
        meta[t] = {"open": _iso(m["open_time"]), "close": _iso(m["close_time"]),
                   "strike": m.get("floor_strike"), "result": m.get("result"),
                   "expiration_value": m.get("expiration_value")}
        time.sleep(0.15)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(orjson.dumps(meta))
    return meta


def pmus_meta(start: int) -> dict | None:
    slug = "btc-updown-15m-" + time.strftime("%Y-%m-%d-%H%M", time.gmtime(start)) + "z"
    try:
        s = orjson.dumps(_get(f"{PMUS}/events/slug/{slug}")).decode()
    except Exception:  # noqa: BLE001
        return None
    import re
    ptb = re.search(r'"priceToBeat":\{"value":"([0-9.]+)"', s)
    sp = re.search(r'"settlementPrice":\{"value":"([0-9.]+)"', s)
    return {"ptb": float(ptb.group(1)) if ptb else None, "settle": float(sp.group(1)) if sp else None}


def asof(rows: list, t: float, max_age: float = 3.0, with_time: bool = False):
    i = bisect.bisect_right([r[0] for r in rows], t) - 1
    if i < 0 or t - rows[i][0] > max_age:
        return None
    return (rows[i][0], rows[i][1]) if with_time else rows[i][1]


def episodes(series: list[tuple[int, float, float]], persist: int) -> list[dict]:
    """Contiguous runs of positive net edge lasting >= ``persist`` consecutive seconds. Each episode
    is valued ONCE at the second it qualifies (the ``persist``-th second: before that it could not be
    confirmed), edge x size, so no liquidity is re-counted (audit A1-F4: it used the first second)."""
    out, run = [], []
    for s, e, q in series + [(10**12, -1.0, 0.0)]:
        if e > 0 and (not run or s == run[-1][0] + 1):
            run.append((s, e, q))
            continue
        if len(run) >= persist:
            q0 = run[persist - 1]
            out.append({"start": run[0][0], "secs": len(run), "edge_c": round(100 * q0[1], 2),
                        "size": q0[2], "dollars": round(q0[1] * q0[2], 2)})
        run = [(s, e, q)] if e > 0 else []
    return out


def run(root: Path, lab: Path, max_age: float = 1.0, skew: float = 0.5, persist: int = 2, *,
        pmus_coef: float = PMUS_COEF, pmus_rebate: float = PMUS_REBATE) -> dict:
    if persist < 1 or pmus_coef < 0 or not 0 <= pmus_rebate <= 1:
        raise ValueError("invalid fee or persistence setting")
    # PM US books from the WS streams only (REST is cached and 8 s stale at the median, E22).
    from .pmus_tape import ws_books
    pm = {k: [(t, (b, bq, a, aq)) for t, b, bq, a, aq in v]
          for k, v in ws_books(root, "cpc-btc-updown-15m-").items()}
    ka = load_books(root, "kalshi_books", "KXBTC15M-")
    km = kalshi_meta(sorted(ka), lab / "xvenue" / "kalshi_meta.json")
    by_open = {v["open"]: t for t, v in km.items() if t in ka}
    res: dict = {"windows": [], "fees": {"pmus_coef": pmus_coef, "pmus_rebate": pmus_rebate},
                 "equivalence": {"checked": 0, "strike_match": 0, "result_match": 0, "excluded": []}}
    for slug, prow in sorted(pm.items()):
        start = int(datetime.strptime(slug[-16:-1], "%Y-%m-%d-%H%M").replace(
            tzinfo=__import__("datetime").timezone.utc).timestamp())
        t = by_open.get(start)
        if not t:
            continue
        krow = ka[t]
        meta = km[t]
        pmm = pmus_meta(start)
        eq = {}
        reason = None
        if meta.get("close") != start + 900:
            reason = "close_mismatch"
        elif not pmm or pmm.get("ptb") is None or meta.get("strike") is None:
            reason = "missing_strike"
        if pmm and pmm["ptb"] is not None and meta.get("strike") is not None:
            res["equivalence"]["checked"] += 1
            eq["strike_diff"] = round(pmm["ptb"] - float(meta["strike"]), 4)
            strikes_match = abs(pmm["ptb"] - float(meta["strike"])) < 0.005
            res["equivalence"]["strike_match"] += strikes_match
            if not strikes_match and reason is None:
                reason = "strike_mismatch"
            if reason is None and pmm["settle"] is not None and meta.get("result") in ("yes", "no"):
                pm_up = pmm["settle"] >= pmm["ptb"]
                ok = pm_up == (meta["result"] == "yes")
                res["equivalence"]["result_match"] += ok
                if not ok:
                    res["equivalence"].setdefault("mismatches", []).append(
                        {"slug": slug, "kalshi": t, "ptb": pmm["ptb"], "pm_settle": pmm["settle"],
                         "kalshi_strike": meta.get("strike"), "kalshi_result": meta["result"],
                         "kalshi_expiration_value": meta.get("expiration_value")})
        if reason is not None:
            res["equivalence"]["excluded"].append({"slug": slug, "kalshi": t, "reason": reason, **eq})
            continue
        A, B, gaps = [], [], []
        for s in range(start + 5, start + 900 - 5):
            pt, kt = asof(prow, s, max_age, True), asof(krow, s, max_age, True)
            if pt is None or kt is None or abs(pt[0] - kt[0]) > skew:
                continue
            p, k = pt[1], kalshi_top(kt[1])
            if p is None or k is None:
                continue
            pb, pbq, pa, paq = p
            kb, kbq, ka_, kaq = k
            gaps.append((pb + pa) / 2 - (kb + ka_) / 2)
            n = max(1.0, min(paq, kbq))
            a = kb - pa - (pmus_fee(pa, n, coef=pmus_coef, rebate=pmus_rebate) + kalshi_fee(1 - kb, n)) / n
            n2 = max(1.0, min(pbq, kaq))
            b = pb - ka_ - (pmus_fee(pb, n2, coef=pmus_coef, rebate=pmus_rebate) + kalshi_fee(ka_, n2)) / n2
            A.append((s, a, min(paq, kbq)))
            B.append((s, b, min(pbq, kaq)))
        if not A:
            continue
        ea, eb = episodes(A, persist), episodes(B, persist)
        late = [e for e in ea + eb if e["start"] >= start + 840]
        pos = [x for x in A + B if x[1] > 0]
        res["windows"].append({
            "episodes": len(ea) + len(eb), "episodes_last60s": len(late),
            "episode_dollars": round(sum(e["dollars"] for e in ea + eb), 2),
            "episode_dollars_ex_last60s": round(sum(e["dollars"] for e in ea + eb if e not in late), 2),
            "top_episodes": sorted(ea + eb, key=lambda e: -e["dollars"])[:3],
            "slug": slug, "kalshi": t, **eq, "seconds": len(A),
            "mid_gap_c_p50": round(100 * sorted(gaps)[len(gaps) // 2], 2),
            "mid_gap_c_absmax": round(100 * max(abs(g) for g in gaps), 2),
            "arb_seconds": len(pos),
            "arb_best_c": round(100 * max((x[1] for x in A + B)), 2),
            "arb_dollars_upper": round(sum(x[1] * x[2] for x in pos), 2)})
    w = res["windows"]
    res["params"] = {"max_age_s": max_age, "max_skew_s": skew, "persist_s": persist}
    res["summary"] = {"windows": len(w), "arb_seconds": sum(x["arb_seconds"] for x in w),
                      "episodes": sum(x["episodes"] for x in w),
                      "episode_dollars": round(sum(x["episode_dollars"] for x in w), 2),
                      "episode_dollars_ex_last60s": round(sum(x["episode_dollars_ex_last60s"] for x in w), 2),
                      "windows_with_arb": sum(x["arb_seconds"] > 0 for x in w),
                      "arb_dollars_upper_total": round(sum(x["arb_dollars_upper"] for x in w), 2),
                      "note": "upper bound: counts every positive second at displayed size; overlapping "
                              "seconds re-count the same liquidity; no leg risk or latency modelled"}
    if res["equivalence"].get("mismatches"):
        # Keep the observed prices and diagnostics. One contrary settlement invalidates
        # a guaranteed-dollar estimate over this series, including the matching windows.
        res["summary"].update(episode_dollars=None, episode_dollars_ex_last60s=None,
                              arb_dollars_upper_total=None, note="Observed settlement conflict in matched contracts")
    return res


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-xvenue")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--lab", type=Path, required=True)
    ap.add_argument("--pmus-coef", type=float, default=PMUS_COEF)
    ap.add_argument("--pmus-rebate", type=float, default=0.0, help="API promo 0.70 through 2026-12-31")
    a = ap.parse_args(argv)
    res = run(a.data, a.lab, pmus_coef=a.pmus_coef, pmus_rebate=a.pmus_rebate)
    tag = f"c{a.pmus_coef:g}-r{a.pmus_rebate:g}"
    out = a.lab / "xvenue" / f"xvenue-btc15m-{tag}-{time.strftime('%Y%m%dT%H%MZ', time.gmtime())}.json"
    out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2))
    print("equivalence:", res["equivalence"])
    print("summary:", res["summary"])
    for x in res["windows"][-12:]:
        print(x)
    print("->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
