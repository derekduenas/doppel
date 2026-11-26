"""Generate the parity fixtures for crates/doppel-fair from the Python references.

    uv run python tests/fixtures_fair/gen_fixtures.py                 # regenerate both fixtures
    uv run python tests/fixtures_fair/gen_fixtures.py --vendor ~/perelman/brti.py
    uv run python tests/fixtures_fair/gen_fixtures.py --snapshot ~/perelman/snap

fair_v1.json      inputs + outputs of doppel.lab.fair.fair_up_raw (fair-v1)
brti_replica.json inputs + outputs of PERELMAN brti.py (vendored verbatim as perelman_brti.py, so
                  regeneration and validation never need ~/perelman)
snapshot_books.json  recorded constituent books (PERELMAN snap/, 2026-11-24), top levels only

Floats are written as Python repr strings (exact round trip in both languages); NaN/None = null.
Times: the Rust replica takes integer ns, brti.py takes float seconds (= ns / 1e9).
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REF = HERE / "perelman_brti.py"
FAIR_JSON = HERE / "fair_v1.json"
REPLICA_JSON = HERE / "brti_replica.json"
SNAP_JSON = HERE / "snapshot_books.json"
PERELMAN_COMMIT = "9b55eae3cb7a2030989d18086d7bdee20f51f7ad"  # ~/perelman HEAD when vendored
SNAP_LEVELS = 300
T0 = 1795622400  # 2026-11-25 16:00:00Z, a multiple of 900
S = 1_000_000_000


def f2s(x) -> str | None:
    if x is None:
        return None
    x = float(x)
    return None if math.isnan(x) else repr(x)


def s2f(x) -> float:
    return float("nan") if x is None else float(x)


def load_ref():
    spec = importlib.util.spec_from_file_location("perelman_brti", REF)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------ fair-v1
def fair_paths() -> list[dict]:
    paths = []
    rng = np.random.default_rng(20261125)
    n = 2400
    p0 = 91234.5678 + np.cumsum(rng.normal(0, 2.0, n))                   # calm
    p1 = 60000.0 + np.cumsum(rng.normal(0, 15.0, n))                    # volatile, with holes
    p1[700] = np.nan
    p1[1650:1656] = np.nan
    p1[2150] = np.nan
    p2 = np.full(n, 60000.25)                                           # flat, then a walk
    p2[1500:] += np.cumsum(rng.normal(0, 3.0, n - 1500))
    p3 = 75000.0 + 0.25 * np.cumsum(rng.choice([-1.0, 1.0], n))          # dyadic: exact sums
    for a in (p0, p1, p2, p3):
        paths.append({"t0": T0, "raw": [f2s(v) for v in a]})
    return paths


def _ptb(raw: np.ndarray, t0: int, start: int, lag: int) -> float:
    lo, hi = start - 60 - lag - t0, start - 1 - lag - t0
    seg = raw[lo:hi + 1] if lo >= 0 else raw[0:0]
    if len(seg) == 60 and not np.isnan(seg).any():
        return float(sum(seg) / 60.0)
    v = raw[max(0, start - 1 - t0)]
    return 60000.0 if np.isnan(v) else float(v)


def _tie_ptb(raw: np.ndarray, t0: int, d: int, end: int, lag: int) -> float | None:
    """The exact expected mean fair_up_raw computes (same float ops), so diff == 0."""
    lo_s, hi_s = end - 60 - lag, end - 1 - lag
    i_d = d - t0
    if not (0 <= i_d < len(raw)) or lo_s - t0 < 0:
        return None
    known = [raw[s - t0] for s in range(lo_s, min(d, hi_s) + 1)]
    m = max(0, hi_s - max(d, lo_s - 1))
    v = (sum(known) + m * raw[i_d]) / 60.0
    return None if np.isnan(v) else float(v)


def fair_cases(paths: list[dict]) -> list[dict]:
    cases = []
    offsets = (300, 240, 120, 61, 60, 59, 45, 30, 5, 1, 0, -1, -5, -100, -239)
    for pi, path in enumerate(paths):
        raw = np.array([s2f(v) for v in path["raw"]])
        t0 = path["t0"]
        n = len(raw)
        for end in range(t0 + 600, t0 + n + 301, 300):
            start = end - 300
            for lag, vw in ((0, 300), (0, 60), (1, 300), (-2, 600), (3, 301)):
                ptb = _ptb(raw, t0, start, lag)
                for off in offsets:
                    d = end - off
                    ptbs = [ptb, ptb + 7.5]
                    tie = _tie_ptb(raw, t0, d, end, lag)
                    if tie is not None and off in (30, 1, 0, -5):
                        ptbs.append(tie)
                    for pt in ptbs:
                        cases.append({"path": pi, "ptb": f2s(pt), "d": d, "end": end, "lag": lag, "vol_window": vw})
    return cases


def fair_outputs(paths: list[dict], cases: list[dict]) -> list[str | None]:
    from doppel.lab.fair import fair_up_raw
    arrs = [np.array([s2f(v) for v in p["raw"]]) for p in paths]
    out = []
    for c in cases:
        p = fair_up_raw(arrs[c["path"]], paths[c["path"]]["t0"], float(c["ptb"]), c["d"], c["end"],
                        c["lag"], c["vol_window"])
        out.append(f2s(p))
    return out


def build_fair() -> dict:
    paths = fair_paths()
    cases = fair_cases(paths)
    for c, p in zip(cases, fair_outputs(paths, cases)):
        c["p"] = p
    return {"meta": {"reference": "doppel.lab.fair.fair_up_raw", "tolerance": 1e-9,
                     "python": sys.version.split()[0], "numpy": np.__version__},
            "paths": paths, "cases": cases}


# ------------------------------------------------------------------ replica
class Rng:
    """Tiny deterministic generator so the inputs never depend on numpy's bit stream."""

    def __init__(self, seed: int):
        self.s = seed

    def u(self) -> float:
        self.s = (self.s * 6364136223846793005 + 1442695040888963407) % 2**64
        return (self.s >> 11) / 2**53


def ladder(mid, half, step, n, size):
    return ([[mid - half - i * step, size] for i in range(n)], [[mid + half + i * step, size] for i in range(n)])


def random_book(r: Rng, mid: float, n: int, tick: float = 0.01) -> tuple[list, list]:
    bids, asks = [], []
    pb, pa = mid - tick * (1 + int(r.u() * 30)), mid + tick * (1 + int(r.u() * 30))
    for _ in range(n):
        bids.append([round(pb, 2), round(math.exp(r.u() * 7 - 5), 8)])
        asks.append([round(pa, 2), round(math.exp(r.u() * 7 - 5), 8)])
        pb -= tick * (1 + int(r.u() * 80))
        pa += tick * (1 + int(r.u() * 80))
    return bids, asks


def venue(name, book, retrieved_ns):
    bids, asks = book
    return {"venue": name, "retrieved_ns": retrieved_ns,
            "bids": [[f2s(p), f2s(s)] for p, s in bids], "asks": [[f2s(p), f2s(s)] for p, s in asks]}


def step(calc_ns, venues):
    return {"calc_ns": calc_ns, "venues": venues}


def replica_scenarios() -> list[dict]:
    sc = []
    T = 100 * S
    sc.append({"name": "symmetric_ladder", "steps": [step(T, [venue("a", ladder(90000.0, 1.0, 0.5, 200, 0.4), T)])]})
    b1 = ladder(90000.0, 1.0, 0.5, 100, 0.2)
    sc.append({"name": "consolidation", "steps": [step(T, [venue("a", b1, T), venue("b", b1, T)])]})
    hb, ha = ladder(90000.0, 1.0, 0.5, 200, 0.4)
    hb[0][1] = 5000.0
    sc.append({"name": "huge_order_capped", "steps": [step(T, [venue("a", (hb, ha), T)])]})
    sb, sa = ladder(90000.0, 1.0, 0.5, 200, 0.4)
    sa = [[p + 20.0 * i / 200, s] for i, (p, s) in enumerate(sa)]
    sc.append({"name": "skewed", "steps": [step(T, [venue("a", (sb, sa), T)])]})
    sc.append({"name": "delayed_and_crossed", "steps": [step(T, [
        venue("a", ladder(90000.0, 1.0, 0.5, 100, 0.4), T),
        venue("b", ladder(80000.0, 1.0, 0.5, 100, 0.4), 80 * S),
        venue("c", ([[90010.0, 1.0]], [[90000.0, 1.0]]), T),
        venue("d", ([[0.0, 1.0], [-5.0, 2.0]], [[90000.0, 1.0]]), T)])]})

    def hys(c_mid):
        return [venue("a", ladder(90000.0, 1.0, 0.5, 100, 0.4), 0), venue("b", ladder(90000.0, 1.0, 0.5, 100, 0.4), 0),
                venue("c", ladder(c_mid, 1.0, 0.5, 100, 0.4), 0)]
    sc.append({"name": "hysteresis", "steps": [step(0, hys(95000.0)), step(0, hys(93000.0)), step(0, hys(91000.0)),
                                               step(0, hys(95500.0)), step(0, hys(92500.0))]})
    sc.append({"name": "all_delayed", "steps": [step(T, [venue("a", ladder(9e4, 1, .5, 10, .4), 0)])]})
    sc.append({"name": "all_outliers", "steps": [step(T, [venue("a", ladder(100.0, 0.1, 0.1, 50, 1.0), T),
                                                          venue("b", ladder(200.0, 0.1, 0.1, 50, 1.0), T)])]})
    sc.append({"name": "wide_spread_empty_curve",
               "steps": [step(T, [venue("a", ladder(90500.0, 500.0, 1.0, 60, 0.3), T)])]})
    sc.append({"name": "tiny_sides", "steps": [step(T, [venue("a", ([[100.0, 1.0], [99.9, 2.0], [99.8, 0.5]],
                                                                    [[100.1, 0.7], [100.2, 1.1], [100.3, 3.0]]), T)])]})
    alt = ([[90000.0 - 0.5 * i, 0.1 if i % 2 else 0.3] for i in range(120)],
           [[90001.0 + 0.5 * i, 0.3 if i % 2 else 0.1] for i in range(120)])
    sc.append({"name": "two_valued_sizes", "steps": [step(T, [venue("a", alt, T)])]})
    # staleness boundary: lag exactly 10 s is kept, 10 s + 1 ns is delayed
    c20 = 20 * S
    sc.append({"name": "staleness_boundary", "steps": [step(c20, [
        venue("a", ladder(90000.0, 1.0, 0.5, 80, 0.4), 10 * S),
        venue("b", ladder(90002.0, 1.0, 0.5, 80, 0.3), 10 * S - 1)])]})
    r = Rng(7)
    for k in range(4):
        vs = []
        for j, name in enumerate(("coinbase", "kraken", "bitstamp", "gemini", "lmax")):
            bids, asks = random_book(r, 60000.0 + (r.u() - 0.5) * 10.0, 60 + int(r.u() * 140))
            if k == 1 and j == 0:  # this venue's bid crosses the others' asks: consolidated uncross
                bids.insert(0, [round(asks[0][0] - 0.01, 2), 0.75])
                bids = sorted(bids, key=lambda x: -x[0])
                asks = [a for a in asks if a[0] > bids[0][0]]
            if k == 2 and j == 1:  # dirty levels and duplicate prices
                bids += [[bids[3][0], 0.5], [bids[5][0], 0.0], [-1.0, 1.0], [bids[7][0], float("nan")]]
                asks += [[asks[2][0], 1.25], [0.0, 1.0]]
            vs.append(venue(name, (bids, asks), T - int(r.u() * 3 * S)))
        if k == 3:
            vs[4]["retrieved_ns"] = T - 11 * S
        sc.append({"name": f"random_{k}", "steps": [step(T, vs)]})
    if SNAP_JSON.exists():
        snap = json.loads(SNAP_JSON.read_text())
        sc.append({"name": "recorded_snapshot_2026_11_24",
                   "steps": [step(T, [{**v, "retrieved_ns": T} for v in snap["venues"]])]})
    return sc


def index_cases() -> list[dict]:
    """brti_from_books called directly (no venue filters)."""
    def bk(bids, asks):
        return {"bids": [[f2s(p), f2s(s)] for p, s in bids], "asks": [[f2s(p), f2s(s)] for p, s in asks]}
    return [
        {"name": "empty", "books": []},
        {"name": "side_consumed", "books": [bk([[101.0, 5.0]], [[100.0, 1.0], [100.5, 1.0]])]},
        {"name": "self_crossed_uncrosses", "books": [bk([[100.2, 0.3], [100.0, 1.0], [99.5, 2.0], [99.0, 1.0]],
                                                        [[100.1, 0.2], [100.6, 1.0], [101.0, 2.0], [101.5, 1.0]])]},
    ]


def _levels(xs):
    return [(s2f(p), s2f(s)) for p, s in xs]


def _diag(value, d):
    return {"value": f2s(value), "cap": f2s(d["cap"]), "spacing": f2s(d["spacing"]),
            "utilized_depth": f2s(d["utilized_depth"]), "levels_used": d["levels_used"]}


def replica_outputs(scenarios: list[dict], index: list[dict], brti) -> None:
    for scn in scenarios:
        rep = brti.Replica()
        for st in scn["steps"]:
            books = {v["venue"]: brti.Book(_levels(v["bids"]), _levels(v["asks"]), v["retrieved_ns"] / 1e9)
                     for v in st["venues"]}
            try:
                value, d = rep.calculate(books, st["calc_ns"] / 1e9)
                st["expect"] = {**_diag(value, d), "venues": d["venues"]}
            except brti.CalculationFailure as e:
                st["expect"] = {"error": str(e)}
            st["excluded_after"] = sorted(rep.excluded)
    for c in index:
        books = [brti.Book(_levels(b["bids"]), _levels(b["asks"]), 0.0) for b in c["books"]]
        try:
            c["expect"] = _diag(*brti.brti_from_books(books))
        except brti.CalculationFailure as e:
            c["expect"] = {"error": str(e)}


def build_replica() -> dict:
    brti = load_ref()
    scenarios, index = replica_scenarios(), index_cases()
    replica_outputs(scenarios, index, brti)
    return {"meta": {"reference": "PERELMAN brti.py (CF RTI methodology v17.0)", "perelman_commit": PERELMAN_COMMIT,
                     "brti_py_sha256": sha256(REF), "python": sys.version.split()[0],
                     "note": "times: Rust takes ns; brti.py was given ns / 1e9 seconds"},
            "scenarios": scenarios, "index_cases": index}


# ------------------------------------------------------------------ recorded snapshot
def build_snapshot(snap: Path) -> dict:
    def pairs(x):
        return [(float(p), float(s)) for p, s, *_ in x]
    books = {}
    j = json.loads((snap / "coinbase.json").read_text())
    books["coinbase"] = (pairs(j["bids"]), pairs(j["asks"]))
    k = next(iter(json.loads((snap / "kraken.json").read_text())["result"].values()))
    books["kraken"] = (pairs(k["bids"]), pairs(k["asks"]))
    j = json.loads((snap / "bitstamp.json").read_text())
    books["bitstamp"] = (pairs(j["bids"]), pairs(j["asks"]))
    j = json.loads((snap / "gemini.json").read_text())
    books["gemini"] = ([(float(x["price"]), float(x["amount"])) for x in j["bids"]],
                       [(float(x["price"]), float(x["amount"])) for x in j["asks"]])
    d = json.loads((snap / "cryptocom.json").read_text())["result"]["data"][0]
    books["cryptocom"] = (pairs(d["bids"]), pairs(d["asks"]))
    j = json.loads((snap / "bullish.json").read_text())
    books["bullish"] = ([(float(x["price"]), float(x["priceLevelQuantity"])) for x in j["bids"]],
                        [(float(x["price"]), float(x["priceLevelQuantity"])) for x in j["asks"]])
    out = []
    for name, (bids, asks) in books.items():
        bids = sorted(bids, key=lambda x: -x[0])[:SNAP_LEVELS]
        asks = sorted(asks, key=lambda x: x[0])[:SNAP_LEVELS]
        out.append(venue(name, (bids, asks), 0))
    return {"meta": {"source": "PERELMAN snap/ REST books, 2026-11-24", "levels_per_side": SNAP_LEVELS},
            "venues": out}


def write(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, separators=(",", ":")) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vendor", type=Path, help="copy this brti.py in as perelman_brti.py first")
    ap.add_argument("--snapshot", type=Path, help="rebuild snapshot_books.json from a PERELMAN snap/ dir")
    a = ap.parse_args(argv)
    if a.vendor:
        shutil.copyfile(a.vendor, REF)
    if a.snapshot:
        write(SNAP_JSON, build_snapshot(a.snapshot))
    write(FAIR_JSON, build_fair())
    write(REPLICA_JSON, build_replica())
    print(f"wrote {FAIR_JSON.name}, {REPLICA_JSON.name} (brti.py sha256 {sha256(REF)[:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
