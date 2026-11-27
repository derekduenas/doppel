"""Freeze a prospective, wallet-level persistence test from a profit-blind identity scan."""

import argparse
import hashlib
import math
import os
import re
import tempfile
import time
from pathlib import Path

import orjson

from ..tape.segment import atomic_write, sync_directory
from . import scan as intake


WEEK = 7 * 86400
MARKET_INTERVAL = 900
ASSETS = ("btc", "eth", "sol", "xrp")
HORIZONS = ("5m", "15m")
SCHEMA = "wallet-selection-plan-v1"
MATCHING = {"max_controls": 3, "max_activity_ratio": 2, "max_price_difference": 0.10,
            "max_extreme_price_share_difference": 0.25,
            "max_asset_total_variation": 0.25, "max_horizon_total_variation": 0.25}
CALIPERS = ("notional", "market_windows", "median_price", "extreme_price_share", "asset_mix", "horizon_mix")
DECISION = {
    "unit": "wallet", "markets": {"assets": list(ASSETS), "horizons": list(HORIZONS)},
    "category_support": "separate asset and horizon marginals from the discovery scan",
    "benchmark": "equal-weight mean of frozen controls", "pnl": "after-fee trading P&L in USD",
    "periods": 4, "required_complete_periods": 4, "required_winning_periods": 3,
    "winning_period": "wallet net > 0 and wallet net > control mean",
    "total_wallet_net_must_be_positive": True,
    "event_cluster_test": {"estimand": "sum of paired excess over four weeks", "strata": "week",
                           "cluster": "condition", "bootstrap_samples": 2000, "seed": 20261127,
                           "lower_percentile": 2.5, "lower_must_exceed": 0,
                           "minimum_clusters_in_nonempty_week_to_pass": 2},
    "complete_idle_week": "nonwin",
    "missing_evidence": "inconclusive",
}
ADDRESS = re.compile(r"0x[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")


def digest(doc: dict) -> str:
    return hashlib.sha256(orjson.dumps(doc, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _hash_value(value):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ValueError("expected a lowercase SHA-256 digest")
    return value


def _wallets(addresses):
    if not isinstance(addresses, (list, dict)) or not all(
        isinstance(a, str) and ADDRESS.fullmatch(a) for a in addresses
    ) or len(addresses) != len(set(addresses)):
        raise ValueError("wallets must be unique, lowercase, full-length addresses")


def _windows(frozen_ns):
    if type(frozen_ns) is not int or frozen_ns <= 0:
        raise ValueError("invalid plan freeze clock")
    start = (frozen_ns // 1_000_000_000 // MARKET_INTERVAL + 1) * MARKET_INTERVAL
    return [{"start": start + i * WEEK, "end": start + (i + 1) * WEEK} for i in range(4)]


def _features(row):
    """Only fields measurable during the selection scan may enter the comparison."""
    features = {k: row.get(k) for k in ("usd", "n_windows", "px_p50", "extreme_px_share", "fills", "assets", "horizons")}
    for key in ("usd", "px_p50", "extreme_px_share"):
        value = features[key]
        if type(value) not in (int, float) or not math.isfinite(value):
            return None
    if features["usd"] <= 0 or not all(0 <= features[k] <= 1 for k in ("px_p50", "extreme_px_share")):
        return None
    n, wins = features["fills"], features["n_windows"]
    if type(n) is not int or type(wins) is not int or not 0 < wins <= n:
        return None
    for key, categories in (("assets", ASSETS), ("horizons", HORIZONS)):
        counts = features[key]
        if (not isinstance(counts, dict) or set(counts) - set(categories)
                or any(type(v) is not int or v < 0 for v in counts.values()) or sum(counts.values()) != n):
            return None
    return features


def _distances(a, b):
    """Scale distances by each independent support limit before averaging for ranking."""
    def activity(key):
        return abs(math.log2(a[key]) - math.log2(b[key])) / math.log2(MATCHING["max_activity_ratio"])

    def total_variation(key, categories, limit):
        tv = sum(abs(a[key].get(k, 0) / a["fills"] - b[key].get(k, 0) / b["fills"])
                 for k in categories) / 2
        return tv / limit

    return [
        activity("usd"), activity("n_windows"),
        abs(a["px_p50"] - b["px_p50"]) / MATCHING["max_price_difference"],
        abs(a["extreme_px_share"] - b["extreme_px_share"]) / MATCHING["max_extreme_price_share_difference"],
        total_variation("assets", ASSETS, MATCHING["max_asset_total_variation"]),
        total_variation("horizons", HORIZONS, MATCHING["max_horizon_total_variation"]),
    ]


def _match(candidates, controls, features):
    matches = {}
    for wallet in candidates:
        distances = []
        passing = dict.fromkeys(CALIPERS, 0)
        if features[wallet] is not None:
            for control in controls:
                if features[control] is not None:
                    deltas = _distances(features[wallet], features[control])
                    for key, delta in zip(CALIPERS, deltas):
                        passing[key] += int(delta <= 1 + 1e-12)
                    if max(deltas) <= 1 + 1e-12:
                        distances.append((sum(deltas) / len(deltas), control))
        ranked = sorted(distances)
        selected = [control for _, control in ranked[:MATCHING["max_controls"]]]
        reason = None if selected else (
            "candidate missing matching features" if features[wallet] is None else "no control in common support")
        matches[wallet] = {"status": "matched" if selected else "unmatched", "controls": selected,
                           "controls_in_support": len(ranked), "controls_passing_each_limit": passing, "reason": reason}
    return matches


def _check_members(candidates, controls):
    _wallets(candidates)
    _wallets(controls)
    if (not isinstance(candidates, list) or not isinstance(controls, list)
            or not candidates or not controls or set(candidates) & set(controls)):
        raise ValueError("selection requires nonempty, disjoint candidates and controls")


def _intake_ranking(notionals, seed):
    """Discovery ranks include controls and use the original scan even when matching features are missing."""
    if type(seed) is not int or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0
                                     for v in notionals.values()):
        raise ValueError("invalid interleaved ranking evidence")
    ranked = sorted(notionals, key=lambda w: (-notionals[w], w))
    return {"design": "interleaved", "seed": seed,
            "by_wallet": {w: {"rank": i, "notional_usd": notionals[w]} for i, w in enumerate(ranked, 1)}}


def build_plan(scan_doc: dict, cohort: dict, *, scan_sha256: str, cohort_sha256: str, now_ns=None) -> dict:
    """Build from documents whose file digests were verified by the caller; freeze_plan does so on disk."""
    intake.validate_cohort(cohort)
    candidates, controls = cohort["candidates"], cohort["controls"]
    _check_members(candidates, controls)
    wallets = scan_doc.get("wallets")
    if not isinstance(wallets, dict):
        raise ValueError("selection scan requires a wallet map")
    _wallets(wallets)
    if not set(candidates + controls) <= wallets.keys() or scan_doc.get("span") != cohort["span"]:
        raise ValueError("selection scan differs from frozen cohort membership or span")
    built, frozen = scan_doc.get("built_tw"), cohort["frozen_tw"]
    if (type(built) is not int or type(frozen) is not int or built <= 0
            or not cohort["span"][1] * 1_000_000_000 <= built <= frozen):
        raise ValueError("selection scan must be completed as of the cohort freeze")
    if cohort.get("schema_version") == 2 and (
        digest(scan_doc) != cohort["scan_sha256"] or scan_doc.get("as_of_tw") != cohort["as_of_tw"]
        or cohort["as_of_tw"] > built
    ):
        raise ValueError("selection scan differs from the cohort's pinned scan")
    rule = cohort["rule"]
    design = rule.get("design", "lower-band")
    band = None if design == "interleaved" else rule["control_band_ranks"]
    n, ranked_candidates, ranked_controls = intake.rank_members(
        scan_doc, rule["n_candidates"], rule["n_controls"], rule["min_windows"], band, rule["seed"], design=design)
    if n != cohort["eligible"] or ranked_candidates != candidates or ranked_controls != controls:
        raise ValueError("scan does not reproduce the frozen cohort rankings and controls")
    features = {a: _features(wallets[a]) for a in candidates + controls}
    matches = _match(candidates, controls, features)
    registered = time.time_ns() if now_ns is None else now_ns
    windows = _windows(registered)
    if registered < frozen:
        raise ValueError("selection input is from the future")
    body = {"schema": SCHEMA, "frozen_tw": registered, "scan_built_tw": built, "cohort_frozen_tw": frozen,
            "scan_sha256": _hash_value(scan_sha256), "cohort_sha256": _hash_value(cohort_sha256),
            "cohort_membership_sha256": cohort["sha256"], "scan_span": list(cohort["span"]),
            "candidates": list(candidates), "controls": list(controls),
            "matching_policy": MATCHING.copy(), "decision_rule": orjson.loads(orjson.dumps(DECISION)),
            "features": features, "matches": matches, "windows": windows}
    if design == "interleaved":
        body["intake_ranking"] = _intake_ranking({a: wallets[a]["usd"] for a in candidates + controls}, rule["seed"])
    return {**body, "sha256": digest(body)}


def validate_plan(plan: dict) -> None:
    """Verify policy, clock, membership and actual matches before evaluation."""
    if not isinstance(plan, dict) or plan.get("sha256") != digest({k: v for k, v in plan.items() if k != "sha256"}):
        raise ValueError("selection plan digest mismatch")
    if plan.get("schema") != SCHEMA or plan.get("matching_policy") != MATCHING or plan.get("decision_rule") != DECISION:
        raise ValueError("unrecognized selection plan policy")
    candidates, controls = plan["candidates"], plan["controls"]
    _check_members(candidates, controls)
    if plan.get("windows") != _windows(plan["frozen_tw"]):
        raise ValueError("invalid prospective selection windows")
    for key in ("scan_sha256", "cohort_sha256", "cohort_membership_sha256"):
        _hash_value(plan[key])
    built, frozen, span = plan["scan_built_tw"], plan["cohort_frozen_tw"], plan["scan_span"]
    if (type(built) is not int or type(frozen) is not int or not 0 < built <= frozen <= plan["frozen_tw"]
            or len(span) != 2 or any(type(t) is not int for t in span)
            or not 0 <= span[0] <= span[1] <= built // 1_000_000_000):
        raise ValueError("invalid selection observation clocks")
    features = plan["features"]
    if (set(features) != set(candidates + controls)
            or any(f is not None and (not isinstance(f, dict) or _features(f) is None) for f in features.values())
            or plan["matches"] != _match(candidates, controls, features)):
        raise ValueError("selection plan matching evidence is inconsistent")
    if "intake_ranking" in plan:
        ranking = plan["intake_ranking"]
        members = ranking["by_wallet"]
        if set(members) != set(features):
            raise ValueError("interleaved discovery ranks do not cover the registered sample")
        amounts = {w: row["notional_usd"] for w, row in members.items()}
        if (ranking != _intake_ranking(amounts, ranking["seed"])
                or any(features[w] is not None and features[w]["usd"] != amounts[w] for w in members)):
            raise ValueError("interleaved discovery rank disagrees with recorded activity")
        # All registered members are the consecutive top N+C eligible ranks. Recheck role
        # allocation even when a missing price or category feature made a match impossible.
        ordered_sample = {"wallets": {w: {"usd": v, "n_windows": 1} for w, v in amounts.items()}}
        _, cands, ctrls = intake.rank_members(ordered_sample, len(candidates), len(controls), 1,
                                               seed=ranking["seed"], design="interleaved")
        if (cands, ctrls) != (candidates, controls):
            raise ValueError("interleaved roles disagree with registered discovery ranks")


def freeze_plan(scan_path: Path, cohort_path: Path, out: Path, *, expected_scan_sha256: str,
                expected_cohort_sha256: str) -> dict:
    scan_bytes, cohort_bytes = scan_path.read_bytes(), cohort_path.read_bytes()
    for data, expected in ((scan_bytes, expected_scan_sha256), (cohort_bytes, expected_cohort_sha256)):
        if hashlib.sha256(data).hexdigest() != _hash_value(expected):
            raise ValueError("selection input file digest mismatch")
    plan = build_plan(orjson.loads(scan_bytes), orjson.loads(cohort_bytes),
                      scan_sha256=expected_scan_sha256, cohort_sha256=expected_cohort_sha256)
    validate_plan(plan)
    deadline = plan["windows"][0]["start"] * 1_000_000_000
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".selection-", dir=out.parent) as private:
        staged = Path(private) / "plan.json"
        atomic_write(staged, orjson.dumps(plan, option=orjson.OPT_INDENT_2) + b"\n", replace=False)
        if not plan["frozen_tw"] <= time.time_ns() < deadline:
            raise ValueError("the publication clock is outside the registration interval; refreeze")
        os.link(staged, out)
        # The link refuses overwrite. If it straddled the cutoff, discard only our new link.
        if not plan["frozen_tw"] <= time.time_ns() < deadline:
            if staged.samefile(out):
                out.unlink()
                sync_directory(out.parent)
            raise ValueError("plan publication crossed the registration cutoff; refreeze")
        sync_directory(out.parent)
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", type=Path, required=True)
    parser.add_argument("--cohort", type=Path, required=True)
    parser.add_argument("--scan-sha256", required=True)
    parser.add_argument("--cohort-sha256", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plan = freeze_plan(args.scan, args.cohort, args.out, expected_scan_sha256=args.scan_sha256,
                           expected_cohort_sha256=args.cohort_sha256)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")
    print(f"plan {plan['sha256']} registered at {plan['frozen_tw']}; output {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
