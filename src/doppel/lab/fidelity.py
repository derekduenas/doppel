"""doppel-fidelity: does this genome behave like this wallet?

    doppel-fidelity --data TAPE --lab LAB --genome G.json --wallet ADDR --orders ORDERTAPE \
                    --from TS --to TS --out DIR

The genome is replayed by the twin on the Polymarket INTL book (``twin/intl.py``) for every window
of its universe in [from, to], whether the wallet traded it or not, and its simulated fills are
scored against the wallet's real fills from the order tape. A reconstruction is accepted only if
the verdict below is PASS. ``--lab`` holds ``tokens.jsonl`` (token -> window slug, Up/Down index,
payout); it is read, never refreshed.

WALLET FILLS (order tape rows with ``maker == wallet``; ``verified`` must be true unless
``--allow-unverified``, which also takes rows the store could not check)
- Maker row: filled at its own limit; shares = ``fill_amount`` in maker-amount units (USDC / limit
  for a BUY, shares for a SELL).
- Taker row: one fill per maker order of the same tx, at that maker's limit (its mirror 1 - limit
  when the maker traded the other token: mint / merge), for that maker's filled shares. The taker's
  own limit is not its price (fixture tx 0x4f5bc2: limit 0.87, executed at 0.77). If the tx's maker
  rows are not all in the tape the row falls back to its limit (``taker_price_from_limit``).
- Everything is put in UP terms, the twin's one instrument: direction +1 = more Up exposure (BUY
  Up, SELL Down), price = p for the Up token and 1 - p for the Down token. P&L is unchanged by this
  (payouts sum to 1).
- Time = venue ms of the tx's print on that window's tokens. A fill without a print is placed at
  block time - 2 s for inventory and P&L and left out of fill matching (``located_share``).

CLONE FILLS: the twin's fills (engine clock: our receive time), same form.

WINDOWS
- eligible: the twin simulated it (tokens known, book coverage, outcome known, no maker feed gap).
- comparable: eligible AND traded by the wallet or the clone. Windows neither traded carry no
  information and are only counted. Every score is over the comparable windows.
- Wallet volume in windows that are not comparable is reported (``coverage``), not scored.

SCORES (each with a 95% CI from resampling comparable windows with replacement)
- activity: share of comparable windows traded by both / wallet only / clone only. ``agreement`` =
  the share traded by both (both / either: idle-idle never counts as agreement).
- fills, matched on (window, direction, |price| <= 1 c, |time| <= 2 s):
  ``precision_count`` / ``recall_count``: one-to-one pairs (closest in time first) over clone /
  wallet fills. ``precision_shares`` / ``recall_shares``: sum of min(qty) over those pairs over
  each side's shares. Scale-free: ``precision_volume`` = share of the clone's volume in fills with
  at least one matching wallet fill, ``recall_volume`` = share of the wallet's volume in fills with
  at least one matching clone fill (each side over its own volume, no size comparison).
- position path: net Up inventory at the end of each second 0..L of the window. ``corr`` (pooled
  over window-seconds), ``mad`` (mean |clone - wallet|, shares), and the same for the final
  inventory (``end_corr``, ``end_mad``). Scale-free: ``mad_scaled`` / ``end_mad_scaled`` multiply
  the clone by k and divide by the wallet's mean gross volume per comparable window.
- P&L per window, both sides from their fills with the SAME outcome and the genome's fee model
  (taker / maker coefficient by role): ``corr``, ``slope`` (clone on wallet), ``level_ratio`` =
  sum clone / sum wallet. Scale-free: ``slope_scaled`` and ``level_ratio_scaled`` multiply by k
  (the ratio of net per contract).
- k = wallet gross shares / clone gross shares over the eligible windows.
- A score with an empty denominator or zero variance is null, and null never supports PASS.

NULL BASELINE: the same scores for a time-shuffled wallet. Within each (asset, horizon) the
wallet's per-window fill sets (empty ones included) are moved to a different eligible window
(seeded derangement), keeping time-in-window, direction and price, and settled with the new
window's outcome. ``N_NULL`` shuffles; mean and max per score.

VERDICT: ``THRESHOLDS`` and ``GATES`` below, PROVISIONAL, fixed before any real-data run.

Analysis only: nothing here is a decision input. The twin's decisions use data received by the
decision second; the wallet's fills are read only by the scorer.
"""

from __future__ import annotations

import argparse
import bisect
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import orjson
import pyarrow.parquet as pq

from ..strategy.genome import Costs, Genome, describe, from_dict, validate
from ..twin.engine import SimConfig, fee_usd
from ..twin.intl import IntlWindow, Key, run_intl, windows_from_tokens
from .census import window_of
from .flow import _ts
from .ledger import TokenMap
from .ordermatch import filled_shares

TOL_PX = 0.01
TOL_S = 2.0
BLOCK_SLACK_S = 2      # a fill without a print traded up to ~2 s before its block (dossier's rule)
N_BOOT = 2000
N_NULL = 20
EPS = 1e-9

# ---- PREREGISTERED VERDICT (PROVISIONAL, 2026-11-26: set before any real-data run) -------------
# PASS: every row passes. FAIL: any row fails. Otherwise INCONCLUSIVE, naming the open rows.
# (score, statistic that must clear the threshold to pass, threshold, must beat the shuffle)
#   pass  = statistic >= threshold ("ci_low": the CI's lower bound > threshold)
#   fail  = the CI's upper bound < threshold
#   shuffle = the score must also exceed EVERY shuffled-wallet value (a permutation test: with
#             N_NULL = 20 shuffles, p <= 1/21), or the row stays open. Activity is exempt: a wallet
#             active in every window keeps its activity under any shuffle. A row whose shuffle
#             value is undefined stays open.
# The gated fill scores are the ANY-MATCH volume shares (scale-free): one wallet fill can stand for
# several clone fills and the reverse. Read them next to the one-to-one ``*_count`` scores.
THRESHOLDS = (
    ("activity.agreement", "value", 0.7, False),
    ("fills.recall_volume", "value", 0.5, True),
    ("fills.precision_volume", "value", 0.5, True),
    ("pnl.corr", "ci_low", 0.3, True),
)
# Data gates: when one is not met the verdict is INCONCLUSIVE whatever the scores say.
GATES = {
    "min_comparable_windows": 30,
    "min_located_share": 0.9,     # wallet fills in comparable windows timed by a print
    "max_abs_print_lag_s": 2.0,   # |median receive - venue time| of prints: the two clocks agree
    "min_wallet_fills": 1,        # no scored wallet fill = no wallet data, not a silent wallet
    "min_usable_rows_share": 0.9, # wallet rows that are verified and priced (else the tape is not the wallet)
}


@dataclass(frozen=True, slots=True)
class Fill:
    t: float        # unix seconds
    d: int          # +1 more Up exposure, -1 less
    px: float       # Up-terms price, dollars
    qty: float      # shares
    role: str       # "maker" | "taker"
    timed: bool = True


# ---------------------------------------------------------------- wallet fills
def load_rows(path: Path, wallet: str) -> list[dict]:
    """Every order-tape row of the txs in which ``wallet`` is a maker or the taker (the other
    rows of those txs price its taker fills). Parquet directory or JSONL."""
    rows: list[dict] = []
    if path.is_dir():
        for p in sorted(path.glob("day=*/part-*.parquet")):   # a tx never spans two parts
            part = pq.read_table(p).to_pylist()
            txs = {r["tx"] for r in part if r["maker"].lower() == wallet}
            rows += [r for r in part if r["tx"] in txs]
    else:
        with open(path, "rb") as fh:
            rows = [orjson.loads(line) for line in fh if line.strip()]
        txs = {r["tx"] for r in rows if r["maker"].lower() == wallet}
        rows = [r for r in rows if r["tx"] in txs]
    seen: set[tuple] = set()
    out = []
    for r in rows:
        k = (r["tx"], r["order_id"], r["role"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def wallet_fills(rows: list[dict], wallet: str, wins: list[IntlWindow], tx_ms: dict[tuple[str, Key], int],
                 tmap: TokenMap, t_from: int, t_to: int, allow_unverified: bool = False
                 ) -> tuple[dict[Key, list[Fill]], dict]:
    """Fills of ``wallet`` per window of ``wins``, and what was left out (rows and shares)."""
    tok = {t: (w, up) for w in wins for t, up in ((w.up, True), (w.down, False))}
    by_tx: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_tx[r["tx"]].append(r)
    out: dict[Key, list[Fill]] = defaultdict(list)
    rows_n: Counter = Counter()
    shares: dict[str, float] = defaultdict(float)
    for tx, rs in by_tx.items():
        makers = [r for r in rs if r["role"] == "maker" and r.get("limit")]
        for r in rs:
            if r["maker"].lower() != wallet:
                continue
            if not r.get("limit"):
                rows_n["no_limit"] += 1
                continue
            v = r.get("verified")
            if v is False or (v is None and not allow_unverified):
                rows_n["unverified" if v is False else "unchecked"] += 1
                continue
            if r["role"] == "maker":
                legs = [(float(r["limit"]), filled_shares(r))]
            elif makers and len(makers) == (r.get("n_makers") or len(makers)):
                legs = [(float(m["limit"]) if m["token_id"] == r["token_id"] else 1.0 - float(m["limit"]),
                         filled_shares(m)) for m in makers]
            else:
                legs = [(float(r["limit"]), filled_shares(r))]
                rows_n["taker_price_from_limit"] += 1
            sh = sum(q for _, q in legs)
            hit = tok.get(r["token_id"])
            if hit is None:
                w0 = window_of(tmap.m.get(r["token_id"]) or {})
                if w0 is None:
                    why = "unknown_token_or_other_market" if t_from <= (r.get("block_ts") or 0) < t_to else None
                else:
                    why = "outside_universe" if t_from <= w0["start"] and w0["end"] <= t_to else None
                if why:
                    rows_n[why] += 1
                    shares[why] += sh
                continue
            w, is_up = hit
            ms = tx_ms.get((tx, w.key))
            t = ms / 1000.0 if ms is not None else float(r["block_ts"] - BLOCK_SLACK_S)
            d = (1 if r["side"] == "BUY" else -1) * (1 if is_up else -1)
            for p, q in legs:
                if q > 0 and 0.0 < p < 1.0:
                    out[w.key].append(Fill(t, d, p if is_up else 1.0 - p, q, r["role"], ms is not None))
            rows_n["used"] += 1
    for fs in out.values():
        fs.sort(key=lambda f: f.t)
    return dict(out), {"rows": dict(rows_n), "shares_left_out": {k: round(v, 4) for k, v in shares.items()}}


def clone_fills(res: dict) -> list[Fill]:
    return [Fill(res["start"] + f["t"], 1 if f["side"] == "buy" else -1, f["px"], f["qty"], f["role"])
            for f in res.get("fills") or []]


# ---------------------------------------------------------------- one window
def pnl_usd(fills: list[Fill], outcome: int, costs: Costs) -> float:
    cash = inv = fees = 0.0
    for f in fills:
        cash -= f.d * f.px * f.qty
        inv += f.d * f.qty
        fees += fee_usd(costs.taker_fee_coef if f.role == "taker" else costs.maker_fee_coef, f.px, f.qty,
                        costs.fee_rounding)
    return cash + inv * outcome - fees


def match(clone: list[Fill], wallet: list[Fill]) -> tuple[int, float, float, float]:
    """(one-to-one pairs, sum of min(qty) over them, clone shares in fills with any match, wallet
    shares in fills with any match). ``wallet`` must be sorted by time."""
    wt = [f.t for f in wallet]
    pairs = []
    for i, c in enumerate(clone):
        for j in range(bisect.bisect_left(wt, c.t - TOL_S - EPS), bisect.bisect_right(wt, c.t + TOL_S + EPS)):
            w = wallet[j]
            if w.d == c.d and abs(w.px - c.px) <= TOL_PX + EPS:
                pairs.append((abs(w.t - c.t), abs(w.px - c.px), i, j))
    pairs.sort()
    used_c: set[int] = set()
    used_w: set[int] = set()
    alloc = 0.0
    for _, _, i, j in pairs:
        if i not in used_c and j not in used_w:
            used_c.add(i)
            used_w.add(j)
            alloc += min(clone[i].qty, wallet[j].qty)
    return (len(used_c), alloc, sum(clone[i].qty for i in {p[2] for p in pairs}),
            sum(wallet[j].qty for j in {p[3] for p in pairs}))


def inv_path(fills: list[Fill], start: int, length: int) -> np.ndarray:
    """Net Up inventory at the end of seconds 0..length (fills at or before start + s)."""
    if not fills:
        return np.zeros(length + 1)
    fs = sorted(fills, key=lambda f: f.t)
    cum = np.cumsum([f.d * f.qty for f in fs])
    i = np.searchsorted([f.t for f in fs], start + np.arange(length + 1), side="right")
    return np.where(i > 0, cum[np.maximum(i, 1) - 1], 0.0)


def window_record(clone: list[Fill], wallet: list[Fill], start: int, end: int, outcome: int,
                  costs: Costs, k: float = 1.0) -> dict[str, float]:
    """Additive per-window statistics: every score is a function of their sums over windows."""
    timed = [f for f in wallet if f.timed]
    m, alloc, any_c, any_w = match(clone, timed)
    x, y = inv_path(clone, start, end - start), inv_path(wallet, start, end - start)
    ex, ey = sum(f.d * f.qty for f in clone), sum(f.d * f.qty for f in wallet)
    gx, gy = pnl_usd(clone, outcome, costs), pnl_usd(wallet, outcome, costs)
    tc, tw = bool(clone), bool(wallet)
    return {
        "one": 1.0, "both": float(tc and tw), "w_only": float(tw and not tc), "c_only": float(tc and not tw),
        "n_c": float(len(clone)), "n_w": float(len(timed)), "n_w_all": float(len(wallet)), "m": float(m),
        "q_c": sum(f.qty for f in clone), "q_w": sum(f.qty for f in timed), "alloc": alloc,
        "any_c": any_c, "any_w": any_w, "v_w": sum(f.qty for f in wallet),
        "p_n": float(len(x)), "p_x": float(x.sum()), "p_y": float(y.sum()), "p_xx": float(x @ x),
        "p_yy": float(y @ y), "p_xy": float(x @ y), "p_ad": float(np.abs(x - y).sum()),
        "p_ad_k": float(np.abs(k * x - y).sum()),
        "e_x": ex, "e_y": ey, "e_xx": ex * ex, "e_yy": ey * ey, "e_xy": ex * ey, "e_ad": abs(ex - ey),
        "e_ad_k": abs(k * ex - ey),
        "g_x": gx, "g_y": gy, "g_xx": gx * gx, "g_yy": gy * gy, "g_xy": gx * gy,
    }


# ---------------------------------------------------------------- scores
def _div(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(np.abs(b) > 1e-12, np.asarray(a, dtype=float) / np.where(np.abs(b) > 1e-12, b, 1.0), np.nan)


def _cov(n, sx, sy, sxx, syy, sxy):
    return sxy - _div(sx * sy, n), sxx - _div(sx * sx, n), syy - _div(sy * sy, n)


def _corr(n, sx, sy, sxx, syy, sxy):
    c, vx, vy = _cov(n, sx, sy, sxx, syy, sxy)
    with np.errstate(invalid="ignore"):
        return _div(c, np.sqrt(np.where((vx > 1e-12) & (vy > 1e-12), vx * vy, 0.0)))


def metrics(S: dict, k: float) -> dict[str, np.ndarray]:
    """Scores from column sums (scalars, or one value per bootstrap resample)."""
    n = S["one"]
    gc, gvx, _ = _cov(n, S["g_y"], S["g_x"], S["g_yy"], S["g_xx"], S["g_xy"])   # x = wallet
    slope = _div(gc, np.where(gvx > 1e-12, gvx, 0.0))
    level = _div(S["g_x"], S["g_y"])
    vpw = _div(S["v_w"], n)   # wallet gross shares per comparable window
    return {
        "activity.agreement": _div(S["both"], n),
        "activity.wallet_only": _div(S["w_only"], n),
        "activity.clone_only": _div(S["c_only"], n),
        "fills.precision_count": _div(S["m"], S["n_c"]),
        "fills.recall_count": _div(S["m"], S["n_w"]),
        "fills.precision_shares": _div(S["alloc"], S["q_c"]),
        "fills.recall_shares": _div(S["alloc"], S["q_w"]),
        "fills.precision_volume": _div(S["any_c"], S["q_c"]),
        "fills.recall_volume": _div(S["any_w"], S["q_w"]),
        "path.corr": _corr(S["p_n"], S["p_x"], S["p_y"], S["p_xx"], S["p_yy"], S["p_xy"]),
        "path.mad": _div(S["p_ad"], S["p_n"]),
        "path.mad_scaled": _div(_div(S["p_ad_k"], S["p_n"]), vpw),
        "path.end_corr": _corr(n, S["e_x"], S["e_y"], S["e_xx"], S["e_yy"], S["e_xy"]),
        "path.end_mad": _div(S["e_ad"], n),
        "path.end_mad_scaled": _div(S["e_ad_k"], S["v_w"]),
        "pnl.corr": _corr(n, S["g_x"], S["g_y"], S["g_xx"], S["g_yy"], S["g_xy"]),
        "pnl.slope": slope,
        "pnl.slope_scaled": slope * k,
        "pnl.level_ratio": level,
        "pnl.level_ratio_scaled": level * k,
    }


def _num(x) -> float | None:
    x = float(x)
    return round(x, 6) if np.isfinite(x) else None


def score(records: list[dict], k: float, n_boot: int = N_BOOT, seed: int = 0) -> dict[str, dict]:
    """{score: {"value", "ci95"}} over ``records`` (the comparable windows). ``ci95`` is None
    without a bootstrap, with fewer than 2 windows or when most resamples are undefined."""
    if not records:
        return {}
    cols = list(records[0])
    A = np.array([[r[c] for c in cols] for r in records])
    point = metrics(dict(zip(cols, A.sum(0))), k)
    out = {name: {"value": _num(v), "ci95": None} for name, v in point.items()}
    if n_boot and len(A) >= 2:
        rng = np.random.default_rng(seed)
        chunk = max(1, 2_000_000 // A.size)   # resamples per batch: bounded memory on long runs
        parts = [A[rng.integers(0, len(A), (min(chunk, n_boot - i), len(A)))].sum(1)
                 for i in range(0, n_boot, chunk)]
        S = np.concatenate(parts)
        for name, v in metrics({c: S[:, i] for i, c in enumerate(cols)}, k).items():
            v = v[np.isfinite(v)]
            if len(v) >= 0.5 * n_boot:
                lo, hi = np.quantile(v, (0.025, 0.975))
                out[name]["ci95"] = [_num(lo), _num(hi)]
    return out


def comparable(records: dict[Key, dict]) -> list[dict]:
    return [r for r in records.values() if r["both"] + r["w_only"] + r["c_only"] > 0]


def shuffled(wallet: dict[Key, list[Fill]], windows: list[Key], rng: np.random.Generator
             ) -> dict[Key, list[Fill]] | None:
    """Each window receives the wallet's fills of ANOTHER window of its (asset, horizon), shifted
    by the difference of the starts. None when no group has two windows."""
    groups: dict[tuple[str, str], list[Key]] = defaultdict(list)
    for key in windows:
        groups[key[:2]].append(key)
    out: dict[Key, list[Fill]] = {}
    moved = False
    for keys in groups.values():
        n = len(keys)
        if n < 2:
            out.update({k: wallet.get(k, []) for k in keys})
            continue
        moved = True
        perm = rng.permutation(n)
        while (perm == np.arange(n)).any():
            perm = rng.permutation(n)
        for dst, j in zip(keys, perm):
            src = keys[int(j)]
            out[dst] = [Fill(f.t - src[2] + dst[2], f.d, f.px, f.qty, f.role, f.timed)
                        for f in wallet.get(src, [])]
    return out if moved else None


def decide(n: int, scores: dict, null: dict | None, gates: dict) -> dict:
    """The preregistered verdict. ``gates`` = the ``GATES`` quantities as measured (None = not measured)."""
    checks = []
    for name, stat, th, beat in THRESHOLDS:
        m = scores.get(name) or {"value": None, "ci95": None}
        v, ci = m["value"], m["ci95"]
        s = v if stat == "value" else (ci[0] if ci else None)
        ok = s is not None and (s >= th if stat == "value" else s > th)
        failed = ci is not None and ci[1] is not None and ci[1] < th
        nmax = (null or {}).get(name, {}).get("max")
        note = None
        if ok and beat and (nmax is None or v <= nmax):
            ok, note = False, "not above the shuffled wallet"
        checks.append({"score": name, "statistic": stat, "threshold": th, "value": v, "ci95": ci,
                       "null_max": nmax, "result": "pass" if ok else "fail" if failed else "open", "note": note})
    loc, lag = gates.get("located_share"), gates.get("print_lag_s")
    nwf, use = gates.get("wallet_fills"), gates.get("usable_rows_share")
    data = [f"{n} comparable windows < {GATES['min_comparable_windows']}"
            if n < GATES["min_comparable_windows"] else None,
            f"wallet fills timed by a print {loc} < {GATES['min_located_share']}"
            if loc is not None and loc < GATES["min_located_share"] else None,
            f"|median print lag| {lag} s > {GATES['max_abs_print_lag_s']} s (recorder and venue clocks disagree)"
            if lag is not None and abs(lag) > GATES["max_abs_print_lag_s"] else None,
            f"{nwf} scored wallet fills < {GATES['min_wallet_fills']}"
            if nwf is not None and nwf < GATES["min_wallet_fills"] else None,
            f"usable wallet rows {use} < {GATES['min_usable_rows_share']} (unverified, unchecked or unpriced)"
            if use is not None and use < GATES["min_usable_rows_share"] else None]
    data = [d for d in data if d]
    fails = [c["score"] for c in checks if c["result"] == "fail"]
    opens = [c["score"] for c in checks if c["result"] == "open"]
    if data:
        dec, binding, why = "INCONCLUSIVE", data, "data gate: " + "; ".join(data)
    elif fails:
        dec, binding, why = "FAIL", fails, "below threshold with the whole CI: " + ", ".join(fails)
    elif opens:
        dec, binding, why = "INCONCLUSIVE", opens, "binding: " + ", ".join(opens)
    else:
        dec, binding, why = "PASS", [], "every preregistered score passes"
    return {"decision": dec, "reason": why, "binding": binding, "checks": checks}


def evaluate(clone: dict[Key, list[Fill]], wallet: dict[Key, list[Fill]], windows: dict[Key, tuple[int, int, int]],
             costs: Costs, gates: dict | None = None, n_boot: int = N_BOOT, n_null: int = N_NULL,
             seed: int = 0) -> dict:
    """Scores, null baseline and verdict from normalised fills. ``windows``: eligible window ->
    (start, end, outcome). Pure: the same inputs always give the same document."""
    v_c = sum(f.qty for k in windows for f in clone.get(k, []))
    v_w = sum(f.qty for k in windows for f in wallet.get(k, []))
    k = v_w / v_c if v_c > 0 else float("nan")

    def recs(wal: dict[Key, list[Fill]]) -> dict[Key, dict]:
        return {key: window_record(clone.get(key, []), wal.get(key, []), s, e, o, costs, k)
                for key, (s, e, o) in windows.items()}
    records = recs(wallet)
    comp = comparable(records)
    scores = score(comp, k, n_boot, seed)
    rng = np.random.default_rng(seed + 1)
    draws: dict[str, list[float]] = defaultdict(list)
    n_done = 0
    for _ in range(n_null):
        sh = shuffled(wallet, list(windows), rng)
        if sh is None:
            break
        n_done += 1
        for name, m in score(comparable(recs(sh)), k, 0).items():
            if m["value"] is not None:
                draws[name].append(m["value"])
    null = ({name: {"mean": _num(np.mean(v)), "max": _num(np.max(v)), "n": len(v)} for name, v in draws.items()}
            if n_done else None)
    n_w = sum(r["n_w_all"] for r in comp)
    g = dict(gates or {})
    g["located_share"] = _num(sum(r["n_w"] for r in comp) / n_w) if n_w else None
    g["wallet_fills"] = int(n_w)
    return {"windows": {"eligible": len(windows), "comparable": len(comp),
                        "neither_traded": len(windows) - len(comp)},
            "scale_k": _num(k), "clone_shares": _num(v_c), "wallet_shares": _num(v_w),
            "gates_measured": g, "scores": scores, "null": null, "null_shuffles": n_done,
            "verdict": decide(len(comp), scores, null, g), "records": records}


# ---------------------------------------------------------------- run
def run(root: Path, lab: Path, g: Genome, wallet: str, orders: Path, t_from: int, t_to: int,
        cfg: SimConfig = SimConfig(), n_boot: int = N_BOOT, n_null: int = N_NULL, seed: int = 0,
        allow_unverified: bool = False) -> tuple[dict, list[dict]]:
    """Returns (document, per-window rows)."""
    wallet = wallet.lower()
    tmap = TokenMap(Path(lab) / "tokens.jsonl")
    wins, missing = windows_from_tokens(tmap, g.universe.assets, g.universe.horizons, t_from, t_to)
    rows = load_rows(Path(orders), wallet)
    tx_ms: dict[tuple[str, Key], int] = {}
    results = run_intl(root, g, wins, cfg, lab, {r["tx"] for r in rows}, tx_ms)
    wfills, left = wallet_fills(rows, wallet, wins, tx_ms, tmap, t_from, t_to, allow_unverified)
    by_key = {(r["asset"], r["horizon"], r["start"]): r for r in results}
    elig = {k: r for k, r in by_key.items() if r["eligible"]}
    clone = {k: clone_fills(r) for k, r in elig.items()}
    lags = [r["tape"]["print_lag_s"][1] for r in elig.values() if r["tape"].get("print_lag_s")]
    lag = _num(np.median(lags)) if lags else None
    rn = left["rows"]
    bad = sum(rn.get(k, 0) for k in ("unverified", "unchecked", "no_limit"))
    mine = [r["block_ts"] for r in rows if r["maker"].lower() == wallet and r.get("block_ts")]
    usable = _num(1 - bad / len(mine)) if mine else None
    doc = evaluate(clone, wfills, {k: (r["start"], r["end"], r["outcome"]) for k, r in elig.items()},
                   g.costs, {"print_lag_s": lag, "usable_rows_share": usable}, n_boot, n_null, seed)
    records = doc.pop("records")
    excl = Counter(r["excluded"] for r in results if not r["eligible"])
    excl_w = Counter(r["excluded"] for k, r in by_key.items() if not r["eligible"] and wfills.get(k))
    sh_excl = sum(f.qty for k, fs in wfills.items() if k not in elig for f in fs)
    sh_out = sum(left["shares_left_out"].values())
    both = [r for r in elig.values() if r["outcome_src"] == "payout" and r["brti_outcome"] is not None]
    doc = {
        "params": {"wallet": wallet, "from": t_from, "to": t_to, "tol_px": TOL_PX, "tol_s": TOL_S,
                   "n_boot": n_boot, "n_null": n_null, "seed": seed, "allow_unverified": allow_unverified,
                   "twin": asdict(cfg)},
        "genome": {"id": g.id, "name": g.name, "describe": describe(g)},
        "thresholds": [dict(zip(("score", "statistic", "threshold", "must_beat_shuffle"), t)) for t in THRESHOLDS],
        "gates": GATES, "provisional": True,
        "coverage": {
            "windows_in_range": len(wins) + len(missing), "windows_without_tokens": len(missing),
            "windows_excluded_by_twin": dict(excl), "of_which_wallet_traded": dict(excl_w),
            "wallet": left, "wallet_rows_block_ts": [min(mine), max(mine)] if mine else None,
            "wallet_shares_in_excluded_windows": _num(sh_excl),
            "wallet_volume_share_scored": _num(doc["wallet_shares"] / (doc["wallet_shares"] + sh_excl + sh_out))
            if doc["wallet_shares"] + sh_excl + sh_out > 0 else None},
        "adapter": {"print_lag_s_median": lag,
                    "mirror_dup_prints": sum(r["tape"].get("mirror_dup_prints", 0) for r in elig.values()),
                    "prints_bad_side": sum(r["tape"].get("prints_bad_side", 0) for r in elig.values()),
                    "outcome_src": dict(Counter(r["outcome_src"] for r in elig.values())),
                    "payout_vs_brti": {"windows": len(both),
                                       "agree": sum(r["outcome"] == r["brti_outcome"] for r in both)}},
    } | doc
    out_rows = []
    for k, r in by_key.items():
        row = {"asset": k[0], "horizon": k[1], "start": k[2], "eligible": r["eligible"],
               "excluded": r["excluded"], "outcome": r["outcome"], "outcome_src": r.get("outcome_src"),
               "wallet_fills": len(wfills.get(k, []))}
        if k in records:
            rec = records[k]
            row |= {"clone_fills": int(rec["n_c"]), "matched": int(rec["m"]), "clone_shares": _num(rec["q_c"]),
                    "wallet_shares": _num(rec["v_w"]), "clone_end_inventory": _num(rec["e_x"]),
                    "wallet_end_inventory": _num(rec["e_y"]), "clone_pnl_usd": _num(rec["g_x"]),
                    "wallet_pnl_usd": _num(rec["g_y"])}
        out_rows.append(row)
    return doc, out_rows


def card(doc: dict) -> str:
    v, w, c = doc["verdict"], doc["windows"], doc.get("coverage", {})
    L = [doc["genome"]["describe"] if "genome" in doc else "",
         f"wallet {doc.get('params', {}).get('wallet')}",
         f"VERDICT (PROVISIONAL thresholds): {v['decision']}: {v['reason']}",
         f"windows: {w['comparable']} comparable of {w['eligible']} eligible ({w['neither_traded']} traded by "
         f"neither); excluded {c.get('windows_excluded_by_twin')}, without tokens {c.get('windows_without_tokens')}",
         f"shares: clone {doc['clone_shares']} wallet {doc['wallet_shares']} (k = {doc['scale_k']}); wallet "
         f"volume scored {c.get('wallet_volume_share_scored')}; left out {c.get('wallet')}; order-tape rows of the "
         f"wallet span block_ts {c.get('wallet_rows_block_ts')} (a window the tape does not cover reads as wallet-idle)",
         f"gates measured: {doc['gates_measured']} | adapter: {doc.get('adapter')}", "",
         f"{'score':<26}{'value':>10}  {'CI95':<24}{'shuffle mean':>13}{'shuffle max':>13}"]
    null = doc["null"] or {}
    for name, m in doc["scores"].items():
        nm = null.get(name, {})
        L.append(f"{name:<26}{str(m['value']):>10}  {str(m['ci95']):<24}{str(nm.get('mean')):>13}{str(nm.get('max')):>13}")
    L += ["", "PREREGISTERED THRESHOLDS (PROVISIONAL)",
          f"{'score':<26}{'statistic':<10}{'pass':>8}  {'fail':<18}{'beat shuffle':<14}{'result'}"]
    for ch, t in zip(v["checks"], THRESHOLDS):
        op = ">=" if t[1] == "value" else ">"
        L.append(f"{t[0]:<26}{t[1]:<10}{op + ' ' + str(t[2]):>8}  {'CI high < ' + str(t[2]):<18}"
                 f"{('yes' if t[3] else 'no'):<14}{ch['result']}" + (f" ({ch['note']})" if ch["note"] else ""))
    L.append(f"data gates (else INCONCLUSIVE): {GATES}")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-fidelity")
    ap.add_argument("--data", type=Path, required=True, help="tape root (clob_<asset>_<hz>, ref_*)")
    ap.add_argument("--lab", type=Path, required=True, help="lab dir holding tokens.jsonl (read only)")
    ap.add_argument("--genome", type=Path, required=True)
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--orders", type=Path, required=True,
                    help="doppel-ordertape output: parquet directory or JSONL (maker and taker rows)")
    ap.add_argument("--from", dest="t_from", type=_ts, required=True, help="unix s or ISO UTC")
    ap.add_argument("--to", dest="t_to", type=_ts, required=True)
    ap.add_argument("--out", type=Path, required=True, help="directory")
    ap.add_argument("--latency-ms", type=float, default=SimConfig().latency_ms)
    ap.add_argument("--brti-delay-s", type=int, default=SimConfig().brti_delay_s)
    ap.add_argument("--depth-levels", type=int, default=SimConfig().depth_levels)
    ap.add_argument("--boot", type=int, default=N_BOOT)
    ap.add_argument("--null-shuffles", type=int, default=N_NULL)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--allow-unverified", action="store_true",
                    help="also use rows whose order hash the fill store could not check (verified null)")
    a = ap.parse_args(argv)
    g = from_dict(orjson.loads(a.genome.read_bytes()))
    errs = validate(g)
    if errs:
        print("genome invalid: " + "; ".join(errs), file=sys.stderr)
        return 2
    cfg = SimConfig(latency_ms=a.latency_ms, brti_delay_s=a.brti_delay_s, depth_levels=a.depth_levels)
    doc, rows = run(a.data, a.lab, g, a.wallet, a.orders, a.t_from, a.t_to, cfg, a.boot, a.null_shuffles,
                    a.seed, a.allow_unverified)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "fidelity.json").write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
    with open(a.out / "windows.jsonl", "wb") as f:
        for r in rows:
            f.write(orjson.dumps(r) + b"\n")
    text = card(doc)
    (a.out / "fidelity.txt").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
