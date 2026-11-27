"""Independent re-read of one signed candidate order's book evidence from a sealed hour."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

import orjson

from doppel.tape.segment import read_segment

FILE = "clob_btc_5m/20261125/18.dtape.zst"
SEAL = "9691cd2cc196e6d9e5a36dedf10eba79a9d4cfbacb3146aa6bcda53dc83de883"
BASE = 1795629600
TOKEN = "26573549377964720488352327783145014153434918240677129399684144995785340448937"
CONDITION = "0x54960a36107b4c53431898bcf2d991af3038ca75ffe9b266ea0f54fbd0007204"
TX = "0x32aa9fa6cb2815070bc6e3a9d9d56163e24c3aa09a5ca78017f27fc977a2b6d8"
ORDER = "e1a6ed8291ccbf3794e99f5ce9c8ce8c196a9473d1883c073dad35b73b78253a"
PRICE = .07
SIZE = 60.0


def crosscheck(tape, advance_depth_on_print=False):
    path = tape / FILE
    before = path.stat()
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SEAL:
            raise ValueError("BTC book bytes differ from the original hour seal")
    spans, prints, adds, empties = [], [], [], []
    depth = active = previous = None
    control = Counter()

    def reset(reason, point):
        nonlocal depth, active
        if active is not None:
            active["reset_reason"] = reason
            active["reset_seen"] = point
        depth = active = None

    for rec in read_segment(path, strict=True, verify=True):
        tw, seq = rec["tw"], rec["seq"]
        if rec["stream"] != "clob_btc_5m" or not BASE * 10**9 <= tw < (BASE + 3600) * 10**9:
            raise ValueError("record scope differs from the sealed hour")
        if tw >= (BASE + 900) * 10**9:
            continue
        if previous is not None and (seq != previous[0] + 1 or tw < previous[1]):
            reset("record discontinuity", {"tw_ns": tw, "seq": seq})
        previous = seq, tw
        ctl = (rec.get("meta") or {}).get("ctl")
        if ctl:
            control[ctl] += 1
            if ctl != "subscribe_add":
                reset(ctl, {"tw_ns": tw, "seq": seq})
            continue
        if rec["raw"] in ("PONG", "PING", ""):
            continue
        payload = orjson.loads(rec["raw"])
        for message_index, msg in enumerate(payload if isinstance(payload, list) else [payload]):
            kind = msg.get("event_type")
            if kind not in ("price_change", "book", "last_trade_price"):
                if kind not in ("tick_size_change", "best_bid_ask", "market_resolved", "new_market"):
                    reset("unrecognized message", {"tw_ns": tw, "seq": seq})
                continue
            v = int(msg["timestamp"])
            changes = msg["price_changes"] if kind == "price_change" else [msg]
            for change_index, item in enumerate(changes):
                point = {"venue_ms": v, "tw_ns": tw, "seq": seq, "message_index": message_index,
                         "change_index": change_index}
                is_print = kind == "last_trade_price" and msg.get("transaction_hash", "").lower() == TX
                asset = item.get("asset_id")
                if asset == TOKEN and (kind != "last_trade_price" or advance_depth_on_print):
                    if msg["market"].lower() != CONDITION:
                        raise ValueError("book condition differs from the audited condition")
                    if active is not None and v < active["last"]["venue_ms"]:
                        reset("venue timestamp reversal", point)
                    if active is None and kind == "book":
                        active = {"id": len(spans) + 1, "first": point, "last": point}
                        spans.append(active)
                    if active is not None:
                        active["last"] = point
                        if kind == "book":
                            depth = sum(float(r["size"]) for r in msg["bids"] if float(r["price"]) == PRICE)
                            if depth == 0:
                                empties.append(dict(point, span=active["id"], source="book"))
                        elif kind == "price_change" and item["side"] == "BUY" and float(item["price"]) == PRICE:
                            new = float(item["size"])
                            if depth is None:
                                raise ValueError("increment applied without an initialized level")
                            if new == 0 or depth == 0:
                                empties.append(dict(point, span=active["id"],
                                                    source="price_change_zero" if new == 0 else "before_price_change"))
                            if abs(new - depth - SIZE) <= .020000001:
                                adds.append(dict(point, span=active["id"], old_size=depth, new_size=new))
                            depth = new
                if is_print:
                    prints.append(dict(point, span=active["id"] if active and asset == TOKEN else None,
                                       token=asset, market=msg["market"], taker_side=msg["side"],
                                       price=float(msg["price"]), aggregate_shares=float(msg["size"])))
    after = path.stat()
    if any(getattr(before, key) != getattr(after, key) for key in ("st_dev", "st_ino", "st_size", "st_ctime_ns", "st_mtime_ns")):
        raise ValueError("source changed during inspection")
    if len(prints) != 1:
        raise ValueError(f"expected one transaction print, got {len(prints)}")
    anchor = prints[0]
    if (anchor["token"], anchor["market"].lower(), anchor["price"], anchor["taker_side"]) != (
            TOKEN, CONDITION, PRICE, "SELL") or abs(anchor["venue_ms"] - 1795629745000) > 120_000:
        raise ValueError("unexpected first transaction print")
    arms = {}
    for arm, shift in (("actual", 0), ("placebo", -60_000)):
        ms = anchor["venue_ms"] + shift
        tw = anchor["tw_ns"] + shift * 10**6
        lo, low_tw = ms - 10_000, tw - 10_000 * 10**6
        relevant = [s for s in spans if s["first"]["venue_ms"] <= ms and s["last"]["venue_ms"] >= lo
                    and s["first"]["tw_ns"] <= tw and s["last"]["tw_ns"] >= low_tw]
        full = [s for s in relevant if s["first"]["venue_ms"] <= lo and s["last"]["venue_ms"] >= ms
                and s["first"]["tw_ns"] <= low_tw and s["last"]["tw_ns"] >= tw]
        candidates = [e for e in adds if lo <= e["venue_ms"] < ms and low_tw <= e["tw_ns"] < tw]
        arms[arm] = {"search_start_ms": lo, "endpoint_ms": ms, "search_start_tw": low_tw, "endpoint_tw": tw,
                     "continuous": len(full) == 1, "overlapping_initialized_fragments": relevant,
                     "fragment_candidate_count": len(candidates), "fragment_candidates": candidates}
    previous_zeros = [e for e in empties if e["span"] == anchor["span"] and e["venue_ms"] < anchor["venue_ms"]
                      and e["tw_ns"] < anchor["tw_ns"]]
    zero = max(previous_zeros, key=lambda e:(e["venue_ms"], e["tw_ns"], e["seq"])) if previous_zeros else None
    return {"schema": "wallet-inspection-first-order-crosscheck/1",
            "signed_evidence": "../2026-11-27-wallet-absorption/first-trade.json",
            "wallet": "0xf9b7876ba2c35370e0bedb7f8ee772da722aa37c", "transaction": TX, "order_hash": ORDER,
            "signed_maker_side":"BUY", "signed_size": SIZE, "signed_price": PRICE,
            "source": {"file":FILE, "bytes": before.st_size, "sha256":SEAL, "selected_utc": [BASE, BASE + 900],
                       "section_control_counts": dict(control)},
            "tx_print": anchor, "arms": arms, "last_zero_in_print_span": zero,
            "qualified_residence_upper_ms": (anchor["venue_ms"] - zero["venue_ms"]
                                             if zero and arms["actual"]["continuous"] else None),
            "interpretation": "The print labels an aggregate execution. Depth updates are anonymous. "
                              "Fragment counts can establish multiplicity over the entire search window only when the "
                              "window is continuously covered. A missing continuous window is excluded from the paired comparison."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape", required=True, type=Path)
    tape = parser.parse_args().tape
    current = crosscheck(tape)
    old = crosscheck(tape, advance_depth_on_print=True)
    current["superseded_print_clock"] = {
        arm: {"complete": values["continuous"], "partial_adds": values["fragment_candidate_count"],
              "initialized_fragments": len(values["overlapping_initialized_fragments"])}
        for arm, values in old["arms"].items()}
    print(json.dumps(current, indent=2, sort_keys=True, allow_nan=False))
