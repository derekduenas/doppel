"""Exact per-wallet ledger from on-chain fills, and gate G0 (reconciliation vs Polymarket's P&L API).

Ledger (per wallet, per token): shares bought and sold, cash paid and received, fees. P&L of a
resolved token = payout x net shares + cash received - cash paid (gross), with fees on a separate
line. Tokens are mapped to markets and resolutions through Gamma (cached in ``tokens.jsonl``).
Tokens Gamma doesn't know (e.g. new protocol-V2 positions) stay ``unknown`` and are reported, never
guessed. A token whose running position goes negative inside the pulled span had opening inventory
we didn't see (left-censored). It is excluded from P&L and counted.

G0 compares, per wallet and per fully-covered UTC day:
  ours.gross_trade_pnl  vs  Δ api.trade_pnl
  ours.fees             vs  -Δ api.fees_paid
  ours.notional         vs  Δ api.volume_usdc   (is our fill pull complete?)
Day attribution: P&L on the market's end date, fees and notional on the fill's day.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import defaultdict
from pathlib import Path

import aiohttp
import orjson

from ..tape.segment import read_segment
from ..tape.sources.base import SSL_CTX, UA

GAMMA = "https://gamma-api.polymarket.com/markets"
PNL_API = "https://data-api.polymarket.com/v2/user-pnl"
DAY = 86400


# ---------------------------------------------------------------- fills
def load_fills(root: Path, wallets: set[str] | None = None, job_prefix: str = "cohort-"):
    """Deduplicated Goldsky TRADE rows (by row id) from the tape, grouped by wallet."""
    seen, out = set(), defaultdict(list)
    for f in sorted((root / "goldsky_activity").glob("*/*.dtape.zst")):
        for rec in read_segment(f, verify=True):
            m = rec.get("meta") or {}
            if m.get("status") != 200 or not str(m.get("job", "")).startswith(job_prefix):
                continue
            for r in orjson.loads(rec["raw"]).get("data") or []:
                if r["event_type"] != "TRADE" or r["id"] in seen:
                    continue
                a = r["address"].lower()
                if wallets is not None and a not in wallets:
                    continue
                seen.add(r["id"])
                out[a].append(r)
    for rows in out.values():
        rows.sort(key=lambda r: (r["block_number"], r["id"]))
    return out


# ---------------------------------------------------------------- token map
class TokenMap:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.m: dict[str, dict] = {}
        if self.path.exists():
            for line in self.path.read_bytes().splitlines():
                if line.strip():
                    d = orjson.loads(line)
                    self.m[d["token"]] = d

    def _save(self, recs: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "ab") as fh:
            for d in recs:
                fh.write(orjson.dumps(d) + b"\n")
        for d in recs:
            self.m[d["token"]] = d

    async def ensure(self, tokens: set[str], batch: int = 40, refresh_open: bool = True) -> None:
        need = [t for t in tokens if t not in self.m
                or (refresh_open and self.m[t]["status"] in ("open", "unknown"))]
        if not need:
            return
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX, limit=4),
                                         timeout=aiohttp.ClientTimeout(total=30),
                                         headers={"user-agent": UA}) as s:
            for i in range(0, len(need), batch):
                chunk = need[i:i + batch]
                found: dict[str, dict] = {}
                for closed in ("true", "false"):
                    q = [("clob_token_ids", t) for t in chunk] + [("closed", closed), ("limit", "500")]
                    async with s.get(GAMMA, params=q) as r:
                        data = orjson.loads(await r.read()) if r.status == 200 else []
                    for mk in data:
                        ids = orjson.loads(mk.get("clobTokenIds") or "[]")
                        prices = orjson.loads(mk.get("outcomePrices") or "[]")
                        resolved = bool(mk.get("closed")) and mk.get("umaResolutionStatus") == "resolved"
                        for idx, tok in enumerate(ids):
                            pay = float(prices[idx]) if resolved and idx < len(prices) else None
                            found[tok] = {"token": tok, "slug": mk.get("slug"), "idx": idx,
                                          "condition": mk.get("conditionId"),
                                          "end": mk.get("endDate"), "payout": pay,
                                          "status": "resolved" if pay is not None else "open"}
                    await asyncio.sleep(0.15)
                recs = [found.get(t, {"token": t, "status": "unknown"}) for t in chunk]
                self._save(recs)


def _end_ts(iso: str | None) -> int | None:
    if not iso:
        return None
    from datetime import datetime
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


# ---------------------------------------------------------------- ledger
def wallet_ledger(rows: list[dict], tmap: TokenMap) -> dict:
    tok = defaultdict(lambda: {"bought": 0.0, "sold": 0.0, "paid": 0.0, "recv": 0.0, "fees": 0.0,
                               "pos": 0.0, "censored": False, "first": None})
    day_fees, day_notional = defaultdict(float), defaultdict(float)
    for r in rows:
        t = tok[r["token_id"]]
        sh, usd = float(r["amount_shares"]), float(r["amount_usdc"])
        t["first"] = t["first"] or r["block_timestamp"]
        if r["side"] == "BUY":
            t["bought"] += sh
            t["paid"] += usd
            t["pos"] += sh
        else:
            t["sold"] += sh
            t["recv"] += usd
            t["pos"] -= sh
            if t["pos"] < -1e-6:
                t["censored"] = True
        f = float(r.get("fee") or 0.0)
        t["fees"] += f
        d = r["block_timestamp"] // DAY * DAY
        day_fees[d] += f
        day_notional[d] += usd
    day_pnl = defaultdict(float)
    stats = {"tokens": len(tok), "resolved": 0, "open": 0, "unknown": 0, "censored": 0,
             "resolved_notional": 0.0, "total_notional": 0.0}
    for k, t in tok.items():
        notional = t["paid"] + t["recv"]
        stats["total_notional"] += notional
        info = tmap.m.get(k, {"status": "unknown"})
        if t["censored"]:
            stats["censored"] += 1
            continue
        stats[info["status"]] += 1
        if info["status"] != "resolved":
            continue
        stats["resolved_notional"] += notional
        pnl = info["payout"] * t["pos"] + t["recv"] - t["paid"]
        end = _end_ts(info.get("end")) or t["first"]
        day_pnl[end // DAY * DAY] += pnl
    return {"stats": stats, "day_pnl": dict(day_pnl), "day_fees": dict(day_fees),
            "day_notional": dict(day_notional),
            "span": [rows[0]["block_timestamp"], rows[-1]["block_timestamp"]] if rows else None}


# ---------------------------------------------------------------- G0
async def api_points(wallet: str, interval: str = "1w") -> list[dict]:
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX),
                                     timeout=aiohttp.ClientTimeout(total=30),
                                     headers={"user-agent": UA}) as s:
        async with s.get(PNL_API, params={"user": wallet, "interval": interval,
                                          "fidelity": "1d"}) as r:
            doc = orjson.loads(await r.read())
    return sorted(doc.get("data", {}).get("points", []), key=lambda p: p["timestamp"])


def g0_compare(led: dict, points: list[dict], tol_abs=50.0, tol_rel=0.02) -> dict:
    """Compare every UTC day [d, d+1) fully inside our pulled span and bracketed by API points."""
    by_ts = {p["timestamp"]: p for p in points}
    lo, hi = led["span"]
    days = []
    for d in sorted(by_ts):
        if d + DAY not in by_ts or d < lo or d + DAY > hi:
            continue
        a, b = by_ts[d], by_ts[d + DAY]
        api = {"trade_pnl": b["trade_pnl"] - a["trade_pnl"],
               "fees": -(b["fees_paid"] - a["fees_paid"]),
               "volume": b["volume_usdc"] - a["volume_usdc"]}
        ours = {"trade_pnl": led["day_pnl"].get(d, 0.0), "fees": led["day_fees"].get(d, 0.0),
                "volume": led["day_notional"].get(d, 0.0)}

        def ok(x, y):
            return abs(x - y) <= max(tol_abs, tol_rel * abs(y))
        days.append({"day": d, "api": api, "ours": ours,
                     "pnl_ok": ok(ours["trade_pnl"], api["trade_pnl"]),
                     "fees_ok": ok(ours["fees"], api["fees"]),
                     "volume_ratio": round(ours["volume"] / api["volume"], 4) if api["volume"] else None})
    passed = bool(days) and all(x["pnl_ok"] and x["fees_ok"] for x in days)
    return {"days": days, "n_days": len(days), "pass": passed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-ledger")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--lab", type=Path, required=True)
    ap.add_argument("--cohort", type=Path, required=True)
    a = ap.parse_args(argv)
    cohort = orjson.loads(a.cohort.read_bytes())
    members = set(cohort["candidates"]) | set(cohort["controls"])
    fills = load_fills(a.data, members)
    tmap = TokenMap(a.lab / "tokens.jsonl")
    asyncio.run(tmap.ensure({r["token_id"] for rows in fills.values() for r in rows}))
    report = {"cohort_sha256": cohort["sha256"], "built_tw": time.time_ns(), "wallets": {}}
    for w, rows in sorted(fills.items()):
        led = wallet_ledger(rows, tmap)
        pts = asyncio.run(api_points(w))
        g0 = g0_compare(led, pts)
        role = "cand" if w in cohort["candidates"] else "ctrl"
        report["wallets"][w] = {"role": role, "fills": len(rows), **led["stats"], "g0": g0}
        s = led["stats"]
        cov = s["resolved_notional"] / s["total_notional"] if s["total_notional"] else 0
        print(f"{w[:12]} {role} fills={len(rows):>6} resolved_cov={cov:5.1%} "
              f"unknown={s['unknown']:>4} censored={s['censored']:>3} g0_days={g0['n_days']} "
              f"pass={g0['pass']}", flush=True)
    out = a.lab / "g0" / f"g0-{cohort['sha256'][:12]}-{time.strftime('%Y%m%dT%H%MZ', time.gmtime())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
    scored = [v for v in report["wallets"].values() if v["g0"]["n_days"] > 0]
    print(f"G0: {sum(v['g0']['pass'] for v in scored)}/{len(scored)} wallets with comparable days pass -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
