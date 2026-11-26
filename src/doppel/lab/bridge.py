"""PM US BTC 15m vs Kalshi KXBTC15M on WebSocket books: lead-lag, S5 hedge miss, S6 post-print prices.

Same contract on two venues (E23). Inputs are recorded WS tapes only, read as of OUR receive time
``tw`` (both recorders stamp with the same host clock): PM US ``pmus_ws_15m`` (``twin.tape``, gaps
make the book unknown) and Kalshi ``kalshi_ws`` (``kalshi_ws.py``, seq gaps make it unknown until
the next snapshot). A Kalshi ticker ``KXBTC15M-YYMMMDDHHMM-MM`` names the window CLOSE in US
Eastern time; its window start is close - 900 s. Each window reports its median mid gap, so a bad
pairing shows up as a large gap. Every window is analysed on [start + 5 s, end).

a. Lead-lag. On a 100 ms receive-time grid, the mid of each venue (None when the book is unknown
   or one-sided). Pooled cross-correlation of 100 ms mid changes, corr(dK[i], dP[i+j]) for
   j = -50..50 (lag +j*0.1 s > 0: PM US moves AFTER Kalshi, i.e. Kalshi leads). lead_score_Xs =
   sum of corr over positive lags up to X minus the same over negative lags (> 0: Kalshi leads).
   Events: a "move" on venue X is the first state at which X's mid is >= 1c away from its mid at
   the previous move (or at the first known state after an unknown stretch), so a 1c move made of
   two half-cent steps (separate Kalshi deltas for bid and ask) counts once, on both venues alike.
   A move at t_e is used if it is INITIATING (venue Y's mid did not already move >= 0.5c the same way over [t_e - 1 s,
   t_e]). Y's reference is its mid as of t_e. Y "follows" within h if its mid reaches ref +/- 0.5c
   in the same direction within (t_e, t_e + h]; the opposite-direction rate is the activity
   baseline, and net follow = same - opposite. Scanning stops when Y's book becomes unknown.
   Events need t_e + 5 s < end. Who leads: net_follow(K->P) - net_follow(P->K) at 1 s, with a
   window-cluster bootstrap 95% CI (windows are the independent unit); a lead under ~100 ms
   cannot be told apart from the two recorders' stamping differences.
b. S5 hedge miss. Hypothetical resting Kalshi YES bid b_K = floor_cent(PM bid - fee_PM(PM bid) - m)
   (and the mirror: Kalshi NO bid n_K = floor_cent(1 - PM ask - fee_PM(PM ask) - m)); fee_PM is the
   PM US API taker fee per contract at --size contracts: fee_usd(coef, p, size) (cent half-even,
   the twin's rounding), times (1 - rebate), divided by size (xvenue.pmus_fee convention). The quote
   decided from the PM US book as of t takes effect at t + requote (default 100 ms); it is placed
   only if Kalshi's book is known and its best YES ask > b_K (otherwise it would take, not rest).
   FILL (conservative): the first Kalshi event, while resting, of (i) a print with YES price
   strictly below b_K (trade-through: our better bid would have been hit first), or (ii) the Kalshi
   book showing best YES ask <= b_K (an ask at or under our bid can only rest if it would have
   matched us; real bids are all below it). ``--fill touch`` also counts a YES-selling print AT
   b_K (optimistic: queue ahead of us is unknown). After a fill the quote is gone and is re-decided
   at fill + requote. Hedge: sell YES on PM US at its best bid known at fill + L. Pair net =
   h - b_K - fee_PM(h) - kalshi maker fee; miss = net < 0; slip = -net on a miss; a fill whose PM
   US book is unknown at fill + L is ``unknown`` (neither hit nor miss). Expectancy = mean net over
   known fills (also shown: the strategies-page formula (1-miss)*m - miss*E[slip|miss]). Headline
   numbers keep one fill per hedge horizon (a fill within L of the previous counted fill is
   dropped), so the result does not scale with --requote-ms; ``raw`` keeps every fill.
   Biases: the fill rule needs the price to trade THROUGH us or an ask to rest at our price, so it
   undercounts fills and over-represents adverse ones (miss rate biased UP); a post-only seller
   that would have been rejected by our bid is counted as a fill (rule ii); our own order would
   change the book we read; hedge uses top-of-book price only (``depth_ok`` = top bid qty >= size).
c. S6. For each PM US printed trade (inferred, unprinted volume excluded) in the window, the Kalshi
   executable opposite price at print tw + 0 / 0.25 / 1 / 5 s: after a BUY print (a PM maker sold
   YES) the Kalshi YES ask, rel = print px - ask; after a SELL print the Kalshi YES bid, rel = bid -
   print px. rel > 0 means the hedge locks a profit before the Kalshi taker fee.

    doppel-bridge --data ROOT [--kalshi-data RUST_ROOT] --from T0 --to T1 [--out OUT.json]
"""

from __future__ import annotations

import argparse
import bisect
import heapq
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np
import orjson

from ..twin.engine import fee_usd
from ..twin.tape import INFERRED, NS, WindowTape, iter_windows
from .kalshi_ws import Print, Top, replay

SKIP_S = 5
GRID_NS = 100_000_000
LAGS = 50                       # grid steps each way (5 s)
MOVE = 0.01                     # trigger: mid move >= 1c
RESP = 0.005                    # response: mid >= 0.5c away from the reference
PRE_NS = 1 * NS                 # initiating filter look-back
HORIZONS_S = (0.25, 1.0, 2.0, 5.0)
HEDGE_L_S = (0.25, 1.0)
MARGINS_C = (0, 1, 2)
S6_DELTAS_S = (0.0, 0.25, 1.0, 5.0)
STOP_S = 5                      # S5: no quotes in the last seconds of a window
QS = (5, 10, 25, 50, 75, 90, 95)
BOOT = 1000
EPS = 1e-9
ET = ZoneInfo("America/New_York")
_TICKER = re.compile(r"^KXBTC15M-(\d{2})([A-Z]{3})(\d{2})(\d{2})(\d{2})-(\d{2})$")
_MON = {m: i + 1 for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
                                         "OCT", "NOV", "DEC"))}


@dataclass(frozen=True)
class Cfg:
    size: float = 10.0
    requote_s: float = 0.1
    pmus_coef: float = 0.0695
    pmus_rebate: float = 0.70
    kalshi_maker_coef: float = 0.0
    kalshi_taker_coef: float = 0.07
    fill: str = "through"           # "through" | "touch"

    def pm_fee(self, p: float) -> float:
        """PM US taker fee per contract (dollars) at --size contracts."""
        return fee_usd(self.pmus_coef, p, self.size) * (1 - self.pmus_rebate) / self.size

    def k_maker_fee(self, p: float) -> float:
        if self.kalshi_maker_coef == 0:
            return 0.0
        return math.ceil(self.kalshi_maker_coef * self.size * p * (1 - p) * 100 - EPS) / 100 / self.size

    def k_taker_fee(self, p: float) -> float:
        return math.ceil(self.kalshi_taker_coef * self.size * p * (1 - p) * 100 - EPS) / 100 / self.size


# ---------------------------------------------------------------------------------------- series
class Series:
    """As-of state of one venue's top of book: t (receive ns, ascending), v (Top or None = unknown)."""

    def __init__(self, ev: list[tuple[int, Top | None]]):
        self.t = [e[0] for e in ev]
        self.v = [e[1] for e in ev]

    def at(self, tw: int) -> Top | None:
        i = bisect.bisect_right(self.t, tw) - 1
        return None if i < 0 else self.v[i]

    def mid_at(self, tw: int) -> float | None:
        return mid(self.at(tw))


def mid(top: Top | None) -> float | None:
    if top is None or top[0] is None or top[2] is None:
        return None
    return (top[0] + top[2]) / 2


def kalshi_start(ticker: str) -> int | None:
    """Window start (unix s) of a KXBTC15M ticker: its name is the close in US Eastern time."""
    m = _TICKER.match(ticker)
    if not m or m.group(2) not in _MON or m.group(5) != m.group(6):
        return None
    yy, mon, dd, hh, mi, _ = m.groups()
    close = datetime(2000 + int(yy), _MON[mon], int(dd), int(hh), int(mi), tzinfo=ET)
    return int(close.timestamp()) - 900


def pm_series(wt: WindowTape) -> Series:
    ev: list[tuple[int, int, Top | None]] = [(g, 0, None) for g in wt.gaps]
    for b in wt.books:
        top = (b.best_bid, b.bids[0][1] if b.bids else 0.0, b.best_ask, b.asks[0][1] if b.asks else 0.0)
        ev.append((b.tw, 1, top if b.open else None))
    ev.sort(key=lambda e: (e[0], e[1]))
    return Series([(e[0], e[2]) for e in ev])


def pm_prints(wt: WindowTape) -> list[tuple[int, float, float, str]]:
    return [(t.tw, t.price, t.qty, t.aggressor) for t in wt.trades if not (t.id or "").startswith(INFERRED)]


# ---------------------------------------------------------------------------------------- stats
def _q(xs: list[float]) -> dict | None:
    if not xs:
        return None
    a = np.asarray(xs, dtype=float)
    return {f"p{q}": round(float(np.percentile(a, q)), 3) for q in QS}


def boot_ci(nw: int, stat: Callable[[np.ndarray], float | None], b: int = BOOT) -> list[float] | None:
    """95% percentile CI of ``stat`` over window-cluster bootstrap resamples (None if < 2 windows)."""
    if nw < 2:
        return None
    rng = np.random.default_rng(0)
    vals = []
    for _ in range(b):
        v = stat(rng.integers(0, nw, nw))
        if v is not None and math.isfinite(v):
            vals.append(v)
    if not vals:
        return None
    return [round(float(np.percentile(vals, 2.5)), 4), round(float(np.percentile(vals, 97.5)), 4)]


def _ratio(num: np.ndarray, den: np.ndarray) -> Callable[[np.ndarray], float | None]:
    def f(idx: np.ndarray) -> float | None:
        d = float(den[idx].sum())
        return None if d <= 0 else float(num[idx].sum()) / d
    return f


# ---------------------------------------------------------------------------------------- (a) lead-lag
def _grid_mids(s: Series, g: np.ndarray) -> np.ndarray:
    t = np.asarray(s.t, dtype=np.int64)
    m = np.asarray([np.nan if (x := mid(v)) is None else x for v in s.v], dtype=float)
    if len(t) == 0:
        return np.full(len(g), np.nan)
    i = np.searchsorted(t, g, side="right") - 1
    out = np.where(i >= 0, m[np.clip(i, 0, None)], np.nan)
    return out


def xcorr_sums(mk: np.ndarray, mp: np.ndarray, lags: int = LAGS) -> np.ndarray:
    """(2*lags+1, 6) sums n, sx, sy, sxx, syy, sxy of x = dK[i], y = dP[i+j], j = -lags..lags."""
    dk, dp = np.diff(mk), np.diff(mp)
    out = np.zeros((2 * lags + 1, 6))
    n = len(dk)
    for r, j in enumerate(range(-lags, lags + 1)):
        if j >= 0:
            x, y = dk[: n - j] if j else dk, dp[j:]
        else:
            x, y = dk[-j:], dp[: n + j]
        ok = np.isfinite(x) & np.isfinite(y)
        x, y = x[ok], y[ok]
        out[r] = (len(x), x.sum(), y.sum(), (x * x).sum(), (y * y).sum(), (x * y).sum())
    return out


def _corr(s: np.ndarray) -> np.ndarray:
    n, sx, sy, sxx, syy, sxy = s.T
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = sxy - sx * sy / n
        vx, vy = sxx - sx * sx / n, syy - sy * sy / n
        return np.where((n > 1) & (vx > 0) & (vy > 0), cov / np.sqrt(vx * vy), np.nan)


def lead_score(c: np.ndarray, k: int, lags: int = LAGS) -> float:
    return float(np.nansum(c[lags + 1: lags + 1 + k]) - np.nansum(c[lags - k: lags]))


def follow_events(x: Series, y: Series, a: int, end: int) -> list[dict]:
    """Initiating >= 1c mid moves on X and how Y responds (see module doc)."""
    out = []
    anchor = None
    for tw, v in zip(x.t, x.v):
        m = mid(v)
        if m is None or anchor is None:
            anchor = m
            continue
        d = m - anchor
        if abs(d) < MOVE - EPS:
            continue
        anchor = m
        if tw < a or tw + max(HORIZONS_S) * NS >= end:
            continue
        s = 1.0 if d > 0 else -1.0
        ref = y.mid_at(tw)
        pre = y.mid_at(tw - PRE_NS)
        if ref is None or pre is None:
            continue
        if s * (ref - pre) >= RESP - EPS:
            continue                                   # Y already moved this way: X is responding
        same = opp = None
        i = bisect.bisect_right(y.t, tw)
        while i < len(y.t) and y.t[i] <= tw + max(HORIZONS_S) * NS:
            ym = mid(y.v[i])
            if ym is None:
                break
            dt = (y.t[i] - tw) / NS
            if same is None and s * (ym - ref) >= RESP - EPS:
                same = dt
            if opp is None and s * (ym - ref) <= -(RESP - EPS):
                opp = dt
            if same is not None and opp is not None:
                break
            i += 1
        out.append({"t": tw, "dir": s, "same": same, "opp": opp})
    return out


def _follow_table(evs_w: list[list[dict]]) -> tuple[dict, np.ndarray, np.ndarray, np.ndarray]:
    """Summary + per-window arrays n (W,), same (W, H), opp (W, H)."""
    W, H = len(evs_w), len(HORIZONS_S)
    n, same, opp = np.zeros(W), np.zeros((W, H)), np.zeros((W, H))
    delays = []
    for w, evs in enumerate(evs_w):
        n[w] = len(evs)
        for e in evs:
            for k, h in enumerate(HORIZONS_S):
                same[w, k] += e["same"] is not None and e["same"] <= h
                opp[w, k] += e["opp"] is not None and e["opp"] <= h
            if e["same"] is not None:
                delays.append(e["same"])
    tot = n.sum()
    rows = {}
    for k, h in enumerate(HORIZONS_S):
        ps = float(same[:, k].sum() / tot) if tot else None
        po = float(opp[:, k].sum() / tot) if tot else None
        rows[f"{h:g}s"] = {"p_same": None if ps is None else round(ps, 4),
                           "p_opp": None if po is None else round(po, 4),
                           "net": None if ps is None or po is None else round(ps - po, 4)}
    summ = {"events": int(tot), "by_horizon": rows,
            "median_delay_s": round(float(np.median(delays)), 3) if delays else None,
            "delay_q_s": _q(delays)}
    return summ, n, same, opp


# ---------------------------------------------------------------------------------------- (b) S5
def _mirror_top(v: Top | None) -> Top | None:
    if v is None:
        return None
    bb, bq, ba, aq = v
    return (None if ba is None else round(1 - ba, 6), aq, None if bb is None else round(1 - bb, 6), bq)


def _mirror(s: Series) -> Series:
    return Series([(t, _mirror_top(v)) for t, v in zip(s.t, s.v)])


def _mirror_prints(pr: list[Print]) -> list[Print]:
    return [Print(p.tw, p.ticker, round(1 - p.yes_px, 6), p.qty, "yes" if p.taker_side == "no" else "no") for p in pr]


def _floor_c(x: float) -> float:
    return math.floor(x * 100 + 1e-6) / 100


def quote_px(pm: Top | None, m_c: int, cfg: Cfg) -> float | None:
    """Our Kalshi YES bid from a PM US book (YES side; the NO side runs on mirrored books)."""
    if pm is None or pm[0] is None:
        return None
    b = _floor_c(pm[0] - cfg.pm_fee(pm[0]) - m_c / 100)
    return b if b >= 0.01 - EPS else None


def s5_window(pm: Series, ks: Series, kp: list[Print], start: int, end: int, m_c: int,
              cfg: Cfg) -> tuple[list[dict], dict]:
    """Fills of a hypothetical resting Kalshi YES bid in one window, with hedge outcomes."""
    a, stop = (start + SKIP_S) * NS, (end - STOP_S) * NS
    lq = int(cfg.requote_s * NS)
    heap: list[tuple[int, int, int, object]] = []    # (tw, kind, order, payload): 0 K book, 1 print, 2 decide
    n = 0
    for t, v in zip(ks.t, ks.v):
        heap.append((t, 0, n, v))
        n += 1
    for p in kp:
        heap.append((p.tw, 1, n, p))
        n += 1
    for t in [a, *(t + lq for t in pm.t)]:      # a decision at the analysis start, then after every PM change
        heap.append((t, 2, n, None))
        n += 1
    heapq.heapify(heap)
    q: float | None = None
    fills: list[dict] = []
    cnt = {"placed": 0, "would_cross": 0, "cancel_k_unknown": 0}
    while heap:
        t, kind, _, pay = heapq.heappop(heap)
        if t >= stop:
            break
        if kind == 2:
            if t < a:
                continue
            qn = quote_px(pm.at(t - lq), m_c, cfg)
            if qn is None:
                q = None
                continue
            if q is not None and abs(qn - q) < EPS:
                continue
            k = ks.at(t)
            if k is None:
                q = None
                continue
            if k[2] is not None and k[2] <= qn + EPS:
                cnt["would_cross"] += 1
                q = None
                continue
            q = qn
            cnt["placed"] += 1
            continue
        if q is None:
            continue
        via = None
        if kind == 0:
            if pay is None:
                cnt["cancel_k_unknown"] += 1
                q = None
                continue
            assert isinstance(pay, tuple)
            if pay[2] is not None and pay[2] <= q + EPS:
                via = "book"
        else:
            assert isinstance(pay, Print)
            if pay.yes_px < q - EPS or (cfg.fill == "touch" and pay.taker_side == "no" and pay.yes_px <= q + EPS):
                via = "print"
        if via is None:
            continue
        stale = quote_px(pm.at(t), m_c, cfg)
        f: dict = {"t": t, "q": q, "via": via, "stale": stale is None or stale < q - EPS, "hedge": {}}
        for L in HEDGE_L_S:
            th = t + int(L * NS)
            h = pm.at(th)
            if h is None or h[0] is None or th >= end * NS:
                f["hedge"][L] = None
                continue
            net = h[0] - q - cfg.pm_fee(h[0]) - cfg.k_maker_fee(q)
            f["hedge"][L] = {"px": h[0], "net": net, "depth_ok": h[1] >= cfg.size}
        fills.append(f)
        q = None
        heapq.heappush(heap, (t + lq, 2, n, None))
        n += 1
    return fills, cnt


def dedup_fills(fs: list[dict], L: float) -> list[dict]:
    """Drop fills within L of the previous COUNTED fill: one fill per hedge horizon, so a single
    sell-off re-filling the re-armed quote every requote interval counts once."""
    out: list[dict] = []
    for f in fs:
        if not out or f["t"] >= out[-1]["t"] + int(L * NS):
            out.append(f)
    return out


def s5_summary(per_w: list[list[dict]], m_c: int, L: float) -> dict:
    """Headline on de-duplicated fills (``dedup_fills``); ``raw`` = every fill."""
    row = _s5_stats([dedup_fills(fs, L) for fs in per_w], m_c, L)
    raw = _s5_stats(per_w, m_c, L)
    row["raw"] = {k: raw[k] for k in ("fills", "known", "miss_rate", "slip_given_miss_c", "expectancy_c")}
    return row


def _s5_stats(per_w: list[list[dict]], m_c: int, L: float) -> dict:
    W = len(per_w)
    known, miss, net, slip = np.zeros(W), np.zeros(W), np.zeros(W), np.zeros(W)
    fills = unknown = book = depth = stale = 0
    nets, slips, hits = [], [], []
    for w, fs in enumerate(per_w):
        for f in fs:
            fills += 1
            book += f["via"] == "book"
            stale += f["stale"]
            h = f["hedge"][L]
            if h is None:
                unknown += 1
                continue
            known[w] += 1
            net[w] += h["net"]
            nets.append(100 * h["net"])
            depth += h["depth_ok"]
            if h["net"] < -EPS:
                miss[w] += 1
                slip[w] += -h["net"]
                slips.append(-100 * h["net"])
            else:
                hits.append(100 * h["net"])
    k = int(known.sum())
    mr = float(miss.sum() / k) if k else None
    es = float(np.mean(slips)) if slips else None
    exp = float(np.mean(nets)) if nets else None
    doc = None if mr is None else (1 - mr) * m_c - mr * (es or 0.0)
    ci_m = boot_ci(W, _ratio(miss, known))
    ci_e = boot_ci(W, _ratio(net, known))
    return {"m_c": m_c, "L_ms": int(L * 1000), "fills": fills, "fills_via_book": book,
            "fills_via_print": fills - book, "hedge_unknown": unknown, "known": k,
            "windows_with_fills": int((known > 0).sum()),
            "miss_rate": None if mr is None else round(mr, 4), "miss_rate_ci": ci_m,
            "slip_given_miss_c": None if es is None else round(es, 3),
            "net_given_hit_c": round(float(np.mean(hits)), 3) if hits else None,
            "expectancy_c": None if exp is None else round(exp, 3),
            "expectancy_ci_c": None if ci_e is None else [round(100 * x, 3) for x in ci_e],
            "expectancy_formula_c": None if doc is None else round(doc, 3),
            "net_q_c": _q(nets), "depth_ok_share": round(depth / k, 4) if k else None,
            "stale_quote_share": round(stale / fills, 4) if fills else None}


# ---------------------------------------------------------------------------------------- (c) S6
def s6_rows(per_w: list[tuple[list, Series]], start_end: list[tuple[int, int]], cfg: Cfg) -> list[dict]:
    rows = []
    W = len(per_w)
    for side in ("buy", "sell"):
        for dlt in S6_DELTAS_S:
            rel, px, fee_ok = [], [], 0
            sums, cnts = np.zeros(W), np.zeros(W)
            for w, ((prints, ks), (st, en)) in enumerate(zip(per_w, start_end)):
                for tw, ppx, _qty, agg in prints:
                    if agg != side or tw < (st + SKIP_S) * NS or tw + 5 * NS >= en * NS:
                        continue
                    k = ks.at(tw + int(dlt * NS))
                    if k is None:
                        continue
                    kp = k[2] if side == "buy" else k[0]
                    if kp is None:
                        continue
                    r = 100 * ((ppx - kp) if side == "buy" else (kp - ppx))
                    rel.append(r)
                    px.append(kp)
                    fee_ok += r > 100 * cfg.k_taker_fee(kp) + EPS
                    sums[w] += r
                    cnts[w] += 1
            ci = boot_ci(W, _ratio(sums, cnts))
            rows.append({"print_side": side, "kalshi_px": "yes_ask" if side == "buy" else "yes_bid",
                         "delta_s": dlt, "n": len(rel), "windows": int((cnts > 0).sum()),
                         "rel_mean_c": round(float(np.mean(rel)), 3) if rel else None, "rel_mean_ci_c": ci,
                         "rel_q_c": _q(rel), "kalshi_px_q": _q(px),
                         "share_profitable_after_kalshi_taker_fee": round(fee_ok / len(rel), 4) if rel else None})
    return rows


# ---------------------------------------------------------------------------------------- run
def _crossed(s: Series, g: np.ndarray) -> float:
    """Share of grid points with a known two-sided book that is crossed (bid >= ask): a tripwire for
    a misread NO side (NO levels are taken as NO bids)."""
    n = c = 0
    for tw in g[::10]:
        v = s.at(int(tw))
        if v is not None and v[0] is not None and v[2] is not None:
            n += 1
            c += v[0] >= v[2] - EPS
    return round(c / n, 4) if n else 0.0


def _when(s: str) -> int:
    if s.isdigit():
        return int(s)
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return int((d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp())


def load_kalshi(root: Path, t0: int, t1: int) -> tuple[dict[str, Series], dict[str, list[Print]], dict]:
    evs, kb = replay(root, t0, t1 + 10, want=lambda t: t.startswith("KXBTC15M-"))
    tops: dict[str, list[tuple[int, Top | None]]] = {}
    prints: dict[str, list[Print]] = {}
    for kind, t, tw, x in evs:
        if kind == "book":
            tops.setdefault(t, []).append((tw, x))
        else:
            prints.setdefault(t, []).append(x)
    info = {"stats": kb.stats, "seq_gaps": len(kb.gaps), "gaps_first": kb.gaps[:50]}
    # ctl records are stamped at write time and may sit microseconds out of order: stable sort
    return {t: Series(sorted(v, key=lambda e: e[0])) for t, v in tops.items()}, prints, info


def run(data: Path, kdata: Path, t0: int, t1: int, cfg: Cfg = Cfg()) -> dict:
    ks_all, kp_all, kinfo = load_kalshi(kdata, t0, t1)
    by_start: dict[int, str] = {}
    for t in ks_all:
        st = kalshi_start(t)
        if st is not None:
            by_start[st] = t
    wins, skipped = [], []
    for wt in iter_windows(data, ["btc"], ["15m"], t0, t1):
        tk = by_start.get(wt.start)
        if wt.source != "ws":
            skipped.append({"slug": wt.slug, "start": wt.start, "reason": "no_pmus_ws"})
            continue
        if tk is None:
            skipped.append({"slug": wt.slug, "start": wt.start, "reason": "no_kalshi_ws"})
            continue
        ps, ks = pm_series(wt), ks_all[tk]
        a, end = (wt.start + SKIP_S) * NS, wt.end * NS
        g = np.arange(a, end, GRID_NS, dtype=np.int64)
        mk, mp = _grid_mids(ks, g), _grid_mids(ps, g)
        both = np.isfinite(mk) & np.isfinite(mp)
        if not both.any():
            skipped.append({"slug": wt.slug, "start": wt.start, "ticker": tk, "reason": "no_common_valid_book"})
            continue
        gap = 100 * (mp[both] - mk[both])
        wins.append({"wt": wt, "ticker": tk, "ps": ps, "ks": ks, "kp": kp_all.get(tk, []), "mk": mk, "mp": mp,
                     "row": {"slug": wt.slug, "ticker": tk, "start": wt.start,
                             "pm_book_events": len(ps.t), "kalshi_book_events": len(ks.t),
                             "pm_prints": len(pm_prints(wt)), "kalshi_prints": len(kp_all.get(tk, [])),
                             "grid_known_pm": round(float(np.isfinite(mp).mean()), 4),
                             "grid_known_kalshi": round(float(np.isfinite(mk).mean()), 4),
                             "mid_gap_c_p50": round(float(np.median(gap)), 3),
                             "mid_gap_abs_c_p50": round(float(np.median(np.abs(gap))), 3),
                             "kalshi_crossed_share": _crossed(ks, g)}})
    W = len(wins)
    res: dict = {"params": {"from": t0, "to": t1, "size": cfg.size, "requote_s": cfg.requote_s,
                            "pmus_coef": cfg.pmus_coef, "pmus_rebate": cfg.pmus_rebate,
                            "kalshi_maker_coef": cfg.kalshi_maker_coef, "kalshi_taker_coef": cfg.kalshi_taker_coef,
                            "fill": cfg.fill, "grid_ms": GRID_NS // 1_000_000, "move_c": 100 * MOVE,
                            "resp_c": 100 * RESP, "boot": BOOT},
                 "kalshi_ws": kinfo, "windows": [w["row"] for w in wins], "skipped": skipped}
    # (a) lead-lag
    xs = np.stack([xcorr_sums(w["mk"], w["mp"]) for w in wins]) if W else np.zeros((0, 2 * LAGS + 1, 6))
    c = _corr(xs.sum(axis=0)) if W else np.full(2 * LAGS + 1, np.nan)
    lags_s = [round(j * GRID_NS / NS, 1) for j in range(-LAGS, LAGS + 1)]
    kp_ev = [follow_events(w["ks"], w["ps"], (w["wt"].start + SKIP_S) * NS, w["wt"].end * NS) for w in wins]
    pk_ev = [follow_events(w["ps"], w["ks"], (w["wt"].start + SKIP_S) * NS, w["wt"].end * NS) for w in wins]
    kps, kn, ksame, kopp = _follow_table(kp_ev)
    pks, pn, psame, popp = _follow_table(pk_ev)
    diff = {}
    for k, h in enumerate(HORIZONS_S):
        def stat(idx, k=k):
            a1, b1 = kn[idx].sum(), pn[idx].sum()
            if a1 <= 0 or b1 <= 0:
                return None
            return float((ksame[idx, k].sum() - kopp[idx, k].sum()) / a1
                         - (psame[idx, k].sum() - popp[idx, k].sum()) / b1)
        est = stat(np.arange(W)) if W else None
        diff[f"{h:g}s"] = {"est": None if est is None else round(est, 4), "ci": boot_ci(W, stat)}
    ls = {}
    for k in (10, 50):
        est = lead_score(c, k) if W else None
        ls[f"lead_score_{k // 10}s"] = {"est": None if est is None or not math.isfinite(est) else round(est, 4),
                                        "ci": boot_ci(W, lambda idx, k=k: lead_score(_corr(xs[idx].sum(axis=0)), k))}
    d1 = diff[f"{1.0:g}s"]
    if d1["ci"] is None:
        verdict = "undetermined (fewer than 2 windows)"
    elif d1["ci"][0] > 0:
        verdict = "kalshi leads"
    elif d1["ci"][1] < 0:
        verdict = "pmus leads"
    else:
        verdict = "no significant lead at 1 s"
    peak = int(np.nanargmax(np.abs(c))) if W and np.isfinite(c).any() else None
    res["leadlag"] = {"xcorr": {"lags_s": lags_s, "corr": [None if not math.isfinite(x) else round(float(x), 4) for x in c],
                                "n": [int(x) for x in xs.sum(axis=0)[:, 0]] if W else []},
                      "peak_abs_corr_lag_s": None if peak is None else lags_s[peak], **ls,
                      "kalshi_to_pmus": kps, "pmus_to_kalshi": pks,
                      "net_follow_diff_k_minus_p": diff, "verdict_1s": verdict}
    # (b) S5
    s5 = []
    for side in ("yes", "no"):
        for m_c in MARGINS_C:
            per_w = []
            for w in wins:
                ps, ks, kp = w["ps"], w["ks"], w["kp"]
                if side == "no":
                    ps, ks, kp = _mirror(ps), _mirror(ks), _mirror_prints(kp)
                per_w.append(s5_window(ps, ks, kp, w["wt"].start, w["wt"].end, m_c, cfg))
            for L in HEDGE_L_S:
                row = s5_summary([f for f, _ in per_w], m_c, L)
                row["side"] = side
                for key in ("placed", "would_cross", "cancel_k_unknown"):
                    row[key] = sum(x[key] for _, x in per_w)
                s5.append(row)
    res["s5"] = s5
    # (c) S6
    res["s6"] = s6_rows([(pm_prints(w["wt"]), w["ks"]) for w in wins],
                        [(w["wt"].start, w["wt"].end) for w in wins], cfg)
    return res


def summary_lines(res: dict) -> list[str]:
    ll = res["leadlag"]
    out = [f"windows: {len(res['windows'])} analysed, {len(res['skipped'])} skipped; "
           f"kalshi_ws frames {res['kalshi_ws']['stats']['frames']}, seq gaps {res['kalshi_ws']['seq_gaps']}, "
           f"unparsed {res['kalshi_ws']['stats']['unparsed']}, "
           f"delta_without_book {res['kalshi_ws']['stats']['delta_without_book']} (expected > 0 only at replay start)"]
    if res["windows"]:
        g = sorted(w["mid_gap_abs_c_p50"] for w in res["windows"])
        out.append(f"pairing check: median |PM - Kalshi mid| per window p50 {g[len(g) // 2]:.2f}c, max {g[-1]:.2f}c; "
                   f"max Kalshi crossed share {max(w['kalshi_crossed_share'] for w in res['windows'])}")
    out.append(f"lead-lag: verdict {ll['verdict_1s']}; net-follow diff (K->P minus P->K) "
               + ", ".join(f"{h} {v['est']} {v['ci']}" for h, v in ll["net_follow_diff_k_minus_p"].items()))
    out.append(f"  K->P events {ll['kalshi_to_pmus']['events']} median delay {ll['kalshi_to_pmus']['median_delay_s']} s; "
               f"P->K events {ll['pmus_to_kalshi']['events']} median delay {ll['pmus_to_kalshi']['median_delay_s']} s; "
               f"xcorr peak lag {ll['peak_abs_corr_lag_s']} s, lead_score_1s {ll['lead_score_1s']}")
    out.append("S5, one fill per hedge horizon (side m L: fills known miss [ci] slip|miss expectancy [ci] c/pair | raw fills miss exp):")
    for r in res["s5"]:
        out.append(f"  {r['side']:3} {r['m_c']}c {r['L_ms']:4}ms: {r['fills']:5} {r['known']:5} "
                   f"{r['miss_rate']} {r['miss_rate_ci']} {r['slip_given_miss_c']} {r['expectancy_c']} {r['expectancy_ci_c']} | {r['raw']['fills']} {r['raw']['miss_rate']} {r['raw']['expectancy_c']}")
    out.append("S6 (print side, +s: n rel_mean_c [ci] p10/p50/p90):")
    for r in res["s6"]:
        q = r["rel_q_c"] or {}
        out.append(f"  {r['print_side']:4} +{r['delta_s']:g}s: {r['n']:6} {r['rel_mean_c']} {r['rel_mean_ci_c']} "
                   f"{q.get('p10')}/{q.get('p50')}/{q.get('p90')}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-bridge", description=__doc__.split("\n")[0] if __doc__ else None)
    ap.add_argument("--data", type=Path, required=True, help="Python tape root (pmus_ws_15m)")
    ap.add_argument("--kalshi-data", type=Path, help="Rust tape root (kalshi_ws); default --data")
    ap.add_argument("--from", dest="t0", required=True, help="unix s or ISO (UTC if no offset)")
    ap.add_argument("--to", dest="t1", required=True)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--size", type=float, default=Cfg.size)
    ap.add_argument("--requote-ms", type=float, default=Cfg.requote_s * 1000)
    ap.add_argument("--pmus-coef", type=float, default=Cfg.pmus_coef)
    ap.add_argument("--pmus-rebate", type=float, default=Cfg.pmus_rebate)
    ap.add_argument("--kalshi-maker-coef", type=float, default=Cfg.kalshi_maker_coef)
    ap.add_argument("--fill", choices=("through", "touch"), default=Cfg.fill)
    a = ap.parse_args(argv)
    t0, t1 = _when(a.t0), _when(a.t1)
    cfg = Cfg(size=a.size, requote_s=a.requote_ms / 1000, pmus_coef=a.pmus_coef, pmus_rebate=a.pmus_rebate,
              kalshi_maker_coef=a.kalshi_maker_coef, fill=a.fill)
    res = run(a.data, a.kalshi_data or a.data, t0, t1, cfg)
    res["at"] = int(time.time())
    out = a.out or Path(f"bridge-btc15m-{t0}-{t1}.json")
    out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS))
    print("\n".join(summary_lines(res)))
    print("->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
