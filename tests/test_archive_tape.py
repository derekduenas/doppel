import hashlib
import json
from datetime import UTC, datetime

from ops import archive_tape as arc


def seal(root, hour, data):
    name = f"20261126/{hour:02d}.dtape.zst"
    path = root / "ref_brti_raw" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    row = {"file": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    with (path.parent.parent / "manifest.jsonl").open("a") as out:
        out.write(json.dumps(row) + "\n")
    return row


def receipt(root, hour, row, **changes):
    path = root / "receipts" / "20261126" / f"{hour:02d}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"day": "20261126", "hour": hour, "streams": {
        "ref_brti_raw": dict(row, crc_ok=True) | changes}}))


def copy(src, dst, start=None):
    dst.mkdir(exist_ok=True)
    return arc.archive_locked(src, dst, ["ref_brti_raw"], datetime(2026, 11, 26, 4, tzinfo=UTC),
                              0, start=start)


def test_copy_receipted_revision_and_history_and_resume(tmp_path):
    src, dst = tmp_path / "source", tmp_path / "dest"
    first = seal(src, 1, b"initial")
    receipt(src, 1, first)
    last = seal(src, 1, b"initial and later")
    seal(src, 0, b"previous")
    start = datetime(2026, 11, 26, 1, tzinfo=UTC)
    for _ in range(2):
        out = copy(src, dst, start)
        assert not out["missing"] and not out["receipt_issues"]
        assert (dst / "ref_brti_raw" / first["file"]).read_bytes() == b"initial"
        assert not (dst / "ref_brti_raw/20261126/00.dtape.zst").exists()
        history = (dst / "ref_brti_raw/source-manifest-history.jsonl").read_bytes().splitlines()
        assert [json.loads(line) for line in history] == [first, last]
        assert arc.manifest(dst / "ref_brti_raw/manifest.jsonl") == {first["file"]: first}


def test_unmatched_legacy_receipt_preserves_final_bytes_and_flags_state(tmp_path):
    src, dst = tmp_path / "source", tmp_path / "dest"
    last = seal(src, 1, b"several observations")
    receipt(src, 1, {"bytes": 10})
    out = copy(src, dst)
    assert out["receipt_issues"]
    assert (dst / "ref_brti_raw" / last["file"]).read_bytes() == b"several observations"


def test_missing_or_invented_generation_is_reported(tmp_path):
    src, dst = tmp_path / "source", tmp_path / "dest"
    original = seal(src, 0, b"observations")
    receipt(src, 1, original)
    receipt(src, 0, original, sha256="0"*64)
    out = copy(src, dst)
    assert len(out["receipt_issues"]) == 2


def test_requested_cutoff_does_not_hide_lost_archive_files(tmp_path):
    src, dst = tmp_path / "source", tmp_path / "dest"
    first = seal(src, 1, b"first")
    copy(src, dst)
    (dst / "ref_brti_raw" / first["file"]).unlink()
    (src / "ref_brti_raw/manifest.jsonl").write_bytes(b"")
    (src / "ref_brti_raw" / first["file"]).unlink()
    seal(src, 2, b"second")
    out = copy(src, dst)
    assert out["missing"] == ["ref_brti_raw/" + first["file"]]


def test_archive_primary_interval_requires_all_receipts_and_checksum(tmp_path):
    src, dst = tmp_path / "source", tmp_path / "dest"
    dst.mkdir()
    row = seal(src, 1, b"observations")
    receipt(src, 1, {"bytes": row["bytes"]})
    path = src / "receipts/20261126/01.json"
    path.write_text(json.dumps(json.loads(path.read_bytes()) | {"schema_version": 2}))
    out = arc.archive_locked(src, dst, ["ref_brti_raw"], datetime(2026, 11, 26, 3, tzinfo=UTC),
                             reserve=0, start=datetime(2026, 11, 26, 1, tzinfo=UTC), require_receipts=True)
    assert set(out["receipt_issues"]) == {"ref_brti_raw/" + row["file"], "receipts/20261126/02.json: missing"}


def test_historical_archive_hashes_exact_closed_bytes(tmp_path):
    import os

    from ops import cold_tape as cold

    root, state = tmp_path / "legacy", tmp_path / "state"
    relative = "20261126/01.jsonl.gz"
    src = root / relative
    src.parent.mkdir(parents=True)
    src.write_bytes(b"exact closed legacy bytes")
    os.utime(src, (0, 0))

    class Store:
        remote = "b2:research/perelman"
        objects = {}

        def ensure(self, key, payload, size, digest):
            raw = payload if isinstance(payload, bytes) else payload.read()
            assert size == len(raw) and digest == cold.sha(raw)
            self.objects[key] = raw

    target = Store()
    now = datetime(2026, 11, 27, 12, tzinfo=UTC)
    args = (root, target, state, ["auto"], None, now)
    preview = cold.offload(*args, dry_run=True, historical=[relative])
    assert preview["pending"] == 1 and not target.objects and not state.exists()
    assert cold.offload(*args, historical=[relative])["archived"] == 1
    stored = next(data for key, data in target.objects.items() if key.startswith("data/"))
    assert stored == src.read_bytes()
    proof = json.loads(next(state.glob("*.json")).read_bytes())
    assert not proof["prune"] and proof["identity"]["kind"] == "historical"


def test_named_bucket_upload_requires_streamed_hash_even_if_length_matches(monkeypatch):
    import subprocess

    import pytest

    from ops import cold_tape as cold

    # The S3 stat response for a missing object is a directory placeholder.
    stored = {}

    def call(args, **kwargs):
        subcmd = args[1]
        key = args[3] if subcmd == "lsjson" else args[2]
        if subcmd == "lsjson":
            row = {"IsDir": key not in stored, "Size": len(stored.get(key, b""))}
            return subprocess.CompletedProcess(args, 0, cold.json_bytes(row))
        assert subcmd == "rcat" and key not in stored
        stored[key] = kwargs["input"]
        return subprocess.CompletedProcess(args, 0)

    class Cat:
        def __init__(self, args, **kwargs):
            import io

            assert args[1] == "cat"
            self.stdout = io.BytesIO(stored[args[2]])

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.stdout.close()

        def wait(self):
            return 0

    monkeypatch.setattr(cold.subprocess, "run", call)
    monkeypatch.setattr(cold.subprocess, "Popen", Cat)
    target = cold.ObjectStore("r2:research/apex")
    target.ensure("first", b"aa", 2, cold.sha(b"aa"))
    assert stored["r2:research/apex/first"] == b"aa"
    stored["r2:research/apex/second"] = b"bb"
    with pytest.raises(ValueError, match="SHA-256 verification failed"):
        target.ensure("second", b"aa", 2, cold.sha(b"aa"))
    assert stored["r2:research/apex/second"] == b"bb"
