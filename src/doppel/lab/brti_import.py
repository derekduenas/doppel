"""Import raw BRTI (1 Hz, via Kalshi's CF Benchmarks passthrough) from the PERELMAN recorder's
stream files into DOPPEL tape stream ``ref_brti_raw``.

PERELMAN keeps 14 days; DOPPEL keeps it permanently (ref_* streams are never janitored). Read-only
on PERELMAN's files. Each imported source file is recorded in a state file; the still-open current
hour is re-read each run and only new timestamps are appended (dedupe by ms timestamp).
Raw record written: {"t_ms": ..., "v": "59519.22", "received": ...} per second.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import os
import shutil
import sys
from contextlib import closing
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import orjson

from ..tape.segment import SegmentWriter, atomic_write, read_segment, segment_path, sync_directory

STREAM = "ref_brti_raw"


def _iter_rows(path: Path):
    op = gzip.open if path.suffix == ".gz" else open
    with op(path, "rb") as fh:
        for line in fh:
            if path.suffix != ".gz" and not line.endswith(b"\n"):
                break  # wait for the upstream writer to finish the last line
            try:
                rec = orjson.loads(line)
            except orjson.JSONDecodeError as exc:
                raise ValueError(f"malformed upstream record in {path}") from exc
            if rec.get("kind") != "brti":
                continue
            for t_ms, v in sorted(rec["new"], key=lambda pair: int(pair[0])):
                yield int(t_ms), str(v), rec.get("received")


def _publish_import(stream: Path, work: Path, state_path: Path) -> None:
    """Finish a prepared import. A retry can resume after any one of the renames."""
    state = orjson.loads((work / "ready.json").read_bytes())
    if state["state_path"] != str(state_path.resolve()):
        raise ValueError("pending BRTI import uses a different state path")
    watermark = stream / "import-watermark.json"
    if watermark.exists() and state["cursor"]["max_t"] < orjson.loads(watermark.read_bytes())["max_t_ms"]:
        raise ValueError("prepared BRTI transaction would move the watermark backwards")
    manifest = work / STREAM / "manifest.jsonl"
    if manifest.is_file():
        batch = manifest.read_bytes()
        full = (work / "full-manifest").read_bytes()
        published = stream / "manifest.jsonl"
        prior = published.read_bytes() if published.exists() else b""
        if full != prior + batch and full != prior:
            raise ValueError("prepared BRTI transaction conflicts with the published manifest")
        for line in batch.splitlines():
            row = orjson.loads(line)
            rel = row["file"]
            if datetime.strptime(rel, "%Y%m%d/%H.dtape.zst").strftime("%Y%m%d/%H.dtape.zst") != rel:
                raise ValueError(f"invalid BRTI segment path: {rel}")
            staged = work / STREAM / rel
            dst = stream / rel
            if not staged.resolve().is_relative_to(work.resolve()) or not dst.resolve().is_relative_to(stream.resolve()):
                raise ValueError(f"BRTI path escapes its stream: {rel}")
            remaining = staged.is_file()
            revision = staged if remaining else dst
            if revision.stat().st_size != row["bytes"] or hashlib.sha256(revision.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError(f"prepared BRTI segment checksum differs: {revision}")
            if remaining:
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, dst)
                sync_directory(dst.parent)
        atomic_write(published, full)
    if (work / "amendments").exists():
        atomic_write(stream / "import-amendments.jsonl", (work / "amendments").read_bytes())
    atomic_write(state_path, orjson.dumps(state["cursor"]))
    # The importer never appends a publication timestamp <= max_t. Earlier UTC hours
    # are final only after every segment and its manifest have reached the live tree.
    atomic_write(watermark, orjson.dumps({"max_t_ms": state["cursor"]["max_t"]}))
    finished = stream / ".import-complete"
    work.rename(finished)
    sync_directory(stream)
    shutil.rmtree(finished)


def _prepare(src: Path, stream: Path, work: Path, state: dict, revisions: dict) -> tuple[int, int]:
    """Append new publications, recording previously unseen late observations separately."""
    @lru_cache(maxsize=4)
    def prices(path: Path) -> dict:
        if not path.is_file():
            return {}
        return {d["t_ms"]: d["v"] for r in read_segment(path, strict=True) if (d := orjson.loads(r["raw"]))}

    updates = stream / "import-amendments.jsonl"
    old_updates = updates.read_bytes() if updates.is_file() else b""
    known_updates = {(d["t_ms"], d["v"]) for d in map(orjson.loads, old_updates.splitlines())}
    fresh = []
    done = set(state["done"])
    added = 0
    current: Path | None = None
    new_prices: dict[int, str] = {}
    seq = max((r["last_seq"] for r in revisions.values()), default=0)
    with closing(SegmentWriter(work, STREAM, initial_seq=seq)) as writer:
        for f in sorted([*src.glob("*.jsonl.gz"), *src.glob("*.jsonl")]):
            if f.name in done:
                continue
            for t_ms, v, rcv in _iter_rows(f):
                tw = t_ms * 1_000_000
                staged = segment_path(work, STREAM, tw)
                rel = staged.relative_to(work / STREAM)
                live = stream / rel
                if t_ms <= state["max_t"]:
                    historical = staged if staged != current and staged.is_file() else live
                    if (staged == current and new_prices.get(t_ms) == v) or prices(historical).get(t_ms) == v:
                        continue
                    key = (t_ms, v)
                    if key not in known_updates:
                        fresh.append({"t_ms": t_ms, "v": v, "received": rcv, "upstream": f.name})
                        known_updates.add(key)
                    continue
                if staged != current:
                    current = staged
                    new_prices.clear()
                    if live.exists():
                        old = revisions.get(str(rel))
                        staged.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(live, staged)
                        if old is None or hashlib.sha256(staged.read_bytes()).hexdigest() != old["sha256"]:
                            raise ValueError(f"unsealed prior BRTI revision: {rel}")
                writer.append(orjson.dumps({"t_ms": t_ms, "v": v, "received": rcv}).decode(), tw=tw)
                state["max_t"] = t_ms
                new_prices[t_ms] = v
                added += 1
            if f.suffix == ".gz":
                done.add(f.name)
    if fresh:
        atomic_write(work / "amendments", old_updates + b"".join(orjson.dumps(r) + b"\n" for r in fresh))
    state["done"] = sorted(done)
    return added, len(fresh)


def run(src: Path, root: Path, state_path: Path) -> dict:
    """Prepare whole hour revisions privately and publish the cursor after the data."""
    stream = root / STREAM
    stream.mkdir(parents=True, exist_ok=True)
    work = stream / ".import-work"
    if stream.is_symlink() or (stream / ".import.lock").is_symlink():
        raise ValueError("BRTI stream and importer lock cannot be symlinks")
    with (stream / ".import.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if any(p.is_symlink() for p in (stream, work, stream / ".import-complete", work / STREAM, state_path,
                                        work / "ready.json", work / "full-manifest", work / "amendments",
                                        work / STREAM / "manifest.jsonl")):
            raise ValueError("BRTI transaction paths cannot be symlinks")
        if (stream / ".import-complete").exists():
            shutil.rmtree(stream / ".import-complete")
        if (work / "ready.json").exists():
            _publish_import(stream, work, state_path)
        elif work.exists():
            shutil.rmtree(work)  # interrupted preparation; no live files were touched
        state = orjson.loads(state_path.read_bytes()) if state_path.exists() else {"done": [], "max_t": 0}
        if not src.is_dir():
            raise FileNotFoundError(src)
        watermark = stream / "import-watermark.json"
        if watermark.is_file() and state["max_t"] < orjson.loads(watermark.read_bytes())["max_t_ms"]:
            raise ValueError("BRTI import state is behind its published watermark")
        man = stream / "manifest.jsonl"
        history = man.read_bytes() if man.exists() else b""
        if history and not history.endswith(b"\n"):
            raise ValueError("incomplete BRTI manifest line")
        revisions = {r["file"]: r for r in map(orjson.loads, history.splitlines())}
        if state["max_t"] != max((r["last_tw"] for r in revisions.values()), default=0) // 1_000_000:
            raise ValueError("BRTI import state differs from the retained tape")
        work.mkdir(mode=0o700)
        added, late = _prepare(src, stream, work, state, revisions)
        batch_manifest = work / STREAM / "manifest.jsonl"
        if batch_manifest.exists():
            atomic_write(work / "full-manifest", history + batch_manifest.read_bytes())
            for day in batch_manifest.parent.iterdir():
                if day.is_dir():
                    sync_directory(day)
            sync_directory(batch_manifest.parent)
        atomic_write(work / "ready.json", orjson.dumps({"state_path": str(state_path.resolve()), "cursor": state}))
        sync_directory(stream)
        _publish_import(stream, work, state_path)
        return {"added": added, "amendments": late, "max_t": state["max_t"], "files_done": len(state["done"])}


def load_raw(root: Path, include_live: bool = True) -> dict[int, float]:
    """Raw BRTI by second from both sources: the PERELMAN import (``ref_brti_raw``) and DOPPEL's own
    Kalshi poller (``ref_brti_live``, values in each record's ``meta["new"]``). Both are the same
    CF Benchmarks publication; ``brti_source_agreement`` checks that they match."""
    out: dict[int, float] = {}
    for f in sorted((root / "ref_brti_raw").glob("*/*.dtape.zst")):
        for rec in read_segment(f, verify=True):
            if rec["raw"]:
                d = orjson.loads(rec["raw"])
                out[d["t_ms"] // 1000] = float(d["v"])
    if include_live:
        for sec, v in _live_values(root):
            out.setdefault(sec, v)
    return out


def load_raw_rcv(root: Path, include_live: bool = True) -> dict[int, tuple[float, float]]:
    """Like ``load_raw`` but each second carries OUR receive time (unix s), with the twin's rules
    (``twin.features.RefStore``): PERELMAN's ``received`` stamp, else publication + 1 s; live poller
    seconds at the record's ``tw``. Analyses that evaluate fair at a decision time must use only
    seconds received by then (E29; Astra audit 2026-11-26)."""
    out: dict[int, tuple[float, float]] = {}
    for f in sorted((root / "ref_brti_raw").glob("*/*.dtape.zst")):
        for rec in read_segment(f, verify=True):
            if rec["raw"]:
                d = orjson.loads(rec["raw"])
                t_ms = int(d["t_ms"])
                rc = d.get("received")
                rcv = t_ms / 1000.0 + 1.0 if rc is None else (float(rc) / 1000.0 if float(rc) > 1e11 else float(rc))
                out[t_ms // 1000] = (float(d["v"]), rcv)
    if include_live:
        for f in sorted((root / "ref_brti_live").glob("*/*.dtape.zst")):
            for rec in read_segment(f, verify=True):
                for t_ms, v in (rec.get("meta") or {}).get("new") or []:
                    out.setdefault(int(t_ms) // 1000, (float(v), rec["tw"] / 1e9))
    return out


def _live_values(root: Path):
    for f in sorted((root / "ref_brti_live").glob("*/*.dtape.zst")):
        for rec in read_segment(f, verify=True):
            for t_ms, v in (rec.get("meta") or {}).get("new") or []:
                yield int(t_ms) // 1000, float(v)


def brti_source_agreement(root: Path) -> dict:
    """Compare the two raw-BRTI sources on overlapping seconds."""
    a = load_raw(root, include_live=False)
    diffs = [abs(a[s] - v) for s, v in _live_values(root) if s in a]
    return {"overlap_s": len(diffs), "max_abs_diff": max(diffs) if diffs else None,
            "mismatch_gt_0.005": sum(d > 0.005 for d in diffs)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-brti-import")
    ap.add_argument("--src", type=Path, default=Path("/var/lib/private/perelman/stream"))
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--state", type=Path, required=True)
    a = ap.parse_args(argv)
    print(orjson.dumps(run(a.src, a.data, a.state)).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
