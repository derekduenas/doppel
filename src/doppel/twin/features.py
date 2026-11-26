"""Genome features at a decision second, from data RECEIVED at or before it.

Reference data:
- raw BRTI (BTC only) by publication second, from ``ref_brti_raw`` (PERELMAN import) and
  ``ref_brti_live`` (our Kalshi poller), the same merge as ``lab.brti_import.load_raw`` but loaded
  one hour at a time. A BRTI print for second s is NOT known at s: the twin treats it as known at
  s + ``brti_delay_s`` (default 1 s). Everything derived from BRTI (fair_up, brti moves,
  dist_to_open_usd, the edges) is computed as of d_eff = d - brti_delay_s.
- the published PM US settlement series (``ref_prices``), only at window boundaries:
  priceToBeat = A(S-1), settlement = A(E-1), Up iff settlement >= priceToBeat (ties Up).

fair_up is fair-v1 (``lab.fair.fair_up_raw``) with the genome's ``vol_window_s`` and the measured
alignment lag (E19: 0). Assets other than BTC have no raw index, hence no fair and no BRTI features:
those features are NaN, NaN never satisfies a condition, and the engine never quotes without fair.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import orjson

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
        self._bounds: dict[str, dict[int, float]] = {}
        self._bounds_range: dict[str, tuple[int, int]] = {}

    # -- published series ---------------------------------------------------------------------
    def load_boundaries(self, asset: str, lo: int, hi: int) -> None:
        """Boundary values A(t) with t + 1 a multiple of 300, lo <= t <= hi. Later chunks override
        earlier ones (same order as ``lab.refprice.load_series``)."""
        key = f"crypto:{asset}"
        out: dict[int, float] = {}
        for f in sorted((self.root / "ref_prices").glob("*/*.dtape.zst")):
            for rec in read_segment(f, verify=True):
                m = rec.get("meta") or {}
                if m.get("asset") != key or m.get("status") != 200:
                    continue
                if "from" in m and "to" in m and (m["to"] < lo or m["from"] > hi):
                    continue
                for ser in orjson.loads(rec["raw"]).get("series", []):
                    for c in ser.get("candles") or []:
                        t = int(c["timestamp"])
                        if lo <= t <= hi and (t + 1) % BOUNDARY == 0:
                            out[t] = float(c["close"])
        self._bounds[asset] = out
        self._bounds_range[asset] = (lo, hi)

    def boundary(self, asset: str, t: int) -> float | None:
        r = self._bounds_range.get(asset)
        if r is None or not r[0] <= t <= r[1]:
            raise KeyError(f"boundaries for {asset} not loaded around {t}")
        return self._bounds[asset].get(t)

    # -- raw BRTI -----------------------------------------------------------------------------
    def _hour(self, hour: int) -> dict[int, tuple[float, float]]:
        """second -> (raw BRTI, our receive time in unix s). Receive time decides when a second is
        KNOWN (audit 2026-11-26: the 5 s live poller can deliver a second ~4 s after it prints)."""
        if hour in self._brti:
            return self._brti[hour]
        out: dict[int, tuple[float, float]] = {}
        p = hour_path(self.root, "ref_brti_raw", hour)
        if p.exists():
            for rec in read_segment(p, verify=True):
                if rec["raw"]:
                    d = orjson.loads(rec["raw"])
                    s = int(d["t_ms"]) // 1000
                    if hour <= s < hour + 3600:
                        out[s] = (float(d["v"]), _recv_s(d.get("received"), int(d["t_ms"])))
        for h in (hour, hour + 3600):     # live poller files are by receive hour (>= print hour)
            p = hour_path(self.root, "ref_brti_live", h)
            if not p.exists():
                continue
            for rec in read_segment(p, verify=True):
                for t_ms, v in (rec.get("meta") or {}).get("new") or []:
                    s = int(t_ms) // 1000
                    if hour <= s < hour + 3600:
                        out.setdefault(s, (float(v), rec["tw"] / 1e9))
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
                        settle=self.boundary(asset, end - 1))
        if asset == "btc":
            ref.raw_t0 = start - vol_window_s - RAW_PAD_S
            ref.raw, ref.rcv = self.brti_rcv(ref.raw_t0, end)
        return ref


def _recv_s(received, t_ms: int) -> float:
    """PERELMAN's ``received`` stamp in unix seconds (s or ms); publication + 1 s if absent."""
    if received is None:
        return t_ms / 1000.0 + 1.0
    r = float(received)
    return r / 1000.0 if r > 1e11 else r


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

    def fair(self, d: int) -> float:
        r = self.ref
        if r.raw is None or r.ptb is None:
            return NAN
        de = d - self.cfg.brti_delay_s
        if de - self.vol_window < r.raw_t0:
            return NAN
        known = self._raw_known(d)
        assert known is not None
        p = fair_up_raw(known, r.raw_t0, r.ptb, de, r.end, self.cfg.fair_lag_s, self.vol_window)
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
        if known is not None and r.ptb is not None:
            proj = projected_settlement(known, r.raw_t0, de, r.end, self.cfg.fair_lag_s)
            if proj is not None:
                x["dist_to_open_usd"] = proj - r.ptb
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
