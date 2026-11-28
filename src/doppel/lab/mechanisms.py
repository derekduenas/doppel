"""Three preregisterable mechanism studies on recorded tape (STATE-AND-PLAN §9.2 item 4).

    doppel-mechanisms --data TAPE --from TS --to TS [--assets btc --horizons 5m,15m] --out OUT.json

Each study is a pure function of one ``markout.MarketPath`` (receive-time top of book, settlement,
and for BTC fair-v1 and the received BRTI index), so synthetic paths exercise the same code as tape.
Observations are taken at STATED times, one per window per grid cell, never at every snapshot.
CIs: percentile bootstrap resampling 15-minute clock blocks (5m and 15m windows of one block share
one BTC path), at level 1 - 0.05 / grid_size (Bonferroni over the study's grid). Each study has ONE
headline statistic per grid cell; ``effect`` is true when its Bonferroni CI excludes zero (for
(b), whose question is "does the gap pay", only when the lower bound is above zero). Nothing
here searches for a strategy; the grids are fixed below and their sizes reported.

(a) ``flb`` favourite-longshot: at start + f * L (f in FLB_FRACS), 5-cent buckets of the mid:
    realized YES rate vs mean mid and mean ask, and the net P&L of buying YES at the ask / NO at the
    bid after the venue taker fee. Headline: slope b of (y - mid) = a + b (mid - 0.5) (cents per
    cent). b > 0 = favourites underpriced / longshots overpriced.
(b) ``final60``: at end - o (o in FINAL_OFFSETS s), mid vs fair-v1: signed error mid - fair,
    Brier of each against the outcome, and a fixed gap trade decided on the book at t: buy YES when
    fair - ask - fee > THETA_C, sell YES when bid - fair - fee > THETA_C, executed LATENCY_S later as
    an IOC limited to the decision price (no fill if the book moved away; counted as missed).
    Headline: that trade's net P&L per contract. BTC only (fair-v1 is PM US BTC settlement).
(c) ``overreaction``: first trigger per window in [start, end - 60): a mid cross of 50 c, or a
    trailing 15-minute BRTI move of at least X bps (X in BRTI_BPS); direction = sign of the move.
    Entry after LATENCY_S at the then-current book. Follow = take in the move's direction, fade =
    the opposite. Headline: the signed mid move dm(+60 s) = dir * (mid(t + 60) - mid(t)); < 0 =
    overreaction (fading pays before costs), > 0 = momentum. Follow and fade net P&L at +10 s,
    +60 s and settlement are reported beside it.

Venues: PM US only. Fees: ``xvenue.pmus_fee`` (coef * p * (1-p), cent half-even) at a stated
order size (``--size``, default 100 contracts; rounding makes the per-contract fee size-dependent).
Kalshi BTC and Polymarket intl books are recorded, but these studies are not wired for them: their
settlement labels are not in the recorded market streams (intl resolutions live in the ledger's
Gamma ``tokens.jsonl`` cache, Kalshi results in ``xvenue``'s metadata cache), the repo has no intl
fee function, and fair-v1 is the PM US BTC settlement rule only. No depth check: fills assume the
top level holds ``--size`` contracts.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path
from typing import Callable

import numpy as np
import orjson

from .markout import NS, MarketPath, ci, cluster_draws, pmus_paths

ALPHA = 0.05
FLB_FRACS = (0.25, 0.5, 0.75)
FINAL_OFFSETS = (50, 30, 10)
THETA_C = 1.0
BRTI_BPS = (20, 50)
TRIGGERS = ("cross50", *(f"brti{x}" for x in BRTI_BPS))
LATENCY_S = 0.25
MOVE_S = 900
OR_H = (10, 60, "settle")
N_BOOT = 2000
SIZE = 100.0

Fee = Callable[[float], float]   # price (0-1) -> taker fee in cents per contract


def pmus_fee_c(coef: float, size: float = SIZE) -> Fee:
    from .xvenue import pmus_fee
    return lambda p: 100.0 * pmus_fee(p, size, coef=coef) / size


# ---------------------------------------------------------------------------- per-window rows
def _top(p: MarketPath, t_s: float) -> tuple[float, float] | None:
    top = p.top_asof(int(round(t_s * NS)))
    if top is None or top[0] is None or top[1] is None:
        return None
    return top[0], top[1]


def flb_rows(p: MarketPath, fee: Fee, fracs=FLB_FRACS) -> list[dict]:
    if p.outcome is None or p.end is None:
        return []
    out = []
    for f in fracs:
        top = _top(p, p.start + f * (p.end - p.start))
        if top is None:
            continue
        b, a = top
        y = p.outcome
        out.append({"cell": f, "cluster": p.cluster, "mid": 50 * (a + b), "ask": 100 * a, "bid": 100 * b, "y": y,
                    "pnl_yes": 100 * y - 100 * a - fee(a), "pnl_no": 100 * b - 100 * y - fee(b)})
    return out


def final60_rows(p: MarketPath, fee: Fee, offsets=FINAL_OFFSETS, theta_c: float = THETA_C) -> list[dict]:
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
        pnl, missed = None, False
        land = _top(p, t + LATENCY_S)          # decided on the book at t, executed LATENCY_S later
        if f - 100 * a - fee(a) > theta_c:
            if land is not None and land[1] <= a + 1e-9:     # IOC with limit = the decision ask
                pnl = 100 * y - 100 * land[1] - fee(land[1])
            missed = pnl is None
        elif 100 * b - f - fee(b) > theta_c:
            if land is not None and land[0] >= b - 1e-9:
                pnl = 100 * land[0] - 100 * y - fee(land[0])
            missed = pnl is None
        out.append({"cell": o, "cluster": p.cluster, "mid": 50 * (a + b), "fair": f, "y": y, "pnl": pnl,
                    "missed": missed})
    return out


def _trigger(p: MarketPath, name: str, t_s: float, prev_mid: float | None, mid: float) -> int:
    if name == "cross50":
        if prev_mid is None:
            return 0
        return 1 if prev_mid < 50 <= mid else (-1 if prev_mid >= 50 > mid else 0)
    if p.index is None:
        return 0
    now, then = p.index(t_s), p.index(t_s - MOVE_S)
    if now is None or then is None or then <= 0:
        return 0
    bps = 1e4 * (now / then - 1)
    x = float(name[4:])
    return 1 if bps >= x else (-1 if bps <= -x else 0)


def overreaction_rows(p: MarketPath, fee: Fee, triggers=TRIGGERS) -> list[dict]:
    if p.end is None:
        return []
    out = []
    for name in triggers:
        prev = None
        for tw in p.tws:
            t_s = tw / NS
            if t_s >= p.end - 60:
                break
            m = p.mid_asof(tw)
            if m is None:
                continue
            d = _trigger(p, name, t_s, prev, m) if t_s >= p.start else 0
            prev = m
            if d == 0:
                continue
            top = _top(p, t_s + LATENCY_S)
            if top is None:
                break
            b, a = top
            mid0 = 50 * (a + b)
            row: dict = {"cell": name, "cluster": p.cluster, "dir": d, "t": t_s}
            for h in OR_H:
                if h == "settle":
                    mh = None if p.outcome is None else 100.0 * p.outcome
                else:
                    mh = p.mid_asof(int(round((t_s + LATENCY_S + h) * NS)))
                key = f"{h}s" if h != "settle" else "settle"
                if mh is None:
                    row[f"dm_{key}"] = row[f"follow_{key}"] = row[f"fade_{key}"] = None
                    continue
                row[f"dm_{key}"] = d * (mh - mid0)
                if d > 0:      # follow buys YES at the ask, fade sells YES at the bid
                    row[f"follow_{key}"] = mh - 100 * a - fee(a)
                    row[f"fade_{key}"] = 100 * b - mh - fee(b)
                else:
                    row[f"follow_{key}"] = 100 * b - mh - fee(b)
                    row[f"fade_{key}"] = mh - 100 * a - fee(a)
            out.append(row)
            break
    return out


# ---------------------------------------------------------------------------- aggregation
def _r(x):
    return None if x is None or not math.isfinite(x) else round(float(x), 4)


def _mean(rows: list[dict], col: str, level: float, n_boot: int, seed: int) -> dict:
    rs = [r for r in rows if r.get(col) is not None]
    if not rs:
        return {"mean": None, "ci": [None, None], "n": 0, "blocks": 0}
    cl = np.array([r["cluster"] for r in rs])
    v = np.array([r[col] for r in rs], float)
    est, draws = cluster_draws(cl, v, np.ones_like(v), n_boot, seed)
    lo, hi = ci(draws[:, 0], level)
    return {"mean": _r(est[0]), "ci": [_r(lo), _r(hi)], "n": len(rs), "blocks": int(len(set(cl.tolist())))}


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


def _effect(h: dict) -> bool:
    lo, hi = h["ci"]
    return lo is not None and hi is not None and (lo > 0 or hi < 0)


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
            buckets[f"{5 * k:02d}-{5 * k + 5:02d}c"] = {
                "n": len(br), "yes_rate": _r(float(np.mean([r["y"] for r in br]))),
                "mean_mid_c": _r(float(np.mean([r["mid"] for r in br]))),
                "mean_ask_c": _r(float(np.mean([r["ask"] for r in br]))),
                "yes_minus_mid_c": _mean([{**r, "v": 100 * r["y"] - r["mid"]} for r in br], "v", 0.95, n_boot, seed),
                "yes_minus_ask_c": _mean([{**r, "v": 100 * r["y"] - r["ask"]} for r in br], "v", 0.95, n_boot, seed),
                "buy_yes_net_c": _mean(br, "pnl_yes", 0.95, n_boot, seed),
                "buy_no_net_c": _mean(br, "pnl_no", 0.95, n_boot, seed)}
        head = _slope(rs, level, n_boot, seed)
        cells[str(f)] = {"headline_slope": head, "effect": _effect(head), "buckets": buckets}
    return {"cells": cells, "effect_any": any(c["effect"] for c in cells.values()),
            "prereg": {"hypothesis": "realized YES rate departs from the mid linearly in the mid "
                                     "(favourite-longshot bias), b != 0",
                       "metric": "OLS slope b of (100 y - mid) on (mid - 50), one observation per window per f",
                       "threshold": f"Bonferroni CI at level {level:.4f} excludes 0",
                       "grid": {"f": list(fracs)}, "grid_size": len(fracs),
                       "buckets": "5 c by mid (informational, 95% CIs, not tested)"}}


def summarize_final60(rows: list[dict], n_boot: int = N_BOOT, seed: int = 0, offsets=FINAL_OFFSETS) -> dict:
    level = 1 - ALPHA / len(offsets)
    cells = {}
    for o in offsets:
        rs = [r for r in rows if r["cell"] == o]
        ext = [{**r, "err": r["mid"] - r["fair"], "abs": abs(r["mid"] - r["fair"]),
                "dbrier": ((r["mid"] / 100 - r["y"]) ** 2 - (r["fair"] / 100 - r["y"]) ** 2) * 1e4} for r in rs]
        head = _mean(rs, "pnl", level, n_boot, seed)
        pays = head["ci"][0] is not None and head["ci"][0] > 0
        cells[str(o)] = {"headline_trade_net_c": head, "effect": pays, "n_windows": len(rs),
                         "missed_fills": sum(1 for r in rs if r["missed"]),
                         "signed_error_mid_minus_fair_c": _mean(ext, "err", 0.95, n_boot, seed),
                         "abs_gap_c": _mean(ext, "abs", 0.95, n_boot, seed),
                         "brier_mid_minus_fair_x1e4": _mean(ext, "dbrier", 0.95, n_boot, seed)}
    return {"cells": cells, "effect_any": any(c["effect"] for c in cells.values()),
            "prereg": {"hypothesis": "in the last 60 s the PM US mid lags fair-v1 by more than the taker fee",
                       "metric": f"net P&L (c/contract) of taking the gap when it exceeds the fee by {THETA_C} c, "
                                 "one decision per window per offset",
                       "threshold": f"Bonferroni CI at level {level:.4f} excludes 0 (pays iff lower bound > 0)",
                       "grid": {"offset_s_before_end": list(offsets), "theta_c": THETA_C}, "grid_size": len(offsets)}}


def summarize_overreaction(rows: list[dict], n_boot: int = N_BOOT, seed: int = 0, triggers=TRIGGERS) -> dict:
    level = 1 - ALPHA / len(triggers)
    cells = {}
    for name in triggers:
        rs = [r for r in rows if r["cell"] == name]
        head = _mean(rs, "dm_60s", level, n_boot, seed)
        c: dict = {"headline_dm_60s_c": head, "effect": _effect(head), "n_events": len(rs),
                   "reading": (None if not _effect(head) else ("overreaction" if (head["mean"] or 0) < 0 else "momentum"))}
        for k in ("10s", "60s", "settle"):
            for leg in ("dm", "follow", "fade"):
                if leg == "dm" and k == "60s":
                    continue
                c[f"{leg}_{k}_c"] = _mean(rs, f"{leg}_{k}", 0.95, n_boot, seed)
        cells[name] = c
    return {"cells": cells, "effect_any": any(c["effect"] for c in cells.values()),
            "prereg": {"hypothesis": "after a sharp move the PM US mid partly reverses within 60 s",
                       "metric": "dm(+60 s) = dir * (mid(t+0.25+60) - mid(t+0.25)), first trigger per window",
                       "threshold": f"Bonferroni CI at level {level:.4f} excludes 0 (< 0 overreaction, > 0 momentum)",
                       "grid": {"triggers": list(triggers), "brti_move_window_s": MOVE_S},
                       "grid_size": len(triggers)}}


def run_paths(paths, fee: Fee, studies=("flb", "final60", "overreaction"), n_boot: int = N_BOOT,
              seed: int = 0) -> dict:
    rows: dict[str, list[dict]] = {s: [] for s in studies}
    n = 0
    for p in paths:
        n += 1
        if "flb" in rows:
            rows["flb"] += flb_rows(p, fee)
        if "final60" in rows:
            rows["final60"] += final60_rows(p, fee)
        if "overreaction" in rows:
            rows["overreaction"] += overreaction_rows(p, fee)
    summ = {"flb": summarize_flb, "final60": summarize_final60, "overreaction": summarize_overreaction}
    return {"windows_read": n, "alpha": ALPHA, "bootstrap": {"n_boot": n_boot, "seed": seed, "cluster": "start // 900"},
            "studies": {s: summ[s](rows[s], n_boot, seed) for s in studies}}


def main(argv=None) -> int:
    from ..twin.cli import parse_ts
    ap = argparse.ArgumentParser(prog="doppel-mechanisms", description="Mechanism studies on PM US tape.")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--from", dest="t_from", type=parse_ts, required=True)
    ap.add_argument("--to", dest="t_to", type=parse_ts, required=True)
    ap.add_argument("--assets", default="btc")
    ap.add_argument("--horizons", default="5m,15m")
    ap.add_argument("--study", choices=("all", "flb", "final60", "overreaction"), default="all")
    ap.add_argument("--pmus-coef", type=float, default=0.095)
    ap.add_argument("--size", type=float, default=SIZE)
    ap.add_argument("--boot", type=int, default=N_BOOT)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    studies = ("flb", "final60", "overreaction") if a.study == "all" else (a.study,)
    assets, hz = a.assets.split(","), a.horizons.split(",")
    paths = pmus_paths(a.data, assets, hz, a.t_from, a.t_to)
    res = run_paths(paths, pmus_fee_c(a.pmus_coef, a.size), studies, a.boot, a.seed)
    res.update({"built_tw": time.time_ns(), "range": [a.t_from, a.t_to], "venue": "pmus", "assets": assets,
                "horizons": hz, "fee": {"pmus_coef": a.pmus_coef, "order_size": a.size, "rounding": "cent half-even"},
                "evidence_scope": "hypotheses to preregister, not findings; no strategy search"})
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2))
    print(f"windows read={res['windows_read']}")
    for s, v in res["studies"].items():
        heads = {k: (c.get("headline_slope") or c.get("headline_trade_net_c") or c.get("headline_dm_60s_c"))
                 for k, c in v["cells"].items()}
        print(f"{s}: effect_any={v['effect_any']} " + " ".join(f"{k}:{h['mean']}{h['ci']}(n={h['n']})" for k, h in heads.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
