"""Skill-decay sensor (STATE-AND-PLAN §9.2 item 3): is a strategy's probability still better than the
market mid, and has that edge trended to zero?

Input rows: one forecast each, ``(t, p, p_mkt, y)`` = time, the strategy's P(YES), the market mid as
a probability, and the realized outcome (0/1). Rows are ordered by ``t`` and cut into consecutive
windows of ``window_size`` rows. Per window k:

    LL_x  = mean log-loss of forecast x,  Brier_x = mean squared error of x
    dLL_k = LL_mkt - LL_strat            (> 0: the strategy beats the market)

Probabilities are clipped to [CLIP, 1 - CLIP] (CLIP = 1e-4) so one confident miss cannot dominate
a window.

Detector: one-sided Page-Hinkley with a FIXED reference of zero edge, on the standardized series
x_k = dLL_k / sigma (sigma = std of dLL over the first ``warmup`` windows, or given):

    U_k = sum_{i <= k} (x_i - delta),   M_k = max_{i <= k} U_i,   g_k = M_k - U_k
    DECAY when g_k > lam

(the same statistic as a CUSUM g_k = max(0, g_{k-1} + delta - x_k)). The classic Page-Hinkley uses
the running mean as reference and fires on ANY downward shift; with the zero reference it fires when
the edge falls below ``delta`` sigma, i.e. "trends to <= 0", and a drop that leaves the edge well
above ``delta`` sigma is not flagged by design. Defaults delta = 0.5, lam = 8: after the edge goes to
zero the statistic drifts up at ~0.5 per window, so the expected delay is ~(lam + 1.17) / 0.5 ~ 18
windows; under a steady edge of mu >= 2 * delta sigma per window the Siegmund approximation gives an
average run length of ~2e4 windows (tests measure both over seeds). A steady edge smaller than
~delta sigma per window is not resolvable at this window size and WILL be flagged: lengthen windows.
The detector starts after the warmup (sigma is unknown before), so a decay inside warmup is missed.

Informational sensor for the edge-decay watchdog (§9 item 5); there is no watchdog in the repo yet,
so ``watchdog_update`` is the streaming entry point it can call once per closed window.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import orjson

CLIP = 1e-4
DELTA = 0.5
LAM = 8.0
WARMUP = 10


def _ll(p: np.ndarray, y: np.ndarray) -> np.ndarray:
    p = np.clip(p, CLIP, 1 - CLIP)
    return -(y * np.log(p) + (1 - y) * np.log(1 - p))


def window_scores(rows, window_size: int) -> list[dict]:
    """rows: iterable of (t, p, p_mkt, y). Consecutive windows of ``window_size`` rows (a short last
    window is dropped)."""
    arr = np.array(sorted(rows, key=lambda r: r[0]), dtype=float).reshape(-1, 4)
    out = []
    for k in range(len(arr) // window_size):
        w = arr[k * window_size:(k + 1) * window_size]
        p, pm, y = w[:, 1], w[:, 2], w[:, 3]
        ll_s, ll_m = float(_ll(p, y).mean()), float(_ll(pm, y).mean())
        out.append({"k": k, "t0": float(w[0, 0]), "t1": float(w[-1, 0]), "n": len(w),
                    "ll_strat": round(ll_s, 6), "ll_mkt": round(ll_m, 6),
                    "brier_strat": round(float(np.mean((np.clip(p, 0, 1) - y) ** 2)), 6),
                    "brier_mkt": round(float(np.mean((np.clip(pm, 0, 1) - y) ** 2)), 6),
                    "dll": ll_m - ll_s})
    return out


def watchdog_update(state: dict | None, dll: float, *, delta: float = DELTA, lam: float = LAM,
                    warmup: int = WARMUP, sigma: float | None = None) -> dict:
    """Feed one closed window's dLL; returns the new JSON-serializable state with ``status`` in
    WARMUP | OK | DECAY (DECAY latches)."""
    s = dict(state) if state else {"k": -1, "warm": [], "sigma": sigma, "u": 0.0, "m": 0.0, "g": 0.0,
                                   "status": "WARMUP", "alarm_k": None}
    s["k"] += 1
    if s["sigma"] is None:
        s["warm"] = [*s["warm"], float(dll)]
        if len(s["warm"]) >= warmup:
            sd = float(np.std(s["warm"], ddof=1))
            s["sigma"] = sd if sd > 0 else 1e-12
            s["status"] = "OK"
        return s
    if s["status"] == "WARMUP":
        s["status"] = "OK"
    s["u"] += dll / s["sigma"] - delta
    s["m"] = max(s["m"], s["u"])
    s["g"] = s["m"] - s["u"]
    if s["status"] != "DECAY" and s["g"] > lam:
        s["status"], s["alarm_k"] = "DECAY", s["k"]
    return s


def detect(dlls, *, delta: float = DELTA, lam: float = LAM, warmup: int = WARMUP,
           sigma: float | None = None) -> dict:
    s: dict | None = None
    g = []
    for x in dlls:
        s = watchdog_update(s, x, delta=delta, lam=lam, warmup=warmup, sigma=sigma)
        g.append(round(s["g"], 4))
    s = s or {"status": "WARMUP", "alarm_k": None, "sigma": sigma}
    return {"status": s["status"], "alarm_window": s["alarm_k"], "sigma": s["sigma"], "g": g,
            "params": {"delta": delta, "lam": lam, "warmup": warmup, "clip": CLIP}}


def _rows(path: Path):
    for line in path.read_bytes().splitlines():
        if not line.strip():
            continue
        r = orjson.loads(line)
        p_mkt = r.get("p_mkt", r.get("mid"))
        if r.get("p") is None or p_mkt is None or r.get("y") is None:
            continue
        p_mkt = float(p_mkt)
        if p_mkt > 1.0:      # mid given in cents
            p_mkt /= 100.0
        yield (float(r.get("t", 0.0)), float(r["p"]), p_mkt, float(r["y"]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-skilldecay", description="Skill-decay sensor.")
    ap.add_argument("--input", type=Path, required=True,
                    help="JSONL rows {t, p, p_mkt (or mid, 0-1 or cents), y}")
    ap.add_argument("--window-size", type=int, default=50)
    ap.add_argument("--delta", type=float, default=DELTA)
    ap.add_argument("--lam", type=float, default=LAM)
    ap.add_argument("--warmup", type=int, default=WARMUP)
    ap.add_argument("--sigma", type=float)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    wins = window_scores(list(_rows(a.input)), a.window_size)
    res = detect([w["dll"] for w in wins], delta=a.delta, lam=a.lam, warmup=a.warmup, sigma=a.sigma)
    for w in wins:
        w["dll"] = round(w["dll"], 6)
    doc = {"windows": wins, "detector": res, "evidence_scope": "informational sensor, never a gate"}
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
    mean = sum(w["dll"] for w in wins) / len(wins) if wins else math.nan
    print(f"windows={len(wins)} mean dLL={mean:.5f} status={res['status']} alarm_window={res['alarm_window']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
