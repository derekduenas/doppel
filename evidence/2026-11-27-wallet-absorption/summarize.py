"""Summarize the completed Apex wallet-absorption study from its retained receipts."""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
import os
from pathlib import Path

import pyarrow.parquet as pq

from doppel.lab.fillstore import iter_wallet_fills
from doppel.lab.ordertape import committed_parts


def read(path):
    return json.loads(path.read_bytes())


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def ref(root, path):
    return {"path": os.path.relpath(path, root), "sha256": digest(path)}


def fileset(root, folder, paths):
    """Compact hash of the sorted list of [relative filename, file SHA-256] pairs."""
    rows = [[p.relative_to(folder).as_posix(), digest(p)] for p in sorted(paths)]
    if not rows:
        raise ValueError(f"empty artifact set: {folder}")
    return {"path": folder.relative_to(root).as_posix(), "files": len(rows),
            "manifest_sha256": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()}


def order_comparison(fills, parts, report):
    wallet = report["wallet"]
    lo, hi = report["window_blocks"]
    start, end = report["window_time"]
    fills = [r for r in fills if lo <= r["block_number"] <= hi and start <= r["block_timestamp"] < end]
    if len(fills) != report["goldsky_fills"]:
        raise ValueError(f"store count disagrees with {wallet} receipt")
    provider = defaultdict(Counter)
    provider_roles = Counter()
    for r in fills:
        key = (r["transaction_hash"].lower(), r["block_number"], r["order_id"].lower().removeprefix("0x"),
               r["token_id"], r["side"])
        role = r["order_type"] or "missing"
        provider[key][role] += 1
        provider_roles[role] += 1
    decoded = defaultdict(set)
    timing, roles = Counter(), Counter()
    row_count = extra = 0
    cols = ["tx", "block", "order_id", "token_id", "side", "role", "ts_flag"]
    for part in parts:
        table = pq.read_table(part, columns=cols, filters=[
            ("maker", "=", wallet), ("verified", "=", True), ("block", ">=", lo), ("block", "<=", hi)])
        for r in table.to_pylist():
            row_count += 1
            key = (r["tx"], r["block"], r["order_id"].lower().removeprefix("0x"), r["token_id"], r["side"])
            if key not in provider:
                extra += 1
                continue
            role = r["role"] or "missing"
            decoded[key].add(role)
            roles[role] += 1
            timing[r["ts_flag"] or "plausible"] += 1
    missing = set(provider) - set(decoded)
    conflicts = sum(len(rs) != 1 for rs in decoded.values())
    mismatch = sum(n for key, rs in decoded.items() if len(rs) == 1
                   for role, n in provider[key].items() if role not in rs)
    return {"provider_fill_rows": len(fills), "provider_roles_by_fill": dict(provider_roles),
            "distinct_provider_order_keys": len(provider), "verified_account_order_rows": row_count,
            "matched_signed_rows": sum(roles.values()), "matched_distinct_order_keys": len(decoded),
            "unmatched_verified_rows": extra, "provider_fills_without_signed_order": sum(provider[k].total() for k in missing),
            "all_provider_fill_rows_linked": not missing and not extra and not conflicts,
            "matched_key_count_equals_identity_receipt": len(decoded) == report["identity"]["signer_coverage"]["verified_orders"],
            "conflicting_signed_role_keys": conflicts, "provider_signed_role_disagreements_by_fill": mismatch,
            "signed_roles_by_matched_row": dict(roles), "creation_time_flags_by_matched_row": dict(timing)}


def wallet_summary(root, path):
    r = read(path)
    checks, p = r["fill_check"], r["pairing"]
    recon = r["reconciliation"]
    share = [v for k, v in recon.items() if k != "cash"]
    collateral = {k: v["residual_micro"] for k, v in r["collateral_reconciliation"].items()}
    tx_kinds = Counter(e["kind"] for e in r["condition_cash"])
    return {"receipt": ref(root, path), "wallet": r["wallet"], "economic_status": r["status"],
            "causes": r["causes"], "source_checks": {k: v for k, v in r["completeness"].items() if k != "goldsky_queries"},
            "window_time": r["window_time"], "window_blocks": r["window_blocks"], "goldsky_fills": r["goldsky_fills"],
            "chain_legs": r["chain_legs"], "economic_event_counts": dict(tx_kinds), "nonfill_tx_counts": r["tx_kinds"],
            "fill_checks": {k: v for k, v in checks.items() if not k.startswith("examples")},
            "tokens": len(r["tokens"]), "known_conditions": len({v["condition"] for v in r["tokens"].values() if v["condition"]}),
            "paired_conditions": len(p["markets"]), "tokens_without_pair": len(p["tokens_without_pair"]),
            "pairing": {k: v for k, v in p.items() if k not in ("markets", "tokens_without_pair")},
            "max_abs_share_residual": max((abs(v["residual"]) for v in share if "residual" in v), default=None),
            "cash_residual": recon["cash"].get("residual"), "cash_log_residual": recon["cash"].get("log_residual"),
            "collateral_residual_micro": collateral, "max_abs_collateral_residual_micro": max(map(abs, collateral.values()), default=None),
            "identity": {k: v for k, v in r["identity"].items() if k != "tx_senders"}}


def summarize(root):
    root = root.resolve()
    scope_file = root / "evidence/2026-11-27-wallet-selection/collection-scope.json"
    scope = read(scope_file)
    if len(scope["candidates"]) != 1 or len(scope["controls"]) != 1 or scope["candidates"] == scope["controls"]:
        raise ValueError("expected one distinct candidate and control")
    candidate, control = scope["candidates"][0], scope["controls"][0]
    wallets = [candidate, control]
    source, inputs, windows = {}, {}, {}
    for key, path in (("short_window_tokens", root.parent.parent / "lab/tokens.jsonl"),
                      ("day_and_gap_tokens", root / "day-with-chain-tokens.jsonl")):
        inputs[key] = ref(root, path)
    for name in ("first-source", "day-proof"):
        folder = root / "reviewed" / name
        summary, pages = folder / "summary.json", folder / "pagination.jsonl"
        proof = read(summary)
        if digest(pages) != proof["pagination_sha256"]:
            raise ValueError(f"pagination checksum mismatch: {name}")
        pagination = [json.loads(line) for line in pages.read_bytes().splitlines() if line]
        if ({r["address"] for r in pagination} != set(wallets) or len(pagination) != len(wallets)
                or any([r["start_time"], r["end_time"]] != proof["interval"] or r["complete"] is not True
                       for r in pagination)):
            raise ValueError(f"source proof does not cover the intended wallets and window: {name}")
        source[name] = {"summary": ref(root, summary), "pagination": ref(root, pages), "proof": proof}

    order_parts, store_rows = {}, {}
    plan = {"signed-five": wallets, "signed-fifteen": wallets}
    plan.update({f"signed-day-{w[:6]}": [w] for w in wallets})
    for folder_name, expected in plan.items():
        folder = root / folder_name
        day = folder_name.startswith("signed-day-")
        tag = expected[0][:6]
        store = root / (f"day-store-{tag}" if day else "reviewed/first-source/fills")
        orders = root / (f"day-orders-{tag}" if day else "first-orders")
        if store not in store_rows:
            state = store / "state.json"
            files = [store / v["file"] for v in read(state)["segments"].values() if v["file"]]
            inputs[store.relative_to(root).as_posix()] = fileset(root, store, [state, *files])
            store_rows[store] = dict(iter_wallet_fills(store, expected))
        if orders not in order_parts:
            order_parts[orders] = committed_parts(orders)
            inputs[orders.name] = fileset(root, orders, [*order_parts[orders], *orders.glob("_state/flush-*.json")])
        cohort_file = folder / "cohort.json"
        cohort = read(cohort_file)
        paths = sorted(folder.glob("wallet-*.json"))
        if ({p.stem.removeprefix("wallet-") for p in paths} != set(expected) or
                {row["wallet"] for row in cohort["rows"]} != set(expected) or cohort["summary"]["wallets"] != len(expected)):
            raise ValueError(f"cohort membership differs: {folder_name}")
        reports = []
        for pth in paths:
            report = wallet_summary(root, pth)
            w = report["wallet"]
            if pth.stem != f"wallet-{w}" or any(report[k] != cohort["summary"][k] for k in ("window_time", "window_blocks")):
                raise ValueError(f"wallet identity or interval differs: {pth}")
            report["orders"] = order_comparison(store_rows[store].get(w, []), order_parts[orders], report)
            reports.append(report)
        if cohort["summary"]["reconciled"] != sum(r["economic_status"] == "reconciled" for r in reports):
            raise ValueError(f"cohort and individual reconciliation statuses differ: {folder_name}")
        windows[folder_name] = {"cohort_receipt": ref(root, cohort_file), "source_proof": "day-proof" if day else "first-source",
                                "store": store.relative_to(root).as_posix(), "order_tape": orders.name,
                                "token_metadata": "day_and_gap_tokens" if day else "short_window_tokens",
                                "shared_verified_signers": cohort["shared_verified_signers"], "wallets": reports}

    original = wallet_summary(root, root / f"day-{candidate[:6]}/wallet-{candidate}.json")
    if original["economic_status"] != "not_reconciled":
        raise ValueError("original failed attempt was replaced")
    failed = {k: original[k] for k in ("receipt", "wallet", "economic_status", "causes", "window_time")}
    failed.update({k: original["source_checks"][k] for k in ("chain_error", "receipts_missing")})
    gap = None
    if (root / "signed-gap").is_dir():
        paths = sorted((root / "signed-gap").glob("wallet-*.json"))
        if {p.stem.removeprefix("wallet-") for p in paths} != set(wallets):
            raise ValueError("gap diagnostic incomplete")
        gap = {"cohort_receipt": ref(root, root / "signed-gap/cohort.json"), "wallets": []}
        for path in paths:
            r = wallet_summary(root, path)
            gap["wallets"].append({**{k: r[k] for k in ("wallet", "receipt", "window_time", "window_blocks",
                                     "economic_status", "causes", "goldsky_fills", "chain_legs", "source_checks")},
                                   "chain_orderfilled": r["fill_checks"]["order_filled"]["chain"]})
    short_start, short_end = source["first-source"]["proof"]["interval"]
    day_start, day_end = source["day-proof"]["proof"]["interval"]
    if day_end - day_start != 86400 or short_end - short_start != 900 or short_end > day_start:
        raise ValueError("unexpected sealed source intervals")
    for name, window in windows.items():
        start, end = window["wallets"][0]["window_time"]
        expected = ([day_start, day_end] if name.startswith("signed-day-") else
                    [short_start, short_start + (300 if name == "signed-five" else 900)])
        if [start, end] != expected:
            raise ValueError(f"wallet window differs from source proof: {name}")
    return {"schema": "wallet-absorption-study-summary/1", "collection_scope": {**scope, "receipt": ref(root, scope_file)},
            "source_proofs": source, "inputs": inputs, "windows": windows, "original_failed_candidate_day": failed,
            "gap_diagnostic": gap, "scope": {"account_count": len(wallets), "short_windows_overlap": True,
            "covered_seconds_per_wallet": short_end - short_start + day_end - day_start,
            "uncovered_interval": [short_end, day_start], "uncovered_seconds": day_start - short_end,
            "day_window_count_per_wallet": 1, "strategy_identified": False, "weekly_selection_established": False,
            "gap_interpretation": "The archived queries do not cover the intervening interval. Its chain counts are diagnostic."}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", required=True, type=Path)
    print(json.dumps(summarize(parser.parse_args().study), indent=2, sort_keys=True, allow_nan=False))
