"""Independently join archived CLOB frames, committed decoded makers and one missed route."""

import argparse
import hashlib
from collections import Counter
from pathlib import Path

import orjson
import pyarrow.parquet as pq

from doppel.lab.intlbook import px
from doppel.lab.ordermatch import filled_shares
from doppel.lab.ordertape import committed_parts
from doppel.tape.segment import read_segment


def sha(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def read_row(path, key, value):
    with path.open("rb") as file:
        return next(r for line in file if (r := orjson.loads(line))[key] == value)


def trace(sample, segment, orders, oid, tx):
    baseline = read_row(sample / "replay.jsonl", "id", oid)
    order = read_row(sample / "orders.jsonl", "order_id", oid)
    if tx not in {entry[0] for entry in order["fills_at"]}:
        raise ValueError("selected transaction is absent from the order")
    frames = []
    for rec in read_segment(segment):
        if tx not in rec["raw"]:
            continue
        obj = orjson.loads(rec["raw"])
        for frame in obj if isinstance(obj, list) else [obj]:
            if frame.get("event_type") == "last_trade_price" and frame.get("transaction_hash") == tx:
                frames.append({"seq": rec["seq"], "tw": rec["tw"], "message": frame})
    if not frames:
        raise ValueError("no matching public frame")
    decoded = []
    sources = {}
    for part in committed_parts(orders):
        rows = pq.read_table(part, filters=[("tx", "=", tx)]).to_pylist()
        if rows:
            sources[part.relative_to(orders).as_posix()] = sha(part)
            decoded.extend(rows)
    makers = [r for r in decoded if r["role"] == "maker"]
    takers = [r for r in decoded if r["role"] == "taker"]
    if (len(takers) != 1 or not makers or any(r["n_makers"] != len(makers) for r in decoded)
            or len({r["order_id"] for r in makers}) != len(makers)
            or not any(r["verified"] is True and r["order_id"] == oid for r in makers)):
        raise ValueError("incomplete maker set or missing verified order")
    condition = takers[0]["condition_id"].removeprefix("0x")
    if any(r["condition_id"].removeprefix("0x") != condition for r in makers) or any(
            f["message"]["market"].removeprefix("0x") != condition for f in frames):
        raise ValueError("market mismatch")
    tokens = {r["token_id"] for r in decoded} | {f["message"]["asset_id"] for f in frames}
    if order["token_id"] not in tokens or len(tokens) > 2:
        raise ValueError("token pair mismatch")
    levels = Counter()
    for r in makers:
        native = r["token_id"] == order["token_id"]
        price = px(r["limit"] if native else 1 - r["limit"])
        side = r["side"] if native else {"BUY": "SELL", "SELL": "BUY"}[r["side"]]
        levels[(side, price)] += filled_shares(r)
    return {
        "order_id": oid, "transaction": tx, "token_id": order["token_id"],
        "maker": order["maker"], "price": order["price"], "side": order["side"],
        "observed_order_shares": order["filled"], "placed_venue_ms": order["placed_venue_ms"],
        "baseline": {k: baseline[k] for k in ("fills_seen", "fills_routed", "fills_delivered",
                                              "queue_ahead", "pessimistic", "optimistic")},
        "sources_sha256": {"segment": {str(segment): sha(segment)}, "order_parts": sources,
                           "baseline_replay": sha(sample / "replay.jsonl"),
                           "baseline_orders": sha(sample / "orders.jsonl")},
        "matching_public_frames": frames, "decoded_makers": len(makers),
        "maker_shares": [{"side": s, "price": p, "shares": round(q, 6)} for (s, p), q in sorted(levels.items())],
        "maker_shares_total": round(sum(levels.values()), 6),
    }


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    for arg in ("sample", "segment", "orders"):
        ap.add_argument(arg, type=Path)
    ap.add_argument("order_id")
    ap.add_argument("tx")
    args = ap.parse_args()
    report = trace(args.sample, args.segment, args.orders, args.order_id, args.tx)
    print(orjson.dumps(report, option=orjson.OPT_INDENT_2).decode())
