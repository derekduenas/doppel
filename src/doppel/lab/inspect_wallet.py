"""Retrospective inspection of reconciled wallets on a sealed public order book."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
from pathlib import Path
import tempfile

import orjson
import pyarrow.parquet as pq

from .absorb import goldsky_complete
from .fillstore import iter_wallet_fills
from .goldsky_proof import digest
from .intlbook import px
from .ordertape import committed_parts, ts_flag
from .tradeallocation import Allocation, load_allocations

LOOKBACK_MS = 10_000
PLACEBO_MS = -60_000
SIZE_TOL = 0.02
PRINT_BLOCK_TOL_MS = 120_000


@dataclass
class Subject:
    wallet: str
    span: tuple[int, int]
    orders: list[dict]
    allocations: dict[str, Allocation | str]
    pairs: dict[str, str]
    receipt: dict


def sealed_files(folder: Path, paths: list[Path]) -> tuple[dict[str, str], str]:
    refs = {p.relative_to(folder).as_posix(): digest(p) for p in sorted(paths)}
    seal = hashlib.sha256(orjson.dumps(list(refs.items()))).hexdigest()
    return refs, seal


def source_seals(store: Path, tape: Path):
    state = store / "state.json"
    parts = [store / s["file"] for s in orjson.loads(state.read_bytes())["segments"].values() if s["file"]]
    _, store_sha = sealed_files(store, [state, *parts])
    orders = committed_parts(tape)
    refs, tape_sha = sealed_files(tape, [*orders, *tape.glob("_state/flush-*.json")])
    return store_sha, tape_sha, refs, orders


def subject(ledger: Path, store: Path, tape: Path, study: dict) -> Subject:
    raw = ledger.read_bytes()
    r = orjson.loads(raw)
    wallet, (start, end) = r["wallet"], r["window_time"]
    c = r["completeness"]
    if (r["status"] != "reconciled" or r["not_reconciled"] is not False or r["causes"]
            or not all(c.get(k) is True for k in ("goldsky_complete", "goldsky_store_verified",
                       "block_time_window_verified", "chain_scan_complete"))
            or c["receipts_missing"] or c["chain_error"]):
        raise ValueError(f"Layer 1 receipt did not reconcile for {wallet}")
    ledger_sha = hashlib.sha256(raw).hexdigest()
    matches = [win for win in study["windows"].values() for report in win["wallets"]
               if report["wallet"] == wallet and report["receipt"]["sha256"] == ledger_sha]
    if len(matches) != 1:
        raise ValueError(f"ledger differs from the published Layer 1 receipt for {wallet}")
    win, = matches
    store_sha, tape_sha, refs, parts = source_seals(store, tape)
    inputs = study["inputs"]
    if (store_sha != inputs[win["store"]]["manifest_sha256"] or
            tape_sha != inputs[win["order_tape"]]["manifest_sha256"]):
        raise ValueError(f"signed tape or fill store differs from the Layer 1 fileset for {wallet}")
    pages = {(wallet, g["start_time"], g["end_time"]):
             {**g, "address": wallet, "complete": g["stop"] == "done", "cursor": None}
             for g in c["goldsky_queries"]}
    all_rows = dict(iter_wallet_fills(store, [wallet])).get(wallet, [])
    if not goldsky_complete(pages, wallet, start, end, fills=all_rows):
        raise ValueError(f"fill store differs from the Layer 1 source binding for {wallet}")
    fills = [f for f in all_rows if start <= f["block_timestamp"] < end]
    lo, hi = r["window_blocks"]
    if (len(fills) != r["goldsky_fills"] or len(fills) != r["fill_check"]["order_filled"]["matched"]
            or any(not lo <= f["block_number"] <= hi for f in fills)):
        raise ValueError(f"receipt and fill scope disagree for {wallet}")

    index = defaultdict(list)
    fields = ["block", "block_ts", "tx", "order_id", "token_id", "side", "role", "condition_id",
              "created_ms", "maker_amount", "taker_amount", "fill_amount"]
    for part in parts:
        table = pq.read_table(part, columns=fields, filters=[("maker", "=", wallet), ("verified", "=", True),
                              ("block", ">=", lo), ("block", "<=", hi)])
        for row in table.to_pylist():
            key = row["tx"], row["order_id"].lower().removeprefix("0x")
            index[key].append(row)
    expected = defaultdict(list)
    for f in fills:
        expected[(f["transaction_hash"].lower(), f["order_id"].lower().removeprefix("0x"))].append(f)
    if index.keys() != expected.keys():
        raise ValueError(f"signed order keys differ from the reconciled fills for {wallet}")

    # An order can have several executions. Select it by fill, never by its creation time.
    by_order: dict[str, dict] = {}
    pairs = {}
    roles = Counter()
    for market in r["pairing"]["markets"].values():
        a, b = market["tokens"]
        pairs[a], pairs[b] = b, a
    for (tx, oid), rows in expected.items():
        decoded = index[tx, oid]
        if len(decoded) != 1:
            raise ValueError(f"ambiguous signed order in {tx}")
        o = decoded[0]
        token, side, role = o["token_id"], o["side"], o["role"]
        cash, shares = ((o["maker_amount"], o["taker_amount"]) if side == "BUY"
                        else (o["taker_amount"], o["maker_amount"]))
        if (not 0 < cash <= shares or side not in ("BUY", "SELL") or role not in ("maker", "taker")
                or any((token, side, role, o["block"], o["block_ts"]) !=
                       (f["token_id"], f["side"], f["order_type"], f["block_number"], f["block_timestamp"])
                       for f in rows)):
            raise ValueError(f"signed order economics differ from Layer 1 in {tx}")
        amount_key = "amount_usdc" if side == "BUY" else "amount_shares"
        if sum(round(f[amount_key] * 1e6) for f in rows) != o["fill_amount"] and role == "maker":
            raise ValueError(f"signed maker fill quantity differs from Layer 1 in {tx}")
        condition = o["condition_id"].lower().removeprefix("0x")
        known = r["tokens"][token].get("condition")
        if known and known.lower().removeprefix("0x") != condition:
            raise ValueError(f"signed condition differs from Layer 1 in {tx}")
        terms = {"wallet": wallet, "order_id": oid, "token": token, "market": condition, "side": side,
                 "price": px(cash / shares), "size": shares / 1e6, "created_ms": o["created_ms"]}
        item = by_order.setdefault(oid, {**terms, "fills": []})
        if any(item[k] != v for k, v in terms.items()):
            raise ValueError(f"conflicting terms for order {oid}")
        roles[role] += len(rows)
        item["fills"].append({"tx": tx, "block": o["block"], "block_ts": o["block_ts"], "role": role,
                              "shares_micro": sum(round(f["amount_shares"] * 1e6) for f in rows),
                              "log_index": min(int(f["id"].rsplit("_", 1)[1]) for f in rows)})
    selected = []
    for item in by_order.values():
        item["fills"].sort(key=lambda f: (f["block"], f["log_index"]))
        if any(f["role"] == "maker" for f in item["fills"]):
            selected.append(item)
    allocations = load_allocations(tape)
    if source_seals(store, tape)[:2] != (store_sha, tape_sha):
        raise ValueError(f"Layer 1 inputs changed during inspection for {wallet}")
    receipt = {"wallet": wallet, "layer1_sha256": ledger_sha, "window_time": [start, end],
               "bound_source_fills": len(fills), "fill_roles": dict(roles), "store_manifest_sha256": store_sha,
               "signed_order_manifest_sha256": tape_sha, "signed_order_files": refs,
               "unique_orders_filled_in_window": len(by_order), "orders_with_maker_fills": len(selected)}
    return Subject(wallet, (start, end), selected, allocations, pairs, receipt)


def anchor(order, book, owner):
    """Locate the first observed maker execution using a complete decoded transaction."""
    fill = order["fills"][0]
    if any(f["role"] != "maker" for f in order["fills"]):
        return None, "mixed_execution_roles"
    records = book.prints.get(fill["tx"], [])
    if not records:
        return None, "missing_transaction_print"
    alloc = owner.allocations.get(fill["tx"])
    if not isinstance(alloc, Allocation):
        return None, "unallocated_transaction"
    hits = []
    for p in records:
        if abs(p.venue_ms - fill["block_ts"] * 1000) > PRINT_BLOCK_TOL_MS:
            return None, "print_block_time_disagrees"
        if alloc.match(p, owner.pairs.get(p.asset)) != "allocated":
            return None, "incompatible_transaction_print"
        hits.append(p)
    if len({p.venue_ms for p in hits}) != 1:
        return None, "ambiguous_transaction_time"
    # The earliest receipt of a direct or mirrored execution is the information boundary.
    return min(hits, key=lambda p: (p.tw_ns, p.ordinal)), "located"


def search(order, book, anchor_ms, anchor_tw):
    key = order["token"], order["price"], order["side"]
    lo = anchor_ms - LOOKBACK_MS
    first_tw = anchor_tw - LOOKBACK_MS * 1_000_000
    token_spans = book.spans.get(order["token"], [])
    covered = [s for s in token_spans if s.start_ms <= lo and anchor_ms <= s.end_ms
               and s.start_tw <= first_tw and anchor_tw <= s.end_tw]
    if len(covered) != 1:
        return None
    # For the synthetic time, use the same receipt lag as the real print. Both clocks
    # must precede their endpoint. No delayed evidence enters just the placebo arm.
    span = covered[0]
    candidates = [e for e in book.events.get(key, ()) if e.span_id == span.span_id
                  and e.src == "price_change" and lo <= e.venue_ms < anchor_ms and first_tw <= e.tw_ns < anchor_tw
                  and abs(e.new_size - e.old_size - order["size"]) <= SIZE_TOL + 1e-9]
    return span, candidates


def event_key(order, e):
    # In a binary book a bid on A and an ask on B are mirror views of one level.
    return (order["market"], e.venue_ms, e.old_size, e.new_size,
            order["token"] if order["side"] == "BUY" else order.get("mate") or order["token"],
            order["price"] if order["side"] == "BUY" else px(1 - order["price"]))


def mark(e):
    return {"venue_ms": e.venue_ms, "tw_ns": e.tw_ns, "seq": e.seq, "ordinal": e.ordinal,
            "span_id": e.span_id, "old_size": e.old_size, "new_size": e.new_size, "source": e.src}


def inspect(owners, book):
    rows, work = [], []
    claims = [Counter(), Counter()]
    paired_claims = [Counter(), Counter()]
    for owner in owners:
        for order in owner.orders:
            fills = order["fills"]
            first = fills[0]
            anchor_print, status = anchor(order, book, owner)
            total = sum(f["shares_micro"] for f in fills)
            size_micro = round(order["size"] * 1e6)
            if total > size_micro + len(fills):
                raise ValueError(f"fills exceed signed size for {order['order_id']}")
            row = {k: v for k, v in order.items() if k != "fills"}
            row.update(first_observed_tx=first["tx"], first_block=first["block"], block_ts=first["block_ts"],
                       clock_flag=ts_flag(order["created_ms"], first["block_ts"]) or "plausible",
                       observed_filled_micro=total, signed_size_micro=size_micro, fill_transactions=len(fills),
                       fully_filled_by_observed_trades=total == size_micro,
                       anchor_status=status, placement_identity="unknown", cancellation="unknown",
                       residence_upper_ms=None, continuous_coverage=False, paired_coverage=False)
            rows.append(row)
            if anchor_print is None:
                continue
            a = anchor_print
            row["anchor"] = {"venue_ms": a.venue_ms, "tw_ns": a.tw_ns, "asset": a.asset, "price": a.price,
                             "size": a.size, "side": a.side, "seq": a.seq, "ordinal": a.ordinal}
            row["mate"] = owner.pairs.get(row["token"])
            arms = []
            for shift, counter in zip((0, PLACEBO_MS), claims):
                result = search(row, book, a.venue_ms + shift, a.tw_ns + shift * 1_000_000)
                if (shift == 0 and result is not None and a.asset == row["token"]
                        and result[0].span_id != a.span_id):
                    result = None
                arms.append(result)
                if result is not None:
                    counter.update({event_key(row, e) for e in result[1]})
            real, placebo = arms
            row["continuous_coverage"] = real is not None
            row["paired_coverage"] = real is not None and placebo is not None
            row["unbroken_between_arms"] = (real is not None and placebo is not None
                                           and real[0].span_id == placebo[0].span_id)
            if row["paired_coverage"]:
                for result, counts in zip(arms, paired_claims):
                    counts.update({event_key(row, e) for e in result[1]})
            work.append((row, arms))
            if real is None:
                continue
            key = row["token"], row["price"], row["side"]
            zs = [e for e in book.empty.get(key, ()) if e.span_id == real[0].span_id and e.venue_ms < a.venue_ms
                  and e.tw_ns < a.tw_ns and (e.new_size == 0 or (e.old_size == 0 and e.src == "price_change"))]
            if zs:
                last = max(zs, key=lambda e: (e.venue_ms, e.tw_ns, e.ordinal))
                row["last_empty_evidence"] = mark(last)
                row["residence_upper_ms"] = a.venue_ms - last.venue_ms

    for row, arms in work:
        for name, result, counter, paired_counts, shift in zip(("actual", "placebo"), arms, claims,
                                                               paired_claims, (0, PLACEBO_MS)):
            if result is None:
                continue
            _, matches = result
            unique = len(matches) == 1 and counter[event_key(row, matches[0])] == 1
            info = {"size_compatible_adds": len(matches), "sole_uncontested_add": unique}
            if row["paired_coverage"]:
                info["paired_sole_uncontested_add"] = len(matches) == 1 and paired_counts[event_key(row, matches[0])] == 1
            if unique:
                e = matches[0]
                info["candidate"] = mark(e)
                info["conditional_add_to_anchor_ms"] = row["anchor"]["venue_ms"] + shift - e.venue_ms
            row[name] = info
    return sorted(rows, key=lambda r: (r["wallet"], r["first_block"], r["order_id"]))


def summary(rows):
    paired = [r for r in rows if r["paired_coverage"]]
    return {"maker_orders": len(rows), "clock_flags": dict(Counter(r["clock_flag"] for r in rows)),
            "anchor_status": dict(Counter(r["anchor_status"] for r in rows)),
            "with_actual_coverage": sum(r["continuous_coverage"] for r in rows),
            "paired_orders": len(paired), "paired_conditions": len({r["market"] for r in paired}),
            "paired_unbroken_between_arms": sum(r["unbroken_between_arms"] for r in paired),
            "paired_compatible": {arm: sum(r[arm]["size_compatible_adds"] > 0 for r in paired)
                                  for arm in ("actual", "placebo")},
            "paired_sole_uncontested": {arm: sum(r[arm]["paired_sole_uncontested_add"] for r in paired)
                                         for arm in ("actual", "placebo")},
            "paired_add_multiplicity": {arm: dict(Counter(str(r[arm]["size_compatible_adds"]) for r in paired))
                                        for arm in ("actual", "placebo")},
            "residence_upper_bounds": sum(r["residence_upper_ms"] is not None for r in rows),
            "wallet_identified_placements": 0}


def run(specs, data, stream, out, layer1_summary):
    from .inspection_book import read_book

    if out.exists():
        raise FileExistsError(out)
    binding = layer1_summary.read_bytes()
    study = orjson.loads(binding)
    if study.get("schema") != "wallet-absorption-study-summary/1":
        raise ValueError("unrecognized Layer 1 study summary")
    owners = [subject(*paths, study) for paths in specs]
    start, end = owners[0].span
    if len({o.wallet for o in owners}) != len(owners) or any(o.span != (start, end) for o in owners):
        raise ValueError("use distinct wallets with identical Layer 1 UTC bounds")
    signatures, txs, pairs = defaultdict(set), set(), {}
    for o in owners:
        for token, mate in o.pairs.items():
            if pairs.setdefault(token, mate) != mate:
                raise ValueError("conflicting outcome pairs")
        for row in o.orders:
            signatures[row["token"], row["price"], row["side"]].add(row["size"])
            txs.add(row["fills"][0]["tx"])
    book = read_book(data, stream, start, end, signatures, txs, pairs)
    rows = inspect(owners, book)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as tmp:
        dest = Path(tmp) / "results"
        dest.mkdir()
        evidence = dest / "orders.jsonl"
        evidence.write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in rows))
        doc = {"schema": "wallet-inspection/1", "use": "retrospective_diagnostic",
               "layer1_summary_sha256": hashlib.sha256(binding).hexdigest(),
               "window_time": [start, end], "stream": stream, "source_segments": book.sources,
               "missing_segments": book.missing, "book_counts": dict(book.counts),
               "lookback_ms": LOOKBACK_MS, "placebo_shift_ms": PLACEBO_MS, "size_tolerance_shares": SIZE_TOL,
               "orders_sha256": digest(evidence), "inputs": [o.receipt for o in owners],
               "wallets": {o.wallet: summary([r for r in rows if r["wallet"] == o.wallet]) for o in owners}}
        (dest / "summary.json").write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2) + b"\n")
        dest.rename(out)
    return doc


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--subject", action="append", nargs=3, metavar=("LEDGER", "STORE", "ORDERS"), type=Path, required=True)
    p.add_argument("--layer1-summary", type=Path, required=True, help="published Layer 1 receipt and fileset digests")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--stream", default="clob_btc_5m")
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args(argv)
    doc = run(a.subject, a.data, a.stream, a.out, a.layer1_summary)
    print(orjson.dumps(doc["wallets"]).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
