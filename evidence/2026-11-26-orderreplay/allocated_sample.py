"""Replay a saved order sample using its original interval, streams and the decoded order cache."""

import argparse
import hashlib
import time
from pathlib import Path

import orjson

from doppel.lab import orderreplay, ordertape, tradeallocation


def sha(path):
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    for name in ("tape", "sample", "orders", "out"):
        ap.add_argument(name, type=Path)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    params = orjson.loads((args.sample / "report.json").read_bytes())["params"]
    start = time.monotonic()
    doc, rows = orderreplay.run(args.tape, args.sample, params["from"], params["to"],
                                params["streams"], reduce_quotes=True, orders=args.orders)
    report = args.out / "report.json"
    results = args.out / "replay.jsonl"
    report.write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
    results.write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in rows))
    source = [Path(__file__), *(Path(mod.__file__) for mod in (orderreplay, tradeallocation, ordertape))]
    inputs = [args.sample / "report.json", args.sample / "orders.jsonl", *ordertape.committed_parts(args.orders)]
    receipt = {"wall_s": round(time.monotonic() - start, 2), "params": doc["params"],
               "sha256": {str(f): sha(f) for f in [*source, *inputs, report, results]}}
    (args.out / "receipt.json").write_bytes(orjson.dumps(receipt, option=orjson.OPT_INDENT_2))
    print(doc["adapter_gate"], doc["bracket_violations"], receipt["wall_s"], flush=True)


if __name__ == "__main__":
    main()
