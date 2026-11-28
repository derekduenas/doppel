"""Export receipt-time taker decisions for offline revealed-preference inversion."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import math
from pathlib import Path

import orjson

from ..tape.segment import atomic_write
from ..tape.universe import HORIZONS
from .decision_book import DEPTH_AGE_NS, GRID_S, scan
from .goldsky_proof import digest
from .intlbook import NS
from .market_opportunities import _inputs, _unchanged, merge_identity, observable_identity, public_listings
from .ordermatch import PRINT_BLOCK_TOL_MS
from .ordertape import committed_parts
from .rulefit import Fair, bind_fill_receipts, up_order, wallet_fills
from .tradeallocation import Allocation, load_allocations

MICRO = 1_000_000
MS_NS = 1_000_000
MAX_PRINT_DELAY_NS = 3 * NS
SCHEMA = "doppel.wallet-decisions/v1"
COVERAGE = ("goldsky_store_verified", "goldsky_pagination_complete", "chain_scan_complete",
            "block_time_window_verified")


def _condition(value: str) -> str:
    return value.lower().removeprefix("0x")


def _ledger_coverage(ledger: dict, wallet: str, start: int, end: int) -> bool:
    bounds = ledger.get("window_time")
    return (
        ledger.get("wallet") == wallet and isinstance(bounds, list) and len(bounds) == 2
        and all(type(b) is int for b in bounds) and 3600 <= bounds[0] <= start < end <= bounds[1]
        and ledger.get("status") == "reconciled" and ledger.get("not_reconciled") is False
        and ledger.get("causes") == [] and isinstance(ledger.get("condition_cash"), list)
        and all(ledger.get("completeness", {}).get(k) is True for k in COVERAGE)
    )


def _endpoints(ledger: dict, token: str, side: int, condition: str) -> tuple[int, int] | None:
    rec = ledger.get("reconciliation", {}).get(token, {})
    info = ledger.get("tokens", {}).get(token, {})
    opening, closing = rec.get("start"), rec.get("end")
    if (any(type(v) not in (float, int) or not 0 <= v < math.inf for v in (opening, closing))
            or rec.get("residual") != 0 or rec.get("log_residual") != 0
            or (info.get("open"), info.get("close")) != (opening, closing)
            or info.get("idx") != side or not isinstance(info.get("condition"), str)
            or _condition(info["condition"]) != condition):
        return None
    return round(opening * MICRO), round(closing * MICRO)


def _market_path(ledger, fills, tokens, observed, allocations, slugs, gaps):
    """Only include net inventory paths with complete exact share legs and unambiguous receipts."""
    fills_by_token = defaultdict(list)
    for fill in fills:
        fills_by_token[fill["token"]].append(fill)
    legs_by_token = defaultdict(list)
    for leg in ledger["condition_cash"]:
        for token, shares in leg["shares"].items():
            if shares:
                legs_by_token[token].append((leg, shares))
    paths = {}
    for slug, pair in sorted(slugs.items()):
        if pair.keys() != {0, 1}:
            gaps[slug] = "missing_binary_token_pair"
            continue
        up, down = pair[0], pair[1]
        opening, events = [], []
        condition = ledger.get("tokens", {}).get(up, {}).get("condition")
        if not isinstance(condition, str):
            gaps[slug] = "unverified_opening_inventory"
            continue
        condition = _condition(condition)
        for idx, token in enumerate((up, down)):
            endpoints = _endpoints(ledger, token, idx, condition)
            if endpoints is None:
                gaps[slug] = "unverified_opening_inventory"
                break
            initial, final = endpoints
            opening.append(initial)
            signed = {f["tx"]: f for f in fills_by_token[token]}
            legs = legs_by_token[token]
            if (len(signed) != len(fills_by_token[token]) or len(legs) != len(signed)
                    or set(signed) != {leg["tx"] for leg, _ in legs}):
                gaps[slug] = "ledger_signed_fill_mismatch_or_nonfill_movement"
                break
            for leg, delta in legs:
                fill = signed[leg["tx"]]
                call = allocations.get(fill["tx"])
                tw = fill.get("receipt_tw")
                prints = observed["fill_prints"].get(fill["tx"], ())
                native = {p.venue_ms for p in prints if p.tw_ns == tw}
                if (not isinstance(call, Allocation) or call.market != condition
                        or fill["role"] not in ("maker", "taker")
                        or (fill["role"] == "taker" and (call.token, call.side) != (token, fill["side"]))
                        or tw is None or len(native) != 1 or not observable_identity(tokens[token], tw)
                        or observed["state_integrity"].get(("print", tw, token)) != "covered"):
                    gaps[slug] = "unverified_wallet_fill_receipt"
                    break
                venue_ns = next(iter(native)) * MS_NS
                if not 0 <= tw - venue_ns <= MAX_PRINT_DELAY_NS:
                    gaps[slug] = "unbounded_fill_receipt_delay"
                    break
                amount = round(fill["shares"] * MICRO) * (1 if fill["side"] == "BUY" else -1)
                if (leg.get("kind") != fill["side"].lower() or type(leg.get("chain_log_index")) is not int
                        or type(delta) is not int or delta != amount or delta == 0
                        or fill.get("price") is None or not 0 < fill["price"] < 1):
                    gaps[slug] = "ledger_share_delta_disagrees"
                    break
                up_side, px_c = up_order(fill["side"], fill["price"], idx)
                events.append({"tx": fill["tx"], "receipt_tw_ns": tw, "venue_fill_ns": venue_ns,
                               "delta_up_micro": delta if idx == 0 else -delta, "role": fill["role"],
                               "up_side": up_side, "px_c": px_c, "size": abs(delta) / MICRO})
            if slug in gaps:
                break
            if initial + sum(delta for _, delta in legs) != final:
                gaps[slug] = "ledger_position_path_disagrees"
                break
        if slug not in gaps:
            paths[slug] = (opening[0] - opening[1], events)
    return paths


def _quote(observed: dict, key: tuple, cutoff: int) -> tuple[float, float, int] | None:
    if observed["state_integrity"].get(key) != "covered":
        return None
    detail = observed["detail"][key]
    age = cutoff - (detail["last_depth_tw_ns"] or 0)
    bid, ask, qty = detail["best_bid"], detail["best_ask"], detail["ask_size"]
    if (not detail["known"] or not 0 < age <= DEPTH_AGE_NS or bid is None or ask is None
            or qty is None or not 0 < bid <= ask < 1 or not 0 < qty < math.inf
            or detail["last_depth_venue_ms"] * MS_NS >= cutoff):
        return None
    return ask, qty, detail["last_depth_tw_ns"]


def _rows(paths, slugs, tokens, observed, fair, start, end, covered_end, gaps, rejected):
    rows = []
    for slug, (opening, events) in paths.items():
        up, down = slugs[slug][0], slugs[slug][1]
        info = tokens[up]
        lo, hi = max(start, info["start"]), min(end, info["end"])
        takes = [e for e in events if e["role"] == "taker" and lo * NS <= e["venue_fill_ns"] <=
                 e["receipt_tw_ns"] < hi * NS]
        if not takes:
            gaps[slug] = "no_in_window_taker_anchor"
            continue
        opportunities = [(e["venue_fill_ns"], e) for e in takes]
        first = min(e["receipt_tw_ns"] for e in takes)
        grid_start = lo + (-lo) % GRID_S
        opportunities.extend(
            (s * NS, None) for s in range(grid_start, hi - GRID_S + 1, GRID_S) if first < s * NS
        )
        qualified_take_receipt = None
        for cut, take in sorted(opportunities, key=lambda item: item[0]):
            if take is None and (qualified_take_receipt is None or qualified_take_receipt >= cut):
                rejected["prior_eligible_take_unobserved"] += 1
                continue
            # The absorption window is bounded by chain timestamps. Preserve a right
            # margin for settlement of executions within the allocator's join tolerance.
            label_end = cut + GRID_S * NS if take is None else cut + 1
            if label_end + PRINT_BLOCK_TOL_MS * MS_NS >= covered_end * NS:
                rejected["insufficient_chain_right_margin"] += 1
                continue
            if take is None and any(e["venue_fill_ns"] < cut + GRID_S * NS
                                    and e["receipt_tw_ns"] >= cut for e in events):
                rejected["wallet_execution_in_grid_cell"] += 1
                continue
            if any(e is not take and e["venue_fill_ns"] <= cut <= e["receipt_tw_ns"] for e in events):
                rejected["position_order_ambiguous"] += 1
                continue
            if not all(observable_identity(tokens[t], cut) for t in (up, down)):
                rejected["unobserved_token_identity"] += 1
                continue
            a = _quote(observed, ("cut", cut, up), cut)
            b = _quote(observed, ("cut", cut, down), cut)
            if a is None or b is None or a[0] + b[0] < 1 - 1e-9:
                rejected["missing_stale_or_crossed_book"] += 1
                continue
            fv = fair.evidence(info, cut) if fair is not None else {}
            fair_c, opening_tw = fv.get("fair_c"), fv.get("opening_receipt_ns")
            if fair_c is None or not 0 <= fair_c <= 100 or type(opening_tw) is not int or opening_tw >= cut:
                rejected["missing_causal_fair"] += 1
                continue
            inv = opening + sum(e["delta_up_micro"] for e in events if e["receipt_tw_ns"] < cut)
            bb, ba = round(100 * (1 - b[0]), 8), round(100 * a[0], 8)
            row = {"action": "none" if take is None else "take", "window": slug,
                   "window_start_ns": info["start"] * NS, "cutoff_tw_ns": cut,
                   "tte_s": (info["end"] * NS - cut) / NS, "inv": inv / MICRO, "fair_c": fair_c,
                   "bb_c": bb, "ba_c": ba, "depth_bid": b[1], "depth_ask": a[1],
                   "mid_c": (bb + ba) / 2, "spread_c": ba - bb,
                   "up_ask_tw_ns": a[2], "down_ask_tw_ns": b[2], "fair_opening_tw_ns": opening_tw}
            if take is not None:
                if qualified_take_receipt is None:
                    qualified_take_receipt = take["receipt_tw_ns"]
                row.update({k: take[k] for k in ("tx", "up_side", "px_c", "size", "receipt_tw_ns", "venue_fill_ns")})
                row["label_source"] = "verified_signed_call_and_compatible_clob_print"
            else:
                row["label_source"] = "no_reconciled_wallet_execution_in_fixed_cell"
            rows.append((cut, slug, take["tx"] if take is not None else "", row))
    return [row for _, _, _, row in sorted(rows, key=lambda item: item[:3])]


def export(data: Path, orders: Path, ledger_path: Path, brti: Path, wallet: str,
           start: int, end: int, horizon: str) -> tuple[list[dict], dict]:
    if horizon not in ("5m", "15m") or not 3600 <= start < end:
        raise ValueError("give increasing Unix-second bounds and a BTC 5m or 15m horizon")
    wallet = wallet.lower()
    order_paths = committed_parts(orders) if orders.is_dir() else [orders]
    published = sorted((brti / "ref_prices").glob("*/*.dtape.zst"))
    files = [ledger_path, *order_paths, *published]
    missing_reference = []
    last_market_end = (end - 1) // HORIZONS[horizon] * HORIZONS[horizon] + HORIZONS[horizon]
    # RefStore can read the capture hour after the last publication hour.
    last_reference_hour = last_market_end // 3600 * 3600 + 3600
    for stream in ("ref_brti_raw", "ref_brti_live"):
        sources, holes = _inputs(brti, stream, start - 3600, last_reference_hour + 1, True)
        files.extend(p for _, p, _, _ in sources)
        missing_reference.extend(holes)
    source_files = {p: (p.stat(), digest(p)) for p in files}
    report = {"schema": SCHEMA, "wallet": wallet, "interval": [start, end], "market_family": f"btc_{horizon}",
              "max_depth_age_ns": DEPTH_AGE_NS, "max_print_delay_ns": MAX_PRINT_DELAY_NS,
              "chain_join_tolerance_ms": PRINT_BLOCK_TOL_MS, "grid_s": GRID_S,
              "inputs": [{"path": str(p), "sha256": sha} for p, (_, sha) in source_files.items()],
              "status": "ABSTAIN", "evidence_gap": None}
    ledger = orjson.loads(ledger_path.read_bytes())
    rows: list[dict] = []
    if _ledger_coverage(ledger, wallet, start, end):
        ledger_start, ledger_end = ledger["window_time"]
        fills, source, verified = wallet_fills(orders, wallet)
        fills = [f for f in fills if ledger_start <= f["block_ts"] < ledger_end]
        report["absorption_interval"] = ledger["window_time"]
        report["signed_wallet_input"] = source
        report["signed_taker_rows_in_absorption_interval"] = sum(f["role"] == "taker" for f in fills)
        if verified is None:
            report["evidence_gap"] = "unverified_order_tape"
        else:
            rows = _assemble(data, orders, ledger, brti, wallet, start, end, horizon, fills, report)
    else:
        report["evidence_gap"] = "missing_complete_reconciled_absorption"
    for p, (before, _) in source_files.items():
        _unchanged(p, before)
    if orders.is_dir() and order_paths != committed_parts(orders):
        raise ValueError("published order tape changed during export")
    if (published != sorted((brti / "ref_prices").glob("*/*.dtape.zst"))
            or any((brti / rel).exists() for rel in missing_reference)):
        raise ValueError("reference input appeared during export")
    report["counts"] = dict(Counter(row["action"] for row in rows))
    if report["counts"].get("take") and report["counts"].get("none"):
        report["status"] = "OBSERVED_DECISIONS"
    elif report["evidence_gap"] is None:
        report["evidence_gap"] = "missing_qualified_takes_or_passes"
    return rows, report


def _assemble(data, orders, ledger, brti, wallet, start, end, horizon, fills, report):
    stream = f"clob_btc_{horizon}"
    length = HORIZONS[horizon]
    ledger_start, ledger_end = ledger["window_time"]
    scan_from = ledger_start - length
    txs = {f["tx"] for f in fills}
    tokens, listing = public_listings(data, scan_from, ledger_end, exclude_wallets=(wallet,), exclude_txs=txs,
                                     require_seals=True)
    books, holes = _inputs(data, stream, scan_from, ledger_end, True)
    report["book_sources"] = [r for _, _, _, r in books]
    report["listing_sources"] = listing
    report["missing_book_hours"] = holes
    initial = scan(data, stream, scan_from, ledger_end, [], {}, txs, {}, length)
    for token, mate in initial["mates"].items():
        if mate not in tokens or token not in initial["mate_known_tw"]:
            continue
        info = tokens[mate]
        merge_identity(tokens, token, info | {"idx": 1 - info["idx"],
                      "known_tw_ns": max(info["known_tw_ns"], initial["mate_known_tw"][token]),
                      "witness_source": "paired_clob_tokens"})
    active = {tokens[f["token"]]["slug"] for f in fills if f["role"] == "taker" and f["token"] in tokens}
    slugs: dict[str, dict[int, str]] = defaultdict(dict)
    for token, info in tokens.items():
        if (info["asset"] == "btc" and info["hz"] == horizon and info["slug"] in active
                and info["start"] < end and start < info["end"]):
            pair = slugs[info["slug"]]
            if info["idx"] in pair:
                raise ValueError(f"duplicate token identity in {info['slug']}")
            pair[info["idx"]] = token
    report["unmapped_signed_taker_rows"] = sum(
        f["role"] == "taker" and f["token"] not in tokens for f in fills
    )
    alloc = load_allocations(orders, txs)
    bind_fill_receipts(fills, initial["fill_prints"], initial["mates"], alloc, initial["mate_known_tw"])
    queries = set()
    for fill in fills:
        tx, token, tw = fill["tx"], fill["token"], fill.get("receipt_tw")
        if tw is None or token not in tokens or tokens[token]["slug"] not in slugs:
            continue
        queries.add((tw, token, ("print", tw, token)))
        for p in initial["fill_prints"].get(tx, ()):
            cut = p.venue_ms * MS_NS
            if p.tw_ns == tw and start * NS <= cut <= tw < end * NS:
                for t in slugs[tokens[token]["slug"]].values():
                    queries.add((cut, t, ("cut", cut, t)))
    for pair in slugs.values():
        info = tokens[next(iter(pair.values()))]
        lo, hi = max(start, info["start"]), min(end, info["end"])
        for s in range(lo + (-lo) % GRID_S, hi - GRID_S + 1, GRID_S):
            for t in pair.values():
                queries.add((s * NS, t, ("cut", s * NS, t)))
    observed = scan(data, stream, scan_from, ledger_end, list(queries), {}, set(), {}, length, details=True)
    observed["fill_prints"] = initial["fill_prints"]
    gaps: dict[str, str] = {}
    rejected: Counter[str] = Counter()
    paths = _market_path(ledger, fills, tokens, observed, alloc, slugs, gaps)
    rows = _rows(paths, slugs, tokens, observed, Fair(brti, start, end) if paths else None,
                 start, end, ledger_end, gaps, rejected)
    report["window_gaps"] = gaps
    report["excluded_cuts"] = dict(sorted(rejected.items()))
    for _, path, before, _ in books:
        _unchanged(path, before)
    if any((data / p).exists() for p in [*holes, *listing["missing"]]):
        raise ValueError("a missing market tape segment appeared during export")
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("data", "orders", "absorption", "brti", "out"):
        p.add_argument(f"--{name}", type=Path, required=True)
    p.add_argument("--wallet", required=True)
    p.add_argument("--from", dest="start", type=int, required=True)
    p.add_argument("--to", dest="end", type=int, required=True)
    p.add_argument("--horizon", choices=("5m", "15m"), required=True)
    a = p.parse_args(argv)
    rows, report = export(a.data, a.orders, a.absorption, a.brti, a.wallet, a.start, a.end, a.horizon)
    a.out.mkdir(parents=True, exist_ok=False)
    data = b"".join(orjson.dumps(r, option=orjson.OPT_SORT_KEYS) + b"\n" for r in rows)
    atomic_write(a.out / "decisions.jsonl", data, replace=False)
    atomic_write(a.out / "evidence.json", orjson.dumps(
        report | {"decisions_sha256": digest(a.out / "decisions.jsonl")}, option=orjson.OPT_SORT_KEYS
    ) + b"\n", replace=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
