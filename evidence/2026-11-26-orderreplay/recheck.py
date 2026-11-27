"""Recheck a saved sample and vary the anomalous order's plausible placement."""

import argparse
from pathlib import Path

import orjson

from doppel.lab.ordermatch import Params, build_tape, placement_candidate_count
from doppel.lab.orderreplay import run


def rows(path):
    return [orjson.loads(line) for line in path.read_bytes().splitlines() if line]


def save_rows(path, records):
    path.write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in records))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tape", type=Path)
    ap.add_argument("sample", type=Path)
    ap.add_argument("out", type=Path)
    args = ap.parse_args()
    original = orjson.loads((args.sample / "report.json").read_bytes())
    params = original["params"]
    start, end, streams = params["from"], params["to"], params["streams"]
    orders = rows(args.sample / "orders.jsonl")
    target = max(rows(args.sample / "replay.jsonl"), key=lambda r: r["pessimistic"]["error"])
    p = Params(size_tol_rel=0.02)
    tape = build_tape(args.tape, streams, start, end, {r["token_id"] for r in orders}, [], p)
    variants = []
    for row in orders:
        events = tape.events[(row["token_id"], row["price"], row["side"])]
        first = min((f[1] for f in row["fills_at"] if f[1] is not None), default=None)
        row["placement_candidates"] = placement_candidate_count(events, row["created_ms"], row["size"], p, first)
        if row["order_id"] == target["id"]:
            assert first is not None
            for e in events:
                dt = e.venue_ms - row["created_ms"]
                if p.lo_ms <= dt <= p.hi_ms and e.venue_ms <= first and p.near(e.delta, row["size"]):
                    variants.append(row | {
                        "order_id": f"placement-{len(variants) + 1}", "placed_venue_ms": e.venue_ms,
                        "placed_tw_ns": e.tw_ns, "placed_src": e.src,
                    })
    del tape
    args.out.mkdir(parents=True, exist_ok=False)
    save_rows(args.out / "orders.jsonl", orders)
    report, replay = run(args.tape, args.out, start, end, streams, reduce_quotes=True)
    (args.out / "report.json").write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
    save_rows(args.out / "replay.jsonl", replay)
    print("brackets", report["bracket_violations"], flush=True)
    cases = args.out / "placements"
    cases.mkdir()
    save_rows(cases / "orders.jsonl", variants)
    _, results = run(args.tape, cases, start, end, streams, reduce_quotes=True)
    save_rows(cases / "replay.jsonl", results)
    for r in results:
        print(r["id"], r["placed_ms"], r["queue_ahead"], r["real_filled"],
              r["pessimistic"]["filled"], r["optimistic"]["filled"], flush=True)


if __name__ == "__main__":
    main()
