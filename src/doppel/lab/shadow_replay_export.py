"""Export sealed forward replay evidence and the complete Shadow trial inventory."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path

from ..paper.epoch import _book_complete
from ..strategy.genome import Genome, from_dict
from ..strategy.registry import Registry, git_sha
from ..strategy.shadow_admission import (
    MAX_ARTIFACT_BYTES, _canon, _hex, _json, _num, _read, _sha, simulation_digest,
)
from ..tape.segment import atomic_write, sync_directory
from ..twin.cli import code_sha256
from ..twin.engine import SimConfig, exclusion
from ..twin.evaluate import VERDICT_OPTIONS, evaluation_inputs, fingerprint, replay_snapshot, stamp, verify_hours
from ..twin.features import RefStore
from ..twin.flowfeat import rtds_hours_present, uses_intl
from ..twin.tape import NS, iter_windows
from ..twin.verdict import verdict
from .promotion import ABSTENTION_PLAN_SCHEMA, ABSTENTION_PROOF_SCHEMA, PBO_S
from .shadow_scenarios import replay_scenarios
from .shadow_trial_ledger import TrialLedger, read_plan

RUN_SCHEMA = "doppel.shadow-replay-run-spec/v1"
POLICY_SCHEMA = "doppel.shadow-fixed-abstention/v1"
ABSTENTION_MAX_BOOK_AGE_NS = 3 * NS
WINDOW_POLICY = {
    "schema": "doppel.shadow-window-policy/v1",
    "eligibility": "canonical_twin_on_preregistered_panel_trial",
    "missing_returns": "reject_unaligned",
    "abstention_max_book_age_ns": ABSTENTION_MAX_BOOK_AGE_NS,
}
SCENARIO_POLICY = {
    "schema": "doppel.shadow-scenario-policy/v1",
    "fees": {"api_live_rebated": [0.0695, 0.70], "api_base_0695": [0.0695, 0.0], "maker_0": [0.0, 0.0]},
    "sizes": [25, 100, 500],
    "depth": "full_sealed_l2",
    "maker": "pessimistic_l2_fifo",
    "completion": "full_requested_entry_or_quote",
}


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _local_reference(root: Path, reference: object, expected: str) -> bytes:
    if not isinstance(reference, dict) or reference.get("sha256") != expected:
        raise ValueError("reference differs from the preregistered digest")
    name = reference.get("path")
    if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("evidence requires a relative path under the run spec directory")
    path = root / name
    if not path.resolve().is_relative_to(root):
        raise ValueError("evidence path escapes the run spec directory")
    raw = _read(path)
    if _sha(raw) != expected:
        raise ValueError("trial source file hash mismatch")
    return raw


def _run_spec(path: Path, spec_sha: str, plan_sha: str, commit: str) -> dict:
    raw = _read(path)
    if not _hex(spec_sha) or _sha(raw) != spec_sha:
        raise ValueError("run spec differs from its independently pinned SHA-256")
    spec = _json(raw)
    span = spec.get("forward_range_s")
    release = git_sha()
    if (spec.get("schema") != RUN_SCHEMA or not _hex(spec.get("run_id"))
            or not isinstance(spec.get("family"), str) or not spec["family"].strip()
            or not _hex(plan_sha) or spec.get("plan_sha256") != plan_sha
            or not _hex(commit, 40) or release != {"sha": commit, "dirty": False}
            or spec.get("commit_sha") != commit or spec.get("scoring_sha256") != code_sha256()
            or not _hex(spec.get("simulation_sha256"))
            or spec.get("window_policy_sha256") != _sha(_canon(WINDOW_POLICY))
            or spec.get("scenario_policy_sha256") != _sha(_canon(SCENARIO_POLICY))
            or not isinstance(span, list) or len(span) != 2 or any(type(t) is not int for t in span)
            or not 0 < span[0] < span[1] <= time.time()):
        raise ValueError("run spec release, protocol, policies or closed forward interval differ")
    return spec


def _attempts(spec: dict, plan: dict) -> list[dict]:
    attempts = spec.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != len(plan["attempts"]):
        raise ValueError("run spec must cover every planned trial")
    for item, identity in zip(attempts, plan["attempts"], strict=True):
        digest = _sha(_canon([spec["run_id"], identity["genome_id"], identity["subject_id"], identity["variant_id"]]))
        fields = {"trial_id", "variant"}
        if identity["trial_id"] in plan["abstentions"]:
            fields.add("policy")
        if (not isinstance(item, dict) or set(item) != fields
                or item.get("trial_id") != identity["trial_id"] or identity["trial_id"] != digest):
            raise ValueError("run spec does not match the ordered, unique trial identities")
    return attempts


def _registration(registry: Registry, records: list[dict], spec: dict, genome_id: str) -> tuple[Genome, dict, dict, SimConfig]:
    reg = registry.registration(genome_id)
    bound = registry.protocol_binding(genome_id)
    binding_record = next((r for r in records if r.get("kind") == "protocol" and genome_id in r["genome_ids"]), None)
    if reg is None or bound is None or binding_record is None or reg["family"] != spec["family"]:
        raise ValueError("trial has no preregistered family and evaluation protocol")
    span = [reg["eval_window"]["start_s"], reg["eval_window"]["end_s"]]
    config = SimConfig(**bound["params"]["simulation"])
    if (span != spec["forward_range_s"] or reg["tw"] >= span[0] * NS or binding_record["tw"] >= span[0] * NS
            or bound["code_sha256"] != spec["scoring_sha256"]
            or simulation_digest(config) != spec["simulation_sha256"]):
        raise ValueError("trial registration differs from the sealed interval or simulation")
    genome = from_dict(reg["genome"])
    if genome.universe.venue != "pmus" or len(genome.universe.assets) != 1 or len(genome.universe.horizons) != 1:
        raise ValueError("canonical replay requires a single PM US asset and horizon")
    return genome, reg, bound, config


def _panel(data: Path, genome: Genome, start: int, end: int, cfg: SimConfig) -> tuple[list[tuple[int, int]], bool]:
    """Fix the common eligibility grid from source observations before running any policy."""
    store = RefStore(data)
    asset = genome.universe.assets[0]
    store.load_boundaries(asset, start - 1, end)
    windows = []
    complete = True
    for window in iter_windows(data, genome.universe.assets, genome.universe.horizons, start, end, cfg.depth_levels):
        ref = store.window_ref(asset, window.start, window.end, genome.fair.vol_window_s)
        if uses_intl(genome) and not rtds_hours_present(data, window.start - 120, window.end):
            window.stats["rtds_missing"] = True
        if exclusion(genome, window, ref, cfg.min_trade_coverage, cfg.max_inferred_share,
                     cfg.max_unidentified_share) is None:
            windows.append((window.start, window.end))
            complete = complete and _book_complete(window, ABSTENTION_MAX_BOOK_AGE_NS)
    if len(windows) < 2 * PBO_S or any(left[1] > right[0] for left, right in zip(windows, windows[1:])):
        raise ValueError("preregistered panel does not contain at least 32 nonoverlapping eligible windows")
    return windows, complete


def _publish(path: Path, raw: bytes, *, bounded: bool = False) -> str:
    if bounded and len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("evidence exceeds the admission artifact size limit")
    try:
        atomic_write(path, raw, replace=False)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != raw:
            raise ValueError(f"previous {path.name} differs from the reproduced artifact") from None
    return _sha(raw)


def _candidate(data: Path, lab: Path | None, genome: Genome, start: int, end: int, cfg: SimConfig,
               windows: list[tuple[int, int]], output: Path, replay: dict) -> dict:
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output) as trace:
        def append_trace(row: dict) -> None:
            trace.write(_canon(row) + b"\n")

        scenarios, diagnostics = replay_scenarios(
            data, lab, genome, start, end, cfg, windows, trace=append_trace,
        )
        trace.flush()
        os.fsync(trace.fileno())
        trace_sha = _digest(Path(trace.name))
        target = output / "scenario-trace.jsonl"
        try:
            os.link(trace.name, target, follow_symlinks=False)
        except FileExistsError:
            if target.is_symlink() or _digest(target) != trace_sha:
                raise ValueError("previous trace differs from the reproduced artifact") from None
        sync_directory(output)
    diag_sha = _publish(output / "scenarios.json", _canon(diagnostics) + b"\n")
    replay = {**replay, "scenarios": scenarios, "scenario_trace_sha256": trace_sha,
              "scenario_diagnostics_sha256": diag_sha}
    replay_sha = _publish(output / "replay.json", _canon(replay) + b"\n", bounded=True)
    return {"replay_sha256": replay_sha, "scenario_trace_sha256": trace_sha, "scenario_diagnostics_sha256": diag_sha}


def _recorded_result(records: list[dict], genome_id: str, expected: dict, span: list[int]) -> dict:
    result = next((r for r in records if r.get("kind") == "result" and r.get("genome_id") == genome_id
                   and r.get("binding") is True), None)
    if (result is None or result.get("protocol_unbound") is not False
            or result.get("data_range") != span or result.get("verdict") != expected):
        raise ValueError("the first binding registry result does not reproduce from the sealed replay")
    return result


def _abstention(root: Path, source: dict, policy_sha: str, input_sha: str, windows: list[tuple[int, int]],
                complete: bool, identity: dict) -> dict:
    policy = _json(_local_reference(root, source["policy"], policy_sha))
    if (policy.get("schema") != POLICY_SCHEMA or policy.get("entry_policy") != "always_abstain"
            or policy.get("subject_id") != identity["subject_id"] or policy.get("variant_id") != identity["variant_id"]
            or not _hex(policy.get("fit_evidence_sha256")) or not complete):
        raise ValueError("abstention lacks a pinned no-entry policy or a fully observed forward panel")
    _local_reference(root, policy.get("fit_evidence"), policy["fit_evidence_sha256"])
    return {"schema": ABSTENTION_PROOF_SCHEMA, "policy_sha256": policy_sha, "input_sha256": input_sha,
            "windows_sha256": _sha(_canon(windows)), "observed_windows": len(windows), "entry_attempts": 0}


def _window_rows(rows: list[dict], grid: list[tuple[int, int]]) -> tuple[list[dict], list[float]]:
    output, eligible, returns = [], [], []
    for row in rows:
        keys = ("slug", "asset", "horizon", "start", "end", "eligible", "excluded")
        output.append({key: row.get(key) for key in keys})
        if row["eligible"]:
            net = row["net_c"]
            if not _num(net):
                raise ValueError("canonical replay has an invalid eligible window return")
            eligible.append((row["start"], row["end"]))
            returns.append(net)
            output[-1]["net_c"] = net
    if eligible != grid:
        raise ValueError("trial's canonical eligible windows differ from the preregistered panel")
    return output, returns


def _refusal(exc: Exception, roots: dict[str, Path]) -> str:
    message = str(exc)
    for name, path in sorted(roots.items(), key=lambda item: -len(str(item[1]))):
        message = message.replace(str(path), name)
    return f"{type(exc).__name__}: {message}"[:1000]


def export_run(run_spec: Path, run_spec_sha256: str, plan_file: Path, plan_sha256: str,
               data: Path, lab: Path | None, registry_root: Path, commit_sha: str, out: Path) -> dict:
    """Replay a preregistered family and publish only exact, hash-bound evidence."""
    spec = _run_spec(run_spec, run_spec_sha256, plan_sha256, commit_sha)
    start, end = spec["forward_range_s"]
    plan = read_plan(plan_file, plan_sha256, spec["family"], start)
    if plan["schema"] != ABSTENTION_PLAN_SCHEMA:
        raise ValueError("the exporter requires a v2 preregistered trial plan")
    attempts = _attempts(spec, plan)
    registry = Registry(registry_root)
    registry_bytes = _read(registry.path)
    if errors := registry.verify():
        raise ValueError(f"registry integrity failed: {errors[0]}")
    records = registry.records()
    family = {r["genome_id"] for r in records if r.get("kind") == "register" and r.get("family") == spec["family"]}
    executable = {r["genome_id"] for r in plan["attempts"] if r["trial_id"] not in plan["abstentions"]}
    abstained = {r["genome_id"] for r in plan["attempts"] if r["trial_id"] in plan["abstentions"]}
    if not family or not family <= executable or abstained & family:
        raise ValueError("planned executable trials must cover the complete registered family")
    panel = next((r for r in plan["attempts"] if r["trial_id"] == spec.get("panel_trial_id")
                  and r["trial_id"] not in plan["abstentions"]), None)
    if panel is None:
        raise ValueError("run spec requires a preregistered executable panel trial")
    panel_genome, _, _, panel_cfg = _registration(registry, records, spec, panel["genome_id"])
    intl = uses_intl(panel_genome)
    if intl and lab is None:
        raise ValueError("frozen lab token map required for this panel")
    tokens = lab / "tokens.jsonl" if intl and lab is not None else None
    paths = evaluation_inputs(data, tokens)
    digests: dict[str, str] = {}
    input_sha, stamps = fingerprint(data, paths, require_binding=True, file_digests=digests)
    verify_hours(data, panel_genome.universe.horizons, start, end, paths, segment_digests=digests)
    windows, complete = _panel(data, panel_genome, start, end, panel_cfg)
    registry_sha = _sha(registry_bytes)
    context = _sha(_canon([run_spec_sha256, plan_sha256, input_sha, registry_sha]))

    def check_sources() -> None:
        if (_digest(registry.path) != registry_sha or code_sha256() != spec["scoring_sha256"]
                or evaluation_inputs(data, tokens) != paths or any(stamp(path) != stamps[key] for key, path in paths.items())):
            raise ValueError("sealed sources changed during replay")

    def record(identity: dict, source: dict) -> tuple[str, list[float] | None, str, dict]:
        evidence = {"input_sha256": input_sha, "variant_sha256": identity["variant_id"]}
        trial = identity["trial_id"]
        roots = {"TAPE": data.resolve(), "OUTPUT": out.resolve(), "SPEC": run_spec.parent.resolve(),
                 "REGISTRY": registry_root.resolve()}
        try:
            _local_reference(run_spec.parent.resolve(), source["variant"], identity["variant_id"])
            if trial in plan["abstentions"]:
                proof = _abstention(run_spec.parent.resolve(), source, plan["abstentions"][trial], input_sha,
                                    windows, complete, identity)
                return "ABSTAIN", [0.0] * len(windows), "verified_fixed_no_entry", {**evidence, "abstention": proof}
            genome, reg, bound, config = _registration(registry, records, spec, identity["genome_id"])
            if (genome.universe.assets != panel_genome.universe.assets
                    or genome.universe.horizons != panel_genome.universe.horizons or uses_intl(genome) != intl):
                raise ValueError("trial differs from the common market universe or snapshot source set")
            rows, evaluation = replay_snapshot(data, lab, reg, binding=bound)
            if evaluation.get("input_sha256") != input_sha:
                raise ValueError("canonical evaluator used different sealed inputs")
            window_rows, returns = _window_rows(rows, windows)
            reconstructed = {**verdict(genome, rows, **VERDICT_OPTIONS), "p_source": "twin-replay", "evaluation": evaluation}
            result = _recorded_result(records, genome.id, reconstructed, spec["forward_range_s"])
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            return "ERROR", None, _refusal(exc, roots), evidence
        evidence["registry_result_sha256"] = result["hash"]
        if reconstructed["decision"] != "PASS":
            return "FAIL", returns, reconstructed["decision"], evidence

        replay = {"schema": "doppel.shadow-replay/v1", "genome_id": genome.id, "trial_id": trial,
                  "commit_sha": commit_sha, "run_spec_sha256": run_spec_sha256,
                  "verdict": result["verdict"], "windows": window_rows}
        try:
            candidate = _candidate(data, lab, genome, start, end, config, windows, out / "candidates" / trial, replay)
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            return "PASS", returns, f"scenario_unavailable: {_refusal(exc, roots)}", evidence
        return "PASS", returns, "binding_pass", {**evidence, **candidate}

    with TrialLedger(out, plan, plan_sha256, windows, context) as ledger:
        check_sources()
        done = len(ledger.rows)
        for identity, source in zip(plan["attempts"][done:], attempts[done:], strict=True):
            ledger.begin(identity)
            disposition, returns, reason, evidence = record(identity, source)
            check_sources()
            ledger.finish(identity, disposition=disposition, returns_c=returns, reason=reason, evidence=evidence)
        rows = ledger.rows
        candidates = []
        for row in rows:
            if "replay_sha256" in row:
                dest = out / "candidates" / row["trial_id"]
                for filename, field in (("replay.json", "replay_sha256"), ("scenarios.json", "scenario_diagnostics_sha256"),
                                        ("scenario-trace.jsonl", "scenario_trace_sha256")):
                    path = dest / filename
                    if path.is_symlink() or _digest(path) != row[field]:
                        raise ValueError(f"completed trial output differs from its checkpoint: {filename}")
                candidates.append({"trial_id": row["trial_id"], "replay_sha256": row["replay_sha256"]})
        closed = ledger.publish()
        status = ("REJECT_INCOMPLETE_LEDGER" if any(row["disposition"] == "ERROR" for row in rows)
                  else "READY" if candidates else "NO_CANDIDATE")
        receipt = {"schema": "doppel.shadow-replay-run-receipt/v1", "status": status, "run_spec_sha256": run_spec_sha256,
                   "plan_sha256": plan_sha256, "commit_sha": commit_sha, "scoring_sha256": spec["scoring_sha256"],
                   "registry_sha256": registry_sha, "input_sha256": input_sha, "n_trials": len(rows),
                   "windows": len(windows), "trial_head_sha256": closed["trials"][-1]["sha256"],
                   "trial_ledger_sha256": _digest(out / "trials.json"), "candidates": candidates,
                   "dispositions": {name: sum(row["disposition"] == name for row in rows)
                                    for name in ("PASS", "FAIL", "ABSTAIN", "ERROR")}}
        _publish(out / "run-receipt.json", _canon(receipt) + b"\n", bounded=True)
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="doppel promote replay-export", description=__doc__)
    for name in ("run-spec", "plan", "data", "registry-root", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--lab", type=Path)
    for name in ("run-spec-sha256", "plan-sha256", "commit-sha"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args(argv)
    try:
        receipt = export_run(args.run_spec, args.run_spec_sha256, args.plan, args.plan_sha256, args.data,
                             args.lab, args.registry_root, args.commit_sha, args.out)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        print(f"REFUSED {exc}", file=sys.stderr)
        return 2
    print(_canon(receipt).decode())
    return 2 if receipt["status"] == "REJECT_INCOMPLETE_LEDGER" else 0
