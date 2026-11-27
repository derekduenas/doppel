"""Trace replay expiry and arrival of the own prints for undelivered fresh-sample orders."""

import argparse
import hashlib
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import orjson

from doppel.lab import orderreplay
from doppel.lab.intlbook import IntlBook, LevelEvent, Print
from doppel.lab.tradeallocation import load_allocations


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for arg in ("sample", "tape", "orders"):
        ap.add_argument(arg, type=Path)
    args = ap.parse_args()
    replay_dir = args.sample / "orderreplay"
    report = orjson.loads((replay_dir / "orderreplay.json").read_bytes())
    raw = (replay_dir / "orderreplay.jsonl").read_bytes()
    rows = {r["id"]: r for line in raw.splitlines() if (r := orjson.loads(line))["fills_delivered"] < r["fills_routed"]}
    specs, _, _ = orderreplay.load_specs(args.sample / "ordermatch")
    specs = [s for s in specs if s.id in rows]
    pending, wanted = defaultdict(list), defaultdict(list)
    for spec in specs:
        pending[spec.key].append(spec)
        for tx, _, _ in spec.fills_at:
            wanted[(tx, spec.token)].append(spec)
    events = {s.id: {"own_prints": [], "expiry": []} for s in specs}
    record = {}
    items = []
    feed, cancel = IntlBook.feed, orderreplay.QuoteSim.cancel

    def observed_feed(self, rec):
        nonlocal record, items
        record = {"seq": rec["seq"], "tw_ns": rec["tw"]}
        items = feed(self, rec)
        for event in items:
            if isinstance(event, Print):
                for spec in specs:
                    if event.tx in {f[0] for f in spec.fills_at}:
                        events[spec.id]["own_prints"].append(record | {"print": asdict(event)})
        return items

    def observed_cancel(self, tick):
        for spec in specs:
            if self is spec.sims.get("pessimistic"):
                matches = [e for e in items if isinstance(e, (LevelEvent, Print)) and
                           (e.asset == spec.token or isinstance(e, Print) and e.market == spec.market)]
                events[spec.id]["expiry"].append(record | {"venue_ms": [e.venue_ms for e in matches],
                                                            "first_event": asdict(matches[0]) if matches else None})
        return cancel(self, tick)

    p = report["params"]
    splits = load_allocations(args.orders)
    with patch.object(IntlBook, "feed", observed_feed), patch.object(orderreplay.QuoteSim, "cancel", observed_cancel):
        for stream in p["streams"]:
            orderreplay.replay_stream(args.tape, stream, p["from"], p["to"], pending, wanted, Counter(),
                                      {}, lambda s: None, splits)
    for spec in specs:
        actual = orderreplay.result(spec)
        original = rows[spec.id]
        fields = ("price", "placed_ms", "end_ms", "fills_seen", "fills_raw_routed", "fills_allocated_routed",
                  "fills_routed", "fills_delivered", "real_filled", "optimistic", "pessimistic")
        if any(actual[k] != original[k] for k in fields):
            raise ValueError(f"isolated trace diverged from the frozen replay for {spec.id}")
        events[spec.id]["reproduced"] = {k: actual[k] for k in fields}
        events[spec.id]["placed_tw_ns"] = spec.placed_tw
    print(orjson.dumps({"frozen_replay_sha256": hashlib.sha256(raw).hexdigest(), "orders": events},
                       option=orjson.OPT_INDENT_2).decode())


if __name__ == "__main__":
    main()
