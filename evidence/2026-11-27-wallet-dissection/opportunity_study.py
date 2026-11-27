"""Replay the registered primary interval and the first qualifying adjacent day markets."""
import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import orjson

from doppel.lab.fillstore import iter_wallet_fills
from doppel.lab.goldsky_proof import digest
from doppel.lab.inspect_wallet import subject
from doppel.lab.ledger import TokenMap
from doppel.lab.market_opportunities import study_panel
from doppel.tape.universe import intl_slug

CANDIDATE = "0xf9b7876ba2c35370e0bedb7f8ee772da722aa37c"
CONTROL = "0x28cc505fb60501dbb2199a9662ba175919af904f"
FAMILY = ("btc", "5m")
PRIOR = Path(__file__).resolve().parents[1]
POLICY = Path(__file__).resolve().parent
DAY_FIRST = int(datetime(2026, 11, 25, 21, 35, tzinfo=timezone.utc).timestamp())


def economic_rows(folder, study, groups):
    rows, receipts, markets, pairing = {}, {}, {}, {}
    spans = set()
    for group in groups:
        window = study["windows"][group]
        token_ref = study["inputs"][window["token_metadata"]]
        token_path = folder / token_ref["path"]
        if digest(token_path) != token_ref["sha256"]:
            raise ValueError("economic token map differs from the Layer 1 source")
        tmap = TokenMap(token_path).m
        store = folder / window["store"]
        for record in window["wallets"]:
            wallet = record["wallet"]
            if wallet in rows:
                raise ValueError("duplicate wallet in source scope")
            ledger = folder / record["receipt"]["path"]
            owner = subject(ledger, store, folder / window["order_tape"], study)
            lo, hi = owner.span
            spans.add((lo, hi))
            ledger_data = orjson.loads(ledger.read_bytes())
            pairs = ledger_data["pairing"]["markets"]
            by_token = {t: condition for condition, pair in pairs.items() for t in pair["tokens"]}
            fills = dict(iter_wallet_fills(store, [wallet]))[wallet]
            source = [f for f in fills if lo <= f["block_timestamp"] < hi]
            assigned = []
            mapping = {}
            for f in source:
                token = f["token_id"]
                info = tmap[token]
                condition = info["condition"].lower().removeprefix("0x")
                if info["idx"] not in (0, 1) or condition != by_token[token] or not info["slug"]:
                    raise ValueError(f"unreconciled economic market identity for {token}")
                slug = info["slug"]
                if slug in mapping and mapping[slug] != condition:
                    raise ValueError(f"economic slug {slug} has multiple conditions")
                mapping[slug] = condition
                assigned.append((f["block_timestamp"], slug, f["transaction_hash"].lower()))
            if len(assigned) != record["goldsky_fills"] or owner.receipt["bound_source_fills"] != len(assigned):
                raise ValueError("re-read economic sample differs from reconciled fill count")
            rows[wallet], markets[wallet] = assigned, mapping
            receipts[wallet] = {"ledger_sha256": digest(ledger), "token_metadata_sha256": token_ref["sha256"],
                                "store_manifest_sha256": owner.receipt["store_manifest_sha256"],
                                "order_manifest_sha256": owner.receipt["signed_order_manifest_sha256"],
                                "ledger_window_time": [lo, hi], "all_reconciled_fills": len(assigned),
                                "economic_pairing_in_ledger_window": record["pairing"]}
            pairing[wallet] = pairs
        if digest(token_path) != token_ref["sha256"]:
            raise ValueError("economic metadata changed during replay")
    if len(spans) != 1 or set(rows) != {CANDIDATE, CONTROL}:
        raise ValueError("unpaired wallet scope")
    return spans.pop(), rows, receipts, markets, pairing


def replay(tape, econ, start, end):
    (lo, hi), rows, receipts, markets, pairing = econ
    if not lo <= start < end <= hi:
        raise ValueError("selected interval outside the reconciled economic source")
    activity = {w: Counter(slug for ts, slug, _ in fs if start <= ts < end) for w, fs in rows.items()}
    excluded = {tx for fs in rows.values() for _, _, tx in fs}
    day_counts = {w: Counter(slug for _, slug, _ in fs) for w, fs in rows.items()}
    panel = study_panel(tape, start, end, [FAMILY], activity, excluded)
    for window in panel["windows"]:
        slug = window["slug"]
        for w, label in window["wallets"].items():
            label["fills_in_market_over_source_ledger"] = day_counts[w][slug]
            market = markets[w].get(slug)
            label["market_pairing_over_source_ledger"] = pairing[w].get(market)
    panel.update(economic_sources=receipts, excluded_wallet_transactions=len(excluded))
    return panel


def run(tape, folder):
    study_file = PRIOR / "2026-11-27-wallet-absorption" / "run-summary.json"
    study = orjson.loads(study_file.read_bytes())
    first = economic_rows(folder, study, ["signed-fifteen"])
    primary = replay(tape, first, *first[0])
    day = economic_rows(folder, study, ["signed-day-0xf9b7", "signed-day-0x28cc"])
    counts = Counter(slug for _, slug, _ in day[1][CANDIDATE])
    bound = day[0][1]
    secondary, selection = None, None
    for i, start in enumerate(range(DAY_FIRST, bound - 600 + 1, 300)):
        slugs = [intl_slug(*FAMILY, start + step) for step in (0, 300)]
        if sum(counts[s] > 0 for s in slugs) == 1:
            selection = {"start": start, "end": start + 600, "earlier_pairs_checked": i,
                         "ledger_scope_candidate_fill_counts": {s: counts[s] for s in slugs}}
            secondary = replay(tape, day, start, start + 600)
            break
    return {"schema": "wallet-dissection-opportunities/1", "candidate": CANDIDATE, "control": CONTROL,
            "layer1_summary_sha256": digest(study_file), "protocol_sha256": digest(POLICY / "PROTOCOL.md"),
            "secondary_protocol_sha256": digest(POLICY / "OPPORTUNITY-CHECK.md"),
            "primary": primary, "secondary_selection": selection, "secondary": secondary,
            "economic_labels": "Counts include every matched economic fill row of the wallet, by the actual token's "
                               "market. Interval counts use half-open block time. Market selection uses the full source "
                               "ledger. Pairing measures conditional-token holdings over that ledger after reconciliation."}


def compact(full):
    """Keep every transition in visibility, full source seals and full-report SHA on branch."""
    data = orjson.loads(full.read_bytes())
    if data["schema"] != "wallet-dissection-opportunities/1":
        raise ValueError("unexpected opportunity artifact")
    data["full_report_sha256"] = digest(full)
    for case in ("primary", "secondary"):
        panel = data[case]
        if panel is None:
            continue
        cells = panel.pop("cells")
        for window in panel["windows"]:
            group = [c for c in cells if c["slug"] == window["slug"]]
            runs = []
            for c in group:
                status = [c["identity_observed"], c["book_state"], c["capture_integrity"], c["eligible"]]
                if runs and runs[-1]["state"] == status and runs[-1]["to"] == c["cell_start"]:
                    runs[-1]["to"] += panel["cell_s"]
                else:
                    runs.append({"from": c["cell_start"], "to": c["cell_start"] + panel["cell_s"], "state": status})
            window["coverage_runs"] = runs
        panel["coverage_state_fields"] = ["identity_observed", "book_state", "capture_integrity", "eligible"]
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape", type=Path)
    parser.add_argument("--layer1", type=Path)
    parser.add_argument("--compact", type=Path, help="compact an existing full opportunity artifact")
    args = parser.parse_args()
    if args.compact and not (args.tape or args.layer1):
        data = compact(args.compact)
    elif args.tape and args.layer1 and not args.compact:
        data = run(args.tape, args.layer1)
    else:
        parser.error("supply --tape and --layer1 together or supply --compact")
    print(orjson.dumps(data, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode())
