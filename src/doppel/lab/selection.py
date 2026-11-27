"""Stage 0: freeze a prospective comparison, then grade one wallet on completed weeks."""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from pathlib import Path

import numpy as np
import orjson

from ..tape.segment import atomic_write
from . import selection_plan
from .selection_evidence import window_profit

WALLET = re.compile(r"0x[0-9a-f]{40}")
MICRO = 1_000_000


def paired_week(candidate: str, controls: list[str], profits: dict[str, dict[str, int]]) -> tuple[dict, dict]:
    """Pair by condition on the union of activity. A proven inactive member contributes zero."""
    conditions = sorted({key for member in (candidate, *controls) for key in profits[member]})
    n = len(controls)
    # Keep the equal-weight control benchmark exact until rendering and bootstrapping.
    excess = {key: n * profits[candidate].get(key, 0) - sum(profits[c].get(key, 0) for c in controls)
              for key in conditions}
    pnl = sum(profits[candidate].values())
    control_total = sum(sum(profits[c].values()) for c in controls)
    return {"candidate_pnl_micro": pnl, "control_pnl_sum_micro": control_total, "controls_n": n,
            "excess_usd": sum(excess.values()) / (n * MICRO), "conditions": len(conditions),
            "won": pnl > 0 and sum(excess.values()) > 0}, excess


def paired_interval(clusters: list[dict], controls_n: int) -> list[float] | None:
    """Resample whole conditions, keeping wallet/control pairing and four weekly strata intact."""
    policy = selection_plan.DECISION["event_cluster_test"]
    if not any(clusters) or any(week and len(week) < policy["minimum_clusters_in_nonempty_week_to_pass"]
                                for week in clusters):
        return None
    rng = np.random.default_rng(policy["seed"])
    samples = np.zeros(policy["bootstrap_samples"])
    for week in clusters:
        if not week:
            continue
        values = np.array([week[k] for k in sorted(week)], dtype=np.float64) / (controls_n * MICRO)
        for i in range(0, len(samples), 32):
            batch = samples[i:i + 32]
            indices = rng.integers(len(values), size=(len(batch), len(values)))
            batch += values[indices].sum(axis=1)
    lower = policy["lower_percentile"] / 100
    return np.quantile(samples, (lower, 1 - lower)).tolist()


def evaluate(plan: dict, wallet: str, reports: Path | None, tokens_path: Path | None,
             as_of_ns: int) -> dict:
    """Read completed observation windows only. The CLI supplies its clock and verifies the plan pin."""
    selection_plan.validate_plan(plan)
    wallet = wallet.lower()
    if not WALLET.fullmatch(wallet):
        raise ValueError("expected a 20-byte wallet address")
    if as_of_ns < plan["frozen_tw"]:
        raise ValueError("evaluation precedes the match plan freeze")
    match = plan["matches"].get(wallet)
    result = {"schema": "wallet-selection/1", "wallet": wallet, "plan_sha256": plan["sha256"],
              "cohort_file_sha256": plan["cohort_sha256"], "cohort_frozen_tw": plan["cohort_frozen_tw"],
              "as_of_tw": as_of_ns, "scope": "individual prospective matched-control test",
              "metric": "settled crypto-window trading P&L after fill fees, excluding unattributed income",
              "status": "INCONCLUSIVE", "selected_for_next_layer": False, "weeks": []}
    if match is None:
        return result | {"reason": "outside_frozen_candidate_list", "controls": [],
                         "cohort_role": "control" if wallet in plan["controls"] else "outside"}
    result["cohort_role"] = "candidate"
    ranking = plan.get("intake_ranking")
    result["candidate_rank"] = (ranking["by_wallet"][wallet]["rank"] if ranking
                                else plan["candidates"].index(wallet) + 1)
    result["matching"] = match
    if match["status"] != "matched":
        return result | {"reason": "no_matched_control", "controls": []}
    controls = match["controls"]
    result["controls"] = controls
    tokens = None
    if tokens_path is not None and any(w["end"] * 10**9 <= as_of_ns for w in plan["windows"]):
        raw_tokens = tokens_path.read_bytes()
        tokens = {}
        for line in raw_tokens.splitlines():
            if line.strip():
                row = orjson.loads(line)
                previous = tokens.get(row["token"])
                if previous and previous.get("status") == "resolved" and previous != row:
                    raise ValueError("resolved token metadata changed in the supplied snapshot")
                tokens[row["token"]] = row
        result["tokens_file_sha256"] = hashlib.sha256(raw_tokens).hexdigest()
    clusters = []
    seen = set()
    for span in plan["windows"]:
        start, end = span["start"], span["end"]
        week = dict(span)
        result["weeks"].append(week)
        if end * 10**9 > as_of_ns:
            week["status"] = "pending"
            continue
        profits, hashes, blockers = {}, {}, {}
        for member in (wallet, *controls):
            path = reports / str(start) / f"wallet-{member}.json" if reports else None
            if path is None or not path.is_file():
                blockers[member] = "missing absorption receipt"
                continue
            raw = path.read_bytes()
            hashes[member] = hashlib.sha256(raw).hexdigest()
            if tokens is None:
                blockers[member] = "missing token metadata"
                continue
            try:
                profits[member] = window_profit(orjson.loads(raw), tokens, member, start, end)
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                blockers[member] = f"unusable receipt: {exc}"
        week["receipt_sha256"] = hashes
        if blockers:
            week.update(status="inconclusive", reasons=blockers)
            continue
        stats, excess = paired_week(wallet, controls, profits)
        if seen.intersection(excess):
            week.update(status="inconclusive", reason="condition appears in multiple weeks")
            continue
        seen.update(excess)
        clusters.append(excess)
        week.update(status="complete", **stats)
    result["complete_weeks"] = len(clusters)
    result["winning_weeks"] = sum(w.get("won", False) for w in result["weeks"])
    policy = plan["decision_rule"]
    if len(clusters) != policy["required_complete_periods"]:
        return result | {"reason": "four_complete_weeks_required"}
    net = sum(w["candidate_pnl_micro"] for w in result["weeks"])
    interval = paired_interval(clusters, len(controls))
    result.update(candidate_pnl_micro=net, paired_excess_ci95_usd=interval)
    if result["winning_weeks"] < policy["required_winning_periods"] or net <= 0:
        return result | {"status": "FAIL", "reason": "profit_or_relative_persistence_failed"}
    if interval is None:
        return result | {"reason": "insufficient_independent_conditions"}
    if interval[0] <= 0:
        return result | {"status": "FAIL", "reason": "paired_excess_lower_bound_not_positive"}
    return result | {"status": "PASS", "selected_for_next_layer": True, "reason": "wallet_persistence_passed"}


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["freeze"]:
        return selection_plan.main(argv[1:])
    ap = argparse.ArgumentParser(prog="doppel-select", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    sub.add_parser("freeze", help="freeze matching and four future weeks")
    p = sub.add_parser("wallet", help="evaluate one wallet against its frozen controls")
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--plan-sha256", required=True, help="canonical plan hash recorded before the first window")
    p.add_argument("--wallet", required=True)
    p.add_argument("--reports", type=Path, help="parent of START/wallet-ADDRESS.json absorption receipts")
    p.add_argument("--tokens", type=Path, help="settlement metadata for observed tokens")
    p.add_argument("--out", type=Path, help="write the decision once to a new file")
    a = ap.parse_args(argv)
    try:
        plan = orjson.loads(a.plan.read_bytes())
        if plan["sha256"] != a.plan_sha256:
            raise ValueError("plan differs from the prospective pin")
        result = evaluate(plan, a.wallet, a.reports, a.tokens, time.time_ns())
        raw = orjson.dumps(result, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
        if a.out:
            atomic_write(a.out, raw, replace=False)
        print(raw.decode())
        return 0 if result["selected_for_next_layer"] else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        ap.error(str(exc))


if __name__ == "__main__":
    sys.exit(main())
