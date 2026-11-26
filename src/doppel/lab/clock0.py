"""C3 "clock 0": can an ANONYMOUS intl CLOB print predict its taker's informedness before RTDS names
the wallet (~2.2 s later)?

Unit: one taker transaction (all ``last_trade_price`` prints sharing a tx hash in one asset).
Labels come from RTDS, joined by tx hash, and are NEVER features:
  * ``informed``: the taker wallet's point-in-time score as of ``as_of`` (``flow.informedness``,
    windows ended before as_of only) has excess >= flow.INFORMED_EXCESS_C.
  * ``weight``: flow.weight of that score (continuous, -1..1).
  * ``m_c``: the tx's own settlement markout, cents/share = 100 * dir * (payout_token - price).
Only transactions RECEIVED after ``as_of`` are used, so scores never saw them.

Features (all known at the first print's receive time, from public market data only):
  log_usd, n_prints, price, px_span_c (levels swept), dist50 (|p - 0.5|), tte_s, hz_5m, coin,
  buy_up (direction on Up), burst_1s / burst_5s (prior prints in the market), usd_5s, same_dir_5s
  (share of the last 5 s of market USD in the same Up direction), dmove_5s_c (price change vs the
  market's print 5 s earlier).
The window/coin of an asset id is public metadata (Gamma); here it is read off the joined RTDS row.

Evaluation: time split (first ``train_frac`` of txs by receive time train, rest test).
HistGradientBoosting vs a size-only baseline (the H3 "anonymous flow" arm's information).
This is a MEASUREMENT, not a strategy verdict.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

import numpy as np
import orjson

from ..tape.segment import read_segment, segment_path
from .dossier import STREAMS as CLOB_STREAMS
from .flow import (HOUR, LEN, NS, RTDS_STREAMS, informedness, is_informed, parse_rtds, tape_hours,
                   weight, window_outcomes)
from .ledger import TokenMap

FEATURES = ("log_usd", "n_prints", "price", "px_span_c", "dist50", "tte_s", "hz_5m", "coin",
            "buy_up", "burst_1s", "burst_5s", "usd_5s", "same_dir_5s", "dmove_5s_c")
COINS = {"btc": 0, "eth": 1, "sol": 2, "xrp": 3}


def _iso(s: str) -> int:
    return int(time.mktime(time.strptime(s.rstrip("Z"), "%Y-%m-%dT%H:%M:%S")) - time.timezone)


def clob_prints(root: Path, hours: list[int]):
    """(tw_ns, tx, asset, market, price, size, side) for every last_trade_price print."""
    for h in hours:
        for s in CLOB_STREAMS:
            f = segment_path(root, s, h * NS)
            if not f.exists():
                continue
            for rec in read_segment(f, verify=True):
                raw = rec.get("raw") or ""
                if "last_trade_price" not in raw:
                    continue
                try:
                    obj = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    continue
                for m in obj if isinstance(obj, list) else [obj]:
                    if not isinstance(m, dict) or m.get("event_type") != "last_trade_price":
                        continue
                    tx = str(m.get("transaction_hash") or "").lower()
                    if not tx:
                        continue
                    try:
                        yield (rec["tw"], tx, str(m["asset_id"]), str(m.get("market") or ""),
                               float(m["price"]), float(m["size"]), str(m.get("side") or "").upper())
                    except (KeyError, TypeError, ValueError):
                        continue


def rtds_takers(root: Path, hours: list[int]) -> dict[tuple[str, str], dict]:
    """(tx, token) -> taker row info from RTDS (fee > 0)."""
    out: dict[tuple[str, str], dict] = {}
    for h in hours:
        for s in RTDS_STREAMS:
            f = segment_path(root, s, h * NS)
            if not f.exists():
                continue
            for rec in read_segment(f, verify=True):
                r = parse_rtds(rec.get("raw") or "", rec["tw"])
                if r is None or not r[1].taker:
                    continue
                key, fl = r
                k = (key[0], fl.token)
                if k not in out or fl.tw < out[k]["tw"]:
                    out[k] = {"tw": fl.tw, "wallet": fl.wallet, "slug": fl.slug, "coin": fl.coin,
                              "hz": fl.hz, "start": fl.start, "idx": fl.idx, "side": fl.side}
    return out


def build(root: Path, lab: Path, as_of: int, until: int, fetch: bool = False) -> tuple[list[dict], dict]:
    hours = [h for h in tape_hours(root, CLOB_STREAMS) if as_of - HOUR < h <= until]
    if fetch:   # resolve outcomes from Gamma; scores still use only windows ended before as_of
        from .flow import _tmap
        tmap = _tmap(Path(lab), root, until, True)
    else:
        tmap = TokenMap(Path(lab) / "tokens.jsonl")
    outcomes = window_outcomes(tmap)
    t0 = time.time()
    scores = informedness(root, tmap, as_of)
    t_scores = time.time() - t0
    takers = rtds_takers(root, [h for h in tape_hours(root, RTDS_STREAMS) if as_of - HOUR < h <= until + HOUR])
    # group prints into (tx, asset) units, ordered by first receive
    units: dict[tuple[str, str], dict] = {}
    market_hist: dict[str, deque] = defaultdict(deque)   # market -> (tw, usd, up_dir, price)
    rows: list[dict] = []
    counts = defaultdict(int)
    prints = sorted(p for p in clob_prints(root, hours) if as_of * NS < p[0] <= until * NS)
    for tw, tx, asset, mk, px, sz, side in prints:
        counts["prints"] += 1
        u = units.get((tx, asset))
        if u is None:
            units[(tx, asset)] = u = {"tw": tw, "tx": tx, "asset": asset, "mk": mk, "px": [px],
                                      "usd": px * sz, "size": sz, "side": side, "n": 1}
            u["_hist"] = list(market_hist[mk])   # market state BEFORE this tx's first print
        elif tw - u["tw"] <= 500_000_000:      # prints of one tx arrive together
            u["px"].append(px); u["usd"] += px * sz; u["size"] += sz; u["n"] += 1
        info = takers.get((tx, asset))
        updir = None
        if info is not None:
            updir = (1 if side == "BUY" else -1) * (1 if info["idx"] == 0 else -1)
        dq = market_hist[mk]
        dq.append((tw, px * sz, updir, px))
        while dq and dq[0][0] < tw - 5 * NS:
            dq.popleft()
    for u in units.values():
        info = takers.get((u["tx"], u["asset"]))
        if info is None:
            counts["no_rtds_taker"] += 1
            continue
        rec = scores.get(info["wallet"])
        tws = u["tw"] / NS
        end = info["start"] + LEN[info["hz"]]
        up = 1 if info["idx"] == 0 else -1
        d = 1 if u["side"] == "BUY" else -1
        hist = u.pop("_hist")
        h1 = [x for x in hist if x[0] >= u["tw"] - NS]
        usd5 = sum(x[1] for x in hist)
        same = sum(x[1] for x in hist if x[2] is not None and x[2] == d * up)
        old = [x for x in hist if x[0] <= u["tw"] - 4 * NS]
        vwap = sum(u["px"]) / len(u["px"])
        pay_up = outcomes.get(info["slug"])
        m_c = None
        if pay_up is not None:
            pay_tok = pay_up if info["idx"] == 0 else 1.0 - pay_up
            m_c = 100.0 * d * (pay_tok - vwap)
        rows.append({
            "tw": u["tw"], "wallet": info["wallet"], "known": rec is not None,
            "informed": bool(is_informed(rec)), "weight": weight(rec), "m_c": m_c,
            "excess": float(rec["excess"]) if rec else None,
            "log_usd": math.log1p(u["usd"]), "n_prints": u["n"], "price": vwap,
            "px_span_c": 100.0 * (max(u["px"]) - min(u["px"])), "dist50": abs(vwap - 0.5),
            "tte_s": end - tws, "hz_5m": 1.0 if info["hz"] == "5m" else 0.0,
            "coin": COINS.get(info["coin"], -1), "buy_up": 1.0 if d * up > 0 else 0.0,
            "burst_1s": len(h1), "burst_5s": len(hist), "usd_5s": math.log1p(usd5),
            "same_dir_5s": same / usd5 if usd5 > 0 else 0.5,
            "dmove_5s_c": 100.0 * (vwap - old[-1][3]) * (1 if d * up > 0 else -1) if old else 0.0,
        })
    counts["units"] = len(units)
    counts["labelled"] = len(rows)
    meta = {"as_of": as_of, "until": until, "scores_s": round(t_scores, 1),
            "wallets_scored": len(scores), "counts": dict(counts)}
    return sorted(rows, key=lambda r: r["tw"]), meta


def _auc(y: np.ndarray, s: np.ndarray) -> float | None:
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y, s)) if 0 < y.sum() < len(y) else None


def evaluate(rows: list[dict], train_frac: float = 0.6) -> dict:
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    X = np.array([[r[f] for f in FEATURES] for r in rows], dtype=float)
    w = np.array([r["weight"] for r in rows], dtype=float)
    cut = int(len(rows) * train_frac)
    # Relative label: taker wallet in the top decile of point-in-time excess (threshold from TRAIN rows
    # with a scored wallet). The absolute flow.INFORMED_EXCESS_C bar is unreachable while history is
    # short (K-prior shrinkage), so the absolute label is reported but not modelled.
    ex = np.array([np.nan if r.get("excess") is None else r["excess"] for r in rows], dtype=float)
    tr_ex = ex[:cut][np.isfinite(ex[:cut])]
    thr = float(np.quantile(tr_ex, 0.9)) if len(tr_ex) else math.inf
    y = np.where(np.isfinite(ex), ex >= thr, False).astype(int)
    out: dict = {"n": len(rows), "n_train": cut, "n_test": len(rows) - cut,
                 "base_rate_informed_abs": round(float(np.mean([r["informed"] for r in rows])), 4) if rows else None,
                 "label": "top-decile excess (train threshold)", "excess_threshold_c": round(thr, 3),
                 "base_rate_label": round(float(y.mean()), 4) if len(y) else None,
                 "known_wallet_share": round(float(np.mean([r["known"] for r in rows])), 4)}
    if cut < 200 or len(rows) - cut < 200 or y[:cut].sum() < 20:
        out["status"] = "insufficient"
        return out
    clf = HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=31,
                                         l2_regularization=1.0, random_state=0)
    clf.fit(X[:cut], y[:cut])
    p = clf.predict_proba(X[cut:])[:, 1]
    yt = y[cut:]
    out["auc_model"] = _auc(yt, p)
    out["auc_size_only"] = _auc(yt, X[cut:, FEATURES.index("log_usd")])
    reg = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, random_state=0)
    reg.fit(X[:cut], w[:cut])
    pw = reg.predict(X[cut:])
    out["corr_weight_model"] = round(float(np.corrcoef(pw, w[cut:])[0, 1]), 4)
    # does the anonymous score sort the test txs' OWN settlement markout (what a maker eats)?
    m = np.array([np.nan if r["m_c"] is None else r["m_c"] for r in rows[cut:]], dtype=float)
    usd = np.expm1(X[cut:, FEATURES.index("log_usd")])
    ok = np.isfinite(m)
    dec = []
    if ok.sum() >= 100:
        q = np.quantile(p[ok], np.linspace(0, 1, 6))
        for i in range(5):
            sel = ok & (p >= q[i]) & (p <= q[i + 1] if i == 4 else p < q[i + 1])
            if sel.sum():
                dec.append({"quintile": i + 1, "n": int(sel.sum()),
                            "informed_rate": round(float(yt[sel].mean()), 4),
                            "taker_markout_c_usd_wtd": round(float(np.average(m[sel], weights=usd[sel])), 3)})
    out["by_predicted_quintile"] = dec
    # direct toxicity: predict the tx's own settlement markout (what the maker on the other side eats)
    mm = np.array([np.nan if r["m_c"] is None else r["m_c"] for r in rows], dtype=float)
    tr = np.isfinite(mm[:cut])
    if tr.sum() >= 500 and ok.sum() >= 200:
        from scipy.stats import spearmanr
        rm = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, l2_regularization=1.0,
                                           random_state=0)
        rm.fit(X[:cut][tr], np.clip(mm[:cut][tr], -100, 100))
        pm = rm.predict(X[cut:])
        rho: Any = spearmanr(pm[ok], m[ok])
        q = np.quantile(pm[ok], np.linspace(0, 1, 6))
        mq = []
        for i in range(5):
            sel = ok & (pm >= q[i]) & (pm <= q[i + 1] if i == 4 else pm < q[i + 1])
            if sel.sum():
                mq.append({"quintile": i + 1, "n": int(sel.sum()),
                           "pred_c": round(float(pm[sel].mean()), 3),
                           "taker_markout_c_usd_wtd": round(float(np.average(m[sel], weights=usd[sel])), 3)})
        out["markout_model"] = {"spearman": round(float(rho.statistic), 4), "p": float(rho.pvalue),
                                "by_predicted_quintile": mq,
                                "note": "windows are correlated: many txs share one outcome; p overstates"}
    from sklearn.inspection import permutation_importance
    pi: Any = permutation_importance(clf, X[cut:], yt, scoring="roc_auc", n_repeats=5, random_state=0)
    out["importance_auc_drop"] = {f: round(float(v), 4) for f, v in
                                  sorted(zip(FEATURES, pi.importances_mean), key=lambda t: -t[1])}
    out["status"] = "ok"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-clock0")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--lab", type=Path, required=True)
    ap.add_argument("--as-of", required=True, help="score instant, ISO UTC; only later txs are used")
    ap.add_argument("--until", help="ISO UTC (default now)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--fetch", action="store_true", help="refresh token outcomes from Gamma first")
    a = ap.parse_args(argv)
    as_of = _iso(a.as_of)
    until = _iso(a.until) if a.until else int(time.time())
    rows, meta = build(a.data, a.lab, as_of, until, a.fetch)
    res = {"meta": meta, "eval": evaluate(rows), "features": FEATURES, "built_tw": time.time_ns()}
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2))
    print(orjson.dumps(res, option=orjson.OPT_INDENT_2).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
