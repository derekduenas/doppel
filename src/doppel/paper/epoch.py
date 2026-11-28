"""Monitoring data for an admitted Shadow journal's settled paper windows."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..strategy.genome import Genome
from ..strategy.sequential import (
    ALPHA, BLOCK, LAMBDA_CAP, LOG_E_MAX_JSON, METHOD, PRIOR_VAR, loss_bound_c,
)
from ..tape.universe import HORIZONS
from ..twin.tape import NS, WindowTape
from .inputs import LiveWindow


def _book_complete(wt: WindowTape, max_age_ns: int) -> bool:
    """A fresh two-sided WS book throughout the market interval."""
    start, end = wt.start * NS, wt.end * NS
    if wt.source != "ws" or any(start <= gap < end for gap in wt.gaps):
        return False
    last_opening_gap = max((gap for gap in wt.gaps if gap < start), default=-1)
    latest = None
    for book in wt.books:
        if book.tw >= end:
            break
        if book.tw > start:
            if latest is None or latest < last_opening_gap or book.tw - latest > max_age_ns:
                return False
        latest = book.tw if book.open and book.mid is not None else None
    return latest is not None and latest >= last_opening_gap and end - latest <= max_age_ns


def _markouts(w: LiveWindow, book_complete: bool) -> dict:
    """Quantity-weighted twin marks for a settled window with observed in-market mids."""
    marks: dict[str, float | None] = {"2": None, "10": None}
    fills = w.sim.fills
    if book_complete and fills:
        total = sum(f["qty"] for f in fills)
        for seconds in marks:
            if all(f["t_ns"] + int(seconds) * NS < w.end * NS
                   and f["markout_c"].get(seconds) is not None for f in fills):
                marks[seconds] = round(sum(f["qty"] * f["markout_c"][seconds] for f in fills) / total, 4)
    return {"status": "MEASURED" if all(v is not None for v in marks.values()) else "UNMEASURED",
            "n_paper_fills": len(fills), "by_horizon_c": marks}


@dataclass
class ShadowEpochs:
    """Bounded positive-direction aGRAPA state for one contiguous market series."""

    next_start: int
    span_s: int
    max_age_ns: int
    bound_c: float
    blocked: str | None = None
    n_windows: int = 0
    n_blocks: int = 0
    pending: dict[int, tuple[float | None, str | None]] = field(default_factory=dict)
    block_net: list[float] = field(default_factory=list)
    z_sum: float = 0.0
    z_ss: float = 0.0
    log_e: float = 0.0

    @classmethod
    def start(cls, g: Genome, first_start: int, max_book_age_s: float) -> ShadowEpochs:
        span = HORIZONS[g.universe.horizons[0]]
        bound = loss_bound_c(g)
        age = int(max_book_age_s * NS) if math.isfinite(max_book_age_s) else 0
        if len(g.universe.horizons) != 1 or len(g.universe.assets) != 1:
            reason = "overlapping_market_universe"
        elif g.mode != "taker":
            reason = "maker_trade_coverage_unmeasured"
        elif age <= 0 or not math.isfinite(bound) or bound <= 0:
            reason = "measurement_bound_unavailable"
        else:
            reason = None
        return cls(first_start, span, age, bound, reason)

    def settle(self, w: LiveWindow, result: dict | None, *, inactive: bool) -> dict:
        complete = (result is not None and self.max_age_ns > 0 and not w.late
                    and not w.sim.stale_book_s and not inactive and _book_complete(w.wt, self.max_age_ns))
        value = result["net_c"] if result is not None else None
        marks = _markouts(w, complete)
        if self.blocked is None:
            if inactive:
                reason = "shadow_inactive"
            elif result is None:
                reason = "unsettled_market"
            elif not complete:
                reason = "incomplete_book_observation"
            elif value is None or not math.isfinite(value):
                reason = "invalid_net_c"
            else:
                reason = None
            self._observe(w.start, w.end, value, reason)
        return {"e_value": self.snapshot(), "paper_markouts": marks,
                "epoch_net_c": value if complete and value is not None and math.isfinite(value) else None,
                "decay_watchdog": {"status": "UNMEASURED", "reason": "no_sealed_strategy_forecast"}}

    def _observe(self, start: int, end: int, value: float | None, reason: str | None) -> None:
        if (start < self.next_start or start in self.pending or (start - self.next_start) % self.span_s
                or end != start + self.span_s):
            self.blocked = "invalid_market_epoch"
        if self.blocked is not None:
            self.pending.clear()
            return
        self.pending[start] = value, reason
        # Receipt order can differ from market order. The runtime times out overdue settlements,
        # keeping this queue bounded. A missing epoch invalidates the series instead of shifting blocks.
        while self.next_start in self.pending:
            value, reason = self.pending.pop(self.next_start)
            if reason is not None:
                self.blocked = reason
                break
            assert value is not None
            self.next_start += self.span_s
            self.n_windows += 1
            self.block_net.append(value)
            if len(self.block_net) < BLOCK:
                continue
            z = (sum(self.block_net) / BLOCK) / self.bound_c
            self.block_net.clear()
            if z < -1.0:
                self.blocked = "loss_bound_violated"
                break
            self._bet(min(1.0, z))
        if self.blocked is not None:
            self.pending.clear()

    def _bet(self, z: float) -> None:
        # Frozen betting_path uses past-only aGRAPA. Keep its positive path in constant space.
        m = self.z_sum / (self.n_blocks + 1)
        v = (PRIOR_VAR + self.z_ss) / (self.n_blocks + 1)
        fraction = min(LAMBDA_CAP, max(0.0, m / (v + m * m)))
        self.log_e += math.log1p(fraction * z)
        self.z_ss += (z - m) ** 2
        self.z_sum += z
        self.n_blocks += 1

    def snapshot(self) -> dict:
        measured = self.blocked is None and self.n_blocks > 0
        return {"status": "MEASURED" if measured else "UNMEASURED", "scope": "PAPER_ONLY",
                "reason": None if measured else self.blocked or "awaiting_complete_block",
                "method": METHOD, "block_windows": BLOCK, "n_windows": self.n_windows,
                "through_s": self.next_start if self.n_windows else None,
                "n_blocks": self.n_blocks, "e_n": math.exp(min(self.log_e, LOG_E_MAX_JSON)) if measured else None,
                "threshold": 1.0 / ALPHA}
