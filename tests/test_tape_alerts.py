import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


path = Path(__file__).resolve().parents[1] / "ops/tape_alerts.py"
spec = importlib.util.spec_from_file_location("tape_alerts", path)
alerts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alerts)

UNIT = "doppel-tape.service"
ACTIVE = {"ActiveState": "active", "InvocationID": "one", "NRestarts": "0"}
NOW = 1_800_000_000 * 10**9


@pytest.fixture
def healthy_host(monkeypatch):
    monkeypatch.setattr(alerts, "runtime_issues", lambda _: {})


def write_status(root, *, snapshot_age=0, source_age=0, uptime=120):
    (root / "status.json").write_text(json.dumps({
        "tw": NOW - snapshot_age * 10**9, "uptime_s": uptime,
        "sources": {"fast_book": {"last_msg_age_s": source_age}},
    }))


@pytest.mark.parametrize(("snapshot_age", "source_age", "key"), [
    (10, 50, None), (11, 50, "fast_book/stall"), (61, 0, "status"),
    (0, None, "fast_book/stall"),
])
def test_stall_accounts_for_snapshot_age(tmp_path, snapshot_age, source_age, key):
    write_status(tmp_path, snapshot_age=snapshot_age, source_age=source_age)
    actual = alerts.recorder_issues(UNIT, tmp_path / "status.json", ACTIVE, ACTIVE, NOW)
    assert set(actual) == ({f"{UNIT}/{key}"} if key else set())


def test_disk_alert_threshold_matches_df(monkeypatch, tmp_path):
    monkeypatch.setattr(alerts.shutil, "disk_usage", lambda _: SimpleNamespace(used=8, free=2))
    assert alerts.disk_issue(tmp_path) is None
    monkeypatch.setattr(alerts.shutil, "disk_usage", lambda _: SimpleNamespace(used=81, free=19))
    assert alerts.disk_issue(tmp_path) == f"{tmp_path} is 81.0% full"


def test_memory_watchdog_matches_operational_thresholds(tmp_path):
    info = tmp_path / "meminfo"
    pressure = tmp_path / UNIT / "memory.pressure"
    pressure.parent.mkdir()
    info.write_text("MemAvailable:       614400 kB\n")
    pressure.write_text("some avg10=60.0 avg60=60.0 avg300=60.0 total=100\n"
                        "full avg10=90.0 avg60=90.0 avg300=40.0 total=100\n")
    assert alerts.runtime_issues((UNIT, "gone.service"), info, tmp_path) == {}
    info.write_text("MemAvailable:       610304 kB\n")
    pressure.write_text("full avg10=0.0 avg60=0.0 avg300=40.5 total=100\n")
    issues = alerts.runtime_issues((UNIT,), info, tmp_path)
    assert set(issues) == {"host/memory", f"{UNIT}/pressure"}


def test_restart_alert_with_reset_counters_and_retry(tmp_path, monkeypatch, healthy_host):
    write_status(tmp_path)
    current = ACTIVE.copy()
    monkeypatch.setattr(alerts, "unit_status", lambda _: current.copy())
    monkeypatch.setattr(alerts.time, "time_ns", lambda: NOW)
    recorders = [(UNIT, tmp_path)]
    state = tmp_path / "state.json"
    webhook = tmp_path / "test-webhook"
    sent = []
    monkeypatch.setattr(alerts, "send_discord", lambda _, messages: sent.extend(messages))
    alerts.poll(recorders, [], state, webhook)
    assert sent == []
    current["InvocationID"] = "two"
    monkeypatch.setattr(alerts, "send_discord",
                        lambda *_: (_ for _ in ()).throw(OSError("unavailable")))
    with pytest.raises(OSError, match="unavailable"):
        alerts.poll(recorders, [], state, webhook)
    assert json.loads(state.read_bytes())["units"][UNIT]["InvocationID"] == "one"
    monkeypatch.setattr(alerts, "send_discord", lambda _, messages: sent.extend(messages))
    alerts.poll(recorders, [], state, webhook)
    alerts.poll(recorders, [], state, webhook)
    assert sent == [f"{UNIT} restarted"]


def test_dry_run_does_not_write_state_or_send(tmp_path, monkeypatch, capsys, healthy_host):
    monkeypatch.setattr(alerts, "unit_status", lambda _: {"ActiveState": "inactive"})
    monkeypatch.setattr(alerts, "send_discord", lambda *_: pytest.fail("dry run may not send"))
    state = tmp_path / "state.json"
    alerts.poll([(UNIT, tmp_path)], [], state, None, dry_run=True)
    assert not state.exists()
    assert json.loads(capsys.readouterr().out) == {f"{UNIT}/inactive": f"{UNIT} is inactive"}


def test_journal_only_is_default(tmp_path, monkeypatch, capsys, healthy_host):
    monkeypatch.setattr(alerts, "unit_status", lambda _: {"ActiveState": "failed"})
    monkeypatch.setattr(alerts, "send_discord", lambda *_: pytest.fail("no configured webhook"))
    state = tmp_path / "state.json"
    alerts.poll([(UNIT, tmp_path)], [], state, None)
    alerts.poll([(UNIT, tmp_path)], [], state, None)
    assert capsys.readouterr().out == f"{UNIT} is failed\n"
