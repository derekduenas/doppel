"""Shadow comparison: does tape B (Rust recorder) capture at least what tape A (Python) captures?

Per stream present in both tapes, over whole UTC hours [from, to): data records, control records
(gaps, reconnects, backpressure drops), and, where the venue stamps messages with an ms ``timestamp``,
our receive lag (receive - venue time) p50/p99. Cutover rule (preregistered here, not tuned):
for every common stream, B has >= 99.5% of A's data records, no more gap/drop control events than A
plus 2 (every control record counts, including subscribes, which errs toward not cutting over), and
B's p99 receive lag is no worse than A's + 50 ms. REST pollers are compared on counts only.

A pass certifies only the streams compared. ``--expect`` names the streams B is supposed to record
(the shadow's ``--only`` list): any of them missing or empty in either tape fails, as does an empty
hour range or a reference tape with no data, so a typo or a unit started without a stream cannot pass.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import orjson

from .segment import read_segment, segment_path

NS = 1_000_000_000
MIN_RECORD_RATIO = 0.995
MAX_EXTRA_GAPS = 2
MAX_EXTRA_P99_MS = 50.0


def _venue_ms(raw: str) -> int | None:
    if not raw or '"timestamp"' not in raw:
        return None
    try:
        o = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return None
    for m in o if isinstance(o, list) else [o]:
        if isinstance(m, dict) and str(m.get("timestamp") or "").isdigit():
            return int(m["timestamp"])
    return None


def stream_stats(root: Path, stream: str, hours: list[int]) -> dict:
    data = ctl = 0
    lag: list[float] = []
    for h in hours:
        f = segment_path(Path(root), stream, h * NS)
        if not f.exists():
            continue
        for i, rec in enumerate(read_segment(f, verify=True)):
            if rec.get("stream") == "ctl" or (rec.get("meta") or {}).get("ctl"):
                ctl += 1
                continue
            data += 1
            if i % 50 == 0:  # sample: enough for percentiles, cheap on 1M-message hours
                v = _venue_ms(rec.get("raw") or "")
                if v is not None:
                    lag.append(rec["tw"] / 1e6 - v)
    lag.sort()

    def q(p: float) -> float | None:
        return round(lag[int(p * (len(lag) - 1))], 1) if lag else None
    return {"data": data, "ctl": ctl, "lag_n": len(lag), "lag_p50_ms": q(0.5), "lag_p99_ms": q(0.99)}


def compare(a: Path, b: Path, t_from: int, t_to: int, expect: list[str] | None = None) -> dict:
    hours = list(range(t_from // 3600 * 3600, t_to, 3600))
    sa = {p.name for p in Path(a).iterdir() if p.is_dir()}
    sb = {p.name for p in Path(b).iterdir() if p.is_dir()}
    rows, ok_all = {}, True
    for s in sorted(sa & sb):
        x, y = stream_stats(a, s, hours), stream_stats(b, s, hours)
        reasons = []
        if x["data"] and y["data"] < MIN_RECORD_RATIO * x["data"]:
            reasons.append(f"records {y['data']}/{x['data']}")
        if y["ctl"] > x["ctl"] + MAX_EXTRA_GAPS:
            reasons.append(f"ctl {y['ctl']} vs {x['ctl']}")
        if x["lag_p99_ms"] is not None and y["lag_p99_ms"] is not None \
                and y["lag_p99_ms"] > x["lag_p99_ms"] + MAX_EXTRA_P99_MS:
            reasons.append(f"p99 lag {y['lag_p99_ms']} vs {x['lag_p99_ms']} ms")
        rows[s] = {"a": x, "b": y, "pass": not reasons, "reasons": reasons}
        ok_all &= not reasons
    problems = [] if hours else ["empty hour range"]
    if not any(r["a"]["data"] for r in rows.values()):
        problems.append("reference tape has no data in range")
    for s in expect or []:
        r = rows.get(s)
        if r is None or not r["a"]["data"] or not r["b"]["data"]:
            problems.append(f"expected stream {s} missing or empty")
    return {"hours": [hours[0], hours[-1] + 3600] if hours else [], "streams": rows,
            "only_a": sorted(sa - sb), "only_b": sorted(sb - sa), "problems": problems,
            "pass": ok_all and not problems}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-tape-compare")
    ap.add_argument("--a", type=Path, required=True, help="reference tape (Python)")
    ap.add_argument("--b", type=Path, required=True, help="candidate tape (Rust)")
    ap.add_argument("--from", dest="t_from", type=int, required=True, help="unix s (hour-aligned)")
    ap.add_argument("--to", dest="t_to", type=int, required=True)
    ap.add_argument("--expect", default="", help="comma list of streams B must record (missing/empty = FAIL)")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    res = compare(a.a, a.b, a.t_from, a.t_to, [s for s in a.expect.split(",") if s])
    if a.out:
        a.out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2))
    for s, r in res["streams"].items():
        print(f"{'PASS' if r['pass'] else 'FAIL'} {s:16} A {r['a']['data']:>9} ctl {r['a']['ctl']:>3} p99 {r['a']['lag_p99_ms']}"
              f" | B {r['b']['data']:>9} ctl {r['b']['ctl']:>3} p99 {r['b']['lag_p99_ms']} {'; '.join(r['reasons'])}")
    print("only in A:", res["only_a"], "| only in B:", res["only_b"])
    for pr in res["problems"]:
        print("PROBLEM:", pr)
    print("CUTOVER:", "PASS" if res["pass"] else "FAIL")
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
