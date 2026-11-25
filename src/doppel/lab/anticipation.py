"""Anticipation test: do a wallet's BTC taker fills REACT to public raw-BRTI moves, or LEAD them?

Per wallet, over TAKER fills in BTC up/down windows (any horizon) whose token is in the token map:
  d      = +1 if the fill adds "BTC up" exposure (BUY Up / SELL Down), else -1
  pre_k  = raw[t] - raw[t-k]            (the move the fill could have reacted to)
  post_k = raw[t+k] - raw[t]            (the move the fill could have anticipated)
  plac_k = raw[t-60] - raw[t-60-k]      (placebo: same statistic a minute earlier, ~50% if nothing)
for k in LAGS. Per lag: n, aligned = share of d*move > 0 among nonzero moves (zeros counted
separately), mean d*move in bp of raw[t], and a 95% bootstrap CI on each aligned share.

A fill is used only if EVERY second needed by every lag, both anchors and the placebo is present,
so all statistics for a wallet share one sample (``n_usable``; dropped fills are counted).

Timing caveat. ``block_timestamp`` has 1 s resolution and the trade happened up to ~1.5 s before its
block, so the true trade time lies in roughly (t-2, t]. At anchor t a LEADER's move can leak into
pre_1..2; at anchor t-1 a REACTOR's trigger can leak into post. Everything is therefore computed at
both anchors (t and t-1) with its own verdict. The headline verdict is anchor t. If a real wallet's
verdict flips between anchors, treat it as unresolved at 1 s resolution.

Bootstrap. Fills with the same second and direction have identical moves (one taker order split
across makers yields several rows), so the bootstrap resamples (second, direction) groups, not rows.
Overlapping windows of nearby seconds are still autocorrelated; CIs are somewhat optimistic.

Verdict (constants below):
  REACTS_TO_PUBLIC_MOVE  some k in PRE_LAGS: pre CI low > max(ALIGN_MIN, placebo aligned at k)
  LEADS_MOVE             no k in PRE_LAGS has pre CI low > ALIGN_MIN, and
                         some k in POST_LAGS: post CI low > ALIGN_MIN
  NO_CLEAR_TIMING        otherwise
Interpretation (lead review). LEADS_MOVE means "trades before the move shows up in BRTI". BRTI
is a once-per-second composite of exchange order books, so a wallet acting on faster PUBLIC feeds
(Coinbase/Binance ticks) also leads BRTI. LEADS_MOVE is therefore NOT evidence of private
information until it is re-tested against the recorded spot tapes (spot_coinbase/kraken/bitstamp).
A lag only counts if it has >= --min-fills nonzero (second, direction) groups. Wallets with fewer
than --min-fills usable fills or groups are labelled TOO_FEW_FILLS and not tested.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import orjson

from .brti_import import load_raw
from .census import window_of
from .fair import to_array
from .ledger import TokenMap, load_fills

LAGS = (1, 2, 3, 5, 10, 30)
PRE_LAGS = (1, 2, 3, 5)  # k <= 5
POST_LAGS = (1, 2, 3, 5)
PLACEBO_OFFSET = 60
ANCHOR_SHIFTS = (0, 1)  # t, t-1
ALIGN_MIN = 0.55
N_BOOT = 2000
SEED = 20261125
CI = (2.5, 97.5)
BOOT_CELLS = 2_000_000  # max resample-matrix cells held at once (memory)

REACTS = "REACTS_TO_PUBLIC_MOVE"
LEADS = "LEADS_MOVE"
NONE = "NO_CLEAR_TIMING"
TOO_FEW = "TOO_FEW_FILLS"


def direction(side: str, idx: int) -> int:
    """+1 if the fill increases exposure to BTC up (BUY Up or SELL Down), else -1."""
    up = idx == 0
    return 1 if (side == "BUY") == up else -1


def btc_taker_fills(rows: list[dict], tmap: TokenMap) -> tuple[np.ndarray, np.ndarray, dict]:
    """(t, d) int arrays for taker fills in BTC windows with a known token, plus exclusion counts."""
    ts, ds = [], []
    skip = {"maker": 0, "unknown_token": 0, "not_btc_window": 0}
    for r in rows:
        if r.get("order_type") != "taker":
            skip["maker"] += 1
            continue
        info = tmap.m.get(r["token_id"])
        if not info or info.get("idx") not in (0, 1):
            skip["unknown_token"] += 1
            continue
        w = window_of(info)
        if not w or w["asset"] != "btc":
            skip["not_btc_window"] += 1
            continue
        ts.append(int(r["block_timestamp"]))
        ds.append(direction(r["side"], int(info["idx"])))
    return np.asarray(ts, dtype=np.int64), np.asarray(ds, dtype=np.int64), skip


def _usable(t: np.ndarray, t0: int, a: np.ndarray) -> np.ndarray:
    """Mask of fills for which every second needed by any lag, anchor and the placebo exists."""
    kmax, smax = max(LAGS), max(ANCHOR_SHIFTS)
    offs = sorted({o for s in ANCHOR_SHIFTS for k in LAGS
                   for o in (-s, -s - k, -s + k, -s - PLACEBO_OFFSET, -s - PLACEBO_OFFSET - k)})
    i = t - t0
    ok = (i - smax - PLACEBO_OFFSET - kmax >= 0) & (i + kmax < len(a))
    for o in offs:
        j = np.clip(i + o, 0, len(a) - 1)  # out-of-range rows are already False in ok
        ok &= ~np.isnan(a[j])
    return ok


def _boot_cis(pos: np.ndarray, nz: np.ndarray, rng: np.random.Generator) -> list:
    """Group bootstrap CIs of sum(pos[:, j]) / sum(nz[:, j]) for every column j at once.

    pos, nz: (groups, stats) counts. Each resample draws groups with replacement; its count
    vector W (resamples x groups) is shared by all stats, so the cost is one matmul per chunk."""
    g, m = pos.shape
    if g == 0:
        return [None] * m
    chunk = max(1, BOOT_CELLS // g)
    num, den = [], []
    done = 0
    while done < N_BOOT:
        b = min(chunk, N_BOOT - done)
        idx = rng.integers(0, g, size=(b, g)) + (np.arange(b) * g)[:, None]
        w = np.bincount(idx.ravel(), minlength=b * g).reshape(b, g).astype(np.float64)
        num.append(w @ pos)
        den.append(w @ nz)
        done += b
    nu, de = np.vstack(num), np.vstack(den)
    out: list = []
    for j in range(m):
        ok = de[:, j] > 0
        if not ok.any() or nz[:, j].sum() == 0:
            out.append(None)
            continue
        lo, hi = np.percentile(nu[ok, j] / de[ok, j], CI)
        out.append([round(float(lo), 4), round(float(hi), 4)])
    return out


def _stat(dm: np.ndarray, ref: np.ndarray, group: np.ndarray, ngroups: int):
    """Point stats for d * move per fill, plus per-group (aligned, nonzero) counts for the CI."""
    pos_f, neg_f = dm > 0, dm < 0
    npos, nneg = int(pos_f.sum()), int(neg_f.sum())
    nz = npos + nneg
    pos_g = np.bincount(group, weights=pos_f, minlength=ngroups)
    nz_g = np.bincount(group, weights=pos_f | neg_f, minlength=ngroups)
    st = {"aligned": round(npos / nz, 4) if nz else None,
          "nonzero": nz, "zeros": int(len(dm) - nz), "nonzero_groups": int((nz_g > 0).sum()),
          "mean_bps": round(float(np.mean(dm / ref) * 1e4), 4) if len(dm) else None,
          "ci95": None}
    return st, pos_g, nz_g


def _verdict(by_lag: dict, min_groups: int) -> str:
    """A lag counts only with >= min_groups independent nonzero (second, direction) groups."""
    def lo(s: dict) -> float | None:
        return s["ci95"][0] if s["ci95"] is not None and s["nonzero_groups"] >= min_groups else None

    pre_above = False
    for k in PRE_LAGS:
        s = by_lag[str(k)]
        p, plac = lo(s["pre"]), s["placebo"]["aligned"]
        if p is not None and p > ALIGN_MIN:
            pre_above = True
            if p > max(ALIGN_MIN, plac if plac is not None else 0.0):
                return REACTS
    if pre_above:  # pre beats 0.55 but not the placebo: not a clean lead either
        return NONE
    for k in POST_LAGS:
        p = lo(by_lag[str(k)]["post"])
        if p is not None and p > ALIGN_MIN:
            return LEADS
    return NONE


def analyze_fills(t: np.ndarray, d: np.ndarray, t0: int, a: np.ndarray, min_fills: int = 30,
                  seed: int = SEED) -> dict:
    """Per-lag timing statistics at each anchor for one wallet's (t, d) fills."""
    ok = _usable(t, t0, a)
    t, d = t[ok], d[ok]
    out: dict = {"n_fills": int(len(ok)), "n_usable": int(ok.sum()),
                 "dropped_missing_brti": int((~ok).sum())}
    # groups: identical (second, direction) -> identical moves
    key = t * 2 + (d > 0)
    _, group = np.unique(key, return_inverse=True)
    group = group.ravel()
    ngroups = int(group.max()) + 1 if len(group) else 0
    out["n_groups"] = ngroups
    if len(t) < min_fills or ngroups < min_fills:
        out["verdict"] = TOO_FEW
        out["verdict_t_minus_1"] = TOO_FEW
        out["anchors"] = {}
        return out
    anchors: dict = {}
    cells, pos_cols, nz_cols = [], [], []
    for s in ANCHOR_SHIFTS:
        i = t - t0 - s
        ref = a[i]
        by_lag = {}
        for k in LAGS:
            moves = {"pre": a[i] - a[i - k], "post": a[i + k] - a[i],
                     "placebo": a[i - PLACEBO_OFFSET] - a[i - PLACEBO_OFFSET - k]}
            row: dict = {"n": int(len(t))}
            for name, mv in moves.items():
                st, pg, ng = _stat(d * mv, ref, group, ngroups)
                row[name] = st
                cells.append(st)
                pos_cols.append(pg)
                nz_cols.append(ng)
            by_lag[str(k)] = row
        anchors["t" if s == 0 else f"t-{s}"] = {"by_lag": by_lag}
    cis = _boot_cis(np.column_stack(pos_cols), np.column_stack(nz_cols),
                    np.random.default_rng(seed))
    for st, ci in zip(cells, cis):
        st["ci95"] = ci
    for an in anchors.values():
        an["verdict"] = _verdict(an["by_lag"], min_fills)
    out["anchors"] = anchors
    out["verdict"] = anchors["t"]["verdict"]
    out["verdict_t_minus_1"] = anchors["t-1"]["verdict"]
    return out


def analyze_wallet(rows: list[dict], tmap: TokenMap, t0: int, a: np.ndarray,
                   min_fills: int = 30, seed: int = SEED) -> dict:
    t, d, skip = btc_taker_fills(rows, tmap)
    res = analyze_fills(t, d, t0, a, min_fills=min_fills, seed=seed)
    return {"excluded": skip, **res}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-anticipation")
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--lab", type=Path, required=True)
    ap.add_argument("--cohort", type=Path, required=True)
    ap.add_argument("--min-fills", type=int, default=30)
    a = ap.parse_args(argv)
    cohort = orjson.loads(a.cohort.read_bytes())
    cands, ctrls = list(cohort["candidates"]), list(cohort["controls"])
    members = sorted(set(cands) | set(ctrls))
    raw = load_raw(a.data)
    if not raw:
        print("no raw BRTI in tape", file=sys.stderr)
        return 1
    t0, arr = to_array(raw)
    span = [min(raw), max(raw)]
    del raw
    tmap = TokenMap(a.lab / "tokens.jsonl")  # as is: no Gamma fetch (unknown tokens are counted)
    fills = load_fills(a.data, set(members))
    report = {"cohort_sha256": cohort["sha256"], "built_tw": time.time_ns(),
              "brti_span": span, "min_fills": a.min_fills,
              "params": {"lags": LAGS, "pre_lags": PRE_LAGS, "post_lags": POST_LAGS,
                         "placebo_offset": PLACEBO_OFFSET, "align_min": ALIGN_MIN,
                         "n_boot": N_BOOT, "seed": SEED},
              "wallets": {}}
    for w in members:
        rows = fills.pop(w, [])
        res = analyze_wallet(rows, tmap, t0, arr, min_fills=a.min_fills)
        del rows
        role = "cand" if w in cands else "ctrl"
        report["wallets"][w] = {"role": role, **res}
        print(f"{w[:12]} {role} n={res['n_usable']:>6} (btc_taker={res['n_fills']}) "
              f"-> {res['verdict']} [t-1: {res['verdict_t_minus_1']}]", flush=True)
    stamp = time.strftime("%Y%m%dT%H%MZ", time.gmtime())
    out = a.lab / "anticipation" / f"anticipation-{cohort['sha256'][:12]}-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
