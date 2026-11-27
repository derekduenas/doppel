"""Audit the registered wallets against their reconciled day and independently witnessed market state."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import orjson

from doppel.lab.fillstore import iter_wallet_fills
from doppel.lab.goldsky_proof import digest
from doppel.lab.inspect_wallet import subject, source_seals
from doppel.lab.market_opportunities import _inputs, _unchanged, public_listings
from doppel.lab.decision_book import scan, DEPTH_AGE_NS, GRID_S
from doppel.lab.intlbook import px
from doppel.lab.ledger import TokenMap
from doppel.tape.universe import intl_slug

EVIDENCE = Path(__file__).resolve().parents[1]
CANDIDATE = "0xf9b7876ba2c35370e0bedb7f8ee772da722aa37c"
CONTROL = "0x28cc505fb60501dbb2199a9662ba175919af904f"
NS = 10**9
MARKET_S = 300
STREAM = "clob_btc_5m"
START = int(datetime(2026, 11, 25, 23, tzinfo=timezone.utc).timestamp())
END = START + 3600
WALLETS = {"0xf9b7": CANDIDATE, "0x28cc": CONTROL}


def source_exclusions(folder, layer1):
    """Only disclose the wallets' transaction identifiers from the verified day stores to listing discovery."""
    excludes, receipts = set(), {}
    for tag, wallet in WALLETS.items():
        win = layer1["windows"]["signed-day-" + tag]
        store = folder / win["store"]
        manifest = layer1["inputs"][win["store"]]["manifest_sha256"]
        store_hash = source_seals(store, folder / win["order_tape"])[0]
        if store_hash != manifest:
            raise ValueError("wallet exclusion source differs from Layer 1")
        for f in dict(iter_wallet_fills(store, [wallet]))[wallet]:
            excludes.add(f["transaction_hash"].lower())
        if source_seals(store, folder / win["order_tape"])[0] != manifest:
            raise ValueError("wallet exclusion source changed during read")
        receipts[wallet] = store_hash
    return excludes, receipts


def public_rows(tape, layer1_root, first, second):
    warmup = START - MARKET_S
    excluded, stores = source_exclusions(layer1_root, first)
    tokens, public_proofs = public_listings(tape, warmup, END, exclude_wallets=WALLETS.values(),
                                           exclude_txs=excluded, require_seals=True)
    down = {}
    for token, info in tokens.items():
        if info["idx"] == 1 and (info["asset"], info["hz"]) == ("btc", "5m") and START <= info["start"] < END:
            if info["start"] in down:
                raise ValueError("conflicting witnessed Down tokens")
            if info["slug"] != intl_slug("btc", "5m", info["start"]):
                raise ValueError("inconsistent peer listing identity")
            down[info["start"]] = token, info
    times = range(START, END, GRID_S)
    queries = []
    for t in times:
        token, info = down.get(t - t % MARKET_S, ("", {}))
        known = info.get("known_tw_ns")
        queries.append((t * NS, token if known is not None and known < t * NS else "", t))
    inputs, missing = _inputs(tape, STREAM, warmup, END, sealed=True)
    sealed = {r["file"]: r for r in second["source_segments"]}
    for _, _, _, r in inputs:
        if r["file"] not in sealed or sealed[r["file"]]["sha256"] != r["sha256"]:
            raise ValueError("CLOB slice differs from Layer 2 day source")
    if any(r in sealed for r in missing):
        raise ValueError("archived CLOB segment recorded by Layer 2 has disappeared")
    result = scan(tape, STREAM, warmup, END, queries, {}, set(), {}, MARKET_S, grid_from=END, details=True)
    for _, path, before, receipt in inputs:
        _unchanged(path, before)
        public_proofs["sources"].append(receipt)
    public_proofs["missing"].extend(missing)
    if any((tape / name).exists() for name in public_proofs["missing"]):
        raise ValueError("claimed missing segment appeared during replay")
    observations, diagnostics = [], Counter()
    for tw, token, t in queries:
        data = result["detail"][t]
        capture = "covered" if result["state_integrity"][t] == "covered" else "gap"
        book = None
        if not token:
            reason = "no_prior_independent_down_identity"
        elif data["status"] != "known":
            reason = data["status"]
        elif data["best_bid"] is None or data["best_ask"] is None:
            reason = "one_sided"
        elif data["best_bid"] > data["best_ask"]:
            reason = "crossed"
        else:
            reason = "priced"
            info = down[t - t % MARKET_S][1]
            book = {"bid": data["best_bid"], "ask": data["best_ask"], "bid_shares": data["bid_size"],
                    "depth_tw_ns": data["last_depth_tw_ns"], "identity_tw_ns": info["known_tw_ns"]}
            if not 0 < tw - book["depth_tw_ns"] <= DEPTH_AGE_NS or book["identity_tw_ns"] >= tw:
                raise ValueError("invalid prior observation")
        observations.append({"cutoff_s": t, "book": book, "capture": capture})
        diagnostics[reason] += 1
    return ({"schema": "rival-public/1", "scope": "real", "start": START, "end": END, "cells": observations},
            {"sources": public_proofs, "holdout_down_witnesses": {str(w): pair for w, pair in down.items()}, "exclusion_store_sha256": stores,
             "excluded_transactions": len(excluded), "cell_diagnostics": dict(diagnostics)})


def signed_sources(folder, layer1, layer2, inspection):
    """Re-read exactly the store rows, signed-order terms and execution anchors published in Layers 1 and 2."""
    day_path = EVIDENCE / "2026-11-27-wallet-inspection/day-summary.json"
    data = (inspection / "orders.jsonl").read_bytes()
    if (digest(inspection / "summary.json") != digest(day_path)
            or hashlib.sha256(data).hexdigest() != layer2["orders_sha256"]):
        raise ValueError("Layer 2 day sources differ from the published study")
    inspected = {}
    for line in data.splitlines():
        row = orjson.loads(line)
        key = row["wallet"], row["order_id"]
        if key in inspected:
            raise ValueError("duplicate Layer 2 signed-order row")
        inspected[key] = row
    economic = {}
    conditions = {}
    source_receipts = {}
    exclusions = set()
    for tag, wallet in WALLETS.items():
        win = layer1["windows"]["signed-day-" + tag]
        record, = win["wallets"]
        if record["wallet"] != wallet:
            raise ValueError("unexpected wallet in paired signed day")
        ledger = folder / record["receipt"]["path"]
        store = folder / win["store"]
        signed = folder / win["order_tape"]
        owner = subject(ledger, store, signed, layer1)
        token_meta = layer1["inputs"][win["token_metadata"]]
        metadata = folder / token_meta["path"]
        if digest(metadata) != token_meta["sha256"]:
            raise ValueError("Layer 1 token map differs from the published study")
        tmap = TokenMap(metadata).m
        from_ledger = orjson.loads(ledger.read_bytes())["tokens"]
        signed_orders = {o["order_id"]: o for o in owner.orders}
        if (owner.span != tuple(layer2["window_time"]) or
                set(signed_orders) != {oid for w, oid in inspected if w == wallet}):
            raise ValueError("Layer 1 and 2 populations disagree")
        all_fills = dict(iter_wallet_fills(store, [wallet]))[wallet]
        fills = []
        for f in all_fills:
            t = f["block_timestamp"]
            if not owner.span[0] <= t < owner.span[1]:
                continue
            idx = tmap[f["token_id"]]["idx"]
            info = tmap[f["token_id"]]
            economic_token = from_ledger[f["token_id"]]
            condition = info["condition"].lower().removeprefix("0x")
            if (idx not in (0, 1) or economic_token["idx"] != idx or
                    condition != economic_token["condition"].lower().removeprefix("0x") or
                    conditions.setdefault(info["slug"], condition) != condition):
                raise ValueError("fill-token market differs from the economic ledger")
            oid = f["order_id"].lower().removeprefix("0x")
            tx = f["transaction_hash"].lower()
            if f["order_type"] == "maker":
                order = signed_orders[oid]
                ir = inspected[(wallet, oid)]
                if any(order[k] != ir[k] for k in ("order_id", "wallet", "token", "side", "price", "market", "size")):
                    raise ValueError("inspected maker terms differ from signed terms")
                if not any(s["tx"] == tx and s["block_ts"] == t and s["role"] == "maker" for s in order["fills"]):
                    raise ValueError("maker transaction absent from reconciled signed order")
            fills.append({"tx": tx, "token": f["token_id"], "slug": info["slug"], "idx": idx, "side": f["side"],
                          "block_ts": t, "role": f["order_type"], "id": oid, "mate": owner.pairs[f["token_id"]]})
            exclusions.add(tx)
        if len(fills) != owner.receipt["bound_source_fills"] or len(fills) != record["goldsky_fills"]:
            raise ValueError("re-read fill denominator mismatch")
        economic[wallet] = fills
        source_receipts[wallet] = {k: owner.receipt[k] for k in
                                   ("layer1_sha256","store_manifest_sha256","signed_order_manifest_sha256",
                                    "unique_orders_filled_in_window","orders_with_maker_fills","bound_source_fills")}
        source_receipts[wallet]["token_metadata_sha256"] = digest(metadata)
        if source_receipts[wallet]["token_metadata_sha256"] != token_meta["sha256"]:
            raise ValueError("economic metadata changed during replay")
    if any(w not in economic for w, _ in inspected):
        raise ValueError("unexpected wallet in Layer 2")
    return economic, source_receipts, exclusions, inspected


def wallet_targets(public, wallet, fills, inspected):
    """Score first observed executions as an economic target. A missing execution censors potential negatives."""
    from doppel.lab.rivals import public_reason

    markets = [intl_slug("btc", "5m", w) for w in range(public["start"], public["end"], MARKET_S)]
    by_market = {slug: {"fills_whole_reconciled_day": 0, "fills_in_market_by_block_clock": 0,
                        "maker_fills_whole_reconciled_day": 0, "maker_orders": set(), "down_buy_maker_orders": set(),
                        "located_qualifying_first_executions": 0, "unlocated_qualifying_first_executions": 0,
                        "positive_cells": 0, "observable_negative_cells": 0, "missing_execution_target_cells": 0,
                        "unobservable_book_cells": 0, "negative_censored_by_unlocated_order": 0,
                        "first_positive_example": None}
                 for slug in markets}
    matched_orders = {}
    for f in fills:
        if f["slug"] not in by_market:
            continue
        group = by_market[f["slug"]]
        group["fills_whole_reconciled_day"] += 1
        opening = int(f["slug"].rsplit("-", 1)[1])
        if opening <= f["block_ts"] < opening + MARKET_S:
            group["fills_in_market_by_block_clock"] += 1
        if f["role"] != "maker":
            continue
        group["maker_fills_whole_reconciled_day"] += 1
        group["maker_orders"].add(f["id"])
        if (f["idx"], f["side"]) == (1, "BUY"):
            group["down_buy_maker_orders"].add(f["id"])
            matched_orders[f["id"]] = f["slug"]
    located, missing = defaultdict(list), set()
    for oid, slug in matched_orders.items():
        row = inspected[(wallet, oid)]
        key = slug, px(row["price"])
        if row["placement_identity"] != "unknown":
            raise ValueError("unexpected independent placement provenance in Layer 2")
        if row["anchor_status"] == "located":
            tw = row["anchor"]["tw_ns"]
            if type(tw) is not int or tw <= 0:
                raise ValueError("invalid Layer 2 execution receipt")
            located[key].append((tw, oid))
            by_market[slug]["located_qualifying_first_executions"] += 1
        else:
            missing.add(key)
            by_market[slug]["unlocated_qualifying_first_executions"] += 1
    labels = []
    for c in public["cells"]:
        t = c["cutoff_s"]
        slug = intl_slug("btc", "5m", t - t % MARKET_S)
        value = None
        if c["capture"] == "covered" and public_reason(c["book"], t, DEPTH_AGE_NS // NS) is None:
            key = slug, px(c["book"]["bid"])
            active = sorted((tw, oid) for tw, oid in located[key] if t * NS <= tw < (t + GRID_S) * NS)
            if active:
                value = True
                if by_market[slug]["first_positive_example"] is None:
                    tw, oid = active[0]
                    ir = inspected[wallet, oid]
                    by_market[slug]["first_positive_example"] = {
                        "cutoff_s": t, "order_id": oid, "signed_limit": ir["price"],
                        "signed_shares": ir["size"], "first_tx": ir["first_observed_tx"],
                        "first_receipt_tw_ns": tw, "native_seq": ir["anchor"]["seq"]}
            elif key not in missing:
                value = False
            else:
                by_market[slug]["negative_censored_by_unlocated_order"] += 1
        else:
            by_market[slug]["unobservable_book_cells"] += 1
        labels.append({"cutoff_s": t, "action": None, "execution": value})
        count_key = ("missing_execution_target_cells" if value is None else "positive_cells" if value else
                     "observable_negative_cells")
        by_market[slug][count_key] += 1
    for group in by_market.values():
        for k in ("maker_orders", "down_buy_maker_orders"):
            group[k] = len(group[k])
    return labels, by_market


def write_json(path, data):
    with path.open("xb") as f:
        f.write(orjson.dumps(data, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2))
        f.write(b"\n")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("phase", choices=["public", "labels"])
    cli.add_argument("--archive", required=True, type=Path)
    cli.add_argument("--out", required=True, type=Path, help="Study directory; public phase creates it")
    cli.add_argument("--predictions", type=Path, help="Frozen predictor output required before reading order labels")
    args = cli.parse_args()
    if (args.predictions is None) != (args.phase == "public"):
        cli.error("only labels takes --predictions")
    first = EVIDENCE / "2026-11-27-wallet-absorption/run-summary.json"
    second = EVIDENCE / "2026-11-27-wallet-inspection/day-summary.json"
    layer1, layer2 = (orjson.loads(p.read_bytes()) for p in (first, second))
    if layer2["layer1_summary_sha256"] != digest(first) or not layer2["window_time"][0] <= START < END <= layer2["window_time"][1]:
        raise ValueError("historical holdout outside the source binding")
    layer1_root = args.archive / "research/wallet-absorption.crsLww"
    out = args.out
    public_path = out / "public.json"
    if args.phase == "public":
        out.mkdir()
        doc, receipts = public_rows(args.archive / "tape", layer1_root, layer1, layer2)
        write_json(public_path, doc)
        receipts |= {"layer1_summary_sha256": digest(first), "layer2_summary_sha256": digest(second),
                     "public_sha256": digest(public_path)}
        write_json(out / "public-source.json", receipts)
        return
    public = orjson.loads(public_path.read_bytes())
    predictions = orjson.loads(args.predictions.read_bytes())
    sha = digest(public_path)
    receipt = orjson.loads((out/"public-source.json").read_bytes())
    if (predictions["public_sha256"] != sha or receipt["public_sha256"] != sha or
            receipt["layer1_summary_sha256"] != digest(first) or receipt["layer2_summary_sha256"] != digest(second)):
        raise ValueError("published prediction and public-intake identities disagree")
    economic, sources, _, inspected = signed_sources(layer1_root, layer1, layer2,
        args.archive / "research/wallet-inspection.jOPOHm/qualified-day")
    subjects, diagnostics = {}, {}
    witnessed_tokens = {info["slug"]: token for token, info in receipt["holdout_down_witnesses"].values()}
    for wallet, fills in economic.items():
        for f in fills:
            witnessed = witnessed_tokens.get(f["slug"])
            if witnessed is not None and (f["token"] if f["idx"] == 1 else f["mate"]) != witnessed:
                raise ValueError("independent Down identity differs from reconciled economic market")
        subjects[wallet], diagnostics[wallet] = wallet_targets(public, wallet, fills, inspected)
    write_json(out/"labels.json", {"schema": "rival-labels/1", "scope": "real", "start": START, "end": END,
                                  "public_sha256": sha, "predictions_sha256": digest(args.predictions), "subjects": subjects})
    write_json(out/"label-source.json", {"schema": "rival-economic-label-audit/1", "wallets": diagnostics,
               "layer1_receipts": sources, "layer1_summary_sha256": digest(first),
               "layer2_summary_sha256": digest(second), "layer2_order_rows_sha256": layer2["orders_sha256"],
               "public_sha256": sha, "predictions_sha256": digest(args.predictions), "labels_sha256": digest(out/"labels.json"),
               "action_labels": {"complete_owner_decision_source": None, "witnessed_post": 0, "witnessed_no_action": 0,
                                 "reviewed_sources": ["fill store", "signed orders with fills", "anonymous price-level changes",
                                                      "transaction-allocated CLOB prints", "peer RTDS listings"]},
               "execution_target": "First received Layer 2 compatible execution of a signed Down buy maker order at the "
                                   "cutoff bid in [cutoff, cutoff+10s). An unresolved matching order in the same market "
                                   "censors otherwise negative cells. Only books with eligible capture are scored."})


if __name__ == "__main__":
    main()
