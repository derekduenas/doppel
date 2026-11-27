"""Reconstruct wallet pagination evidence from sealed Goldsky capture segments, offline."""

import argparse
from collections import defaultdict
import hashlib
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qs, urlsplit

import orjson

from ..tape.segment import read_segment
from .goldsky import BASE, PAGES
from .goldsky import _interval


def digest(path: Path) -> str:
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def recover(tape: Path, cohort: Path, start: int, end: int, segments: list[str], out: Path) -> dict:
    """Publish a new evidence directory. A discontinuity or an unsealed segment fails the run."""
    if start >= end or not segments or len(set(segments)) != len(segments):
        raise ValueError("use a nonempty interval and distinct segment names")
    if out.exists():
        raise FileExistsError(out)
    raw_cohort = cohort.read_bytes()
    data = orjson.loads(raw_cohort)
    wallets = set(w.lower() for w in data["candidates"] + data["controls"])
    if not wallets:
        raise ValueError("empty cohort")
    stream = tape / "goldsky_activity"
    manifest = (stream / "manifest.jsonl").read_bytes()
    seals = {r["file"]: r for line in manifest.splitlines() if line and (r := orjson.loads(line))}
    states = {}
    cursors: dict[str, set[str]] = defaultdict(set)
    archived = {}
    for name in sorted(segments):
        if not re.fullmatch(r"[0-9]{8}/(?:[01][0-9]|2[0-3])\.dtape\.zst", name):
            raise ValueError(f"invalid segment name {name}")
        path = stream / name
        before = path.stat()
        seal = seals.get(name)
        if not seal or before.st_size != seal["bytes"] or digest(path) != seal["sha256"]:
            raise ValueError(f"unsealed or changed segment {name}")
        archived[name] = {"bytes": seal["bytes"], "sha256": seal["sha256"]}
        for rec in read_segment(path, strict=True):
            meta = rec.get("meta") or {}
            if meta.get("status") != 200 or not rec.get("raw"):
                continue
            url = urlsplit(meta.get("url", ""))
            if url._replace(query="", fragment="").geturl() != BASE:
                continue
            q = parse_qs(url.query, keep_blank_values=True)
            if (set(q) - {"address", "start_time", "end_time", "limit", "cursor"}
                    or not {"address", "start_time", "end_time", "limit"} <= q.keys()
                    or any(len(values) != 1 for values in q.values())):
                continue
            wallet, lo, hi = _interval({key: vals[0] for key, vals in q.items()})
            if wallet not in wallets or (lo, hi) != (start, end):
                continue
            state = states.setdefault(wallet, {"address": wallet, "start_time": lo, "end_time": hi,
                       "pages": 0, "rows": 0, "cursor": None, "complete": False, "stop": "archived_prefix"})
            request_cursor = q["cursor"][0] if "cursor" in q else None
            if state["complete"] or request_cursor != state["cursor"]:
                raise ValueError(f"discontinuous query for {wallet} in {name}")
            page = orjson.loads(rec["raw"])
            rows, cursor = page["data"], page.get("next_cursor")
            if (not isinstance(rows, list) or not 0 <= len(rows) <= int(q["limit"][0])
                    or any(r["address"].lower() != wallet or not start <= r["block_timestamp"] <= end for r in rows)):
                raise ValueError(f"rows differ from wallet query for {wallet} in {name}")
            if cursor is not None and (not isinstance(cursor, str) or not cursor or not rows or cursor in cursors[wallet]):
                raise ValueError(f"invalid next cursor for {wallet} in {name}")
            if cursor:
                cursors[wallet].add(cursor)
            state.update(pages=state["pages"] + 1, rows=state["rows"] + len(rows), tw=rec["tw"],
                         job=meta.get("job"), cursor=cursor, complete=cursor is None,
                         stop="done" if cursor is None else "archived_prefix")
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError(f"segment changed during replay: {name}")
    if not states:
        raise ValueError("no matching wallet pages")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".pagination-", dir=out.parent) as tmp:
        stage = Path(tmp) / "proof"
        stage.mkdir()
        rows_file = stage / PAGES
        with rows_file.open("xb") as fh:
            for wallet in sorted(states):
                fh.write(orjson.dumps(states[wallet]) + b"\n")
        summary = {"source": "sealed_goldsky_pages", "interval": [start, end], "segments": archived,
                   "cohort_sha256": hashlib.sha256(raw_cohort).hexdigest(),
                   "cohort_wallets": len(wallets), "queries": len(states),
                   "complete": sum(s["complete"] for s in states.values()), "missing": len(wallets - states.keys()),
                   "incomplete": sum(not s["complete"] for s in states.values()),
                   "pages": sum(s["pages"] for s in states.values()), "rows": sum(s["rows"] for s in states.values()),
                   "complete_rows": sum(s["rows"] for s in states.values() if s["complete"]),
                   "pagination_sha256": digest(rows_file)}
        (stage / "summary.json").write_bytes(orjson.dumps(summary, option=orjson.OPT_INDENT_2) + b"\n")
        stage.rename(out)
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--cohort", type=Path, required=True)
    p.add_argument("--start", type=int, required=True)
    p.add_argument("--end", type=int, required=True)
    p.add_argument("--segments", nargs="+", required=True, help="YYYYMMDD/HH.dtape.zst")
    p.add_argument("--out", type=Path, required=True, help="new scratch output directory")
    a = p.parse_args()
    summary = recover(a.data, a.cohort, a.start, a.end, a.segments, a.out)
    print(orjson.dumps(summary).decode())


if __name__ == "__main__":
    main()
