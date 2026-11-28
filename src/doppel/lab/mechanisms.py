"""Four preregisterable mechanism studies on recorded tape (STATE-AND-PLAN §9.2 item 4).

    doppel-mechanisms --data TAPE --from TS --to TS [--assets btc --horizons 5m,15m]
                      [--study all|flb|final60|overreaction|opening] --out OUT.json

Definitions, thresholds and preregistration stubs: ``docs/MECHANISMS.md``. Summary:

Each study is a pure function of one ``markout.MarketPath`` (receive-time L2 book, settlement, and
for BTC fair-v1 and the received BRTI index), so synthetic paths exercise the same code as tape.
Observations are taken at STATED times, one per window per grid cell, never at every snapshot.
CIs: percentile bootstrap resampling 15-minute clock blocks (5m and 15m windows of one block share
one BTC path). Each study has ONE headline statistic per grid cell, tested at level
1 - 0.05 / grid_size (Bonferroni over the study's stated family); everything else is informational
at 95%. Nothing here searches for a strategy; the grids are fixed below and their sizes reported.

Execution (every net number): a take decided at receive time t walks the L2 book received by t for
exactly ``size`` contracts (SIZES = 25 / 100 / 500) -> decision VWAP; it executes LATENCY_S later
on the book received by t + 0.25 s and fills only if that book also holds the full size at a VWAP
at or better than the decision VWAP (else MISSED; never a partial fill, never "the top holds it").
Each level crossed is one fill with its own cent-rounded fee. Net = signed (mark - VWAP) minus the
fee, in cents per contract, in four fee columns side by side (``markout.TIERS``: gateway_0095,
api_base_0695, api_live_rebated, maker_0; which tier applies to which order path is in the
``markout`` docstring). maker_0 on a crossing fill is a zero-fee bound, not a passive-fill model
(no queue). Net means are over FILLED attempts; the fill rate beside each is filled / attempted.
Marks are the received mid (or settlement), not an exit cost.

(a) ``flb`` favourite-longshot: at start + f * L (f in FLB_FRACS), 5-cent buckets of the mid:
    realized YES rate vs mid and ask; net of buying YES / selling YES (= buying NO) to settlement.
    Headline (gross, fee-free): slope b of (100 y - mid) = a + b (mid - 50). grid_size 3.
(b) ``final60``: at end - o (o in FINAL_OFFSETS s): mid vs fair-v1 error and Brier, and a gap
    trade gated per tier and size: buy when fair - VWAP_ask - fee > THETA_C, sell when
    VWAP_bid - fair - fee > THETA_C, to settlement. Headline: that trade's net c/contract at
    HEAD_SIZE under each tier; family = offsets x tiers (grid_size 12); "pays" iff lower bound > 0,
    stated per tier. Other sizes = capacity curve (informational). BTC only.
(c) ``overreaction``: first trigger per window in [start + OPEN_MASK_S, end - 60): a mid cross of
    50 c, or |z| >= Z (Z in Z_GRID) on the 1-second grid, where
        z(s) = [ln B(s) - ln B(s - MOVE_S)] / (sigma(s) * sqrt(end - s))
    B(s) = BRTI value received by s (``MarketPath.index``), sigma(s)^2 = mean of squared 1-s log
    returns of B ending in (s - MOVE_S - VOL_S, s - MOVE_S] (strictly before the move, so the move
    cannot inflate its own scale; >= MIN_VOL_OBS finite returns and sigma > 0, else no trigger).
    z is the move's shift in the standardized distance to strike over the remaining time.
    dir = sign of the move. Headline (gross): dm(+60 s) = dir * (mid(t+0.25+60) - mid(t+0.25));
    < 0 overreaction, > 0 momentum. grid_size 3. Follow/fade nets at +10 s, +60 s, settlement.
(d) ``opening``: the first OPEN_MASK_S seconds that (c) excludes. At start + o (o in OPEN_OFFSETS)
    with dir = sign(mid - fair-v1): headline (gross) dm(+60 s) as in (c); < 0 = the opening
    mispricing corrects toward fair-v1. Error / Brier vs fair, fade (toward fair) and follow nets.
    Windows where fair-v1 is not yet known (opening not received) are counted as ``unknown``.
    grid_size 2. BTC only.

Venues: PM US only. Kalshi BTC and Polymarket intl books are recorded but not wired here: their
settlement labels are not in the recorded market streams, the repo has no intl fee function, and
fair-v1 is the PM US BTC settlement rule only.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np
import orjson

from .markout import NS, TIERS, MarketPath, ci, cluster_draws, pmus_paths, tier_fee_usd

ALPHA = 0.05
SIZES = (25, 100, 500)
HEAD_SIZE = 100
LATENCY_S = 0.25
FLB_FRACS = (0.25, 0.5, 0.75)
FINAL_OFFSETS = (50, 30, 10)
THETA_C = 1.0
OPEN_MASK_S = 45
OPEN_OFFSETS = (10, 30)
Z_GRID = (1.5, 2.5)
MOVE_S = 60
VOL_S = 900
MIN_VOL_OBS = VOL_S // 2
TRIGGERS = ("cross50", *(f"z{z:g}" for z in Z_GRID))
OR_H = (10, 60, "settle")
N_BOOT = 2000
STUDIES = ("flb", "final60", "overreaction", "opening")

Levels = tuple[tuple[float, float], ...]


# ---------------------------------------------------------------------------- execution
def walk_cost(levels: Levels, size: float) -> tuple[float, dict[str, float]] | None:
    """Exact walk of ``size`` contracts through levels (best first): (VWAP $, fee c/ct per tier),
    one fill per level crossed; None when the book does not hold the full size."""
    need, fills = float(size), []
    for px, q in levels:
        if q <= 0:
            continue
        x = min(q, need)
        fills.append((px, x))
        need -= x
        if need <= 1e-9:
            vwap = sum(px * x for px, x in fills) / size
            return vwap, {t: 100.0 * sum(tier_fee_usd(t, px, x) for px, x in fills) / size for t in TIERS}
    return None


def _side_cost(p: MarketPath, t_s: float, side: str, size: float):
    lv = p.levels_asof(int(round(t_s * NS)))
    if lv is None:
        return None
    return walk_cost(lv[1] if side == "buy" else lv[0], size)


def execute(p: MarketPath, t_s: float, side: str, size: float, limit: float | None = None):
    """Decide at t_s, land LATENCY_S later: (VWAP $, fee c/ct per tier) or None (missed).
    ``limit``: decision VWAP if already known; fills only at or better than it."""
    if limit is None:
        dec = _side_cost(p, t_s, side, size)
        if dec is None:
            return None
        limit = dec[0]
    got = _side_cost(p, t_s + LATENCY_S, side, size)
    if got is None or (side == "buy" and got[0] > limit + 1e-9) or (side == "sell" and got[0] < limit - 1e-9):
        return None
    return got


def _legs(p: MarketPath, t_s: float, side: str, marks: dict, leg: str) -> dict:
    """Columns ``{leg}_fill_{size}`` (1/0) and ``{leg}_{h}_{size}_{tier}`` (net c/ct, None if missed)."""
    sg = 1.0 if side == "buy" else -1.0
    row: dict = {}
    for size in SIZES:
        ex = execute(p, t_s, side, size)
        row[f"{leg}_fill_{size}"] = 0.0 if ex is None else 1.0
        for h, m in marks.items():
            for tier in TIERS:
                row[f"{leg}_{h}_{size}_{tier}"] = (None if ex is None or m is None
                                                    else sg * (m - 100 * ex[0]) - ex[1][tier])
    return row


def _top(p: MarketPath, t_s: float) -> tuple[float, float] | None:
    top = p.top_asof(int(round(t_s * NS)))
    if top is None or top[0] is None or top[1] is None:
        return None
    return top[0], top[1]


def _hk(h) -> str:
    return h if h == "settle" else f"{h}s"


def _event_row(p: MarketPath, t_s: float, d: int, cell) -> dict:
    """Gross dm from the landing mid (unconditional on our fill) plus follow / fade legs."""
    row: dict = {"cell": cell, "cluster": p.cluster, "dir": d, "t": t_s}
    m0 = p.mid_asof(int(round((t_s + LATENCY_S) * NS)))
    marks = {}
    for h in OR_H:
        if h == "settle":
            mh = None if p.outcome is None else 100.0 * p.outcome
        else:
            mh = p.mid_asof(int(round((t_s + LATENCY_S + h) * NS)))
        marks[_hk(h)] = mh
        row[f"dm_{_hk(h)}"] = None if m0 is None or mh is None else d * (mh - m0)
    follow = "buy" if d > 0 else "sell"
    row.update(_legs(p, t_s, follow, marks, "follow"))
    row.update(_legs(p, t_s, "sell" if follow == "buy" else "buy", marks, "fade"))
    return row


# ---------------------------------------------------------------------------- per-window rows
def flb_rows(p: MarketPath, fracs=FLB_FRACS) -> list[dict]:
    if p.outcome is None or p.end is None:
        return []
    out = []
    for f in fracs:
        t = p.start + f * (p.end - p.start)
        top = _top(p, t)
        if top is None:
            continue
        b, a = top
        y = p.outcome
        row = {"cell": f, "cluster": p.cluster, "mid": 50 * (a + b), "ask": 100 * a, "bid": 100 * b, "y": y}
        row.update(_legs(p, t, "buy", {"settle": 100.0 * y}, "yes"))
        row.update(_legs(p, t, "sell", {"settle": 100.0 * y}, "no"))
        out.append(row)
    return out


def final60_rows(p: MarketPath, offsets=FINAL_OFFSETS, theta_c: float = THETA_C) -> list[dict]:
    if p.outcome is None or p.end is None or p.fair is None:
        return []
    out = []
    for o in offsets:
        t = p.end - o
        top, fv = _top(p, t), p.fair(t)
        if top is None or fv is None:
            continue
        b, a = top
        y, f = p.outcome, 100 * fv
        row: dict = {"cell": o, "cluster": p.cluster, "mid": 50 * (a + b), "fair": f, "y": y}
        for size in SIZES:
            cb, cs = _side_cost(p, t, "buy", size), _side_cost(p, t, "sell", size)
            row[f"nodepth_{size}"] = 1.0 if cb is None or cs is None else 0.0
            for tier in TIERS:
                side, lim = None, 0.0
                if cb is not None and f - 100 * cb[0] - cb[1][tier] > theta_c:
                    side, lim = "buy", cb[0]
                elif cs is not None and 100 * cs[0] - f - cs[1][tier] > theta_c:
                    side, lim = "sell", cs[0]
                pnl = None
                if side is not None:
                    ex = execute(p, t, side, size, limit=lim)
                    if ex is not None:
                        sg = 1.0 if side == "buy" else -1.0
                        pnl = sg * (100 * y - 100 * ex[0]) - ex[1][tier]
                row[f"fired_{size}_{tier}"] = 0.0 if side is None else 1.0
                row[f"pnl_{size}_{tier}"] = pnl
        out.append(row)
    return out


def z_series(p: MarketPath, mask_s: float = OPEN_MASK_S) -> list[tuple[int, float]]:
    """(s, z) on the integer receive-second grid in [start + mask, end - 60); see module docstring."""
    if p.index is None or p.end is None:
        return []
    s0, s1 = math.ceil(p.start + mask_s), p.end - 60
    if s1 <= s0:
        return []
    lo = s0 - MOVE_S - VOL_S - 1
    vals = [p.index(float(s)) for s in range(lo, s1)]
    lb = np.log(np.array([v if v is not None and v > 0 else np.nan for v in vals], float))
    r = np.diff(lb)                                    # r[k]: return ending at second lo + k + 1
    ok = np.isfinite(r)
    c2 = np.concatenate([[0.0], np.cumsum(np.where(ok, r * r, 0.0))])
    cn = np.concatenate([[0], np.cumsum(ok)])
    i = np.arange(s0 - lo, s1 - lo)                    # index of s in lb
    a, b = i - MOVE_S - VOL_S, i - MOVE_S              # returns k in [a, b) end in (s-MOVE-VOL, s-MOVE]
    n = cn[b] - cn[a]
    with np.errstate(invalid="ignore", divide="ignore"):
        sig = np.sqrt((c2[b] - c2[a]) / n)
        z = (lb[i] - lb[i - MOVE_S]) / (sig * np.sqrt(p.end - (i + lo)))
    z = np.where((n >= MIN_VOL_OBS) & (sig > 0), z, np.nan)
    return [(int(s), float(v)) for s, v in zip(i + lo, z) if math.isfinite(v)]


def overreaction_rows(p: MarketPath, triggers=TRIGGERS, mask_s: float = OPEN_MASK_S) -> list[dict]:
    if p.end is None:
        return []
    out = []
    zs = None
    for name in triggers:
        hit: tuple[float, int] | None = None
        if name == "cross50":
            prev = None
            for tw in p.tws:
                t_s = tw / NS
                if t_s >= p.end - 60:
                    break
                m = p.mid_asof(tw)
                if m is None:
                    continue
                if prev is not None and t_s >= p.start + mask_s:   # a cross inside the mask is consumed
                    d = 1 if prev < 50 <= m else (-1 if prev >= 50 > m else 0)
                    if d:
                        hit = (t_s, d)
                        break
                prev = m
        else:
            zs = z_series(p, mask_s) if zs is None else zs
            x = float(name[1:])
            hit = next(((float(s), 1 if z > 0 else -1) for s, z in zs if abs(z) >= x), None)
        if hit is not None:
            out.append(_event_row(p, hit[0], hit[1], name))
    return out


def opening_rows(p: MarketPath, offsets=OPEN_OFFSETS) -> list[dict]:
    if p.fair is None or p.end is None:
        return []
    out = []
    for o in offsets:
        t = p.start + o
        top, fv = _top(p, t), p.fair(t)
        if top is None or fv is None:
            out.append({"cell": o, "cluster": p.cluster, "unknown": 1.0})
            continue
        mid, f = 50 * (top[0] + top[1]), 100 * fv
        e = mid - f
        d = 1 if e > 0 else (-1 if e < 0 else 0)
        row = _event_row(p, t, d, o) if d else {"cell": o, "cluster": p.cluster, "dir": 0}
        y = p.outcome
        row.update({"unknown": 0.0, "mid": mid, "fair": f, "err": e, "abs": abs(e),
                    "dbrier": None if y is None else ((mid / 100 - y) ** 2 - (fv - y) ** 2) * 1e4})
        out.append(row)
    return out


# ---------------------------------------------------------------------------- aggregation
def _r(x):
    return None if x is None or not math.isfinite(x) else round(float(x), 4)


def _means(rows: list[dict], cols: list[str], level: float, n_boot: int, seed: int) -> dict[str, dict]:
    """Mean of each column over rows where it is not None, with cluster-bootstrap CIs (shared draws)."""
    if not rows:
        return {c: {"mean": None, "ci": [None, None], "n": 0, "blocks": 0} for c in cols}
    cl = np.array([r["cluster"] for r in rows])
    v = np.array([[np.nan if r.get(c) is None else r[c] for c in cols] for r in rows], float)
    ok = np.isfinite(v)
    est, draws = cluster_draws(cl, np.where(ok, v, 0.0), ok.astype(float), n_boot, seed)
    out = {}
    for j, c in enumerate(cols):
        lo, hi = ci(draws[:, j], level)
        out[c] = {"mean": _r(est[j]), "ci": [_r(lo), _r(hi)], "n": int(ok[:, j].sum()),
                  "blocks": int(len(set(cl[ok[:, j]].tolist())))}
    return out


def _mean(rows, col, level, n_boot, seed) -> dict:
    return _means(rows, [col], level, n_boot, seed)[col]


def _legs_table(rows: list[dict], leg: str, hs, n_boot: int, seed: int) -> dict:
    """Capacity curve: per size, fill rate (filled / attempted) and net c/ct per horizon per tier."""
    out = {}
    for size in SIZES:
        att = [r for r in rows if r.get(f"{leg}_fill_{size}") is not None]
        cols = [f"{leg}_{h}_{size}_{t}" for h in hs for t in TIERS]
        m = _means(att, cols, 0.95, n_boot, seed)
        out[str(size)] = {"attempts": len(att), "fill_rate": _r(np.mean([r[f"{leg}_fill_{size}"] for r in att]))
                          if att else None,
                          "net_c": {h: {t: m[f"{leg}_{h}_{size}_{t}"] for t in TIERS} for h in hs}}
    return out


def _effect(h: dict) -> bool:
    lo, hi = h["ci"]
    return lo is not None and hi is not None and (lo > 0 or hi < 0)


def _slope(rows: list[dict], level: float, n_boot: int, seed: int) -> dict:
    """OLS slope of (y*100 - mid) on (mid - 50), cluster-bootstrapped."""
    if len(rows) < 3:
        return {"mean": None, "ci": [None, None], "n": len(rows), "blocks": 0}
    cl = np.array([r["cluster"] for r in rows])
    x = np.array([r["mid"] - 50.0 for r in rows])
    e = np.array([100.0 * r["y"] - r["mid"] for r in rows])
    ids, inv = np.unique(cl, return_inverse=True)
    C = len(ids)
    sums = np.zeros((C, 5))   # n, sx, sy, sxx, sxy
    np.add.at(sums, inv, np.stack([np.ones_like(x), x, e, x * x, x * e], 1))

    def b(s):
        n, sx, sy, sxx, sxy = s[..., 0], s[..., 1], s[..., 2], s[..., 3], s[..., 4]
        with np.errstate(invalid="ignore", divide="ignore"):
            return (n * sxy - sx * sy) / (n * sxx - sx * sx)
    est = float(b(sums.sum(0)))
    lo = hi = None
    if C >= 2:
        rng = np.random.default_rng(seed)
        draws = np.concatenate([b(sums[rng.integers(0, C, size=(min(250, n_boot - i), C))].sum(1))
                                for i in range(0, n_boot, 250)])
        lo, hi = ci(draws, level)
    return {"mean": _r(est), "ci": [_r(lo), _r(hi)], "n": len(rows), "blocks": C}


def summarize_flb(rows: list[dict], n_boot: int = N_BOOT, seed: int = 0, fracs=FLB_FRACS) -> dict:
    level = 1 - ALPHA / len(fracs)
    cells = {}
    for f in fracs:
        rs = [r for r in rows if r["cell"] == f]
        buckets = {}
        for k in range(20):
            br = [r for r in rs if 5 * k <= r["mid"] < 5 * (k + 1) or (k == 19 and r["mid"] == 100)]
            if not br:
                continue
            ext = [{**r, "ym": 100 * r["y"] - r["mid"], "ya": 100 * r["y"] - r["ask"]} for r in br]
            cols = ["ym", "ya", *(f"{leg}_settle_{HEAD_SIZE}_{t}" for leg in ("yes", "no") for t in TIERS)]
            m = _means(ext, cols, 0.95, n_boot, seed)
            buckets[f"{5 * k:02d}-{5 * k + 5:02d}c"] = {
                "n": len(br), "yes_rate": _r(float(np.mean([r["y"] for r in br]))),
                "mean_mid_c": _r(float(np.mean([r["mid"] for r in br]))),
                "mean_ask_c": _r(float(np.mean([r["ask"] for r in br]))),
                "yes_minus_mid_c": m["ym"], "yes_minus_ask_c": m["ya"],
                f"buy_yes_net_c_at_{HEAD_SIZE}": {t: m[f"yes_settle_{HEAD_SIZE}_{t}"] for t in TIERS},
                f"buy_no_net_c_at_{HEAD_SIZE}": {t: m[f"no_settle_{HEAD_SIZE}_{t}"] for t in TIERS}}
        head = _slope(rs, level, n_boot, seed)
        cells[str(f)] = {"headline_slope": head, "effect": _effect(head), "effect_tier": "gross (fee-free)",
                         "buckets": buckets,
                         "capacity": {"buy_yes": _legs_table(rs, "yes", ["settle"], n_boot, seed),
                                      "buy_no": _legs_table(rs, "no", ["settle"], n_boot, seed)}}
    return {"cells": cells, "effect_any": any(c["effect"] for c in cells.values()),
            "prereg": {"hypothesis": "realized YES rate departs from the mid linearly in the mid "
                                     "(favourite-longshot bias), b != 0",
                       "metric": "OLS slope b of (100 y - mid) on (mid - 50), one observation per window per f",
                       "threshold": f"Bonferroni CI at level {level:.4f} excludes 0", "tier": "gross (fee-free)",
                       "grid": {"f": list(fracs)}, "grid_size": len(fracs),
                       "informational": f"5 c buckets (95% CIs, net at {HEAD_SIZE} per tier) and the capacity "
                                        "curve per size"}}


def summarize_final60(rows: list[dict], n_boot: int = N_BOOT, seed: int = 0, offsets=FINAL_OFFSETS) -> dict:
    grid = len(offsets) * len(TIERS)
    level = 1 - ALPHA / grid
    cells = {}
    for o in offsets:
        rs = [r for r in rows if r["cell"] == o]
        ext = [{**r, "err": r["mid"] - r["fair"], "abs": abs(r["mid"] - r["fair"]),
                "dbrier": ((r["mid"] / 100 - r["y"]) ** 2 - (r["fair"] / 100 - r["y"]) ** 2) * 1e4} for r in rs]
        heads = _means(rs, [f"pnl_{HEAD_SIZE}_{t}" for t in TIERS], level, n_boot, seed)
        heads = {t: heads[f"pnl_{HEAD_SIZE}_{t}"] for t in TIERS}
        cap = {}
        for size in SIZES:
            m = _means(rs, [f"pnl_{size}_{t}" for t in TIERS], 0.95, n_boot, seed)
            cap[str(size)] = {"no_depth": int(sum(r[f"nodepth_{size}"] for r in rs)), "by_tier": {}}
            for t in TIERS:
                fired = int(sum(r[f"fired_{size}_{t}"] for r in rs))
                filled = sum(1 for r in rs if r[f"pnl_{size}_{t}"] is not None)
                cap[str(size)]["by_tier"][t] = {"fired": fired, "filled": filled,
                                                "fill_rate": _r(filled / fired) if fired else None,
                                                "net_c_per_fill": m[f"pnl_{size}_{t}"]}
        info = _means(ext, ["err", "abs", "dbrier"], 0.95, n_boot, seed)
        cells[str(o)] = {f"headline_trade_net_c_at_{HEAD_SIZE}": heads,
                         "effect_by_tier": {t: heads[t]["ci"][0] is not None and heads[t]["ci"][0] > 0 for t in TIERS},
                         "n_windows": len(rs), "capacity": cap,
                         "signed_error_mid_minus_fair_c": info["err"], "abs_gap_c": info["abs"],
                         "brier_mid_minus_fair_x1e4": info["dbrier"]}
    return {"cells": cells,
            "effect_any_by_tier": {t: any(c["effect_by_tier"][t] for c in cells.values()) for t in TIERS},
            "prereg": {"hypothesis": "in the last 60 s the PM US mid lags fair-v1 by more than the taker fee",
                       "metric": f"net P&L (c/contract, per filled trade) of taking the gap at {HEAD_SIZE} contracts "
                                 f"when it exceeds that tier's fee by {THETA_C} c, one decision per window per offset",
                       "threshold": f"Bonferroni CI at level {level:.4f}; pays under a tier iff lower bound > 0",
                       "grid": {"offset_s_before_end": list(offsets), "tiers": list(TIERS), "theta_c": THETA_C,
                                "head_size": HEAD_SIZE},
                       "grid_size": grid, "informational": "sizes 25 / 500 (capacity curve), error, Brier"}}


def _summarize_events(rows: list[dict], cells_of, n_boot: int, seed: int, extra=()) -> dict:
    level = 1 - ALPHA / len(cells_of)
    cells = {}
    for name in cells_of:
        rs = [r for r in rows if r["cell"] == name]
        ev = [r for r in rs if r.get("dir")]
        head = _mean(ev, "dm_60s", level, n_boot, seed)
        hs = [_hk(h) for h in OR_H]
        c: dict = {"headline_dm_60s_c": head, "effect": _effect(head), "effect_tier": "gross (fee-free mid move)",
                   "n_events": len(ev),
                   "reading": None if not _effect(head) else ("overreaction" if (head["mean"] or 0) < 0 else "momentum"),
                   "dm_c": _means(ev, [f"dm_{h}" for h in hs if h != "60s"], 0.95, n_boot, seed),
                   "follow": _legs_table(ev, "follow", hs, n_boot, seed),
                   "fade": _legs_table(ev, "fade", hs, n_boot, seed)}
        if extra:
            c["unknown_fair_or_book"] = int(sum(r.get("unknown", 0) for r in rs))
            known = [r for r in rs if not r.get("unknown")]
            m = _means(known, list(extra), 0.95, n_boot, seed)
            c.update({"signed_error_mid_minus_fair_c": m["err"], "abs_error_c": m["abs"],
                      "brier_mid_minus_fair_x1e4": m["dbrier"]})
        cells[str(name)] = c
    return {"cells": cells, "effect_any": any(c["effect"] for c in cells.values()), "level": level}


def summarize_overreaction(rows: list[dict], n_boot: int = N_BOOT, seed: int = 0, triggers=TRIGGERS) -> dict:
    out = _summarize_events(rows, triggers, n_boot, seed)
    out["prereg"] = {"hypothesis": "after a sharp move outside the opening the PM US mid partly reverses within 60 s",
                     "metric": "dm(+60 s) = dir * (mid(t+0.25+60) - mid(t+0.25)), first trigger per window "
                               f"in [start + {OPEN_MASK_S} s, end - 60 s)",
                     "threshold": f"Bonferroni CI at level {out.pop('level'):.4f} excludes 0 "
                                  "(< 0 overreaction, > 0 momentum)", "tier": "gross (fee-free)",
                     "grid": {"triggers": list(triggers), "z": list(Z_GRID), "move_s": MOVE_S, "vol_s": VOL_S,
                              "min_vol_obs": MIN_VOL_OBS, "opening_mask_s": OPEN_MASK_S},
                     "grid_size": len(triggers)}
    return out


def summarize_opening(rows: list[dict], n_boot: int = N_BOOT, seed: int = 0, offsets=OPEN_OFFSETS) -> dict:
    out = _summarize_events(rows, offsets, n_boot, seed, extra=("err", "abs", "dbrier"))
    out["prereg"] = {"hypothesis": f"in the first {OPEN_MASK_S} s the PM US mid's gap to fair-v1 closes within 60 s",
                     "metric": "dm(+60 s) with dir = sign(mid - fair-v1) at start + o; < 0 = corrects toward fair",
                     "threshold": f"Bonferroni CI at level {out.pop('level'):.4f} excludes 0",
                     "tier": "gross (fee-free)", "grid": {"offset_s_after_start": list(offsets)},
                     "grid_size": len(offsets)}
    return out


def run_paths(paths, studies=STUDIES, n_boot: int = N_BOOT, seed: int = 0) -> dict:
    fn = {"flb": flb_rows, "final60": final60_rows, "overreaction": overreaction_rows, "opening": opening_rows}
    rows: dict[str, list[dict]] = {s: [] for s in studies}
    n = 0
    for p in paths:
        n += 1
        for s in studies:
            rows[s] += fn[s](p)
    summ = {"flb": summarize_flb, "final60": summarize_final60, "overreaction": summarize_overreaction,
            "opening": summarize_opening}
    return {"windows_read": n, "alpha": ALPHA, "bootstrap": {"n_boot": n_boot, "seed": seed, "cluster": "start // 900"},
            "execution": {"sizes": list(SIZES), "head_size": HEAD_SIZE, "latency_s": LATENCY_S,
                          "rule": "L2 walk at decision; fill at t+latency only if full size at VWAP <= decision VWAP "
                                  "(buy) / >= (sell), else missed"},
            "fee_tiers": {t: {"coef": c, "rebate": r} for t, (c, r) in TIERS.items()},
            "studies": {s: summ[s](rows[s], n_boot, seed) for s in studies}}


def _print(res: dict) -> None:
    print(f"windows read={res['windows_read']}")
    for s, v in res["studies"].items():
        for k, c in v["cells"].items():
            if s == "final60":
                h = c[f"headline_trade_net_c_at_{HEAD_SIZE}"]
                print(f"{s}[{k}]: " + " ".join(f"{t}={h[t]['mean']}{h[t]['ci']}(n={h[t]['n']}) pays={c['effect_by_tier'][t]}"
                                               for t in TIERS))
            else:
                h = c.get("headline_slope") or c["headline_dm_60s_c"]
                print(f"{s}[{k}] (gross): {h['mean']}{h['ci']}(n={h['n']}) effect={c['effect']}")


def main(argv=None) -> int:
    from ..twin.cli import parse_ts
    ap = argparse.ArgumentParser(prog="doppel-mechanisms", description="Mechanism studies on PM US tape.")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--from", dest="t_from", type=parse_ts, required=True)
    ap.add_argument("--to", dest="t_to", type=parse_ts, required=True)
    ap.add_argument("--assets", default="btc")
    ap.add_argument("--horizons", default="5m,15m")
    ap.add_argument("--study", choices=("all", *STUDIES), default="all")
    ap.add_argument("--boot", type=int, default=N_BOOT)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    studies = STUDIES if a.study == "all" else (a.study,)
    assets, hz = a.assets.split(","), a.horizons.split(",")
    paths = pmus_paths(a.data, assets, hz, a.t_from, a.t_to)
    res = run_paths(paths, studies, a.boot, a.seed)
    res.update({"built_tw": time.time_ns(), "range": [a.t_from, a.t_to], "venue": "pmus", "assets": assets,
                "horizons": hz, "evidence_scope": "hypotheses to preregister, not findings; no strategy search"})
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2))
    _print(res)
    return 0


if __name__ == "__main__":
    sys.exit(main())
