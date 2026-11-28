"""Verify operator-approved evidence before admitting a genome to shadow.

Research labels carry no authorization. The intake service gets its trust settings from
the release operator, separately from the applicant's bundle. A signed closure attests
that the trial inventory was complete, including every attempt outside the registry.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import math
import os
from pathlib import Path
import stat
import tempfile
import time

import orjson

from ..lab import promotion
from ..lab.layer5b import RECORDING_STREAMS
from ..twin.cli import code_sha256
from ..twin.engine import SimConfig
from ..twin.evaluate import VERDICT_OPTIONS
from ..twin.verdict import block_bootstrap_p
from .genome import Genome
from .registry import Registry, protocol_matches
from .shadow_archetypes import archetype, maker_evidence, requires_offshore
from .shadow_probability import MAX_BYTES as MAX_MODEL_BYTES, parse_probability

EVIDENCE_FILES = ("plan.json", "trials.json", "replay.json", "gates.json", "promotion.json")
OFFSHORE_ROLES = ("binance", "hyperliquid")
OFFSHORE_FILES = {f"{role}.json" for role in OFFSHORE_ROLES}
FEE_GRID = {"api_live_rebated": 0.02085, "api_base_0695": 0.0695, "maker_0": 0.0}
REPORTING_GRID = {**FEE_GRID, "gateway_0095": 0.095}
SIZES = (25, 100, 500)
COMMON_GATES = frozenset(("dsr", "pbo", "fdr", "hac", "bootstrap", "promo_edge", "markout_2s", "markout_10s",
                          "paper_replay_slippage", "residual_correlation"))
MAKER_GATES = frozenset(("maker_queue_fill", "adverse_selection_stress"))
GATES = COMMON_GATES | MAKER_GATES | {"taker_timing", "offshore_receipts"}
SIGNATURE_DOMAIN = b"DOPPEL-SHADOW-APPROVAL-v2\0"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
REJECT_FOR = {"plan.json": "REJECT_INCOMPLETE_LEDGER", "trials.json": "REJECT_INCOMPLETE_LEDGER",
              "replay.json": "REJECT_NONCANONICAL_REPLAY", "promotion.json": "REJECT_NONCANONICAL_REPLAY"}


class AdmissionRejected(ValueError):
    """A preflight rejection that grants no runtime authority."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code


@dataclass(frozen=True)
class AdmissionTrust:
    public_key: bytes
    registry_root: Path
    commit_sha: str
    plan_sha256: str
    active_genomes: tuple[str, ...]


def _canon(doc) -> bytes:
    return orjson.dumps(doc, option=orjson.OPT_SORT_KEYS)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def simulation_digest(cfg: SimConfig) -> str:
    """Digest of the executable paper configuration and its bound replay representation."""
    return _sha(_canon(asdict(cfg)))


def _require(condition, code: str, why: str):
    if not condition:
        raise AdmissionRejected(code, why)


def _hex(raw, size=64):
    return isinstance(raw, str) and len(raw) == size and all(x in "0123456789abcdef" for x in raw)


def _num(value):
    return type(value) in (int, float) and math.isfinite(value)


def _int(value, minimum=0):
    return type(value) is int and value >= minimum


def _read(path: Path, limit=MAX_ARTIFACT_BYTES) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("expected a bounded regular evidence file")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("evidence grew past the bound")
        return data


def _json(raw):
    doc = orjson.loads(raw)
    if not isinstance(doc, dict) or raw not in (_canon(doc), _canon(doc) + b"\n"):
        raise ValueError("expected unambiguous canonical JSON")
    return doc


def _approval(bundle, trust, genome):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    code = "REJECT_UNTRUSTED_APPROVAL"
    try:
        raw = _read(bundle / "approval.json", 65536)
        approval = _json(raw)
        signature = _read(bundle / "approval.sig", 64)
        Ed25519PublicKey.from_public_bytes(trust.public_key).verify(signature, SIGNATURE_DOMAIN + raw)
    except (OSError, ValueError, InvalidSignature) as exc:
        raise AdmissionRejected(code, "missing or invalid lead approval") from exc
    _verify_approval_metadata(approval, trust, genome)
    return approval, _sha(raw)


def _verify_approval_metadata(approval, trust, genome, *, require_current=True):
    code = "REJECT_UNTRUSTED_APPROVAL"
    try:
        kind = archetype(genome)
        offshore = requires_offshore(genome)
    except ValueError as exc:
        raise AdmissionRejected(code, str(exc)) from exc
    now = time.time_ns()
    scoring = code_sha256()
    _require(approval.get("schema") == "doppel.shadow-approval/v2" and approval.get("genome_id") == genome.id
             and approval.get("archetype") == kind
             and _hex(trust.commit_sha, 40) and approval.get("commit_sha") == trust.commit_sha
             and approval.get("scoring_sha256") == scoring,
             code, "approval does not match this genome, archetype and evaluator release")
    _require("trial_ledger_schema" not in approval
             or approval["trial_ledger_schema"] == promotion.ABSTENTION_LEDGER_SCHEMA,
             code, "unsupported trial ledger policy in approval")
    start, end = approval.get("valid_from_ns"), approval.get("valid_until_ns")
    _require(_int(start, 1) and _int(end, 1) and start < end and (not require_current or start <= now < end),
             code, "approval is outside its validity interval")
    reference = approval.get("files")
    _require(isinstance(reference, dict) and all(_hex(digest) for digest in reference.values()),
             code, "approval has invalid artifact digests")
    common = set(EVIDENCE_FILES)
    specific = {"layer5b.json", "taker-measurements.json"} if genome.mode == "taker" else {"maker.json"}
    for name in common - set(reference):
        raise AdmissionRejected(REJECT_FOR.get(name, "REJECT_UNMEASURED_GATE"), f"missing {name} in approval")
    _require(specific <= set(reference), "REJECT_UNMEASURED_GATE", "missing archetype evidence")
    extra = set(reference) - common - specific - {"probability.json"}
    _require(extra in (set(), OFFSHORE_FILES) and (not offshore or extra == OFFSHORE_FILES),
             "REJECT_UNMEASURED_GATE", "offshore evidence requires both canonical source receipts")


def _artifact(bundle, approval, name, code):
    try:
        raw = _read(bundle / name, MAX_MODEL_BYTES if name == "probability.json" else MAX_ARTIFACT_BYTES)
        _require(_sha(raw) == approval["files"][name], code, f"{name} differs from the signed evidence")
        return _decode_evidence(name, raw)
    except AdmissionRejected:
        raise
    except (OSError, ValueError) as exc:
        raise AdmissionRejected(code, f"missing or invalid {name}") from exc


def _decode_evidence(name: str, raw: bytes) -> dict:
    if name in OFFSHORE_FILES or name in {"layer5b.json", "promotion.json"}:
        result = orjson.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("evidence must be a JSON object")
        return result
    return _json(raw)


def _window_key(row):
    start, end = row["start"], row["end"]
    if not _int(start) or not _int(end) or start >= end:
        raise ValueError("expected whole-second market interval")
    return start, end


def _canonical_replay(genome, trust, approval, replay, simulation_sha256):
    code = "REJECT_NONCANONICAL_REPLAY"
    _require(replay.get("schema") == "doppel.shadow-replay/v1" and replay.get("genome_id") == genome.id
             and replay.get("commit_sha") == trust.commit_sha,
             code, "replay schema, genome or release mismatch")
    registry = Registry(trust.registry_root)
    registry_bytes = registry.path.read_bytes()
    _require(bool(registry_bytes) and not registry.verify(), code, "registry integrity failed")
    reg = registry.registration(genome.id)
    binding = registry.protocol_binding(genome.id)
    records = registry.records()
    result = next((row for row in records if row.get("kind") == "result" and row.get("genome_id") == genome.id
                   and row.get("binding") is True), None)
    _require(reg is not None and binding is not None and result is not None
             and registry.path.read_bytes() == registry_bytes,
             code, "replay has no stable canonical registry binding")
    assert reg is not None and binding is not None and result is not None
    verdict = replay["verdict"]
    evaluation = verdict["evaluation"]
    win = reg["eval_window"]
    start, end = win["start_s"], win["end_s"]
    span = [start, end]
    # Result evidence is the registry's first binding replay, never an imported PASS.
    _require(result["hash"] == approval.get("registry_result_sha256") and result["data_range"] == span
             and result.get("protocol_unbound") is False and result["verdict"] == verdict
             and verdict.get("p_source") == "twin-replay" and verdict.get("decision") == "PASS"
             and evaluation.get("range") == span and evaluation.get("protocol_unbound") is False
             and evaluation.get("code_sha256") == approval["scoring_sha256"]
             and _hex(evaluation.get("input_sha256")) and protocol_matches(binding, evaluation)
             and _hex(simulation_sha256) and _sha(_canon(evaluation.get("simulation"))) == simulation_sha256,
             code, "binding verdict, sealed input or executable configuration mismatch")
    _require(result["tw"] <= approval["valid_from_ns"], code, "approval predates the registry replay")
    windows, returns = [], []
    for row in replay["windows"]:
        if type(row.get("eligible")) is not bool:
            raise AdmissionRejected(code, "window does not state eligibility")
        if row["eligible"]:
            t = _window_key(row)
            _require(start <= t[0] < t[1] <= end and _num(row.get("net_c"))
                     and (not windows or windows[-1][1] <= t[0]),
                     code, "eligible windows must be unique, ordered and nonoverlapping")
            windows.append(t)
            returns.append(row["net_c"])
    _require(len(windows) >= 2 * promotion.PBO_S, code, "too few eligible windows for the registered PBO partitions")
    observed = block_bootstrap_p(returns, VERDICT_OPTIONS["block"], VERDICT_OPTIONS["n_boot"], VERDICT_OPTIONS["seed"])
    stated = verdict.get("p_value")
    # The existing registry verdict rounds its p-value for storage.
    _require(_num(stated) and 0 <= stated <= 1 and observed is not None and abs(observed - stated) <= 0.00000051,
             code, "window returns do not reproduce the registered replay p-value")
    return reg, records, windows, returns


def _complete_ledger(genome, trust, approval, plan, ledger, reg, records, windows, returns, replay):
    code = "REJECT_INCOMPLETE_LEDGER"
    expected = plan.get("attempts")
    trials = ledger.get("trials")
    start = reg["eval_window"]["start_s"]
    schema = approval.get("trial_ledger_schema", promotion.CANONICAL_LEDGER_SCHEMA)
    v2 = schema == promotion.ABSTENTION_LEDGER_SCHEMA
    plan_schema = promotion.ABSTENTION_PLAN_SCHEMA if v2 else "doppel.shadow-trial-plan/v1"
    _require(_hex(trust.plan_sha256) and approval["files"]["plan.json"] == trust.plan_sha256
             and plan.get("schema") == plan_schema and plan.get("family") == reg["family"]
             and _int(plan.get("frozen_ns"), 1) and plan["frozen_ns"] < start * 10**9
             and ledger.get("schema") == schema
             and ledger.get("plan_sha256") == trust.plan_sha256
             and isinstance(expected, list) and isinstance(trials, list)
             and _int(approval.get("n_trials"), 2) and len(expected) == len(trials) == approval["n_trials"]
             and ledger.get("windows") == [list(window) for window in windows],
             code, "plan, exact trial count or aligned window inventory differs from the approved record")
    ids, attempts, previous, values = set(), set(), "0" * 64, []
    candidate, candidate_id = None, approval.get("candidate_trial_id")
    family = {r["genome_id"] for r in records if r.get("kind") == "register" and r.get("family") == reg["family"]}
    coverage = set()
    for i, (trial, item) in enumerate(zip(trials, expected, strict=True)):
        _require(isinstance(trial, dict) and isinstance(item, dict), code, "invalid trial row")
        identity = {key: trial.get(key) for key in ("trial_id", "genome_id", "subject_id", "variant_id")}
        rows = trial.get("returns_c")
        body = {key: v for key, v in trial.items() if key != "sha256"}
        key = (identity["genome_id"], identity["subject_id"], identity["variant_id"])
        _require(item == identity and all(_hex(value) for value in identity.values())
                 and identity["trial_id"] not in ids and key not in attempts
                 and _int(trial.get("seq")) and trial["seq"] == i and trial.get("previous_sha256") == previous
                 and trial.get("sha256") == _sha(_canon(body))
                 and isinstance(rows, list) and len(rows) == len(windows) and all(_num(v) for v in rows),
                 code, "trial ledger row is missing, duplicated, unaligned, unhashed or disconnected")
        previous = trial["sha256"]
        ids.add(identity["trial_id"])
        attempts.add(key)
        coverage.add(identity["genome_id"])
        values.append(rows)
        if identity["trial_id"] == candidate_id:
            _require(identity["genome_id"] == genome.id and rows == returns, code,
                     "candidate trial differs from its canonical replay")
            _require(not v2 or (trial.get("disposition") == "PASS" and replay.get("trial_id") == candidate_id
                                and trial.get("replay_sha256") == approval["files"]["replay.json"]),
                     code, "candidate trial did not bind the exported measured PASS replay")
            candidate = i
    _require(previous == approval.get("trial_head_sha256") and candidate is not None and family <= coverage,
             code, "closure, candidate, or registered family inventory is incomplete")
    try:
        input_sha256 = replay["verdict"]["evaluation"]["input_sha256"]
        abstentions = promotion.verified_abstentions(ledger, input_sha256, plan=plan) if v2 else None
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        raise AdmissionRejected(code, str(exc)) from exc
    _require(abstentions is None or all(trials[i]["genome_id"] not in family for i in abstentions),
             code, "fixed no-entry controls cannot replace a registered strategy trial")
    return candidate, values, abstentions


def _statistics(candidate, values, *, abstentions: set[int] | None = None):
    try:
        stats = promotion.canonical_trial_statistics(values, abstentions=abstentions or frozenset())[2][candidate]
    except ValueError as exc:
        raise AdmissionRejected("REJECT_UNMEASURED_GATE", str(exc)) from exc
    if abstentions is not None:
        stats["verified_abstentions"] = len(abstentions)
    return stats


def _scenarios(replay):
    code = "REJECT_NONCANONICAL_REPLAY"
    result = {}
    for row in replay["scenarios"]:
        tier, size = row.get("fee_tier"), row.get("size")
        key = (tier, size)
        attempted, filled, net = row.get("attempted"), row.get("filled"), row.get("net_c")
        _require(tier in REPORTING_GRID and _int(size, 1) and size in SIZES and key not in result
                 and _num(row.get("coefficient")) and row["coefficient"] == REPORTING_GRID[tier]
                 and row.get("execution") == "l2_book_walk_vwap"
                 and _int(attempted, 1) and _int(filled) and filled <= attempted and _num(net),
                 code, "invalid fee/depth scenario or unavailable capacity denominator")
        result[key] = {"fee_tier": tier, "size": size, "net_c": net, "attempted": attempted,
                       "filled": filled, "fill_rate": filled / attempted}
    _require({(tier, size) for tier in FEE_GRID for size in SIZES} <= set(result), code, "incomplete 3×3 fee/depth grid")
    return list(result.values())


def _measured_gates(genome, trust, evidence, approved, replay, windows):
    code = "REJECT_UNMEASURED_GATE"
    doc = evidence["gates.json"]
    src = doc.get("gates")
    has_offshore = OFFSHORE_FILES <= set(evidence)
    applicable = COMMON_GATES | ({"taker_timing"} if genome.mode == "taker" else MAKER_GATES)
    if has_offshore:
        applicable = applicable | {"offshore_receipts"}
    _require(doc.get("schema") == "doppel.shadow-gates/v2" and doc.get("genome_id") == genome.id
             and doc.get("archetype") == approved["archetype"]
             and isinstance(src, dict) and set(src) == GATES
             and all(isinstance(row, dict) for row in src.values()), code, "incomplete v3 gate map")
    for name, row in src.items():
        if name in applicable:
            _require(row.get("status") != "MEASURED_FAIL", "REJECT_GATE_FAILED", f"{name} measured a failure")
            _require(row.get("status") == "MEASURED_PASS", code, f"{name} is unmeasured")
        else:
            _require(row == {"status": "NOT_APPLICABLE_ARCHETYPE"}, code, f"invalid inapplicable gate: {name}")
    result = {}
    external = ("markout_2s", "markout_10s", "paper_replay_slippage")
    for name in external:
        row = src[name]
        _require(_num(row.get("value")) and _int(row.get("observations"), 1)
                 and _hex(row.get("source_sha256")), code, f"{name} has no finite measurement provenance")
        result[name] = {"value": row["value"], "observations": row["observations"], "source_sha256": row["source_sha256"]}
    cor = src["residual_correlation"]
    active = trust.active_genomes
    _require(len(set(active)) == len(active) and all(_hex(v) and v != genome.id for v in active),
             code, "invalid operator active-genome inventory")
    vals = cor.get("values")
    _require(isinstance(vals, dict) and set(vals) == set(active) and _hex(cor.get("source_sha256"))
             and (not active or _int(cor.get("observations"), 3))
             and all(_num(r) and -1 <= r <= 1 for r in vals.values()), code,
             "correlation evidence does not cover the operator's full active set")
    result["residual_correlation"] = {"values": vals, "source_sha256": cor["source_sha256"],
                                      "observations": cor.get("observations")}
    if has_offshore:
        coverage = windows if requires_offshore(genome) and genome.mode == "maker" else ()
        result["offshore_receipts"] = _offshore_proofs(src["offshore_receipts"], evidence, approved, coverage)
    if genome.mode == "taker":
        doc = evidence["taker-measurements.json"]
        digest = approved["files"]["taker-measurements.json"]
        measurements = doc.get("measurements")
        names = (*external, "residual_correlation")
        _require(doc.get("schema") == "doppel.shadow-taker-measurements/v1"
                 and doc.get("genome_id") == genome.id and doc.get("replay_sha256") == approved["files"]["replay.json"]
                 and isinstance(measurements, dict) and set(measurements) == set(names),
                 code, "taker measurements do not bind the sealed genome and replay")
        for name in names:
            expected = {k: v for k, v in result[name].items() if k != "source_sha256"}
            _require(result[name]["source_sha256"] == digest and measurements[name] == expected,
                     code, f"{name} disagrees with its signed measurement artifact")
        result["taker_timing"] = _taker_timing(genome, src, evidence["layer5b.json"], approved)
    else:
        maker_digest = approved["files"]["maker.json"]
        _require(all(src[name].get("source_sha256") == maker_digest for name in MAKER_GATES),
                 code, "maker gates must reference the signed queue, fill and stress evidence")
        _require(all(result[name]["value"] > 0 for name in ("markout_2s", "markout_10s")),
                 "REJECT_GATE_FAILED", "maker post-fill markouts must be positive")
        evaluation = replay["verdict"]["evaluation"]
        try:
            maker = maker_evidence(genome, evidence["maker.json"], approved["files"]["replay.json"],
                                   evaluation["input_sha256"], expected_windows=len(windows),
                                   base_cancel_latency_ms=evaluation["simulation"]["latency_ms"])
        except ValueError as exc:
            raise AdmissionRejected("REJECT_MAKER_EVIDENCE", str(exc)) from exc
        for name in ("markout_2s", "markout_10s"):
            fields = ("value", "observations", "source_sha256")
            _require(all(maker[name][key] == result[name][key] for key in fields),
                     code, f"{name} differs from the signed maker receipt")
        result["maker_queue_fill"] = maker["queue"]
        result["adverse_selection_stress"] = maker["stress"]
    return result


def _offshore_proofs(gate, evidence, approved, windows):
    code = "REJECT_UNMEASURED_GATE"
    hashes = {role: approved["files"][role + ".json"] for role in OFFSHORE_ROLES}
    _require(gate.get("sources_sha256") == hashes, code, "offshore gate does not name both signed source receipts")
    seen = set()
    for role in OFFSHORE_ROLES:
        doc = evidence[role + ".json"]
        segments = doc.get("segments")
        _require(doc.get("schema") == "doppel.layer5b-source/v1" and doc.get("role") == role
                 and doc.get("clock") == "local_receive_ns" and isinstance(segments, list) and segments,
                 code, f"invalid native {role} receipt")
        spans = []
        for segment in segments:
            digest, path, covered = (segment.get(k) for k in ("sha256", "path", "covered_range_ns"))
            _require(_hex(digest) and digest not in seen
                     and isinstance(path, str) and path and not Path(path).is_absolute()
                     and ".." not in Path(path).parts and path.endswith(".dtape.zst")
                     and segment.get("stream") in RECORDING_STREAMS[role]
                     and isinstance(covered, list) and len(covered) == 2
                     and all(_int(t, 1) for t in covered) and covered[0] < covered[1],
                     code, f"invalid or reused {role} segment proof")
            seen.add(digest)
            spans.append(covered)
        merged: list[list[int]] = []
        for left, right in sorted(spans):
            if merged and left <= merged[-1][1]:
                merged[-1][1] = max(right, merged[-1][1])
            else:
                merged.append([left, right])
        _require(all(any(left <= start * 10**9 and end * 10**9 <= right for left, right in merged)
                     for start, end in windows), code, f"{role} receipts do not cover the maker's offshore guard replay")
    return hashes


def _taker_timing(genome, src, timing, approved):
    code = "REJECT_UNMEASURED_GATE"
    sources = timing.get("source_receipts_sha256")
    subjects = timing.get("subjects")
    rss = timing.get("peak_rss_bytes")
    names = {"scripted_lead_taker": "lead", "scripted_fair_taker": "fair", "random_taker_control": None}
    _require(src["taker_timing"].get("source_sha256") == approved["files"]["layer5b.json"]
             and timing.get("schema") == "doppel.layer5b-score/v1" and timing.get("source_kind") == "recorded"
             and timing.get("status") == "MEASURED_PASS" and timing.get("software_status") == "PASS"
             and _hex(timing.get("plan_sha256")) and _hex(timing.get("forecast_sha256"))
             and isinstance(sources, dict) and {"books", "brti", "binance", "hyperliquid"} <= set(sources)
             and all(_hex(h) for h in sources.values())
             and all(sources[role] == approved["files"][role + ".json"] for role in OFFSHORE_ROLES)
             and isinstance(rss, dict) and rss.get("limit") == 1_500_000_000 and rss.get("under_limit") is True
             and all(_int(rss.get(k), 1) and rss[k] < 1_500_000_000 for k in ("forecast", "score"))
             and isinstance(subjects, dict) and set(subjects) == set(names),
             code, "the real-recorded Layer 5b proof is missing or incomplete")
    for subject, trigger in names.items():
        row = subjects[subject]
        _require(row.get("zero_leakage") is True and _int(row.get("truth"), 1),
                 code, f"{subject} has no leakage-tested truth denominator")
        if trigger is None:
            _require(row.get("status") == row.get("model_status") == "ABSTAIN"
                     and _int(row.get("predicted")) and row["predicted"] == 0
                     and _int(row.get("matched")) and row["matched"] == 0,
                     code, "the random taker control did not demonstrably abstain")
        else:
            _require(row.get("status") == "PASS" and row.get("model_status") == "FIT"
                     and row.get("trigger_source") == trigger
                     and _int(row.get("truth"), 20) and _int(row.get("truth_markets"), 10)
                     and row["truth_markets"] <= row["truth"]
                     and _int(row.get("matched"), 1) and _int(row.get("predicted"), 1)
                     and row["matched"] <= min(row["predicted"], row["truth"])
                     and all(_num(row.get(k)) and 0.90 <= row[k] <= 1 for k in ("precision", "recall", "f1"))
                     and _num(row.get("median_timing_delta_ns")) and abs(row["median_timing_delta_ns"]) <= 10**9
                     and _num(row.get("median_absolute_delta_ns")) and 0 <= row["median_absolute_delta_ns"] <= 10**9,
                     code, f"{subject} lacks measured timing recovery within one second")
            m, p, n = row["matched"], row["predicted"], row["truth"]
            _require(all(math.isclose(row[k], v, abs_tol=0.00000051) for k, v in
                         (("precision", m / p), ("recall", m / n), ("f1", 2 * m / (p + n)))),
                     code, f"{subject} timing metrics contradict its matched entries")
    family = genome.evidence.get("taker_trigger_source")
    _require(family in ("lead", "fair") and src["taker_timing"].get("trigger_source") == family,
             code, "the registered taker must use the same measured trigger family as its timing evidence")
    return {"source_sha256": src["taker_timing"]["source_sha256"],
            "subjects": subjects, "plan_sha256": timing["plan_sha256"],
            "candidate_trigger_source": family, "peak_rss_bytes": rss}


def _issue(path, receipt):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Commit complete bytes atomically without replacing an existing admission.
    raw = _canon(receipt) + b"\n"
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent) as tmp:
            tmp.write(raw)
            tmp.flush()
            os.fsync(tmp.fileno())
            try:
                os.link(tmp.name, path, follow_symlinks=False)
            except FileExistsError:
                _require(_read(path) == raw, "REJECT_RECEIPT_CONFLICT", "existing admission differs")
            fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError as exc:
        raise AdmissionRejected("REJECT_RECEIPT_IO", "cannot persist the admission receipt") from exc


def _check_promotion(report, approved, windows, stats):
    code = "REJECT_NONCANONICAL_REPLAY"
    rows = report.get("candidates")
    sources = report.get("sources")
    _require(isinstance(rows, list) and isinstance(sources, dict)
             and approved["files"]["trials.json"] in sources.values()
             and report.get("trials", {}).get("n_trials") == approved["n_trials"], code,
             "promotion result does not state the verified trial inventory")
    if approved.get("trial_ledger_schema") == promotion.ABSTENTION_LEDGER_SCHEMA:
        _require(report.get("trials", {}).get("ledger_schema") == promotion.ABSTENTION_LEDGER_SCHEMA
                 and _int(report.get("pbo", {}).get("verified_abstentions"))
                 and report["pbo"]["verified_abstentions"] == stats["verified_abstentions"],
                 code, "promotion report differs from the signed trial ledger policy and abstention count")
    matches = [row for row in rows if (row.get("genome") or {}).get("id") == approved["genome_id"]]
    _require(len(matches) == 1 and matches[0].get("decision") == "PROMOTE"
             and matches[0].get("base_decision") == "PASS" and matches[0].get("windows") == len(windows)
             and sources.get(matches[0].get("input")) == approved["files"]["replay.json"],
             code, "promotion lacks a passing row tied to the exact canonical replay")
    row = matches[0]
    fields = (("dsr", "dsr", "dsr"), ("pbo", "pbo", "pbo"), ("fdr", "bh_q", "fdr_q"),
              ("hac", "t", "hac_t"), ("hac", "p_one_sided", "hac_p"),
              ("bootstrap", "p_one_sided", "bootstrap_p"))
    for section, field, name in fields:
        claimed = row.get(section, {}).get(field)
        _require(_num(claimed) and math.isclose(claimed, stats[name], rel_tol=0, abs_tol=5.1e-7),
                 code, f"promotion {name} disagrees with the canonical trial ledger")
    _require(row.get("fdr", {}).get("bh_survives") is stats["bh_survives"],
             code, "promotion FDR decision disagrees with the canonical trial ledger")


def _verified_evidence(genome, trust, approved, evidence, simulation_sha256):
    try:
        replay = evidence["replay.json"]
        reg, records, windows, returns = _canonical_replay(genome, trust, approved, replay, simulation_sha256)
        candidate, values, abstentions = _complete_ledger(
            genome, trust, approved, evidence["plan.json"], evidence["trials.json"], reg, records, windows,
            returns, replay)
        scenarios = _scenarios(replay)
        measured = _measured_gates(genome, trust, evidence, approved, replay, windows)
        stats = _statistics(candidate, values, abstentions=abstentions)
        _check_promotion(evidence["promotion.json"], approved, windows, stats)
        if "probability.json" in evidence:
            try:
                parse_probability(evidence["probability.json"], approved["files"]["probability.json"],
                                  genome, before_ns=reg["eval_window"]["start_s"] * 10**9)
            except (ValueError, KeyError, OverflowError) as exc:
                raise AdmissionRejected("REJECT_UNMEASURED_GATE", f"invalid candidate probability: {exc}") from exc
    except AdmissionRejected:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError, OverflowError) as exc:
        raise AdmissionRejected("REJECT_INVALID_EVIDENCE", "malformed evidence or failed statistical computation") from exc
    code = "REJECT_GATE_FAILED"
    _require(stats["dsr"] >= promotion.DSR_MIN and stats["pbo"] < promotion.PBO_MAX
             and stats["bh_survives"] and stats["hac_t"] > 0 and stats["hac_p"] <= 0.05
             and stats["bootstrap_p"] <= 0.05, code, "required DSR, PBO, BH, HAC or bootstrap threshold failed")
    _require(any(r["fee_tier"] == "api_live_rebated" and r["net_c"] > 0 and r["fill_rate"] >= 0.5 for r in scenarios),
             code, "no live-fee depth size was profitable with >= 50% completion")
    _require(all(measured[k]["value"] >= 0 for k in ("markout_2s", "markout_10s"))
             and abs(measured["paper_replay_slippage"]["value"]) <= 0.3
             and all(r < 0.30 for r in measured["residual_correlation"]["values"].values()),
             code, "short-horizon markout, paper slippage or diversification threshold failed")
    return reg["eval_window"]["start_s"] * 10**9, scenarios, measured, stats


def _statistics_match(stated, stats):
    return (isinstance(stated, dict) and set(stated) == set(stats)
            and all((type(stated[k]) is type(v) and stated[k] == v)
                    if type(v) in (bool, int) else (_num(stated[k]) and math.isclose(stated[k], v, abs_tol=1e-10))
                    for k, v in stats.items()))


def admit_shadow(genome: Genome, bundle: Path, trust: AdmissionTrust, receipt_dir: Path, *,
                 simulation_sha256: str, require_probability: bool = False) -> dict:
    """Validate all proofs and issue an immutable receipt."""
    approved, approval_hash = _approval(bundle, trust, genome)
    _require(not require_probability or "probability.json" in approved["files"],
             "REJECT_UNMEASURED_GATE", "live Shadow requires a signed candidate probability model")
    evidence = {name: _artifact(bundle, approved, name, REJECT_FOR.get(name, "REJECT_UNMEASURED_GATE"))
                for name in approved["files"]}
    start_ns, scenarios, measured, stats = _verified_evidence(genome, trust, approved, evidence, simulation_sha256)
    _require(_statistics_match(approved.get("statistics"), stats), "REJECT_INCOMPLETE_LEDGER",
             "signed statistics differ from the independently recomputed trial evidence")
    _require(time.time_ns() < approved["valid_until_ns"], "REJECT_UNTRUSTED_APPROVAL", "approval expired during admission")
    body = {"schema": "doppel.shadow-admission/v2", "state": "SHADOW_ACTIVE", "genome_id": genome.id,
            "archetype": approved["archetype"], "commit_sha": trust.commit_sha,
            "scoring_sha256": approved["scoring_sha256"], "replay_start_ns": start_ns,
            "simulation_sha256": simulation_sha256, "approval_sha256": approval_hash,
            "valid_until_ns": approved["valid_until_ns"], "registry_result_sha256": approved["registry_result_sha256"],
            "replay_sha256": approved["files"]["replay.json"], "trial_plan_sha256": trust.plan_sha256,
            "trial_ledger_sha256": approved["files"]["trials.json"], "trial_head_sha256": approved["trial_head_sha256"],
            "promotion_sha256": approved["files"]["promotion.json"],
            "probability_sha256": approved["files"].get("probability.json"),
            "n_trials": stats["trial_count"], "scenario_grid": scenarios, "gates": {**stats, **measured},
            "gate_statuses": {k: row["status"] for k, row in evidence["gates.json"]["gates"].items()}}
    if "trial_ledger_schema" in approved:
        body["trial_ledger_schema"] = approved["trial_ledger_schema"]
    receipt = {**body, "receipt_sha256": _sha(_canon(body))}
    _issue(receipt_dir / genome.id / "admission_receipt.json", receipt)
    return receipt
