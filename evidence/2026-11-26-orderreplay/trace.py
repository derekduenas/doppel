"""Trace the largest observed pessimistic overfill in the saved replay sample."""

import argparse
from pathlib import Path

import orjson

from doppel.lab import orderreplay as replay
from doppel.lab.intlbook import LevelEvent, Print


def rows(path):
    return [orjson.loads(line) for line in path.read_bytes().splitlines() if line]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tape", type=Path)
    ap.add_argument("sample", type=Path)
    args = ap.parse_args()
    report = orjson.loads((args.sample / "report.json").read_bytes())
    target = max(rows(args.sample / "replay.jsonl"), key=lambda r: r["pessimistic"]["error"])
    spec = next(r for r in rows(args.sample / "orders.jsonl") if r["order_id"] == target["id"])
    print("matched", spec, flush=True)
    original_replay = replay.replay
    original_trade = replay.QuoteSim._on_trade

    def clock_rows(root, stream, start, end, book):
        for items in original_replay(root, stream, start, end, book):
            token = spec["token_id"]
            market = book.market_of.get(token)
            for item in items:
                if (isinstance(item, (Print, LevelEvent)) and market and item.market == market
                        and spec["placed_venue_ms"] - 50 <= item.venue_ms <= spec["end_ms"] + 50):
                    price = item.price if item.asset == token else replay.px(1 - item.price)
                    if isinstance(item, Print) or price == spec["price"]:
                        print("frame", item, flush=True)
            yield items

    def trace_trade(self, tick, trade):
        q = self.live["bid" if spec["side"] == "BUY" else "ask"]
        if (q is None or q.price != spec["price"] or q.live_ns is None
                or q.live_ns // replay.MS_NS != spec["placed_venue_ms"]):
            return original_trade(self, tick, trade)
        queue, remaining = q.queue, q.remaining
        original_trade(self, tick, trade)
        if (queue, remaining) != (q.queue, q.remaining):
            print("trade", trade, "queue", queue, q.queue, "remaining", remaining, q.remaining, flush=True)

    replay.replay = clock_rows
    replay.QuoteSim._on_trade = trace_trade
    params = report["params"]
    result, out = replay.run(args.tape, args.sample, params["from"], params["to"], params["streams"],
                             reduce_quotes=True)
    print("result", next(r for r in out if r["id"] == target["id"]), flush=True)
    print("bracket", result["bracket_violations"], flush=True)


if __name__ == "__main__":
    main()
