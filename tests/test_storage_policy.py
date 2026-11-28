"""ops/storage.py: policy parsing, eligibility, R2 comparison and unclassified detection."""

import gzip
import os
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

from ops import cold_tape as cold
from ops import storage

ROOT = Path(__file__).resolve().parents[1]


def test_policy_parses_and_every_class_is_complete():
    pol = tomllib.loads((ROOT / "ops" / "storage-policy.toml").read_text())
    names = [c["name"] for c in pol["class"]]
    assert len(names) == len(set(names))
    for c in pol["class"]:
        assert {"local", "r2", "include", "min_age", "local_days", "why"} <= c.keys()
        storage.seconds(c["min_age"])
        assert not c["r2"].startswith("/")
    secrets = set(pol["never"]["paths"])
    assert not secrets & {c["local"] for c in pol["class"]}
    assert "/etc/doppel" in secrets
    delegated = {c["name"] for c in pol["class"] if storage.archive_owns(c)}
    assert delegated == {"tape", "tape-rust"}


def test_copy_leaves_native_payload_classes_to_archive_writer(tmp_path, monkeypatch):
    common = {"local": str(tmp_path), "r2": "apex/tape", "min_age": "2h"}
    tape = {**common, "name": "tape", "uploader": "archive_tape", "include": ["**/*.dtape.zst"]}
    activity = {**common, "name": "tape-goldsky-activity", "include": ["goldsky_activity/*.jsonl"]}
    policy = {"class": [tape, activity], "r2": {"remote": "r2:bucket", "bwlimit": "25M"},
              "do_only": {"skip_names": []}}
    calls = []

    def run(_, *args):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(storage, "rclone", run)
    assert storage.cmd_copy(policy, "tape") == 1 and calls == []
    assert storage.cmd_copy(policy, None) == 0
    assert len(calls) == 1 and calls[0][-2:] == ("--include", "goldsky_activity/*.jsonl")


def test_eligibility_respects_includes_skips_and_min_age(tmp_path):
    (tmp_path / "s" / "20261127").mkdir(parents=True)
    old, new = tmp_path / "s/20261127/01.dtape.zst", tmp_path / "s/20261127/02.dtape.zst"
    old.write_bytes(b"x" * 5); new.write_bytes(b"y" * 3)
    (tmp_path / "status.json").write_text("{}")
    (tmp_path / "s/20261127/notes.txt").write_text("n")
    t = time.time()
    os.utime(old, (t - 3 * 3600, t - 3 * 3600))
    cls = {"local": str(tmp_path), "include": ["**/*.dtape.zst"], "min_age": "2h"}
    assert storage.local_files(cls, ["status.json"], t) == {"s/20261127/01.dtape.zst": 5}


def test_compare_flags_missing_and_size_mismatch_only_after_grace():
    now = 10_000.0
    local = {"a": 5, "b": 7, "c": 9}
    remote = {"a": 5, "b": 6}
    mt = {"a": 0.0, "b": 0.0, "c": now - 10}
    r = storage.compare(local, remote, mt, now, grace=100)
    assert r["verified_paths"] == ["a"]
    assert r["missing"] == ["c"] and r["size_mismatch"] == ["b"]
    assert r["overdue"] == ["b"]          # c is missing but still inside its grace


def test_unclassified_top_level_entries_are_reported(tmp_path):
    for d in ("tape", "lab", "mystery"):
        (tmp_path / d).mkdir()
    (tmp_path / "status.json").write_text("{}")
    pol = {"class": [{"local": str(tmp_path / "tape")}, {"local": str(tmp_path / "lab")}],
           "do_only": {"paths": [], "skip_names": ["status.json"]}, "never": {"paths": []},
           "audit": {"classified_roots": [str(tmp_path)]}}
    assert storage.unclassified(pol) == [str(tmp_path / "mystery")]


def test_exact_verification_rejects_missing_seals_and_equal_size_corruption(tmp_path, monkeypatch):
    cls = {"name": "tape", "local": str(tmp_path), "r2": "apex/tape", "include": ["**/*.dtape.zst"],
           "min_age": "2h", "local_days": 14}
    policy = {"class": [cls], "r2": {"remote": "r2:bucket"}, "do_only": {"skip_names": []}}
    root = tmp_path / "spot_l2_kraken"
    root.joinpath("20000101").mkdir(parents=True)
    verified = root / "20000101/00.dtape.zst"
    unsealed = root / "20000101/01.dtape.zst"
    for f in (verified, unsealed):
        f.write_bytes(b"ABC")
        os.utime(f, (0, 0))
    (root / "manifest.jsonl").write_bytes(cold.json_bytes({
        "file": "20000101/00.dtape.zst", "bytes": 3, "sha256": cold.sha(b"ABC"),
    }))
    key = verified.relative_to(tmp_path).as_posix()
    inventory = [key, unsealed.relative_to(tmp_path).as_posix()]
    calls = []

    def remote(policy, cls, name, size):
        calls.append((storage.remote_key(policy, cls, name), size))
        return cold.sha(b"XYZ")

    monkeypatch.setattr(storage, "remote_digest", remote)
    preview = storage.summarize_verify(policy, cls, dry_run=True, inventory=inventory)
    assert not calls and preview["objects"][key]["remote_verified"] is False
    assert preview["issues"] == [f"missing native segment seal: {inventory[1]}"]
    result = storage.summarize_verify(policy, cls, dry_run=False, inventory=[key])
    assert calls == [(f"r2:bucket/apex/tape/{key}", 3)]
    assert not result["objects"] and result["issues"] == [f"remote SHA-256 mismatch: {key}"]
    monkeypatch.setattr(storage, "remote_digest", lambda *_: cold.sha(b"ABC"))
    result = storage.summarize_verify(policy, cls, dry_run=False, inventory=[key])
    assert result["objects"][key] == {"bytes": 3, "sha256": cold.sha(b"ABC"), "remote_verified": True}
    cls["uploader"] = "archive_tape"
    immutable = cold.data_key(key, cold.sha(b"ABC"))
    monkeypatch.setattr(storage, "remote_files", lambda *_: {key: 3, immutable: 3})
    monkeypatch.setattr(storage, "remote_digest", remote)
    result = storage.summarize_verify(policy, cls, dry_run=False, inventory=[key])
    assert calls[-1] == (f"r2:bucket/apex/tape/{immutable}", 3)
    assert not result["objects"] and result["issues"] == [f"remote SHA-256 mismatch: {key}"]


def test_verify_historical_inventory_stays_scoped_and_hashes_exact_gzip(tmp_path, monkeypatch):
    cls = {"name": storage.PERELMAN_HOLD, "local": str(tmp_path), "r2": f"sealed/{storage.PERELMAN_HOLD}",
           "include": ["*.jsonl.gz"], "min_age": "0s", "local_days": 0}
    policy = {"class": [cls], "r2": {"remote": "r2:bucket"}, "do_only": {"skip_names": []}}
    held = tmp_path / "2000-01-01T00.jsonl.gz"
    held.write_bytes(gzip.compress(b'{"kind":"example"}\n'))
    (tmp_path / "not-a-held-hour.jsonl.gz").write_bytes(b"skip")
    expected = cold.sha(held.read_bytes())
    monkeypatch.setattr(storage, "remote_digest", lambda *_: expected)
    result = storage.summarize_verify(policy, cls, dry_run=False)
    assert not result["issues"] and list(result["objects"]) == [held.name]
    assert result["objects"][held.name]["sha256"] == expected
