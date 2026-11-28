from datetime import datetime, timezone
from pathlib import Path

import orjson
import pytest

from doppel.lab.scan import scan_digest
from doppel.tape.segment import SegmentWriter
from ops import seal_cohort_v2 as freeze

NS = freeze.NS
HOUR = 3600 * NS


def write(path: Path, obj):
    path.write_bytes(orjson.dumps(obj, option=orjson.OPT_SORT_KEYS))
    return path


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    tape = tmp_path / "tape"
    inputs = {"rtds": {}, "clob": {}}
    hour = freeze.CUTOFF - HOUR
    dayhour = datetime.fromtimestamp(hour / NS, timezone.utc).strftime("%Y%m%d/%H")
    for kind, stream in (("rtds", "rtds_a"), ("clob", "clob_btc_5m")):
        writer = SegmentWriter(tape, stream)
        writer.append("{}", tw=hour + 1)
        writer.close()
        path = f"{stream}/{dayhour}.dtape.zst"
        inputs[kind][path] = freeze.file_sha(tape / path)
    tokens = write(tmp_path / "tokens.json", {"known": "earlier"})
    v1 = write(tmp_path / "v1.json", {"frozen": True})
    inputs["tokens"] = freeze.file_sha(tokens)
    inputs["cohort_v1"] = freeze.file_sha(v1)
    monkeypatch.setattr(freeze, "TOKENS_SHA256", inputs["tokens"])
    monkeypatch.setattr(freeze, "V1_SHA256", inputs["cohort_v1"])
    sniper, donor = "0x" + "a" * 40, "0x" + "b" * 40
    stats = {"schema": "doppel-cohort-v2/1/stats", "frozen_tw": freeze.CUTOFF,
             "wallets": {sniper: {"slice": "sniper"}, donor: {"slice": "donor"}}}
    doc = {"schema": "doppel-cohort-v2/1", "frozen_tw": freeze.CUTOFF,
           "period_a": {"from_tw": hour - HOUR, "split_tw": hour,
                        "until_tw": freeze.CUTOFF},
           "stats_sha256": scan_digest(stats), "code_sha256": freeze.BUILDER_SHA256,
           "inputs": inputs, "sniper": [sniper], "donor": [donor], "controls": [],
           "eligible": {}, "shortfall": {}, "null": {}}
    return tape, doc, write(tmp_path / "stats.json", stats), tokens, v1


def run(tmp_path, frozen, name):
    tape, doc, stats, tokens, v1 = frozen
    full = doc | {"sha256": scan_digest(doc)}
    manifest = write(tmp_path / "cohort.json", full)
    return freeze.seal(tape, manifest, stats, tokens, v1, tmp_path / name)


def test_cohort_exports_exact_cutoff_and_is_repeatable(tmp_path, frozen):
    first = run(tmp_path, frozen, "one")
    second = run(tmp_path, frozen, "two")
    assert first == second
    assert first["cutoff_ns_exclusive"] == freeze.CUTOFF
    assert first["inputs"]["records"] == 2
    assert first["inputs"]["last_native_receive_ns"] == freeze.CUTOFF - HOUR + 1
    for filename, entry in first["slices"].items():
        assert freeze.file_sha(tmp_path / "one" / filename) == entry["sha256"]
        assert (tmp_path / "one" / filename).read_bytes() == (tmp_path / "two" / filename).read_bytes()
    with pytest.raises(FileExistsError):
        run(tmp_path, frozen, "one")


def test_post_cutoff_segment_and_wrong_v1_fail_before_export(tmp_path, frozen):
    _, doc, _, _, v1 = frozen
    v1.write_text("{}")
    with pytest.raises(ValueError, match="pinned bytes"):
        run(tmp_path, frozen, "v1-fail")
    v1.write_bytes(b'{"frozen":true}')
    doc["inputs"]["rtds"]["rtds_a/20261128/00.dtape.zst"] = "1" * 64
    with pytest.raises(ValueError, match="registered freeze"):
        run(tmp_path, frozen, "date-fail")
    assert not (tmp_path / "v1-fail").exists()
    assert not (tmp_path / "date-fail").exists()
