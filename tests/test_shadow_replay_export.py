from dataclasses import replace
from pathlib import Path

import orjson
import pytest

from doppel import cli
from doppel.lab import promotion, shadow_replay_export as exporter
from doppel.lab.shadow_bundle import build_shadow_bundle
from doppel.strategy.shadow_admission import FEE_GRID, SIGNATURE_DOMAIN, SIZES, admit_shadow, simulation_digest
from doppel.twin.evaluate import stamp
from test_shadow_admission import Bundle, encode, label, write


class RunFixture:
    """Keep the real registry, ledger, signing and admission path and replace tape playback."""

    def __init__(self, root: Path, monkeypatch):
        base = root / "base"
        base.mkdir()
        self.bundle = Bundle(base)
        b = self.bundle
        evaluation = b.replay["verdict"]["evaluation"]
        self.input_sha = evaluation["input_sha256"]
        self.data = root / "sealed"
        self.data.mkdir()
        part = self.data / "attested-input"
        part.write_bytes(b"fixture input")
        self.plan_file = root / "plan.json"
        self.spec_file = root / "run.json"
        self.out = root / "output"
        windows = [(row["start"], row["end"]) for row in b.replay["windows"]]
        monkeypatch.setattr(exporter, "git_sha", lambda: {"sha": b.commit, "dirty": False})
        monkeypatch.setattr(exporter, "evaluation_inputs", lambda *args: {"attested-input": part})
        monkeypatch.setattr(exporter, "fingerprint", lambda *args, **kwargs: (
            self.input_sha, {"attested-input": stamp(part)},
        ))
        monkeypatch.setattr(exporter, "verify_hours", lambda *args, **kwargs: None)
        monkeypatch.setattr(exporter, "_panel", lambda *args: (windows, True))
        alt = b.plan["attempts"][1]["genome_id"]
        failed = {**b.replay["verdict"], "decision": "CONTINUE"}
        b.registry.record_result(alt, failed, (b.start, b.end))
        base_rows = b.replay["windows"]
        fail_rows = [{**row, "net_c": row["net_c"] - 1} for row in base_rows]
        monkeypatch.setattr(exporter, "replay_snapshot", lambda data, lab, reg, **kwargs: (
            base_rows if reg["genome_id"] == b.genome.id else fail_rows, evaluation,
        ))
        monkeypatch.setattr(exporter, "verdict", lambda genome, rows, **kwargs: {
            k: v for k, v in (b.replay["verdict"] if genome.id == b.genome.id else failed).items()
            if k not in ("p_source", "evaluation")
        })

        def scenarios(data, lab, genome, start, end, config, grid, *, trace):
            assert tuple(grid) == tuple(windows)
            trace({"window": grid[0], "filled_qty": 25})
            return b.replay["scenarios"], {"fixture_scenarios": len(b.replay["scenarios"])}

        monkeypatch.setattr(exporter, "replay_scenarios", scenarios)
        self.attempts = []
        plan_rows = []
        run_id = label("registered run")
        for index, genome_id in enumerate((b.genome.id, alt, label("non-executable slot"))):
            name = f"variant-{index}.json"
            variant = write(root / name, {"selected_during_development": index})
            subject = label(f"wallet-{index}")
            trial_id = exporter._sha(encode([run_id, genome_id, subject, variant]))
            plan_rows.append({"trial_id": trial_id, "genome_id": genome_id, "subject_id": subject, "variant_id": variant})
            self.attempts.append({"trial_id": trial_id, "variant": {"path": name, "sha256": variant}})
        fit_sha = write(root / "fit.json", {"verdict": "NOT_IDENTIFIED"})
        policy_sha = write(root / "abstention.json", {
            "schema": exporter.POLICY_SCHEMA, "entry_policy": "always_abstain", "subject_id": plan_rows[-1]["subject_id"],
            "variant_id": plan_rows[-1]["variant_id"], "fit_evidence_sha256": fit_sha,
            "fit_evidence": {"path": "fit.json", "sha256": fit_sha},
        })
        self.attempts[-1]["policy"] = {"path": "abstention.json", "sha256": policy_sha}
        self.plan_sha = write(self.plan_file, {**b.plan, "schema": promotion.ABSTENTION_PLAN_SCHEMA, "attempts": plan_rows,
                                               "abstentions": {plan_rows[-1]["trial_id"]: policy_sha}})
        self.spec_sha = write(self.spec_file, {
            "schema": exporter.RUN_SCHEMA, "run_id": run_id, "family": "fixture", "plan_sha256": self.plan_sha,
            "commit_sha": b.commit, "scoring_sha256": evaluation["code_sha256"], "simulation_sha256": simulation_digest(b.cfg),
            "forward_range_s": [b.start, b.end], "panel_trial_id": plan_rows[0]["trial_id"],
            "window_policy_sha256": exporter._sha(encode(exporter.WINDOW_POLICY)),
            "scenario_policy_sha256": exporter._sha(encode(exporter.SCENARIO_POLICY)), "attempts": self.attempts,
        })
        self.replay = self.out / "candidates" / plan_rows[0]["trial_id"] / "replay.json"

    def args(self):
        return [self.spec_file, self.spec_sha, self.plan_file, self.plan_sha, self.data, None,
                self.bundle.registry.root, self.bundle.commit, self.out]

    def run(self):
        return exporter.export_run(*self.args())


def test_export_promote_sign_admit_with_failed_and_abstained_variants(tmp_path, monkeypatch):
    run = RunFixture(tmp_path, monkeypatch)
    b = run.bundle
    assert cli.main(["promote", "replay-export", "--run-spec", str(run.spec_file), "--run-spec-sha256", run.spec_sha,
                     "--plan", str(run.plan_file), "--plan-sha256", run.plan_sha, "--data", str(run.data),
                     "--registry-root", str(b.registry.root), "--commit-sha", b.commit, "--out", str(run.out)]) == 0
    receipt_path = run.out / "run-receipt.json"
    receipt = orjson.loads(receipt_path.read_bytes())
    assert receipt["status"] == "READY"
    assert receipt["dispositions"] == {"PASS": 1, "FAIL": 1, "ABSTAIN": 1, "ERROR": 0}
    ledger_path = run.out / "trials.json"
    ledger = orjson.loads(ledger_path.read_bytes())
    assert ledger["trials"][-1]["abstention"]["input_sha256"] == run.input_sha
    assert ledger["trials"][-1]["returns_c"] == [0.0] * 48
    replay = orjson.loads(run.replay.read_bytes())
    assert replay["verdict"] == b.replay["verdict"]
    assert {(row["fee_tier"], row["size"]) for row in replay["scenarios"]} == {
        (name, size) for name in FEE_GRID for size in SIZES
    }
    assert replay["scenario_trace_sha256"] == exporter._digest(run.replay.with_name("scenario-trace.jsonl"))
    before = receipt_path.read_bytes(), ledger_path.read_bytes(), run.replay.read_bytes()
    assert run.run() == receipt
    assert (receipt_path.read_bytes(), ledger_path.read_bytes(), run.replay.read_bytes()) == before

    report_dir = tmp_path / "promotion"
    assert promotion.main(["--results", str(run.replay), "--trials", str(ledger_path), "--out", str(report_dir)]) == 0
    (report_dir / "plan.json").write_bytes(run.plan_file.read_bytes())
    gates = tmp_path / "gates"
    gates.mkdir()
    write(gates / "gates.json", {"gates": {name: b.gates["gates"][name] for name in (
        "paper_replay_slippage", "residual_correlation",
    )}})
    write(gates / "maker.json", {**b.maker, "replay_sha256": exporter._digest(run.replay)})
    write(gates / "probability.json", {
        "schema": "doppel.shadow-probability/v1", "genome_id": b.genome.id, "offset_s": 60,
        "fit_end_ns": (b.start - 60) * 10**9, "fit_sha256": label("development calibration"),
        "intercept": 0, "weights": {"fair_up": 1},
    })
    trust = replace(b.trust, plan_sha256=run.plan_sha)
    bundle = build_shadow_bundle(report_dir, run.replay, ledger_path, gates / "gates.json", genome=b.genome,
                                 trust=trust, validity_ns=(b.approval["valid_from_ns"], b.approval["valid_until_ns"]))
    signed = tmp_path / "operator-signed"
    bundle.write(signed, b.private.sign(SIGNATURE_DOMAIN + bundle.approval))
    admitted = admit_shadow(b.genome, signed, trust, tmp_path / "receipts",
                            simulation_sha256=simulation_digest(b.cfg), require_probability=True)
    assert admitted["state"] == "SHADOW_ACTIVE" and admitted["n_trials"] == 3
    assert admitted["replay_sha256"] == exporter._digest(run.replay)
    assert admitted["gates"]["verified_abstentions"] == 1

    run.replay.with_name("scenario-trace.jsonl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="differs from its checkpoint"):
        run.run()


@pytest.mark.parametrize("tamper", ["partial_panel", "wrong_abstention_policy", "missing_variant"])
def test_unobserved_or_unpinned_trial_is_recorded_and_blocks_promotion(tmp_path, monkeypatch, tamper):
    run = RunFixture(tmp_path, monkeypatch)
    if tamper == "partial_panel":
        windows = [(w["start"], w["end"]) for w in run.bundle.replay["windows"]]
        monkeypatch.setattr(exporter, "_panel", lambda *args: (windows, False))
    elif tamper == "wrong_abstention_policy":
        (tmp_path / "abstention.json").write_bytes(b"another policy")
    else:
        (tmp_path / "variant-1.json").unlink()
    receipt = run.run()
    assert receipt["status"] == "REJECT_INCOMPLETE_LEDGER"
    ledger_path = run.out / "trials.json"
    ledger = orjson.loads(ledger_path.read_bytes())
    assert len(ledger["trials"]) == 3
    row, = [r for r in ledger["trials"] if r["disposition"] == "ERROR"]
    assert row["returns_c"] is None and "abstention" not in row
    assert promotion.main(["--results", str(run.replay), "--trials", str(ledger_path),
                           "--out", str(tmp_path / "promotion")]) == 2


def test_run_spec_digest_is_required_before_trials_begin(tmp_path, monkeypatch):
    run = RunFixture(tmp_path, monkeypatch)
    args = run.args()
    args[1] = "0" * 64
    with pytest.raises(ValueError, match="independently pinned"):
        exporter.export_run(*args)
    assert not run.out.exists()
