"""Copy sealed tape segments and receipts to an archive outside the recorder volume."""

import argparse
import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from shutil import disk_usage


def manifest(path):
    if not path.exists():
        return {}
    # The writer may still be appending the last line. A published line ends in a newline.
    rows = path.read_bytes().split(b"\n")[:-1]
    return {row["file"]: row for row in map(json.loads, filter(None, rows))}


def segment_hour(name, extension):
    return datetime.strptime(name, f"%Y%m%d/%H.{extension}").replace(tzinfo=UTC)


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def copy_sealed(source, target, sha):
    if target.is_file():
        if digest(target) != sha:
            raise ValueError(f"archive checksum mismatch: {target}")
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.partial")
    calc = hashlib.sha256()
    try:
        with source.open("rb") as src, staging.open("wb") as out:
            while chunk := src.read(1024 * 1024):
                calc.update(chunk)
                out.write(chunk)
            if calc.hexdigest() != sha:
                raise ValueError(f"source checksum mismatch: {source}")
            out.flush()
            os.fsync(out.fileno())
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)


def archive(source, destination, streams, through, reserve):
    if not source.is_dir() or source.resolve() == destination.resolve():
        raise ValueError("source and archive must be distinct directories")
    destination.mkdir(parents=True, exist_ok=True)
    if source.stat().st_dev == destination.stat().st_dev:
        raise ValueError("archive must be on a separate filesystem")
    with (destination / ".archive.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return archive_locked(source, destination, streams, through, reserve)


def archive_locked(source, destination, streams, through, reserve):
    copied = {}
    missing = []
    wanted = []
    for stream in streams:
        if Path(stream).name != stream or stream in (".", ".."):
            raise ValueError(f"invalid stream name: {stream}")
        dest_manifest = destination / stream / "manifest.jsonl"
        copied[stream] = manifest(dest_manifest)
        rows = manifest(source / stream / "manifest.jsonl")
        if not rows:
            raise ValueError(f"no sealed manifest entries for {stream}")
        for name, row in sorted(rows.items()):
            if segment_hour(name, "dtape.zst") >= through:
                continue
            src, dst = source / stream / name, destination / stream / name
            if not src.is_file():
                if dst.is_file() and digest(dst) == row["sha256"]:
                    copied[stream][name] = row
                else:
                    missing.append(f"{stream}/{name}")
                continue
            if src.stat().st_size != row["bytes"]:
                raise ValueError(f"source size differs from the manifest: {src}")
            wanted.append((stream, name, row, src, dst))

    needed = sum(row["bytes"] for _, _, row, _, dst in wanted if not dst.exists())
    if disk_usage(destination).free - needed < reserve:
        raise ValueError(f"copy requires {needed} bytes and would cross the archive disk reserve")

    for stream, name, row, src, dst in wanted:
        copy_sealed(src, dst, row["sha256"])
        copied[stream][name] = row

    for stream, rows in copied.items():
        path = destination / stream / "manifest.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        staging = path.with_suffix(".partial")
        with staging.open("w") as handle:
            for name in sorted(rows):
                handle.write(json.dumps(rows[name], separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        staging.replace(path)

    receipts = 0
    for src in sorted((source / "receipts").glob("*/*.json")):
        name = src.relative_to(source / "receipts")
        if segment_hour(str(name), "json") < through:
            copy_sealed(src, destination / "receipts" / name, digest(src))
            receipts += 1
    return {"streams": {stream: len(rows) for stream, rows in copied.items()},
            "copied_bytes": needed, "receipts": receipts, "missing": missing}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--through", required=True, help="exclusive UTC hour, YYYYMMDD/HH")
    parser.add_argument("--streams", nargs="+", required=True)
    parser.add_argument("--reserve-gib", type=int, default=35)
    args = parser.parse_args()
    through = segment_hour(f"{args.through}.dtape.zst", "dtape.zst")
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    if through > now or args.reserve_gib < 0:
        parser.error("use a closed UTC hour boundary and a nonnegative reserve")
    try:
        result = archive(args.source, args.destination, args.streams, through, args.reserve_gib * 2**30)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    print(json.dumps(result, sort_keys=True))
    return int(bool(result["missing"]))


if __name__ == "__main__":
    raise SystemExit(main())
