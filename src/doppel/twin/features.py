"""Genome features at a decision second, from data RECEIVED at or before it.

Reference data:
- raw BRTI (BTC only) by publication second, from ``ref_brti_raw`` (PERELMAN import) and
  ``ref_brti_live`` (our Kalshi poller), the same merge as ``lab.brti_import.load_raw`` but loaded
  one hour at a time. A BRTI print for second s is NOT known at s: the twin treats it as known at
  s + ``brti_delay_s`` (default 1 s). Everything derived from BRTI (fair_up, brti moves,
  dist_to_open_usd, the edges) is computed as of d_eff = d - brti_delay_s.
- the published PM US settlement series (``ref_prices``), only at window boundaries:
  priceToBeat = A(S-1) after its first retained receipt. Revisions become eligible at receipt.
  Final historical A(S-1) and A(E-1) are available for retrospective settlement labels.

Opening rules (``FeatureConfig.opening``, docs/decisions/2026-11-27-opening-price-protocol.md):
- ``published``: the opening is the published A(S-1) once its row is received (the rule above).
- ``received_raw``: the opening is also computable from raw BRTI: mean of the 60 raw seconds
  [S-60-lag, S-1-lag], known at d iff every one of them was received by d (``computed_opening``).
  Whichever source is known first is used for the whole window (tie: published).

fair_up is fair-v1 (``lab.fair.fair_up_raw``) with the genome's ``vol_window_s`` and the measured
alignment lag (E19: 0). Assets other than BTC have no raw index, hence no fair and no BRTI features:
those features are NaN, NaN never satisfies a condition, and the engine never quotes without fair.
"""

from __future__ import annotations

from bisect import bisect_right
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import orjson

from ..lab.brti_import import raw_observations
from ..lab.fair import fair_up_raw
from ..strategy.genome import FEATURES, Genome, fee_c
from ..tape.segment import read_segment
from .tape import NS, Book, hour_path

NAN = float("nan")
BOUNDARY = 300          # every 5m and 15m window starts on a multiple of 300 s
RAW_PAD_S = 120         # raw BRTI kept before (start - vol_window) for moves and lags


@dataclass
class WindowRef:
    asset: str
    start: int
    end: int
    ptb: float | None                   # A(S-1)
    settle: float | None                # A(E-1)
    raw: np.ndarray | None = None       # raw BRTI by second, index 0 == raw_t0 (BTC only)
    raw_t0: int = 0
    rcv: np.ndarray | None = None       # our receive time (unix s, float) of each raw second; NaN unknown

    # None is an explicitly supplied static reference (e.g. a synthetic test).
    # Tape-backed windows always carry a history, including an empty one.
    ptb_history: tuple[tuple[int, float | None], ...] | None = field(default=None, kw_only=True)

    def opening_at(self, receive_ns: int) -> tuple[int | None, float | None]:
        """Latest opening value recorded by receive_ns, inclusive, and its receipt."""
        if self.ptb_history is None:
            return None, self.ptb
        i = bisect_right(self.ptb_history, receive_ns, key=lambda x: x[0])
        return self.ptb_history[i - 1] if i else (None, None)

    def published_first_s(self) -> float:
        """When a usable published opening was first received (unix s): -inf for a static reference,
        +inf if none was. A conflicting revision (value None) is not usable."""
        if self.ptb_history is None:
            return -math.inf if self.ptb is not None else math.inf
        return next((tw / NS for tw, v in self.ptb_history if v is not None), math.inf)

    def computed_opening(self, lag: int = 0) -> tuple[float | None, float | None]:
        """(A(S-1) computed from raw BRTI, receive time of its last-received second); see
        ``computed_opening``."""
        if self.raw is None or self.rcv is None:
            return None, None
        return computed_opening(self.raw, self.rcv, self.raw_t0, self.start, lag)

    def opening_known(self, d: int, rule: str = "published", lag: int = 0) -> tuple[str | None, float | None]:
        """(source, opening) known at decision second d under ``rule``; (None, None) before."""
        if rule == "published":
            _, value = self.opening_at(d * NS)
            return ("published" if value is not None else None), value
        if rule != "received_raw":
            raise ValueError(f"unknown opening rule {rule!r}")
        value, known_s = self.computed_opening(lag)
        tc = known_s if value is not None and known_s is not None else math.inf
        tp = self.published_first_s()
        if min(tc, tp) > d:
            return None, None
        if tp <= tc:     # the published row came first (or at the same instant): legacy for this window
            _, pub = self.opening_at(d * NS)
            return ("published" if pub is not None else None), pub
        return "computed", value

    def opening_summary(self, lag: int = 0) -> dict:
        """Per-window record of both opening sources (for parity and receipt-time reports)."""
        value, known_s = self.computed_opening(lag)
        tp = self.published_first_s()
        return {"computed": value, "computed_known_s": known_s,
                "published_first_s": tp if math.isfinite(tp) else None}

    @property
    def outcome(self) -> int | None:
        """1 = Up (YES pays $1), 0 = Down. Ties settle Up."""
        if self.ptb is None or self.settle is None:
            return None
        return 1 if self.settle >= self.ptb else 0


class RefStore:
    """Raw BRTI and the published series' boundary values, read lazily with bounded memory."""

    def __init__(self, root: Path, keep_hours: int = 4):
        self.root = Path(root)
        self.keep_hours = keep_hours
        self._brti: dict[int, dict[int, tuple[float, float]]] = {}
        self._bounds: dict[str, dict[int, tuple[tuple[int, float | None], ...]]] = {}
        self._bounds_range: dict[str, tuple[int, int]] = {}

    # -- published series ---------------------------------------------------------------------
    def load_boundaries(self, asset: str, lo: int, hi: int) -> None:
        """Load boundary revisions in receive order for decisions and final settlement labels."""
        key = f"crypto:{asset}"
        out: dict[int, dict[int, float | None]] = {}
        for f in sorted((self.root / "ref_prices").glob("*/*.dtape.zst")):
            for rec in read_segment(f, verify=True, strict=True):
                m = rec.get("meta") or {}
                if m.get("asset") != key or m.get("status") != 200:
                    continue
                if "from" in m and "to" in m and (m["to"] < lo or m["from"] > hi):
                    continue
                for ser in orjson.loads(rec["raw"]).get("series", []):
                    for c in ser.get("candles") or []:
                        t = int(c["timestamp"])
                        if lo <= t <= hi and (t + 1) % BOUNDARY == 0:
                            tw = rec["tw"]
                            value = float(c["close"])
                            if type(tw) is not int or tw < (t + 1) * NS or not math.isfinite(value) or value <= 0:
                                continue
                            history = out.setdefault(t, {})
                            if tw not in history:
                                history[tw] = value
                            elif history[tw] != value:
                                history[tw] = None
        self._bounds[asset] = {t: tuple(sorted(versions.items())) for t, versions in out.items()}
        self._bounds_range[asset] = (lo, hi)

    def boundary(self, asset: str, t: int) -> float | None:
        r = self._bounds_range.get(asset)
        if r is None or not r[0] <= t <= r[1]:
            raise KeyError(f"boundaries for {asset} not loaded around {t}")
        revisions = self._bounds[asset].get(t)
        return revisions[-1][1] if revisions else None

    # -- raw BRTI -----------------------------------------------------------------------------
    def _hour(self, hour: int) -> dict[int, tuple[float, float]]:
        """second -> (raw BRTI, our receive time in unix s). Receive time decides when a second is
        KNOWN (audit 2026-11-26: the 5 s live poller can deliver a second ~4 s after it prints)."""
        if hour in self._brti:
            return self._brti[hour]
        source = hour_path(self.root, "ref_brti_raw", hour)
        # Live files are indexed by capture hour; a poll can cross the publication-hour boundary.
        live = [hour_path(self.root, "ref_brti_live", h) for h in (hour, hour+3600)]
        out = raw_observations([source] if source.exists() else [], [p for p in live if p.exists()],
                               hour, hour+3599)
        self._brti[hour] = out
        for h in [h for h in self._brti if h < hour - self.keep_hours * 3600]:
            del self._brti[h]
        return out

    def brti(self, lo: int, hi: int) -> np.ndarray:
        """Raw BRTI for seconds [lo, hi] (NaN where missing)."""
        return self.brti_rcv(lo, hi)[0]

    def brti_rcv(self, lo: int, hi: int) -> tuple[np.ndarray, np.ndarray]:
        """(raw BRTI, receive time unix s) for seconds [lo, hi]; NaN where missing."""
        a = np.full(hi - lo + 1, np.nan)
        r = np.full(hi - lo + 1, np.nan)
        for hour in range(lo // 3600 * 3600, hi + 1, 3600):
            for s, (v, rc) in self._hour(hour).items():
                if lo <= s <= hi:
                    a[s - lo] = v
                    r[s - lo] = rc
        return a, r

    def window_ref(self, asset: str, start: int, end: int, vol_window_s: int) -> WindowRef:
        ref = WindowRef(asset=asset, start=start, end=end, ptb=self.boundary(asset, start - 1),
                        settle=self.boundary(asset, end - 1),
                        ptb_history=self._bounds[asset].get(start - 1, ()))
        if asset == "btc":
            ref.raw_t0 = start - vol_window_s - RAW_PAD_S
            ref.raw, ref.rcv = self.brti_rcv(ref.raw_t0, end)
        return ref


# ---------------------------------------------------------------------------- opening
OPENING_SECONDS = 60


def computed_opening(raw: np.ndarray, rcv: np.ndarray, t0: int, start: int,
                     lag: int = 0) -> tuple[float | None, float | None]:
    """A(S-1) from raw BRTI as PM US defines it (E18/E19: mean of raw[t-59..t] at lag 0), with the
    time it became known: the latest receive stamp of the 60 seconds [S-60-lag, S-1-lag]. Same
    arithmetic as ``refprice.settlement_value`` (ascending Python sum / 60.0). (None, None) if any
    second is missing, conflicting (NaN) or has no receive stamp."""
    lo, hi = start - OPENING_SECONDS - lag, start - 1 - lag
    if lo < t0 or hi - t0 >= len(raw):
        return None, None
    xs: list[float] = []
    known = -math.inf
    for s in range(lo, hi + 1):
        v, r = float(raw[s - t0]), float(rcv[s - t0])
        if not (math.isfinite(v) and math.isfinite(r)):
            return None, None
        xs.append(v)
        known = max(known, r)
    return sum(xs) / 60.0, known


def opening_parity(pairs, tol_usd: float, min_share: float) -> dict:
    """Agreement of computed vs published openings over (computed, published) pairs; a pair with
    either side missing is counted but not compared."""
    diffs = sorted(abs(c - p) for c, p in pairs if c is not None and p is not None)
    n = len(diffs)
    within = sum(1 for x in diffs if x <= tol_usd + 1e-9)
    share = within / n if n else None
    return {"windows": len(pairs), "compared": n, "tol_usd": tol_usd, "min_share": min_share,
            "within_tol": within, "share_within_tol": share,
            "max_abs_usd": diffs[-1] if n else None,
            "p99_abs_usd": float(np.percentile(diffs, 99)) if n else None,
            "pass": share is not None and share >= min_share}


# ---------------------------------------------------------------------------- features
def projected_settlement(raw: np.ndarray, t0: int, d: int, end: int, lag: int = 0) -> float | None:
    """fair-v1's projection of the settlement average at second d: known prints of the final 60
    plus the last print for each unknown one (zero drift). Same arithmetic as ``fair_up_raw``."""
    lo_s, hi_s = end - 60 - lag, end - 1 - lag
    i_d = d - t0
    if i_d < 0 or i_d >= len(raw) or math.isnan(raw[i_d]) or lo_s - t0 < 0:
        return None
    known = [raw[s - t0] for s in range(lo_s, min(d, hi_s) + 1)]
    if any(math.isnan(k) for k in known):
        return None
    m = max(0, hi_s - max(d, lo_s - 1))
    return float((sum(known) + m * raw[i_d]) / 60.0)


def placebo_u(start: int, d: int) -> float:
    """Deterministic, market-independent U[0,1) for placebo arms (splitmix64 of window start, second)."""
    z = (start * 0x9E3779B97F4A7C15 + d * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    z ^= z >> 31
    return (z >> 11) / float(1 << 53)


@dataclass(frozen=True)
class FeatureConfig:
    brti_delay_s: int = 1
    fair_lag_s: int = 0
    opening: str = "published"          # "published" | "received_raw" (see module docstring)


class Features:
    """x(d) for one window. ``book`` must be the valid book as of d (receive time <= d) or None."""

    def __init__(self, genome: Genome, ref: WindowRef, cfg: FeatureConfig = FeatureConfig()):
        self.g, self.ref, self.cfg = genome, ref, cfg
        self.coef = genome.costs.taker_fee_coef
        self.vol_window = genome.fair.vol_window_s

    def _raw_known(self, d: int) -> np.ndarray | None:
        """Raw BRTI with every second we had NOT received by decision second d set to NaN."""
        r = self.ref
        if r.raw is None:
            return None
        if r.rcv is None:
            return r.raw
        if getattr(self, "_known_d", None) != d:
            self._known = np.where(r.rcv <= d, r.raw, np.nan)
            self._known_d = d
        return self._known

    def brti_at(self, s: int, raw: np.ndarray | None = None) -> float:
        r = self.ref
        raw = r.raw if raw is None else raw
        if raw is None:
            return NAN
        i = s - r.raw_t0
        return float(raw[i]) if 0 <= i < len(raw) else NAN

    def opening(self, d: int) -> float | None:
        return self.ref.opening_known(d, self.cfg.opening, self.cfg.fair_lag_s)[1]

    def fair(self, d: int) -> float:
        r = self.ref
        opening = self.opening(d)
        if r.raw is None or opening is None:
            return NAN
        de = d - self.cfg.brti_delay_s
        if de - self.vol_window < r.raw_t0:
            return NAN
        known = self._raw_known(d)
        assert known is not None
        p = fair_up_raw(known, r.raw_t0, opening, de, r.end, self.cfg.fair_lag_s, self.vol_window)
        return NAN if p is None else float(p)

    def at(self, d: int, book: Book | None, inventory_up: float) -> dict[str, float]:
        r = self.ref
        x = {k: NAN for k in FEATURES}
        x["tau_s"] = float(d - r.start)
        x["tte_s"] = float(r.end - d)
        x["inventory_up"] = float(inventory_up)
        x["placebo_u"] = placebo_u(r.start, d)
        x["placebo_block_u"] = placebo_u(r.start, r.start + (d - r.start) // 30 * 30)
        fair = self.fair(d)
        x["fair_up"] = fair
        de = d - self.cfg.brti_delay_s
        known = self._raw_known(d)
        now = self.brti_at(de, known)
        for k, name in ((1, "brti_move_1s"), (5, "brti_move_5s"), (30, "brti_move_30s")):
            x[name] = now - self.brti_at(de - k, known)
        opening = self.opening(d)
        if known is not None and opening is not None:
            proj = projected_settlement(known, r.raw_t0, de, r.end, self.cfg.fair_lag_s)
            if proj is not None:
                x["dist_to_open_usd"] = proj - opening
        if book is not None and book.open:
            bb, ba = book.best_bid, book.best_ask
            if ba is not None:
                x["depth_top_up"] = book.asks[0][1]
                if fair == fair:
                    x["edge_buy_up_c"] = 100.0 * (fair - ba) - fee_c(ba, self.coef)
            if bb is not None:
                x["depth_top_down"] = book.bids[0][1]
                down_ask = 1.0 - bb
                if fair == fair:
                    x["edge_buy_down_c"] = 100.0 * ((1.0 - fair) - down_ask) - fee_c(down_ask, self.coef)
            if bb is not None and ba is not None:
                x["mid_up_c"] = 50.0 * (bb + ba)
                x["spread_c"] = 100.0 * (ba - bb)
        return x
