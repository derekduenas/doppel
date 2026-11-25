"""Verdict: aggregate simulated windows and apply the genome's numeric KillRule.

- Evaluated windows are the eligible ones (book data, settled, and for a maker a trade feed). A
  window where the strategy did not trade counts as 0 net. Excluded windows are counted by reason.
- net_c_per_window = mean net cents per evaluated window; net_c_per_contract = total net cents /
  total contracts (a ratio, bootstrapped as a ratio). 95% CI from a seeded moving-block bootstrap
  over consecutive windows (``block`` windows per block, default 12 = one hour of 5m windows).
- KillRule precedence: max drawdown breached -> KILL; fewer than ``min_windows`` TRADED windows ->
  CONTINUE; CI low > pass_ci_low_gt -> PASS; CI high < kill_ci_high_lt -> KILL; else CONTINUE.
- Capacity: our fills against the displayed size at the level we took (taker) or the print that
  filled us (maker), and our contracts against the venue's total traded volume.
"""

from __future__ import annotations

import math

import numpy as np

from ..strategy.genome import Genome


def block_bootstrap_ci(values, weights=None, block: int = 12, n_boot: int = 2000, seed: int = 0,
                       chunk: int = 250) -> tuple[float | None, float | None]:
    """95% percentile CI of mean(values) (or sum(values)/sum(weights)) under a moving-block
    bootstrap of consecutive observations. Deterministic for a given seed."""
    v = np.asarray(values, dtype=float)
    w = None if weights is None else np.asarray(weights, dtype=float)
    n = len(v)
    if n < 2:
        return None, None
    b = max(1, min(int(block), n))
    k = -(-n // b)
    rng = np.random.default_rng(seed)
    stats: list[np.ndarray] = []
    done = 0
    while done < n_boot:
        m = min(chunk, n_boot - done)
        starts = rng.integers(0, n - b + 1, size=(m, k))
        idx = (starts[:, :, None] + np.arange(b)[None, None, :]).reshape(m, -1)[:, :n]
        if w is None:
            stats.append(v[idx].mean(axis=1))
        else:
            den = w[idx].sum(axis=1)
            num = v[idx].sum(axis=1)
            stats.append(np.where(den > 0, num / np.where(den > 0, den, 1.0), np.nan))
        done += m
    s = np.concatenate(stats)
    s = s[~np.isnan(s)]
    if len(s) < 0.5 * n_boot:
        return None, None
    lo, hi = np.percentile(s, [2.5, 97.5])
    return float(lo), float(hi)


def max_drawdown(pnl_usd) -> float:
    c = np.cumsum(np.asarray(pnl_usd, dtype=float))
    if not len(c):
        return 0.0
    peak = np.maximum.accumulate(np.concatenate([[0.0], c]))[1:]
    return float(max(0.0, np.max(peak - c)))


def _wmean(pairs) -> float | None:
    num = den = 0.0
    for v, w in pairs:
        if v is not None and w > 0:
            num += v * w
            den += w
    return round(num / den, 4) if den > 0 else None


def decide(g: Genome, n_traded: int, ci: tuple[float | None, float | None], dd: float) -> tuple[str, str]:
    k = g.kill
    if dd > k.max_drawdown_usd:
        return "KILL", f"max drawdown ${dd:.2f} > ${k.max_drawdown_usd:g}"
    if n_traded < k.min_windows:
        return "CONTINUE", f"{n_traded} traded windows < min_windows {k.min_windows}"
    lo, hi = ci
    if lo is None or hi is None:
        return "CONTINUE", "no confidence interval (no contracts or too few windows)"
    if lo > k.pass_ci_low_gt:
        return "PASS", f"CI low {lo:.3f} > {k.pass_ci_low_gt:g} ({k.metric})"
    if hi < k.kill_ci_high_lt:
        return "KILL", f"CI high {hi:.3f} < {k.kill_ci_high_lt:g} ({k.metric})"
    return "CONTINUE", f"CI [{lo:.3f}, {hi:.3f}] straddles the pass/kill lines ({k.metric})"


def verdict(g: Genome, results: list[dict], block: int = 12, n_boot: int = 2000, seed: int = 0) -> dict:
    ev = [r for r in results if r.get("eligible")]
    excluded: dict[str, int] = {}
    sources: dict[str, int] = {}
    for r in results:
        sources[r["source"]] = sources.get(r["source"], 0) + 1
        if not r.get("eligible"):
            excluded[r["excluded"]] = excluded.get(r["excluded"], 0) + 1
    net_c = np.array([r["net_c"] for r in ev], dtype=float)
    ctr = np.array([r["contracts"] for r in ev], dtype=float)
    n, n_traded = len(ev), int((ctr > 0).sum())
    pw = float(net_c.mean()) if n else None
    pw_ci = block_bootstrap_ci(net_c, None, block, n_boot, seed) if n else (None, None)
    pc = float(net_c.sum() / ctr.sum()) if ctr.sum() > 0 else None
    pc_ci = block_bootstrap_ci(net_c, ctr, block, n_boot, seed) if pc is not None else (None, None)
    by_end = sorted(ev, key=lambda r: (r["end"], r["start"]))
    dd = max_drawdown([r["net_usd"] for r in by_end])
    ci = pw_ci if g.kill.metric == "net_c_per_window" else pc_ci
    decision, reason = decide(g, n_traded, ci, dd)
    fills = [f for r in ev for f in r["fills"]]
    tk = [f for f in fills if f["role"] == "taker"]
    mk = [f for f in fills if f["role"] == "maker"]
    vol = sum((r["tape"].get("venue_volume") or r["tape"].get("trade_volume") or 0.0) for r in ev)
    traded_vol = sum((r["tape"].get("venue_volume") or r["tape"].get("trade_volume") or 0.0)
                     for r in ev if r["contracts"] > 0)
    horizons = sorted({str(k) for f in fills for k in f.get("markout_c", {})}, key=int)
    total_net = float(net_c.sum() / 100.0) if n else 0.0
    best = float(net_c.max() / 100.0) if n else 0.0
    sd = float(net_c.std(ddof=1)) if n > 1 else None
    out = {
        "decision": decision, "reason": reason, "metric": g.kill.metric,
        "kill_rule": {"min_windows": g.kill.min_windows, "pass_ci_low_gt": g.kill.pass_ci_low_gt,
                      "kill_ci_high_lt": g.kill.kill_ci_high_lt, "max_drawdown_usd": g.kill.max_drawdown_usd},
        "windows_total": len(results), "windows_evaluated": n, "windows_traded": n_traded,
        "windows_stood_down": sum(1 for r in ev if r.get("stood_down")),
        "excluded": excluded, "sources": sources,
        "net_c_per_window": {"mean": _r(pw), "ci95": [_r(pw_ci[0]), _r(pw_ci[1])],
                             "naive_t": _r(pw / (sd / math.sqrt(n))) if pw is not None and sd else None},
        "net_c_per_contract": {"value": _r(pc), "ci95": [_r(pc_ci[0]), _r(pc_ci[1])]},
        "bootstrap": {"block_windows": block, "n_boot": n_boot, "seed": seed},
        "total_net_usd": round(total_net, 4), "net_excl_best_window_usd": round(total_net - best, 4) if n else 0.0,
        "gross_usd": round(sum(r["gross_usd"] for r in ev), 4), "fees_usd": round(sum(r["fees_usd"] for r in ev), 4),
        "contracts": round(float(ctr.sum()), 4), "max_drawdown_usd": round(dd, 4),
        "capacity": {
            "taker_fill_vs_displayed": _ratio(sum(f["qty"] for f in tk), sum(f["ref_qty"] for f in tk)),
            "taker_depth_capped": sum(r["taker"]["depth_capped"] for r in ev),
            "maker_fill_vs_print": _ratio(sum(f["qty"] for f in mk if f["via"] == "trade"),
                                          sum(f["ref_qty"] for f in mk if f["via"] == "trade")),
            "share_of_venue_volume": _ratio(float(ctr.sum()), vol),
            "share_of_venue_volume_traded_windows": _ratio(float(ctr.sum()), traded_vol),
            "venue_volume_evaluated": round(vol, 2),
        },
        "markout_c": {
            "taker": {h: _wmean((f["markout_c"].get(h), f["qty"]) for f in tk) for h in horizons},
            "maker": {h: _wmean((f["markout_c"].get(h), f["qty"]) for f in mk) for h in horizons},
        },
        "taker": {k: sum(r["taker"][k] for r in ev) for k in (ev[0]["taker"] if ev else {})},
        "maker": _maker_summary(ev, mk),
    }
    return out


def _maker_summary(ev: list[dict], mk: list[dict]) -> dict:
    if not ev:
        return {}
    keys = [k for k, v in ev[0]["maker"].items() if type(v) is int]
    out: dict = {k: sum(r["maker"][k] for r in ev) for k in keys}
    quoted = sum(r["maker"]["quoted_qty"] for r in ev)
    filled = sum(r["maker"]["fill_qty"] for r in ev)
    live = sum(r["maker"]["quote_live_s"] for r in ev)
    placed = out.get("placed", 0)
    out.update({"quoted_qty": round(quoted, 4), "fill_qty": round(filled, 4),
                "fill_rate": _ratio(filled, quoted),
                "mean_quote_live_s": round(live / placed, 3) if placed else None,
                "fills_via_trade": sum(1 for f in mk if f["via"] == "trade"),
                "fills_via_book_cross": sum(1 for f in mk if f["via"] == "book_cross")})
    return out


def _r(x: float | None, nd: int = 4) -> float | None:
    return None if x is None else round(float(x), nd)


def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 6) if b else None
