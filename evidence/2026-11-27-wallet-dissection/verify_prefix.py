"""Reproduce each signed-token observation from only its strict raw prefix and changed future."""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import tempfile
import zlib

import orjson
import zstandard

from doppel.lab.decision_book import scan
from doppel.lab.goldsky_proof import digest
from doppel.tape.segment import read_segment


def run(tape, case_path, summary_path, independent_path, scratch):
    case, summary, other = [orjson.loads(p.read_bytes()) for p in (case_path, summary_path, independent_path)]
    if summary["case_sha256"] != digest(case_path):
        raise ValueError("case differs from full replay")
    seal = other["source"]
    if seal["sha256"] != digest(tape / seal["file"]) or seal["sha256"] != case["source"]["sha256"]:
        raise ValueError("source seal mismatch")
    stream = Path(seal["file"]).parts[0]
    token = case["tx_print"]["token"]
    price_side = (case["signed_price"], case["signed_maker_side"])
    start, end = summary["window_time"]
    output = {}
    for name, row in summary["primary"].items():
        expected = row["signed"]["state"]
        independent = other["book"][name]
        if (not expected["known"] or expected["flow_status"] != "covered" or
                any(expected[k] != v if k.startswith("last_depth") else
                    not math.isclose(expected[k], v, abs_tol=1e-6, rel_tol=0)
                    for k, v in independent.items())):
            raise ValueError(f"independent reader disagrees at {name}")
        tw = row["receive_cutoff_ns"]
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            root = Path(tmp)
            path = root / seal["file"]
            path.parent.mkdir(parents=True)
            comp = zstandard.ZstdCompressor(level=1)
            chunk = bytearray()
            count = 0
            end_rec = None
            with path.open("wb") as out:
                for rec in read_segment(tape / seal["file"], strict=True):
                    if rec["tw"] >= tw:
                        break
                    chunk.extend(orjson.dumps(rec))
                    chunk.extend(b"\n")
                    if len(chunk) > 1024 * 1024:
                        out.write(comp.compress(chunk))
                        chunk.clear()
                    end_rec = rec
                    count += 1
                if chunk:
                    out.write(comp.compress(chunk))
            if end_rec is None:
                raise ValueError("empty prefix")
            def observe():
                result = scan(root, stream, start, end, [(tw, token, name)], {}, set(), {}, 300,
                              grid_from=end, query_levels={name: price_side}, details=True)
                return result["detail"][name], result["state_integrity"][name]
            before, prefix_integrity = observe()
            snapshot = {"event_type": "book", "market": case["tx_print"]["market"], "asset_id": token,
                        "timestamp": str(tw // 10**6), "bids": [{"price": "0.01", "size": "99999"}],
                        "asks": [{"price": "0.99", "size": "88888"}]}
            trade = {"event_type": "last_trade_price", "market": case["tx_print"]["market"], "asset_id": token,
                     "side": "BUY", "price": "0.99", "size": "77777", "timestamp": str(tw // 10**6)}
            with path.open("ab") as out:
                for i, payload in enumerate((snapshot, trade)):
                    raw = orjson.dumps(payload)
                    record = {"seq": end_rec["seq"] + i + 1, "stream": stream, "tw": tw + i,
                              "tm": end_rec["tm"] + (tw - end_rec["tw"]) + i, "raw": raw.decode(),
                              "crc": zlib.crc32(raw)}
                    out.write(comp.compress(orjson.dumps(record) + b"\n"))
            mutated, mutated_integrity = observe()
            if expected != before or expected != mutated:
                raise ValueError(f"receipt-time state changed at {name}")
        output[name] = {"cutoff_ns": tw, "prior_records": count, "last_prefix_receipt_ns": end_rec["tw"],
                        "independent_numeric_fields_equal": sorted(independent),
                        "all_causal_fields_equal": sorted(expected),
                        "offline_integrity": {"full": row["signed"]["retrospective_integrity"],
                                              "prefix": prefix_integrity, "changed_future": mutated_integrity}}
    return {"schema": "wallet-dissection-prefix-check/1", "source_sha256": seal["sha256"],
            "full_summary_sha256": digest(summary_path), "independent_sha256": digest(independent_path),
            "signed_token": token, "checks": output}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    for k in ("data", "case", "summary", "independent", "scratch"):
        cli.add_argument("--" + k, required=True, type=Path)
    a = cli.parse_args()
    print(orjson.dumps(run(a.data, a.case, a.summary, a.independent, a.scratch), option=orjson.OPT_INDENT_2).decode())
