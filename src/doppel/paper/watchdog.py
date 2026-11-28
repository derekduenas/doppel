"""Causal candidate forecasts and in-process monitoring for an admitted paper session."""

from __future__ import annotations

import bisect
import hashlib
import math
from collections import deque
from dataclasses import dataclass, field
from statistics import stdev

import orjson

from ..lab.skilldecay import CLIP, LAM, watchdog_update
from ..strategy.shadow_probability import ShadowProbability
from ..twin.engine import fee_usd
from ..twin.features import Features
from ..twin.tape import Book, NS
from .inputs import LiveWindow

HEALTHY = "HEALTHY"
WARN = "WARN_THROTTLE_50"
KILL = "KILL_DEMOTE_SHADOW"
MAX_BOOK_AGE = 3 * NS
HORIZONS_NS = {"markout_500ms": NS // 2, "markout_2s": 2 * NS,
               "markout_10s": 10 * NS, "markout_60s": 60 * NS}
OBSERVABLE = ("delta_log_loss", "markout_2s", "markout_10s")
PROMO_FEE = 0.02085
MARKETS_PER_BLOCK = 100
BASELINE_BLOCKS = 20
CONFIRM_BLOCKS = 20
BASELINE_SE = 2.86
DECLINE_SE = 3.5
SIGMA_FLOORS = {"delta_log_loss": 0.01, "markout_2s": 0.10, "markout_10s": 0.10}


def _row(body: dict) -> dict:
    return {**body, "record_sha256": hashlib.sha256(orjson.dumps(body, option=orjson.OPT_SORT_KEYS)).hexdigest()}


def _book_at(books: list[Book], gaps: list[int], t: int, *, strict: bool) -> Book | None:
    # Clock stamps are integer nanoseconds. Subtract one to exclude a tied witness.
    bound = t - int(strict)
    i = bisect.bisect_right(books, bound, key=lambda b: b.tw) - 1
    if i < 0:
        return None
    book = books[i]
    j = bisect.bisect_right(gaps, bound) - 1
    if (not book.open or book.best_bid is None or book.best_ask is None
            or not 0 < book.best_bid <= book.best_ask < 1
            or t - book.tw > MAX_BOOK_AGE or (j >= 0 and gaps[j] >= book.tw)):
        return None
    return book


def paper_marks(w: LiveWindow) -> dict[str, float | None]:
    """Per-fill side-adjusted markouts in cents using fresh in-market books."""
    marks: dict[str, float | None] = dict.fromkeys(HORIZONS_NS)
    if w.late or not w.sim.fills or w.wt.source != "ws":
        return marks
    books, gaps = sorted(w.wt.books, key=lambda b: b.tw), sorted(w.wt.gaps)
    qty = math.fsum(f["qty"] for f in w.sim.fills)
    for name, delta in HORIZONS_NS.items():
        total = 0.0
        for f in w.sim.fills:
            at = f["t_ns"] + delta
            book = _book_at(books, gaps, at, strict=False) if at < w.end * NS else None
            mid = None if book is None else book.mid
            if mid is None:
                break
            side = 1.0 if f["side"] == "buy" else -1.0
            total += 100.0 * side * (mid - f["px"]) * f["qty"]
        else:
            marks[name] = round(total / qty, 4)
    return marks


def _realized_net(w: LiveWindow, y: int) -> float:
    costs = w.sim.g.costs
    fees = math.fsum(fee_usd(PROMO_FEE if f["role"] == "taker" else costs.maker_fee_coef,
                             f["px"], f["qty"], costs.fee_rounding) for f in w.sim.fills)
    return round(w.sim.cash + w.sim.inv * y - fees, 6)


def _delta_ll(row: dict) -> float | None:
    p, mid, y = (row[key] for key in ("p_hat", "p_mkt_mid", "realized_y"))
    if p is None or mid is None or y is None:
        return None
    p, mid = (min(1 - CLIP, max(CLIP, x)) for x in (p, mid))
    return math.log(p) - math.log(mid) if y == 1 else math.log1p(-p) - math.log1p(-mid)


@dataclass
class DecayBlocks:
    """Screen full block means and confirm a material decline from a sealed live baseline."""

    sigma_floor: float
    baseline: list[float] = field(default_factory=list)
    recent: deque[float] = field(default_factory=lambda: deque(maxlen=CONFIRM_BLOCKS))
    sensor: dict | None = None
    mean: float | None = None
    sigma: float | None = None
    completed: int = 0

    @property
    def armed(self) -> bool:
        return (self.mean is not None and self.sigma is not None
                and self.mean - BASELINE_SE * self.sigma / math.sqrt(BASELINE_BLOCKS) > self.sigma_floor)

    def observe(self, value: float | None) -> tuple[bool, bool]:
        if value is None:
            self.recent.clear()
            self.sensor = None
            return False, False
        self.completed += 1
        if self.sigma is None:
            self.baseline.append(value)
            if len(self.baseline) == BASELINE_BLOCKS:
                self.mean = math.fsum(self.baseline) / BASELINE_BLOCKS
                self.sigma = max(self.sigma_floor, stdev(self.baseline))
            return False, False
        if not self.armed:
            return False, False
        self.sensor = watchdog_update(self.sensor, value, sigma=self.sigma)
        self.recent.append(value)
        g = self.sensor["g"]
        if g <= LAM / 2 or len(self.recent) < CONFIRM_BLOCKS or self.mean is None or self.mean <= 0:
            return False, False
        recent_mean = math.fsum(self.recent) / CONFIRM_BLOCKS
        se = math.hypot(self.sigma / math.sqrt(BASELINE_BLOCKS),
                        max(self.sigma_floor, stdev(self.recent)) / math.sqrt(CONFIRM_BLOCKS))
        confirmed = recent_mean <= self.mean / 2 and self.mean - recent_mean >= DECLINE_SE * se
        return confirmed, confirmed and g > LAM


@dataclass
class ShadowWatchdog:
    model: ShadowProbability
    genome_id: str
    state: str = HEALTHY
    pending: dict[str, dict] = field(default_factory=dict)
    ready: dict[str, dict] = field(default_factory=dict)
    detectors: dict[str, DecayBlocks] = field(default_factory=lambda: {
        name: DecayBlocks(SIGMA_FLOORS[name]) for name in OBSERVABLE})
    block_rows: int = 0
    block_values: dict[str, list[float]] = field(default_factory=lambda: {name: [] for name in OBSERVABLE})

    def forecast(self, w: LiveWindow, t: int, now: int, *, active: bool) -> dict:
        if w.slug in self.pending or t != (w.start + self.model.offset_s) * NS:
            raise ValueError("unexpected or duplicate probability cutoff")
        book = _book_at(sorted(w.wt.books, key=lambda b: b.tw), sorted(w.wt.gaps), t,
                        strict=True) if active and t <= now < w.end * NS and not w.late else None
        mid = book.mid if book is not None else None
        probability, inputs = None, None
        if mid is not None:
            # Whole-second reference observations use the prior second. The L2 snapshot
            # has its own strictly prior receipt clock. Never mutate the trading features.
            x = Features(w.sim.g, w.ref, w.cfg.feature_config()).at(t // NS - 1, book, 0.0)
            probability = self.model.predict(x)
            if probability is not None:
                inputs = {name: x[name] for name, _ in self.model.weights}
        row = _row({
            "stage": "forecast", "window_id": w.slug, "ts_ns": t, "genome_id": self.genome_id,
            "window_end_ns": w.end * NS, "observation_ns": now,
            "model_sha256": self.model.sha256, "book_tw_ns": book.tw if book is not None else None,
            "reference_cutoff_ns": t - NS, "features": inputs,
            "p_hat": probability, "p_mkt_mid": mid, "realized_y": None, "net_return_02085": None,
            "e_value_running": None, **dict.fromkeys(HORIZONS_NS),
            "status": "SEALED" if probability is not None else "UNMEASURED",
        })
        self.pending[w.slug] = row
        return row

    def settle(self, w: LiveWindow, t: int, result: dict | None, e_value: float | None,
               *, active: bool) -> tuple[dict, list[dict]]:
        forecast = self.pending.get(w.slug)
        if forecast is None:
            raise ValueError("settlement is missing its scheduled forecast record")
        outcome = None if result is None else result["outcome"]
        row = _row({
            "stage": "settlement", "window_id": w.slug, "ts_ns": t, "genome_id": self.genome_id,
            "forecast_sha256": forecast["record_sha256"], "p_hat": forecast["p_hat"],
            "p_mkt_mid": forecast["p_mkt_mid"], "realized_y": outcome,
            "net_return_02085": None if outcome is None else _realized_net(w, outcome),
            "e_value_running": e_value, **paper_marks(w), "monitor_eligible": active and not w.late,
        })
        self.ready[w.slug] = row
        events = []
        while self.pending and (key := next(iter(self.pending))) in self.ready:
            self.pending.pop(key)
            events.extend(self.consume(self.ready.pop(key)))
        return row, events

    def consume(self, row: dict) -> list[dict]:
        """Consume the reconciliation record in the same form published to JSONL."""
        if self.state == KILL:
            return []
        if row["monitor_eligible"]:
            values = {"delta_log_loss": _delta_ll(row), "markout_2s": row["markout_2s"],
                      "markout_10s": row["markout_10s"]}
            for name, value in values.items():
                if value is not None and math.isfinite(value):
                    self.block_values[name].append(value)
        self.block_rows += 1
        if self.block_rows < MARKETS_PER_BLOCK:
            return []
        means = {name: math.fsum(xs) / MARKETS_PER_BLOCK if len(xs) == MARKETS_PER_BLOCK else None
                 for name, xs in self.block_values.items()}
        self.block_rows = 0
        self.block_values = {name: [] for name in OBSERVABLE}
        decisions = {name: self.detectors[name].observe(value) for name, value in means.items()}
        warns = [name for name, (warn, _) in decisions.items() if warn]
        kills = [name for name, (_, kill) in decisions.items() if kill]
        events = []
        for phase, reasons in ((WARN, warns), (KILL, kills)):
            if reasons and (phase == KILL or self.state == HEALTHY):
                self.state = phase
                events.append({"state": phase, "window_id": row["window_id"], "metrics": reasons,
                               "delta_log_loss": means["delta_log_loss"], "markets_per_block": MARKETS_PER_BLOCK})
        return events

    def snapshot(self) -> dict:
        return {"status": self.state,
                "markets_per_block": MARKETS_PER_BLOCK, "markets_in_block": self.block_rows,
                "log_loss_measured": self.detectors["delta_log_loss"].sigma is not None,
                "metrics": {name: {"complete_blocks": metric.completed, "baseline_mean": metric.mean,
                                   "armed": metric.armed,
                                   "sigma": metric.sigma, "confirmation_blocks": len(metric.recent),
                                   "g": (metric.sensor or {}).get("g")}
                            for name, metric in self.detectors.items()}}
