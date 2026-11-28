from pathlib import Path

import orjson
import pytest

from doppel.lab import taker_entries as entries
from doppel.lab.rulefit import main as rulefit_main

SECOND = 1_000_000_000


def tick(stamp, market, *, lead=0.5, fair=0.5):
    return {
        "ts_ns": stamp, "market_id": market,
        "book": {"tw_ns": stamp - 2, "up_bid": 0.46, "up_ask": 0.5, "down_bid": 0.46, "down_ask": 0.5},
        "fair": {"tw_ns": stamp - 1, "p_up": fair},
        "lead": {"tw_ns": stamp - 1, "p_up": lead, "binance_tw_ns": stamp - 2,
                 "hyperliquid_tw_ns": stamp - 3},
    }


def write(path: Path, rows):
    path.write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in rows))
    return path


def intent(row, side):
    return {"ts_ns": row["ts_ns"], "market_id": row["market_id"], "side": side}


def sample(windows, *, first_ns=20 * SECOND):
    rows, lead, fair, random = [], [], [], []
    for market in range(windows):
        for slot in range(20):
            # Weak moves distinguish the chosen threshold from smaller grid values.
            ln = {1: 0.520, 4 + market % 2: 0.529, 9: 0.479, 13 + market % 3: 0.471}
            fn = {2: 0.531, 7 + market % 2: 0.540, 11: 0.469, 16 + market % 3: 0.460}
            row = tick(first_ns + market * 30 * SECOND + slot * SECOND, f"m{market:02}",
                       lead=ln.get(slot, 0.5), fair=fn.get(slot, 0.5))
            rows.append(row)
            if slot in (4 + market % 2, 13 + market % 3):
                lead.append(intent(row, "BUY_UP" if slot < 8 else "BUY_DOWN"))
            if slot in (7 + market % 2, 16 + market % 3):
                fair.append(intent(row, "BUY_UP" if slot < 10 else "BUY_DOWN"))
            if slot in (3 + market % 2, 12 + market % 2):
                random.append(intent(row, "BUY_DOWN" if market % 2 else "BUY_UP"))
    return rows, {"lead": lead, "fair": fair, "random": random}


@pytest.mark.parametrize(("family", "threshold"), [("lead", 0.02), ("fair", 0.03), ("random", None)])
def test_train_family_and_replay_later_public_only(tmp_path, family, threshold):
    tape, labels = sample(12)
    public = write(tmp_path / "train.jsonl", tape)
    training = write(tmp_path / "train-intents.jsonl", labels[family])
    model = entries.fit(public, training)
    assert model["threshold"] == threshold
    assert model == entries.fit(public, training)

    holdout, truth = sample(3, first_ns=400 * SECOND)
    forecast = list(entries.predict(write(tmp_path / "later.jsonl", holdout), model))
    if family == "random":
        assert model["status"] == "ABSTAIN"
        assert forecast == []
    else:
        assert model["status"] == "FIT"
        assert model["trigger_source"] == family
        assert model["training_confirmation"]["precision"] == 1
        assert model["training_confirmation"]["recall"] == 1
        assert model["rotated_null_p"] <= 0.05
        assert [intent(r, r["side"]) for r in forecast] == truth[family]
        assert all(r["trigger_source"] == family and r["predicted_edge"] >= threshold for r in forecast)


def policy(source="lead"):
    return {"schema": entries.SCHEMA, "status": "FIT", "trigger_source": source, "threshold": 0.02,
            "training_range_ns": [1, 5 * SECOND]}


def test_strict_prior_staleness_crossings_and_future_suffix(tmp_path):
    moments = range(10, 20)
    rows = [tick(i * SECOND, "btc", lead=0.8 if i < 17 else 0.2) for i in moments]
    for idx, witness in ((0, "tw_ns"), (1, "binance_tw_ns"), (2, "hyperliquid_tw_ns")):
        rows[idx]["lead"][witness] = rows[idx]["ts_ns"]  # simultaneous is already too late
    rows[3]["book"]["tw_ns"] += 2  # simultaneous book
    rows[4]["lead"]["binance_tw_ns"] = rows[4]["ts_ns"] - 3 * SECOND - 1  # stale
    rows[5]["lead"]["tw_ns"] = rows[5]["ts_ns"] - 4  # published before either leg
    baseline = list(entries.predict(write(tmp_path / "base.jsonl", rows), policy()))
    assert [(r["ts_ns"] // SECOND, r["side"]) for r in baseline] == [(16, "BUY_UP"), (17, "BUY_DOWN")]

    for row in rows[7:]:
        row["lead"]["p_up"] = 0.5
        row["book"]["up_ask"] = 0.9
    changed = list(entries.predict(write(tmp_path / "perturbed.jsonl", rows), policy()))
    assert changed == baseline[:1]


def test_invalid_or_unverifiable_public_does_not_fire(tmp_path):
    rows = [tick(i * SECOND, str(i), fair=0.9) for i in range(6, 11)]
    rows[0]["fair"]["tw_ns"] = rows[0]["ts_ns"] + 1
    rows[1]["book"]["up_bid"] = 0.4  # over max fair spread
    rows[2]["book"]["down_bid"] = 0.6  # crossed outcome book
    rows[3]["fair"] = None
    rows[4]["fair"]["p_up"] = True
    assert list(entries.predict(write(tmp_path / "bad-book.jsonl", rows), policy("fair"))) == []
    with pytest.raises(ValueError, match="overlaps training"):
        list(entries.predict(write(tmp_path / "past.jsonl", [tick(5 * SECOND, "btc")]), policy()))


def test_training_labels_must_be_on_the_registered_grid(tmp_path):
    tape, labels = sample(12)
    public = write(tmp_path / "train.jsonl", tape)
    labels["lead"][0]["ts_ns"] += 1
    training = write(tmp_path / "off-grid.jsonl", labels["lead"])
    with pytest.raises(ValueError, match="not a registered public cutoff"):
        entries.fit(public, training)


def test_rulefit_entrypoint_keeps_failed_forecast_unpublished(tmp_path):
    model_path = tmp_path / "model.json"
    model_path.write_bytes(orjson.dumps(policy()))
    rows = [tick(10 * SECOND, "btc", lead=0.9), {"ts_ns": 11 * SECOND}]
    public = write(tmp_path / "bad-grid.jsonl", rows)
    out = tmp_path / "predicted_entries.jsonl"
    with pytest.raises(SystemExit) as error:
        rulefit_main(["--taker-entries", "predict", "--public", str(public),
                      "--model", str(model_path), "--out", str(out)])
    assert error.value.code == 2
    assert not out.exists()
