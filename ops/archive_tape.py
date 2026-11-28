"""Copy sealed tape segments and receipts to an archive outside the recorder volume."""

import argparse
import fcntl
import hashlib
import json
import os
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from shutil import disk_usage

if __package__:
    from . import storage
    from .cold_tape import ObjectStore, offload
else:
    import storage
    from cold_tape import ObjectStore, offload


def manifest_rows(path):
    if not path.exists():
        return []
    # The writer may still be appending the last line. A published line ends in a newline.
    rows = path.read_bytes().split(b"\n")[:-1]
    return list(map(json.loads, filter(None, rows)))


def manifest(path):
    return {row["file"]: row for row in manifest_rows(path)}


def segment_hour(name, extension):
    return datetime.strptime(name, f"%Y%m%d/%H.{extension}").replace(tzinfo=UTC)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def copy_sealed(source, target, sha, size=None):
    if target.is_file():
        if digest(target) != sha:
            raise ValueError(f"archive checksum mismatch: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.partial")
    calc = hashlib.sha256()
    try:
        with source.open("rb") as src, staging.open("wb") as out:
            remaining = size
            while remaining is None or remaining > 0:
                chunk = src.read(1024 * 1024 if remaining is None else min(remaining, 1024 * 1024))
                if not chunk:
                    break
                calc.update(chunk)
                out.write(chunk)
                if remaining is not None:
                    remaining -= len(chunk)
            if remaining or calc.hexdigest() != sha:
                raise ValueError(f"source checksum mismatch: {source}")
            out.flush()
            os.fsync(out.fileno())
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)


def archive(source, destination, streams, through, reserve, *, start=None, require_receipts=False):
    if not source.is_dir() or source.resolve() == destination.resolve():
        raise ValueError("source and archive must be distinct directories")
    destination.mkdir(parents=True, exist_ok=True)
    if source.stat().st_dev == destination.stat().st_dev:
        raise ValueError("archive must be on a separate filesystem")
    with (destination / ".archive.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return archive_locked(source, destination, streams, through, reserve, start=start,
                              require_receipts=require_receipts)


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".partial")
    with staging.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    staging.replace(path)


def archive_locked(source, destination, streams, through, reserve, *, start=None, require_receipts=False):
    def selected(name, ext):
        hour = segment_hour(name, ext)
        return (start is None or start <= hour) and hour < through

    receipts = {}
    issues = []
    for src in sorted((source / "receipts").glob("*/*.json")):
        name = src.relative_to(source / "receipts").as_posix()
        if not selected(name, "json"):
            continue
        raw = src.read_bytes()
        doc = json.loads(raw)
        if f"{doc['day']}/{doc['hour']:02d}.json" != name:
            raise ValueError(f"receipt hour differs from its filename: {src}")
        receipts[name] = (doc, hashlib.sha256(raw).hexdigest())
        version = doc.get("schema_version", 1)
        if version not in (1, 2) or type(version) is not int:
            raise ValueError(f"unsupported receipt schema in {src}")
        if version == 2:
            issues.extend(f"{s}/{name}: missing expected stream"
                          for s in doc.get("missing_expected_streams", []) if s in streams)
    if require_receipts:
        if start is None:
            raise ValueError("--require-receipts needs a --from hour")
        hour = start
        while hour < through:
            name = hour.strftime("%Y%m%d/%H.json")
            if name not in receipts:
                issues.append(f"receipts/{name}: missing")
            hour += timedelta(hours=1)

    copied = {}
    histories = {}
    missing = []
    wanted = []
    for stream in dict.fromkeys(streams):
        if not stream or Path(stream).name != stream or stream in (".", ".."):
            raise ValueError(f"invalid stream name: {stream}")
        dest_manifest = destination / stream / "manifest.jsonl"
        copied[stream] = manifest(dest_manifest)
        history_path = destination / stream / "source-manifest-history.jsonl"
        history = manifest_rows(history_path)
        seen = {json.dumps(r, sort_keys=True) for r in history}
        revisions = {}
        raw_rows = manifest_rows(source / stream / "manifest.jsonl")
        if not raw_rows and not copied[stream]:
            raise ValueError(f"no sealed manifest entries for {stream}")
        for row in raw_rows:
            name = row["file"]
            if not selected(name, "dtape.zst"):
                continue
            revisions.setdefault(name, []).append(row)
            key = json.dumps(row, sort_keys=True)
            if key not in seen:
                seen.add(key)
                history.append(row)
        histories[history_path] = history
        expected = {name.removesuffix(".json") + ".dtape.zst": doc["streams"][stream]
                    for name, (doc, _) in receipts.items() if stream in doc["streams"]}
        names = set(revisions) | expected.keys() | {
            n for n in copied[stream] if selected(n, "dtape.zst")}
        for name in sorted(names):
            saved = copied[stream].get(name)
            options = revisions.get(name, [])
            row = options[-1] if options else saved
            attested = expected.get(name)
            receipt_name = name.removesuffix(".dtape.zst") + ".json"
            version = receipts[receipt_name][0].get("schema_version", 1) if receipt_name in receipts else 1
            if version == 2 and attested is None and row is not None:
                issues.append(f"{stream}/{name}: not listed in receipt")
            if attested is not None:
                # Legacy receipts have no digest. Preserve the final seal for forensics
                # and flag disagreements. A v2 receipt identifies its exact generation.
                sha = attested.get("sha256")
                if sha:
                    sealed = next((r for r in reversed(options) if r["sha256"] == sha and
                                   r["bytes"] == attested["bytes"]), None)
                    if sealed is None and saved and saved["sha256"] == sha and saved["bytes"] == attested["bytes"]:
                        sealed = saved
                    if sealed is not None:
                        row = sealed
                if (row is None or attested.get("crc_ok") is not True or row["bytes"] != attested.get("bytes")
                        or (version == 2 and not sha)
                        or (sha is not None and row["sha256"] != sha)):
                    issues.append(f"{stream}/{name}")
            if row is None:
                continue
            if (saved is not None and saved.get("evidence_use") == "diagnostic_only"
                    and row["sha256"] == saved["sha256"]):
                row = saved
            src, dst = source / stream / name, destination / stream / name
            if not src.is_file():
                if dst.is_file() and digest(dst) == row["sha256"]:
                    copied[stream][name] = row
                else:
                    missing.append(f"{stream}/{name}")
                continue
            if src.stat().st_size < row["bytes"]:
                raise ValueError(f"source size differs from the manifest: {src}")
            wanted.append((stream, name, row, src, dst))

    needed = sum(row["bytes"] for _, _, row, _, dst in wanted if not dst.exists())
    if disk_usage(destination).free - needed < reserve:
        raise ValueError(f"copy requires {needed} bytes and would cross the archive disk reserve")

    for stream, name, row, src, dst in wanted:
        copy_sealed(src, dst, row["sha256"], row["bytes"])
        copied[stream][name] = row

    for path, history in histories.items():
        write_rows(path, history)
    for stream, rows in copied.items():
        write_rows(destination / stream / "manifest.jsonl", (rows[k] for k in sorted(rows)))
    for name, (_, sha) in receipts.items():
        copy_sealed(source / "receipts" / name, destination / "receipts" / name, sha)
    return {"streams": {stream: len(rows) for stream, rows in copied.items()},
            "copied_bytes": needed, "receipts": len(receipts), "missing": missing,
            "receipt_issues": issues}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    destinations = parser.add_mutually_exclusive_group(required=True)
    destinations.add_argument("--destination", type=Path)
    destinations.add_argument("--remote", help="named rclone bucket and prefix, e.g. r2:research/apex/tape")
    destinations.add_argument("--storage-class", help="tape class assigned to this writer by the canonical policy")
    parser.add_argument("--policy", type=Path, default=storage.POLICY)
    parser.add_argument("--state", type=Path, help="private directory for upload proofs in object mode")
    parser.add_argument("--dry-run", action="store_true", help="list pending bytes in object mode without writing")
    parser.add_argument("--historical-paths", type=Path,
                        help="list of closed legacy source paths, relative to --source, one per line")
    parser.add_argument("--through", help="exclusive UTC hour, YYYYMMDD/HH (default latest closed hour)")
    parser.add_argument("--from", dest="start", help="inclusive UTC hour, YYYYMMDD/HH")
    parser.add_argument("--require-receipts", action="store_true",
                        help="report missing hourly receipts in the requested interval")
    parser.add_argument("--streams", nargs="+", help="tape streams, or auto for every manifested stream in object mode")
    parser.add_argument("--reserve-gib", type=int, default=35)
    args = parser.parse_args()
    at = datetime.now(UTC)
    now = at.replace(minute=0, second=0, microsecond=0)
    through = segment_hour(f"{args.through}.dtape.zst", "dtape.zst") if args.through else now
    start = segment_hour(f"{args.start}.dtape.zst", "dtape.zst") if args.start else None
    if through > now or (start is not None and start >= through) or args.reserve_gib < 0:
        parser.error("use a closed UTC hour boundary and a nonnegative reserve")
    if args.storage_class:
        if not args.state or args.source or args.historical_paths or args.require_receipts or args.streams:
            parser.error("policy mode requires --state and fixes the source, remote, and streams")
        try:
            policy = tomllib.loads(args.policy.read_text())
            cls = storage.classes(policy, args.storage_class)[0]
            if not storage.archive_owns(cls):
                raise ValueError(f"class is not assigned to archive_tape: {args.storage_class}")
            r2 = policy["r2"]
            root = Path(cls["local"])
            target = ObjectStore(f"{r2['remote']}/{cls['r2']}", binary=r2["rclone"], bwlimit=r2["bwlimit"],
                                 config=r2["config"])
            cutoff = at - timedelta(seconds=storage.seconds(cls["min_age"]))
            result = offload(root, target, args.state, ["auto"], start, through, dry_run=args.dry_run,
                             modified_before_ns=int(cutoff.timestamp()) * 1_000_000_000)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"{exc}\n")
        print(json.dumps({"class": args.storage_class, **result}, sort_keys=True))
        return int(bool(result["issues"]))
    if args.source is None:
        parser.error("--source is required for explicit destination mode")
    if args.remote:
        if args.state is None or args.require_receipts or (args.historical_paths and (args.streams or start)):
            parser.error("object mode requires --state, and historical paths cannot be mixed with stream ranges")
        try:
            historical = args.historical_paths.read_text().splitlines() if args.historical_paths else None
            if historical == []:
                raise ValueError("historical path list is empty")
            result = offload(args.source, ObjectStore(args.remote), args.state, args.streams or ["auto"],
                             start, through, dry_run=args.dry_run, historical=historical)
        except (OSError, ValueError) as exc:
            parser.exit(1, f"{exc}\n")
        print(json.dumps(result, sort_keys=True))
        return int(bool(result["issues"]))
    if not args.streams or args.streams == ["auto"] or args.state or args.dry_run or args.historical_paths:
        parser.error("local archive mode requires named --streams; object settings require --remote")
    try:
        result = archive(args.source, args.destination, args.streams, through, args.reserve_gib * 2**30,
                         start=start, require_receipts=args.require_receipts)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, sort_keys=True))
    return int(bool(result["missing"] or result["receipt_issues"]))


if __name__ == "__main__":
    raise SystemExit(main())
