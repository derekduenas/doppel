import hashlib

import orjson
import pytest

from doppel.lab import selection_plan as sp


def address(n):
    return "0x" + f"{n:040x}"


FREEZE = 1_789_000_000_500_000_000
DAY = 86400
CAND = address(1)


def setup_scan():
    # The second control ties with the first; address order determines their frozen rank.
    wallets = {
        address(i): {"fills": 50, "n_windows": 10, "usd": usd, "px_p50": .52,
                     "assets": {"btc": 40, "sol": 10}, "horizons": {"5m": 50}}
        for i, usd in enumerate([0, 1000, 740, 740, 650, 600, 100], 0) if i
    }
    scan = {"span": [1_788_999_000, 1_789_000_000], "built_tw": FREEZE, "wallets": wallets}
    # Preserve the original v1 canonical body irrespective of newer freeze format defaults.
    cohort = {"rule": {"metric": "crypto-window notional (price*size) on RTDS union, profit-blind",
                       "min_windows": 5, "n_candidates": 1, "control_band_ranks": [2, 6],
                       "n_controls": 5, "seed": 20261125},
              "span": scan["span"], "eligible": 6, "candidates": [CAND],
              "controls": [address(i) for i in range(2, 7)]}
    cohort.update(sha256=sp.digest(cohort), frozen_tw=FREEZE + 1)
    return scan, cohort


def build(scan, cohort):
    return sp.build_plan(scan, cohort, scan_sha256="1" * 64, cohort_sha256="2" * 64, now_ns=FREEZE + 1000)


def test_study_prospectively_matches_and_freezes_every_field():
    scan, cohort = setup_scan()
    plan = build(scan, cohort)
    sp.validate_plan(plan)
    row = plan["matches"][CAND]
    assert row["status"] == "matched" and row["controls"] == [address(2), address(3), address(4)]
    assert row["controls_in_support"] == 4
    assert plan["windows"][0]["start"] * 10**9 > plan["frozen_tw"]
    assert plan["windows"][0]["start"] % 900 == 0
    assert all(w["end"] - w["start"] == 7 * DAY for w in plan["windows"])
    assert all(a["end"] == b["start"] for a, b in zip(plan["windows"], plan["windows"][1:]))
    # Selection never consults the scan's mode label, income or forward profitability.
    scan["wallets"][address(2)].update(net_profit=-100000, label="UNPROFITABLE")
    assert build(scan, cohort) == plan
    plan["frozen_tw"] -= 1
    with pytest.raises(ValueError, match="digest"):
        sp.validate_plan(plan)


@pytest.mark.parametrize("delta", [
    {"n_windows": 5, "px_p50": .63}, {"n_windows": 4}, {"assets": {"eth": 50}},
    {"horizons": {"15m": 50}}, {"assets": {"btc": 3}}, {"px_p50": float("nan")},
])
def test_common_support_and_missing_features(delta):
    scan, cohort = setup_scan()
    for a in cohort["controls"]:
        scan["wallets"][a].update(delta)
    if delta.get("n_windows") == 4:
        # Keep the registered eligibility threshold satisfied while failing the 2x window band.
        scan["wallets"][CAND]["n_windows"] = 20
        for a in cohort["controls"]:
            scan["wallets"][a]["n_windows"] = 8
    row = build(scan, cohort)["matches"][CAND]
    assert row["status"] == "unmatched" and row["controls"] == [] and row["controls_in_support"] == 0
    assert row["reason"] == "no control in common support"


def test_rehashed_overrides_require_consistent_assignments_and_windows():
    for target in ("windows", "matches", "decision_rule"):
        scan, cohort = setup_scan()
        plan = build(scan, cohort)
        if target == "windows":
            plan["windows"][0]["start"] -= 900
        elif target == "matches":
            plan["matches"][CAND]["controls"] = [CAND]
        else:
            plan["decision_rule"]["required_winning_periods"] = 1
        plan["sha256"] = sp.digest({k: v for k, v in plan.items() if k != "sha256"})
        with pytest.raises(ValueError):
            sp.validate_plan(plan)


def test_future_clocks_alias_and_unreproducible_cohort_are_refused():
    scan, cohort = setup_scan()
    with pytest.raises(ValueError, match="future"):
        sp.build_plan(scan, cohort, scan_sha256="1" * 64, cohort_sha256="2" * 64, now_ns=FREEZE)
    scan["wallets"][address(2)]["usd"] = 1200
    with pytest.raises(ValueError, match="reproduce"):
        build(scan, cohort)
    scan, cohort = setup_scan()
    scan["wallets"]["0x" + "AA" * 20] = scan["wallets"][address(2)]
    with pytest.raises(ValueError, match="lowercase"):
        build(scan, cohort)


def test_file_freeze_checks_input_bytes_and_prevents_replacement(tmp_path):
    scan, cohort = setup_scan()
    s, c, out = (tmp_path / n for n in ("scan.json", "cohort.json", "plan.json"))
    s.write_bytes(orjson.dumps(scan))
    c.write_bytes(orjson.dumps(cohort))
    kwargs = {"expected_scan_sha256": hashlib.sha256(s.read_bytes()).hexdigest(),
              "expected_cohort_sha256": hashlib.sha256(c.read_bytes()).hexdigest()}
    with pytest.raises(ValueError, match="file digest"):
        sp.freeze_plan(s, c, out, **(kwargs | {"expected_scan_sha256": "3" * 64}))
    assert not out.exists()
    expected = sp.freeze_plan(s, c, out, **kwargs)
    saved = out.read_bytes()
    assert expected == orjson.loads(saved)
    with pytest.raises(FileExistsError):
        sp.freeze_plan(s, c, out, **kwargs)
    assert out.read_bytes() == saved


def test_v2_requires_exact_frozen_scan_even_when_membership_stays_the_same():
    scan, old = setup_scan()
    scan.update(as_of_tw=FREEZE - 1, segments={"rtds_a": ["sealed"]})
    cohort = {**old, "schema_version": 2, "scan_sha256": sp.digest(scan), "as_of_tw": scan["as_of_tw"]}
    cohort["sha256"] = sp.intake.cohort_digest(cohort)
    sp.validate_plan(build(scan, cohort))
    scan["wallets"][CAND]["px_p50"] = .49
    with pytest.raises(ValueError, match="pinned scan"):
        build(scan, cohort)


def test_extreme_activity_and_missing_candidate_features_cannot_qualify():
    scan, cohort = setup_scan()
    scan["wallets"][CAND]["usd"] = 7000
    match = build(scan, cohort)["matches"][CAND]
    assert match["controls"] == [] and match["controls_passing_each_limit"]["notional"] == 0
    assert match["controls_passing_each_limit"]["median_price"] == 5
    del scan["wallets"][CAND]["px_p50"]
    assert build(scan, cohort)["matches"][CAND]["reason"] == "candidate missing matching features"
