"""Independent one-token replay and cutoff-visible outside-market records for the registered first fill."""
import argparse
from collections import deque
from datetime import datetime, timezone
from itertools import groupby
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import orjson

from doppel.lab.goldsky_proof import digest
from doppel.lab.structural import parse_kalshi_rest, parse_pmus_ws
from doppel.tape.segment import read_segment, segment_path

NS = 10**9


def sealed(root, stream, start):
    p = segment_path(root, stream, start * NS)
    manifest = root / stream / "manifest.jsonl"
    rel = p.relative_to(root / stream).as_posix()
    seals = [r for line in manifest.read_bytes().splitlines() if (r := orjson.loads(line))["file"] == rel]
    if not seals or not p.is_file() or p.stat().st_size != seals[-1]["bytes"] or digest(p) != seals[-1]["sha256"]:
        raise ValueError(f"source mismatch in {p}")
    return p, {"file": p.relative_to(root).as_posix(), "sha256": seals[-1]["sha256"], "bytes": p.stat().st_size}


def public_context(root, cutoff_marks, start):
    """As-of last quote/trade actually received, with upstream freshness kept separate."""
    opening = datetime.fromtimestamp(start, timezone.utc)
    close = datetime.fromtimestamp(start + 900, timezone.utc).astimezone(ZoneInfo("America/New_York"))
    pm = "cpcbtc-btc-updown-5m-" + opening.strftime("%Y-%m-%d-%H%Mz")
    kalshi = "KXBTC15M-" + close.strftime("%y%b%d%H%M-%M").upper()
    reports = {}
    for stream, key in (("kalshi_books", kalshi), ("pmus_books", pm), ("spot_coinbase", "BTC-USD")):
        p, seal = sealed(root, stream, start)
        prior = None
        out = {}
        for rec in read_segment(p, verify=True, strict=True):
            tw = rec["tw"]
            for name, mark in cutoff_marks.items():
                if name not in out and tw >= mark["tw_ns"]:
                    out[name] = prior
            if len(out) == len(cutoff_marks):
                break
            if tw < start * NS:
                continue
            meta = rec.get("meta") or {}
            if meta.get("ctl"):
                prior = None
                continue
            if stream == "spot_coinbase":
                if not rec["raw"].startswith("{"):
                    continue
                tick = orjson.loads(rec["raw"])
                if tick.get("product_id") != key or tick.get("type") != "match":
                    continue
                values = {"trade_price_usd": float(tick["price"]), "venue_time": tick["time"]}
            else:
                if meta.get("key") != key:
                    continue
                if meta.get("status") != 200:
                    prior = None
                    continue
                if stream == "kalshi_books":
                    prices = parse_kalshi_rest(rec["raw"])
                else:
                    parsed = parse_pmus_ws(rec["raw"])
                    prices = parsed[1:] if parsed and parsed[0] == key else None
                if prices is None:
                    prior = None
                    continue
                values = dict(zip(("bid","bid_size","ask","ask_size"), prices))
            prior = {"tw_ns": tw, "seq": rec["seq"], **values}
        for name, mark in cutoff_marks.items():
            state = out.get(name, prior)
            if state is not None and state["tw_ns"] >= mark["tw_ns"]:
                raise ValueError("future external observation")
            out[name] = ({**state, "receipt_age_ms": (mark["tw_ns"] - state["tw_ns"]) / 1e6}
                         if state is not None else None)
        reports[stream] = {"key": key, "source": seal, "states": out,
                           "quality": "REST upstream freshness unverified" if stream.endswith("books") else "public spot trade"}
    return reports


def book_check(root, case):
    marks = {f"addition_{i}": r for i,r in enumerate(case["arms"]["actual"]["fragment_candidates"], 1)}
    marks["execution"] = case["tx_print"]
    start, _ = case["source"]["selected_utc"]
    path, seal = sealed(root, "clob_btc_5m", start)
    if seal["sha256"] != case["source"]["sha256"]:
        raise ValueError("source differs from Layer 2")
    token = case["tx_print"]["token"]
    target = case["signed_price"]
    buy, sell = {}, {}
    active = False
    last = None
    signed_flow = deque()
    prev_seq = None
    samples, captured, observed_marks = [], {}, set()
    def top(tw):
        bb, ba = max(buy, default=None), min(sell, default=None)
        prior = [(side, sz) for t,side,sz in signed_flow if tw - NS <= t < tw]
        return {"best_bid": bb, "bid_size": buy.get(bb), "best_ask": ba, "ask_size": sell.get(ba),
                "level_size": buy.get(target, 0), "last_depth_seq": last[2] if last else None,
                "last_depth_tw_ns": last[0] if last else None, "last_depth_venue_ms": last[1] if last else None,
                "prior_1s_print_count": len(prior), "prior_1s_buy_shares": round(sum(z for s,z in prior if s=="BUY"),6),
                "prior_1s_sell_shares": round(sum(z for s,z in prior if s=="SELL"),6)} if active else None
    lo, hi = marks["addition_1"]["tw_ns"], marks["addition_2"]["tw_ns"]
    records = read_segment(path, strict=True, verify=True)
    for tw, group in groupby(records, key=lambda r:r["tw"]):
        if tw > marks["execution"]["tw_ns"]:
            break
        for name, mark in marks.items():
            if mark["tw_ns"] == tw:
                captured[name] = top(tw)
        if lo <= tw <= hi:
            samples.append(top(tw))
        for rec in group:
            seq, meta = rec["seq"], rec.get("meta") or {}
            if rec["stream"] != "clob_btc_5m":
                raise ValueError("wrong book")
            if prev_seq is not None and seq != prev_seq + 1 or meta.get("ctl") not in (None, "subscribe_add"):
                active, last = False, None
                buy.clear()
                sell.clear()
                signed_flow.clear()
            prev_seq = seq
            if meta.get("ctl") or rec["raw"] in ("", "PONG", "PING"):
                continue
            obj = orjson.loads(rec["raw"])
            for mi, msg in enumerate(obj if isinstance(obj,list) else [obj]):
                kind = msg["event_type"]
                if kind not in ("book","price_change","last_trade_price"):
                    if kind not in ("tick_size_change","best_bid_ask","market_resolved","new_market"):
                        raise ValueError(f"unrecognized event in independent prefix: {kind}")
                    continue
                ms = int(msg["timestamp"])
                if kind == "last_trade_price":
                    if msg["asset_id"] == token:
                        signed_flow.append((tw, msg["side"], float(msg["size"])))
                        while signed_flow and signed_flow[0][0] < tw - NS:
                            signed_flow.popleft()
                        mark = marks["execution"]
                        if (seq, mi, tw, ms) == (mark["seq"], mark["message_index"], mark["tw_ns"], mark["venue_ms"]):
                            if (msg.get("transaction_hash"), float(msg["size"]), float(msg["price"]), msg["side"]) != (
                                    case["transaction"], mark["aggregate_shares"], target, mark["taker_side"]):
                                raise ValueError("execution frame differs")
                            observed_marks.add("execution")
                    continue
                for ci, row in enumerate(msg["price_changes"] if kind == "price_change" else [msg]):
                    if row["asset_id"] != token:
                        continue
                    if last and ms < last[1]:
                        active, last = False, None
                        buy.clear()
                        sell.clear()
                        signed_flow.clear()
                    if kind == "book":
                        active = True
                        for depth, label in ((buy,"bids"),(sell,"asks")):
                            depth.clear()
                            depth.update((float(v["price"]),float(v["size"])) for v in row[label] if float(v["size"])>0)
                    elif active:
                        depth = buy if row["side"] == "BUY" else sell
                        px, sz = float(row["price"]), float(row["size"])
                        old = depth.get(px,0)
                        for name, m in marks.items():
                            if name.startswith("addition") and (seq, mi, ci) == (m["seq"],m["message_index"],m["change_index"]):
                                if (tw, ms, px, row["side"], old, sz) != (m["tw_ns"],m["venue_ms"],target,"BUY",m["old_size"],m["new_size"]):
                                    raise ValueError("candidate addition changed")
                                observed_marks.add(name)
                        if sz:
                            depth[px] = sz
                        else:
                            depth.pop(px,None)
                    if active:
                        last = tw,ms,seq
    if observed_marks != set(marks) or set(captured) != set(marks) or not samples or any(s is None for s in samples):
        raise ValueError("case frame or continuous interval missing")
    spread = {}
    for key in ("best_bid","best_ask","bid_size","ask_size","level_size","prior_1s_print_count",
                "prior_1s_buy_shares","prior_1s_sell_shares"):
        values = [s[key] for s in samples]
        spread[key] = {"min": min(values), "max": max(values), "distinct": len(set(values))}
    return {"source": seal, "book": captured, "between_additions": {"from_tw_ns":lo,"to_tw_ns":hi,
            "prior_states_at_distinct_receipts":len(samples), "range":spread},
            "external":public_context(root, marks, start)}


if __name__ == "__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--data", type=Path, required=True)
    cli.add_argument("--case", type=Path, required=True)
    a=cli.parse_args()
    print(json.dumps(book_check(a.data,json.loads(a.case.read_bytes())),indent=2,sort_keys=True,allow_nan=False))
