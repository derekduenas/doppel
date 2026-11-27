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
from doppel.tape.segment import read_segment, segment_path


def run(tape, case_path, summary_path, independent_path, scratch):
    case, summary, other = [orjson.loads(p.read_bytes()) for p in (case_path, summary_path, independent_path)]
    if summary["case_sha256"] != digest(case_path):
        raise ValueError("case differs from full replay")
    seal = other["source"]
    start, end = summary["window_time"]
    stream = "clob_btc_5m"
    source = segment_path(tape, stream, start * 10**9)
    relative = source.relative_to(tape).as_posix()
    if not (relative == seal["file"] == case["source"]["file"]
            and any(r["file"] == relative and r["sha256"] == seal["sha256"] for r in summary["source_segments"])):
        raise ValueError("source path differs from the registered BTC five-minute segment")
    if seal["sha256"] != digest(source) or seal["sha256"] != case["source"]["sha256"]:
        raise ValueError("source seal mismatch")
    token = case["tx_print"]["token"]
    price_side = (case["signed_price"], case["signed_maker_side"])
    marks = {f"addition_{i}": m for i, m in enumerate(case["arms"]["actual"]["fragment_candidates"], 1)}
    marks["execution"] = case["tx_print"]
    if set(marks) != {"addition_1", "addition_2", "execution"} or set(summary["primary"]) != set(marks) or set(other["book"]) != set(marks):
        raise ValueError("primary anchors differ from preregistration")
    numeric = {"best_bid", "best_ask", "level_size", "bid_size", "ask_size", "last_depth_seq",
               "last_depth_tw_ns", "last_depth_venue_ms", "prior_1s_print_count", "prior_1s_buy_shares",
               "prior_1s_sell_shares"}
    output = {}
    for name, row in summary["primary"].items():
        expected = row["signed"]["state"]
        independent = other["book"][name]
        if (set(independent) != numeric or not expected["known"] or expected["flow_status"] != "covered" or
                any(expected[k] != v if k.startswith("last_depth") else
                    not math.isclose(expected[k], v, abs_tol=1e-6, rel_tol=0)
                    for k, v in independent.items())):
            raise ValueError(f"independent reader disagrees at {name}")
        tw = row["receive_cutoff_ns"]
        mate = row["complement"]["token"]
        if (row["signed"]["token"] != token or not mate or mate == token or
                (tw, row["anchor_seq"], row["anchor_venue_ms"]) !=
                (marks[name]["tw_ns"], marks[name]["seq"], marks[name]["venue_ms"])):
            raise ValueError("primary replay differs from the registered anchor or token")
        legs = {"signed": token, "complement": mate}
        levels = {"signed": price_side, "complement": (round(1-price_side[0], 4), "SELL" if price_side[1] == "BUY" else "BUY")}
        with tempfile.TemporaryDirectory(dir=scratch) as tmp:
            root = Path(tmp)
            path = root / relative
            path.parent.mkdir(parents=True)
            comp = zstandard.ZstdCompressor(level=1)
            chunk = bytearray()
            count = 0
            end_rec = None
            with path.open("wb") as out:
                for rec in read_segment(source, strict=True):
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
                result = scan(root, stream, start, end, [(tw, tok, leg) for leg,tok in legs.items()],
                              {}, set(), {}, 300, grid_from=end, query_levels=levels, details=True)
                return result["detail"], result["state_integrity"]
            before, prefix_integrity = observe()
            snapshot = {"event_type": "book", "market": case["tx_print"]["market"], "asset_id": token,
                        "timestamp": str(tw // 10**6), "bids": [{"price": "0.01", "size": "99999"}],
                        "asks": [{"price": "0.99", "size": "88888"}]}
            trade = {"event_type": "last_trade_price", "market": case["tx_print"]["market"], "asset_id": token,
                     "side": "BUY", "price": "0.99", "size": "77777", "timestamp": str(tw // 10**6)}
            with path.open("ab") as out:
                for i, payload in enumerate((snapshot, snapshot | {"asset_id":mate}, trade)):
                    raw = orjson.dumps(payload)
                    record = {"seq": end_rec["seq"] + i + 1, "stream": stream, "tw": tw + (i//2),
                              "tm": end_rec["tm"] + (tw - end_rec["tw"]) + (i//2), "raw": raw.decode(),
                              "crc": zlib.crc32(raw)}
                    out.write(comp.compress(orjson.dumps(record) + b"\n"))
            mutated, mutated_integrity = observe()
            full = {leg: row[leg]["state"] for leg in legs}
            if full != before or full != mutated:
                raise ValueError(f"receipt-time state changed at {name}")
        output[name] = {"cutoff_ns": tw, "prior_records": count, "last_prefix_receipt_ns": end_rec["tw"],
                        "independent_numeric_fields_equal": sorted(independent),
                        "all_causal_fields_equal_on_both_legs": sorted(expected),
                        "offline_integrity": {"full": {leg: row[leg]["retrospective_integrity"] for leg in legs},
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
