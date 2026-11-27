"""Prune the oldest closed UTC day from approved high-volume streams under one tape root."""

import argparse
from datetime import date, datetime, timezone
import os
from pathlib import Path
import shutil

GIB = 1024**3


def closed_days(root: Path, today: date) -> dict[date, list[Path]]:
    days: dict[date, list[Path]] = {}
    for stream in root.iterdir():
        if not (stream.name.startswith(("clob_", "rtds_", "spot_", "pmus_ws"))
                or stream.name in {"pmus_books", "kalshi_books", "kalshi_ws"}):
            continue
        if stream.is_symlink() or not stream.is_dir():
            continue
        for path in stream.iterdir():
            if (len(path.name) != 8 or not path.name.isascii() or not path.name.isdigit()
                    or path.is_symlink() or not path.is_dir()):
                continue
            try:
                day = date.fromisoformat(path.name)
            except ValueError:
                continue
            if day < today:
                days.setdefault(day, []).append(path)
    return days


def prune(root: Path, floor_gib: int) -> int:
    if floor_gib < 0:
        raise ValueError("free-space floor must be nonnegative")
    floor = floor_gib * GIB
    while (free := shutil.disk_usage(root).free) < floor:
        now = datetime.now(timezone.utc)
        days = closed_days(root, now.date())
        oldest = min(days) if days else None
        message = f"deleting live-stream day {oldest:%Y%m%d}" if oldest else "nothing deletable"
        with (root / "janitor.log").open("a") as log:
            print(f"{now:%Y-%m-%dT%H:%M:%SZ} free={free}B < {floor}B: {message}", file=log)
        if oldest is None:
            return 1
        for path in days[oldest]:
            # A stream replaced by a symlink after enumeration must never redirect deletion.
            fd = os.open(path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                shutil.rmtree(path.name, dir_fd=fd)
            finally:
                os.close(fd)
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("floor_gib", type=int, nargs="?", default=12)
    args = p.parse_args()
    raise SystemExit(prune(args.root, args.floor_gib))
