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
from .ledger import DAY, TokenMap, api_points, g0_compare, g0_verdict, wallet_ledger

WINDOW = re.compile(r"^(btc|eth|sol|xrp)-updown-(5m|15m)-(\d+)$")
LEN = {"5m": 300, "15m": 900}


def window_of(info: dict):
    m = WINDOW.match(info.get("slug") or "")
    if not m:
        return None
    start = int(m.group(3))
    return {"asset": m.group(1), "hz": m.group(2), "start": start, "end": start + LEN[m.group(2)],
            "slug": info["slug"]}


def fingerprint(rows: list[dict], tmap: TokenMap, days: list[int]) -> dict | None:
    """Window-only fingerprint over the given UTC day starts (fills dated in those days)."""
    dayset = set(days)
    wins = defaultdict(lambda: {0: [0.0, 0.0], 1: [0.0, 0.0]})  # idx -> [shares bought, cost]
    tok = defaultdict(lambda: [0.0, 0.0, 0.0])  # pos, cash, fees
    notional = maker = 0.0
    taker_tte, px_entry, buys, n = [], [], 0, 0
    for r in rows:
        if r["block_timestamp"] // DAY * DAY not in dayset:
            continue
        info = tmap.m.get(r["token_id"]) or {}
        w = window_of(info)
        if not w or info.get("status") != "resolved":
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
        if r["side"] == "BUY":
            buys += 1
            px_entry.append(float(r["price"]))
            t[0] += sh
            t[1] -= usd
            b = wins[w["slug"]][info["idx"]]
            b[0] += sh
            b[1] += usd
        else:
            t[0] -= sh
            t[1] += usd
    if n == 0:
        return None
    gross = sum(info_pnl for info_pnl in (
        (tmap.m[k]["payout"] * v[0] + v[1]) for k, v in tok.items()))
    fees = sum(v[2] for v in tok.values())
    locked = 0.0
    paired_windows = 0
    for sides in wins.values():
        up, dn = sides[0], sides[1]
        q = min(up[0], dn[0])
        if q <= 0:
            continue
        paired_windows += 1
        cost = up[1] / up[0] + dn[1] / dn[0]
        locked += q * (1.0 - cost)
    return {"fills": n, "notional": round(notional, 2), "gross": round(gross, 2),
            "fees": round(fees, 2), "locked": round(locked, 2),
            "directional": round(gross - locked, 2),
            "maker_share": round(maker / notional, 4) if notional else None,
            "taker_tte_p50": st.median(taker_tte) if taker_tte else None,
            "px_entry_p50": round(st.median(px_entry), 4) if px_entry else None,
            "px_ge95_share": round(sum(p >= 0.95 for p in px_entry) / len(px_entry), 4) if px_entry else None,
            "buy_share": round(buys / n, 4), "windows": len(wins), "paired_windows": paired_windows}


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
        days = [d["day"] for d in g0["days"]]
        fp = fingerprint(rows, tmap, days) if days else None
        by = {p["timestamp"]: p for p in pts}
        inc = {"maker_rebate": 0.0, "taker_rebate": 0.0, "reward_income": 0.0, "api_trade_pnl": 0.0,
               "api_fees": 0.0}
        for d in days:
            a, b = by[d], by[d + DAY]
            for k in ("maker_rebate", "taker_rebate", "reward_income"):
                inc[k] += (b.get(k) or 0.0) - (a.get(k) or 0.0)
            inc["api_trade_pnl"] += b["trade_pnl"] - a["trade_pnl"]
            inc["api_fees"] += -(b["fees_paid"] - a["fees_paid"])
        # Census eligibility: complete fill pull on >= 1 full day. The window ledger is the method
        # G0 validates; G0's whole-wallet comparability rules are about the API, not this.
        eligible = bool(fp) and g0["volume_ratio"] is not None and abs(g0["volume_ratio"] - 1) <= 0.02
        rec = {"role": "cand" if w in cohort["candidates"] else "ctrl", "g0": verdict,
               "eligible": eligible, "volume_ratio": g0["volume_ratio"],
               "n_days": len(days), "fingerprint": fp,
               "label": label(fp) if fp else None,
               "income": {k: round(v, 2) for k, v in inc.items()}}
        if fp:
            econ = fp["gross"] - fp["fees"] + inc["maker_rebate"] + inc["taker_rebate"] + inc["reward_income"]
            rec["window_economic_pnl"] = round(econ, 2)
            rec["window_economic_per_day"] = round(econ / len(days), 2)
            rec["window_share_of_api_pnl"] = (round(fp["gross"] / inc["api_trade_pnl"], 3)
                                              if inc["api_trade_pnl"] else None)
        out["wallets"][w] = rec
    agg = defaultdict(lambda: {"wallets": 0, "per_day": 0.0, "positive": 0})
    for rec in out["wallets"].values():
        if rec.get("eligible"):
            k = f"{rec['role']}:{rec['label']}"
            agg[k]["wallets"] += 1
            agg[k]["per_day"] += rec.get("window_economic_per_day", 0.0)
            agg[k]["positive"] += rec.get("window_economic_per_day", 0.0) > 0
    out["by_label"] = {k: {**v, "per_day": round(v["per_day"], 2)} for k, v in sorted(agg.items())}
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
