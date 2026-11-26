import gzip
import hashlib
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from doppel.tape.receipts import stream_hour_summary
from doppel.tape.segment import SegmentWriter

spec = importlib.util.spec_from_file_location("recover_brti_receipts", Path(__file__).resolve().parents[1] /
                                            "ops" / "recover_brti_receipts.py")
assert spec is not None and spec.loader is not None
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
START = int(datetime(2026, 11, 26, tzinfo=UTC).timestamp())


def put(w, i):
    sec = START + i
    observation = {"t_ms": sec * 1000, "v": f"{100 + i}.00", "received": sec + 0.25}
    w.append(json.dumps(observation), tw=sec * 10**9)
    w.flush()
    return {"kind": "brti", "received": observation["received"], "new": [[observation["t_ms"], observation["v"]]]}


@pytest.fixture
def archive(tmp_path):
    source = tmp_path / "source"
    writer = SegmentWriter(source, recovery.RAW)
    first = put(writer, 0)
    writer.close()
    history = source / recovery.RAW / "manifest.jsonl"
    previous = recovery.rows(history)
    path = source / recovery.RAW / previous[0]["file"]
    receipt = stream_hour_summary(path)
    receipt.pop("sha256", None)  # historical receipt only carries the counts/times/size
    put(writer, 1)
    writer.close()
    return path, previous, recovery.rows(history)[-1], receipt, first


@pytest.mark.parametrize("has_seal", [False, True])
def test_verified_prefix_carries_original_or_diagnostic_provenance(tmp_path, archive, has_seal):
    source, history, final, receipt, first = archive
    prefix = tmp_path / "prefix"
    original = source.read_bytes()
    row, case = recovery.recover_prefix(source, prefix, final, history if has_seal else [], receipt)
    assert row["sha256"] == hashlib.sha256(original[:receipt["bytes"]]).hexdigest()
    if has_seal:
        assert row == history[-1]
    else:
        assert row["evidence_use"] == "diagnostic_only"
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    name = "2026-11-26T00.jsonl.gz"
    with gzip.open(upstream / name, "wb") as out:
        out.write(json.dumps(first, separators=(",", ":")).encode() + b"\n")
    evidence = recovery.corroborate([(prefix, case)], upstream, tmp_path / "saved")
    assert evidence[name]["matched_publications"] == 1
    assert case["upstream_observations"] == receipt["data_records"] == 1
    assert source.read_bytes() == original


def test_prefix_rejects_false_summary_conflicting_seal_and_truncated_frame(tmp_path, archive):
    source, history, final, receipt, first = archive
    target = tmp_path / "prefix"
    with pytest.raises(ValueError, match="last_tw"):
        recovery.recover_prefix(source, target, final, history, receipt | {"last_tw": receipt["last_tw"] + 1})
    with pytest.raises(ValueError, match="conflicting"):
        recovery.recover_prefix(source, target, final, [history[0] | {"sha256": "c" * 64}], receipt)
    with pytest.raises(ValueError):
        recovery.recover_prefix(source, target, final, [], receipt | {"bytes": receipt["bytes"] - 1})
    with pytest.raises(ValueError, match="final archive seal"):
        recovery.recover_prefix(source, target, final | {"sha256": "e" * 64}, history, receipt)


def test_upstream_receive_time_participates_in_corroboration(tmp_path, archive):
    source, history, final, receipt, first = archive
    target = tmp_path / "prefix"
    _, case = recovery.recover_prefix(source, target, final, history, receipt)
    fake_source = first | {"received": first["received"] + 0.1}
    upstream = tmp_path / "up"
    upstream.mkdir()
    with gzip.open(upstream / "2026-11-26T00.jsonl.gz", "wb") as handle:
        handle.write(json.dumps(fake_source, separators=(",", ":")).encode() + b"\n")
    with pytest.raises(ValueError, match="upstream missing 1"):
        recovery.corroborate([(target, case)], upstream, tmp_path / "saved")


def test_diagnostic_generation_cannot_be_used_as_original_archive(tmp_path, archive):
    source, _, final, _, _ = archive
    manifest = source.parents[1] / "manifest.jsonl"
    manifest.write_text(json.dumps(final | {"evidence_use": "diagnostic_only"}) + "\n")
    with pytest.raises(ValueError, match="original archive generation"):
        recovery.inventory([source.parents[2]], recovery.utc_hour("20261126/01"))


def test_snapshot_publishes_restorable_diagnostic_view(tmp_path, archive, monkeypatch):
    source, history, final, receipt, first = archive
    base = source.parents[2]
    book_writer = SegmentWriter(base, "pmus_ws_5m")
    book_writer.append("{}", tw=START * 10**9)
    book_writer.close()
    book = base / "pmus_ws_5m" / final["file"]
    receipt_file = base / "receipts/20261126/00.json"
    receipt_file.parent.mkdir(parents=True)
    original = {"day": "20261126", "hour": 0, "built_tw": (START + 3600) * 10**9,
                "streams": {"pmus_ws_5m": stream_hour_summary(book), recovery.RAW: receipt}}
    receipt_file.write_text(json.dumps(original))
    historical = tmp_path / "later-manifest"
    historical.write_text(json.dumps(final) + "\n")  # existing source lacks the original prefix seal
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    with gzip.open(upstream / "2026-11-26T00.jsonl.gz", "wb") as handle:
        handle.write(json.dumps(first, separators=(",", ":")).encode() + b"\n")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 27, tzinfo=UTC)

    monkeypatch.setattr(recovery, "datetime", Clock)
    output = tmp_path / "output"
    start, end = (recovery.utc_hour(value) for value in ("20261126/00", "20261126/01"))
    doc = recovery.run([base], historical, upstream, output, start, end, 0)
    published = output / "recovery"
    try:
        assert not (output / "incomplete").exists()
        assert doc["diagnostic_only"] and doc["verify_hours"] == "passed"
        saved = published / "tape" / recovery.RAW / final["file"]
        assert saved.read_bytes() == source.read_bytes()[:receipt["bytes"]]
        assert not saved.stat().st_mode & 0o222
        assert receipt_file.read_bytes() == (published / "tape/receipts/20261126/00.json").read_bytes()
        with pytest.raises(FileExistsError):
            recovery.run([base], historical, upstream, output, start, end, 0)
    finally:
        for path in [published, *published.rglob("*")]:
            if path.is_dir():
                path.chmod(0o700)
