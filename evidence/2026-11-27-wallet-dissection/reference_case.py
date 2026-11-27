"""Recompute the first case's reference availability and account-side evidence on Apex."""
import argparse
from pathlib import Path
from statistics import median
import math

import orjson

from doppel.lab.goldsky_proof import digest
from doppel.lab.rulefit import Fair
from doppel.strategy.genome import FairModel
from doppel.twin.features import RAW_PAD_S
from doppel.twin.tape import hour_path

NS = 10**9
PRIOR = Path(__file__).resolve().parents[1]


def ledger_evidence(case, first, ledger, ledger_digest):
    wallet = case["wallet"]
    condition = first["condition_id"].removeprefix("0x")
    block = first["polygon"]["block_number"]
    if (wallet != first["wallet"] or ledger["wallet"] != wallet or ledger["status"] != "reconciled"
            or case["transaction"] != first["transaction_hash"]
            or case["order_hash"] != first["goldsky"]["order_id"]
            or case["tx_print"]["token"] != first["token_id"] or case["tx_print"]["market"] != first["condition_id"]):
        raise ValueError("receipt identity differs from the registered case")
    pair = {v["idx"]: (k, v) for k, v in ledger["tokens"].items() if v["condition"] == condition}
    if set(pair) != {0, 1} or pair[1][0] != first["token_id"]:
        raise ValueError("the signed Down token or its complement is missing")
    up, down = pair[0][1], pair[1][1]
    before = {i: [r for r in t["path"] if r[0] < block] for i, (_, t) in pair.items()}
    same = {i: [r for r in t["path"] if r[0] == block] for i, (_, t) in pair.items()}
    shares = first["polygon"]["net_wallet_shares_micro"] / 1e6
    if (same[0] or len(same[1]) != 1 or same[1][0][1:3] != ["buy", shares]
            or first["polygon"]["net_wallet_complement_shares_micro"] != 0
            or shares != case["signed_size"] or ledger["window_blocks"][0] >= block):
        raise ValueError("cannot isolate the signed movement at its block")
    qty = {i: (before[i][-1][3] if before[i] else t["open"]) for i, (_, t) in pair.items()}
    first_identity = min(first["rtds"]["received_ns"].values())
    return {"receipt_sha256": ledger_digest, "settlement_block": block, "opening_balance_block": ledger["window_blocks"][0] - 1,
            "first_scanned_block": ledger["window_blocks"][0],
            "condition": first["condition_id"], "up_opening_shares": up["open"], "down_opening_shares": down["open"],
            "economic_movements_in_condition_prior_to_block": sum(map(len, before.values())),
            "own_pre_block_up_shares": qty[0], "own_pre_block_down_shares": qty[1],
            "own_pre_block_net_up": qty[0] - qty[1], "own_post_block_net_up": qty[0] - qty[1] - shares,
            "first_retained_public_identity_receipt_ns": first_identity,
            "rtds_receipts_ns": first["rtds"]["received_ns"],
            "goldsky_receipt_ns": first["goldsky"]["received_ns"],
            "scope": "Retrospective conditional-token holdings at settlement. Client fills, pending settlements, orders, and exposures outside this condition are unobserved."}


def sources(root, info, vol):
    lo = info["start"] - vol - RAW_PAD_S
    hours = list(range(lo // 3600 * 3600, info["end"] + 1, 3600))
    paths = {"ref_prices": sorted((root / "ref_prices").glob("*/*.dtape.zst")),
             "ref_brti_raw": [hour_path(root, "ref_brti_raw", h) for h in hours],
             "ref_brti_live": [hour_path(root, "ref_brti_live", h) for h in hours + [hours[-1]+3600]]}
    out = {}
    for stream, wanted in paths.items():
        manifest = root / stream / "manifest.jsonl"
        seals = {r["file"]: r for line in manifest.read_bytes().splitlines() if (r := orjson.loads(line))}
        if stream == "ref_prices" and set(seals) != {p.relative_to(root/stream).as_posix() for p in wanted}:
            raise ValueError("incomplete settlement-series archive")
        checked = {}
        for path in wanted:
            key = path.relative_to(root / stream).as_posix()
            if key not in seals and not path.exists():
                checked[key] = None
                continue
            seal = seals.get(key)
            if not seal or path.stat().st_size != seal["bytes"] or digest(path) != seal["sha256"]:
                raise ValueError(f"reference segment differs from its seal: {path}")
            checked[key] = {"sha256": seal["sha256"], "bytes": seal["bytes"]}
        out[stream] = {"manifest_sha256": digest(manifest), "selected_files": checked}
    return out


def report(root, ledger_path):
    inspection = PRIOR / "2026-11-27-wallet-inspection/first-trade.json"
    absorption = PRIOR / "2026-11-27-wallet-absorption"
    case = orjson.loads(inspection.read_bytes())
    first = orjson.loads((absorption / "first-trade.json").read_bytes())
    win = orjson.loads((absorption / "run-summary.json").read_bytes())["windows"]["signed-fifteen"]
    ledger_digest = digest(ledger_path)
    expected = next(w for w in win["wallets"] if w["wallet"] == case["wallet"])["receipt"]["sha256"]
    if ledger_digest != expected:
        raise ValueError("ledger differs from the registered signed-fifteen receipt")
    inv = ledger_evidence(case, first, orjson.loads(ledger_path.read_bytes()), ledger_digest)
    start = case["source"]["selected_utc"][0]
    info = {"asset": "btc", "start": start, "end": start + 300}
    files = sources(root, info, FairModel().vol_window_s)
    fair = Fair(root, start, info["end"])
    marks = {f"addition_{i}": r["tw_ns"] for i, r in enumerate(case["arms"]["actual"]["fragment_candidates"], 1)}
    marks["execution"] = case["tx_print"]["tw_ns"]
    ref = fair.store.window_ref("btc", start, info["end"], fair.genome.fair.vol_window_s)
    opening_history = ref.ptb_history or ()
    asof = {name: {"cutoff_tw_ns": tw, "reference": fair.evidence(info, tw),
                   "wallet_identified_in_public_execution_records": inv["first_retained_public_identity_receipt_ns"] < tw,
                   "observed_wallet_client_inventory": None}
            for name, tw in marks.items()}
    for state in asof.values():
        r = state["reference"]
        received = {}
        for second in range(r["volatility_first_second"], r["volatility_last_second"] + 1):
            offset = second - ref.raw_t0
            stamp = ref.rcv[offset]
            if math.isfinite(ref.raw[offset]) and stamp <= r["decision_second"]:
                received[second] = float(stamp)
        if len(received) != r["raw_seconds_known"]:
            raise ValueError("raw-source receipt counts differ from the reference model")
        delays = [v - k for k, v in received.items()]
        r["raw_receipt_basis"] = ("upstream_perelman_only" if not any(files["ref_brti_live"]["selected_files"].values())
                                  else "earliest_documented_upstream_or_local_receipt")
        r["raw_source_lag_s"] = ({"min": round(min(delays), 6), "median": round(median(delays), 6),
                                  "max": round(max(delays), 6)} if delays else None)
        r["latest_raw_source_receipt_s"] = max(received.values(), default=None)
    if files != sources(root, info, fair.genome.fair.vol_window_s) or digest(ledger_path) != ledger_digest:
        raise ValueError("inputs changed during the reference audit")
    return {"schema": "wallet-dissection-first-reference-inventory/2", "wallet": case["wallet"],
            "tx": case["transaction"], "window": [start, info["end"]], "cutoffs": asof,
            "opening_revision_count": len(opening_history),
            "first_retained_opening_receipt_ns": opening_history[0][0] if opening_history else None,
            "retrospective_economic_inventory": inv, "reference_sources": files,
            "reference_scope": "The raw proxy uses the PERELMAN capture's upstream receive clock or the DOPPEL live poll receipt. Historical PM US series downloads supply the opening price only after receipt. The international venue's native settlement benchmark and the wallet's feeds are unobserved."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args()
    print(orjson.dumps(report(args.tape, args.ledger), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode())
