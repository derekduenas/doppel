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

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import numpy as np
import orjson

from ..lab import promotion
from ..twin.cli import code_sha256
from ..twin.engine import SimConfig
from ..twin.evaluate import VERDICT_OPTIONS
from ..twin.verdict import block_bootstrap_p
from .genome import Genome
from .registry import Registry, protocol_matches

EVIDENCE_FILES = ("plan.json", "trials.json", "replay.json", "gates.json", "layer5b.json")
FEE_GRID = {"api_live_rebated": 0.02085, "api_base_0695": 0.0695, "maker_0": 0.0}
REPORTING_GRID = {**FEE_GRID, "gateway_0095": 0.095}
SIZES = (25, 100, 500)
GATES = frozenset(("dsr", "pbo", "fdr", "hac", "bootstrap", "promo_edge", "markout_2s", "markout_10s",
                   "paper_replay_slippage", "residual_correlation", "taker_timing"))
SIGNATURE_DOMAIN = b"DOPPEL-SHADOW-APPROVAL-v1\0"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024


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
    code = "REJECT_UNTRUSTED_APPROVAL"
    try:
        raw = _read(bundle / "approval.json", 65536)
        approval = _json(raw)
        signature = _read(bundle / "approval.sig", 64)
        Ed25519PublicKey.from_public_bytes(trust.public_key).verify(signature, SIGNATURE_DOMAIN + raw)
    except (OSError, ValueError, InvalidSignature) as exc:
        raise AdmissionRejected(code, "missing or invalid lead approval") from exc
    now = time.time_ns()
    scoring = code_sha256()
    _require(approval.get("schema") == "doppel.shadow-approval/v1" and approval.get("genome_id") == genome.id
             and _hex(trust.commit_sha, 40) and approval.get("commit_sha") == trust.commit_sha
             and approval.get("scoring_sha256") == scoring,
             code, "approval does not match this genome and evaluator release")
    start, end = approval.get("valid_from_ns"), approval.get("valid_until_ns")
    _require(isinstance(start, int) and isinstance(end, int) and _int(start) and _int(end) and start <= now < end,
             code, "approval is outside its validity interval")
    reference = approval.get("files")
    _require(isinstance(reference, dict) and set(reference) == set(EVIDENCE_FILES)
             and all(_hex(digest) for digest in reference.values()), code, "approval has incomplete artifact inventory")
    return approval, _sha(raw)


def _artifact(bundle, approval, name, code):
    try:
        raw = _read(bundle / name)
        _require(_sha(raw) == approval["files"][name], code, f"{name} differs from the signed evidence")
        # The benchmark writer emits indented JSON. Its exact bytes are signed by digest.
        return orjson.loads(raw) if name == "layer5b.json" else _json(raw)
    except AdmissionRejected:
        raise
    except (OSError, ValueError) as exc:
        raise AdmissionRejected(code, f"missing or invalid {name}") from exc


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


def _complete_ledger(genome, trust, approval, plan, ledger, reg, records, windows, returns):
    code = "REJECT_INCOMPLETE_LEDGER"
    expected = plan.get("attempts")
    trials = ledger.get("trials")
    start = reg["eval_window"]["start_s"]
    _require(_hex(trust.plan_sha256) and approval["files"]["plan.json"] == trust.plan_sha256
             and plan.get("schema") == "doppel.shadow-trial-plan/v1" and plan.get("family") == reg["family"]
             and _int(plan.get("frozen_ns"), 1) and plan["frozen_ns"] < start * 1e9
             and ledger.get("schema") == "doppel.shadow-trial-ledger/v1"
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
            candidate = i
    _require(previous == approval.get("trial_head_sha256") and candidate is not None and family <= coverage,
             code, "closure, candidate, or registered family inventory is incomplete")
    return candidate, values


def _statistics(candidate, values):
    code = "REJECT_UNMEASURED_GATE"
    arr = np.asarray(values, dtype=float)
    n, size = arr.shape
    moments = [promotion.moments(rows) for rows in arr]
    _require(all(m is not None and all(_num(v) for v in m) for m in moments),
             code, "a planned trial lacks estimable return moments")
    sharpe = np.asarray([m[0] / m[1] for m in moments if m is not None])
    variance = max(float(sharpe.var(ddof=1)), 1 / (size - 1))
    m = moments[candidate]
    assert m is not None
    dsr = promotion.psr(float(sharpe[candidate]), promotion.expected_max_sr(n, variance), size, m[2], m[3])
    pbo = promotion.pbo(arr.T, promotion.PBO_S)["pbo"]
    pvals = [block_bootstrap_p(rows, VERDICT_OPTIONS["block"], VERDICT_OPTIONS["n_boot"], VERDICT_OPTIONS["seed"])
             for rows in arr]
    _require(all(p is not None and _num(p) for p in pvals), code, "trial p-values are unavailable")
    fs, survivors = promotion.bh([p for p in pvals if p is not None])
    rows = arr[candidate]
    hbw, _ = promotion.andrews_bandwidth(rows)
    hvar = promotion.hac_var(rows, hbw)
    t = float(np.mean(rows)) / math.sqrt(hvar / size) if hvar > 0 else float("nan")
    hp = math.erfc(t / math.sqrt(2)) / 2
    _require(all(_num(v) for v in (dsr, pbo, fs[candidate], t, hp)), code, "a required statistic is undefined")
    return {"dsr": dsr, "pbo": pbo, "fdr_q": fs[candidate], "hac_t": t, "hac_p": hp,
            "bootstrap_p": pvals[candidate], "bh_survives": survivors[candidate], "trial_count": n}


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


def _measured_gates(genome, trust, doc, timing, approved):
    code = "REJECT_UNMEASURED_GATE"
    src = doc.get("gates")
    _require(doc.get("schema") == "doppel.shadow-gates/v1" and doc.get("genome_id") == genome.id
             and isinstance(src, dict) and set(src) == GATES
             and all(isinstance(row, dict) and row.get("status") in ("MEASURED", "MEASURED_PASS")
                     for row in src.values()),
             code, "a required v3 gate is absent or unmeasured")
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
    if genome.mode == "taker":
        _require(family in ("lead", "fair") and src["taker_timing"].get("trigger_source") == family,
                 code, "the registered taker must use the same measured trigger family as its timing evidence")
    result["taker_timing"] = {"source_sha256": src["taker_timing"]["source_sha256"],
                              "subjects": subjects, "plan_sha256": timing["plan_sha256"],
                              "candidate_trigger_source": family if genome.mode == "taker" else None,
                              "peak_rss_bytes": rss}
    return result


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


def admit_shadow(genome: Genome, bundle: Path, trust: AdmissionTrust, receipt_dir: Path, *,
                 simulation_sha256: str) -> dict:
    """Validate all proofs and issue an immutable receipt. No canary or order authorization."""
    approved, approval_hash = _approval(bundle, trust, genome)
    replay = _artifact(bundle, approved, "replay.json", "REJECT_NONCANONICAL_REPLAY")
    plan = _artifact(bundle, approved, "plan.json", "REJECT_INCOMPLETE_LEDGER")
    ledger = _artifact(bundle, approved, "trials.json", "REJECT_INCOMPLETE_LEDGER")
    gate = _artifact(bundle, approved, "gates.json", "REJECT_UNMEASURED_GATE")
    timing = _artifact(bundle, approved, "layer5b.json", "REJECT_UNMEASURED_GATE")
    try:
        reg, records, windows, returns = _canonical_replay(genome, trust, approved, replay, simulation_sha256)
        candidate, values = _complete_ledger(genome, trust, approved, plan, ledger, reg, records, windows, returns)
        scenarios = _scenarios(replay)
        measured = _measured_gates(genome, trust, gate, timing, approved)
        stats = _statistics(candidate, values)
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
    _require(time.time_ns() < approved["valid_until_ns"], "REJECT_UNTRUSTED_APPROVAL", "approval expired during admission")
    body = {"schema": "doppel.shadow-admission/v1", "state": "SHADOW_ACTIVE", "genome_id": genome.id,
            "commit_sha": trust.commit_sha, "scoring_sha256": approved["scoring_sha256"],
            "simulation_sha256": simulation_sha256, "approval_sha256": approval_hash,
            "valid_until_ns": approved["valid_until_ns"], "registry_result_sha256": approved["registry_result_sha256"],
            "replay_sha256": approved["files"]["replay.json"], "trial_plan_sha256": trust.plan_sha256,
            "trial_ledger_sha256": approved["files"]["trials.json"], "trial_head_sha256": approved["trial_head_sha256"],
            "n_trials": stats["trial_count"], "scenario_grid": scenarios, "gates": {**stats, **measured}}
    receipt = {**body, "receipt_sha256": _sha(_canon(body))}
    _issue(receipt_dir / genome.id / "admission_receipt.json", receipt)
    return receipt
