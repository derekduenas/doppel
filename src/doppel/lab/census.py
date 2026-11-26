"""Mechanism census for crypto up/down windows.

Per wallet (fills in crypto windows only, resolved tokens only, comparable days from G0):
- economics: gross trade P&L, fees, and the API's rebates/rewards over the same days, giving the
  economic P&L
- complete-set lock: per window, min(shares bought of Up, of Down) x (1 - average pair cost). This
  is the part of P&L that is locked regardless of outcome. Directional P&L is the rest.
- style: maker share of notional, taker time-to-end, price at entry, buy share
- label: transparent rules (below). Labels are hypotheses for the inversion lab, not verdicts.

Aggregate: $ and $/day by label, split candidates vs controls. This is the census report that the
M1 kill line reads.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import statistics as st
import sys
import time
from collections import defaultdict
from pathlib import Path

import orjson

from .fillstore import compact, iter_wallet_fills, wallet_tokens
from .ledger import DAY, TokenMap, _ts_to_block, api_points, g0_compare, g0_verdict, wallet_ledger

WINDOW = re.compile(r"^(btc|eth|sol|xrp)-updown-(5m|15m)-(\d+)$")
LEN = {"5m": 300, "15m": 900}


def window_of(info: dict):
    m = WINDOW.match(info.get("slug") or "")
    if not m:
        return None
    start = int(m.group(3))
    return {"asset": m.group(1), "hz": m.group(2), "start": start, "end": start + LEN[m.group(2)],
            "slug": info["slug"]}


def fingerprint(rows: list[dict], tmap: TokenMap, in_scope) -> dict | None:
    """Window-only fingerprint over the windows selected by ``in_scope(window) -> bool``.

    Window-centric accounting: a window is in scope as a whole (all of its fills count), so a
    position is never split across an interval edge. ``run`` selects windows whose END falls inside
    one of G0's snapshot intervals, the same intervals the API income and G0 use."""
    # idx -> [shares bought, cost, shares sold, proceeds]. Selling a token you never bought is a short
    # (on intl CTF: mint Up+Down for $1, sell both), so a complete set can be locked from either side:
    # buy both for < $1, or sell both for > $1 (audit 2026-11-26: sell-only minters were "directional").
    wins = defaultdict(lambda: {0: [0.0, 0.0, 0.0, 0.0], 1: [0.0, 0.0, 0.0, 0.0]})
    tok = defaultdict(lambda: [0.0, 0.0, 0.0])  # pos, cash, fees
    notional = maker = 0.0
    taker_tte, px_entry, buys, n = [], [], 0, 0
    for r in rows:
        info = tmap.m.get(r["token_id"]) or {}
        w = window_of(info)
        if not w or info.get("status") != "resolved" or not in_scope(w):
            continue
        sh, usd = float(r["amount_shares"]), float(r["amount_usdc"])
        n += 1
        notional += usd
        if r["order_type"] == "maker":
            maker += usd
        else:
            taker_tte.append(w["end"] - r["block_timestamp"])
        t = tok[r["token_id"]]
        t[2] += float(r.get("fee") or 0.0)
        b = wins[w["slug"]][info["idx"]]
        if r["side"] == "BUY":
            buys += 1
            px_entry.append(float(r["price"]))
            t[0] += sh
            t[1] -= usd
            b[0] += sh
            b[1] += usd
        else:
            # entry price of the exposure acquired: selling X at p = buying its complement at 1 - p
            px_entry.append(1.0 - float(r["price"]))
            t[0] -= sh
            t[1] += usd
            b[2] += sh
            b[3] += usd
    if n == 0:
        return None
    gross = sum(info_pnl for info_pnl in (
        (tmap.m[k]["payout"] * v[0] + v[1]) for k, v in tok.items()))
    fees = sum(v[2] for v in tok.values())
    locked = 0.0
    paired_windows = sell_paired = 0
    for sides in wins.values():
        up, dn = sides[0], sides[1]
        paired = False
        q = min(up[0], dn[0])
        if q > 0:
            paired = True
            locked += q * (1.0 - (up[1] / up[0] + dn[1] / dn[0]))
        qs = min(up[2], dn[2])
        if qs > 0:
            paired = True
            sell_paired += 1
            locked += qs * ((up[3] / up[2] + dn[3] / dn[2]) - 1.0)
        paired_windows += paired
    return {"fills": n, "notional": round(notional, 2), "gross": round(gross, 2),
            "fees": round(fees, 2), "locked": round(locked, 2),
            "directional": round(gross - locked, 2),
            "maker_share": round(maker / notional, 4) if notional else None,
            "taker_tte_p50": st.median(taker_tte) if taker_tte else None,
            "px_entry_p50": round(st.median(px_entry), 4) if px_entry else None,
            "px_ge95_share": round(sum(p >= 0.95 for p in px_entry) / len(px_entry), 4) if px_entry else None,
            "buy_share": round(buys / n, 4), "windows": len(wins), "paired_windows": paired_windows,
            "sell_paired_windows": sell_paired}


def attribute_income(rows: list[dict], tmap: TokenMap, ivs: list[dict]):
    """Attribute the API's whole-wallet rebates/rewards to crypto windows, interval by interval, by
    the wallet's window share of volume (fills by block, like the API's volume_usdc).
    Returns (income dict, window volume, api volume, api realized gross P&L) over ``ivs``."""
    inc = {"maker_rebate": 0.0, "taker_rebate": 0.0, "reward_income": 0.0}
    win_vol = api_vol = api_real = 0.0
    for iv in ivs:
        wv = 0.0
        for r in rows:
            if iv["sa"] < int(r["block_number"]) <= iv["sb"] and window_of(tmap.m.get(r["token_id"]) or {}):
                wv += float(r["amount_usdc"])
        av = iv["api"]["volume"]
        share = min(1.0, wv / av) if av > 0 else 0.0
        for k in inc:
            inc[k] += share * iv["api_income"][k]
        win_vol += wv
        api_vol += av
        api_real += iv["api"]["trade_pnl"]
    return inc, win_vol, api_vol, api_real


def label(fp: dict) -> str:
    if fp["px_ge95_share"] is not None and fp["px_ge95_share"] >= 0.6:
        return "ENDGAME"
    if fp["maker_share"] is not None and fp["maker_share"] >= 0.6:
        if fp["windows"] and fp["paired_windows"] / fp["windows"] >= 0.5:
            return "COMPLETE_SET_MAKER"
        return "DIRECTIONAL_MAKER"
    if fp["maker_share"] is not None and fp["maker_share"] <= 0.3:
        tte = fp["taker_tte_p50"]
        if tte is not None and tte <= 360:
            return "LATE_TAKER"
        return "EARLY_TAKER"
    return "MIXED"


def run(root: Path, lab: Path, cohort_path: Path, store: Path | None = None) -> dict:
    cohort = orjson.loads(cohort_path.read_bytes())
    members = set(cohort["candidates"]) | set(cohort["controls"])
    store = store or lab / "fills"
    compact(root, store)
    tmap = TokenMap(lab / "tokens.jsonl")
    asyncio.run(tmap.ensure(wallet_tokens(store, members)))
    out = {"cohort_sha256": cohort["sha256"], "built_tw": time.time_ns(), "wallets": {}}
    for w, rows in iter_wallet_fills(store, members):  # one wallet in memory at a time
        led = wallet_ledger(rows, tmap)
        pts = asyncio.run(api_points(w))
        g0 = g0_compare(led, pts)
        verdict = g0_verdict(led["stats"], g0)
        ivs = [d for d in g0["days"] if d.get("sa") is not None]
        bt = led["bt"]

        def in_scope(win: dict, ivs=ivs, bt=bt) -> bool:
            e = _ts_to_block(bt, win["end"])
            return any(iv["sa"] < e <= iv["sb"] for iv in ivs)
        fp = fingerprint(rows, tmap, in_scope) if ivs else None
        # Rebates/rewards are whole-wallet in the API; attribute to windows by the wallet's window
        # share of volume IN EACH interval (fills by block, like the API's volume_usdc).
        inc, win_vol, api_vol, api_real = attribute_income(rows, tmap, ivs)
        share_all = win_vol / api_vol if api_vol > 0 else None
        # Census eligibility: complete fill pull on >= 1 interval. The window ledger is the method
        # G0 validates; G0's whole-wallet comparability rules are about the API, not this.
        eligible = bool(fp) and g0["volume_ratio"] is not None and abs(g0["volume_ratio"] - 1) <= 0.02
        rec = {"role": "cand" if w in cohort["candidates"] else "ctrl", "g0": verdict,
               "eligible": eligible, "volume_ratio": g0["volume_ratio"],
               "n_days": len(ivs), "fingerprint": fp,
               "label": label(fp) if fp else None,
               "window_share_of_volume": round(share_all, 4) if share_all is not None else None,
               "api_realized_gross": round(api_real, 2),
               "income_attributed": {k: round(v, 2) for k, v in inc.items()}}
        if fp:
            econ = fp["gross"] - fp["fees"] + sum(inc.values())
            rec["window_economic_pnl"] = round(econ, 2)
            rec["window_economic_per_day"] = round(econ / len(ivs), 2)
            # Census self-check: for window specialists, window gross must track the API's realized.
            if share_all is not None and share_all >= 0.95:
                rec["selfcheck"] = {"window_gross": fp["gross"], "api_realized": round(api_real, 2),
                                    "ok": abs(fp["gross"] - api_real) <= max(50.0, 0.05 * abs(api_real))}
        out["wallets"][w] = rec
    agg = defaultdict(lambda: {"wallets": 0, "per_day": 0.0, "positive": 0})
    for rec in out["wallets"].values():
        if rec.get("eligible"):
            k = f"{rec['role']}:{rec['label']}"
            agg[k]["wallets"] += 1
            agg[k]["per_day"] += rec.get("window_economic_per_day", 0.0)
            agg[k]["positive"] += rec.get("window_economic_per_day", 0.0) > 0
    out["by_label"] = {k: {**v, "per_day": round(v["per_day"], 2)} for k, v in sorted(agg.items())}
    sc = [r["selfcheck"]["ok"] for r in out["wallets"].values() if r.get("eligible") and r.get("selfcheck")]
    out["selfcheck"] = {"window_specialists": len(sc), "ok": sum(sc),
                        "rate": round(sum(sc) / len(sc), 4) if sc else None}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-census")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--lab", type=Path, required=True)
    ap.add_argument("--cohort", type=Path, required=True)
    ap.add_argument("--store", type=Path, default=None,
                    help="Parquet fill store (default: <lab>/fills); compacted from the tape first")
    a = ap.parse_args(argv)
    res = run(a.data, a.lab, a.cohort, a.store)
    out = a.lab / "census" / f"census-{res['cohort_sha256'][:12]}-{time.strftime('%Y%m%dT%H%MZ', time.gmtime())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2))
    for k, v in res["by_label"].items():
        print(f"{k:28} wallets={v['wallets']:>3} positive={v['positive']:>3} window_econ_per_day=${v['per_day']:>10,.0f}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
