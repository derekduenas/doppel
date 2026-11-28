"""Run and aggregate isolated Layer 5a replay comparisons on sealed six-hour tape."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

import orjson

from . import answerkey

SEEDS = (271031, 271067, 271091)
TRADERS = ("a", "b", "c", "d", "e", "f")
RANGE = (1795672800, 1795683600, 1795694400)
STREAM = "clob_btc_5m"
REFERENCE_STREAMS = ("ref_brti_raw", "ref_prices")
STREAMS = (STREAM, *REFERENCE_STREAMS)
HOURS = tuple(f"20261126/{hour:02d}.dtape.zst" for hour in range(6, 12))
BUCKET = "doppel-tape"
PREFIX = "sealed/doppel-sealed-20261126/tape"
INPUTS = Path("evidence/2026-11-27-answerkey-inputs")
TIMING = Path("evidence/2026-11-28-layer5b/layer5a-timing-status.json")
PREFLIGHT_SHA = "75fb01711e2ef5f909a1fae4754d67c3d6dc550e82a3cfa40ae17ec93b94b7b9"
PARAMS_SHA = "e6a7faf116359eb7a70e270421be80a5595135d1e9d03fc5256c95af9b273f3f"
TOKENS_SHA = "7f4e995fa7f11f9a2f326170967076161016272dd0b60e6795f7108ca03e3809"
TIMING_SHA = "45a9553985106340d451c9a4a7c95a8f8c94993903efd2cf8c7090ac6395b32c"
INPUT_SCHEMA = "doppel.layer5a-matrix-inputs/v1"
RECEIPT_SCHEMA = "doppel.layer5a-matrix-subject/v1"
RECEIPT = "receipt.json"
SCORE = "score.json"
INPUT_RECEIPT = "input-receipt.json"


def _digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _read(path: Path, expected: str | None = None) -> dict:
    data = path.read_bytes()
    if expected is not None and hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"SHA-256 differs for {path.name}")
    document = orjson.loads(data)
    if not isinstance(document, dict):
        raise ValueError(f"expected a JSON object in {path.name}")
    return document


def _write(path: Path, value: dict) -> None:
    with path.open("xb") as destination:
        destination.write(orjson.dumps(value, option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS) + b"\n")


def _sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _preflight() -> dict:
    report = _read(INPUTS / "book-preflight.json", PREFLIGHT_SHA)
    if (report.get("range") != list(RANGE) or report.get("stream") != STREAM
            or report.get("tokens_sha256") != TOKENS_SHA or set(report.get("segment_sha256", {})) != set(HOURS)):
        raise ValueError("frozen Layer 5a preflight differs from the matrix scope")
    return report


def _seal_rows(manifest: Path) -> dict:
    seals = {}
    with manifest.open("rb") as source:
        for line in source:
            if not line.strip():
                continue
            row = orjson.loads(line)
            if row["file"] in HOURS:
                seals[row["file"]] = row
    if set(seals) != set(HOURS) or any(not _sha(row.get("sha256"))
                                     or type(row.get("bytes")) is not int or row["bytes"] <= 0
                                     for row in seals.values()):
        raise ValueError(f"{manifest.parent.name}: manifest does not seal every exam hour")
    return seals


def fetch_inputs(out: Path, reference_manifests: dict[str, str], get: Callable[[str, Path], None]) -> dict:
    """Fetch exact Layer 5a objects. The frozen preflight pins CLOB and dispatch inputs pin reference manifests."""
    if set(reference_manifests) != set(REFERENCE_STREAMS) or not all(_sha(v) for v in reference_manifests.values()):
        raise ValueError("both reference manifest SHA-256s must be pinned")
    if out.exists():
        raise ValueError("the exam input directory must be fresh")
    frozen = _preflight()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent) as scratch:
        stage = Path(scratch) / "tape"
        manifests, segments = {}, {}
        for stream in STREAMS:
            directory = stage / stream
            directory.mkdir(parents=True)
            manifest = directory / "manifest.jsonl"
            get(f"{PREFIX}/{stream}/manifest.jsonl", manifest)
            checksum = _digest(manifest)
            if stream in reference_manifests and checksum != reference_manifests[stream]:
                raise ValueError(f"{stream}: archived manifest SHA-256 differs from the pin")
            manifests[stream] = checksum
            seals = _seal_rows(manifest)
            if stream == STREAM and {hour: seals[hour]["sha256"] for hour in HOURS} != frozen["segment_sha256"]:
                raise ValueError("archived CLOB snapshot differs from the registered six hours")
            for hour in HOURS:
                relative = f"{stream}/{hour}"
                path = stage / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                get(f"{PREFIX}/{relative}", path)
                size, digest = seals[hour]["bytes"], seals[hour]["sha256"]
                if path.stat().st_size != size or _digest(path) != digest:
                    raise ValueError(f"archived segment differs from the pinned seal: {relative}")
                segments[relative] = {"bytes": size, "sha256": digest}
        receipt = {"schema": INPUT_SCHEMA, "prefix": PREFIX, "range": list(RANGE), "tokens_sha256": TOKENS_SHA,
                   "preflight_sha256": PREFLIGHT_SHA, "manifests": manifests, "segments": segments}
        _write(stage / INPUT_RECEIPT, receipt)
        stage.rename(out)
    return receipt


def _r2_reader() -> Callable[[str, Path], None]:
    account = os.environ.get("EXAM_R2_ACCOUNT_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", account) or not all(
            os.environ.get(name) for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")):
        raise ValueError("configure the exam read-only R2 account and credentials")
    endpoint = f"https://{account}.r2.cloudflarestorage.com"

    def get(key: str, destination: Path) -> None:
        result = subprocess.run(
            ["aws", "s3api", "get-object", "--bucket", BUCKET, "--key", key, "--endpoint-url", endpoint,
             "--region", "auto", "--no-cli-pager", str(destination)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=900, check=False,
        )
        if result.returncode:
            raise ValueError(f"R2 get-object failed for {key} (status {result.returncode})")

    return get


def _coordinate(seed_slot: int, trader_slot: int) -> tuple[int, str]:
    if not 1 <= seed_slot <= len(SEEDS) or not 0 <= trader_slot < len(TRADERS):
        raise ValueError("unregistered Layer 5a matrix coordinate")
    return SEEDS[seed_slot - 1], TRADERS[trader_slot]


def _validate_inputs(data: Path, frozen: dict) -> str:
    path = data / INPUT_RECEIPT
    receipt = _read(path)
    expected_keys = {f"{stream}/{hour}" for stream in STREAMS for hour in HOURS}
    manifests, segments = receipt["manifests"], receipt["segments"]
    if (receipt["schema"] != INPUT_SCHEMA or receipt["prefix"] != PREFIX or receipt["range"] != list(RANGE)
            or receipt["preflight_sha256"] != PREFLIGHT_SHA or receipt["tokens_sha256"] != TOKENS_SHA
            or set(manifests) != set(STREAMS) or set(segments) != expected_keys):
        raise ValueError("input receipt does not bind the complete registered archive scope")
    for stream in STREAMS:
        if not _sha(manifests[stream]) or _digest(data / stream / "manifest.jsonl") != manifests[stream]:
            raise ValueError(f"{stream}: archived manifest changed after download")
    for key, seal in segments.items():
        stream, hour = key.split("/", 1)
        if (type(seal.get("bytes")) is not int or seal["bytes"] <= 0 or not _sha(seal.get("sha256"))
                or (stream == STREAM and seal["sha256"] != frozen["segment_sha256"][hour])
                or (data / key).stat().st_size != seal["bytes"] or _digest(data / key) != seal["sha256"]):
            raise ValueError(f"downloaded exam segment changed: {key}")
    return _digest(path)


def run_subject(data: Path, out: Path, receipts: Path, materializer: Path,
                seed_slot: int, trader_slot: int, commit: str) -> dict:
    seed, trader = _coordinate(seed_slot, trader_slot)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("subject receipt requires an exact checkout commit")
    if out.exists() or receipts.exists():
        raise ValueError("subject output and receipt directories must be fresh")
    frozen = _preflight()
    inputs_sha = _validate_inputs(data, frozen)
    tokens = INPUTS / "tokens.jsonl"
    if _digest(tokens) != TOKENS_SHA:
        raise ValueError("Layer 5a token map SHA-256 differs")
    params_raw = (INPUTS / "exam-params.json").read_bytes()
    if hashlib.sha256(params_raw).hexdigest() != PARAMS_SHA:
        raise ValueError("Layer 5a parameter SHA-256 differs")
    variants = answerkey.policy_overrides(params_raw)
    params = dict(variants.get(trader, {}))
    start, split, end = RANGE
    midpoint_orders = None
    if trader == "e" and "rate_per_s" not in params:
        midpoint_orders = answerkey.midpoint_training_orders(
            data, tokens, STREAM, start, split, end, seed, variants.get("a"),
        )
        params["rate_per_s"] = max(midpoint_orders, 1) / (split - start)
    scored = answerkey.run_strategy(trader, data, tokens, STREAM, start, split, end, out, seed, params,
                                    slice_s=600, materializer=materializer.resolve(strict=True))
    score = {key: scored[key] for key in ("policy", "params", "cells", "tools", "pipeline", "fidelity_reason",
                                          "train_visible_maker_orders")}
    score["decoy_reference_maker_orders"] = midpoint_orders
    receipts.mkdir(parents=True, exist_ok=False)
    _write(receipts / SCORE, score)
    receipt = {"schema": RECEIPT_SCHEMA, "source_scope": "matrix_replay_comparison", "commit": commit,
               "seed_slot": seed_slot, "trader_slot": trader_slot, "seed": seed, "trader": trader,
               "policy": scored["policy"], "range": list(RANGE), "stream": STREAM, "tokens_sha256": TOKENS_SHA,
               "policy_params_sha256": PARAMS_SHA, "input_sha256": inputs_sha,
               "score_sha256": _digest(receipts / SCORE), "slice_s": 600}
    _write(receipts / RECEIPT, receipt)
    return receipt


def _abstention(cells: dict) -> str:
    observed = cells["explained_by_observed_inputs"]
    if observed.get("genome_emitted") is True or observed.get("recovered") is True:
        return "FAIL"
    if (observed.get("genome_emitted") is False and observed.get("recovered") is False
            and observed.get("fidelity_state") == "not_run"):
        return "PASS"
    return "UNMEASURED"


def aggregate(root: Path, commit: str) -> dict:
    input_hashes, subjects = set(), {}
    for path in sorted(root.rglob(RECEIPT)):
        receipt = _read(path)
        seed_slot, trader_slot = receipt["seed_slot"], receipt["trader_slot"]
        if type(seed_slot) is not int or type(trader_slot) is not int:
            raise ValueError("receipt coordinate must be a matrix integer")
        seed, trader = _coordinate(seed_slot, trader_slot)
        coordinate = (seed_slot, trader_slot)
        if coordinate in subjects:
            raise ValueError(f"duplicate subject receipt for seed {seed}, trader {trader}")
        policy = answerkey.POLICIES[trader].name
        expected = {"schema": RECEIPT_SCHEMA, "source_scope": "matrix_replay_comparison", "commit": commit,
                    "seed": seed, "trader": trader, "policy": policy, "range": list(RANGE), "stream": STREAM,
                    "tokens_sha256": TOKENS_SHA, "policy_params_sha256": PARAMS_SHA, "slice_s": 600}
        if (any(receipt.get(key) != value for key, value in expected.items())
                or not _sha(receipt.get("input_sha256")) or not _sha(receipt.get("score_sha256"))):
            raise ValueError(f"receipt scope differs for seed {seed}, trader {trader}")
        score = _read(path.with_name(SCORE), receipt["score_sha256"])
        cells = score["cells"]
        if score["policy"] != policy or set(cells) != set(answerkey.OBLIGATIONS):
            raise ValueError(f"malformed component scores for seed {seed}, trader {trader}")
        input_hashes.add(receipt["input_sha256"])
        subjects[coordinate] = score
    if len(input_hashes) > 1:
        raise ValueError("matrix subjects used different sealed inputs")
    rows = []
    for seed_slot, seed in enumerate(SEEDS, 1):
        for trader_slot, trader in enumerate(TRADERS):
            score = subjects.get((seed_slot, trader_slot))
            row = {"seed": seed, "trader": trader, "policy": answerkey.POLICIES[trader].name,
                   "status": "MISSING" if score is None else "VERIFIED_RECEIPT"}
            if score is not None:
                cells = score["cells"]
                row.update(components=answerkey.totals({trader: cells}), fidelity=cells["fidelity_verdict"]["state"],
                           fidelity_reason=score.get("fidelity_reason"), cells=cells,
                           abstention=_abstention(cells) if trader in ("e", "f") else None)
                if trader == "e" and (source := subjects.get((seed_slot, 0))) is not None:
                    count = source["train_visible_maker_orders"]
                    if (type(count) is not int or count < 0 or score["decoy_reference_maker_orders"] != count
                            or score["params"]["rate_per_s"] != max(count, 1) / (RANGE[1] - RANGE[0])):
                        raise ValueError(f"decoy normalization differs from midpoint maker for seed {seed}")
            rows.append(row)
    controls = [row.get("abstention") for row in rows if row["trader"] in ("e", "f")]
    abstention = "FAIL" if "FAIL" in controls else "PASS" if all(s == "PASS" for s in controls) else "UNMEASURED"
    timing = _read(TIMING, TIMING_SHA)
    totals_by_seed = {str(seed): answerkey.totals({row["trader"]: row["cells"] for row in rows
                                                   if row["seed"] == seed and "cells" in row})
                      for seed in SEEDS}
    return {"schema": "doppel.layer5a-matrix-summary/v1", "source_scope": "matrix_replay_comparison",
            "commit": commit, "registered_scorecard_revision": "9eac081367fb837495e828e537df98a7f9d07052",
            "input_sha256": next(iter(input_hashes), None), "complete": len(subjects) == 18,
            "receipts": len(subjects), "expected": 18, "subjects": rows, "component_totals_by_seed": totals_by_seed,
            "preregistered_gates": {
                "family_precision": {"status": "UNMEASURED", "threshold": .90,
                                     "reason": "Layer 5a records component claims without family confidence and interval verdicts"},
                "recoverable_strategies": {"status": "UNMEASURED", "threshold": .60,
                                           "reason": "Layer 5a has no sealed right-family confident recovery field"},
                "negative_controls": {"status": abstention, "threshold": 1.0, "expected": len(controls),
                                      "measured": sum(s in ("PASS", "FAIL") for s in controls),
                                      "abstained": controls.count("PASS")},
                "taker_timing": timing,
            }}


def markdown(summary: dict) -> str:
    lines = ["# Layer 5a matrix replay comparison", "",
             f"Verified subject receipts: {summary['receipts']}/{summary['expected']}", "",
             "| Seed | Trader | Policy | Receipt | Confident component claims | Wrongly confident | Fidelity | Control |",
             "| --- | --- | --- | --- | ---: | ---: | --- | --- |"]
    for row in summary["subjects"]:
        components = row.get("components", {})
        wrong = components["categories"].get("wrongly_confident", 0) if components else "—"
        lines.append(f"| {row['seed']} | {row['trader']} | {row['policy']} | {row['status']} | "
                     f"{components.get('confident_claims', '—')} | {wrong} | {row.get('fidelity', '—')} | "
                     f"{row.get('abstention') or '—'} |")
    lines.extend(["", "Preregistered gates", ""])
    lines.extend(f"- {name}: {gate['status']}" for name, gate in summary["preregistered_gates"].items())
    lines.extend(["", "Component totals and per-cell reasons are in summary.json.", ""])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    subcommands = cli.add_subparsers(dest="command", required=True)
    fetch = subcommands.add_parser("fetch")
    fetch.add_argument("--out", required=True, type=Path)
    fetch.add_argument("--brti-manifest-sha256", required=True)
    fetch.add_argument("--prices-manifest-sha256", required=True)
    subject = subcommands.add_parser("run")
    for name in ("data", "out", "receipts", "materializer"):
        subject.add_argument(f"--{name}", required=True, type=Path)
    for name in ("seed-slot", "trader-slot"):
        subject.add_argument(f"--{name}", required=True, type=int)
    subject.add_argument("--commit", required=True)
    all_subjects = subcommands.add_parser("aggregate")
    all_subjects.add_argument("--receipts", required=True, type=Path)
    all_subjects.add_argument("--out", required=True, type=Path)
    all_subjects.add_argument("--commit", required=True)
    args = cli.parse_args(argv)
    if args.command == "fetch":
        fetch_inputs(args.out, {"ref_brti_raw": args.brti_manifest_sha256, "ref_prices": args.prices_manifest_sha256},
                     _r2_reader())
        return 0
    if args.command == "run":
        run_subject(args.data, args.out, args.receipts, args.materializer, args.seed_slot, args.trader_slot, args.commit)
        return 0
    summary = aggregate(args.receipts, args.commit)
    args.out.mkdir(parents=True, exist_ok=False)
    _write(args.out / "summary.json", summary)
    (args.out / "summary.md").write_text(markdown(summary))
    return 0 if summary["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
