from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from ops import cold_tape as cold
from ops import tape_janitor as janitor


NOW = datetime(2026, 11, 27, 12, tzinfo=UTC)
OPS = Path(__file__).resolve().parents[1] / "ops"


class MemoryArchive:
    remote = "r2:private/tape"

    def __init__(self):
        self.objects = {}
        self.transfers = 0
        self.fail = False

    def ensure(self, key, payload, size, expected_sha):
        data = payload if isinstance(payload, bytes) else payload.read()
        assert len(data) == size and cold.sha(data) == expected_sha
        if self.fail:
            raise ValueError("remote SHA-256 verification failed")
        self.objects.setdefault(key, data)
        assert self.objects[key] == data
        self.transfers += 1

    def verify(self, key, size, expected_sha):
        data = self.objects.get(key)
        if data is None or len(data) != size or cold.sha(data) != expected_sha:
            raise ValueError("remote SHA-256 verification failed")


def write_segment(root, stream, day, hour="00", data=b"bytes"):
    name = f"{day}/{hour}.dtape.zst"
    path = root / stream / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    row = {"file": name, "bytes": len(data), "sha256": cold.sha(data)}
    with (root / stream / "manifest.jsonl").open("ab") as out:
        out.write(cold.json_bytes(row))
    return path


def test_only_fully_verified_expired_data_is_eligible(tmp_path):
    root, state = tmp_path / "with spaces", tmp_path / "state"
    expired = write_segment(root, "clob_btc_5m", "20261119")
    same_day = write_segment(root, "spot_l2_lmax", "20261119")
    next_day = write_segment(root, "rtds_a", "20261120")
    protected = write_segment(root, "pmus_ws_5m", "20261117")
    paid = write_segment(root, "goldsky_activity", "20261119")
    reference = write_segment(root, "ref_brti_raw", "20261119")
    kept_hour = write_segment(root, "lead_binance_spot", "20261120", "12")
    active = write_segment(root, "rtds_b", "20261127", "12")
    unverified = write_segment(root, "pmus_ws_5m", "20261118")
    (root / "janitor-protect.txt").write_text("20261117\n")
    target = MemoryArchive()

    preview = cold.offload(root, target, state, ["auto"], None, NOW, dry_run=True)
    assert preview["pending"] == 8 and not state.exists() and not target.transfers
    result = cold.offload(root, target, state,
                          ["clob_btc_5m", "spot_l2_lmax", "rtds_a", "pmus_ws_5m", "goldsky_activity",
                           "ref_brti_raw", "lead_binance_spot", "rtds_b"],
                          datetime(2026, 11, 19, tzinfo=UTC), NOW)
    assert result["archived"] == 6 and not result["issues"] and target.transfers == 12

    preview = janitor.prune(root, 0, target=target, state=state, now=NOW, retain_days=7, dry_run=True)
    assert set(preview["ready"]) == {"clob_btc_5m/20261119/00.dtape.zst", "spot_l2_lmax/20261119/00.dtape.zst"}
    assert expired.exists() and same_day.exists()
    report = janitor.prune(root, 0, target=target, state=state, now=NOW, retain_days=7)
    assert set(report["deleted"]) == set(preview["ready"]) and not report["issues"]
    assert not expired.exists() and not same_day.exists()
    assert all(path.exists() for path in (protected, next_day, paid, reference, kept_hour, active, unverified))


def test_failed_remote_verification_or_deleted_object_never_authorizes_cleanup(tmp_path):
    root, state = tmp_path / "tape", tmp_path / "state"
    path = write_segment(root, "clob_btc_5m", "20261001")
    target = MemoryArchive()
    target.fail = True
    report = cold.offload(root, target, state, ["auto"], None, NOW)
    assert "remote SHA-256 verification failed" in report["issues"]
    assert not list(state.glob("*.json"))
    assert janitor.prune(root, 0, target=target, state=state, now=NOW)["deleted"] == []
    target.fail = False
    assert cold.offload(root, target, state, ["auto"], None, NOW)["archived"] == 1
    transfers = target.transfers
    assert cold.offload(root, target, state, ["auto"], None, NOW)["verified"] == 1
    assert target.transfers == transfers
    key = next(k for k in target.objects if k.startswith("data/"))
    target.objects[key] = b"wrong"
    report = janitor.prune(root, 0, target=target, state=state, now=NOW)
    assert report["issues"] and path.is_file()
    target.objects.clear()
    report = janitor.prune(root, 0, target=target, state=state, now=NOW)
    assert report["issues"] and path.is_file()


def test_prune_refuses_changed_bytes_or_link(tmp_path):
    root, state = tmp_path / "tape", tmp_path / "state"
    path = write_segment(root, "clob_btc_5m", "20261001", data=b"old")
    target = MemoryArchive()
    assert cold.offload(root, target, state, ["auto"], None, NOW)["archived"] == 1
    path.write_bytes(b"new")
    result = janitor.prune(root, 0, target=target, state=state, now=NOW)
    assert result["retained"] and path.read_bytes() == b"new"
    elsewhere = tmp_path / "keep"
    elsewhere.write_bytes(b"old")
    path.unlink()
    path.symlink_to(elsewhere)
    result = janitor.prune(root, 0, target=target, state=state, now=NOW)
    assert result["issues"] and path.is_symlink() and elsewhere.read_bytes() == b"old"


def test_receipt_mismatch_is_preserved_with_proof_but_not_pruned(tmp_path):
    root, state = tmp_path / "tape", tmp_path / "state"
    path = write_segment(root, "spot_l2_lmax", "20261001")
    receipt = root / "receipts/20261001/00.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_bytes(cold.json_bytes({"day": "20261001", "hour": 0, "streams": {
        "spot_l2_lmax": {"bytes": 1, "sha256": "0" * 64, "crc_ok": True}}}))
    target = MemoryArchive()
    assert cold.offload(root, target, state, ["auto"], None, NOW)["archived"] == 1
    assert receipt.read_bytes() in target.objects.values()
    manifests = [json.loads(data) for key, data in target.objects.items() if key.startswith("checksums/")]
    assert manifests[0]["receipt_sha256"] == cold.sha(receipt.read_bytes())
    report = janitor.prune(root, 0, target=target, state=state, now=NOW)
    assert report["retained"] and path.exists()


def test_cold_retention_has_no_fallback_and_local_policy_protects_registered_days(tmp_path, monkeypatch):
    root = tmp_path / "tape"
    path = write_segment(root, "clob_btc_5m", "20261001")
    monkeypatch.setattr(janitor.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    assert janitor.prune(root, 12)["issues"]
    assert path.is_file()
    (root / "janitor-protect.txt").write_text("20261001\n")
    older = write_segment(root, "clob_btc_5m", "20260930")
    ran = subprocess.run(["sh", str(OPS / "doppel-janitor.sh"), str(root), "1000000"], capture_output=True)
    assert ran.returncode == 0
    assert path.is_file() and not older.exists()
    with pytest.raises(ValueError, match="7 to 14"):
        janitor.prune(root, 12, retain_days=6)
