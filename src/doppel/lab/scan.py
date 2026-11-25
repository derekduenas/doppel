"""Wallet activity scan over the live RTDS identity tape, restricted to crypto up/down windows,
plus a frozen, activity-selected study cohort.

Selection never looks at profit, so choosing the cohort cannot leak outcomes. Candidates are the
most active wallets by window notional. Controls are a seeded random sample of wallets in a lower
activity band. The cohort file is written once with its selection rule, input span and sha256.
Later stages (P&L, skill) score the frozen cohort on what happens next.
"""

from __future__ import annotations

import argparse
import hashlib
import random
import re
import statistics as st
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import orjson

from ..tape.segment import read_segment

WINDOW = re.compile(r"^(btc|eth|sol|xrp)-updown-(5m|15m)-(\d+)$")
LEN = {"5m": 300, "15m": 900}


def iter_fills(root: Path, streams=("rtds_a", "rtds_b"), since: int = 0):
    """Deduplicated identity fills (union of sockets) in crypto windows."""
    seen = set()
    for s in streams:
        for f in sorted((root / s).glob("*/*.dtape.zst")):
            for rec in read_segment(f, verify=True):
                raw = rec["raw"]
                if not raw or raw[0] != "{":
                    continue
                p = orjson.loads(raw).get("payload") or {}
                m = WINDOW.match(p.get("eventSlug") or "")
                if not m or p.get("timestamp", 0) < since:
                    continue
                key = (p["transactionHash"].lower(), p["proxyWallet"].lower(), p["asset"],
                       p["side"], p["size"], p["price"])
                if key in seen:
                    continue
                seen.add(key)
                start = int(m.group(3))
                yield {**p, "_asset": m.group(1), "_hz": m.group(2),
                       "_tte": start + LEN[m.group(2)] - p["timestamp"]}


def scan(root: Path, since: int = 0) -> dict:
    w = defaultdict(lambda: {"fills": 0, "taker": 0, "maker": 0, "usd": 0.0, "taker_usd": 0.0,
                             "fees": 0.0, "buys": 0, "windows": set(), "assets": Counter(),
                             "hz": Counter(), "tte_taker": [], "tte_maker": [], "px": [],
                             "outcomes": defaultdict(set), "first": None, "last": None,
                             "name": None})
    span = [None, None]
    for p in iter_fills(root, since=since):
        a = w[p["proxyWallet"].lower()]
        usd = float(p["price"]) * float(p["size"])
        taker = (p.get("fee") or 0) > 0
        a["fills"] += 1
        a["usd"] += usd
        a["fees"] += float(p.get("fee") or 0)
        if taker:
            a["taker"] += 1
            a["taker_usd"] += usd
            a["tte_taker"].append(p["_tte"])
        else:
            a["maker"] += 1
            a["tte_maker"].append(p["_tte"])
        a["buys"] += p["side"] == "BUY"
        a["windows"].add(p["eventSlug"])
        a["assets"][p["_asset"]] += 1
        a["hz"][p["_hz"]] += 1
        a["px"].append(float(p["price"]))
        if p["side"] == "BUY":
            a["outcomes"][p["eventSlug"]].add(p.get("outcomeIndex"))
        ts = p["timestamp"]
        a["first"] = ts if a["first"] is None else min(a["first"], ts)
        a["last"] = ts if a["last"] is None else max(a["last"], ts)
        a["name"] = a["name"] or p.get("name") or p.get("pseudonym")
        span[0] = ts if span[0] is None else min(span[0], ts)
        span[1] = ts if span[1] is None else max(span[1], ts)
    out = {}
    for addr, a in w.items():
        px = a["px"]
        out[addr] = {
            "name": a["name"], "fills": a["fills"], "taker_fills": a["taker"],
            "maker_fills": a["maker"], "usd": round(a["usd"], 2),
            "taker_share": round(a["taker_usd"] / a["usd"], 4) if a["usd"] else None,
            "fees": round(a["fees"], 4), "buy_share": round(a["buys"] / a["fills"], 4),
            "n_windows": len(a["windows"]), "assets": dict(a["assets"]), "horizons": dict(a["hz"]),
            "both_sides_windows": sum(1 for v in a["outcomes"].values() if {0, 1} <= v),
            "tte_taker_p50": st.median(a["tte_taker"]) if a["tte_taker"] else None,
            "tte_maker_p50": st.median(a["tte_maker"]) if a["tte_maker"] else None,
            "px_p50": round(st.median(px), 4),
            "extreme_px_share": round(sum(1 for x in px if x >= 0.9 or x <= 0.1) / len(px), 4),
            "first": a["first"], "last": a["last"],
        }
    return {"span": span, "built_tw": time.time_ns(), "wallets": out}


def select_cohort(scan_doc: dict, n_cand=100, n_ctrl=50, min_windows=5, ctrl_band=(101, 1000),
                  seed=20261125) -> dict:
    ws = [(a, v) for a, v in scan_doc["wallets"].items() if v["n_windows"] >= min_windows]
    ws.sort(key=lambda kv: (-kv[1]["usd"], kv[0]))
    cands = [a for a, _ in ws[:n_cand]]
    band = [a for a, _ in ws[ctrl_band[0] - 1:ctrl_band[1]]]
    rng = random.Random(seed)
    ctrls = sorted(rng.sample(band, min(n_ctrl, len(band))))
    rule = {"metric": "crypto-window notional (price*size) on RTDS union, profit-blind",
            "min_windows": min_windows, "n_candidates": n_cand, "control_band_ranks": ctrl_band,
            "n_controls": n_ctrl, "seed": seed}
    body = {"rule": rule, "span": scan_doc["span"], "eligible": len(ws),
            "candidates": cands, "controls": ctrls}
    digest = hashlib.sha256(orjson.dumps(body, option=orjson.OPT_SORT_KEYS)).hexdigest()
    return {**body, "frozen_tw": time.time_ns(), "sha256": digest}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-scan")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--freeze-cohort", type=Path, help="write cohort file (refuses to overwrite)")
    a = ap.parse_args(argv)
    doc = scan(a.data)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
    ws = doc["wallets"]
    tot = sum(v["usd"] for v in ws.values())
    print(f"span {doc['span']} wallets {len(ws)} window notional ${tot:,.0f}")
    if a.freeze_cohort:
        if a.freeze_cohort.exists():
            print(f"cohort exists, not overwriting: {a.freeze_cohort}", file=sys.stderr)
            return 1
        c = select_cohort(doc)
        a.freeze_cohort.parent.mkdir(parents=True, exist_ok=True)
        a.freeze_cohort.write_bytes(orjson.dumps(c, option=orjson.OPT_INDENT_2))
        print(f"cohort frozen sha256={c['sha256'][:16]} eligible={c['eligible']} "
              f"candidates={len(c['candidates'])} controls={len(c['controls'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
