"""Canonical end-of-window evaluation against a snapshot of sealed tape."""

from __future__ import annotations

import hashlib
from dataclasses import asdict
from pathlib import Path

import orjson

from ..strategy.genome import from_dict
from ..strategy import sequential
from ..tape.segment import read_segment
from .cli import code_sha256, run_twin
from .engine import SimConfig
from .flowfeat import uses_intl
from .tape import WS_STREAMS, hour_path
from .verdict import verdict


VERDICT_OPTIONS = {"block": 12, "n_boot": 2000, "seed": 0, "op_cost_usd_month": 60.0}
REFERENCE_STREAMS = ("ref_prices", "ref_brti_raw", "ref_brti_live")


def evaluation_inputs(data: Path, tokens: Path | None) -> dict[str, Path]:
    streams = [*WS_STREAMS, *REFERENCE_STREAMS]
    if tokens is not None:
        streams += ["rtds_a", "rtds_b", *(p.name for p in data.glob("clob_*") if p.is_dir())]
    paths = {str(p.relative_to(data)): p
             for stream in streams for p in (data / stream).glob("*/*.dtape.zst")}
    if not any(p.split("/", 1)[0] in WS_STREAMS for p in paths):
        raise ValueError("no Polymarket US websocket tape in the snapshot")
    for stream in {name.split("/", 1)[0] for name in paths}:
        paths[f"{stream}/manifest.jsonl"] = data / stream / "manifest.jsonl"
    paths.update({str(p.relative_to(data)): p for p in (data / "receipts").glob("*/*.json")})
    if tokens is not None:
        paths["lab/tokens.jsonl"] = tokens
    return dict(sorted(paths.items()))


def stamp(path: Path) -> tuple[int, int, int, int, int]:
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def fingerprint(data: Path, paths: dict[str, Path], *, require_binding: bool = False,
                file_digests: dict[str, str] | None = None) -> tuple[str, dict[str, tuple]]:
    """Check sealed bytes and include their manifest provenance in the snapshot identity."""
    manifests: dict[str, dict[str, dict]] = {}
    entries = {}
    stamps = {}
    for stream in {key.split("/", 1)[0] for key in paths if key.endswith(".dtape.zst")}:
        name = f"{stream}/manifest.jsonl"
        path = data / name
        marks = stamp(path)
        raw = path.read_bytes()
        if stamp(path) != marks:
            raise ValueError(f"manifest changed while reading: {name}")
        rows = map(orjson.loads, raw.splitlines())
        manifests[stream] = {r["file"]: r for r in rows}
        entries[name] = [len(raw), hashlib.sha256(raw).hexdigest()]
        stamps[name] = marks
    for name, path in paths.items():
        if name in stamps:
            continue
        marks = stamp(path)
        with path.open("rb") as f:
            sha = hashlib.file_digest(f, "sha256").hexdigest()
        if stamp(path) != marks:
            raise ValueError(f"evaluation input changed while hashing: {name}")
        if name.endswith(".dtape.zst"):
            stream, rel = name.split("/", 1)
            seal = manifests.get(stream, {}).get(rel)
            if seal is None or (seal.get("bytes"), seal.get("sha256")) != (marks[2], sha):
                raise ValueError(f"tape segment is unsealed or differs from its manifest: {name}")
            if require_binding and seal.get("evidence_use") == "diagnostic_only":
                raise ValueError(f"diagnostic_only tape revision cannot enter a canonical replay: {name}")
            # Older receipts tolerated a damaged final frame. Hash agreement alone only
            # establishes that the snapshot still contains those same bytes.
            for _ in read_segment(path, strict=True):
                pass
        entries[name] = [marks[2], sha]
        stamps[name] = marks
    if file_digests is not None:
        file_digests.update({name: row[1] for name, row in entries.items()})
    return hashlib.sha256(orjson.dumps(entries, option=orjson.OPT_SORT_KEYS)).hexdigest(), stamps


def protocol() -> dict:
    config = asdict(SimConfig())
    config["markouts_s"] = list(config["markouts_s"])
    return {"code_sha256": code_sha256(), "simulation": config, "verdict_options": VERDICT_OPTIONS,
            "flow_delay_s": 0, "sequential_alpha": sequential.ALPHA}


def verify_hours(data: Path, horizons, start: int, end: int, paths: dict[str, Path],
                 *, segment_digests: dict[str, str] | None = None) -> None:
    """Reject an omitted evaluation hour or a snapshot missing input attested in its receipt."""
    streams = {key.split("/", 1)[0] for key in paths} | set(WS_STREAMS) | set(REFERENCE_STREAMS)
    flow = "lab/tokens.jsonl" in paths
    if flow:
        streams.update(("rtds_a", "rtds_b"))
    for hour in range(start // 3600 * 3600, end, 3600):
        segment = hour_path(data, "pmus_ws", hour).relative_to(data / "pmus_ws")
        receipt = data / "receipts" / segment.parent / (segment.name[:2] + ".json")
        doc = orjson.loads(receipt.read_bytes())
        if doc.get("day") != segment.parent.name or doc.get("hour") != int(segment.name[:2]):
            raise ValueError(f"wrong hour in receipt {receipt}")
        version = doc.get("schema_version", 1)
        if type(version) is not int or version not in (1, 2):
            raise ValueError(f"unsupported hourly receipt version in {receipt}")
        if version == 2 and doc.get("missing_expected_streams"):
            raise ValueError(f"hourly receipt reports missing expected streams in {receipt}: "
                             f"{doc['missing_expected_streams']}")
        present = doc["streams"]
        for horizon in horizons:
            if not any(present.get(s, {}).get("data_records", 0) > 0
                       for s in ("pmus_ws", f"pmus_ws_{horizon}")):
                raise ValueError(f"no recorded PM US {horizon} data in {receipt}")
        if flow and not any(present.get(s, {}).get("data_records", 0) > 0 for s in ("rtds_a", "rtds_b")):
            raise ValueError(f"no recorded RTDS data for flow evaluation in {receipt}")
        if version == 2:
            for stream in streams - present.keys():
                key = f"{stream}/{segment.as_posix()}"
                if key in paths:
                    raise ValueError(f"evaluation segment is not in hourly receipt: {key}")
        for stream, row in present.items():
            if stream not in streams and not ("lab/tokens.jsonl" in paths and stream.startswith("clob_")):
                continue
            key = f"{stream}/{segment.as_posix()}"
            if (row.get("crc_ok") is not True or key not in paths
                    or paths[key].stat().st_size != row.get("bytes")):
                raise ValueError(f"snapshot differs from hourly receipt: {key}")
            if version == 2 or "sha256" in row:
                if segment_digests is None:
                    with paths[key].open("rb") as f:
                        actual = hashlib.file_digest(f, "sha256").hexdigest()
                else:
                    actual = segment_digests[key]
                if row.get("sha256") != actual:
                    raise ValueError(f"hourly receipt digest differs from snapshot: {key}")


def replay_snapshot(data: Path, lab: Path | None, registration: dict, *, through: int | None = None) -> tuple[list[dict], dict]:
    """Replay a sealed prefix starting at the registered first second."""
    g = from_dict(registration["genome"])
    win = registration["eval_window"]
    start, end = win["start_s"], win["end_s"]
    if not float(start).is_integer() or not float(end).is_integer():
        raise ValueError("canonical tape evaluation requires whole-second bounds")
    if through is not None:
        if not start < through <= end:
            raise ValueError("no closed evaluation hours available to monitor")
        end = through
    need_tokens = uses_intl(g)
    if need_tokens and lab is None:
        raise ValueError("this genome requires --lab with the frozen tokens.jsonl")
    tokens = lab / "tokens.jsonl" if need_tokens and lab is not None else None
    policy = protocol()
    registered_policy = registration.get("evaluation_protocol")
    if registered_policy is not None and registered_policy != policy:
        raise ValueError("installed evaluator differs from the preregistered protocol")
    paths = evaluation_inputs(data, tokens)
    digests: dict[str, str] = {}
    input_sha, stamps = fingerprint(data, paths, require_binding=True, file_digests=digests)
    verify_hours(data, g.universe.horizons, int(start), int(end), paths, segment_digests=digests)
    cfg = SimConfig()
    windows = run_twin(data, g, int(start), int(end), cfg, lab=lab)
    if (evaluation_inputs(data, tokens) != paths or any(stamp(p) != stamps[k] for k, p in paths.items())
            or code_sha256() != policy["code_sha256"]):
        raise ValueError("evaluation input or source changed during replay")
    return windows, {**policy, "input_sha256": input_sha, "files": len(paths),
                     **({"tokens_sha256": digests["lab/tokens.jsonl"]} if need_tokens else {}),
                     "range": [int(start), int(end)], "protocol_preregistered": registered_policy is not None}


def evaluate(data: Path, lab: Path | None, registration: dict) -> dict:
    """Recompute the final verdict over the complete registered range."""
    windows, evidence = replay_snapshot(data, lab, registration)
    result = verdict(from_dict(registration["genome"]), windows, **VERDICT_OPTIONS)
    if result["p_value"] is None:
        raise ValueError("the canonical replay has too few evaluated windows for a p-value")
    return {**result, "p_source": "twin-replay", "evaluation": evidence}
