from pathlib import Path

import pytest

import doppel.paper.runtime as runtime
import doppel.paper.shadow as shadow
from doppel.paper.journal import iter_records
from doppel.strategy.shadow_admission import AdmissionRejected

from test_paper import maker

NOW = 1_800_000_000_000_000_000


def test_observer_is_not_admitted_and_rejected_bundle_cannot_create_journal(tmp_path, monkeypatch):
    def reject(*_):
        raise AdmissionRejected("REJECT_INCOMPLETE_LEDGER", "missing trial ledger")

    monkeypatch.setattr(runtime, "authorize", reject)
    genome = maker()
    observer = runtime.PaperRuntime(genome, tmp_path, tmp_path / "observer", clock=lambda: NOW)
    observer.start(NOW // runtime.NS)
    assert next(iter_records(tmp_path / "observer"))["admission"] == {"state": "RESEARCH_OBSERVE"}
    guarded = runtime.PaperRuntime(genome, tmp_path, tmp_path / "guarded", clock=lambda: NOW,
                                   shadow_bundle=tmp_path / "bundle")
    with pytest.raises(AdmissionRejected) as exc:
        guarded.start(NOW // runtime.NS)
    assert exc.value.code == "REJECT_INCOMPLETE_LEDGER"
    assert not (tmp_path / "guarded").exists()


def test_shadow_journal_binds_receipt_and_cannot_resume_as_observer(tmp_path, monkeypatch):
    verified = shadow.ShadowSession("a" * 64, NOW + 60 * runtime.NS, "b" * 64)
    monkeypatch.setattr(shadow.ShadowSession, "stop_reason", lambda *args: None)
    calls = []
    def authorize(*args):
        calls.append(args)
        return verified
    monkeypatch.setattr(runtime, "authorize", authorize)
    genome = maker()
    target = tmp_path / "j"
    with pytest.raises(ValueError, match="current clock"):
        runtime.PaperRuntime(genome, tmp_path, target, clock=lambda: NOW,
                             shadow_bundle=Path("bundle")).start(1)
    rt = runtime.PaperRuntime(genome, tmp_path, target, clock=lambda: NOW, shadow_bundle=Path("bundle"))
    rt.start(NOW // runtime.NS)
    assert len(calls) == 1
    assert next(iter_records(target))["admission"] == verified.journal_binding()
    with pytest.raises(runtime.RecoveryError, match="shadow admission and operating mode"):
        runtime.PaperRuntime(genome, tmp_path, target, clock=lambda: NOW).start(None)


def test_shadow_expiry_and_revocation(monkeypatch):
    import hashlib

    old = b"approved config"
    session = shadow.ShadowSession("a" * 64, NOW + 1, hashlib.sha256(old).hexdigest())
    monkeypatch.setattr(shadow, "_owner_file", lambda _: old)
    assert session.stop_reason(NOW) is None
    assert session.stop_reason(NOW + 1) == "shadow_admission_expired"
    monkeypatch.setattr(shadow, "_owner_file", lambda _: b"revoked")
    assert session.stop_reason(NOW) == "shadow_approval_changed"
