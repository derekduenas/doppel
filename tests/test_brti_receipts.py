"""Producer and recorder interleavings seen on the Nov 26 reference tape."""

import gzip
import hashlib
from concurrent.futures import ThreadPoolExecutor

import orjson
import pytest

from doppel.lab import brti_import
from doppel.tape.receipts import ReceiptPending, build_receipt, finish_receipts
from doppel.tape.segment import SegmentWriter, read_segment, segment_path


START = 1795622400  # 2026-11-25 16:00 UTC


def publish(src, *seconds):
    src.mkdir(exist_ok=True)
    with (src / "w.jsonl").open("ab") as fh:
        for sec in seconds:
            row = {"kind": "brti", "received": sec + 0.5, "new": [[sec * 1000, "10"]]}
            fh.write(orjson.dumps(row) + b"\n")


def test_import_rollover_waits_for_last_batch_and_receipt_is_immutable(tmp_path):
    src, tape, state = tmp_path / "upstream", tmp_path / "tape", tmp_path / "state"
    publish(src, START)
    brti_import.run(src, tape, state)
    with pytest.raises(ReceiptPending):
        build_receipt(tape, "20261125", 16)
    # The raw writer can still append even though the recorder has advanced to 17 UTC.
    finish_receipts(tape, START, True)
    finish_receipts(tape, START + 3600, True)
    receipt = tape / "receipts/20261125/16.json"
    assert not receipt.exists()
    publish(src, START + 3599, START + 3600)
    brti_import.run(src, tape, state)
    finish_receipts(tape, START + 3600, True)  # restarted recorder reads its cursor
    raw = segment_path(tape, "ref_brti_raw", START * 10**9)
    doc = orjson.loads(receipt.read_bytes())
    entry = doc["streams"]["ref_brti_raw"]
    assert doc["schema_version"] == 2 and doc["missing_expected_streams"] == []
    assert entry["records"] == 2 and entry["bytes"] == raw.stat().st_size
    assert entry["sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert [orjson.loads(r["raw"])["received"] for r in read_segment(raw)] == [START + 0.5, START + 3599.5]
    publish(src, START + 3610)
    brti_import.run(src, tape, state)
    assert build_receipt(tape, "20261125", 16) == doc
    assert entry["sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()


@pytest.mark.parametrize("fail_at", ["manifest.jsonl", "state", "import-watermark.json"])
def test_interrupted_import_finishes_once_on_retry(tmp_path, monkeypatch, fail_at):
    src, tape, state = tmp_path / "upstream", tmp_path / "tape", tmp_path / "state"
    publish(src, START, START + 3599)
    brti_import.run(src, tape, state)
    publish(src, START + 3600, START + 7200)
    original = brti_import.atomic_write

    def fail(path, data):
        if path.name == fail_at:
            raise OSError("interrupted")
        original(path, data)

    with monkeypatch.context() as patch:
        patch.setattr(brti_import, "atomic_write", fail)
        with pytest.raises(OSError, match="interrupted"):
            brti_import.run(src, tape, state)
        with pytest.raises(ReceiptPending):
            build_receipt(tape, "20261125", 16)
    assert brti_import.run(src, tape, state)["added"] == 0
    assert orjson.loads(state.read_bytes())["max_t"] == (START + 7200) * 1000
    segs = sorted((tape / "ref_brti_raw").glob("*/*.dtape.zst"))
    seqs = [r["seq"] for s in segs for r in read_segment(s, strict=True)]
    assert seqs == [1, 2, 3, 4]
    seals = [orjson.loads(line) for line in (tape / "ref_brti_raw/manifest.jsonl").read_bytes().splitlines()]
    assert [m["last_seq"] for m in seals] == [2, 3, 4]
    assert build_receipt(tape, "20261125", 16)["streams"]["ref_brti_raw"]["records"] == 2


def test_expected_import_gap_and_unmanifested_segment(tmp_path):
    src, root, state = tmp_path / "source", tmp_path / "tape", tmp_path / "state"
    root.mkdir()
    with pytest.raises(ReceiptPending):
        build_receipt(root, "20261125", 16, require_brti_import=True)
    publish(src, START + 3600)
    brti_import.run(src, root, state)
    w = SegmentWriter(root, "pmus_ws")
    w.append("a", tw=START * 10**9)
    w.flush()
    with pytest.raises(ReceiptPending, match="no matching"):
        build_receipt(root, "20261125", 16)
    w.close()
    doc = build_receipt(root, "20261125", 16)
    assert doc["missing_expected_streams"] == ["ref_brti_raw"]
    assert doc["streams"]["pmus_ws"]["crc_ok"] is True


def test_late_new_seconds_are_preserved_without_mutating_a_sealed_hour(tmp_path):
    src, root, state = tmp_path / "source", tmp_path / "tape", tmp_path / "state"
    publish(src, START, START + 3600)
    brti_import.run(src, root, state)
    doc = build_receipt(root, "20261125", 16)
    publish(src, START + 2)
    assert brti_import.run(src, root, state)["amendments"] == 1
    assert brti_import.run(src, root, state)["amendments"] == 0
    row = orjson.loads((root / "ref_brti_raw/import-amendments.jsonl").read_bytes())
    assert (row["t_ms"], row["received"]) == ((START + 2) * 1000, START + 2.5)
    assert doc == build_receipt(root, "20261125", 16)
    p = segment_path(root, "ref_brti_raw", START * 10**9)
    assert hashlib.sha256(p.read_bytes()).hexdigest() == doc["streams"]["ref_brti_raw"]["sha256"]


def test_malformed_closed_source_does_not_advance_cursor_or_watermark(tmp_path):
    src, root, state = tmp_path / "source", tmp_path / "tape", tmp_path / "state"
    publish(src, START)
    brti_import.run(src, root, state)
    cursor = state.read_bytes()
    publish(src, START + 3600)
    bad = src / "z.jsonl.gz"
    with gzip.open(bad, "wb") as fh:
        fh.write(b'{"kind":"brti",damaged\n')
    with pytest.raises(ValueError, match="malformed upstream"):
        brti_import.run(src, root, state)
    assert state.read_bytes() == cursor
    assert not segment_path(root, "ref_brti_raw", (START + 3600) * 10**9).exists()
    with pytest.raises(ReceiptPending):
        build_receipt(root, "20261125", 16)


def test_incomplete_last_open_line_waits_for_its_newline(tmp_path):
    src, root, state = tmp_path / "source", tmp_path / "tape", tmp_path / "state"
    publish(src, START)
    p = src / "w.jsonl"
    p.write_bytes(p.read_bytes().rstrip(b"\n"))
    assert brti_import.run(src, root, state)["added"] == 0
    with p.open("ab") as fh:
        fh.write(b"\n")
    assert brti_import.run(src, root, state)["added"] == 1


def test_concurrent_imports_publish_one_generation(tmp_path):
    src, root, state = tmp_path / "source", tmp_path / "tape", tmp_path / "state"
    publish(src, START, START + 3600)
    with ThreadPoolExecutor(max_workers=2) as pool:
        imports = [pool.submit(brti_import.run, src, root, state) for _ in range(2)]
        assert sorted(job.result()["added"] for job in imports) == [0, 2]
        receipts = [pool.submit(build_receipt, root, "20261125", 16) for _ in range(2)]
        assert receipts[0].result() == receipts[1].result()
    assert len((root / "ref_brti_raw/manifest.jsonl").read_bytes().splitlines()) == 2


def test_failed_prepared_revision_keeps_previous_published_bytes(tmp_path, monkeypatch):
    src, root, state = tmp_path / "source", tmp_path / "tape", tmp_path / "state"
    publish(src, START)
    brti_import.run(src, root, state)
    path = segment_path(root, "ref_brti_raw", START * 10**9)
    before = path.read_bytes()
    publish(src, START + 1)
    with monkeypatch.context() as patch:
        def stop(*args):
            raise OSError("power cut before publishing")
        patch.setattr(brti_import, "_publish_import", stop)
        with pytest.raises(OSError, match="power cut"):
            brti_import.run(src, root, state)
    staged = path.parents[1] / ".import-work" / path.relative_to(root)
    staged.write_bytes(b"damaged prepared bytes")
    with pytest.raises(ValueError, match="prepared BRTI segment checksum differs"):
        brti_import.run(src, root, state)
    assert path.read_bytes() == before
