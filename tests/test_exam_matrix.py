import hashlib

import orjson
import pytest

from doppel.lab import answerkey, exam_matrix as matrix
from test_answerkey import T0, synth

COMMIT = "1" * 40


def test_frozen_matrix_scope_and_simulation_only_decoy_denominator(tmp_path):
    assert matrix._preflight()["holdout"]["book_pairs"] == 36
    data = tmp_path / "source"
    data.mkdir()
    synth(data, minutes=15, rate_hz=2)
    tokens = data / "tokens.jsonl"
    end, split = T0 + 900, T0 + 600
    midpoint = answerkey.run_strategy("a", data, tokens, matrix.STREAM, T0, split, end,
                                      tmp_path / "midpoint", matrix.SEEDS[0], tools=False)
    assert answerkey.midpoint_training_orders(data, tokens, matrix.STREAM, T0, split, end, matrix.SEEDS[0]) == (
        midpoint["train_visible_maker_orders"]
    )


def _objects(monkeypatch):
    objects, seals, manifest_pins = {}, {}, {}
    for stream in matrix.STREAMS:
        rows = []
        for hour in matrix.HOURS:
            data = f"{stream} {hour}".encode()
            digest = hashlib.sha256(data).hexdigest()
            rows.append(orjson.dumps({"file": hour, "bytes": len(data), "sha256": digest}))
            objects[f"{matrix.PREFIX}/{stream}/{hour}"] = data
            if stream == matrix.STREAM:
                seals[hour] = digest
        raw = b"\n".join(rows) + b"\n"
        objects[f"{matrix.PREFIX}/{stream}/manifest.jsonl"] = raw
        if stream in matrix.REFERENCE_STREAMS:
            manifest_pins[stream] = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(matrix, "_preflight", lambda: {"segment_sha256": seals})
    return objects, manifest_pins


def test_r2_fetch_does_not_fetch_other_hours_and_requires_checksums(tmp_path, monkeypatch):
    objects, manifest_pins = _objects(monkeypatch)
    fetched = []

    def get(key, destination):
        fetched.append(key)
        destination.write_bytes(objects[key])

    data = tmp_path / "data"
    receipt = matrix.fetch_inputs(data, manifest_pins, get)
    assert len(receipt["segments"]) == 18
    assert len(fetched) == 21 and set(fetched) == set(objects)
    assert matrix._validate_inputs(data, matrix._preflight()) == matrix._digest(data / matrix.INPUT_RECEIPT)
    key = f"{matrix.REFERENCE_STREAMS[0]}/{matrix.HOURS[0]}"
    objects[f"{matrix.PREFIX}/{key}"] = b"corrupt"
    blocked = tmp_path / "blocked"
    with pytest.raises(ValueError, match="differs from the pinned seal"):
        matrix.fetch_inputs(blocked, manifest_pins, get)
    assert not blocked.exists()
    manifest_pins[matrix.REFERENCE_STREAMS[0]] = "0" * 64
    with pytest.raises(ValueError, match="manifest SHA-256"):
        matrix.fetch_inputs(blocked, manifest_pins, get)
    assert not blocked.exists()
    (data / key).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="downloaded exam segment changed"):
        matrix._validate_inputs(data, matrix._preflight())


def _score(trader, midpoint=17, *, abstains=True):
    cells = {name: answerkey._cell(None, None, "unknown", None) for name in answerkey.OBLIGATIONS}
    cells["fidelity_verdict"]["state"] = "not_run"
    cells["explained_by_observed_inputs"].update(
        genome_emitted=not abstains, recovered=not abstains, fidelity_state="not_run",
    )
    return {"policy": answerkey.POLICIES[trader].name, "params": {
                "rate_per_s": midpoint / (matrix.RANGE[1] - matrix.RANGE[0]),
            } if trader == "e" else {},
            "cells": cells, "tools": {}, "pipeline": {}, "fidelity_reason": None,
            "train_visible_maker_orders": midpoint if trader == "a" else 0,
            "decoy_reference_maker_orders": midpoint if trader == "e" else None}


def _input_receipt(folder, manifest_pin="2" * 64):
    path = folder / matrix.INPUT_RECEIPT
    matrix._write(path, {"schema": matrix.INPUT_SCHEMA, "prefix": matrix.PREFIX, "range": list(matrix.RANGE),
                         "preflight_sha256": matrix.PREFLIGHT_SHA, "tokens_sha256": matrix.TOKENS_SHA,
                         "manifests": {stream: manifest_pin for stream in matrix.STREAMS}})
    return matrix._digest(path)


def _receipt(folder, seed_slot, trader_slot, *, score=None, manifest_pin="2" * 64):
    folder.mkdir()
    seed, trader = matrix._coordinate(seed_slot, trader_slot)
    score_path = folder / matrix.SCORE
    matrix._write(score_path, score if score is not None else _score(trader))
    receipt = {"schema": matrix.RECEIPT_SCHEMA, "source_scope": "matrix_replay_comparison", "commit": COMMIT,
               "seed_slot": seed_slot, "trader_slot": trader_slot, "seed": seed, "trader": trader,
               "range": list(matrix.RANGE), "stream": matrix.STREAM, "policy": answerkey.POLICIES[trader].name,
               "tokens_sha256": matrix.TOKENS_SHA, "policy_params_sha256": matrix.PARAMS_SHA,
               "slice_s": 600, "input_sha256": _input_receipt(folder, manifest_pin),
               "score_sha256": matrix._digest(score_path)}
    matrix._write(folder / matrix.RECEIPT, receipt)


def test_aggregate_is_complete_only_with_all_receipts_and_keeps_unmeasured_gates(tmp_path):
    for seed_slot in reversed(range(1, 4)):
        for trader_slot in reversed(range(6)):
            if (seed_slot, trader_slot) != (2, 4):
                _receipt(tmp_path / f"{seed_slot}-{trader_slot}", seed_slot, trader_slot)
    partial = matrix.aggregate(tmp_path, COMMIT)
    assert partial["receipts"] == 17 and not partial["complete"]
    assert [row["trader"] for row in partial["subjects"] if row["status"] == "MISSING"] == ["e"]
    assert partial["preregistered_gates"]["negative_controls"]["status"] == "UNMEASURED"
    _receipt(tmp_path / "2-4", 2, 4, score=_score("e", abstains=False))
    summary = matrix.aggregate(tmp_path, COMMIT)
    assert summary["complete"] and summary["receipts"] == 18
    assert [(r["seed"], r["trader"]) for r in summary["subjects"]] == [
        (seed, trader) for seed in matrix.SEEDS for trader in matrix.TRADERS
    ]
    gates = summary["preregistered_gates"]
    assert gates["negative_controls"]["status"] == "FAIL"
    assert gates["negative_controls"]["abstained"] == 5
    assert all(gates[name]["status"] == "UNMEASURED" for name in (
        "family_precision", "recoverable_strategies", "taker_timing",
    ))
    assert gates["taker_timing"]["evaluated_timing_pairs"] == 0
    assert "18/18" in matrix.markdown(summary)


@pytest.mark.parametrize("fault, error", [
    ("duplicate", "duplicate subject receipt"),
    ("scope", "different sealed inputs"),
    ("tampered", "SHA-256 differs"),
    ("decoy", "decoy normalization differs"),
])
def test_aggregate_refuses_replaced_or_incompatible_subject_evidence(tmp_path, fault, error):
    one = tmp_path / "one"
    _receipt(one, 1, 0)
    two = tmp_path / "two"
    if fault == "duplicate":
        _receipt(two, 1, 0)
    elif fault == "scope":
        _receipt(two, 1, 1, manifest_pin="3" * 64)
    elif fault == "tampered":
        (one / matrix.SCORE).write_bytes(b"{}")
    else:
        _receipt(two, 1, 4, score=_score("e", midpoint=18))
    with pytest.raises(ValueError, match=error):
        matrix.aggregate(tmp_path, COMMIT)


def test_matrix_decoy_repasses_exact_serial_rate_to_single_subject(tmp_path, monkeypatch):
    input_sha = _input_receipt(tmp_path)
    monkeypatch.setattr(matrix, "_validate_inputs", lambda *_: input_sha)
    monkeypatch.setattr(answerkey, "midpoint_training_orders", lambda *_: 23)
    tool = tmp_path / "doppel-tape"
    tool.touch()

    def run(trader, _data, _tokens, _stream, _start, _split, _end, _out, seed, params, *, slice_s, materializer):
        assert (trader, seed, slice_s, materializer) == ("e", matrix.SEEDS[2], 600, tool.resolve())
        assert params["rate_per_s"] == 23 / (matrix.RANGE[1] - matrix.RANGE[0])
        return _score("e", midpoint=23)

    monkeypatch.setattr(answerkey, "run_strategy", run)
    receipt_dir = tmp_path / "receipt"
    receipt = matrix.run_subject(tmp_path, tmp_path / "out", receipt_dir, tool, 3, 4, COMMIT)
    assert receipt["score_sha256"] == matrix._digest(receipt_dir / matrix.SCORE)
    assert matrix._read(receipt_dir / matrix.SCORE)["decoy_reference_maker_orders"] == 23
