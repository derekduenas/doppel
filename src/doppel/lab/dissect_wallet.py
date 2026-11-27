"""Reconstruct recorder-visible state at the execution anchors of a sealed wallet inspection."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
from pathlib import Path
import re
import tempfile

import orjson

from ..tape.segment import segment_path
from .goldsky_proof import digest
from .decision_book import scan
from .inspect_wallet import LOOKBACK_MS, search
from .inspection_book import _Projection
from .intlbook import _records

NS = 1_000_000_000
STAT_FIELDS = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")


def source_files(tape: Path, doc: dict):
    start, end = doc["window_time"]
    stream = doc["stream"]
    if end <= start or not re.fullmatch(r"clob_[a-z0-9]+_(?:5|15)m", stream):
        raise ValueError("invalid inspected book scope")
    manifest = tape / stream / "manifest.jsonl"
    sealed = {r["file"]: r for line in manifest.read_bytes().splitlines() if line and (r := orjson.loads(line))} if manifest.exists() else {}
    files = {r["file"]: r for r in doc["source_segments"]}
    if len(files) != len(doc["source_segments"]):
        raise ValueError("duplicate sealed segment")
    missing = set(doc["missing_segments"])
    checked = {}
    actual_missing = set()
    for hour in range(start - start % 3600, end, 3600):
        path = segment_path(tape, stream, hour * NS)
        key = path.relative_to(tape).as_posix()
        if key in files:
            seal = files[key]
            original = sealed.get(path.relative_to(tape / stream).as_posix()) or {}
            if (original.get("sha256"), original.get("bytes")) != (seal["sha256"], seal["bytes"]):
                raise ValueError(f"book has no matching recorder seal: {key}")
            before = path.stat() if path.is_file() else None
            if before is None or before.st_size != seal["bytes"] or digest(path) != seal["sha256"]:
                raise ValueError(f"book differs from the inspected source: {key}")
            after = path.stat()
            if any(getattr(before, f) != getattr(after, f) for f in STAT_FIELDS):
                raise ValueError(f"book changed while checking its recorder seal: {key}")
            checked[path] = before
        elif path.exists():
            raise ValueError(f"book coverage differs from inspection: {key}")
        else:
            actual_missing.add(path.relative_to(tape / stream).as_posix())
    if missing != actual_missing:
        raise ValueError("missing book coverage differs from inspection")
    if set(files) != {p.relative_to(tape).as_posix() for p in checked}:
        raise ValueError("unexpected book sources in inspection")
    return checked


def inputs(summary: Path, orders_file: Path):
    raw = summary.read_bytes()
    doc = orjson.loads(raw)
    order_bytes = orders_file.read_bytes()
    if doc.get("schema") != "wallet-inspection/2" or hashlib.sha256(order_bytes).hexdigest() != doc["orders_sha256"]:
        raise ValueError("expected a bound Layer 2 inspection")
    rows = [orjson.loads(line) for line in order_bytes.splitlines()]
    keys = {(r["wallet"], r["order_id"]) for r in rows}
    counts = Counter(r["wallet"] for r in rows)
    if (len(keys) != len(rows) or set(counts) - set(doc["wallets"])
            or any(counts[w] != d["maker_orders"] for w, d in doc["wallets"].items())):
        raise ValueError("signed order population differs from inspection")
    return hashlib.sha256(raw).hexdigest(), doc, rows


def case_queries(case, rows, sources, tape, start, end):
    if case is None:
        return []
    if case.get("schema") != "wallet-inspection-first-order-crosscheck/1":
        raise ValueError("invalid primary case schema")
    case_order = [r for r in rows if (r["wallet"], r["order_id"]) == (case["wallet"], case["order_hash"])]
    if len(case_order) != 1:
        raise ValueError("primary order absent or duplicated")
    order = case_order[0]
    a = order.get("anchor") or {}
    p, seal = case["tx_print"], case["source"]
    src = next((s for s in sources if s["file"] == seal["file"]), {})
    if (order["anchor_status"] != "located" or not order.get("continuous_coverage")
            or (src.get("sha256"), src.get("bytes")) != (seal["sha256"], seal["bytes"])
            or order["first_observed_tx"] != case["transaction"]
            or p["token"] not in (order["token"], order.get("mate"))
            or order["price"] != case["signed_price"] or order["side"] != case["signed_maker_side"]
            or order["size"] != case["signed_size"] or order["market"] != p["market"].removeprefix("0x")
            or (a.get("asset"), a.get("price"), a.get("size"), a.get("side")) !=
               (p["token"], p["price"], p["aggregate_shares"], p["taker_side"])
            or any(a.get(k) != p[k] for k in ("tw_ns", "venue_ms", "seq"))):
        raise ValueError("case anchor disagrees with Layer 2")
    first, last = seal["selected_utc"]
    stream = Path(seal["file"]).parts[0]
    if (not start <= first < last <= end or not first * NS <= p["tw_ns"] < last * NS
            or any(segment_path(tape, stream, tw).relative_to(tape).as_posix() != seal["file"]
                   for tw in (first * NS, last * NS - 1))):
        raise ValueError("case source does not cover its declared selection")
    actual = case["arms"]["actual"]
    bounds = (p["venue_ms"] - LOOKBACK_MS, p["tw_ns"] - LOOKBACK_MS * 1_000_000, p["venue_ms"], p["tw_ns"])
    if (not actual["continuous"]
            or tuple(actual[k] for k in ("search_start_ms", "search_start_tw", "endpoint_ms", "endpoint_tw")) != bounds):
        raise ValueError("case search bounds disagree with Layer 2")
    marks = actual["fragment_candidates"]
    if not marks or len(marks) != actual["fragment_candidate_count"] or len(marks) != order["actual"]["size_compatible_adds"]:
        raise ValueError("case additions disagree with Layer 2")

    # Replay the original signed-token projection and the full search interval. The
    # capture after the print is needed only to verify retrospective span coverage.
    key = (order["token"], order["price"], order["side"])
    tx = case["transaction"].lower()
    projection = _Projection({key: {order["size"]}}, {tx}, {})
    wanted = {(m["tw_ns"], m["seq"]) for m in [p, *marks]}
    positions = {}
    for rec in _records(tape, stream, first, last, strict=True):
        if not first * NS <= rec["tw"] < last * NS:
            continue
        projection.record(rec)
        stamp = rec["tw"], rec.get("seq")
        if stamp not in wanted or (rec.get("meta") or {}).get("ctl") is not None:
            continue
        raw = orjson.loads(rec["raw"])
        ordinal = 0
        for i, msg in enumerate(raw if isinstance(raw, list) else [raw]):
            width = max(1, len(msg.get("price_changes") or []))
            for j in range(width):
                pos = (*stamp, i, j)
                if pos in positions:
                    raise ValueError("ambiguous native case packet position")
                positions[pos] = (ordinal + j, msg.get("market"))
            ordinal += width

    def native_ordinal(mark):
        native = positions.get((mark["tw_ns"], mark["seq"], mark["message_index"], mark["change_index"]))
        if native is None or native[1] != p["market"]:
            raise ValueError("case addition differs from its native position or market")
        return native[0]

    prints = projection.out.prints.get(tx, [])
    if not prints:
        raise ValueError("case execution missing from sealed book")
    hit = min(prints, key=lambda q: (q.tw_ns, q.ordinal))
    if (any(q.venue_ms != p["venue_ms"] for q in prints)
            or (hit.tw_ns, hit.venue_ms, hit.seq, hit.ordinal, hit.asset, hit.market, hit.side, hit.price, hit.size, hit.span_id) !=
               (p["tw_ns"], p["venue_ms"], p["seq"], native_ordinal(p), p["token"], p["market"],
                p["taker_side"], p["price"], p["aggregate_shares"], p["span"])
            or a.get("ordinal") != hit.ordinal):
        raise ValueError("case execution differs from the sealed book")
    found = search(order, projection.out, hit.venue_ms, hit.tw_ns)
    if found is None or (hit.asset == order["token"] and found[0].span_id != hit.span_id):
        raise ValueError("case search lacks one continuous native span")
    native_marks = [(e.tw_ns, e.venue_ms, e.seq, e.ordinal, e.span_id, e.old_size, e.new_size) for e in found[1]]
    supplied = [(m["tw_ns"], m["venue_ms"], m["seq"], native_ordinal(m), m["span"], m["old_size"], m["new_size"])
                for m in marks]
    if supplied != native_marks:
        raise ValueError("case addition differs from the complete native search")
    return [(f"addition_{i}", order, m) for i, m in enumerate(marks, 1)] + [("execution", order, a)]


def run(tape: Path, summary: Path, orders_file: Path, out: Path, case_file: Path | None = None):
    if out.exists():
        raise FileExistsError(out)
    summary_hash, doc, rows = inputs(summary, orders_file)
    sources = source_files(tape, doc)
    case_raw = case_file.read_bytes() if case_file else None
    start, end = doc["window_time"]
    cases = case_queries(orjson.loads(case_raw) if case_raw else None, rows, doc["source_segments"], tape, start, end)
    queries, levels, query_tokens = [], {}, {}
    def add(key, row, mark):
        tw = mark["tw_ns"]
        if not start * NS <= tw < end * NS:
            raise ValueError("out-of-window anchor")
        for leg, token in (("signed", row["token"]), ("complement", row.get("mate"))):
            if not token:
                continue
            query_key = (key, leg)
            queries.append((tw, token, query_key))
            query_tokens[query_key] = token
            levels[query_key] = ((row["price"], row["side"]) if leg == "signed" else
                                 (round(1 - row["price"], 4), "SELL" if row["side"] == "BUY" else "BUY"))
    for r in rows:
        if r["anchor_status"] == "located":
            add((r["wallet"], r["order_id"]), r, r["anchor"])
    for name, row, mark in cases:
        add(name, row, mark)
    result = scan(tape, doc["stream"], start, end, sorted(queries, key=lambda q: q[0]), {}, set(), {}, 300,
                  grid_from=end, query_levels=levels, details=True)
    if any((tape / doc["stream"] / rel).exists() for rel in doc["missing_segments"]):
        raise ValueError("a missing book segment appeared during dissection")
    for path, prev in sources.items():
        now = path.stat()
        if any(getattr(prev, f) != getattr(now, f) for f in STAT_FIELDS):
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
            obj[leg] = {"token": query_tokens[q], "state": state, "retrospective_integrity": result["state_integrity"][q],
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
