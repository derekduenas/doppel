import hashlib

import orjson
import pytest

from doppel.lab import scan, selection, selection_plan


CANDIDATE = "0x" + "11" * 20
CONTROLS = ["0x" + "22" * 20, "0x" + "33" * 20]


def sources():
    wallets = {}
    for i, address in enumerate([CANDIDATE, *CONTROLS]):
        wallets[address] = {"usd": 100 - i, "fills": 30, "n_windows": 5, "px_p50": 0.4, "extreme_px_share": 0.1,
                            "assets": {"btc": 30}, "horizons": {"5m": 30}}
    doc = {"wallets": wallets, "built_tw": 2_000_000_000, "span": [0, 1]}
    rule = {"metric": "crypto-window notional (price*size) on RTDS union, profit-blind", "n_candidates": 1,
            "n_controls": 2, "control_band_ranks": [2, 3], "min_windows": 1, "seed": 1}
    cohort = {"span": doc["span"], "eligible": 3, "rule": rule, "candidates": [CANDIDATE],
              "controls": CONTROLS, "frozen_tw": 3_000_000_000}
    cohort["sha256"] = scan.cohort_digest(cohort)
    return doc, cohort


def plan():
    doc, cohort = sources()
    return selection_plan.build_plan(doc, cohort, scan_sha256="1" * 64, cohort_sha256="2" * 64,
                                     now_ns=4_000_000_000)


def test_cli_pins_legacy_freeze_and_waits_for_future_weeks(tmp_path, capsys):
    doc, cohort = sources()
    sp, cp, pp = (tmp_path / key for key in ("scan", "cohort", "plan"))
    sp.write_bytes(orjson.dumps(doc))
    cp.write_bytes(orjson.dumps(cohort))
    freeze = ["freeze", "--scan", str(sp), "--cohort", str(cp), "--out", str(pp),
              "--scan-sha256", hashlib.sha256(sp.read_bytes()).hexdigest(),
              "--cohort-sha256", hashlib.sha256(cp.read_bytes()).hexdigest()]
    assert selection.main(freeze) == 0
    capsys.readouterr()
    ref = orjson.loads(pp.read_bytes())["sha256"]
    argv = ["wallet", "--plan", str(pp), "--plan-sha256", ref, "--wallet", CANDIDATE]
    assert selection.main(argv) == 1
    result = orjson.loads(capsys.readouterr().out)
    assert not result["selected_for_next_layer"] and result["complete_weeks"] == 0
    assert [week["status"] for week in result["weeks"]] == ["pending"] * 4
    assert result["controls"] == CONTROLS
    # A valid control is never admitted as this plan's candidate.
    assert selection.main([*argv[:-1], CONTROLS[0]]) == 1
    assert orjson.loads(capsys.readouterr().out)["reason"] == "outside_frozen_candidate_list"
    with pytest.raises(SystemExit):
        selection.main([*argv[:4], "a" * 64, *argv[5:]])


def test_three_wins_need_a_complete_fourth_week_and_all_frozen_controls(tmp_path, monkeypatch):
    prospective = plan()
    final_ns = prospective["windows"][-1]["end"] * 10**9
    tokens = tmp_path / "tokens.jsonl"
    tokens.write_text("")
    # The adapter's chain and token accounting is exercised in test_selection_evidence.
    # Here each condition has a different winner among controls. Both controls stay in the mean.
    def observed(receipt, metadata, member, start, end):
        assert receipt["wallet"] == member and receipt["window_time"] == [start, end]
        return receipt["derived"]
    monkeypatch.setattr(selection, "window_profit", observed)
    paths = {}
    for i, window in enumerate(prospective["windows"]):
        a, b = window["start"], window["end"]
        for member in (CANDIDATE, *CONTROLS):
            if i == 3:
                values = {}
            elif member == CANDIDATE:
                values = {f"a{i}": 10_000_000, f"b{i}": 15_000_000}
            elif member == CONTROLS[0]:
                values = {f"a{i}": 8_000_000}
            else:
                values = {f"b{i}": 4_000_000}
            path = tmp_path / str(a) / f"wallet-{member}.json"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(orjson.dumps({"wallet": member, "window_time": [a, b], "derived": values}))
            paths[i, member] = path
    def score(at=final_ns):
        return selection.evaluate(prospective, CANDIDATE, tmp_path, tokens, at)
    assert score(final_ns - 1)["status"] == "INCONCLUSIVE"
    passed = score()
    assert passed["status"] == "PASS" and passed["selected_for_next_layer"]
    assert passed["winning_weeks"] == 3 and passed["weeks"][0]["excess_usd"] == 19
    assert passed["paired_excess_ci95_usd"][0] > 0
    paths[3, CONTROLS[0]].unlink()
    missing = score()
    assert missing["status"] == "INCONCLUSIVE" and missing["complete_weeks"] == 3
    assert CONTROLS[0] in missing["weeks"][3]["reasons"]


def test_negative_absolute_profit_and_weak_paired_evidence():
    stats, _ = selection.paired_week(CANDIDATE, CONTROLS,
            {CANDIDATE: {"a": -1}, CONTROLS[0]: {"a": -10}, CONTROLS[1]: {"a": -10}})
    assert stats["excess_usd"] > 0 and not stats["won"]
    assert selection.paired_interval([{"a": 20}, {}, {}, {}], 1) is None
    interval = selection.paired_interval([{"a": -100, "b": 101}] * 4, 1)
    assert interval is not None and interval[0] < 0 < interval[1]
