"""W3: the intl informed-flow guard.

Polymarket intl publishes every fill with its wallet and role about 2 s after it prints (RTDS
``activity/trades``, tape streams ``rtds_a``/``rtds_b``). The same BTC/ETH/SOL/XRP 5m/15m windows trade
on Polymarket US on the same start-time grid. If informed intl wallets are buying Up in a window, a
PM US maker should pull or shade before that information reaches the PM US book. This module gives
(a) point-in-time wallet informedness scores (W10 ML loop 5), (b) per-second intl flow features for
any window and (c) ``flow_predictiveness``, a MEASUREMENT of whether (b) leads PM US. (c) is not a
strategy verdict.

Clocks and look-ahead (read before trusting any number)
-------------------------------------------------------
* Features at decision second d use only rows RECEIVED by us (record ``tw``, ns) at or before d:
  ``tw <= d * 1e9``. The venue ``timestamp`` never decides inclusion. A fill that printed before d
  but reached us after d does not exist at d.
* RTDS rows are deduplicated across the two sockets and keep their EARLIEST receive time. A key
  (tx, wallet, token, side, size, price) seen m times on one socket is kept max-over-sockets m
  times, so genuine same-size fills inside one transaction survive, as in the wallet activity scan.
* Informedness as of T uses only windows that ENDED before ``T - EMBARGO_S``, and inside each such
  window only taker rows received by ``end + EMBARGO_S`` (< T). Outcomes come from ``TokenMap``
  payouts. An Up/Down outcome is fixed at window end; the embargo covers resolution latency.
* intl mid: the Up token's CLOB best bid/ask received STRICTLY before d and at most 3 s old
  (``dossier.BookIndex.quote``), else null.

Definitions (cents per share unless noted)
------------------------------------------
dir = +1 BUY, -1 SELL of the traded token. udir = +1 for Up exposure (BUY Up, SELL Down), else -1.
Taker row: ``fee > 0``. USD of a row = price * size.

Cell = one wallet's taker rows in one window (both tokens). Cell settlement markout:
    m = 100 * sum(size * dir * (payout_token - price)) / sum(size)
mu(T) = equal-weight mean of m over all cells released as of T.
score(T) = (sum of the wallet's released m + K_PRIOR * mu) / (n + K_PRIOR), n = released cells.
The unit is the wallet x window CELL, not the row: rows in one window share one payout and order
slicing multiplies rows, so a per-row n would overstate evidence and favour slicers.
excess = score - mu. Feature weight = clip(excess / W_SCALE_C, -1, 1); a wallet with no released
cell weighs 0 (its best estimate IS mu). Informed wallet: excess >= INFORMED_EXCESS_C.
Optional short-horizon label (needs the intl CLOB tape or a BookIndex): per row
m30 = 100 * dir * (mid_token(ts + 30 s) - price), same cell and shrinkage scheme with
K_PRIOR_MID30; ``coverage`` reports how many taker rows got a label.

Features at d for window (asset, hz, start), N in LOOKBACKS, taker rows with tw in (d - N, d]:
  intl_flow_up_usd_{N}s     = sum udir * price * size                     (USD)
  intl_informed_flow_{N}s   = sum udir * price * size * weight(wallet)    (USD, informed-equivalent)
  intl_informed_share_{N}s  = sum_{informed wallets} price*size / sum price*size (0-1, null if none)
  intl_mid_up_c             = 100 * (bid + ask) / 2 of the intl Up token as of d (null if > 3 s old)

Predictiveness: one observation per window per tau (tau = fraction of the window length), at
d = start + tau, never pooled across seconds. Targets: ``settle`` = sign / value of A(end-1) - A(d)
on the PM US published settlement series (``refprice``; A(end-1) is the settlement statistic) and
``pm10`` = PM US YES mid(d + 10 s) - mid(d) in cents (``pmus_ws_{hz}``, else ``pmus_books``; as-of,
<= 3 s old). AUC drops zero targets; Spearman keeps them. Placebo: the SAME feature taken at d - 60
(stale by 60 s), paired with the targets at d. Windows are split in time order (first SPLIT_FRAC of
window starts vs the rest); nothing is fitted. Scores used for a window are as of its start.
"""

from __future__ import annotations

import argparse
import asyncio
import bisect
import calendar
import hashlib
import math
import sys
import time
from array import array
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import orjson

from ..tape.segment import read_segment, segment_path
from ..tape.universe import pmus_slug
from .dossier import NS, BookIndex, build_index_files
from .dossier import STREAMS as CLOB_STREAMS
from .ledger import TokenMap
from .refprice import load_series
from .scan import LEN, WINDOW

ASSETS = ("btc", "eth", "sol", "xrp")
HZS = ("5m", "15m")
RTDS_STREAMS = ("rtds_a", "rtds_b")
PMUS_STREAMS = ("pmus_ws_5m", "pmus_ws_15m", "pmus_books")
HOUR = 3600

# ---------------------------------------------------------------- constants (one place)
EMBARGO_S = 60            # a window's outcome labels scores only from end + 60 s on
K_PRIOR = 100.0           # EB prior strength, in wallet x window cells
K_PRIOR_MID30 = 20.0      # same for the +30 s mid markout (far less noisy than settlement)
MID30_S = 30
W_SCALE_C = 10.0          # weight = clip(excess / W_SCALE_C, -1, 1)
INFORMED_EXCESS_C = 5.0   # informed wallet: score - mu >= this
LOOKBACKS = (5, 30, 120)  # feature windows, seconds of receive time
MID_MAX_AGE_S = 3.0
PLACEBO_SHIFT_S = 60
PM_HORIZON_S = 10
TAU_FRACS = (0.1, 0.25, 0.5, 0.75, 0.9)
SPLIT_FRAC = 0.5
K_MOM_MIN_N = 5           # wallets with >= this many cells enter the k method-of-moments check
UNIT = "wallet x window cell; m = 100*sum(size*dir*(payout-price))/sum(size), cents/share"

FEATURE_NAMES = tuple(f"intl_{k}_{n}s" for k in ("flow_up_usd", "informed_flow", "informed_share")
                      for n in LOOKBACKS) + ("intl_mid_up_c",)


# ---------------------------------------------------------------- RTDS rows
@dataclass(slots=True)
class Fill:
    tw: int       # our earliest receive time across sockets, ns
    ts: int       # venue trade timestamp, s
    wallet: str   # lower-case proxy wallet
    slug: str     # intl event slug
    coin: str
    hz: str
    start: int
    token: str
    idx: int      # outcomeIndex: 0 = Up, 1 = Down
    side: str     # BUY / SELL
    price: float
    size: float
    fee: float

    @property
    def end(self) -> int:
        return self.start + LEN[self.hz]

    @property
    def taker(self) -> bool:
        return self.fee > 0

    @property
    def dir(self) -> int:
        return 1 if self.side == "BUY" else -1

    @property
    def udir(self) -> int:
        return self.dir if self.idx == 0 else -self.dir


def parse_rtds(raw: str, tw: int) -> tuple[tuple, Fill] | None:
    """(dedup key, Fill) for an RTDS record in a crypto window, else None."""
    if not raw or raw[0] != "{" or "-updown-" not in raw:
        return None
    try:
        obj = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return None
    p = obj.get("payload") if isinstance(obj, dict) else None
    if not isinstance(p, dict):
        return None
    m = WINDOW.match(str(p.get("eventSlug") or ""))
    if not m:
        return None
    try:
        side = str(p["side"]).upper()
        idx = int(p["outcomeIndex"])
        if side not in ("BUY", "SELL") or idx not in (0, 1):
            return None
        f = Fill(tw=int(tw), ts=int(p.get("timestamp") or 0), wallet=str(p["proxyWallet"]).lower(),
                 slug=m.group(0), coin=m.group(1), hz=m.group(2), start=int(m.group(3)),
                 token=str(p["asset"]), idx=idx, side=side, price=float(p["price"]),
                 size=float(p["size"]), fee=float(p.get("fee") or 0.0))
        key = (str(p["transactionHash"]).lower(), f.wallet, f.token, side, p["size"], p["price"])
    except (KeyError, TypeError, ValueError):
        return None
    return key, f


def hours_between(lo_s: int, hi_s: int) -> list[int]:
    """Hour starts (unix s) of the segment hours covering [lo_s, hi_s]."""
    return list(range(lo_s // HOUR * HOUR, hi_s + 1, HOUR))


def tape_hours(root: Path, streams: Iterable[str]) -> list[int]:
    """Hour starts that have a segment in any of ``streams``."""
    hs: set[int] = set()
    for s in streams:
        for f in (Path(root) / s).glob("*/*.dtape.zst"):
            try:
                hs.add(calendar.timegm(time.strptime(f.parent.name + f.name[:2], "%Y%m%d%H")))
            except ValueError:
                continue
    return sorted(hs)


def iter_rtds(root: Path, hours: Iterable[int],
              streams: tuple[str, ...] = RTDS_STREAMS) -> Iterator[tuple[int, list[Fill]]]:
    """(hour, deduplicated crypto-window fills received in that hour, sorted by tw), one segment
    hour at a time. Dedup state spans the previous consecutive hour, so a fill whose two receipts
    straddle an hour edge is emitted once."""
    prev_h: int | None = None
    prev_seen: dict[str, Counter] = {}
    prev_emit: Counter = Counter()
    for h in hours:
        if prev_h is None or h != prev_h + HOUR:
            prev_seen, prev_emit = {}, Counter()
        recs: list[tuple[int, str, tuple, Fill]] = []
        for s in streams:
            f = segment_path(Path(root), s, h * NS)
            if not f.exists():
                continue
            for rec in read_segment(f, verify=True):
                r = parse_rtds(rec.get("raw") or "", rec["tw"])
                if r is not None:
                    recs.append((r[1].tw, s, r[0], r[1]))
        recs.sort(key=lambda x: x[0])
        seen: dict[str, Counter] = {s: Counter() for s in streams}
        emit: Counter = Counter()
        out: list[Fill] = []
        for _, s, key, fl in recs:
            seen[s][key] += 1
            have = seen[s][key] + prev_seen.get(s, Counter())[key]
            if have > emit[key] + prev_emit[key]:
                emit[key] += 1
                out.append(fl)
        prev_h, prev_seen, prev_emit = h, seen, emit
        yield h, out


def window_outcomes(tmap: TokenMap) -> dict[str, float]:
    """intl window slug -> Up payout (0..1) from resolved TokenMap entries."""
    out: dict[str, float] = {}
    for info in tmap.m.values():
        if info.get("status") != "resolved" or info.get("payout") is None:
            continue
        slug = str(info.get("slug") or "")
        if not WINDOW.match(slug) or slug in out:
            continue
        p = float(info["payout"])
        out[slug] = p if info.get("idx") == 0 else 1.0 - p
    return out


# ---------------------------------------------------------------- intl book quotes
class Quotes(Protocol):
    def quote(self, asset: str, t_ns: int, max_age_s: float = ...) -> tuple[float, float] | None: ...


class HourlyBooks:
    """As-of intl CLOB quotes read from the tape one segment hour at a time (a few hours cached)."""

    def __init__(self, root: Path, streams: Iterable[str] = CLOB_STREAMS, keep: int = 3):
        self.root, self.streams, self.keep = Path(root), tuple(streams), keep
        self._c: dict[int, BookIndex] = {}
        self.loaded = 0

    def _get(self, h: int) -> BookIndex:
        b = self._c.get(h)
        if b is None:
            b = build_index_files([(s, segment_path(self.root, s, h * NS)) for s in self.streams])
            self._c[h] = b
            self.loaded += 1
            while len(self._c) > self.keep:
                del self._c[min(self._c)]
        return b

    def quote(self, asset: str, t_ns: int, max_age_s: float = MID_MAX_AGE_S):
        h = t_ns // NS // HOUR * HOUR
        q = self._get(h).quote(asset, t_ns, max_age_s)
        if q is None and t_ns - h * NS <= max_age_s * NS:  # last quote may sit in the previous hour
            q = self._get(h - HOUR).quote(asset, t_ns, max_age_s)
        return q


def _mid_c(q: tuple[float, float] | None) -> float | None:
    if q is None:
        return None
    b, a = q
    if not (math.isfinite(b) and math.isfinite(a)):
        return None
    return 100.0 * (b + a) / 2.0


# ---------------------------------------------------------------- point-in-time scores
class _Log:
    """Released cells in end order with prefix sums (index j = first j cells)."""

    __slots__ = ("ends", "cm", "cm2", "cn", "cu", "e30", "c30")

    def __init__(self) -> None:
        self.ends = array("q")
        self.cm = array("d", [0.0])
        self.cm2 = array("d", [0.0])
        self.cn = array("q", [0])
        self.cu = array("d", [0.0])
        self.e30 = array("q")
        self.c30 = array("d", [0.0])


class Scorer:
    """Online, point-in-time wallet informedness (W10 ML loop 5).

    ``park`` adds taker rows to their (window, wallet) cell; ``finalize_until(t)`` releases windows
    with ``end + EMBARGO_S < t`` in end order. ``score(w, T)``/``view(T)`` see only cells of windows
    with ``end < T - EMBARGO_S``, so any T is answerable once the tape is read past it."""

    def __init__(self, outcomes: Mapping[str, float], k: float = K_PRIOR,
                 k_mid30: float = K_PRIOR_MID30, books: Quotes | None = None):
        self.outcomes = outcomes
        self.k, self.k30 = float(k), float(k_mid30)
        self.books = books
        self._pend: dict[str, dict[str, list[float]]] = {}
        self._pend_end: dict[str, int] = {}
        self._w: dict[str, _Log] = {}
        self._g = _Log()
        self.last_end = -1
        self.released_until = -1
        self.counts: Counter = Counter()

    # -- building
    def park(self, f: Fill) -> None:
        if not f.taker:
            return
        if f.tw > (f.end + EMBARGO_S) * NS:
            self.counts["rows_after_embargo"] += 1
            return
        if f.end + EMBARGO_S < self.released_until and f.slug not in self._pend:
            self.counts["rows_after_release"] += 1
            return
        cells = self._pend.setdefault(f.slug, {})
        self._pend_end[f.slug] = f.end
        c = cells.get(f.wallet)
        if c is None:
            c = cells[f.wallet] = [0.0] * 9
        sd = f.size * f.dir
        c[0] += f.size                      # shares
        c[1 if f.idx == 0 else 2] += sd     # signed shares of Up / of Down
        c[3] += sd * f.price                # signed cost
        c[4] += 1                           # rows
        c[5] += f.price * f.size            # notional USD
        self.counts["taker_rows"] += 1
        if self.books is not None:
            mid = _mid_c(self.books.quote(f.token, (f.ts + MID30_S) * NS, MID_MAX_AGE_S))
            if mid is not None:
                c[6] += sd * (mid / 100.0 - f.price)
                c[7] += f.size
                c[8] += 1
                self.counts["mid30_labeled_rows"] += 1

    def finalize_until(self, t_s: int) -> list[str]:
        """Release, in end order, every pending window with end + EMBARGO_S < t_s."""
        due = sorted((s for s, e in self._pend_end.items() if e + EMBARGO_S < t_s),
                     key=lambda s: (self._pend_end[s], s))
        for slug in due:
            self._release(slug)
        self.released_until = max(self.released_until, t_s)
        return due

    def _release(self, slug: str) -> None:
        end = self._pend_end.pop(slug)
        cells = self._pend.pop(slug)
        if end < self.last_end:
            raise RuntimeError(f"windows must be released in end order ({slug})")
        self.last_end = end
        up = self.outcomes.get(slug)
        if up is None:
            self.counts["unresolved_windows"] += 1
            self.counts["unresolved_rows"] += sum(int(c[4]) for c in cells.values())
            return
        self.counts["windows"] += 1
        g = self._g
        for w in sorted(cells):
            c = cells[w]
            if c[0] <= 0:
                continue
            m = 100.0 * (up * c[1] + (1.0 - up) * c[2] - c[3]) / c[0]
            lg = self._w.get(w)
            if lg is None:
                lg = self._w[w] = _Log()
            for x in (lg, g):
                x.ends.append(end)
                x.cm.append(x.cm[-1] + m)
                x.cm2.append(x.cm2[-1] + m * m)
            lg.cn.append(lg.cn[-1] + int(c[4]))
            lg.cu.append(lg.cu[-1] + c[5])
            if c[7] > 0:
                m30 = 100.0 * c[6] / c[7]
                for x in (lg, g):
                    x.e30.append(end)
                    x.c30.append(x.c30[-1] + m30)

    # -- reading
    def mu(self, as_of: int) -> tuple[float | None, int]:
        """(population mean cell markout, number of cells) as of ``as_of``."""
        n = bisect.bisect_left(self._g.ends, as_of - EMBARGO_S)
        return (self._g.cm[n] / n if n else None), n

    def score(self, wallet: str, as_of: int,
              _mu: tuple[float | None, int] | None = None) -> dict | None:
        lg = self._w.get(wallet)
        if lg is None:
            return None
        cut = as_of - EMBARGO_S
        j = bisect.bisect_left(lg.ends, cut)
        if j == 0:
            return None
        mu = (_mu or self.mu(as_of))[0]
        assert mu is not None  # the wallet's own cells are in the population
        s = (lg.cm[j] + self.k * mu) / (j + self.k)
        rec = {"score": s, "excess": s - mu, "n": j, "n_trades": int(lg.cn[j]),
               "notional": lg.cu[j], "raw": lg.cm[j] / j}
        j30 = bisect.bisect_left(lg.e30, cut)
        if j30:
            n30 = bisect.bisect_left(self._g.e30, cut)
            mu30 = self._g.c30[n30] / n30
            s30 = (lg.c30[j30] + self.k30 * mu30) / (j30 + self.k30)
            rec.update(score_mid30=s30, excess_mid30=s30 - mu30, n_mid30=j30)
        return rec

    def view(self, as_of: int) -> ScoreView:
        return ScoreView(self, as_of)

    def wallets(self) -> list[str]:
        return list(self._w)

    def k_mom(self, as_of: int) -> float | None:
        """Method-of-moments prior strength sigma^2_within / tau^2_between (diagnostic only)."""
        cut = as_of - EMBARGO_S
        ss = dof = 0.0
        means, inv_n = [], []
        for lg in self._w.values():
            j = bisect.bisect_left(lg.ends, cut)
            if j >= 2:
                ss += lg.cm2[j] - lg.cm[j] ** 2 / j
                dof += j - 1
            if j >= K_MOM_MIN_N:
                means.append(lg.cm[j] / j)
                inv_n.append(1.0 / j)
        if dof <= 0 or len(means) < 3:
            return None
        sig2 = ss / dof
        tau2 = float(np.var(means, ddof=1)) - sig2 * float(np.mean(inv_n))
        return sig2 / tau2 if tau2 > 0 else None

    def snapshot(self, as_of: int) -> dict:
        v = self.view(as_of)
        mu, ncells = v.mu_n
        wallets = {w: {k: _r(x) for k, x in v[w].items()} for w in sorted(v)}
        informed = sum(1 for r in wallets.values() if (r["excess"] or 0) >= INFORMED_EXCESS_C)
        n30 = bisect.bisect_left(self._g.e30, as_of - EMBARGO_S)
        return {"as_of": as_of, "unit": UNIT, "embargo_s": EMBARGO_S, "k_prior": self.k,
                "k_prior_mid30": self.k30, "w_scale_c": W_SCALE_C,
                "informed_excess_c": INFORMED_EXCESS_C, "mu_c": _r(mu), "cells": ncells,
                "mu_mid30_c": _r(self._g.c30[n30] / n30) if n30 else None, "cells_mid30": n30,
                "k_mom": _r(self.k_mom(as_of), 2), "n_wallets": len(wallets),
                "n_informed": informed, "counts": dict(self.counts), "wallets": wallets}


class ScoreView(Mapping):
    """Lazy wallet -> score record as of one instant (only queried wallets are computed)."""

    def __init__(self, sc: Scorer, as_of: int):
        self._sc, self.as_of = sc, as_of
        self.mu_n = sc.mu(as_of)
        self._cache: dict[str, dict | None] = {}

    def __getitem__(self, key: str) -> dict:
        if key not in self._cache:
            self._cache[key] = self._sc.score(key, self.as_of, self.mu_n)
        r = self._cache[key]
        if r is None:
            raise KeyError(key)
        return r

    def __iter__(self) -> Iterator[str]:
        return (w for w in self._sc.wallets() if self.get(w) is not None)

    def __len__(self) -> int:
        return sum(1 for _ in self)


def _r(x, n: int = 4):
    if x is None:
        return None
    if isinstance(x, (int, np.integer)) and not isinstance(x, bool):
        return int(x)
    x = float(x)
    return round(x, n) if math.isfinite(x) else None


def informedness_doc(root: Path, tmap: TokenMap, as_of: int, k: float = K_PRIOR,
                     idx: Quotes | None = None, mid30: bool = False) -> dict:
    """Snapshot document (header + wallets) of informedness as of ``as_of`` (unix s).
    ``idx``: a quote source for the +30 s mid label; ``mid30=True`` reads the intl CLOB tape hourly."""
    root = Path(root)
    books: Quotes | None = idx if idx is not None else (HourlyBooks(root) if mid30 else None)
    sc = Scorer(window_outcomes(tmap), k=k, books=books)
    span: list[int | None] = [None, None]
    for h, fills in iter_rtds(root, [h for h in tape_hours(root, RTDS_STREAMS) if h <= as_of]):
        for f in fills:
            if f.tw <= as_of * NS:  # belt and braces: nothing received after as_of
                sc.park(f)
                span[0] = f.tw if span[0] is None else span[0]
                span[1] = f.tw
        sc.finalize_until(min(h + HOUR, as_of))
    sc.finalize_until(as_of)  # every hour <= as_of is read, even if as_of's own hour has no segment
    doc = sc.snapshot(as_of)
    rows, lab = sc.counts["taker_rows"], sc.counts["mid30_labeled_rows"]
    doc["mid30"] = {"source": None if books is None else type(books).__name__,
                    "coverage": round(lab / rows, 4) if books is not None and rows else 0.0}
    doc["input_tw_span"] = span
    return doc


def informedness(root: Path, tmap: TokenMap, as_of: int, k: float = K_PRIOR,
                 idx: Quotes | None = None, mid30: bool = False) -> dict[str, dict]:
    """wallet -> {score, excess, n (cells), n_trades, notional, raw[, *_mid30]} as of ``as_of``."""
    return informedness_doc(root, tmap, as_of, k, idx, mid30)["wallets"]


def snapshot_path(lab: Path, as_of: int) -> Path:
    return Path(lab) / "flow" / f"informedness-{time.strftime('%Y%m%d', time.gmtime(as_of))}.json"


def write_snapshot(lab: Path, doc: dict) -> Path:
    """Persist a daily snapshot; refuses to overwrite (FileExistsError)."""
    p = snapshot_path(lab, int(doc["as_of"]))
    p.parent.mkdir(parents=True, exist_ok=True)
    body = {**doc, "built_tw": time.time_ns()}
    body["sha256"] = hashlib.sha256(orjson.dumps({k: v for k, v in body.items() if k != "built_tw"},
                                                 option=orjson.OPT_SORT_KEYS)).hexdigest()
    with open(p, "xb") as fh:
        fh.write(orjson.dumps(body, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
    return p


# ---------------------------------------------------------------- per-second features
def weight(rec: Mapping | None) -> float:
    """Informedness weight in [-1, 1]; 0 for a wallet without a released cell."""
    if not rec:
        return 0.0
    return max(-1.0, min(1.0, float(rec["excess"]) / W_SCALE_C))


def is_informed(rec: Mapping | None) -> bool:
    return rec is not None and len(rec) > 0 and float(rec["excess"]) >= INFORMED_EXCESS_C


def features_from_fills(fills: Iterable[Fill], start: int, end: int, scores: Mapping,
                        idx: Quotes | None = None, up_token: str | None = None,
                        seconds: Iterable[int] | None = None) -> dict[int, dict]:
    """Per-second features for one window from its fills (any order; maker rows ignored except to
    find the Up token). ``seconds`` defaults to start..end-1."""
    fills = list(fills)
    if up_token is None:
        up_token = next((f.token for f in fills if f.idx == 0), None)
    tk = sorted((f for f in fills if f.taker), key=lambda f: f.tw)
    tw = np.array([f.tw for f in tk], dtype=np.int64)
    gross = np.array([f.price * f.size for f in tk], dtype=float)
    signed = gross * np.array([f.udir for f in tk], dtype=float)
    recs = {w: scores.get(w) for w in {f.wallet for f in tk}}
    wt = np.array([weight(recs[f.wallet]) for f in tk], dtype=float)
    inf = np.array([is_informed(recs[f.wallet]) for f in tk], dtype=float)

    def cs(a: np.ndarray) -> np.ndarray:
        return np.concatenate(([0.0], np.cumsum(a)))

    c_flow, c_inf, c_g, c_gi = cs(signed), cs(signed * wt), cs(gross), cs(gross * inf)
    ds = np.arange(start, end, dtype=np.int64) if seconds is None else \
        np.asarray(list(seconds), dtype=np.int64)
    hi = np.searchsorted(tw, ds * NS, side="right")
    cols: dict[str, np.ndarray] = {}
    for n in LOOKBACKS:
        lo = np.searchsorted(tw, (ds - n) * NS, side="right")
        cols[f"intl_flow_up_usd_{n}s"] = c_flow[hi] - c_flow[lo]
        cols[f"intl_informed_flow_{n}s"] = c_inf[hi] - c_inf[lo]
        g = c_g[hi] - c_g[lo]
        has = ((hi - lo) > 0) & (g > 0)
        cols[f"intl_informed_share_{n}s"] = np.where(has, (c_gi[hi] - c_gi[lo]) / np.where(has, g, 1.0),
                                                     np.nan)
    out: dict[int, dict] = {}
    for i, d in enumerate(ds.tolist()):
        row: dict = {k: _r(v[i]) for k, v in cols.items()}
        row["intl_mid_up_c"] = (_r(_mid_c(idx.quote(up_token, d * NS, MID_MAX_AGE_S)), 3)
                                if idx is not None and up_token else None)
        out[d] = row
    return out


def window_flow_features(root: Path, asset: str, hz: str, start: int, scores: Mapping,
                         idx: Quotes | None = None, up_token: str | None = None,
                         book: bool = True) -> dict[int, dict]:
    """Features for seconds start..end-1 of one intl window, reading only the tape hours it needs.
    Without ``idx`` (and with ``book``) the intl mid comes from ``clob_{asset}_{hz}`` segments."""
    root = Path(root)
    end = start + LEN[hz]
    slug = f"{asset}-updown-{hz}-{start}"
    hours = hours_between(start - max(LOOKBACKS) - 300, end)
    fills = [f for _, fs in iter_rtds(root, hours) for f in fs if f.slug == slug]
    if idx is None and book:
        s = f"clob_{asset}_{hz}"
        idx = build_index_files([(s, segment_path(root, s, h * NS))
                                 for h in hours_between(start - 10, end)])
    return features_from_fills(fills, start, end, scores, idx, up_token)


# ---------------------------------------------------------------- PM US side
def _pmus_mid(md: dict) -> float:
    """YES mid in cents from a PM US marketData book; NaN when a side is empty."""
    try:
        bids = [float(lv["px"]["value"]) for lv in md.get("bids") or [] if float(lv.get("qty") or 0) > 0]
        offers = [float(lv["px"]["value"]) for lv in md.get("offers") or []
                  if float(lv.get("qty") or 0) > 0]
    except (KeyError, TypeError, ValueError):
        return math.nan
    if not bids or not offers:
        return math.nan
    return 100.0 * (max(bids) + min(offers)) / 2.0


def load_pmus_mids(root: Path, slugs: set[str], lo_s: int, hi_s: int
                   ) -> tuple[dict[str, tuple[np.ndarray, np.ndarray]], Counter]:
    """PM US slug -> (receive tw ns, YES mid cents) sorted, from ``pmus_ws_*`` when the slug has
    WS books, else from ``pmus_books`` (1 s REST). Read one segment hour at a time."""
    src: dict[str, dict[str, tuple[array, array]]] = {"ws": {}, "rest": {}}
    for stream in PMUS_STREAMS:
        kind = "rest" if stream == "pmus_books" else "ws"
        for h in hours_between(lo_s, hi_s):
            f = segment_path(Path(root), stream, h * NS)
            if not f.exists():
                continue
            for rec in read_segment(f, verify=True):
                raw = rec.get("raw") or ""
                if '"marketData"' not in raw:
                    continue
                if kind == "rest" and (rec.get("meta") or {}).get("status") != 200:
                    continue
                try:
                    obj = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    continue
                md = obj.get("marketData") if isinstance(obj, dict) else None
                if not isinstance(md, dict) or md.get("marketSlug") not in slugs:
                    continue
                a = src[kind].setdefault(md["marketSlug"], (array("q"), array("d")))
                a[0].append(int(rec["tw"]))
                a[1].append(_pmus_mid(md))
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    used: Counter = Counter()
    for slug in slugs:
        kind = "ws" if slug in src["ws"] else "rest" if slug in src["rest"] else None
        if kind is None:
            continue
        t = np.frombuffer(src[kind][slug][0], dtype=np.int64)
        v = np.frombuffer(src[kind][slug][1], dtype=np.float64)
        o = np.argsort(t, kind="stable")
        out[slug] = (t[o].copy(), v[o].copy())
        used[kind] += 1
    return out, used


def asof_mid(book: tuple[np.ndarray, np.ndarray], t_s: int,
             max_age_s: float = MID_MAX_AGE_S) -> float | None:
    """Latest mid received strictly before t_s, at most max_age_s old (else None)."""
    tw, v = book
    t = t_s * NS
    i = int(np.searchsorted(tw, t, side="left")) - 1
    if i < 0 or t - int(tw[i]) > max_age_s * NS or not math.isfinite(v[i]):
        return None
    return float(v[i])


# ---------------------------------------------------------------- metrics (numpy only)
def _ranks(a: np.ndarray) -> np.ndarray:
    _, inv, cnt = np.unique(a, return_inverse=True, return_counts=True)
    cum = np.cumsum(cnt)
    return ((cum - cnt + 1 + cum) / 2.0)[inv]


def auc(x: np.ndarray, y: np.ndarray) -> tuple[float, float, int, int] | None:
    """(AUC, Hanley-McNeil SE, n_pos, n_neg) of score x for y > 0 vs y < 0 (y == 0 dropped).
    None when a class is empty or x is constant."""
    m = y != 0
    x, pos = x[m], y[m] > 0
    n1, n0 = int(pos.sum()), int((~pos).sum())
    if n1 == 0 or n0 == 0 or np.all(x == x[0]):
        return None
    a = float((_ranks(x)[pos].sum() - n1 * (n1 + 1) / 2.0) / (n1 * n0))
    q1, q2 = a / (2 - a), 2 * a * a / (1 + a)
    se = math.sqrt(max(0.0, a * (1 - a) + (n1 - 1) * (q1 - a * a) + (n0 - 1) * (q2 - a * a)) / (n1 * n0))
    return a, se, n1, n0


def spearman(x: np.ndarray, y: np.ndarray) -> float | None:
    if len(x) < 3 or np.all(x == x[0]) or np.all(y == y[0]):
        return None
    return float(np.corrcoef(_ranks(x), _ranks(y))[0, 1])


def _cell(x: np.ndarray, y: np.ndarray) -> dict:
    a = auc(x, y)
    act = x != 0
    aa = auc(x[act], y[act]) if act.any() else None
    return {"n": int(len(x)), "n_active": int(act.sum()),
            "auc": _r(a[0]) if a else None, "auc_se": _r(a[1]) if a else None,
            "n_pos": a[2] if a else None, "n_neg": a[3] if a else None,
            "auc_active": _r(aa[0]) if aa else None, "spearman": _r(spearman(x, y))}


# ---------------------------------------------------------------- predictiveness
def flow_predictiveness(root: Path, tmap: TokenMap, t_from: int, t_to: int,
                        assets: Iterable[str] = ASSETS, hzs: Iterable[str] = HZS,
                        k: float = K_PRIOR,
                        features: Iterable[str] = ("intl_informed_flow_30s", "intl_flow_up_usd_30s"),
                        tau_fracs: Iterable[float] = TAU_FRACS, split_frac: float = SPLIT_FRAC,
                        placebo_shift_s: int = PLACEBO_SHIFT_S) -> dict:
    """Does intl flow at d lead (a) the PM US settlement move after d and (b) the PM US mid over the
    next 10 s? Windows with start in [t_from, t_to). Scores for each window are as of its start and
    use the whole tape before it. One pass over RTDS, one segment hour at a time."""
    root = Path(root)
    assets, hzs, features, tau_fracs = tuple(assets), tuple(hzs), tuple(features), tuple(tau_fracs)
    grid: dict[str, tuple[str, str, int, int]] = {}
    for hz in hzs:
        L = LEN[hz]
        for st in range(-(-t_from // L) * L, t_to, L):
            for a in assets:
                grid[f"{a}-updown-{hz}-{st}"] = (a, hz, st, st + L)
    longest = max((LEN[h] for h in hzs), default=0)
    series = {a: load_series(root, f"crypto:{a}", t_from - 1, t_to + longest) for a in assets}
    pm, pm_src = load_pmus_mids(root, {pmus_slug(a, hz, st) for a, hz, st, _ in grid.values()},
                                t_from - 10, t_to + longest + PM_HORIZON_S + 10)
    sc = Scorer(window_outcomes(tmap), k=k)
    keep: dict[str, list[Fill]] = defaultdict(list)
    todo = sorted(grid, key=lambda s: (grid[s][3], s))
    nxt = 0
    samples: list[dict] = []

    def evaluate(slug: str) -> None:
        a, hz, st, end = grid[slug]
        feats = features_from_fills(keep.pop(slug, []), st, end, sc.view(st), None, None,
                                    seconds=range(st - placebo_shift_s, end))
        ser = series[a]
        a_end = ser.get(end - 1)
        book = pm.get(pmus_slug(a, hz, st))
        for fr in tau_fracs:
            d = min(end - 1, st + int(round(fr * (end - st))))
            settle = a_end - ser[d] if a_end is not None and d in ser else None
            pm10 = None
            if book is not None:
                m0, m1 = asof_mid(book, d), asof_mid(book, d + PM_HORIZON_S)
                pm10 = m1 - m0 if m0 is not None and m1 is not None else None
            samples.append({"start": st, "frac": fr, "settle": settle, "pm10": pm10,
                            "x": {f: feats[d][f] for f in features},
                            "xp": {f: feats[d - placebo_shift_s][f] for f in features}})

    hi_s = t_to + longest + EMBARGO_S + 1
    for h, fills in iter_rtds(root, [h for h in tape_hours(root, RTDS_STREAMS) if h <= hi_s]):
        for f in fills:
            sc.park(f)
            if f.taker and f.slug in grid and f.tw <= f.end * NS:
                keep[f.slug].append(f)
        sc.finalize_until(h + HOUR)
        while nxt < len(todo) and grid[todo[nxt]][3] + EMBARGO_S < h + HOUR:
            evaluate(todo[nxt])
            nxt += 1
    sc.finalize_until(2**62)
    for slug in todo[nxt:]:
        evaluate(slug)

    starts = sorted({s["start"] for s in samples})
    cut = starts[min(len(starts) - 1, int(len(starts) * split_frac))] if starts else None
    splits = {"all": lambda s: True, "early": lambda s: s["start"] < cut,
              "late": lambda s: s["start"] >= cut}
    res: dict = {}
    for f in features:
        for tgt in ("settle", "pm10"):
            for var in ("feature", "placebo"):
                xk = "x" if var == "feature" else "xp"
                for sp, keep_s in splits.items():
                    for fr in tau_fracs:
                        rows = [s for s in samples if s["frac"] == fr and keep_s(s)
                                and s[tgt] is not None and s[xk][f] is not None]
                        x = np.array([s[xk][f] for s in rows], dtype=float)
                        y = np.array([s[tgt] for s in rows], dtype=float)
                        res.setdefault(f, {}).setdefault(tgt, {}).setdefault(var, {}).setdefault(
                            sp, {})[str(fr)] = _cell(x, y)
    return {"from": t_from, "to": t_to, "assets": list(assets), "hzs": list(hzs),
            "params": {"k_prior": k, "embargo_s": EMBARGO_S, "w_scale_c": W_SCALE_C,
                       "informed_excess_c": INFORMED_EXCESS_C, "placebo": f"feature at d-{placebo_shift_s}s",
                       "pm_horizon_s": PM_HORIZON_S, "tau_fracs": list(tau_fracs),
                       "split_frac": split_frac, "scores_as_of": "window start"},
            "windows": len(grid), "samples": len(samples), "split_cut": cut,
            "coverage": {"settle": sum(s["settle"] is not None for s in samples),
                         "pm10": sum(s["pm10"] is not None for s in samples),
                         "pmus_source": dict(pm_src),
                         "active_feature": {f: sum(bool(s["x"][f]) for s in samples) for f in features}},
            "scorer": dict(sc.counts), "results": res}


# ---------------------------------------------------------------- CLI
def _ts(s: str) -> int:
    if s.lstrip("-").isdigit():
        return int(s)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return calendar.timegm(time.strptime(s.rstrip("Z"), fmt))
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"bad time {s!r} (unix seconds or YYYY-MM-DD[THH:MM[:SS]]Z)")


def _tmap(lab: Path, root: Path, hi_s: int, fetch: bool) -> TokenMap:
    """TokenMap with one token per intl window seen on the tape up to hi_s (Gamma, CLI only)."""
    tm = TokenMap(lab / "tokens.jsonl")
    if fetch:
        toks: dict[str, str] = {}
        for _, fs in iter_rtds(root, [h for h in tape_hours(root, RTDS_STREAMS) if h <= hi_s]):
            for f in fs:
                toks.setdefault(f.slug, f.token)
        asyncio.run(tm.ensure(set(toks.values())))
    return tm


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-flow")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("scores", "features", "predict"):
        p = sub.add_parser(name)
        p.add_argument("--data", type=Path, required=True)
        p.add_argument("--lab", type=Path, required=True)
        p.add_argument("--k", type=float, default=K_PRIOR)
        p.add_argument("--no-fetch", action="store_true", help="use tokens.jsonl as is (no Gamma)")
        if name == "scores":
            p.add_argument("--as-of", type=_ts, default=None, help="default: today 00:00Z")
            p.add_argument("--mid30", action="store_true", help="also +30 s intl-mid markout")
        elif name == "features":
            p.add_argument("--asset", choices=ASSETS, required=True)
            p.add_argument("--hz", choices=HZS, required=True)
            p.add_argument("--start", type=_ts, required=True)
            p.add_argument("--scores", type=Path, default=None, help="snapshot with as_of <= start")
            p.add_argument("--out", type=Path, default=None)
        else:
            p.add_argument("--from", dest="t_from", type=_ts, required=True)
            p.add_argument("--to", dest="t_to", type=_ts, required=True)
            p.add_argument("--assets", default=",".join(ASSETS))
            p.add_argument("--hz", default=",".join(HZS))
    a = ap.parse_args(argv)
    fetch = not a.no_fetch

    if a.cmd == "scores":
        as_of = a.as_of if a.as_of is not None else int(time.time()) // 86400 * 86400
        path = snapshot_path(a.lab, as_of)
        if path.exists():
            print(f"snapshot exists, not overwriting: {path}", file=sys.stderr)
            return 1
        doc = informedness_doc(a.data, _tmap(a.lab, a.data, as_of, fetch), as_of, a.k, mid30=a.mid30)
        try:
            path = write_snapshot(a.lab, doc)
        except FileExistsError:
            print(f"snapshot exists, not overwriting: {path}", file=sys.stderr)
            return 1
        print(f"as_of={as_of} wallets={doc['n_wallets']} informed={doc['n_informed']} "
              f"mu_c={doc['mu_c']} cells={doc['cells']} k={a.k} k_mom={doc['k_mom']} "
              f"mid30_coverage={doc['mid30']['coverage']} -> {path}")
        return 0

    if a.cmd == "features":
        if a.scores is not None:
            doc = orjson.loads(a.scores.read_bytes())
            if int(doc["as_of"]) > a.start:
                print(f"snapshot as_of {doc['as_of']} is after window start {a.start}: look-ahead",
                      file=sys.stderr)
                return 1
            scores: Mapping = doc["wallets"]
        else:
            scores = informedness(a.data, _tmap(a.lab, a.data, a.start, fetch), a.start, a.k)
        feats = window_flow_features(a.data, a.asset, a.hz, a.start, scores)
        out = a.out or a.lab / "flow" / f"features-{a.asset}-{a.hz}-{a.start}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(orjson.dumps({str(k): v for k, v in feats.items()},
                                     option=orjson.OPT_INDENT_2))
        act = sum(1 for v in feats.values() if v["intl_flow_up_usd_30s"])
        mids = sum(1 for v in feats.values() if v["intl_mid_up_c"] is not None)
        print(f"{a.asset} {a.hz} {a.start}: {len(feats)} s, flow_30s active {act} s, "
              f"intl mid {mids} s, scored wallets {len(scores)} -> {out}")
        return 0

    tm = _tmap(a.lab, a.data, a.t_to + 900 + EMBARGO_S, fetch)
    res = flow_predictiveness(a.data, tm, a.t_from, a.t_to, a.assets.split(","), a.hz.split(","),
                              a.k)
    out = a.lab / "flow" / (f"predict-{a.t_from}-{a.t_to}-"
                            f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2))
    print(f"windows={res['windows']} samples={res['samples']} coverage={res['coverage']}")
    for f, by_t in res["results"].items():
        for tgt, by_v in by_t.items():
            for var in ("feature", "placebo"):
                for sp in ("early", "late"):
                    cells = by_v[var][sp]
                    txt = " ".join(f"{fr}:{c['auc']}" for fr, c in cells.items())
                    print(f"{f:<24} {tgt:<6} {var:<7} {sp:<5} AUC by tau {txt}")
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
