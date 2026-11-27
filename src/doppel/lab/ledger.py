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
import fcntl
import math
import os
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
KEEP = ("token_id", "side", "amount_shares", "amount_usdc", "fee", "block_timestamp",
        "block_number", "order_type", "price", "order_id", "counterparty_address",
        "transaction_hash")


def load_fills(root: Path, wallets: set[str] | None = None, job_prefix: str = "cohort-"):
    """Deduplicated Goldsky TRADE rows (by row id) from the tape, grouped by wallet.
    Rows keep only the fields the ledger/census use (memory: ~1.7M rows on a shared 8 GB box)."""
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
                out[a].append({k: r.get(k) for k in KEEP} | {"id": r["id"]})
    for rows in out.values():
        rows.sort(key=lambda r: (r["block_number"], r["id"]))
    return out


# ---------------------------------------------------------------- token map
class TokenMap:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.m: dict[str, dict] = {}
        self._revision = None
        if self.path.exists():
            with self.path.open("rb") as fh:
                fcntl.flock(fh, fcntl.LOCK_SH)
                self._refresh(fh)

    @staticmethod
    def _merge(previous: dict | None, row: dict) -> dict:
        if previous is None or previous.get("status") == "unknown":
            return row
        if row.get("status") == "unknown":
            return previous
        fields = ["condition", "idx"]
        if previous.get("status") == "resolved":
            fields.extend(("slug", "end"))
            if row.get("status") == "resolved":
                fields.append("payout")
        if any(previous.get(k) is not None and row.get(k) is not None
               and previous[k] != row[k] for k in fields):
            raise ValueError(f"conflicting venue metadata for token {row['token']}")
        return previous if previous.get("status") == "resolved" else row

    def _refresh(self, fh) -> None:
        st = os.fstat(fh.fileno())
        revision = (st.st_ino, st.st_size, st.st_mtime_ns)
        if revision != self._revision:
            fh.seek(0)
            cache = {}
            for line in fh:
                if line.strip():
                    d = orjson.loads(line)
                    cache[d["token"]] = self._merge(cache.get(d["token"]), d)
            self.m = cache
            self._revision = revision

    def _save(self, recs: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+b") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            self._refresh(fh)  # another ledger/census process can share this cache
            pending = {}
            for row in recs:
                tok = row["token"]
                pending[tok] = self._merge(pending.get(tok, self.m.get(tok)), row)
            changed = [r for t, r in pending.items() if self.m.get(t) != r]
            if not changed:
                return
            slots = {(r.get("condition"), r.get("idx")): t for t, r in self.m.items() if r.get("condition")}
            for row in changed:
                if row.get("condition"):
                    slot = (row["condition"], row["idx"])
                    if slots.setdefault(slot, row["token"]) != row["token"]:
                        raise ValueError(f"contradictory tokens for condition and outcome {slot}")
            if fh.seek(0, os.SEEK_END):
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    fh.write(b"\n")
            fh.write(b"".join(orjson.dumps(row) + b"\n" for row in changed))
            fh.flush()
            os.fsync(fh.fileno())
            self.m.update(pending)
            st = os.fstat(fh.fileno())
            self._revision = (st.st_ino, st.st_size, st.st_mtime_ns)

    async def ensure(self, tokens: set[str], batch: int = 40, refresh_open: bool = True) -> None:
        # Old cache files stored only IDs observed in fills. Query the traded side of a
        # resolved market again when its corresponding settled outcome is absent.
        resolved = defaultdict(set)
        for row in self.m.values():
            if row.get("status") == "resolved" and row.get("condition"):
                resolved[row["condition"]].add(row["idx"])
        need = sorted(t for t in tokens if t not in self.m
                      or (refresh_open and self.m[t]["status"] in ("open", "unknown"))
                      or (self.m[t]["status"] == "resolved" and self.m[t].get("condition")
                          and len(resolved[self.m[t]["condition"]]) == 1))
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
                        ids = mk.get("clobTokenIds") or []
                        ids = orjson.loads(ids) if isinstance(ids, str) else ids
                        if not isinstance(ids, list) or not any(t in chunk for t in ids):
                            continue
                        if not all(isinstance(t, str) and t for t in ids) or len(set(ids)) != len(ids):
                            raise ValueError("invalid outcome token identities in requested venue market")
                        prices = mk.get("outcomePrices") or []
                        prices = orjson.loads(prices) if isinstance(prices, str) else prices
                        settled = mk.get("closed") is True and mk.get("umaResolutionStatus") == "resolved"
                        pair = len(ids) == 2 and all(mk.get(k) for k in ("conditionId", "slug", "endDate"))
                        if pair and settled:
                            if (not isinstance(prices, list) or len(prices) != 2
                                    or not all(0 <= float(p) <= 1 for p in prices)
                                    or not math.isclose(sum(map(float, prices)), 1.0, abs_tol=1e-9)):
                                raise ValueError("inconsistent binary payouts in requested venue market")
                        for idx, tok in enumerate(ids):
                            if tok not in chunk and not pair:
                                continue
                            pay = float(prices[idx]) if settled and idx < len(prices) else None
                            row = {"token": tok, "slug": mk.get("slug"), "idx": idx,
                                   "condition": mk.get("conditionId"), "end": mk.get("endDate"),
                                   "payout": pay, "status": "resolved" if pay is not None else "open"}
                            found[tok] = self._merge(found.get(tok), row)
                    await asyncio.sleep(0.15)
                for tok in chunk:
                    found.setdefault(tok, {"token": tok, "status": "unknown"})
                self._save(list(found.values()))


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
    fills_b: list[tuple[int, float, float]] = []  # (block, fee, notional) for block-exact G0
    tok_moves: dict[str, list[tuple[int, float]]] = defaultdict(list)  # token -> (block, +/-shares)
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
        fills_b.append((int(r["block_number"]), f, usd))
        tok_moves[r["token_id"]].append((int(r["block_number"]), sh if r["side"] == "BUY" else -sh))
    day_pnl = defaultdict(float)
    tok_pnl: list[tuple[int, float]] = []  # (realization ts ~ market end, pnl)
    tok_end: dict[str, int | None] = {}
    stats = {"tokens": len(tok), "resolved": 0, "open": 0, "unknown": 0, "censored": 0,
             "resolved_notional": 0.0, "total_notional": 0.0, "unresolved_exposure": 0.0}
    for k, t in tok.items():
        notional = t["paid"] + t["recv"]
        stats["total_notional"] += notional
        info = tmap.m.get(k, {"status": "unknown"})
        tok_end[k] = _end_ts(info.get("end"))
        if t["censored"]:
            stats["censored"] += 1
            continue
        stats[info["status"]] += 1
        if info["status"] != "resolved":
            # payout in [0, 1] -> this token's P&L is uncertain by |net shares|
            stats["unresolved_exposure"] += abs(t["pos"])
            continue
        stats["resolved_notional"] += notional
        pnl = info["payout"] * t["pos"] + t["recv"] - t["paid"]
        end = _end_ts(info.get("end")) or t["first"]
        day_pnl[end // DAY * DAY] += pnl
        tok_pnl.append((end, pnl))
    bt = sorted({(int(r["block_number"]), int(r["block_timestamp"])) for r in rows})
    return {"stats": stats, "day_pnl": dict(day_pnl), "day_fees": dict(day_fees),
            "day_notional": dict(day_notional), "fills_b": fills_b, "tok_pnl": tok_pnl, "bt": bt,
            "tok_moves": dict(tok_moves), "tok_end": tok_end,
            "span": [rows[0]["block_timestamp"], rows[-1]["block_timestamp"]] if rows else None,
            "span_blocks": [bt[0][0], bt[-1][0]] if bt else None}


def boundary_exposure(led: dict, block: int) -> float:
    """Shares held at ``block`` in tokens not yet ended by then. The API's trade_pnl marks such
    open positions to market at its snapshots; our ledger books them at resolution, so this much
    exposure (each share can swing up to $1) is legitimately unmatched across that snapshot."""
    tot = 0.0
    for tok, moves in (led.get("tok_moves") or {}).items():
        end = (led.get("tok_end") or {}).get(tok)
        if end is not None and _ts_to_block(led["bt"], end) <= block:
            continue
        pos = sum(q for b, q in moves if b <= block)
        tot += abs(pos)
    return tot


def _ts_to_block(bt: list[tuple[int, int]], ts: int) -> float:
    """Interpolate a block number for a timestamp from observed (block, ts) pairs."""
    import bisect
    tss = [t for _, t in bt]
    i = bisect.bisect_left(tss, ts)
    if i <= 0:
        b, t = bt[0]
    elif i >= len(bt):
        b, t = bt[-1]
    else:
        (b0, t0), (b1, t1) = bt[i - 1], bt[i]
        return b0 + (b1 - b0) * ((ts - t0) / (t1 - t0) if t1 != t0 else 0)
    return b + (ts - t) / 2.0  # ~2 s blocks outside the observed range


# ---------------------------------------------------------------- G0
async def api_points(wallet: str, interval: str = "1w") -> list[dict]:
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX),
                                     timeout=aiohttp.ClientTimeout(total=30),
                                     headers={"user-agent": UA}) as s:
        async with s.get(PNL_API, params={"user": wallet, "interval": interval,
                                          "fidelity": "1d"}) as r:
            doc = orjson.loads(await r.read())
    return sorted(doc.get("data", {}).get("points", []), key=lambda p: p["timestamp"])


def g0_compare(led: dict, points: list[dict], tol_abs=50.0, tol_rel=0.02,
               warmup_blocks: int = 1800) -> dict:
    """Compare each interval between consecutive API snapshots that lies inside our pulled span.

    API points are snapshots at ``source_block`` (not exactly at the labeled midnight), so when
    available, fees and notional are summed over fills with a.source_block < block <= b.source_block
    and P&L over tokens whose market end maps into that block range. Falls back to UTC days when a
    point has no source_block (synthetic tests)."""
    by_ts = {p["timestamp"]: p for p in points}
    lo, hi = led["span"]
    blo, bhi = (led.get("span_blocks") or [None, None])
    days = []
    for d in sorted(by_ts):
        if d + DAY not in by_ts:
            continue
        a, b = by_ts[d], by_ts[d + DAY]
        # Realized gross P&L: the API's trade_pnl marks open positions to market at each snapshot
        # (unrealized_pnl); our ledger books at resolution. Compare realized with realized.
        def realized(p: dict) -> float:
            return p["trade_pnl"] - (p.get("unrealized_pnl") or 0.0)
        api = {"trade_pnl": realized(b) - realized(a),
               "fees": -(b["fees_paid"] - a["fees_paid"]),
               "volume": b["volume_usdc"] - a["volume_usdc"]}
        sa, sb = a.get("source_block"), b.get("source_block")
        if sa is not None and sb is not None and led.get("fills_b") is not None and blo is not None:
            # Positions opened before our first pulled fill can resolve inside an interval and are
            # invisible to us (no sells to flag them). Require ~1 h of pulled history before the
            # interval starts, so short-dated windows opened earlier have already resolved.
            if sa < blo + warmup_blocks or sb > bhi:
                continue
            fee = sum(f for blk, f, _ in led["fills_b"] if sa < blk <= sb)
            vol = sum(u for blk, _, u in led["fills_b"] if sa < blk <= sb)
            pnl = sum(p for ts, p in led["tok_pnl"] if sa < _ts_to_block(led["bt"], ts) <= sb)
            ours = {"trade_pnl": pnl, "fees": fee, "volume": vol,
                    "boundary_exposure": round(boundary_exposure(led, sa) + boundary_exposure(led, sb), 2)}
        else:
            if d < lo or d + DAY > hi:
                continue
            ours = {"trade_pnl": led["day_pnl"].get(d, 0.0), "fees": led["day_fees"].get(d, 0.0),
                    "volume": led["day_notional"].get(d, 0.0)}

        def ok(x, y):
            return abs(x - y) <= max(tol_abs, tol_rel * abs(y))
        days.append({"day": d, "api": api, "ours": ours, "sa": sa, "sb": sb,
                     "api_income": {k: (b.get(k) or 0.0) - (a.get(k) or 0.0)
                                    for k in ("maker_rebate", "taker_rebate", "reward_income")},
                     "pnl_ok": ok(ours["trade_pnl"], api["trade_pnl"]),
                     "fees_ok": ok(ours["fees"], api["fees"]),
                     "volume_ratio": round(ours["volume"] / api["volume"], 4) if api["volume"] else None})
    tot = {k: {"api": sum(x["api"][k] for x in days), "ours": sum(x["ours"][k] for x in days)}
           for k in ("trade_pnl", "fees", "volume")}

    def ok(x, y):
        return abs(x - y) <= max(tol_abs, tol_rel * abs(y))
    vol_ratio = tot["volume"]["ours"] / tot["volume"]["api"] if tot["volume"]["api"] else None
    bexp = sum(x["ours"].get("boundary_exposure", 0.0) for x in days)
    return {"days": days, "n_days": len(days), "totals": tot, "boundary_exposure": round(bexp, 2),
            "volume_ratio": round(vol_ratio, 4) if vol_ratio is not None else None,
            "pnl_ok": bool(days) and ok(tot["trade_pnl"]["ours"], tot["trade_pnl"]["api"]),
            "fees_ok": bool(days) and ok(tot["fees"]["ours"], tot["fees"]["api"])}


def g0_verdict(stats: dict, g0: dict, min_resolved=0.95, max_censored=0.02, vol_tol=0.02,
               min_volume=1000.0) -> str:
    """PASS / FAIL on comparable wallets; otherwise NOT_COMPARABLE with the reason.
    Daily rows are diagnostics; the verdict uses span totals, since day edges differ (snapshot
    time vs block time) and cancel across adjacent days."""
    if g0["n_days"] == 0:
        return "NOT_COMPARABLE:no_full_day_in_span"
    tn = stats["total_notional"] or 0.0
    if g0["totals"]["volume"]["api"] < min_volume:
        return "NOT_COMPARABLE:too_little_volume"
    if tn and stats["resolved_notional"] / tn < min_resolved:
        return "NOT_COMPARABLE:resolved_coverage"
    if stats["tokens"] and stats["censored"] / stats["tokens"] > max_censored:
        return "NOT_COMPARABLE:opening_inventory"
    if g0["volume_ratio"] is None or abs(g0["volume_ratio"] - 1) > vol_tol:
        return "NOT_COMPARABLE:fill_pull_incomplete"
    tol = max(50.0, 0.02 * abs(g0["totals"]["trade_pnl"]["api"]))
    if stats.get("unresolved_exposure", 0.0) > tol:
        return "NOT_COMPARABLE:unresolved_exposure"
    return "PASS" if g0["pnl_ok"] and g0["fees_ok"] else "FAIL"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-ledger")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--lab", type=Path, required=True)
    ap.add_argument("--cohort", type=Path, required=True)
    ap.add_argument("--store", type=Path, default=None,
                    help="Parquet fill store (default: <lab>/fills); compacted from the tape first")
    a = ap.parse_args(argv)
    from .fillstore import compact, iter_wallet_fills, wallet_tokens
    cohort = orjson.loads(a.cohort.read_bytes())
    members = set(cohort["candidates"]) | set(cohort["controls"])
    store = a.store or a.lab / "fills"
    print(f"fillstore: {orjson.dumps(compact(a.data, store)).decode()}", flush=True)
    tmap = TokenMap(a.lab / "tokens.jsonl")
    asyncio.run(tmap.ensure(wallet_tokens(store, members)))
    report = {"cohort_sha256": cohort["sha256"], "built_tw": time.time_ns(), "wallets": {}}
    for w, rows in iter_wallet_fills(store, members):  # one wallet in memory at a time
        led = wallet_ledger(rows, tmap)
        pts = asyncio.run(api_points(w))
        g0 = g0_compare(led, pts)
        role = "cand" if w in cohort["candidates"] else "ctrl"
        verdict = g0_verdict(led["stats"], g0)
        report["wallets"][w] = {"role": role, "fills": len(rows), **led["stats"], "g0": g0,
                                "verdict": verdict}
        t = g0.get("totals", {})
        pnl = t.get("trade_pnl", {})
        print(f"{w[:12]} {role} days={g0['n_days']} vol_ratio={g0.get('volume_ratio')} "
              f"pnl api={pnl.get('api', 0):>9.1f} ours={pnl.get('ours', 0):>9.1f} -> {verdict}",
              flush=True)
    out = a.lab / "g0" / f"g0-{cohort['sha256'][:12]}-{time.strftime('%Y%m%dT%H%MZ', time.gmtime())}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
    vs = [v["verdict"] for v in report["wallets"].values()]
    comp = [v for v in vs if not v.startswith("NOT_COMPARABLE")]
    npass = sum(v == "PASS" for v in comp)
    report["summary"] = {"comparable": len(comp), "pass": npass,
                         "pass_rate": round(npass / len(comp), 4) if comp else None,
                         "gate": "G0 needs pass_rate >= 0.95 on >= 50 comparable wallets"}
    out.write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
    print(f"G0: {npass}/{len(comp)} comparable wallets pass ({len(vs) - len(comp)} not comparable) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
