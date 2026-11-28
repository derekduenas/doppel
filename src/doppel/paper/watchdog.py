"""Causal candidate forecasts and in-process monitoring for an admitted paper session."""

from __future__ import annotations

import bisect
import hashlib
import math
from dataclasses import dataclass, field

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
class ShadowWatchdog:
    model: ShadowProbability
    genome_id: str
    state: str = HEALTHY
    pending: dict[str, dict] = field(default_factory=dict)
    ready: dict[str, dict] = field(default_factory=dict)
    detectors: dict[str, dict] = field(default_factory=dict)

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
        if not row["monitor_eligible"] or self.state == KILL:
            return []
        values = {"delta_log_loss": _delta_ll(row), "markout_2s": row["markout_2s"],
                  "markout_10s": row["markout_10s"]}
        for name, value in values.items():
            if value is not None:
                self.detectors[name] = watchdog_update(self.detectors.get(name), value)
        warns = [k for k in OBSERVABLE if self.detectors.get(k, {}).get("g", 0) > LAM / 2]
        kills = [k for k in OBSERVABLE if self.detectors.get(k, {}).get("status") == "DECAY"]
        events = []
        for phase, reasons in ((WARN, warns), (KILL, kills)):
            if reasons and (phase == KILL or self.state == HEALTHY):
                self.state = phase
                events.append({"state": phase, "window_id": row["window_id"], "metrics": reasons,
                               "delta_log_loss": values["delta_log_loss"]})
        return events

    def snapshot(self) -> dict:
        return {"status": self.state,
                "log_loss_measured": self.detectors.get("delta_log_loss", {}).get("sigma") is not None,
                "metrics": {name: {key: value for key, value in stat.items() if key != "warm"}
                            for name, stat in self.detectors.items()}}
