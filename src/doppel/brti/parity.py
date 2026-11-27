"""Parity of two BRTI recorder streams (A = PERELMAN, B = DOPPEL) over a publication-time window.

Published values (``brti``, and ``rti`` per index id): every second present in A must be present in
B with the identical value string. PASS needs 100% identical values on overlapping seconds, no
conflicting values inside B, and coverage (overlap / A's seconds) >= 99.9%. ``brti`` rows are read
with the frozen importer's own reader (``doppel.lab.brti_import._iter_rows``), so B is parsed exactly
as ``doppel-brti-import`` will parse it. Receive-stamp differences (B - A, first sighting) are reported.

Replica values come from independent exchange sockets and cannot be bit-identical; they are reported
(coverage, |B - A| p50/p99/max) and never gate.
"""

from __future__ import annotations

import argparse
import gzip
import sys
from datetime import UTC, datetime
from pathlib import Path

import orjson

from ..lab.brti_import import _iter_rows

COVERAGE_MIN = 0.999
EXAMPLES = 20


def _stamp(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).timestamp())


def _files(root: Path, lo: int, hi: int) -> list[Path]:
    out = []
    for f in sorted([*root.glob("*.jsonl.gz"), *root.glob("*.jsonl")]):
        try:
            hour = datetime.strptime(f.name.split(".")[0], "%Y-%m-%dT%H").replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
        if lo - 3600 <= hour <= hi + 3600:  # a value is written in the hour it is received
            out.append(f)
    return out


def _lines(path: Path):
    op = gzip.open if path.suffix == ".gz" else open
    with op(path, "rb") as fh:
        for line in fh:
            if path.suffix != ".gz" and not line.endswith(b"\n"):
                break  # the writer has not finished this line
            yield line


def _see(table: dict, conflicts: set, sec: int, value: str, received) -> None:
    rcv = float(received) if received is not None else float("inf")
    prior = table.get(sec)
    if prior is None:
        table[sec] = (value, rcv)
        return
    if prior[0] != value:
        conflicts.add(sec)
    if rcv < prior[1]:
        table[sec] = (value, rcv)


def read_stream(root: Path, lo: int, hi: int) -> dict:
    """Publication second -> (value, first receive stamp) for brti and each rti id; replica t -> value."""
    brti: dict[int, tuple[str, float]] = {}
    rti: dict[str, dict[int, tuple[str, float]]] = {}
    conflicts: dict[str, set[int]] = {"brti": set()}
    replica: dict[int, float] = {}
    for f in _files(root, lo, hi):
        for t_ms, v, rcv in _iter_rows(f):
            if lo <= t_ms // 1000 < hi:
                _see(brti, conflicts["brti"], t_ms // 1000, v, rcv)
        for line in _lines(f):
            if line.startswith(b'{"kind":"rti"'):
                rec = orjson.loads(line)
                table = rti.setdefault(rec["id"], {})
                bad = conflicts.setdefault("rti:" + rec["id"], set())
                for t_ms, v in rec["new"]:
                    if lo <= int(t_ms) // 1000 < hi:
                        _see(table, bad, int(t_ms) // 1000, str(v), rec.get("received"))
            elif line.startswith(b'{"kind":"replica"'):
                rec = orjson.loads(line)
                if rec.get("value") is not None and lo <= rec["t"] < hi:
                    replica.setdefault(rec["t"], float(rec["value"]))
    return {"brti": brti, "rti": rti, "conflicts": conflicts, "replica": replica}


def _pct(xs: list[float], q: float):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, round(q * (len(s) - 1))))]


def compare_values(a: dict, b: dict, conflicts_a: set, conflicts_b: set) -> dict:
    overlap = sorted(set(a) & set(b))
    missing = sorted(set(a) - set(b))
    extra = sorted(set(b) - set(a))
    mismatch = [s for s in overlap if a[s][0] != b[s][0]]
    diffs = [(b[s][1] - a[s][1]) * 1000 for s in overlap
             if a[s][1] != float("inf") and b[s][1] != float("inf")]
    coverage = len(overlap) / len(a) if a else 0.0
    ok = bool(a) and not mismatch and not conflicts_b and coverage >= COVERAGE_MIN
    return {
        "pass": ok, "a_seconds": len(a), "b_seconds": len(b), "overlap": len(overlap),
        "coverage": round(coverage, 6), "missing_in_b": len(missing), "missing_examples": missing[:EXAMPLES],
        "extra_in_b": len(extra), "extra_examples": extra[:EXAMPLES],
        "value_mismatch": len(mismatch),
        "mismatch_examples": [{"t": s, "a": a[s][0], "b": b[s][0]} for s in mismatch[:EXAMPLES]],
        "conflicts_a": len(conflicts_a), "conflicts_b": len(conflicts_b),
        "received_b_minus_a_ms": {"p50": _pct(diffs, 0.5), "p99": _pct(diffs, 0.99),
                                  "abs_p99": _pct([abs(d) for d in diffs], 0.99),
                                  "abs_max": max((abs(d) for d in diffs), default=None)},
    }


def compare_replica(a: dict[int, float], b: dict[int, float]) -> dict:
    overlap = sorted(set(a) & set(b))
    diffs = [abs(b[t] - a[t]) for t in overlap]
    return {"gating": False, "a_seconds": len(a), "b_seconds": len(b), "overlap": len(overlap),
            "coverage": round(len(overlap) / len(a), 6) if a else 0.0,
            "abs_diff_usd": {"p50": _pct(diffs, 0.5), "p99": _pct(diffs, 0.99), "max": max(diffs, default=None)}}


def parity(a_dir: Path, b_dir: Path, lo: int, hi: int, gate: frozenset[str] = frozenset({"brti", "rti"})) -> dict:
    a, b = read_stream(a_dir, lo, hi), read_stream(b_dir, lo, hi)
    out: dict = {"window": [lo, hi], "gate": sorted(gate),
                 "brti": compare_values(a["brti"], b["brti"], a["conflicts"]["brti"], b["conflicts"]["brti"]),
                 "rti": {}, "replica": compare_replica(a["replica"], b["replica"])}
    for index in sorted(a["rti"]):
        out["rti"][index] = compare_values(a["rti"][index], b["rti"].get(index, {}),
                                           a["conflicts"].get("rti:" + index, set()),
                                           b["conflicts"].get("rti:" + index, set()))
    gated = []
    if "brti" in gate:
        gated.append(out["brti"]["pass"])
    if "rti" in gate:
        gated += [r["pass"] for r in out["rti"].values()]
    out["pass"] = bool(gated) and all(gated)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-brti-parity")
    ap.add_argument("--a", type=Path, required=True, help="reference stream (PERELMAN)")
    ap.add_argument("--b", type=Path, required=True, help="candidate stream (DOPPEL)")
    ap.add_argument("--from", dest="lo", type=_stamp, required=True, help="unix seconds or ISO-8601, inclusive")
    ap.add_argument("--to", dest="hi", type=_stamp, required=True, help="unix seconds or ISO-8601, exclusive")
    ap.add_argument("--gate", default="brti,rti", help="kinds that decide PASS: brti, rti (default both)")
    a = ap.parse_args(argv)
    gate = frozenset(x.strip() for x in a.gate.split(",") if x.strip())
    if not gate or gate - {"brti", "rti"}:
        ap.error("--gate takes brti and/or rti")
    if a.hi <= a.lo:
        ap.error("--to must be after --from")
    res = parity(a.a, a.b, a.lo, a.hi, gate)
    print(orjson.dumps(res, option=orjson.OPT_INDENT_2).decode())
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
