"""ops/storage.py: policy parsing, eligibility, R2 comparison and unclassified detection."""

import importlib.util
import os
import time
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("storage", ROOT / "ops" / "storage.py")
storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(storage)


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
