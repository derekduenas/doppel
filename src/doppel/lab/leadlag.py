"""BTC perp mids vs raw BRTI and Coinbase spot: lead-lag on our own receive-time tape.

Sources (all read strictly as of OUR receive time ``tw``; one host clock stamps every stream):
- ``kalshi_perp``: KXBTCPERP mid from ``perp_kalshi_btc`` (WS); if that stream has no book in the
  range, ``perp_kalshi_btc_rest`` (1 Hz REST book) is used and named in the output.
- ``cbde_perp``: BIP-20DEC30-CDE mid from ``perp_cbde_btc``.
- ``coinbase_spot``: BTC-USD mid from ``spot_l2_coinbase``.
- ``brti``: raw BRTI (``ref_brti_raw`` + ``ref_brti_live``, ``brti_import.raw_observations``). A
  publication second becomes known at its FIRST documented receive time; an observation without a
  receive time is dropped. ``ref_brti_live`` polls every 5 s, so BRTI receive times are coarse:
  the output reports receive minus publication lag, and a perp "lead" over BRTI smaller than that
  lag is the poll, not the market.

All mids are taken as log prices in basis points (1e4 ln mid), so venues with different contract
units and price levels compare directly (``perps.py`` already converts to USD per BTC).

a. Cross-correlation. On a 100 ms grid, x = change of X's mid, y = change of Y's mid;
   corr(x[i], y[i+j]) for j = -50..50 (lag +j*0.1 s > 0: Y moves AFTER X, X leads), pooled over
   fixed blocks (``--block-s``, default 300 s; the bootstrap unit, as windows are in bridge.py).
   lead_score_Xs = sum of corr over lags (0, X] minus over [-X, 0): > 0 means X leads.
b. Event follow. A move of X is the first state at which X's mid is >= ``--move-bps`` away from its
   mid at the previous move (or at the first known state after an unknown stretch). It is used if
   INITIATING (Y's mid did not already move >= ``--resp-bps`` the same way over [t - 1 s, t]).
   Y follows within h if its mid reaches ref +/- resp in the same direction within (t, t + h]
   (ref = Y's mid as of t); the opposite-direction rate is the activity baseline; net = same - opp.
   Who leads: net(X->Y) - net(Y->X) at each horizon, with a block bootstrap 95% CI.

    doppel-leadlag --data ROOT --from T0 --to T1 [--out OUT.json]
"""

from __future__ import annotations

import argparse
import bisect
import math
import sys
import time
from pathlib import Path

import numpy as np
import orjson

from .bridge import _corr, _when, boot_ci, lead_score, xcorr_sums
from .brti_import import raw_observations
from .kalshi_ws import segments
from .perps import NS, load

GRID_NS = 100_000_000
LAGS = 50
HORIZONS_S = (0.25, 1.0, 2.0, 5.0)
PRE_NS = NS
EPS = 1e-9
PAIRS = (("kalshi_perp", "brti"), ("kalshi_perp", "coinbase_spot"), ("cbde_perp", "brti"),
         ("cbde_perp", "coinbase_spot"), ("kalshi_perp", "cbde_perp"), ("coinbase_spot", "brti"))


class Mids:
    """As-of mid (bps of log price) of one source: t ascending (ns), v (NaN = unknown)."""

    def __init__(self, t: list[int], v: list[float]):
        self.t = t
        self.v = v

    def at(self, tw: int) -> float:
        i = bisect.bisect_right(self.t, tw) - 1
        return math.nan if i < 0 else self.v[i]

    def grid(self, g: np.ndarray) -> np.ndarray:
        t = np.asarray(self.t, dtype=np.int64)
        if len(t) == 0:
            return np.full(len(g), np.nan)
        i = np.searchsorted(t, g, side="right") - 1
        return np.where(i >= 0, np.asarray(self.v, dtype=float)[np.clip(i, 0, None)], np.nan)


def _bps(x: float | None) -> float:
    return math.nan if x is None or not x > 0 else 1e4 * math.log(x)


def book_mids(root: Path, stream: str, t0: int, t1: int) -> tuple[Mids, dict]:
    f = load(root, stream, t0, t1)
    return Mids([b[0] for b in f.book], [math.nan if b[1] is None else _bps(b[1].mid) for b in f.book]), \
        {"stream": stream, "book_events": len(f.book), "stats": f.stats}


def brti_mids(root: Path, t0: int, t1: int) -> tuple[Mids, dict]:
    """Raw BRTI known at its first receive time; the newest publication second received wins."""
    obs = raw_observations(sorted((root / "ref_brti_raw").glob("*/*.dtape.zst")),
                           segments(root, "ref_brti_live", t0 - 3600, t1 + 60), t0 - 3600, t1 + 60)
    rows = sorted((rcv, pub, v) for pub, (v, rcv) in obs.items() if math.isfinite(rcv) and math.isfinite(v))
    t: list[int] = []
    v: list[float] = []
    newest = -1
    lags = []
    for rcv, pub, val in rows:
        if t0 <= pub <= t1:
            lags.append(rcv - pub)
        if pub > newest:
            newest = pub
            t.append(round(rcv * NS))
            v.append(_bps(val))
    q = {f"p{p}": round(float(np.percentile(lags, p)), 3) for p in (50, 90, 99)} if lags else None
    return Mids(t, v), {"stream": "ref_brti_raw+ref_brti_live", "seconds": len(lags), "receive_lag_s": q,
                        "dropped_no_receive_time": sum(not math.isfinite(r) for r, _ in obs.values())}


def follow_events(x: Mids, y: Mids, lo: int, hi: int, move: float, resp: float) -> list[dict]:
    """Initiating moves of X in [lo, hi - 5 s) and how Y responds (module doc b)."""
    out = []
    anchor = math.nan
    hmax = max(HORIZONS_S) * NS
    for tw, m in zip(x.t, x.v):
        if math.isnan(m) or math.isnan(anchor):
            anchor = m
            continue
        d = m - anchor
        if abs(d) < move - EPS:
            continue
        anchor = m
        if tw < lo or tw + hmax >= hi:
            continue
        s = 1.0 if d > 0 else -1.0
        ref, pre = y.at(tw), y.at(tw - PRE_NS)
        if math.isnan(ref) or math.isnan(pre) or s * (ref - pre) >= resp - EPS:
            continue
        same = opp = None
        i = bisect.bisect_right(y.t, tw)
        while i < len(y.t) and y.t[i] <= tw + hmax:
            ym = y.v[i]
            if math.isnan(ym):
                break
            dt = (y.t[i] - tw) / NS
            if same is None and s * (ym - ref) >= resp - EPS:
                same = dt
            if opp is None and s * (ym - ref) <= -(resp - EPS):
                opp = dt
            if same is not None and opp is not None:
                break
            i += 1
        out.append({"t": tw, "same": same, "opp": opp})
    return out


def _follow(evs: list[dict], blocks: np.ndarray, nb: int) -> tuple[dict, np.ndarray, np.ndarray, np.ndarray]:
    H = len(HORIZONS_S)
    n, same, opp = np.zeros(nb), np.zeros((nb, H)), np.zeros((nb, H))
    delays = []
    for e in evs:
        b = int(np.searchsorted(blocks, e["t"], side="right") - 1)
        n[b] += 1
        for k, h in enumerate(HORIZONS_S):
            same[b, k] += e["same"] is not None and e["same"] <= h
            opp[b, k] += e["opp"] is not None and e["opp"] <= h
        if e["same"] is not None:
            delays.append(e["same"])
    tot = n.sum()
    rows = {}
    for k, h in enumerate(HORIZONS_S):
        ps = float(same[:, k].sum() / tot) if tot else None
        po = float(opp[:, k].sum() / tot) if tot else None
        rows[f"{h:g}s"] = {"p_same": None if ps is None else round(ps, 4), "p_opp": None if po is None else round(po, 4),
                           "net": None if ps is None or po is None else round(ps - po, 4)}
    return ({"events": int(tot), "by_horizon": rows,
             "median_delay_s": round(float(np.median(delays)), 3) if delays else None}, n, same, opp)


def pair(x: Mids, y: Mids, t0: int, t1: int, block_s: int, move: float, resp: float) -> dict:
    """Lead-lag of X vs Y over [t0, t1) (module doc a, b). Positive = X leads."""
    edges = np.arange(t0, t1, block_s, dtype=np.int64) * NS
    nb = len(edges)
    S = np.zeros((nb, 2 * LAGS + 1, 6))
    both = 0
    basis = []
    for b, lo in enumerate(edges):
        hi = min(lo + block_s * NS, t1 * NS)
        g = np.arange(lo, hi + 1, GRID_NS, dtype=np.int64)
        gx, gy = x.grid(g), y.grid(g)
        S[b] = xcorr_sums(gx, gy, LAGS)
        ok = np.isfinite(gx) & np.isfinite(gy)
        both += int(ok.sum())
        basis.append(gx[ok] - gy[ok])
    c = _corr(S.sum(axis=0))
    lags = np.arange(-LAGS, LAGS + 1) * GRID_NS / NS
    finite = np.isfinite(c)
    peak = float(lags[finite][np.argmax(np.abs(c[finite]))]) if finite.any() else None
    res: dict = {
        "grid_points_both_known": both,
        "basis_bps_p50": round(float(np.median(np.concatenate(basis))), 2) if both else None,
        "xcorr": {f"{lag:+.1f}": (None if not np.isfinite(v) else round(float(v), 4)) for lag, v in zip(lags, c)},
        "peak_abs_corr_lag_s": peak,
        "corr_lag0": None if not np.isfinite(c[LAGS]) else round(float(c[LAGS]), 4),
    }
    for k in (1, 5):
        res[f"lead_score_{k}s"] = round(lead_score(c, 10 * k, LAGS), 4)
        res[f"lead_score_{k}s_ci"] = boot_ci(nb, lambda idx, k=k: lead_score(_corr(S[idx].sum(axis=0)), 10 * k, LAGS))
    lo_ns, hi_ns = t0 * NS, t1 * NS
    fx, nx, sx, ox = _follow(follow_events(x, y, lo_ns, hi_ns, move, resp), edges, nb)
    fy, ny, sy, oy = _follow(follow_events(y, x, lo_ns, hi_ns, move, resp), edges, nb)
    res["x_to_y"], res["y_to_x"] = fx, fy
    diff = {}
    for k, h in enumerate(HORIZONS_S):
        def stat(idx, k=k):
            a, b = nx[idx].sum(), ny[idx].sum()
            if a <= 0 or b <= 0:
                return None
            return float((sx[idx, k].sum() - ox[idx, k].sum()) / a - (sy[idx, k].sum() - oy[idx, k].sum()) / b)
        est = stat(np.arange(nb))
        diff[f"{h:g}s"] = {"est": None if est is None else round(est, 4), "ci": boot_ci(nb, stat)}
    res["net_follow_diff_x_minus_y"] = diff
    return res


def run(root: Path, t0: int, t1: int, block_s: int = 300, move: float = 2.0, resp: float = 1.0) -> dict:
    src: dict[str, Mids] = {}
    info: dict[str, dict] = {}
    km, ki = book_mids(root, "perp_kalshi_btc", t0, t1)
    if not any(math.isfinite(v) for v in km.v):
        km, ki = book_mids(root, "perp_kalshi_btc_rest", t0, t1)
    src["kalshi_perp"], info["kalshi_perp"] = km, ki
    src["cbde_perp"], info["cbde_perp"] = book_mids(root, "perp_cbde_btc", t0, t1)
    src["coinbase_spot"], info["coinbase_spot"] = book_mids(root, "spot_l2_coinbase", t0, t1)
    src["brti"], info["brti"] = brti_mids(root, t0, t1)
    pairs = {}
    for a, b in PAIRS:
        if any(math.isfinite(v) for v in src[a].v) and any(math.isfinite(v) for v in src[b].v):
            pairs[f"{a}__{b}"] = pair(src[a], src[b], t0, t1, block_s, move, resp)
    return {"from": t0, "to": t1, "block_s": block_s, "move_bps": move, "resp_bps": resp, "sources": info, "pairs": pairs}


def summary_lines(res: dict) -> list[str]:
    out = []
    for k, v in res["sources"].items():
        out.append(f"{k}: {v}")
    for name, p in res["pairs"].items():
        d1 = p["net_follow_diff_x_minus_y"]["1s"]
        out.append(f"{name}: both-known {p['grid_points_both_known']} pts, basis p50 {p['basis_bps_p50']} bps, "
                   f"corr0 {p['corr_lag0']}, peak lag {p['peak_abs_corr_lag_s']} s, lead_score_1s {p['lead_score_1s']} "
                   f"{p['lead_score_1s_ci']}; events x->y {p['x_to_y']['events']} y->x {p['y_to_x']['events']}; "
                   f"net-follow diff 1s {d1['est']} {d1['ci']}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-leadlag", description=__doc__.split("\n")[0] if __doc__ else None)
    ap.add_argument("--data", type=Path, required=True, help="Rust tape root (perp_*, spot_l2_coinbase, ref_brti_*)")
    ap.add_argument("--from", dest="t0", required=True, help="unix s or ISO (UTC if no offset)")
    ap.add_argument("--to", dest="t1", required=True)
    ap.add_argument("--block-s", type=int, default=300)
    ap.add_argument("--move-bps", type=float, default=2.0)
    ap.add_argument("--resp-bps", type=float, default=1.0)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    t0, t1 = _when(a.t0), _when(a.t1)
    res = run(a.data, t0, t1, a.block_s, a.move_bps, a.resp_bps)
    res["at"] = int(time.time())
    out = a.out or Path(f"leadlag-perps-{t0}-{t1}.json")
    out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS))
    print("\n".join(summary_lines(res)))
    print("->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
