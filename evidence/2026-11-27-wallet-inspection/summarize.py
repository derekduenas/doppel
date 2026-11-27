"""Verify retained per-order results against the published sources and independent first case."""
import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
from statistics import median


def read(path):
    return json.loads(path.read_bytes())


def checksum(path):
    return sha256(path.read_bytes()).hexdigest()


def quartiles(values):
    if not values:
        return None
    return {"min": min(values), "median": median(values), "max": max(values)}


def wallet_rows(rows):
    paired = [row for row in rows if row["paired_coverage"]]
    seen = Counter()
    for row in paired:
        actual = row["actual"]["paired_sole_uncontested_add"]
        placebo = row["placebo"]["paired_sole_uncontested_add"]
        seen["both" if actual and placebo else "actual_only" if actual else "placebo_only" if placebo else "neither"] += 1
    return {"conditions": len({r["market"] for r in rows}), "unique_signed_orders": len(rows),
            "signed_to_block_seconds": quartiles([(r["created_ms"] - 1000 * r["block_ts"]) / 1000 for r in rows]),
            "fully_filled_by_observed_trades": sum(r["fully_filled_by_observed_trades"] for r in rows),
            "paired_sole_by_arm": {k: seen[k] for k in ("both", "actual_only", "placebo_only", "neither")},
            "paired_sole_against_all_observed_orders":
                {arm: sum(r[arm]["sole_uncontested_add"] for r in paired) for arm in ("actual", "placebo")},
            "maximum_displayed_residence_bounds_ms":
                quartiles([r["residence_upper_ms"] for r in rows if r["residence_upper_ms"] is not None]),
            "owner_identified_placements": sum(r["placement_identity"] != "unknown" for r in rows),
            "known_cancellations": sum(r["cancellation"] != "unknown" for r in rows)}


def collect(folder, study, first=None):
    report = folder / "summary.json"
    doc = read(report)
    order_file = folder / "orders.jsonl"
    if checksum(order_file) != doc["orders_sha256"] or checksum(study) != doc["layer1_summary_sha256"]:
        raise ValueError("results or upstream study digest changed")
    upstream = read(study)
    rows = [json.loads(line) for line in order_file.read_bytes().splitlines()]
    if len({(r["wallet"], r["order_id"]) for r in rows}) != len(rows):
        raise ValueError("duplicate signed order")
    wallets = {}
    for source in doc["inputs"]:
        w = source["wallet"]
        sub = [r for r in rows if r["wallet"] == w]
        seals = [(win, rec) for win in upstream["windows"].values() for rec in win["wallets"]
                 if w == rec["wallet"] and rec["receipt"]["sha256"] == source["layer1_sha256"]]
        (win, rec), = seals
        if (source["bound_source_fills"] != rec["goldsky_fills"] or
                source["signed_order_manifest_sha256"] != upstream["inputs"][win["order_tape"]]["manifest_sha256"] or
                source["store_manifest_sha256"] != upstream["inputs"][win["store"]]["manifest_sha256"]):
            raise ValueError(f"source binding changed for {w}")
        if (len(sub) != source["orders_with_maker_fills"] or len(sub) != doc["wallets"][w]["maker_orders"] or
                sum(r["paired_coverage"] for r in sub) != doc["wallets"][w]["paired_orders"]):
            raise ValueError(f"per-order denominator disagrees for {w}")
        wallets[w] = wallet_rows(sub)
    if first is not None:
        case = read(first)
        item, = [r for r in rows if (r["wallet"], r["order_id"]) == (case["wallet"], case["order_hash"])]
        anchor, direct = item["anchor"], case["tx_print"]
        if (item["first_observed_tx"] != case["transaction"] or
                [anchor[k] for k in ("seq", "venue_ms", "tw_ns", "size", "side")] !=
                [direct[k] for k in ("seq", "venue_ms", "tw_ns", "aggregate_shares", "taker_side")] or
                item["residence_upper_ms"] != case["qualified_residence_upper_ms"] or
                item["actual"]["size_compatible_adds"] != case["arms"]["actual"]["fragment_candidate_count"] or
                item["continuous_coverage"] != case["arms"]["actual"]["continuous"] or
                item["paired_coverage"] != (item["continuous_coverage"] and case["arms"]["placebo"]["continuous"])):
            raise ValueError("independent first-order check disagrees")
    segments = doc["source_segments"]
    gaps = [(right["first_tw"] - left["last_tw"], left["last_tw"], right["first_tw"])
            for left, right in zip(segments, segments[1:]) if left["last_tw"] and right["first_tw"]]
    return {"summary_sha256": checksum(report), "per_order_sha256": doc["orders_sha256"],
            "source_files": len(segments), "source_bytes": sum(p["bytes"] for p in segments),
            "missing_source_files": doc["missing_segments"], "largest_between_file_gap_ns":
                dict(zip(("duration", "last_receipt", "next_receipt"), max(gaps))) if gaps else None,
            "wallets": wallets}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--study", required=True, type=Path, help="Apex wallet-inspection working directory")
    cli.add_argument("--layer1-summary", required=True, type=Path)
    cli.add_argument("--first-case", required=True, type=Path)
    args = cli.parse_args()
    out = {name: collect(args.study / name, args.layer1_summary, args.first_case if name == "paired-first15" else None)
           for name in ("paired-first15", "paired-day")}
    print(json.dumps(out, sort_keys=True, indent=2, allow_nan=False))
