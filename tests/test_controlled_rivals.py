"""The registered control runs exogenous market receipts and private decisions through separate paths."""
import importlib.util
from dataclasses import replace
from pathlib import Path
import sys

import orjson
import pytest

PATH = Path(__file__).resolve().parents[1] / "evidence/2026-11-27-wallet-rivals/controlled.py"
spec = importlib.util.spec_from_file_location("controlled_rivals_fixture", PATH)
assert spec and spec.loader
controls = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = controls
spec.loader.exec_module(controls)


def test_public_panel_uses_the_prior_snapshot_and_keeps_every_market_and_cut():
    split = replace(controls.SPLITS["dev"], markets=2)
    panel = orjson.loads(controls.public_bytes(split))
    assert len(panel["cells"]) == 60
    assert "subjects" not in panel
    for cell, tick in zip(panel["cells"], controls.market_ticks(split), strict=True):
        assert cell["cutoff_s"] == tick.second
        assert cell["capture"] == ("gap" if tick.slot == 29 else "covered")
        if tick.slot == 0:
            assert cell["book"] is None
            continue
        book = cell["book"]
        assert book["depth_tw_ns"] == tick.second * controls.NS - controls.NS // 2
        assert book["identity_tw_ns"] < tick.second * controls.NS
        assert book["bid"] == tick.bid_cents / 100 and book["bid_shares"] == tick.bid_size
    assert sum(c["book"]["bid_shares"] == 1600 for c in panel["cells"] if c["book"] and c["capture"] == "covered") == 2
    assert controls.public_bytes(split) == controls.public_bytes(replace(split, private_seed=123))


def test_controls_record_complete_private_journals_and_bind_predictions_as_opaque_bytes(tmp_path):
    split = replace(controls.SPLITS["dev"], markets=1)
    public, predictions = tmp_path / "public.json", tmp_path / "predictions.json"
    public.write_bytes(controls.public_bytes(split))
    outcomes = []
    journals = []
    for i in (0, 1):
        # Hash binding is the only dependency on the prediction file.
        predictions.write_bytes(str(i).encode())
        output, journal = tmp_path / f"labels-{i}", tmp_path / f"actions-{i}"
        controls.labels(split, public, predictions, journal, output)
        row = orjson.loads(output.read_bytes())
        assert row["predictions_sha256"] == controls.digest(predictions)
        assert row["public_sha256"] == controls.digest(public)
        outcomes.append(row["subjects"])
        journals.append(journal.read_bytes())
    assert journals[0] == journals[1] and outcomes[0] == outcomes[1]
    actions = [orjson.loads(line) for line in journals[0].splitlines()]
    assert len(actions) == 90
    for tick, triple in zip(controls.market_ticks(split), zip(actions[::3], actions[1::3], actions[2::3], strict=True), strict=True):
        for wallet, action in zip(controls.SUBJECTS, triple, strict=True):
            assert action["wallet"] == wallet and action["cutoff_s"] == tick.second
            if action["order"]:
                assert action["order"] == {"side":"BUY","limit_cents":tick.bid_cents,"shares":60,"token":tick.token}
        assert triple[0]["order"] and (triple[1]["order"] is not None) == (tick.bid_size <= 1600)
    public.write_bytes(public.read_bytes() + b" ")
    with pytest.raises(ValueError, match="registered source"):
        controls.labels(split, public, predictions, tmp_path / "bad-journal", tmp_path / "bad-labels")
    assert not (tmp_path / "bad-journal").exists() and not (tmp_path / "bad-labels").exists()
