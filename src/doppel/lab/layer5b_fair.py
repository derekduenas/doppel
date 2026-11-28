"""Prepare Layer 5b fair-value witnesses from receipt-stamped raw BRTI."""

from __future__ import annotations

import math
from typing import Iterable, Iterator

import numpy as np

from ..twin.tape import NS
from .fair import BRTI_DELAY_S, fair_up_raw
from .taker_entries import MAX_AGE_NS

OPENING_SECONDS = 60


def _publications(records: Iterable[dict]) -> Iterator[tuple[int, int, float]]:
    """Decode the native BRTI poll envelopes without using archived import stamps."""
    last_tw = 0
    for record in records:
        tw = record.get("tw")
        if (record.get("stream") != "ref_brti_live" or type(tw) is not int
                or tw <= 0 or tw < last_tw):
            raise ValueError("invalid native BRTI stream or nonmonotonic receipt clock")
        last_tw = tw
        meta = record.get("meta") or {}
        if meta.get("ctl") or meta.get("status") != 200:
            continue
        for time_ms, raw in meta.get("new") or ():
            if (type(time_ms) is not int or not 0 < time_ms * 1_000_000 <= tw
                    or type(raw) not in (int, float) or not math.isfinite(raw) or raw <= 0):
                raise ValueError("invalid publication timestamp or native BRTI value")
            yield tw, time_ms // 1000, float(raw)


class FairWitnesses:
    """Past-only fair-v1 with the initial mark recomputed from received raw BRTI."""

    def __init__(self, records: Iterable[dict], *, vol_window_s: int = 300,
                 delay_s: int = BRTI_DELAY_S):
        if type(vol_window_s) is not int or not 2 <= vol_window_s <= 3600:
            raise ValueError("invalid BRTI volatility window")
        if type(delay_s) is not int or not 0 <= delay_s <= 30:
            raise ValueError("invalid BRTI publication delay")
        self._observations = _publications(records)
        self._next = next(self._observations, None)
        self._known: dict[int, tuple[int, float]] = {}
        self._last = self._market = 0
        self._vol_window, self._delay = vol_window_s, delay_s

    def at(self, cutoff_ns: int, market_start: int, market_end: int) -> dict | None:
        if (type(cutoff_ns) is not int or cutoff_ns < self._last or market_start < self._market
                or not 0 < market_start * NS <= cutoff_ns < market_end * NS):
            raise ValueError("fair-value cuts must be ordered within their scheduled markets")
        self._last = cutoff_ns
        lo = market_start - max(OPENING_SECONDS, self._vol_window + self._delay)
        if market_start != self._market:
            self._known = {t: point for t, point in self._known.items() if t >= lo}
            self._market = market_start
        while self._next is not None and self._next[0] < cutoff_ns:
            receipt, t, value = self._next
            if t >= lo:
                old = self._known.get(t)
                if old is None:
                    self._known[t] = (receipt, value)
                elif old[0] == receipt and old[1] != value:
                    self._known[t] = (receipt, math.nan)
            self._next = next(self._observations, None)

        d = cutoff_ns // NS - self._delay
        history = [self._known.get(t) for t in range(d - self._vol_window, d + 1)]
        opening = [self._known.get(t) for t in range(market_start - OPENING_SECONDS, market_start)]
        active_settle = max(0, d - (market_end - OPENING_SECONDS) + 1)
        settle = [self._known.get(t) for t in range(market_end - OPENING_SECONDS,
                                                   market_end - OPENING_SECONDS + active_settle)]
        present = [point for point in (*history, *opening, *settle) if point is not None]
        if len(present) != len(history) + len(opening) + len(settle):
            return None
        tw = max(point[0] for point in present)
        if cutoff_ns - tw > MAX_AGE_NS or any(not math.isfinite(point[1]) for point in present):
            return None
        ptb = sum(point[1] for point in opening if point is not None) / OPENING_SECONDS
        raw = np.fromiter((self._known.get(t, (0, math.nan))[1] for t in range(lo, d + 1)), dtype=float)
        probability = fair_up_raw(raw, lo, ptb, d, market_end, vol_window=self._vol_window)
        return None if probability is None else {"tw_ns": tw, "p_up": probability}
