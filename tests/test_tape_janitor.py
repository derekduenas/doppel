from datetime import date, datetime, timezone
import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

OPS = Path(__file__).resolve().parents[1] / "ops"
spec = importlib.util.spec_from_file_location("tape_janitor", OPS / "tape_janitor.py")
janitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(janitor)


def test_one_byte_below_floor_prunes_only_oldest_real_stream_day(tmp_path, monkeypatch):
    root = tmp_path / "tape with spaces"
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    retained = ["pmus_ws_5m/20000102", f"pmus_ws_5m/{today}", "pmus_ws_5m/29991231",
                "pmus_ws_5m/19990230", "goldsky_activity/20000101", "ref_brti_raw/20000101",
                "receipts/20000101"]
    pruned = ["pmus_ws_5m/20000101", "rtds_a/20000101"]
    for rel in [*retained, *pruned]:
        (root / rel).mkdir(parents=True)
        (root / rel / "keep-or-prune").touch()
    (root / "pmus_ws_link").symlink_to(root / "ref_brti_raw", target_is_directory=True)
    (root / "rtds_a/19990101").symlink_to(root / "goldsky_activity/20000101", target_is_directory=True)
    assert min(janitor.closed_days(root, date(2000, 1, 2))) == date(2000, 1, 1)
    space = iter([12 * janitor.GIB - 1, 12 * janitor.GIB])
    monkeypatch.setattr(janitor.shutil, "disk_usage", lambda _: SimpleNamespace(free=next(space)))
    assert janitor.prune(root, 12) == 0
    assert all(not (root / rel).exists() for rel in pruned)
    assert all((root / rel / "keep-or-prune").is_file() for rel in retained)
    assert (root / "rtds_a/19990101").is_symlink()
    assert "deleting live-stream day 20000101" in (root / "janitor.log").read_text()


def test_entry_point_refuses_future_only_tree_below_floor(tmp_path):
    root = tmp_path / "shadow tape"
    future = root / "clob_btc/29991231"
    future.mkdir(parents=True)
    script = str(OPS / "doppel-janitor.sh")
    assert subprocess.run(["sh", script, str(root), "0"]).returncode == 0
    assert not (root / "janitor.log").exists()
    assert subprocess.run(["sh", script, str(root), "1000000"]).returncode == 1
    assert future.is_dir()
    assert "nothing deletable" in (root / "janitor.log").read_text()
