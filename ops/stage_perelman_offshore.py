#!/usr/bin/env python3
"""Stage only PERELMAN's Binance and Hyperliquid records from closed hourly gzip files."""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re

HOUR_FILE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2})\.jsonl\.gz")
KIND_VENUES = {"bn": "binance", "hl": "hyperliquid"}


def sha256(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def generation(path):
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


def cutoff(value):
    t = datetime.strptime(value, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
    if t.strftime("%Y-%m-%dT%H") != value:
        raise ValueError("expected an exact UTC hour in YYYY-MM-DDTHH format")
    return t


def closed_hours(root, through):
    newest = through.timestamp()
    for path in sorted(root.glob("*.jsonl.gz")):
        match = HOUR_FILE.fullmatch(path.name)
        if not match:
            continue
        hour = cutoff(match[1])
        if hour + timedelta(hours=1) > through:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"expected a regular input file: {path.name}")
        stat = path.stat()
        if stat.st_mtime < newest:
            yield path, hour


def received_venue(row):
    kind = row.get("kind")
    if kind == "feed_error" and row.get("venue") in KIND_VENUES.values():
        return row["venue"]
    return KIND_VENUES.get(kind)


def stage(source, destination, hour):
    before = generation(source)
    original_sha = sha256(source)
    selected = Counter()
    with gzip.open(source, "rb") as stream, destination.open("xb") as file:
        with gzip.GzipFile(filename="", mode="wb", fileobj=file, mtime=0) as target:
            for line in stream:
                record = json.loads(line)
                venue = received_venue(record)
                if venue is not None:
                    target.write(line)
                    selected[f"{venue}/{record['kind']}"] += 1
    if generation(source) != before:
        raise ValueError(f"source changed while reading: {source.name}")
    return {
        "hour_utc": hour.isoformat(),
        "source": {"file": source.name, "bytes": before[2], "sha256": original_sha},
        "offshore": {"file": destination.name, "bytes": destination.stat().st_size,
                     "sha256": sha256(destination), "rows": dict(selected)},
    }


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, required=True, help="PERELMAN stream directory on Forge")
    p.add_argument("--out", type=Path, required=True, help="new staging directory")
    p.add_argument("--through", type=cutoff, required=True, help="exclusive UTC YYYY-MM-DDTHH")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    if not a.source.is_dir():
        p.error("source directory does not exist")
    candidates = list(closed_hours(a.source, a.through))
    if not candidates:
        p.error("no confirmed closed gzip hours before cutoff")
    if a.dry_run:
        print(json.dumps({"confirmed_hours": len(candidates), "first": candidates[0][0].name,
                          "last": candidates[-1][0].name,
                          "source_bytes": sum(path.stat().st_size for path, _ in candidates)}))
        return 0

    a.out.mkdir(parents=True, exist_ok=False)
    receipts, staged_paths, latest_closed_ns = [], [], 0
    for source, hour in candidates:
        dest = a.out / source.name
        receipts.append(stage(source, dest, hour))
        # The archive's closed-file check uses mtime as well as the upstream hour.
        closed_ns = source.stat().st_mtime_ns
        os.utime(dest, ns=(closed_ns, closed_ns))
        latest_closed_ns = max(latest_closed_ns, closed_ns)
        staged_paths.append(dest.name)
    manifest = a.out / "offshore-manifest.json"
    manifest.write_text(json.dumps({"schema": "perelman-offshore/1", "through_utc": a.through.isoformat(),
                                    "hours": receipts}, indent=2, sort_keys=True) + "\n")
    os.utime(manifest, ns=(latest_closed_ns, latest_closed_ns))
    (a.out / "upload-paths.txt").write_text("\n".join([*staged_paths, manifest.name]) + "\n")
    print(json.dumps({"staged_hours": len(receipts), "manifest_sha256": sha256(manifest)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
