from dataclasses import replace
import hashlib
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import orjson
import pytest

from doppel import cli
from doppel.lab import promotion
from doppel.lab.shadow_bundle import build_shadow_bundle
from doppel.strategy.genome import Condition, Genome, MakerRule, Source, TakerRule, Universe, to_dict
from doppel.strategy.registry import Registry
from doppel.strategy.shadow_admission import (
    COMMON_GATES, MAKER_GATES, AdmissionRejected, AdmissionTrust, FEE_GRID, SIGNATURE_DOMAIN, SIZES,
    _statistics, admit_shadow, simulation_digest,
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
    def __init__(self, root, taker=False, maker_kind="maker", offshore_guard=False):
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
                  "evidence": {"taker_trigger_source": "fair"}} if taker else {
                      "maker": MakerRule(half_spread_c=1),
                      "evidence": {"archetype": maker_kind, **({"cancel_guard_sources": ["binance"]} if offshore_guard else {})}}
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
        self.kind = "taker_fair" if taker else maker_kind
        self.gates = {"schema": "doppel.shadow-gates/v2", "genome_id": self.genome.id, "archetype": self.kind,
                      "gates": {name: {"status": "MEASURED_PASS"} for name in COMMON_GATES}}
        self.maker = {"schema": "doppel.shadow-maker/v1", "genome_id": self.genome.id, "archetype": self.kind,
                      "replay_sha256": digest(encode(self.replay)), "input_sha256": evaluation["input_sha256"],
                      "queue": {"model": "pessimistic_l2_fifo", "source_sha256": label("native queue"),
                                "windows": 48, "quote_observations": 100, "known_queue_observations": 95,
                                "fills": 60, "verified_fills": 60, "fill_windows": 48, "feed_gap_windows": 0,
                                "min_trade_coverage": .95, "max_inferred_share": .2, "max_unidentified_share": .1},
                      "markouts": {h: {"value": .1, "observations": 48, "windows": 48,
                                       "source_sha256": label("markout_" + h)} for h in ("2s", "10s")},
                      "stress": {"method": "delayed_cancel", "source_sha256": label("delayed cancel replay"),
                                 "plan_sha256": label("frozen stress plan"), "windows": 48,
                                 "base_cancel_latency_ms": self.cfg.latency_ms, "extra_cancel_latency_ms": 200,
                                 "net_c_per_window": .2, "ci_low_c_per_window": .05}}
        self.offshore = {}
        if taker:
            streams = {"binance": "lead_binance_spot_l2", "hyperliquid": "lead_hyperliquid_l2"}
            for role, stream in streams.items():
                self.offshore[role + ".json"] = {
                    "schema": "doppel.layer5b-source/v1", "role": role, "clock": "local_receive_ns",
                    "segments": [{"stream": stream, "path": f"{stream}/20231114/22.dtape.zst",
                                  "sha256": label(stream), "covered_range_ns": [self.start * 10**9, self.end * 10**9]}]}

        src = self.gates["gates"]
        for name in ("markout_2s", "markout_10s", "paper_replay_slippage"):
            src[name].update(value=0.1, observations=48, source_sha256=label(name))
        src["residual_correlation"].update(values={}, observations=0, source_sha256=label("verified empty roster"))
        na = {"status": "NOT_APPLICABLE_ARCHETYPE"}
        src["taker_timing"] = {"status": "MEASURED_PASS", "trigger_source": "fair"} if taker else dict(na)
        src["offshore_receipts"] = {"status": "MEASURED_PASS"} if taker else dict(na)
        for name in MAKER_GATES:
            src[name] = dict(na) if taker else {"status": "MEASURED_PASS"}
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
        self.approval = {"schema": "doppel.shadow-approval/v2", "commit_sha": self.commit, "genome_id": self.genome.id,
                         "archetype": self.kind, "statistics": _statistics(0, [returns, prices]),
                         "scoring_sha256": bound["code_sha256"], "registry_result_sha256": result["hash"],
                         "valid_from_ns": now - 10**9, "valid_until_ns": now + 60 * 10**9, "n_trials": 2,
                         "trial_head_sha256": prev, "candidate_trial_id": identities[0]["trial_id"]}
        self.trust = AdmissionTrust(self.private.public_key().public_bytes_raw(), reg.root, self.commit, plan_sha, ())
        self.registry = reg
        self.seal()

    def seal(self):
        src = self.gates["gates"]
        replay_hash = digest(encode(self.replay))
        report = {"sources": {"canonical.json": replay_hash, "trials.json": digest(encode(self.trials))},
                  "trials": {"n_trials": 2},
                  "candidates": [{"input": "canonical.json", "genome": {"id": self.genome.id},
                                  "decision": "PROMOTE", "base_decision": "PASS", "windows": 48}]}
        files = {"plan.json": self.plan, "trials.json": self.trials, "replay.json": self.replay,
                 "gates.json": self.gates, "promotion.json": report, **self.offshore}
        if self.genome.mode == "taker":
            hashes = {role: digest(encode(doc)) for name, doc in self.offshore.items()
                      for role in [name.removesuffix(".json")]}
            self.timing["source_receipts_sha256"].update(hashes)
            src["offshore_receipts"]["sources_sha256"] = hashes
            src["taker_timing"]["source_sha256"] = digest(encode(self.timing))
            files["layer5b.json"] = self.timing
        else:
            self.maker["replay_sha256"] = replay_hash
            for name in MAKER_GATES:
                src[name]["source_sha256"] = digest(encode(self.maker))
            files["maker.json"] = self.maker
        self.approval["files"] = {name: write(self.bundle / name, obj) for name, obj in files.items()}
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
    b = Bundle(tmp_path, taker=True)
    b.timing["subjects"]["scripted_lead_taker"].update(changes)
    b.seal()
    b.reject("REJECT_UNMEASURED_GATE")


def test_memory_pass_label_cannot_override_peak_rss_measurement(tmp_path):
    b = Bundle(tmp_path, taker=True)
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


def test_taker_requires_actual_offshore_receipt_and_exact_benchmark_links(tmp_path):
    b = Bundle(tmp_path, taker=True)
    (b.bundle / "binance.json").unlink()
    b.reject("REJECT_UNMEASURED_GATE")
    b.seal()
    b.timing["source_receipts_sha256"]["binance"] = label("different recorded Binance receipt")
    write(b.bundle / "layer5b.json", b.timing)
    b.approval["files"]["layer5b.json"] = digest(encode(b.timing))
    b.gates["gates"]["taker_timing"]["source_sha256"] = b.approval["files"]["layer5b.json"]
    b.approval["files"]["gates.json"] = write(b.bundle / "gates.json", b.gates)
    b.sign()
    b.reject("REJECT_UNMEASURED_GATE")


def test_maker_guard_requires_offshore_and_structural_twap_uses_maker_gates(tmp_path):
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    Bundle(guarded, offshore_guard=True).reject("REJECT_UNMEASURED_GATE")
    plain = tmp_path / "plain"
    plain.mkdir()
    result = Bundle(plain, maker_kind="structural_twap").admit()
    assert result["archetype"] == "structural_twap"
    assert result["gate_statuses"]["taker_timing"] == "NOT_APPLICABLE_ARCHETYPE"


def test_explicit_failure_and_statistic_tampering_are_rejected(tmp_path):
    b = Bundle(tmp_path)
    b.gates["gates"]["dsr"]["status"] = "MEASURED_FAIL"
    b.seal()
    b.reject("REJECT_GATE_FAILED")
    b.gates["gates"]["dsr"]["status"] = "MEASURED_PASS"
    b.approval["statistics"]["dsr"] -= .01
    b.seal()
    b.reject("REJECT_INCOMPLETE_LEDGER")


def test_promotion_cli_to_signed_maker_bundle_to_admission(tmp_path):
    b = Bundle(tmp_path)
    results = b.bundle
    report = tmp_path / "promotion"
    replay = results / "replay.json"
    ledger = results / "trials.json"
    # The reporting command consumes the same canonical replay later admitted.
    assert promotion.main(["--results", str(replay), "--trials", str(ledger), "--out", str(report)]) == 0
    (report / "plan.json").write_bytes((results / "plan.json").read_bytes())
    cuts = (b.approval["valid_from_ns"], b.approval["valid_until_ns"])
    product = build_shadow_bundle(report, replay, ledger, results / "gates.json",
                                  genome=b.genome, trust=b.trust, validity_ns=cuts)
    assert product == build_shadow_bundle(report, replay, ledger, results / "gates.json",
                                          genome=b.genome, trust=b.trust, validity_ns=cuts)
    assert product == replace(product, approval=encode(orjson.loads(product.approval)))
    output = tmp_path / "signed"
    genome = tmp_path / "candidate.json"
    write(genome, to_dict(b.genome))
    signing = tmp_path / "test-signing.key"
    signing.touch(mode=0o600)
    signing.write_bytes(b.private.private_bytes_raw())
    assert cli.main(["promote", "bundle", "--promotion-dir", str(report), "--replay-artifact", str(replay),
                     "--trial-ledger", str(ledger), "--gate-results", str(results / "gates.json"), "--genome", str(genome),
                     "--registry-root", str(b.trust.registry_root), "--out", str(output), "--commit-sha", b.commit,
                     "--plan-sha256", b.trust.plan_sha256, "--public-key-hex", b.trust.public_key.hex(),
                     "--valid-from-ns", str(cuts[0]), "--valid-until-ns", str(cuts[1]), "--signing-key", str(signing)]) == 0
    assert (output / "approval.json").read_bytes() == product.approval
    receipt = admit_shadow(b.genome, output, b.trust, tmp_path / "live-owned-receipt",
                           simulation_sha256=simulation_digest(b.cfg))
    assert receipt["state"] == "SHADOW_ACTIVE"
    assert receipt["archetype"] == "maker" and receipt["n_trials"] == 2
    assert receipt["gates"]["maker_queue_fill"]["verified_fills"] == 60
    assert receipt["gate_statuses"]["taker_timing"] == "NOT_APPLICABLE_ARCHETYPE"
    assert not (output / "layer5b.json").exists()
