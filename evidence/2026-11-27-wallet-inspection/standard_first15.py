"""Repeat the native matcher comparison on the frozen first 15 minutes."""
import hashlib
from pathlib import Path

import orjson

from doppel.lab.ordermatch import run


def sha(path):
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


archives = Path("/var/backups/doppel-sealed-20261126")
study = archives / "research/wallet-inspection.jOPOHm"
receipt = orjson.loads((study / "first15/summary.json").read_bytes())
orders = archives / "research/wallet-absorption.crsLww/first-orders"
tape = archives / "tape"
stream = receipt["stream"]
start, end = receipt["window_time"]
assert (start, end, stream) == (1795629600, 1795630500, "clob_btc_5m")
wallets = [r["wallet"] for r in receipt["inputs"]]
inputs = {name: sha(orders / name) for name in receipt["inputs"][0]["signed_order_parts"]}
assert all(inputs == r["signed_order_parts"] for r in receipt["inputs"])
segments = {s["file"]: sha(tape / s["file"]) for s in receipt["source_segments"]}
assert segments and all(segments[s["file"]] == s["sha256"] for s in receipt["source_segments"])
doc, rows, _ = run(tape, orders, start, end, wallets, streams=[stream], min_fp_orders=10**9, focus=True)
out = {
    "code_revision": "75ebb2f8bef23e854deaba05099a4a8e84b41b0f",
    "procedure": "standard_first15.py",
    "resource_limits": {"MemoryMax": "1G", "MemorySwapMax": 0, "Nice": 19, "IOSchedulingClass": "idle"},
    "window": [start, end], "order_source": str(orders), "source_order_sha256": inputs,
    "tape_source": str(tape), "source_segment_sha256": segments,
    "wallet_scope": {r["wallet"]: {"layer1_sha256": r["layer1_sha256"],
                                   "orders_with_maker_fills_by_fill_time": r["orders_with_maker_fills"]}
                     for r in receipt["inputs"]},
    "focus": True, "min_fp_orders": 10**9, "report": doc, "representative_rows": len(rows),
}
(study / "native-75ebb2f/standard-first15.json").write_bytes(orjson.dumps(out, option=orjson.OPT_INDENT_2) + b"\n")
