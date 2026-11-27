"""Reconstruct recorder-visible state at the execution anchors of a sealed wallet inspection."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from pathlib import Path
import tempfile

import orjson

from ..tape.segment import read_segment, segment_path
from .goldsky_proof import digest
from .decision_book import scan
from .inspection_book import _Projection

NS = 1_000_000_000


def source_files(tape: Path, doc: dict):
    start, end = doc["window_time"]
    stream = doc["stream"]
    files = {r["file"]: r for r in doc["source_segments"]}
    if len(files) != len(doc["source_segments"]):
        raise ValueError("duplicate sealed segment")
    missing = set(doc["missing_segments"])
    checked = {}
    for hour in range(start - start % 3600, end, 3600):
        path = segment_path(tape, stream, hour * NS)
        key = path.relative_to(tape).as_posix()
        if key in files:
            seal = files[key]
            if not path.is_file() or path.stat().st_size != seal["bytes"] or digest(path) != seal["sha256"]:
                raise ValueError(f"book differs from the inspected source: {key}")
            checked[path] = path.stat()
        elif path.relative_to(tape / stream).as_posix() not in missing or path.exists():
            raise ValueError(f"book coverage differs from inspection: {key}")
    if set(files) != {p.relative_to(tape).as_posix() for p in checked}:
        raise ValueError("unexpected book sources in inspection")
    return checked


def inputs(summary: Path, orders_file: Path):
    raw = summary.read_bytes()
    doc = orjson.loads(raw)
    if doc.get("schema") != "wallet-inspection/2" or digest(orders_file) != doc["orders_sha256"]:
        raise ValueError("expected a bound Layer 2 inspection")
    rows = [orjson.loads(line) for line in orders_file.read_bytes().splitlines()]
    keys = {(r["wallet"], r["order_id"]) for r in rows}
    counts = Counter(r["wallet"] for r in rows)
    if (len(keys) != len(rows) or set(counts) - set(doc["wallets"])
            or any(counts[w] != d["maker_orders"] for w, d in doc["wallets"].items())):
        raise ValueError("signed order population differs from inspection")
    return hashlib.sha256(raw).hexdigest(), doc, rows


def case_queries(case, rows, sources, tape, start):
    if case is None:
        return []
    case_order = [r for r in rows if (r["wallet"], r["order_id"]) == (case["wallet"], case["order_hash"])]
    if len(case_order) != 1:
        raise ValueError("primary order absent or duplicated")
    order = case_order[0]
    a = order.get("anchor") or {}
    p = case["tx_print"]
    seal = case["source"]
    if (not any(s["file"] == seal["file"] and s["sha256"] == seal["sha256"] for s in sources)
            or order["first_observed_tx"] != case["transaction"] or order["token"] != p["token"]
            or order["price"] != case["signed_price"] or order["side"] != case["signed_maker_side"]
            or order["size"] != case["signed_size"] or order["market"] != p["market"].removeprefix("0x")
            or any(a.get(k) != p[k] for k in ("tw_ns", "venue_ms", "seq"))):
        raise ValueError("case anchor disagrees with Layer 2")
    actual = case["arms"]["actual"]
    marks = actual["fragment_candidates"]
    positions = [(m["seq"], m["message_index"], m["change_index"]) for m in marks]
    if (not actual["continuous"] or not marks or len(set(positions)) != len(marks)
            or len(marks) != actual["fragment_candidate_count"] or len(marks) != order["actual"]["size_compatible_adds"]
            or any(m["tw_ns"] >= a["tw_ns"] or m["venue_ms"] >= a["venue_ms"] for m in marks)):
        raise ValueError("case additions disagree with Layer 2")
    # Bind the supplied alternative timestamps and sizes to their native packet offsets.
    wanted = dict(zip(positions, marks))
    checked = set()
    wanted_seq = {k[0] for k in wanted}
    key = (order["token"], order["price"], order["side"])
    projection = _Projection({key: {order["size"]}}, set(), {})
    prev = None
    for rec in read_segment(tape / seal["file"], strict=True):
        if rec["tw"] < start * NS:
            continue
        if rec["tw"] > p["tw_ns"]:
            break
        if prev is not None and rec["seq"] != prev + 1:
            projection.reset("sequence_discontinuity")
        prev = rec["seq"]
        projection.record(rec)
        if rec["seq"] in wanted_seq:
            raw = orjson.loads(rec["raw"])
            ordinal = 0
            for i, msg in enumerate(raw if isinstance(raw, list) else [raw]):
                changes = msg.get("price_changes") or []
                for j in range(len(changes)):
                    pos = (rec["seq"], i, j)
                    if pos in wanted:
                        m = wanted[pos]
                        if any(e.seq == pos[0] and e.ordinal == ordinal+j and e.src == "price_change"
                               and (e.tw_ns, e.venue_ms, e.old_size, e.new_size) ==
                                   (m["tw_ns"], m["venue_ms"], m["old_size"], m["new_size"])
                               for e in projection.out.events[key]):
                            checked.add(pos)
                ordinal += max(1, len(changes))
    if checked != set(wanted):
        raise ValueError("case addition differs from the sealed book")
    return [(f"addition_{i}", order, m) for i, m in enumerate(marks, 1)] + [("execution", order, a)]


def run(tape: Path, summary: Path, orders_file: Path, out: Path, case_file: Path | None = None):
    if out.exists():
        raise FileExistsError(out)
    summary_hash, doc, rows = inputs(summary, orders_file)
    sources = source_files(tape, doc)
    case_raw = case_file.read_bytes() if case_file else None
    start, end = doc["window_time"]
    cases = case_queries(orjson.loads(case_raw) if case_raw else None, rows, doc["source_segments"], tape, start)
    queries, levels = [], {}
    def add(key, row, mark):
        tw = mark["tw_ns"]
        if not start * NS <= tw < end * NS:
            raise ValueError("out-of-window anchor")
        for leg, token in (("signed", row["token"]), ("complement", row.get("mate"))):
            if not token:
                continue
            query_key = (key, leg)
            queries.append((tw, token, query_key))
            levels[query_key] = ((row["price"], row["side"]) if leg == "signed" else
                                 (round(1 - row["price"], 4), "SELL" if row["side"] == "BUY" else "BUY"))
    for r in rows:
        if r["anchor_status"] == "located":
            add((r["wallet"], r["order_id"]), r, r["anchor"])
    for name, row, mark in cases:
        add(name, row, mark)
    result = scan(tape, doc["stream"], start, end, sorted(queries, key=lambda q: q[0]), {}, set(), {}, 300,
                  grid_from=end, query_levels=levels, details=True)
    for path, prev in sources.items():
        now = path.stat()
        if any(getattr(prev, f) != getattr(now, f) for f in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")):
            raise ValueError(f"book changed during dissection: {path}")

    def context(key, mark):
        obj = {"receive_cutoff_ns": mark["tw_ns"], "anchor_venue_ms": mark["venue_ms"], "anchor_seq": mark["seq"]}
        for leg in ("signed", "complement"):
            q = (key, leg)
            if q not in levels:
                obj[leg] = {"status": "no_bound_token"}
                continue
            state = result["detail"][q]
            last = state.get("last_depth_tw_ns")
            if last is not None and last >= mark["tw_ns"]:
                raise ValueError("cutoff or later depth entered a receipt-time row")
            vx = state.get("last_depth_venue_ms")
            obj[leg] = {"state": state, "retrospective_integrity": result["state_integrity"][q],
                        "last_depth_after_anchor_venue_time": vx > mark["venue_ms"] if vx is not None else None}
        return obj

    detailed = []
    for row in rows:
        entry = {k: row[k] for k in ("wallet", "order_id", "market", "token", "price", "side", "anchor_status")}
        entry["context"] = (context((row["wallet"], row["order_id"]), row["anchor"])
                            if row["anchor_status"] == "located" else None)
        detailed.append(entry)
    wallet_counts = {}
    for wallet, d in doc["wallets"].items():
        mine = [r for r in detailed if r["wallet"] == wallet]
        ctx = [r["context"] for r in mine if r["context"]]
        signed = [c["signed"]["state"] for c in ctx]
        wallet_counts[wallet] = {
            "orders": len(mine), "located": len(ctx), "missing_anchor": len(mine) - len(ctx),
            "signed_book_status": dict(Counter(s["status"] for s in signed)),
            "observable_two_sided": sum(s["known"] and s["best_bid"] is not None and s["best_ask"] is not None for s in signed),
            "last_depth_after_anchor_venue_time": sum(c["signed"]["last_depth_after_anchor_venue_time"] is True for c in ctx),
            "audit_integrity": dict(Counter(c["signed"]["retrospective_integrity"] for c in ctx))}
        if len(ctx) != d["anchor_status"].get("located", 0):
            raise ValueError("execution context denominator changed")
    output = {"schema": "wallet-dissection/1", "use": "retrospective_execution_context",
              "clock": "recorder_receipt_strictly_before_cutoff", "window_time": [start, end],
              "inspection_summary_sha256": summary_hash, "inspection_orders_sha256": doc["orders_sha256"],
              "source_segments": doc["source_segments"], "missing_segments": doc["missing_segments"],
              "wallets": wallet_counts,
              "case_sha256": hashlib.sha256(case_raw).hexdigest() if case_raw else None,
              "primary": {k: context(k, m) for k, _, m in cases},
              "posting_identity": "unresolved", "posting_training_labels": 0}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        dest = Path(tmp) / "result"
        dest.mkdir()
        result_rows = dest / "orders.jsonl"
        result_rows.write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in detailed))
        output["orders_sha256"] = digest(result_rows)
        (dest / "summary.json").write_bytes(orjson.dumps(output, option=orjson.OPT_INDENT_2) + b"\n")
        dest.rename(out)
    return output


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--data", type=Path, required=True)
    cli.add_argument("--inspection-summary", type=Path, required=True)
    cli.add_argument("--orders", type=Path, required=True)
    cli.add_argument("--case", type=Path)
    cli.add_argument("--out", type=Path, required=True)
    a = cli.parse_args(argv)
    d = run(a.data, a.inspection_summary, a.orders, a.out, a.case)
    print(orjson.dumps(d["wallets"]).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
