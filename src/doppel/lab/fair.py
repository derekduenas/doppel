"""fair-v0: empirical fair value for PM US crypto Up/Down windows, scored on exact settlement data.

Settlement (FINDINGS E18): A is the published rolling-60 s BRTI average; priceToBeat = A(S-1),
settlement = A(E-1); Up iff A(E-1) >= A(S-1).

Model at decision second d (only data with timestamp <= d is used):
    remaining h = (E-1) - d
    sigma_h  = std of A(t+h) - A(t) over the trailing ``lookback`` seconds (t + h <= d)
    P(Up)    = Phi((A(d) - A(S-1)) / sigma_h)
This is causal and assumption-light: the smoothing of A, its autocorrelation and the current
volatility regime are all captured by measuring the h-step spread directly. It is a baseline for
calibration, NOT an edge claim; market prices are the comparison that matters (next step).
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import orjson

from .refprice import load_series

LEN = {"5m": 300, "15m": 900}
DECISIONS = {"5m": (30, 60, 120, 180, 240, 270, 285, 295),
             "15m": (60, 180, 300, 450, 600, 750, 840, 870, 890)}


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def to_array(series: dict[int, float]):
    t0, t1 = min(series), max(series)
    a = np.full(t1 - t0 + 1, np.nan)
    for t, v in series.items():
        a[t - t0] = v
    return t0, a


def fair_up(a: np.ndarray, t0: int, start: int, d: int, end: int, lookback: int = 1800,
            floor: float = 1e-9) -> float | None:
    h = (end - 1) - d
    i_d, i_open = d - t0, start - 1 - t0
    if i_open < 0 or i_d >= len(a) or np.isnan(a[i_d]) or np.isnan(a[i_open]):
        return None
    diff = a[i_d] - a[i_open]
    if h <= 0:
        return 1.0 if diff >= 0 else 0.0
    lo = i_d - lookback
    if lo < 0:
        return None
    seg = a[lo:i_d + 1]
    steps = seg[h:] - seg[:-h]
    steps = steps[~np.isnan(steps)]
    if len(steps) < 60:
        return None
    sd = float(np.sqrt(np.mean(steps ** 2)))  # zero-drift second moment
    if sd < floor:
        return 1.0 if diff >= 0 else 0.0
    return phi(diff / sd)


def calibrate(series: dict[int, float], hz: str, lookback: int = 1800) -> dict:
    t0, a = to_array(series)
    L = LEN[hz]
    first = (t0 + lookback + L) // L * L + L
    last = (t0 + len(a) - 1) // L * L - L
    rows = []
    for start in range(first, last + 1, L):
        end = start + L
        i_o, i_c = start - 1 - t0, end - 1 - t0
        if np.isnan(a[i_o]) or np.isnan(a[i_c]):
            continue
        y = 1 if a[i_c] >= a[i_o] else 0
        for tau in DECISIONS[hz]:
            p = fair_up(a, t0, start, start + tau, end, lookback)
            if p is not None:
                rows.append((tau, p, y))
    out: dict = {"hz": hz, "n": len(rows), "by_tau": {}}
    arr = np.array(rows, dtype=float) if rows else np.zeros((0, 3))
    out["windows"] = int(len(arr) / max(1, len(DECISIONS[hz])))
    for tau in DECISIONS[hz]:
        m = arr[arr[:, 0] == tau] if len(arr) else arr
        if not len(m):
            continue
        p, y = np.clip(m[:, 1], 1e-6, 1 - 1e-6), m[:, 2]
        base = y.mean()
        brier = float(np.mean((p - y) ** 2))
        brier_base = float(np.mean((base - y) ** 2))
        ll = float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
        bins = []
        for lo, hi in ((0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.0001)):
            k = (p >= lo) & (p < hi)
            if k.sum():
                bins.append({"bin": [lo, round(min(hi, 1), 2)], "n": int(k.sum()),
                             "pred": round(float(p[k].mean()), 4), "obs": round(float(y[k].mean()), 4)})
        out["by_tau"][str(tau)] = {"n": int(len(m)), "up_rate": round(float(base), 4),
                                   "brier": round(brier, 5), "brier_climatology": round(brier_base, 5),
                                   "skill": round(1 - brier / brier_base, 4) if brier_base else None,
                                   "logloss": round(ll, 5), "reliability": bins}
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-fair")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--asset", default="btc")
    ap.add_argument("--hz", default="5m", choices=list(LEN))
    ap.add_argument("--out", type=Path)
    ap.add_argument("--raw", action="store_true", help="fair-v1 on raw BRTI vs v0 (BTC only)")
    a = ap.parse_args(argv)
    if a.raw:
        from .brti_import import load_raw
        raw = load_raw(a.data)
        ser = load_series(a.data, "crypto:btc")
        al = alignment(raw, ser)
        lag = min(al, key=lambda k: al[k]["mae"])
        print("alignment (lag: mae/p99 $):", {k: (v["mae"], v["p99"]) for k, v in al.items()})
        res = calibrate_raw(raw, ser, a.hz, lag)
        res["alignment"] = al
        if a.out:
            a.out.parent.mkdir(parents=True, exist_ok=True)
            a.out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS, default=str))
        print(f"btc {a.hz} windows~{res['windows']} lag={lag}")
        for tau, v in res["by_tau"].items():
            print(f"  tau={tau:>4}s n={v['n']:>4} v1_raw brier={v['v1_raw']['brier']:.4f} "
                  f"ll={v['v1_raw']['logloss']:.4f} | v0_series brier={v['v0_series']['brier']:.4f} "
                  f"ll={v['v0_series']['logloss']:.4f}")
        return 0
    s = load_series(a.data, f"crypto:{a.asset}")
    res = {"asset": a.asset, "span": [min(s), max(s)] if s else None, "seconds": len(s),
           **calibrate(s, a.hz)}
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2))
    print(f"{a.asset} {a.hz} seconds={res['seconds']} windows~{res['windows']}")
    for tau, v in res["by_tau"].items():
        rel = " ".join(f"{b['pred']:.2f}/{b['obs']:.2f}({b['n']})" for b in v["reliability"])
        print(f"  tau={tau:>4}s n={v['n']:>5} brier={v['brier']:.4f} skill={v['skill']:+.3f} ll={v['logloss']:.4f}  pred/obs: {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------- fair-v1 (raw BRTI)
def fair_up_raw(raw: np.ndarray, t0: int, ptb: float, d: int, end: int, lag: int = 0,
                vol_window: int = 300) -> float | None:
    """P(Up) from raw 1 Hz BRTI (btc-window-lab accrued-average model, METHOD.md).

    Settlement = mean of raw over the 60 seconds [end-60-lag, end-1-lag] (``lag`` aligns the PM US
    label convention; measured, see ``alignment``). Known samples count as-is; unknown future
    samples are the last raw value plus a zero-drift random walk with per-second variance from the
    trailing ``vol_window`` increments. Var(final mean) = sigma^2 * sum_k w_k^2, where w_k is the
    fraction of the 60 samples at or after future second k."""
    lo_s, hi_s = end - 60 - lag, end - 1 - lag
    i_d = d - t0
    if i_d - vol_window < 0 or i_d >= len(raw) or np.isnan(raw[i_d]):
        return None
    hist = raw[i_d - vol_window:i_d + 1]
    if np.isnan(hist).any():
        return None
    sig2 = float(np.mean(np.diff(hist) ** 2))
    known = [raw[s - t0] for s in range(lo_s, min(d, hi_s) + 1)]
    if any(np.isnan(known)):
        return None
    m = max(0, hi_s - max(d, lo_s - 1))  # unknown samples
    mean = (sum(known) + m * raw[i_d]) / 60.0
    var = 0.0
    for k in range(d + 1, hi_s + 1):
        w = (hi_s - max(k, lo_s) + 1) / 60.0
        var += w * w
    var *= sig2
    diff = mean - ptb
    if var <= 0:
        return 1.0 if diff >= 0 else 0.0
    return phi(diff / math.sqrt(var))


def alignment(raw: dict[int, float], series: dict[int, float], lags=range(-3, 4)) -> dict:
    """Mean |rolling60(raw) shifted by lag - published series| for each lag (cents)."""
    out = {}
    common = sorted(set(series) & set(raw))
    for lag in lags:
        errs = []
        for t in common[::7]:
            xs = [raw.get(s) for s in range(t - 59 - lag, t + 1 - lag)]
            if None in xs:
                continue
            errs.append(abs(sum(xs) / 60.0 - series[t]))  # type: ignore[arg-type]
        if errs:
            out[lag] = {"n": len(errs), "mae": round(float(np.mean(errs)), 4),
                        "p99": round(float(np.percentile(errs, 99)), 4)}
    return out


def calibrate_raw(raw: dict[int, float], series: dict[int, float], hz: str, lag: int) -> dict:
    t0, r = to_array(raw)
    s0, a = to_array(series)
    L = LEN[hz]
    lo = max(min(raw), min(series)) + 400
    hi = min(max(raw), max(series))
    res: dict = {"hz": hz, "lag": lag, "by_tau": {}}
    rows = []
    for start in range((lo // L + 1) * L, hi - L, L):
        end = start + L
        po, pc = series.get(start - 1), series.get(end - 1)
        if po is None or pc is None:
            continue
        y = 1 if pc >= po else 0
        for tau in DECISIONS[hz]:
            p1 = fair_up_raw(r, t0, po, start + tau, end, lag)
            p0 = fair_up(a, s0, start, start + tau, end)
            if p1 is not None and p0 is not None:
                rows.append((tau, p1, p0, y))
    arr = np.array(rows, dtype=float) if rows else np.zeros((0, 4))
    res["windows"] = int(len(arr) / max(1, len(DECISIONS[hz])))
    for tau in DECISIONS[hz]:
        m = arr[arr[:, 0] == tau] if len(arr) else arr
        if not len(m):
            continue
        y = m[:, 3]
        out: dict = {"n": int(len(m))}
        for name, col in (("v1_raw", 1), ("v0_series", 2)):
            p = np.clip(m[:, col], 1e-6, 1 - 1e-6)
            out[name] = {"brier": round(float(np.mean((p - y) ** 2)), 5),
                         "logloss": round(float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))), 5)}
        res["by_tau"][str(tau)] = out
    return res
