"""Build a separate H1 input snapshot from archived segments and the original hourly receipts.

Only ref_brti_raw can be shortened, and only at a byte boundary attested by the
original receipt. Historical SHA seals and later reconstructions are distinguished.
Recovered files without an exact historical seal are restricted to diagnostic use.
"""

import argparse
import gzip
import hashlib
import json
import shutil
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from doppel.tape.receipts import stream_hour_summary
from doppel.tape.segment import read_segment
from doppel.twin.evaluate import REFERENCE_STREAMS, evaluation_inputs, fingerprint, verify_hours
from doppel.twin.tape import WS_STREAMS

RAW = "ref_brti_raw"
STREAMS = (*WS_STREAMS, *REFERENCE_STREAMS)


def sha(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def utc_hour(text: str) -> datetime:
    return datetime.strptime(text, "%Y%m%d/%H").replace(tzinfo=UTC)


def inventory(archives, end):
    segments = {}
    receipts = {}
    for archive in archives:
        for stream in STREAMS:
            manifest = archive / stream / "manifest.jsonl"
            if not manifest.is_file():
                continue
            latest = {row["file"]: row for row in rows(manifest)}
            for name, row in latest.items():
                hour = utc_hour(name.removesuffix(".dtape.zst"))
                if hour >= end:
                    continue
                if row.get("evidence_use") == "diagnostic_only":
                    raise ValueError(f"recovery requires the original archive generation: {stream}/{name}")
                key = stream, name
                if key in segments and segments[key][1] != row:
                    raise ValueError(f"different archived manifest rows for {stream}/{name}")
                segments[key] = (archive / stream / name, row)
        for path in (archive / "receipts").glob("*/*.json"):
            name = path.relative_to(archive / "receipts").as_posix()
            if utc_hour(name.removesuffix(".json")) >= end:
                continue
            if name in receipts and receipts[name].read_bytes() != path.read_bytes():
                raise ValueError(f"different original receipts for {name}")
            receipts[name] = path
    return segments, receipts


def recover_prefix(source, target, final, previous, receipt):
    """Authenticates the entire file first; literal prefix bytes retain original frame boundaries."""
    sealed = source.read_bytes()
    if len(sealed) != final["bytes"] or hashlib.sha256(sealed).hexdigest() != final["sha256"]:
        raise ValueError(f"final archive seal mismatch: {source}")
    n = receipt["bytes"]
    if not 0 < n < final["bytes"] or receipt.get("crc_ok") is not True:
        raise ValueError(f"unusable original receipt for {source}")
    prefix = sealed[:n]
    target.write_bytes(prefix)
    digest = hashlib.sha256(prefix).hexdigest()
    summary = stream_hour_summary(target)
    for field in ("bytes", "records", "data_records", "ctl", "first_tw", "last_tw", "max_silence_s", "crc_ok"):
        if summary[field] != receipt[field]:
            raise ValueError(f"{field} differs from original receipt for {source}")
    if receipt.get("sha256", digest) != digest:
        raise ValueError(f"prefix differs from receipt checksum for {source}")
    matched = [r for r in previous if r["file"] == final["file"] and r["bytes"] == n]
    if any(r["sha256"] != digest for r in matched):
        raise ValueError(f"conflicting historical seal for {source}")
    record = {"file": final["file"], "bytes": n, "sha256": digest,
              "provenance": "receipt_prefix_upstream_reconstructed", "evidence_use": "diagnostic_only"}
    manifest_row = matched[-1] if matched else record
    return manifest_row, {
        "classification": "historical_seal" if matched else "upstream_reconstructed_diagnostic",
        "file": final["file"], "source_final_sha256": final["sha256"], "source_final_bytes": final["bytes"],
        "receipt_bytes": n, "prefix_sha256": digest, "summary": {k: v for k, v in summary.items() if k != "file"},
        "historical_sha256": manifest_row["sha256"] if matched else None
    }


def corroborate(cases, upstream, saved):
    """Check every inferred and historically sealed prefix against upstream publication/receive pairs."""
    wanted = defaultdict(dict)
    for seg, case in cases:
        stamps = set()
        for rec in read_segment(seg, strict=True):
            obs = json.loads(rec["raw"])
            ms, received = obs["t_ms"], obs["received"]
            if rec["stream"] != RAW or ms * 1_000_000 != rec["tw"] or ms in stamps or received is None:
                raise ValueError(f"invalid or duplicate raw BRTI observation in {seg}")
            stamps.add(ms)
            hour = datetime.fromtimestamp(received / 1000 if received > 1e11 else received, UTC)
            file = hour.strftime("%Y-%m-%dT%H.jsonl.gz")
            if ms in wanted[file]:
                raise ValueError(f"duplicate publication timestamp {ms} in {file}")
            wanted[file][ms] = (str(obs["v"]), received)
        case["upstream_observations"] = len(stamps)
    stats = {}
    saved.mkdir()
    for filename, expected in sorted(wanted.items()):
        src = upstream / filename
        start = src.stat()
        found = set()
        saved_rows = saved / filename
        with gzip.open(src, "rb") as inp, saved_rows.open("xb") as sink, gzip.GzipFile(
                filename="", mode="wb", fileobj=sink, mtime=0) as out:
            for line in inp:
                if b'"kind":"brti"' not in line:
                    continue
                msg = json.loads(line)
                retain = False
                for ms, value in msg["new"]:
                    if ms in expected and (str(value), msg["received"]) == expected[ms]:
                        found.add(ms)
                        retain = True
                if retain:
                    out.write(line)
        original_digest = sha(src)
        finish = src.stat()
        if (start.st_ino, start.st_size, start.st_mtime_ns, start.st_ctime_ns) != (
                finish.st_ino, finish.st_size, finish.st_mtime_ns, finish.st_ctime_ns):
            raise ValueError(f"upstream capture changed while reading {src}")
        if len(found) != len(expected):
            raise ValueError(f"upstream missing {len(expected) - len(found)} exact publication/receive pairs in {filename}")
        stats[filename] = {"original_sha256": original_digest, "matched_publications": len(found),
                           "matching_rows_file_sha256": sha(saved_rows)}
    return stats


def write_json(path, doc):
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")


def run(archives, history, upstream, out, start, end, reserve):
    if not archives or end <= start or end > datetime.now(UTC).replace(minute=0, second=0, microsecond=0):
        raise ValueError("specify an elapsed nonempty interval with closed hourly receipts")
    segments, receipts = inventory(archives, end)
    if not segments:
        raise ValueError("empty archive selection")
    if shutil.disk_usage(out.parent).free - sum(row["bytes"] for _, row in segments.values()) < reserve:
        raise ValueError("snapshot would exceed the disk reserve")
    # Publish only after all checks pass. A failed attempt stays under incomplete for inspection.
    out.mkdir(exist_ok=False)
    stage = out / "incomplete"
    stage.mkdir()
    tape = stage / "tape"
    originals = stage / "originals"
    originals.mkdir()
    history_copy = originals / "ref_brti_raw-manifest.jsonl"
    # The live importer can be appending a final line. Preserve only committed newline-terminated entries.
    complete, sep, _ = history.read_bytes().rpartition(b"\n")
    if not sep:
        raise ValueError("historical manifest has no complete entries")
    history_copy.write_bytes(complete + sep)
    previous = rows(history_copy)
    for name, src in receipts.items():
        path = tape / "receipts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, path)
    cases = []
    manifests = defaultdict(list)
    for (stream, name), (source, final) in sorted(segments.items()):
        target = tape / stream / name
        target.parent.mkdir(parents=True, exist_ok=True)
        receipt_path = tape / "receipts" / name.replace(".dtape.zst", ".json")
        doc = json.loads(receipt_path.read_bytes()) if receipt_path.exists() and stream == RAW else None
        receipt = doc["streams"].get(RAW) if doc else None
        if receipt is not None and receipt["bytes"] != final["bytes"] and utc_hour(name.removesuffix(".dtape.zst")) >= start:
            # A later historical revision cannot be used as a contemporaneous seal.
            hist = [r for r in previous if r.get("closed_tw", 0) and r["closed_tw"] <= doc["built_tw"]]
            row, case = recover_prefix(source, target, final, hist, receipt)
            case["original_receipt_sha256"] = sha(receipt_path)
            cases.append((target, case))
        else:
            shutil.copyfile(source, target)
            if target.stat().st_size != final["bytes"] or sha(target) != final["sha256"]:
                raise ValueError(f"archive seal mismatch for {source}")
            row = final
        manifests[stream].append(row)
    proof = corroborate(cases, upstream, originals / "upstream-brti-rows")
    for stream, entries in manifests.items():
        path = tape / stream / "manifest.jsonl"
        path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in entries))
    selected = evaluation_inputs(tape, None)
    verify_hours(tape, ("5m",), int(start.timestamp()), int(end.timestamp()), selected)
    digest, _ = fingerprint(tape, selected)
    out_doc = {"from_utc": start.isoformat(), "end_exclusive_utc": end.isoformat(),
               "archives": [str(p.resolve()) for p in archives], "upstream": str(upstream.resolve()),
               "historical_manifest_sha256": sha(history_copy), "prefixes": [case for _, case in cases],
               "upstream_files": proof, "canonical_input_sha256": digest, "canonical_input_files": len(selected),
               "verify_hours": "passed", "fingerprint": "passed", "evaluated_strategy": False,
               "diagnostic_only": any(case["classification"] == "upstream_reconstructed_diagnostic" for _, case in cases)}
    write_json(stage / "report.json", out_doc)
    for path in stage.rglob("*"):
        path.chmod(path.stat().st_mode & ~0o222)
    published = stage.rename(out / "recovery")
    published.chmod(published.stat().st_mode & ~0o222)
    return out_doc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archives", nargs="+", required=True, type=Path, help="immutable sealed tape roots")
    parser.add_argument("--history", required=True, type=Path, help="original append-only raw BRTI manifest")
    parser.add_argument("--upstream", required=True, type=Path, help="closed original PERELMAN capture directory")
    parser.add_argument("--output", required=True, type=Path, help="new run directory with an existing parent")
    parser.add_argument("--from", dest="start", required=True, type=utc_hour)
    parser.add_argument("--through", dest="end", required=True, type=utc_hour, help="exclusive UTC hour")
    parser.add_argument("--reserve-gib", type=int, default=35)
    args = parser.parse_args()
    if args.reserve_gib < 0:
        parser.error("disk reserve must be nonnegative")
    doc = run(args.archives, args.history, args.upstream, args.output, args.start, args.end, args.reserve_gib * 2**30)
    print(json.dumps({k: doc[k] for k in ("canonical_input_sha256", "canonical_input_files", "diagnostic_only")}, sort_keys=True))


if __name__ == "__main__":
    main()
