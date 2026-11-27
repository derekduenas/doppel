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


def wallet_rows(rows, tokens, capture_gaps):
    paired = [row for row in rows if row["paired_coverage"]]
    seen = Counter()
    for row in paired:
        actual = row["actual"]["paired_sole_uncontested_add"]
        placebo = row["placebo"]["paired_sole_uncontested_add"]
        seen["both" if actual and placebo else "actual_only" if actual else "placebo_only" if placebo else "neither"] += 1
    missing_print = [r for r in rows if r["anchor_status"] == "missing_transaction_print"]
    def in_gap(row):
        # The block is a coarse time proxy. Count only cases where the entire print/block
        # tolerance falls inside a between-file capture gap. Other cases remain unresolved.
        second = row["block_ts"]
        return any(lo < (second - 120) * 10**9 and (second + 120) * 10**9 < hi for lo, hi in capture_gaps)

    families = Counter(tokens.get(r["token"], "unknown") for r in rows)
    offsets = [(r["created_ms"] - 1000 * r["block_ts"]) / 1000 for r in rows if type(r["created_ms"]) is int]
    return {"conditions": len({r["market"] for r in rows}), "unique_signed_orders": len(rows),
            "orders_by_retrospective_market_family": dict(families),
            "missing_prints_with_block_window_inside_capture_gap": sum(in_gap(r) for r in missing_print),
            "signed_minus_first_block_seconds": quartiles(offsets), "orders_without_numeric_clock": len(rows) - len(offsets),
            "fully_filled_by_observed_trades": sum(r["fully_filled_by_observed_trades"] for r in rows),
            "paired_sole_by_arm": {k: seen[k] for k in ("both", "actual_only", "placebo_only", "neither")},
            "paired_sole_against_all_observed_orders":
                {arm: sum(r[arm]["sole_uncontested_add"] for r in paired) for arm in ("actual", "placebo")},
            "maximum_displayed_residence_bounds_ms":
                quartiles([r["residence_upper_ms"] for r in rows if r["residence_upper_ms"] is not None]),
            "owner_identified_placements": sum(r["placement_identity"] != "unknown" for r in rows),
            "known_cancellations": sum(r["cancellation"] != "unknown" for r in rows)}


def collect(folder, study, source_root, first=None):
    report = folder / "summary.json"
    doc = read(report)
    order_file = folder / "orders.jsonl"
    if checksum(order_file) != doc["orders_sha256"] or checksum(study) != doc["layer1_summary_sha256"]:
        raise ValueError("results or upstream study digest changed")
    upstream = read(study)
    rows = [json.loads(line) for line in order_file.read_bytes().splitlines()]
    if len({(r["wallet"], r["order_id"]) for r in rows}) != len(rows):
        raise ValueError("duplicate signed order")
    if doc["schema"] != "wallet-inspection/2":
        raise ValueError("the study predates the continuous-residence check")
    segments = doc["source_segments"]
    capture_gaps = [(a["last_tw"], b["first_tw"]) for a, b in zip(segments, segments[1:])
                    if a["last_tw"] and b["first_tw"] and b["first_tw"] > a["last_tw"]]
    metadata = {}
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
        meta = upstream["inputs"][win["token_metadata"]]
        if meta["sha256"] not in metadata:
            token_file = source_root / meta["path"]
            if checksum(token_file) != meta["sha256"]:
                raise ValueError("retrospective market identity file changed")
            families = {}
            for line in token_file.read_bytes().splitlines():
                entry = json.loads(line)
                slug = entry.get("slug") or ""
                prefix, _, stamp = slug.rpartition("-")
                if prefix and stamp.isdecimal():
                    families[entry["token"]] = prefix
            metadata[meta["sha256"]] = families
        wallets[w] = wallet_rows(sub, metadata[meta["sha256"]], capture_gaps)
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
    gaps = [(hi - lo, lo, hi) for lo, hi in capture_gaps]
    return {"summary_sha256": checksum(report), "per_order_sha256": doc["orders_sha256"],
            "source_files": len(segments), "source_bytes": sum(p["bytes"] for p in segments),
            "missing_source_files": doc["missing_segments"], "largest_between_file_gap_ns":
                dict(zip(("duration", "last_receipt", "next_receipt"), max(gaps))) if gaps else None,
            "wallets": wallets}


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--study", required=True, type=Path, help="Apex wallet-inspection working directory")
    cli.add_argument("--layer1-summary", required=True, type=Path)
    cli.add_argument("--layer1-root", required=True, type=Path, help="retained Layer 1 source directory")
    cli.add_argument("--first-case", required=True, type=Path)
    args = cli.parse_args()
    out = {name: collect(args.study / name, args.layer1_summary, args.layer1_root,
                         args.first_case if name == "qualified-first15" else None)
           for name in ("qualified-first15", "qualified-day")}
    print(json.dumps(out, sort_keys=True, indent=2, allow_nan=False))
