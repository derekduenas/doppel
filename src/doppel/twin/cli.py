"""doppel-twin: replay a genome on our PM US tape and print a falsifiable verdict.

    doppel-twin --data TAPE --genome GENOME.json [--from TS --to TS] [--latency-ms 250] --out OUT.json

TS is unix seconds or ISO-8601 UTC (e.g. 2026-11-26T00:00:00Z). Without --from/--to the span of
the PM US WS tape is used. Output: per-window results (fills, fees, markouts, inventory path, maker
quality), the verdict, the config and a sha256 of the twin's own source so a result names the code
that produced it.
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import sys
from dataclasses import asdict
from pathlib import Path

import orjson

from ..strategy.genome import Genome, describe, from_dict, validate
from ..tape.universe import HORIZONS
from .engine import SimConfig, run_windows
from .features import RefStore
from .tape import iter_windows, tape_span
from .verdict import verdict

SRC = Path(__file__).resolve().parent


def code_sha256() -> str:
    """sha256 over the twin package, the genome contract and fair-v1 sources."""
    files = sorted(SRC.glob("*.py")) + [SRC.parent / "strategy" / "genome.py", SRC.parent / "lab" / "fair.py"]
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


def parse_ts(s: str) -> int:
    s = s.strip()
    if s.lstrip("-").isdigit():
        return int(s)
    from .tape import NS, iso_ns
    v = iso_ns(s if "T" in s else s + "T00:00:00Z")
    if v is None:
        raise argparse.ArgumentTypeError(f"bad timestamp {s!r}")
    return v // NS


def run_twin(root: Path, g: Genome, t_from: int, t_to: int, cfg: SimConfig = SimConfig(),
             allow_stale_rest: bool = False) -> list[dict]:
    if g.universe.venue != "pmus":
        raise ValueError(f"the twin replays PM US only (genome venue {g.universe.venue})")
    store = RefStore(root)
    for a in g.universe.assets:
        store.load_boundaries(a, t_from - 1, t_to)
    items = ((wt, store.window_ref(wt.asset, wt.start, wt.end, g.fair.vol_window_s))
             for wt in iter_windows(root, g.universe.assets, g.universe.horizons, t_from, t_to,
                                    cfg.depth_levels, allow_stale_rest))
    return run_windows(g, items, cfg)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-twin")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--genome", type=Path, required=True)
    ap.add_argument("--from", dest="t_from", type=parse_ts)
    ap.add_argument("--to", dest="t_to", type=parse_ts)
    ap.add_argument("--latency-ms", type=float, default=250.0)
    ap.add_argument("--brti-delay-s", type=int, default=1)
    ap.add_argument("--fair-lag-s", type=int, default=0)
    ap.add_argument("--depth-levels", type=int, default=10)
    ap.add_argument("--block", type=int, default=12, help="bootstrap block length in windows")
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--allow-stale-rest", action="store_true",
                    help="diagnostics: also simulate REST-only windows (CDN-stale books); never in the verdict")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    g = from_dict(orjson.loads(a.genome.read_bytes()))
    errs = validate(g)
    if errs:
        print("genome invalid: " + "; ".join(errs), file=sys.stderr)
        return 2
    t_from, t_to = a.t_from, a.t_to
    if t_from is None or t_to is None:
        span = tape_span(a.data)
        if span is None:
            print("no PM US WS tape under --data", file=sys.stderr)
            return 2
        t_from = span[0] if t_from is None else t_from
        t_to = span[1] if t_to is None else t_to
    for h in g.universe.horizons:
        if h not in HORIZONS:
            print(f"unknown horizon {h}", file=sys.stderr)
            return 2
    cfg = SimConfig(latency_ms=a.latency_ms, brti_delay_s=a.brti_delay_s, fair_lag_s=a.fair_lag_s,
                    depth_levels=a.depth_levels)
    results = run_twin(a.data, g, t_from, t_to, cfg, a.allow_stale_rest)
    v = verdict(g, results, a.block, a.boot, a.seed)
    doc = {"twin": {"code_sha256": code_sha256(), "config": asdict(cfg), "allow_stale_rest": a.allow_stale_rest},
           "genome": {"id": g.id, "name": g.name, "status": g.status, "describe": describe(g)},
           "range": [t_from, t_to], "verdict": v, "windows": results}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS))
    print(describe(g))
    pw, pc = v["net_c_per_window"], v["net_c_per_contract"]
    print(f"{v['decision']}: {v['reason']}")
    print(f"windows evaluated={v['windows_evaluated']} traded={v['windows_traded']} excluded={v['excluded']} "
          f"| net/window {pw['mean']}c CI{pw['ci95']} | net/contract {pc['value']}c CI{pc['ci95']} "
          f"| total ${v['total_net_usd']} fees ${v['fees_usd']} maxDD ${v['max_drawdown_usd']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
