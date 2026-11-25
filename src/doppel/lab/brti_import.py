"""Import raw BRTI (1 Hz, via Kalshi's CF Benchmarks passthrough) from the PERELMAN recorder's
stream files into DOPPEL tape stream ``ref_brti_raw``.

PERELMAN keeps 14 days; DOPPEL keeps it permanently (ref_* streams are never janitored). Read-only
on PERELMAN's files. Each imported source file is recorded in a state file; the still-open current
hour is re-read each run and only new timestamps are appended (dedupe by ms timestamp).
Raw record written: {"t_ms": ..., "v": "59519.22", "received": ...} per second.
"""

from __future__ import annotations

import argparse
import gzip
import sys
from pathlib import Path

import orjson

from ..tape.segment import SegmentWriter, read_segment


def _iter_rows(path: Path):
    op = gzip.open if path.suffix == ".gz" else open
    with op(path, "rb") as fh:
        for line in fh:
            if b'"kind":"brti"' not in line:
                continue
            try:
                rec = orjson.loads(line)
            except orjson.JSONDecodeError:
                continue  # partial last line of the open hour
            for t_ms, v in rec.get("new", []):
                yield int(t_ms), str(v), rec.get("received")


def run(src: Path, root: Path, state_path: Path) -> dict:
    state = orjson.loads(state_path.read_bytes()) if state_path.exists() else {"done": [], "max_t": 0}
    done = set(state["done"])
    w = SegmentWriter(root, "ref_brti_raw")
    added = 0
    files = sorted(src.glob("*.jsonl.gz")) + sorted(src.glob("*.jsonl"))
    for f in files:
        if f.name in done:
            continue
        for t_ms, v, rcv in _iter_rows(f):
            if t_ms <= state["max_t"]:
                continue
            w.append(orjson.dumps({"t_ms": t_ms, "v": v, "received": rcv}).decode(),
                     tw=t_ms * 1_000_000)
            state["max_t"] = t_ms
            added += 1
        if f.suffix == ".gz":
            done.add(f.name)
    w.close()
    state["done"] = sorted(done)
    state_path.write_bytes(orjson.dumps(state))
    return {"added": added, "max_t": state["max_t"], "files_done": len(done)}


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
