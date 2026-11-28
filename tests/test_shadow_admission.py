from dataclasses import replace
import hashlib
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import orjson
import pytest

from doppel.strategy.genome import Condition, Genome, MakerRule, Source, TakerRule, Universe
from doppel.strategy.registry import Registry
from doppel.strategy.shadow_admission import (
    AdmissionRejected, AdmissionTrust, EVIDENCE_FILES, FEE_GRID, GATES, SIGNATURE_DOMAIN, SIZES,
    admit_shadow, simulation_digest,
)
from doppel.twin.engine import SimConfig
from doppel.twin.evaluate import RECEIVED_RAW_OPENING, VERDICT_OPTIONS, protocol, protocol_binding
from doppel.twin.verdict import block_bootstrap_p


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encode(doc):
    return orjson.dumps(doc, option=orjson.OPT_SORT_KEYS)


def label(s):
    return digest(s.encode())


def write(path, doc):
    raw = encode(doc)
    path.write_bytes(raw)
    return digest(raw)


class Bundle:
    def __init__(self, root, taker=False):
        self.root = root
        self.bundle = root / "bundle"
        self.bundle.mkdir()
        self.private = Ed25519PrivateKey.generate()
        self.commit = "e" * 40
        self.cfg = SimConfig(opening="received_raw")
        self.start = 1700000100
        self.end = self.start + 300 * 48
        clock = [self.start - 60]
        reg = Registry(root / "registry-root", now=lambda: clock[0], code={"sha": self.commit, "dirty": False})
        kwargs = {"taker": (TakerRule(side="up", when=(Condition("edge_buy_up_c", ">", 1),), size=25),),
                  "evidence": {"taker_trigger_source": "fair"}} if taker else {"maker": MakerRule(half_spread_c=1)}
        self.genome = Genome(name="fixture candidate", universe=Universe(venue="pmus"), source=Source(kind="manual"),
                             mode="taker" if taker else "maker", **kwargs)
        other = Genome(name="fixture alternative", mode="maker", universe=Universe(venue="pmus"),
                       source=Source(kind="manual"), maker=MakerRule(half_spread_c=1.1))
        for g in (self.genome, other):
            reg.register(g, "fixture", "held-out fixture", (self.start, self.end))
        bound = protocol_binding(RECEIVED_RAW_OPENING)
        reg.bind_protocol(bound, family="fixture")
        windows = [(i, i + 300) for i in range(self.start, self.end, 300)]
        returns = [4.0 + ((i * 37) % 17 - 8) / 4 for i in range(len(windows))]
        prices = [r - 1 for r in returns]
        p = block_bootstrap_p(returns, VERDICT_OPTIONS["block"], VERDICT_OPTIONS["n_boot"], VERDICT_OPTIONS["seed"])
        assert p is not None
        evaluation = {**protocol(RECEIVED_RAW_OPENING), "range": [self.start, self.end],
                      "input_sha256": label("sealed input"), "protocol_unbound": False}
        verdict = {"decision": "PASS", "p_source": "twin-replay", "p_value": round(p, 6), "evaluation": evaluation}
        clock[0] = self.end + 1
        result = reg.record_result(self.genome.id, verdict, (self.start, self.end))
        self.replay = {"schema": "doppel.shadow-replay/v1", "commit_sha": self.commit, "genome_id": self.genome.id,
                       "verdict": verdict, "windows": [{"start": a, "end": b, "eligible": True, "net_c": r}
                                                        for (a, b), r in zip(windows, returns)],
                       "scenarios": [{"fee_tier": name, "coefficient": fee, "size": size,
                                      "execution": "l2_book_walk_vwap", "attempted": 15, "filled": 9, "net_c": 6}
                                     for name, fee in FEE_GRID.items() for size in SIZES]}
        identities = [{"trial_id": label(str(i)), "genome_id": g.id, "subject_id": label("wallet"),
                       "variant_id": label(g.name)} for i, g in enumerate((self.genome, other))]
        self.plan = {"schema": "doppel.shadow-trial-plan/v1", "family": "fixture",
                     "frozen_ns": (self.start - 180) * 10**9, "attempts": identities}
        plan_sha = digest(encode(self.plan))
        self.trials = {"schema": "doppel.shadow-trial-ledger/v1", "plan_sha256": plan_sha,
                       "windows": windows, "trials": []}
        prev = "0" * 64
        for seq, (identity, values) in enumerate(zip(identities, (returns, prices))):
            row = {**identity, "seq": seq, "previous_sha256": prev, "returns_c": values}
            row["sha256"] = prev = digest(encode(row))
            self.trials["trials"].append(row)
        self.gates = {"schema": "doppel.shadow-gates/v1", "genome_id": self.genome.id,
                      "gates": {name: {"status": "MEASURED"} for name in GATES}}
        src = self.gates["gates"]
        for name in ("markout_2s", "markout_10s", "paper_replay_slippage"):
            src[name].update(value=0.1, observations=48, source_sha256=label(name))
        src["residual_correlation"].update(values={}, observations=0, source_sha256=label("verified empty roster"))
        src["taker_timing"]["trigger_source"] = "fair"
        self.timing = {"schema": "doppel.layer5b-score/v1", "source_kind": "recorded", "status": "MEASURED_PASS",
                       "software_status": "PASS", "plan_sha256": label("timing preregistration"),
                       "forecast_sha256": label("forecast"),
                       "peak_rss_bytes": {"forecast": 10**8, "score": 10**8, "limit": 1_500_000_000, "under_limit": True},
                       "source_receipts_sha256": {k: label(k) for k in ("books", "brti", "binance", "hyperliquid")},
                       "subjects": {
                           f"scripted_{name}_taker": {"status": "PASS", "model_status": "FIT", "trigger_source": name,
                                                       "precision": 1.0, "recall": 1.0, "f1": 1.0, "predicted": 20,
                                                       "truth": 20, "truth_markets": 10, "matched": 20, "zero_leakage": True,
                                                       "median_timing_delta_ns": -100,
                                                       "median_absolute_delta_ns": 100}
                           for name in ("lead", "fair")
                       }}
        self.timing["subjects"]["random_taker_control"] = {
            "status": "ABSTAIN", "model_status": "ABSTAIN", "predicted": 0, "truth": 8, "matched": 0,
            "zero_leakage": True,
        }
        now = time.time_ns()
        self.approval = {"schema": "doppel.shadow-approval/v1", "commit_sha": self.commit, "genome_id": self.genome.id,
                         "scoring_sha256": bound["code_sha256"], "registry_result_sha256": result["hash"],
                         "valid_from_ns": now - 10**9, "valid_until_ns": now + 60 * 10**9, "n_trials": 2,
                         "trial_head_sha256": prev, "candidate_trial_id": identities[0]["trial_id"]}
        self.trust = AdmissionTrust(self.private.public_key().public_bytes_raw(), reg.root, self.commit, plan_sha, ())
        self.registry = reg
        self.seal()

    def seal(self):
        self.gates["gates"]["taker_timing"]["source_sha256"] = digest(encode(self.timing))
        self.approval["files"] = {name: write(self.bundle / name, obj) for name, obj in zip(
            EVIDENCE_FILES, (self.plan, self.trials, self.replay, self.gates, self.timing), strict=True)}
        self.sign()

    def sign(self):
        write(self.bundle / "approval.json", self.approval)
        signature = self.private.sign(SIGNATURE_DOMAIN + encode(self.approval))
        (self.bundle / "approval.sig").write_bytes(signature)

    def admit(self, **kwargs):
        digest_arg = kwargs.pop("simulation_sha256", simulation_digest(self.cfg))
        return admit_shadow(self.genome, self.bundle, kwargs.pop("trust", self.trust), self.root / "admissions",
                            simulation_sha256=digest_arg)

    def reject(self, code, **kwargs):
        with pytest.raises(AdmissionRejected) as exc:
            self.admit(**kwargs)
        assert exc.value.code == code
        assert not (self.root / "admissions" / self.genome.id / "admission_receipt.json").exists()


def test_valid_bundle_issues_immutable_receipt(tmp_path):
    b = Bundle(tmp_path)
    receipt = b.admit()
    assert receipt["state"] == "SHADOW_ACTIVE" and receipt["n_trials"] == 2
    assert receipt["genome_id"] == b.genome.id and receipt["replay_sha256"] == b.approval["files"]["replay.json"]
    assert receipt["receipt_sha256"] == digest(encode({k: v for k, v in receipt.items() if k != "receipt_sha256"}))
    assert receipt["gates"]["dsr"] >= 0.95 and receipt["gates"]["pbo"] < 0.15
    file = tmp_path / "admissions" / b.genome.id / "admission_receipt.json"
    assert orjson.loads(file.read_bytes()) == receipt
    assert b.admit() == receipt
    old = file.read_bytes()
    b.approval["valid_until_ns"] += 10**9
    b.sign()
    with pytest.raises(AdmissionRejected, match="REJECT_RECEIPT_CONFLICT"):
        b.admit()
    assert file.read_bytes() == old


def test_submitted_pass_cannot_fill_missing_or_truncated_ledger(tmp_path):
    b = Bundle(tmp_path)
    (b.bundle / "trials.json").unlink()
    b.reject("REJECT_INCOMPLETE_LEDGER")
    b.trials["trials"].pop()
    b.seal()  # a signed, self-consistent file still lacks a trial pinned before unblinding
    b.reject("REJECT_INCOMPLETE_LEDGER")
    b.approval["n_trials"] = 1
    b.plan["attempts"].pop()
    b.seal()
    b.reject("REJECT_INCOMPLETE_LEDGER")


def test_exact_trial_identity_window_alignment_and_chain_are_required(tmp_path):
    b = Bundle(tmp_path)
    b.trials["windows"][1], b.trials["windows"][2] = b.trials["windows"][2], b.trials["windows"][1]
    b.seal()
    b.reject("REJECT_INCOMPLETE_LEDGER")
    b.trials["windows"][1], b.trials["windows"][2] = b.trials["windows"][2], b.trials["windows"][1]
    b.trials["trials"][1]["sha256"] = label("forged head")
    b.seal()
    b.reject("REJECT_INCOMPLETE_LEDGER")


@pytest.mark.parametrize("status", ["UNMEASURED", "SKIPPED", None, "PASS"])
def test_submitted_pass_does_not_supply_unmeasured_gate(tmp_path, status):
    b = Bundle(tmp_path)
    b.gates["gates"]["markout_2s"]["status"] = status
    b.seal()
    b.reject("REJECT_UNMEASURED_GATE")


def test_replay_digest_and_binding_are_required(tmp_path):
    b = Bundle(tmp_path)
    b.replay["scenarios"][0]["net_c"] = 60
    write(b.bundle / "replay.json", b.replay)
    b.reject("REJECT_NONCANONICAL_REPLAY")
    b.seal()
    b.replay["verdict"]["evaluation"]["protocol_unbound"] = True
    b.seal()
    b.reject("REJECT_NONCANONICAL_REPLAY")


def test_approval_scope_and_full_grid(tmp_path):
    b = Bundle(tmp_path)
    b.reject("REJECT_UNTRUSTED_APPROVAL", trust=replace(b.trust, public_key=Ed25519PrivateKey.generate().public_key().public_bytes_raw()))
    b.reject("REJECT_NONCANONICAL_REPLAY", simulation_sha256=label("not the replay configuration"))
    b.replay["scenarios"].pop()
    b.seal()
    b.reject("REJECT_NONCANONICAL_REPLAY")


def test_synthetic_timing_and_wrong_trigger_fail_closed(tmp_path):
    b = Bundle(tmp_path, taker=True)
    b.gates["gates"]["taker_timing"]["trigger_source"] = "lead"
    b.seal()
    b.reject("REJECT_UNMEASURED_GATE")
    b.gates["gates"]["taker_timing"]["trigger_source"] = "fair"
    b.timing["source_kind"] = "synthetic"
    b.seal()
    b.reject("REJECT_UNMEASURED_GATE")
    b.timing["source_kind"] = "recorded"
    b.seal()
    assert b.admit()["gates"]["taker_timing"]["candidate_trigger_source"] == "fair"


@pytest.mark.parametrize("changes", [
    {"truth_markets": 9},
    {"truth": 19, "predicted": 19, "matched": 19},
    {"precision": 0.85, "recall": 0.85, "f1": 0.85, "matched": 17},
])
def test_timing_pass_label_cannot_override_benchmark_denominators(tmp_path, changes):
    b = Bundle(tmp_path)
    b.timing["subjects"]["scripted_lead_taker"].update(changes)
    b.seal()
    b.reject("REJECT_UNMEASURED_GATE")


def test_memory_pass_label_cannot_override_peak_rss_measurement(tmp_path):
    b = Bundle(tmp_path)
    b.timing["peak_rss_bytes"]["forecast"] = 1_500_000_000
    b.seal()
    b.reject("REJECT_UNMEASURED_GATE")


def test_active_set_expiry_and_no_short_horizon_edge(tmp_path):
    b = Bundle(tmp_path)
    b.reject("REJECT_UNMEASURED_GATE", trust=replace(b.trust, active_genomes=(label("unmeasured active genome"),)))
    b.gates["gates"]["markout_2s"]["value"] = -0.1
    b.seal()
    b.reject("REJECT_GATE_FAILED")
    b.approval["valid_until_ns"] = 1
    b.sign()
    b.reject("REJECT_UNTRUSTED_APPROVAL")
