"""Apply the continuous-residence check to already-sealed version 1 per-order observations."""
import argparse
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory

import orjson

from doppel.lab.inspect_wallet import qualify_residence, summary


def convert(source, out):
    if out.exists():
        raise FileExistsError(out)
    old = (source / "summary.json").read_bytes()
    doc = orjson.loads(old)
    raw = (source / "orders.jsonl").read_bytes()
    if doc["schema"] != "wallet-inspection/1" or hashlib.sha256(raw).hexdigest() != doc["orders_sha256"]:
        raise ValueError("expected an intact version 1 study")
    rows = [orjson.loads(line) for line in raw.splitlines()]
    for row in rows:
        for arm in ("actual", "placebo"):
            row.get(arm, {}).pop("conditional_add_to_anchor_ms", None)
        qualify_residence(row)
    by_wallet = {}
    for wallet, counts in doc["wallets"].items():
        new = summary([r for r in rows if r["wallet"] == wallet])
        if any(new[k] != value for k, value in counts.items()):
            raise ValueError(f"registered comparisons changed for {wallet}")
        by_wallet[wallet] = new
    doc["wallets"] = by_wallet
    doc["schema"] = "wallet-inspection/2"
    doc["prior_summary_sha256"] = hashlib.sha256(old).hexdigest()
    doc["prior_orders_sha256"] = doc["orders_sha256"]
    result = b"".join(orjson.dumps(r) + b"\n" for r in rows)
    doc["orders_sha256"] = hashlib.sha256(result).hexdigest()
    with TemporaryDirectory(dir=out.parent) as tmp:
        folder = Path(tmp) / "results"
        folder.mkdir()
        (folder / "orders.jsonl").write_bytes(result)
        (folder / "summary.json").write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2) + b"\n")
        folder.rename(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("out", type=Path)
    args = parser.parse_args()
    convert(args.source, args.out)
