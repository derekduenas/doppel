"""Chain completeness: how much of on-chain truth (Goldsky Feeds activity) the live RTDS identity
tape captured over a window.

Compares a time window's Goldsky TRADE rows with RTDS ``activity/trades`` rows (sockets a and b,
separately and as a union). Units:
- tx: distinct transaction hashes
- party rows: distinct (tx, wallet) pairs. RTDS carries the taker and its makers; Goldsky carries
  one row per side of each fill.

Edges are trimmed (``--trim`` s) because RTDS stamps trade time and Goldsky stamps block time.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import orjson

from ..tape.segment import read_segment


def goldsky_rows(root: Path, lo: int, hi: int):
    for f in sorted((root / "goldsky_activity").glob("*/*.dtape.zst")):
        for rec in read_segment(f, verify=True):
            if not rec["raw"] or (rec.get("meta") or {}).get("status") != 200:
                continue
            for row in orjson.loads(rec["raw"]).get("data") or []:
                if lo <= row["block_timestamp"] < hi:
                    yield row


def rtds_rows(root: Path, stream: str, lo: int, hi: int):
    for f in sorted((root / stream).glob("*/*.dtape.zst")):
        for rec in read_segment(f, verify=True):
            raw = rec["raw"]
            if not raw or raw[0] != "{":
                continue
            p = orjson.loads(raw).get("payload") or {}
            ts = p.get("timestamp")
            if ts is not None and lo <= ts < hi and p.get("transactionHash"):
                yield p


def compare(root: Path, lo: int, hi: int) -> dict:
    gs_types: Counter = Counter()
    g_tx, g_pairs, g_ids = set(), set(), set()
    g_roles: Counter = Counter()
    for r in goldsky_rows(root, lo, hi):
        if r["id"] in g_ids:
            continue
        g_ids.add(r["id"])
        gs_types[r.get("event_type")] += 1
        if r.get("event_type") != "TRADE":
            continue
        tx = r["transaction_hash"].lower()
        g_tx.add(tx)
        g_pairs.add((tx, r["address"].lower()))
        g_roles[r.get("order_type")] += 1
    out = {"window": [lo, hi], "goldsky": {"rows": len(g_ids), "event_types": dict(gs_types),
                                           "trade_tx": len(g_tx), "trade_party_rows": len(g_pairs),
                                           "roles": dict(g_roles)}}
    union_tx, union_pairs = set(), set()
    for s in ("rtds_a", "rtds_b"):
        tx, pairs = set(), set()
        for p in rtds_rows(root, s, lo, hi):
            t = p["transactionHash"].lower()
            tx.add(t)
            pairs.add((t, p["proxyWallet"].lower()))
        union_tx |= tx
        union_pairs |= pairs
        out[s] = _cov(tx, pairs, g_tx, g_pairs)
    out["rtds_union"] = _cov(union_tx, union_pairs, g_tx, g_pairs)
    return out


def _cov(tx, pairs, g_tx, g_pairs) -> dict:
    return {"tx": len(tx), "pairs": len(pairs),
            "tx_coverage_of_chain": round(len(tx & g_tx) / len(g_tx), 5) if g_tx else None,
            "pair_coverage_of_chain": round(len(pairs & g_pairs) / len(g_pairs), 5) if g_pairs else None,
            "tx_not_on_chain": len(tx - g_tx)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-completeness")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True)
    ap.add_argument("--trim", type=int, default=30)
    a = ap.parse_args(argv)
    res = compare(a.data, a.start + a.trim, a.end - a.trim)
    print(orjson.dumps(res, option=orjson.OPT_INDENT_2).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
