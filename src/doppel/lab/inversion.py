"""Latent fair-value inversion: a wallet's decisions -> bounds on its hidden P(Up), fitted as
p_w = sigma(logit(fair_v1) + g(x)) with g linear (docs/INVERSION.md, STATE-AND-PLAN 9.2 item 1).

    doppel-invert --decisions decision_table.jsonl --venue pmus --out result.json

Every decision is an inequality on p_w, net of the wallet's own venue taker fee per contract:
  took Up at ask a     p_w >= a + fee(a)        (an "Up side" inequality, satisfied)
  took Down at bid b   p_w <= b - fee(b)        ("Down side", satisfied)
  passed               p_w <  a + fee(a) and p_w > b - fee(b)   (both sides violated)
  maker post filled    p_w >= b (bid) / <= a (ask): CENSORED. Unfilled quotes and cancels are
                       unobserved, so posts have no negatives; they are scored for consistency
                       only, never fitted. Makers go to the strategy simulator.
Each executable side of a sampled moment is one row with y = 1 when that side was taken. With
logistic decision noise of scale T, P(y = 1) = sigma(margin / T): a logistic regression with
design [u, 1, s*x, s] where u = s*(logit fair - logit threshold), s = +1 Up side, -1 Down side.
gamma_u = 1/T (the temperature is fitted, not tuned), beta = gamma_x / gamma_u. The plain
intercept absorbs the pass-sampling ratio; the s-signed one is a constant belief offset.

INPUT: rows in rulefit's ``decision_table.jsonl`` STATE_COLS (``bb_c``, ``ba_c``, ``depth_bid``,
``depth_ask``, ``fair_c``, ``tte_s``, ``inv``, ``window``, Up-exposure cents) plus, for taker
decisions, ``action`` = ``take`` (with ``up_side`` bid = bought Up at ``px_c``, ask = sold Up at
``px_c``, and ``size``) or ``none`` (a moment the wallet did not act). Optional feature columns:
``lead``, ``basis``, ``drift``; ``imbalance`` is derived from the depths. Rulefit's maker rows
(``kind`` = ``order``) and Layer 3 ``wallet-dissection/1`` orders (``context``) are read as
censored posts. No existing output writes taker or pass moments with depth and fair-v1 today
(rulefit ``takes`` carry no state; opportunity-panel cells carry ``mid_c`` only), so on real
wallets this reports NOT_IDENTIFIED with the missing fields named.

Hard negatives: a pass is a ``none`` moment in a window where the wallet took at least once,
with an executable side (depth > 0), fair known, and not at the wallet's observed max position
on that side (fit windows only; disable with ``--no-position-filter``). Passes are matched to
takes on window and 60 s time-to-expiry bucket, at most ``ratio`` per take, seeded. Because of
that matching, ``tte_s`` is never claimed as a feature.

Windows are split chronologically (70 % fit, 30 % held out); CIs bootstrap whole windows.
IDENTIFIED needs >= MIN_TAKES takes and >= MIN_PASSES pass rows in the fit, a margin slope
1/T whose CI is above 0, and held-out margin accuracy whose CI beats both baselines (pure
fair_v1 and market mid, both scored against the same fee-adjusted thresholds).
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import orjson

FEATURES = ("lead", "basis", "drift", "imbalance", "spread_c", "tte_s")
MATCHED = ("tte_s",)       # negatives are matched on it: its coefficient is not interpretable
FIT_SHARE = 0.7
TTE_BUCKET_S = 60
RATIO = 3
MIN_TAKES = 30
MIN_PASSES = 30
N_BOOT = 200
SEED = 20261127
L2 = 1.0                   # on standardized feature / bias coefficients (not on 1/T, not the intercept)
EPS_P = 1e-4
CONFOUNDS = (
    "a pass can be a capital, position or attention limit rather than disagreement; the max-position "
    "filter removes only limits visible in the wallet's own inventory path",
    "inversion gives a band on p_w per decision, not a point; the point fit is one member of it",
    "maker cancels and unfilled quotes are unobserved: posts are censored (no negatives) and go to the simulator",
    "the constant belief offset and the pass-sampling intercept separate only through a wallet that "
    "takes on both sides; a one-sided wallet confounds them",
    "features not in the input (a private feed) show up as decision noise (a larger T), never as a coefficient",
)


def fee_fn(venue: str, coef: float | None):
    """Per-contract taker fee (dollars) at price p for an order of q contracts, from the repo's fee
    models: PM US cent half-even (``xvenue.pmus_fee``), Kalshi ceil-to-cent (``xvenue.kalshi_fee``).
    ``intl`` has no repo fee model: its coefficient must be given (``twin.engine.fee_usd`` rounding)."""
    from ..twin.engine import fee_usd
    from .xvenue import KALSHI_COEF, PMUS_COEF, kalshi_fee, pmus_fee
    if venue == "pmus":
        c = PMUS_COEF if coef is None else coef
        return (lambda p, q: pmus_fee(p, q, coef=c) / q), c
    if venue == "kalshi":
        if coef not in (None, KALSHI_COEF):
            raise ValueError("kalshi_fee has a fixed coefficient")
        return (lambda p, q: kalshi_fee(p, q) / q), KALSHI_COEF
    if venue == "intl":
        if coef is None:
            raise ValueError("intl: no fee model in the repo, pass --fee-coef")
        return (lambda p, q: fee_usd(coef, p, q) / q), coef
    raise ValueError(f"unknown venue {venue}")


def _num(x):
    return None if x is None or not math.isfinite(float(x)) else float(x)


def moment(r: dict) -> dict:
    """One input row -> {kind, window, order, fair, bid, ask, depths, inv, size, px, x}."""
    if "context" in r:  # Layer 3 wallet-dissection/1: signed-token terms, no fair/tte/inventory
        st = ((r.get("context") or {}).get("signed") or {}).get("state") or {}
        return {"kind": "post_up" if r["side"] == "BUY" else "post_down", "window": r.get("market"),
                "order": 0, "fair": None, "px": float(r["price"]), "bid": _num(st.get("best_bid")),
                "ask": _num(st.get("best_ask")), "terms": "signed_token", "x": {}}
    action = r.get("action")
    if action not in ("take", "none"):
        if r.get("up_side") not in ("bid", "ask"):
            raise ValueError("row is neither a taker decision nor a maker post")
        kind = "post_up" if r["up_side"] == "bid" else "post_down"
    elif action == "take":
        kind = "take_up" if r["up_side"] == "bid" else "take_down"
    else:
        kind = "none"
    c = lambda k: None if _num(r.get(k)) is None else float(r[k]) / 100  # noqa: E731
    db, da = _num(r.get("depth_bid")), _num(r.get("depth_ask"))
    x = {k: _num(r[k]) for k in FEATURES if k in r}
    if db is not None and da is not None and db + da > 0:
        x["imbalance"] = (db - da) / (db + da)
    return {"kind": kind, "window": r.get("window"), "order": r.get("window_start_ns") or 0,
            "tte": _num(r.get("tte_s")), "fair": c("fair_c"), "bid": c("bb_c"), "ask": c("ba_c"),
            "mid": c("mid_c"), "depth_bid": db, "depth_ask": da, "inv": _num(r.get("inv")),
            "size": _num(r.get("size")), "px": c("px_c"), "terms": "up", "x": x}


def logit(p):
    p = np.clip(np.asarray(p, dtype=float), EPS_P, 1 - EPS_P)
    return np.log(p / (1 - p))


def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.asarray(z, dtype=float)))


def sample_passes(ms: list[dict], ratio: int, seed: int) -> tuple[list[dict], dict]:
    """Takes plus at most ``ratio`` matched passes per take (same window, same tte bucket)."""
    rng = np.random.default_rng(seed)
    takes = [m for m in ms if m["kind"] in ("take_up", "take_down")]
    active = {m["window"] for m in takes}
    pool: dict[tuple, list[dict]] = {}
    for m in ms:
        if m["kind"] == "none" and m["window"] in active and m["tte"] is not None:
            pool.setdefault((m["window"], int(m["tte"] // TTE_BUCKET_S)), []).append(m)
    picked, unmatched = [], 0
    for key in sorted(pool, key=str):
        rng.shuffle(pool[key])  # type: ignore[arg-type]  (in place, seeded)
    for t in takes:
        cell = pool.get((t["window"], int(t["tte"] // TTE_BUCKET_S)), []) if t["tte"] is not None else []
        got = [cell.pop() for _ in range(min(ratio, len(cell)))]
        unmatched += not got
        picked += got
    return takes + picked, {"takes": len(takes), "passes_sampled": len(picked), "takes_without_matched_pass": unmatched,
                            "windows_active": len(active)}


def side_rows(ms, fee, ref_q, limits, use_filter):
    """Executable-side rows: (moment index, s, threshold, y). Drops y=0 rows at the max position."""
    out, dropped = [], {"position_filter": 0, "no_fair": 0, "not_executable": 0}
    for i, m in enumerate(ms):
        if m["fair"] is None:
            dropped["no_fair"] += 1
            continue
        q = m["size"] or ref_q
        n0 = len(out)
        for s, px_key, depth_key, taken in ((1, "ask", "depth_ask", "take_up"), (-1, "bid", "depth_bid", "take_down")):
            px = m["px"] if m["kind"] == taken else m[px_key]
            if px is None or not 0 < px < 1 or (m["kind"] != taken and not (m[depth_key] or 0) > 0):
                continue
            thr = px + s * fee(px, q)
            if not 0 < thr < 1:
                continue
            y = int(m["kind"] == taken)
            if not y and use_filter and m["inv"] is not None and s * m["inv"] + q > limits[s] + 1e-9:
                dropped["position_filter"] += 1
                continue
            out.append((i, s, thr, y))
        dropped["not_executable"] += len(out) == n0
    return out, dropped


def design(ms, rows, feats, mu, sd):
    i = np.array([r[0] for r in rows], dtype=int)
    s = np.array([r[1] for r in rows], dtype=float)
    thr = np.array([r[2] for r in rows])
    y = np.array([r[3] for r in rows], dtype=float)
    lf = logit([ms[k]["fair"] for k in i])
    xs = np.array([[ms[k]["x"][f] for f in feats] for k in i], dtype=float).reshape(len(i), len(feats))
    xs = (xs - mu) / sd
    u = s * (lf - logit(thr))
    X = np.column_stack([u, np.ones(len(i)), s[:, None] * xs, s])
    return X, y, i, s, thr


def fit(X, y, w):
    """Weighted L2 logistic regression by Newton. Columns: [u, intercept, features..., bias]."""
    k = X.shape[1]
    pen = np.full(k, L2)
    pen[0], pen[1] = 1e-6, 0.0
    g = np.zeros(k)
    g[0] = 1.0
    for _ in range(100):
        p = sigmoid(X @ g)
        grad = X.T @ (w * (p - y)) + pen * g
        H = (X * (w * p * (1 - p))[:, None]).T @ X + np.diag(pen + 1e-9)
        step = np.linalg.solve(H, grad)
        g -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return g


def _ci(a):
    a = np.asarray(a)[np.isfinite(a)]
    return [float(np.quantile(a, 0.025)), float(np.quantile(a, 0.975))] if len(a) else [None, None]


def invert(rows: list[dict], *, venue: str, fee_coef: float | None = None, ratio: int = RATIO, seed: int = SEED,
           n_boot: int = N_BOOT, position_filter: bool = True, features: tuple[str, ...] | None = None) -> dict:
    fee, coef = fee_fn(venue, fee_coef)
    allm = [moment(r) for r in rows]
    posts = [m for m in allm if m["kind"].startswith("post")]
    ms_all = [m for m in allm if not m["kind"].startswith("post")]
    present = [f for f in (features or FEATURES)
               if ms_all and all(m["x"].get(f) is not None for m in ms_all)]
    missing = [f for f in ("action", "fair_c", "ba_c", "bb_c", "depth_ask", "depth_bid", "tte_s", "inv")
               if not any(f in r for r in rows)]
    out: dict = {"schema": "inversion/1", "venue": venue, "fee_coef": coef, "features": present,
                 "never_claimed": list(MATCHED), "confounds": list(CONFOUNDS), "missing_fields": missing,
                 "inputs": {"rows": len(rows), "maker_posts_censored": len(posts), "moments": len(ms_all)}}
    reasons = []
    sample, info = sample_passes(ms_all, ratio, seed)
    out["sampling"] = info | {"ratio": ratio, "seed": seed, "tte_bucket_s": TTE_BUCKET_S}
    wins = sorted({m["window"] for m in sample}, key=lambda w: (min(m["order"] for m in sample if m["window"] == w), str(w)))
    n_fit = int(round(FIT_SHARE * len(wins)))
    fit_w = set(wins[:n_fit])
    widx = {w: j for j, w in enumerate(wins)}
    sizes = [m["size"] for m in sample if m["kind"].startswith("take") and m["size"]]
    ref_q = float(np.median(sizes)) if sizes else 1.0
    # observed max position per side on fit windows, from the inventory path (post-decision)
    post_inv = [(m["inv"] or 0.0) + {"take_up": m["size"] or ref_q, "take_down": -(m["size"] or ref_q)}.get(m["kind"], 0.0)
                for m in ms_all if m["window"] in fit_w and m["inv"] is not None]
    limits = {1: max(post_inv, default=math.inf), -1: max((-v for v in post_inv), default=math.inf)}
    sr, dropped = side_rows(sample, fee, ref_q, limits, position_filter)
    out["sampling"] |= {"reference_size_for_passes": ref_q, "position_filter": position_filter,
                        "observed_max_position": {"up": limits[1], "down": limits[-1]}, "dropped": dropped}
    fit_rows = [r for r in sr if sample[r[0]]["window"] in fit_w]
    test_rows = [r for r in sr if sample[r[0]]["window"] not in fit_w]
    n_takes = sum(r[3] for r in fit_rows)
    n_pass = sum(1 - r[3] for r in fit_rows if sample[r[0]]["kind"] == "none")
    out["counts"] = {"fit_windows": n_fit, "test_windows": len(wins) - n_fit, "fit_rows": len(fit_rows),
                     "test_rows": len(test_rows), "fit_takes": n_takes, "fit_pass_rows": n_pass}
    if n_takes < MIN_TAKES:
        reasons.append(f"fewer than {MIN_TAKES} taker decisions with state in fit windows")
    if n_pass < MIN_PASSES:
        reasons.append(f"fewer than {MIN_PASSES} certified pass rows (executable depth, fair known) in fit windows")
    if reasons or not test_rows:
        out |= {"verdict": "NOT_IDENTIFIED", "reasons": reasons or ["no held-out rows"], "claimed_features": []}
        out["maker_posts"] = {"n": len(posts), "censored": True, "fitted": False}
        return out
    fm = [sample[r[0]] for r in fit_rows]
    F = np.array([[m["x"][f] for f in present] for m in fm], dtype=float).reshape(len(fm), len(present))
    mu, sd = F.mean(0), F.std(0)
    sd[sd == 0] = 1.0
    X, y, mi, s, thr = design(sample, fit_rows, present, mu, sd)
    rw = np.array([widx[sample[k]["window"]] for k in mi])
    g = fit(X, y, np.ones(len(y)))
    rng = np.random.default_rng(seed)
    fit_ids = np.array([widx[w] for w in wins[:n_fit]])
    boots = []
    for _ in range(n_boot):
        cnt = np.bincount(rng.choice(fit_ids, len(fit_ids)), minlength=len(wins))
        boots.append(fit(X, y, cnt[rw].astype(float)))
    G = np.array(boots)

    def raw(gv):  # standardized -> raw units: beta per feature, belief bias, temperature
        b = gv[..., 2:2 + len(present)] / gv[..., :1] / sd
        b0 = gv[..., -1] / gv[..., 0] - (b * mu).sum(-1)
        return b, b0, 1 / gv[..., 0]
    b, b0, T = raw(g)
    B, B0, TT = raw(G)
    coefs = {f: {"value": float(b[j]), "ci95": _ci(B[:, j])} for j, f in enumerate(present)}
    out["coefficients"] = coefs
    out["belief_bias_logit"] = {"value": float(b0), "ci95": _ci(B0)}
    out["temperature_logit"] = {"value": float(T), "ci95": _ci(TT)}
    out["margin_slope"] = {"value": float(g[0]), "ci95": _ci(G[:, 0])}
    out["sampling_intercept"] = float(g[1])
    # held-out margin accuracy: belief only (no sampling intercept), same thresholds for all
    Xt, yt, ti, st, tthr = design(sample, test_rows, present, mu, sd)
    tm = [sample[k] for k in ti]
    belief = Xt[:, 0] + Xt[:, 2:] @ g[2:] / g[0]  # columns 2: already carry the side sign s
    mid = np.array([m["mid"] if m["mid"] is not None else
                    (0.5 * (m["bid"] + m["ask"]) if m["bid"] is not None and m["ask"] is not None else np.nan) for m in tm])
    hit = {"model": (belief > 0) == (yt == 1), "fair_v1": (Xt[:, 0] > 0) == (yt == 1),
           "mid": (st * (logit(mid) - logit(tthr)) > 0) == (yt == 1)}
    hit["mid"] &= np.isfinite(mid)
    tw = np.array([widx[m["window"]] for m in tm])
    def balanced(v, wt):  # mean of the satisfied-share on taken and on not-taken rows:
        t, n = yt == 1, yt == 0  # invariant to the analyst's pass-sampling ratio
        return 0.5 * ((v * wt)[t].sum() / max(wt[t].sum(), 1e-12) + (v * wt)[n].sum() / max(wt[n].sum(), 1e-12))
    one = np.ones(len(yt))
    acc = {k: float(balanced(v, one)) for k, v in hit.items()}
    acc |= {f"{k}_plain": float(v.mean()) for k, v in hit.items()}
    acc |= {f"{k}_on_takes": float(v[yt == 1].mean()) if (yt == 1).any() else None for k, v in hit.items()}
    acc |= {f"{k}_on_non_takes": float(v[yt == 0].mean()) if (yt == 0).any() else None for k, v in hit.items()}
    test_ids = np.unique(tw)
    diffs = {"vs_fair_v1": [], "vs_mid": []}
    for _ in range(n_boot):
        cnt = np.bincount(rng.choice(test_ids, len(test_ids)), minlength=len(wins))[tw].astype(float)
        a = {k: balanced(v, cnt) for k, v in hit.items()}
        diffs["vs_fair_v1"].append(a["model"] - a["fair_v1"])
        diffs["vs_mid"].append(a["model"] - a["mid"])
    diff_ci = {k: _ci(v) for k, v in diffs.items()}
    out["heldout_accuracy"] = acc | {"metric": "balanced (taken vs not-taken rows)", "n": len(yt), "diff_ci95": diff_ci}
    lo_slope = out["margin_slope"]["ci95"][0]
    if lo_slope is None or lo_slope <= 0:
        reasons.append("margin slope 1/T CI reaches 0: decisions do not respond to price vs belief")
    for k, name in (("vs_fair_v1", "pure fair_v1"), ("vs_mid", "market mid")):
        lo = diff_ci[k][0]
        if lo is None or lo <= 0:
            reasons.append(f"held-out margin accuracy does not beat {name} (CI lower bound <= 0)")
    ok = not reasons
    out["verdict"] = "IDENTIFIED" if ok else "NOT_IDENTIFIED"
    out["reasons"] = reasons
    out["claimed_features"] = [f for f in present if ok and f not in MATCHED
                               and (coefs[f]["ci95"][0] > 0 or coefs[f]["ci95"][1] < 0)]
    # per-decision band of p_w from the bootstrap fits (sampled moments and maker posts)
    def band(m):
        if m["fair"] is None or any(m["x"].get(f) is None for f in present):
            return None
        xv = np.array([m["x"][f] for f in present], dtype=float)
        z = logit(m["fair"]) + (B @ xv if len(present) else 0) + B0
        return [float(sigmoid(np.quantile(z, 0.025))), float(sigmoid(np.quantile(z, 0.975)))]
    dec = []
    for i, m in enumerate(sample):
        mine = [r for r in sr if r[0] == i]
        lo = max([r[2] for r in mine if (r[1] == 1) == bool(r[3])] + [0.0])
        hi = min([r[2] for r in mine if (r[1] == -1) == bool(r[3])] + [1.0])
        dec.append({"window": m["window"], "tte_s": m["tte"], "kind": m["kind"], "role": "fit" if m["window"] in fit_w else "test",
                    "implied": [lo, hi], "fair": m["fair"], "band95": band(m)})
    cons = []
    for m in posts:
        bd = band(m) if m["terms"] == "up" else None
        if bd is not None:
            cons.append(bd[1] >= m["px"] if m["kind"] == "post_up" else bd[0] <= m["px"])
        dec.append({"window": m["window"], "kind": m["kind"], "censored": True, "implied":
                    [m["px"], 1.0] if m["kind"] == "post_up" else [0.0, m["px"]], "band95": bd})
    out["maker_posts"] = {"n": len(posts), "censored": True, "fitted": False, "scored": len(cons),
                          "band_consistent_share": float(np.mean(cons)) if cons else None}
    out["decisions"] = dec
    return out


# ---------------------------------------------------------------- answer key (scripted wallets)
def scripted_rows(kind: str, seed: int, windows: int = 60, beta: float = 1.0, noise: float = 0.25,
                  cap: float = 20.0, size: float = 10.0, venue: str = "pmus") -> list[dict]:
    """Synthetic 15m windows, one moment per 10 s, in the decision-table schema. True belief
    p_w = sigma(logit(fair) + beta * lead) with logistic decision noise of scale ``noise``.
    kinds: taker (beta), fair (beta = 0), random (takes at random), capped (beta, net position
    capped at +/- ``cap`` contracts per window). ``imbalance`` (via depths) is an uninformative decoy."""
    fee, _ = fee_fn(venue, None)
    rng = np.random.default_rng(seed)
    b = 0.0 if kind == "fair" else beta
    rows = []
    for w in range(windows):
        lf = rng.normal(0, 0.6)
        inv = 0.0
        for tte in range(890, 0, -10):
            lf += rng.normal(0, 0.12)
            lead = float(rng.normal())
            m = float(sigmoid(lf + rng.normal(0, 0.3)))
            spread = int(rng.integers(1, 3))
            ba = min(99, max(2, math.ceil(100 * m + spread / 2)))
            bb = ba - spread
            da, db = int(rng.integers(0, 200)), int(rng.integers(0, 200))
            r = {"window": f"w{w:03d}", "window_start_ns": w * 900 * 10**9, "tte_s": float(tte),
                 "fair_c": round(100 * float(sigmoid(lf)), 4), "bb_c": float(bb), "ba_c": float(ba),
                 "mid_c": (bb + ba) / 2, "spread_c": float(spread), "depth_bid": db, "depth_ask": da,
                 "inv": inv, "lead": lead, "action": "none"}
            z = lf + b * lead + rng.logistic(0, noise)
            up_ok = da > 0 and (kind != "capped" or inv + size <= cap)
            dn_ok = db > 0 and (kind != "capped" or inv - size >= -cap)
            if kind == "random":
                u = rng.random()
                side = "bid" if u < 0.06 and da > 0 else "ask" if 0.06 <= u < 0.12 and db > 0 else None
            else:
                side = ("bid" if up_ok and z >= logit(ba / 100 + fee(ba / 100, size)) else
                        "ask" if dn_ok and z <= logit(bb / 100 - fee(bb / 100, size)) else None)
            if side:
                r |= {"action": "take", "up_side": side, "px_c": float(ba if side == "bid" else bb), "size": size}
                inv += size if side == "bid" else -size
            rows.append(r)
    return rows


def answer_key(seeds=(1, 2, 3), n_boot: int = 100) -> dict:
    """The acceptance numbers: one function for the test and the report."""
    out = {}
    for seed in seeds:
        res = {}
        for kind in ("taker", "random", "fair", "capped"):
            rows = scripted_rows(kind, seed)
            runs = {"filtered": invert(rows, venue="pmus", seed=seed, n_boot=n_boot)}
            if kind == "capped":
                runs["unfiltered"] = invert(rows, venue="pmus", seed=seed, n_boot=n_boot, position_filter=False)
            res[kind] = {k: {"verdict": r["verdict"], "claimed": r["claimed_features"],
                             "lead": r.get("coefficients", {}).get("lead"),
                             "imbalance": r.get("coefficients", {}).get("imbalance"),
                             "temperature": r.get("temperature_logit"), "accuracy": r.get("heldout_accuracy", {}).get("model"),
                             "acc_fair": r.get("heldout_accuracy", {}).get("fair_v1"),
                             "acc_mid": r.get("heldout_accuracy", {}).get("mid"),
                             "dropped": r["sampling"].get("dropped"), "reasons": r["reasons"]}
                         for k, r in runs.items()}
        out[seed] = res
    return out


def main(argv=None) -> int:
    cli = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    cli.add_argument("--decisions", type=Path, help="decision_table.jsonl / Layer 3 orders.jsonl")
    cli.add_argument("--venue", choices=("pmus", "kalshi", "intl"), help="the wallet's own venue (fee model)")
    cli.add_argument("--fee-coef", type=float)
    cli.add_argument("--ratio", type=int, default=RATIO)
    cli.add_argument("--seed", type=int, default=SEED)
    cli.add_argument("--boot", type=int, default=N_BOOT)
    cli.add_argument("--no-position-filter", action="store_true")
    cli.add_argument("--answer-key", action="store_true", help="run the scripted-wallet acceptance instead")
    cli.add_argument("--out", type=Path, required=True)
    a = cli.parse_args(argv)
    if a.out.exists():
        raise FileExistsError(a.out)
    if a.answer_key:
        res: dict = answer_key(n_boot=a.boot)
    else:
        if a.decisions is None or a.venue is None:
            cli.error("--decisions and --venue are required")
        rows = [orjson.loads(x) for x in a.decisions.read_bytes().splitlines() if x.strip()]
        res = invert(rows, venue=a.venue, fee_coef=a.fee_coef, ratio=a.ratio, seed=a.seed, n_boot=a.boot,
                     position_filter=not a.no_position_filter)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS) + b"\n")
    print(orjson.dumps({k: res[k] for k in ("verdict", "reasons", "claimed_features")} if "verdict" in res else
                       {s: {k: v["filtered"]["verdict"] for k, v in r.items()} for s, r in res.items()},
                       option=orjson.OPT_NON_STR_KEYS).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
