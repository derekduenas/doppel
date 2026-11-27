"""``doppel-opening-parity``: does the opening computed from received raw BRTI match PM US's A(S-1)?

    doppel-opening-parity --data TAPE --from TS --to TS [--asset btc] [--out OUT.json]

For every 5m and 15m window starting in [from, to) it reads the window reference exactly as the
twin does (``RefStore.window_ref``) and compares the computed opening (``features.computed_opening``,
the ``received-raw-opening`` protocol's rule) with the final published A(S-1): count, max/p99
absolute difference in USD, share within $0.01, and when each source became known relative to the
window start. PASS needs >= 99% of compared windows within $0.01 (decision 2026-11-27); exit 1 on FAIL.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import orjson

from ..twin.cli import parse_ts
from ..twin.evaluate import OPENING_PARITY
from ..twin.features import RefStore, opening_parity

LEN = {"5m": 300, "15m": 900}


def _dist(xs: list[float], length: int) -> dict:
    if not xs:
        return {"n": 0}
    q = np.percentile(xs, [10, 50, 90])
    return {"n": len(xs), "p10_s": round(float(q[0]), 3), "p50_s": round(float(q[1]), 3),
            "p90_s": round(float(q[2]), 3), "max_s": round(max(xs), 3),
            "share_within_30s": round(sum(x <= 30 for x in xs) / len(xs), 4),
            "share_before_end": round(sum(x < length for x in xs) / len(xs), 4)}


def parity(root: Path, asset: str, t_from: int, t_to: int) -> dict:
    store = RefStore(root)
    store.load_boundaries(asset, t_from - 1, t_to + max(LEN.values()))
    out: dict = {"asset": asset, "range": [t_from, t_to], "threshold": OPENING_PARITY, "horizons": {}}
    for hz, length in LEN.items():
        pairs, comp_lag, pub_lag = [], [], []
        first = {"computed": 0, "published": 0, "tie": 0, "neither": 0}
        for start in range(-(-t_from // length) * length, t_to, length):
            ref = store.window_ref(asset, start, start + length, 0)
            o = ref.opening_summary(rule="received_raw")
            pairs.append((o["computed"], ref.ptb))
            tc = o["computed_known_s"] if o["computed"] is not None else None
            tp = o["published_first_s"]
            if tc is not None:
                comp_lag.append(tc - start)
            if tp is not None:
                pub_lag.append(tp - start)
            a, b = (math.inf if tc is None else tc), (math.inf if tp is None else tp)
            first["neither" if a == b == math.inf else "tie" if a == b else "computed" if a < b else "published"] += 1
        res = opening_parity(pairs, **OPENING_PARITY)
        res["missing_computed"] = sum(c is None for c, _ in pairs)
        res["missing_published"] = sum(p is None for _, p in pairs)
        res["known_after_start"] = {"computed": _dist(comp_lag, length), "published": _dist(pub_lag, length)}
        res["first_known"] = first
        out["horizons"][hz] = res
    out["pass"] = all(h["pass"] for h in out["horizons"].values())
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-opening-parity")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--from", dest="t_from", type=parse_ts, required=True)
    ap.add_argument("--to", dest="t_to", type=parse_ts, required=True)
    ap.add_argument("--asset", default="btc", help="only btc has a raw index")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    res = parity(a.data, a.asset, a.t_from, a.t_to)
    doc = orjson.dumps(res, option=orjson.OPT_INDENT_2)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_bytes(doc)
    print(doc.decode())
    for hz, h in res["horizons"].items():
        print(f"{hz}: windows={h['windows']} compared={h['compared']} within ${h['tol_usd']}: "
              f"{h['share_within_tol']} max ${h['max_abs_usd']} p99 ${h['p99_abs_usd']} "
              f"{'PASS' if h['pass'] else 'FAIL'}", file=sys.stderr)
    print("PASS" if res["pass"] else "FAIL", file=sys.stderr)
    return 0 if res["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
