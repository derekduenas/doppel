"""Replay trial journal durability and evidence integrity."""

import hashlib

import orjson
import pytest

from doppel.lab import shadow_trial_ledger as ledger


def canonical(value):
    return orjson.dumps(value, option=orjson.OPT_SORT_KEYS)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def store_plan(tmp_path, doc):
    path = tmp_path / "plan.json"
    raw = canonical(doc) + b"\n"
    path.write_bytes(raw)
    return path, digest(raw)


@pytest.fixture
def planned(tmp_path):
    attempts = [
        {key: digest(f"{i}/{key}".encode()) for key in ledger.IDENTITY}
        for i in range(4)
    ]
    doc = {
        "schema": ledger.PLAN_SCHEMA, "family": "wallet-forward", "frozen_ns": 100,
        "attempts": attempts, "abstentions": {attempts[2]["trial_id"]: "2" * 64},
    }
    windows = [(200 + i * 300, 500 + i * 300) for i in range(32)]
    path, plan_sha = store_plan(tmp_path, doc)
    plan = ledger.read_plan(path, plan_sha, doc["family"], windows[0][0])
    return plan, plan_sha, windows


def make_writer(out, planned, context="3" * 64):
    return ledger.TrialLedger(out, *planned, context)


def seal_attempts(writer, planned, begin=0):
    plan, _, windows = planned
    outcomes = [
        ("PASS", [float(i) for i in range(len(windows))], "verified binding pass", {}),
        ("FAIL", [float(-i) for i in range(len(windows))], "measured failure", {}),
        ("ABSTAIN", [0.0] * len(windows), "preregistered always-no-entry policy",
         {"abstention": {
             "schema": ledger.ABSTENTION_SCHEMA, "policy_sha256": "2" * 64, "input_sha256": "a" * 64,
             "windows_sha256": digest(canonical(windows)), "observed_windows": len(windows), "entry_attempts": 0,
         }}),
        ("ERROR", None, "input unavailable", {}),
    ]
    for attempt, (disposition, returns, reason, proof) in zip(plan["attempts"][begin:], outcomes[begin:], strict=True):
        writer.begin(attempt)
        writer.finish(attempt, disposition=disposition, returns_c=returns, reason=reason, evidence=proof)


def test_canonical_ledger_preserves_every_disposition(tmp_path, planned):
    dest = tmp_path / "run"
    with make_writer(dest, planned) as writer:
        seal_attempts(writer, planned)
        closed = writer.publish()
        assert writer.publish() == closed
    ledger.validate_ledger_v2(closed)
    assert len(closed["trials"]) == 4
    assert [row["disposition"] for row in closed["trials"]] == ["PASS", "FAIL", "ABSTAIN", "ERROR"]
    assert closed["trials"][2]["returns_c"] == [0.0] * 32
    assert closed["trials"][3]["returns_c"] is None
    previous = "0" * 64
    for seq, row in enumerate(closed["trials"]):
        assert row["seq"] == seq and row["previous_sha256"] == previous
        assert row["sha256"] == digest(canonical({k: v for k, v in row.items() if k != "sha256"}))
        previous = row["sha256"]
    with make_writer(dest, planned) as resumed:
        assert resumed.rows == closed["trials"]
        assert resumed.publish() == closed
    second = tmp_path / "rerun"
    with make_writer(second, planned) as writer:
        seal_attempts(writer, planned)
        writer.publish()
    assert (dest / "trials.json").read_bytes() == (second / "trials.json").read_bytes()


def test_ledger_rejects_unplanned_or_duplicate_starts_and_concurrent_writer(tmp_path, planned):
    dest = tmp_path / "run"
    first, second, *_ = planned[0]["attempts"]
    with make_writer(dest, planned) as writer:
        with pytest.raises(ValueError, match="next planned"):
            writer.begin(second)
        writer.begin(first)
        writer.begin(first)  # Retry the same durable start.
        with pytest.raises(ValueError, match="another writer"):
            with make_writer(dest, planned):
                pytest.fail("another writer was allowed")
        with pytest.raises(ValueError, match="started planned"):
            writer.finish(second, disposition="ERROR", returns_c=None, reason="missing", evidence={})
        with pytest.raises(ValueError, match="incomplete"):
            writer.publish()
        writer.finish(first, disposition="ERROR", returns_c=None, reason="missing", evidence={})
        with pytest.raises(ValueError, match="next planned"):
            writer.begin(first)
    assert not (dest / "trials.json").exists()


def test_journal_resumes_torn_finish_at_same_start(tmp_path, planned):
    dest = tmp_path / "run"
    first = planned[0]["attempts"][0]
    with make_writer(dest, planned) as writer:
        writer.begin(first)
    log = dest / "trials.log.jsonl"
    before = log.read_bytes()
    torn = b'{"disposition":"PASS"'
    with log.open("ab") as stream:
        stream.write(torn)
    with make_writer(dest, planned) as resumed:
        assert resumed.rows == []
        assert log.read_bytes() == before
        assert (dest / f"trials.torn-{digest(torn)}.partial").read_bytes() == torn
        seal_attempts(resumed, planned)
        assert len(resumed.publish()["trials"]) == 4


def test_journal_replays_finish_when_process_loses_result_after_fsync(tmp_path, planned, monkeypatch):
    dest = tmp_path / "run"
    first = planned[0]["attempts"][0]
    real_fsync = ledger.os.fsync

    def lose_result(fd):
        real_fsync(fd)
        raise OSError("lost after durable finish")

    with make_writer(dest, planned) as writer:
        writer.begin(first)
        with monkeypatch.context() as mp:
            mp.setattr(ledger.os, "fsync", lose_result)
            with pytest.raises(OSError, match="after durable finish"):
                writer.finish(first, disposition="PASS", returns_c=[1.0] * 32, reason="bound pass", evidence={})
        with pytest.raises(ValueError, match="fresh trial ledger"):
            writer.begin(first)
    with make_writer(dest, planned) as resumed:
        assert [row["trial_id"] for row in resumed.rows] == [first["trial_id"]]
        seal_attempts(resumed, planned, begin=1)
        assert len(resumed.publish()["trials"]) == 4


@pytest.mark.parametrize("damage", ["change", "truncate", "reorder", "wrong context", "wrong windows"])
def test_finalized_journal_refuses_tampering_and_divergent_resume(tmp_path, planned, damage):
    dest = tmp_path / "run"
    with make_writer(dest, planned) as writer:
        seal_attempts(writer, planned)
        writer.publish()
    log = dest / "trials.log.jsonl"
    lines = log.read_bytes().splitlines(keepends=True)
    context = "3" * 64
    if damage == "change":
        lines[2] = lines[2].replace(b"binding pass", b"binding fail")
    elif damage == "truncate":
        lines = lines[:-2]
    elif damage == "reorder":
        lines[1], lines[3] = lines[3], lines[1]
    elif damage == "wrong context":
        context = "4" * 64
    else:
        doc, sha, windows = planned
        planned = doc, sha, windows[:-1]
    log.write_bytes(b"".join(lines))
    with pytest.raises(ValueError):
        with make_writer(dest, planned, context):
            pytest.fail("damaged evidence was accepted")


def test_plan_and_abstention_require_the_pinned_identity_and_window_grid(tmp_path, planned):
    plan, plan_sha, windows = planned
    path = tmp_path / "plan.json"
    with pytest.raises(ValueError, match="independently pinned"):
        ledger.read_plan(path, "f" * 64, plan["family"], windows[0][0])
    with pytest.raises(ValueError, match="cutoff"):
        ledger.read_plan(path, plan_sha, plan["family"], 0)
    bad_plan = {**plan, "attempts": [plan["attempts"][0]] * 4}
    bad_path, bad_sha = store_plan(tmp_path, bad_plan)
    with pytest.raises(ValueError, match="duplicate"):
        ledger.read_plan(bad_path, bad_sha, plan["family"], windows[0][0])

    with make_writer(tmp_path / "run", planned) as writer:
        first, second, abstention, _ = plan["attempts"]
        for attempt in (first, second):
            writer.begin(attempt)
            writer.finish(attempt, disposition="FAIL", returns_c=[-1.0] * 32, reason="fail", evidence={})
        writer.begin(abstention)
        proof = {"schema": ledger.ABSTENTION_SCHEMA, "policy_sha256": "c" * 64, "input_sha256": "a" * 64,
                 "windows_sha256": digest(canonical(windows)), "observed_windows": 32, "entry_attempts": 0}
        with pytest.raises(ValueError, match="preregistered policy"):
            writer.finish(abstention, disposition="ABSTAIN", returns_c=[0.0] * 32,
                          reason="no entry", evidence={"abstention": proof})
        proof["policy_sha256"] = "2" * 64
        proof["observed_windows"] = 31
        with pytest.raises(ValueError, match="complete observed"):
            writer.finish(abstention, disposition="ABSTAIN", returns_c=[0.0] * 32,
                          reason="no entry", evidence={"abstention": proof})
        proof["observed_windows"] = 32
        with pytest.raises(ValueError, match="complete observed"):
            writer.finish(abstention, disposition="ABSTAIN", returns_c=[1.0] * 32,
                          reason="no entry", evidence={"abstention": proof})
        assert len(writer.rows) == 2
