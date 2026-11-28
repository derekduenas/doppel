from __future__ import annotations

from pathlib import Path

import pytest

from doppel.lab import layer5b as exam


NS = exam.NS
ORIGIN = 1_800_000_000 * NS


def panel(start_market, count):
    for i in range(start_market, start_market + count):
        for j in range(40):
            t = ORIGIN + (100 * i + j) * NS
            yield {
                "ts_ns": t,
                "market_id": f"btc-fixture-{i:03}",
                "book": {"tw_ns": t - 100_000_000, "up_bid": .48, "up_ask": .52,
                         "down_bid": .48, "down_ask": .52},
                "fair": {"tw_ns": t - 200_000_000, "p_up": {8: .56, 9: .56, 22: .44, 26: .546}.get(j, .5)},
                "lead": {"tw_ns": t - 300_000_000, "binance_tw_ns": t - 400_000_000,
                         "hyperliquid_tw_ns": t - 500_000_000,
                         "p_up": {4: .55, 5: .55, 14: .45, 34: .54}.get(j, .5)},
            }


def create_plan(tmp_path):
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    training, holdout = (inputs / name for name in exam.PUBLIC)
    exam.write_rows(training, panel(0, 24))
    exam.write_rows(holdout, panel(24, 20))
    plan = {
        "schema": "doppel.layer5b-plan/v1",
        "source_kind": "synthetic",
        "training_range_ns": [ORIGIN, ORIGIN + 2400 * NS],
        "holdout_range_ns": [ORIGIN + 2400 * NS, ORIGIN + 4400 * NS],
        "probe_cutoffs_ns": [ORIGIN + sec * NS for sec in (2807, 3400, 3900)],
        "public_sha256": {name: exam.sha256(inputs / name) for name in exam.PUBLIC},
        "source_receipts": {},
    }
    path = tmp_path / "plan.json"
    exam.write_rows(path, (plan,))
    return inputs, path, plan


def test_synthetic_forecast_seals_before_scoring_and_cannot_qualify(tmp_path):
    inputs, plan_path, _ = create_plan(tmp_path)
    forecasts = tmp_path / "forecasts"
    plan_sha = exam.sha256(plan_path)
    commitment = exam.freeze(plan_path, plan_sha, inputs, forecasts)
    assert sorted(p.name for p in forecasts.iterdir()) == [
        "forecast.json", "model-0.jsonl", "model-1.jsonl", "model-2.jsonl",
        "predictions-0.jsonl", "predictions-1.jsonl", "predictions-2.jsonl",
    ]
    result = exam.score(plan_path, plan_sha, inputs, forecasts, commitment["forecast_sha256"])
    assert result["status"] == "UNMEASURED"
    assert result["software_status"] == "PASS"
    for subject, source in list(exam.SUBJECTS.items())[:2]:
        measured = result["subjects"][subject]
        assert measured["status"] == "PASS"
        assert measured["trigger_source"] == source
        assert measured["precision"] == measured["recall"] == measured["f1"] == 1
        assert measured["median_timing_delta_ns"] == measured["median_absolute_delta_ns"] == 0
        assert measured["truth"] == measured["predicted"] == measured["matched"] == 40
        assert measured["truth_markets"] == 20
        assert measured["zero_leakage"] is True
        assert all(probe["altered_future_rows"] > 0 for probe in measured["causality"])
    control = result["subjects"]["random_taker_control"]
    assert control["status"] == "ABSTAIN"
    assert control["truth"] > 0
    assert control["predicted"] == 0
    assert control["zero_leakage"] is True

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        exam.score(plan_path, plan_sha, inputs, forecasts, "f" * 64)
    (forecasts / "predictions-0.jsonl").write_text("")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        exam.score(plan_path, plan_sha, inputs, forecasts, commitment["forecast_sha256"])


def event(ts, market="a", side="BUY_UP"):
    return {"ts_ns": ts, "market_id": market, "side": side}


def test_match_once_in_same_market_and_side_and_count_all_misses():
    actual = [event(2 * NS), event(4 * NS), event(5 * NS, "market-2"), event(6 * NS)]
    prediction = [event(NS), event(2 * NS), event(4 * NS + NS // 2),
                  event(5 * NS, "market-2", "BUY_DOWN"), event(7 * NS + 1)]
    out = exam.timing(prediction, actual)
    assert out["truth"] == 4 and out["predicted"] == 5 and out["matched"] == 2
    assert out["recall"] == .5 and out["precision"] == .4 and out["f1"] == pytest.approx(4 / 9)
    assert out["median_timing_delta_ns"] == -250_000_000
    assert out["median_absolute_delta_ns"] == 750_000_000
    assert out["matched_markets"] == 1


def test_oracle_requires_strictly_earlier_receive_clocks(tmp_path):
    public = tmp_path / "panel"
    panels = list(panel(0, 1))
    panels[4]["lead"]["hyperliquid_tw_ns"] = panels[4]["ts_ns"]
    panels[8]["fair"]["tw_ns"] = panels[8]["ts_ns"]
    exam.write_rows(public, panels)
    assert [r["ts_ns"] for r in exam.oracle(public, "scripted_lead_taker", training=False)] == [
        ORIGIN + 5 * NS, ORIGIN + 14 * NS,
    ]
    assert [r["ts_ns"] for r in exam.oracle(public, "scripted_fair_taker", training=False)] == [
        ORIGIN + 9 * NS, ORIGIN + 22 * NS,
    ]


def test_plan_refuses_overlapping_markets_and_synthetic_relabel(tmp_path):
    inputs, plan_path, plan = create_plan(tmp_path)
    path = inputs / exam.PUBLIC[1]
    path.unlink()
    samples = list(panel(24, 20))
    samples[0]["market_id"] = "btc-fixture-000"
    exam.write_rows(path, samples)
    plan["public_sha256"][path.name] = exam.sha256(path)
    plan_path.unlink()
    exam.write_rows(plan_path, (plan,))
    with pytest.raises(ValueError, match="markets overlap"):
        exam.load_plan(plan_path, exam.sha256(plan_path), inputs)
    plan["source_kind"] = "recorded"
    with pytest.raises(ValueError, match="bound feature producer"):
        exam.source_digests(plan, inputs, ((ORIGIN, ORIGIN + NS),))


def test_unavailable_future_witnesses_remain_missing():
    cutoff = 4 * NS
    row = {"ts_ns": 5 * NS, "market_id": "b", "book": None, "fair": {"p_up": .8, "tw_ns": None},
           "lead": {"p_up": .7, "tw_ns": cutoff + 2, "binance_tw_ns": cutoff - 1,
                    "hyperliquid_tw_ns": cutoff + 1}}
    shifted = exam.shift_future(row, cutoff)
    assert shifted["ts_ns"] == 7 * NS
    assert shifted["fair"] == row["fair"]
    assert shifted["lead"]["p_up"] == pytest.approx(.3)
    assert shifted["lead"]["binance_tw_ns"] == cutoff - 1
    assert shifted["lead"]["tw_ns"] == cutoff + 2 + 2 * NS
    assert row["lead"]["p_up"] == .7
