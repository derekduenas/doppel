"""Repeat the native matcher comparison on the frozen first 15 minutes.

On Apex, extract src from the code revision below into the private research
scratch folder native-723ad93. Copy this script there and execute:

    cd /var/backups/doppel-sealed-20261126/research/wallet-inspection.jOPOHm/native-723ad93
    systemd-run --unit=doppel-standard-first15-723ad93 --property=MemoryMax=1G \
        --property=MemorySwapMax=0 --property=Nice=19 --property=IOSchedulingClass=idle \
        --setenv=PYTHONPATH="$PWD/src" /opt/doppel/.venv/bin/python "$PWD/standard_first15.py"
"""
import hashlib
from pathlib import Path

import orjson

from doppel.lab.ordermatch import run
from doppel.lab.inspect_wallet import sealed_files
from doppel.lab.ordertape import committed_parts


def sha(path):
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


archives = Path("/var/backups/doppel-sealed-20261126")
study = archives / "research/wallet-inspection.jOPOHm"
receipt = orjson.loads((study / "paired-first15/summary.json").read_bytes())
orders = archives / "research/wallet-absorption.crsLww/first-orders"
tape = archives / "tape"
stream = receipt["stream"]
start, end = receipt["window_time"]
assert (start, end, stream) == (1795629600, 1795630500, "clob_btc_5m")
wallets = [r["wallet"] for r in receipt["inputs"]]
inputs, order_seal = sealed_files(orders, [*committed_parts(orders), *orders.glob("_state/flush-*.json")])
# Published independently in evidence/2026-11-27-wallet-absorption/run-summary.json.
assert order_seal == "d60083957ca6fec33781adee8b9e37c55b39159bae51276b3fc6ff1d83b15f47"
assert all(inputs == r["signed_order_files"] and order_seal == r["signed_order_manifest_sha256"]
           for r in receipt["inputs"])
segments = {s["file"]: sha(tape / s["file"]) for s in receipt["source_segments"]}
assert segments and all(segments[s["file"]] == s["sha256"] for s in receipt["source_segments"])
doc, rows, _ = run(tape, orders, start, end, wallets, streams=[stream], min_fp_orders=10**9, focus=True)
out = {
    "code_revision": "723ad93a7e743f91a6ca6d9d6b9b89bb785b975f",
    "procedure": "standard_first15.py",
    "resource_limits": {"MemoryMax": "1G", "MemorySwapMax": 0, "Nice": 19, "IOSchedulingClass": "idle"},
    "window": [start, end], "order_source": str(orders), "source_order_sha256": inputs,
    "source_order_manifest_sha256": order_seal,
    "tape_source": str(tape), "source_segment_sha256": segments,
    "wallet_scope": {r["wallet"]: {"layer1_sha256": r["layer1_sha256"],
                                   "orders_with_maker_fills_by_fill_time": r["orders_with_maker_fills"]}
                     for r in receipt["inputs"]},
    "focus": True, "min_fp_orders": 10**9, "report": doc, "representative_rows": len(rows),
}
Path(__file__).with_name("standard-first15.json").write_bytes(orjson.dumps(out, option=orjson.OPT_INDENT_2) + b"\n")
