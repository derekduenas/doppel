"""Receipt-clock capacity scenarios alongside the unchanged binding twin replay."""

from __future__ import annotations

import math
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from ..strategy.genome import Genome
from ..twin.engine import EPS, MIN_QTY, P_LAND, Quote, SimConfig, TakerOrder, WindowSim, exclusion, floor2
from ..twin.features import Features, RefStore, WindowRef
from ..twin.flowfeat import FlowFeatures, ScoreCache, rtds_hours_present, uses_intl
from ..twin.tape import NS, Book, WindowTape, iter_windows
from .markout import tier_fee_usd
from .promotion import newey_west


FEE_GRID = {"api_live_rebated": 0.02085, "api_base_0695": 0.0695, "maker_0": 0.0}
SIZES = (25, 100, 500)
EXECUTION = "l2_book_walk_vwap"


@dataclass
class Attempt:
    order: int
    kind: str
    side: str
    decided_ns: int
    target: float
    limit: float | None = None
    submitted: float = 0.0
    decision_book_ns: int | None = None
    landed_ns: int | None = None
    book_tw_ns: int | None = None
    available_depth: float | None = None
    probe_vwap: float | None = None
    filled: float = 0.0
    value: float = 0.0
    reasons: list[str] = field(default_factory=list)
    quote: Quote | None = None

    def receipt(self) -> dict:
        q = self.quote
        return {
            "order": self.order, "kind": self.kind, "side": self.side, "decided_ns": self.decided_ns,
            "decision_book_ns": self.decision_book_ns,
            "landed_ns": self.landed_ns, "book_tw_ns": self.book_tw_ns, "limit": self.limit,
            "target_qty": self.target, "submitted_qty": self.submitted, "available_depth": self.available_depth,
            "passive_sweep_probe_vwap": self.probe_vwap, "filled_qty": self.filled,
            "fill_vwap": self.value / self.filled if self.filled else None, "reasons": self.reasons,
            "quote_live_ns": q.live_ns if q else None, "quote_dead_ns": q.dead_ns if q else None,
            "quote_queue_known": q.queue_known if q and q.live_ns is not None else None,
        }


class ScenarioSim(WindowSim):
    """Keep the sealed signals, accounting and passive queue rules while widening landed IOCs."""

    def __init__(self, genome: Genome, tape: WindowTape, reference: WindowRef, cfg: SimConfig, *,
                 tier: str, size: int, features: Features | None = None,
                 can_open: Callable[[int], bool] | None = None, stood_down: bool = False):
        super().__init__(genome, tape, reference, cfg, features=features, can_open=can_open,
                         stood_down=stood_down)
        self.tier = tier
        self.size = size
        self.attempts: list[Attempt] = []
        self._iocs: dict[tuple[int, int], Attempt] = {}
        self._quote_ids: dict[int, Attempt] = {}
        self._live_depth: dict[str, dict[float, float]] = {"buy": {}, "sell": {}}
        self._used_depth: dict[str, dict[float, float]] = {"buy": {}, "sell": {}}
        # Price the desired quote before applying size risk so a clipped order counts in capacity.
        self._quote_policy = replace(genome, risk=replace(genome.risk, max_inventory=math.inf))

    def _attempt(self, kind: str, side: str, t: int, target: float) -> Attempt:
        a = Attempt(len(self.attempts), kind, side, t, target)
        self.attempts.append(a)
        return a

    def valid_book(self, t_ns: int) -> Book | None:
        book = super().valid_book(t_ns)
        if book is None or not 0 <= t_ns - book.tw <= self.max_age_ns:
            return None
        return book

    def _on_book(self, b: Book) -> None:
        # At unchanged levels a new snapshot is not a new supply of contracts. Reduce tracked
        # depletion only to the observed size on a decrease, and release it on observable removal.
        for side, rows in (("buy", b.asks), ("sell", b.bids)):
            levels = {}
            for px, qty in rows:
                if not (math.isfinite(qty) and qty > 0 and 0 < px < 1):
                    raise ValueError("invalid L2 level in scenario replay")
                levels[px] = levels.get(px, 0.0) + qty
            self._live_depth[side] = levels
            used = self._used_depth[side]
            worst = next(reversed(levels), None)
            for px in list(used):
                if px in levels and b.open:
                    used[px] = min(used[px], levels[px])
                elif b.open and self.book_ok and worst is not None:
                    if (side == "buy" and px <= worst) or (side == "sell" and px >= worst):
                        del used[px]
        super()._on_book(b)

    def _depth(self, side: str, limit: float | None) -> list[tuple[float, float]]:
        used = self._used_depth[side]
        return [(px, floor2(max(0.0, qty - used.get(px, 0.0))))
                for px, qty in self._live_depth[side].items()
                if limit is None or (px <= limit + EPS if side == "buy" else px >= limit - EPS)]

    def _taker(self, d: int, t: int, x: dict, book: Book | None) -> None:
        fired = {id(rule) for rule in self.g.taker_actions(x)}
        for i, rule in enumerate(self.g.taker):
            if id(rule) not in fired or self.inflight[i] or self.entries[i] >= rule.max_entries_per_window:
                continue
            last = self.last_attempt[i]
            if last is not None and d - last < rule.cooldown_s:
                continue
            side = "buy" if rule.side == "up" else "sell"
            a = self._attempt("entry", side, t, self.size)
            if book is None:
                a.reasons.append("no_decision_book")
                continue
            a.decision_book_ns = book.tw
            price = book.best_ask if side == "buy" else book.best_bid
            if price is None:
                a.reasons.append("empty_side")
                a.available_depth = 0.0
                continue
            sign = 1 if side == "buy" else -1
            a.limit = price + sign * rule.limit_slippage_c / 100.0
            inflight = sum(o.qty if o.side == "buy" else -o.qty for o in self._inflight_orders())
            room = self._room(side, price, self.inv + inflight)
            if self.inv == 0 and not self.can_open(t):
                room = 0.0
            a.submitted = floor2(min(self.size, room))
            if a.submitted + EPS < a.target:
                a.reasons.append("risk_capped")
            if a.submitted < MIN_QTY:
                continue
            self.last_attempt[i] = d
            self.inflight[i] = True
            self.st.taker["attempts"] += 1
            self._iocs[(i, t)] = a
            self._push(t + self.L, P_LAND, "ioc", TakerOrder(i, side, a.submitted, a.limit, t))

    def _on_ioc(self, t: int, o: TakerOrder) -> None:
        order = o
        if order.rule >= 0:
            self.inflight[order.rule] = False
            a = self._iocs.pop((order.rule, order.decided_ns))
            want = a.submitted
        else:
            self.flatten_inflight = False
            want = min(order.qty, max(0.0, self.inv if order.side == "sell" else -self.inv))
            a = self._attempt("exit", order.side, order.decided_ns, order.qty)
            a.submitted = order.qty
            if want < MIN_QTY:
                a.reasons.append("no_exposure_to_close")
                return
        a.landed_ns = t
        if t >= self.E_ns:
            a.reasons.append("closed")
            return
        book = self.valid_book(t)
        if book is None:
            a.reasons.append("no_landing_book")
            return
        a.book_tw_ns = book.tw
        levels = self._depth(order.side, order.limit)
        a.available_depth = math.fsum(qty for _, qty in levels)
        if a.available_depth + EPS < a.target:
            a.reasons.append("depth_shortfall")
        if not levels:
            a.reasons.append("limit" if self._live_depth[order.side] else "empty_side")

        used = self._used_depth[order.side]
        for price, available in levels:
            remaining = want - a.filled
            if remaining < MIN_QTY - EPS:
                break
            room = self._room(order.side, price, self.inv)
            qty = floor2(min(remaining, available, room))
            if room + EPS < min(remaining, available) and "risk_capped" not in a.reasons:
                a.reasons.append("risk_capped")
            if qty < MIN_QTY:
                continue
            used[price] = used.get(price, 0.0) + qty
            self._fill_attempt(a, t, "taker", price, qty, "ioc" if order.rule >= 0 else "flatten",
                               self._live_depth[order.side][price])
        if a.filled >= MIN_QTY:
            self.st.taker["fills"] += 1
            if order.rule >= 0:
                self.entries[order.rule] += 1

    def _maker(self, t: int, x: dict) -> None:
        rule = self.g.maker
        assert rule is not None
        want = self._quote_policy.maker_quotes(x)
        if want[0] is not None and want[1] is not None and want[1] <= want[0]:
            want = (None, None)
        fair = x["fair_up"]
        ref_c = 100 * fair if rule.quote_ref == "fair" else x.get("mid_up_c", math.nan)
        can_open = self.inv != 0 or self.can_open(t)
        book = self.valid_book(t)
        for quote_side, quote_c in zip(("bid", "ask"), want, strict=True):
            working = self._working(quote_side)
            if quote_c is None:
                if working is not None:
                    self._schedule_quote(quote_side, None, t)
                continue
            side = "buy" if quote_side == "bid" else "sell"
            price = round(quote_c) / 100.0
            size = floor2(min(self.size, self._room(side, price, self.inv))) if can_open else 0.0
            if working is not None:
                age_s = (t - working.decided_ns) / NS
                requote = abs(working.price - price) >= EPS and (
                    abs(ref_c - working.ref_c) >= rule.requote_threshold_c
                    or abs(self.inv - working.inv_at) > EPS or age_s >= rule.max_quote_age_s
                )
                if not requote:
                    if size < MIN_QTY:
                        self._schedule_quote(quote_side, None, t)
                    continue
            a = self._attempt("quote", side, t, self.size)
            a.limit = price
            a.decision_book_ns = book.tw if book is not None else None
            a.submitted = size
            if size + EPS < a.target:
                a.reasons.append("risk_capped")
            if size < MIN_QTY:
                if working is not None:
                    self._schedule_quote(quote_side, None, t)
                continue
            q = Quote(quote_side, price, size, t, ref_c, self.inv, remaining=size)
            a.quote = q
            self._quote_ids[id(q)] = a
            self._schedule_quote(quote_side, q, t)

    def _schedule_quote(self, side: str, q: Quote | None, t: int) -> None:
        pending = self.pending.get(side)
        if pending is not None and pending[1] is not None:
            self._quote_ids[id(pending[1])].reasons.append("superseded")
        super()._schedule_quote(side, q, t)

    def _on_quote_land(self, t: int, payload: tuple[str, int]) -> None:
        side, version = payload
        pending = self.pending.get(side)
        quote = pending[1] if pending and pending[0] == version else None
        super()._on_quote_land(t, payload)
        if quote is None:
            return
        a = self._quote_ids[id(quote)]
        a.landed_ns = t
        if t >= self.E_ns:
            a.reasons.append("closed")
            return
        b = self.valid_book(t)
        if b is None:
            a.reasons.append("no_landing_book")
            return
        a.book_tw_ns = b.tw
        levels = self._depth(a.side, None)
        a.available_depth = math.fsum(qty for _, qty in levels)
        remaining = a.target
        parts = []
        for px, qty in levels:
            fill = min(remaining, qty)
            parts.append(px * fill)
            remaining -= fill
            if remaining < EPS:
                break
        if remaining + EPS < a.target:
            a.probe_vwap = math.fsum(parts) / (a.target - remaining)
        if remaining > EPS:
            a.reasons.append("passive_sweep_depth_shortfall")
        if quote.live_ns is None:
            a.reasons.append("post_only")
        elif not quote.queue_known:
            a.reasons.append("unknown_passive_queue")

    def _maker_fill(self, t: int, q: Quote, f: float, via: str, ref_qty: float) -> None:
        super()._maker_fill(t, q, f, via, ref_qty)
        a = self._quote_ids[id(q)]
        self._annotate_fill(a, q.price, f)

    def _fill_attempt(self, a: Attempt, t: int, role: str, px: float, qty: float, via: str, ref_qty: float) -> None:
        self._fill(t, role, a.side, px, qty, via, ref_qty)
        self._annotate_fill(a, px, qty)

    def _annotate_fill(self, a: Attempt, px: float, qty: float) -> None:
        a.filled += qty
        a.value += px * qty
        self.fills[-1]["order"] = a.order

    def _fill(self, t: int, role: str, side: str, px: float, qty: float, via: str, ref_qty: float) -> None:
        # Only realized costs change. The sealed rule still sees its own fee-adjusted features.
        fees_before = self.fees
        super()._fill(t, role, side, px, qty, via, ref_qty)
        fee = tier_fee_usd(self.tier, px, qty) if role == "taker" else 0.0
        self.fees = fees_before + fee
        self.fills[-1]["fee"] = fee


@dataclass
class _Cell:
    tier: str
    size: int
    settled: list[tuple[int, float]] = field(default_factory=list)
    open_spans: list[tuple[int, int]] = field(default_factory=list)
    summaries: list[dict] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)

    def simulate(self, g: Genome, wt: WindowTape, ref: WindowRef, cfg: SimConfig,
                 features: Features | None, trace: Callable[[dict], None] | None) -> None:
        day = wt.start // 86400
        self.settled = [(end, net) for end, net in self.settled if (end - 1) // 86400 >= day]
        self.open_spans = [(start, end) for start, end in self.open_spans if end > wt.start * NS]
        realized = math.fsum(net for end, net in self.settled if end <= wt.start and (end - 1) // 86400 == day)

        def can_open(t: int) -> bool:
            return sum(start <= t < end for start, end in self.open_spans) < g.risk.max_open_windows

        sim = ScenarioSim(g, wt, ref, cfg, tier=self.tier, size=self.size, features=features, can_open=can_open,
                          stood_down=realized <= -g.risk.max_loss_per_day + 1e-12)
        result = sim.run()
        self.settled.append((wt.end, result["net_usd"]))
        if result["first_fill_ns"] is not None:
            self.open_spans.append((result["first_fill_ns"], wt.end * NS))
        entries = [a for a in sim.attempts if a.kind != "exit"]
        counts = Counter(reason for a in entries for reason in a.reasons)
        counts["attempted"] = len(entries)
        counts["observed"] = sum(
            a.book_tw_ns is not None or (a.landed_ns is None and a.decision_book_ns is not None)
            for a in entries
        )
        counts["filled"] = sum(a.filled + EPS >= a.target for a in entries)
        counts["partial"] = sum(MIN_QTY <= a.filled < a.target - EPS for a in entries)
        counts["no_fill"] = len(entries) - counts["filled"] - counts["partial"]
        self.counts.update(counts)
        summary = {
            "start": wt.start, "end": wt.end, "net_c": result["net_c"], "fees_usd": result["fees_usd"],
            "stood_down": result["stood_down"], "counts": dict(counts),
            "target_contracts": sum(a.target for a in entries),
            "executed_contracts": round(math.fsum(a.filled for a in entries), 6),
        }
        self.summaries.append(summary)
        if trace is not None:
            trace({"fee_tier": self.tier, "size": self.size, **summary,
                   "attempts": [a.receipt() for a in sim.attempts], "fills": sim.fills})

    def outputs(self) -> tuple[dict, dict]:
        if not self.counts["attempted"] or not self.counts["observed"]:
            raise ValueError(f"unavailable scenario, {self.tier} / {self.size}: {dict(self.counts)}")
        returns = [row["net_c"] for row in self.summaries]
        total = round(math.fsum(returns), 4)
        key = {"fee_tier": self.tier, "size": self.size}
        attempted, filled = self.counts["attempted"], self.counts["filled"]
        row = {**key, "coefficient": FEE_GRID[self.tier], "execution": EXECUTION,
               "attempted": attempted, "filled": filled, "net_c": total}
        target = sum(w["target_contracts"] for w in self.summaries)
        executed = math.fsum(w["executed_contracts"] for w in self.summaries)
        return row, {**key, "counts": dict(self.counts), "fill_rate": filled / attempted,
                     "contract_completion": executed / target, "mean_net_c": total / len(returns),
                     "hac": newey_west(returns), "windows": self.summaries}


def replay_scenarios(data: Path, lab: Path | None, genome: Genome, start: int, end: int, cfg: SimConfig,
                     windows: list[tuple[int, int]], *,
                     trace: Callable[[dict], None] | None = None) -> tuple[list[dict], dict]:
    """Stream the canonical eligible panel once, with independent risk and fills for each cell."""
    expected = set(windows)
    if (not windows or len(expected) != len(windows) or sorted(expected) != windows
            or any(not start <= a < b <= end for a, b in windows)
            or any(a[1] > b[0] for a, b in zip(windows, windows[1:]))):
        raise ValueError("scenario panel must be ordered, nonoverlapping and inside the bound range")
    if genome.universe.venue != "pmus":
        raise ValueError("the bound scenario executor supports PM US only")

    store = RefStore(data)
    for asset in genome.universe.assets:
        store.load_boundaries(asset, start - 1, end)
    if uses_intl(genome) and lab is None:
        raise ValueError("the scenario executor needs the frozen lab token map for intl features")
    cache = ScoreCache(data, lab) if uses_intl(genome) and lab is not None else None
    cells = [_Cell(tier, size) for tier in FEE_GRID for size in SIZES]
    visits: list[tuple[int, int]] = []
    for wt in iter_windows(data, genome.universe.assets, genome.universe.horizons, start, end,
                           depth=sys.maxsize):
        pair = (wt.start, wt.end)
        if pair not in expected:
            continue
        if cache is not None and not rtds_hours_present(data, wt.start - 120, wt.end):
            wt.stats["rtds_missing"] = True
        ref = store.window_ref(wt.asset, wt.start, wt.end, genome.fair.vol_window_s)
        why = exclusion(genome, wt, ref, cfg.min_trade_coverage, cfg.max_inferred_share, cfg.max_unidentified_share)
        if why is not None:
            raise ValueError(f"canonical window {pair} unavailable for L2 scenarios: {why}")
        features = FlowFeatures(genome, ref, cfg.feature_config(), data, cache) if cache is not None else None
        for cell in cells:
            cell.simulate(genome, wt, ref, cfg, features, trace)
        visits.append(pair)
    if visits != windows:
        raise ValueError("L2 scenario tape does not reproduce the complete bound window panel")
    rows, diagnostics = zip(*(cell.outputs() for cell in cells), strict=True)
    return list(rows), {"schema": "doppel.shadow-scenarios/v1", "venue": "pmus",
                        "clock": "local_receive_ns", "execution": EXECUTION,
                        "promo_valid_through_utc": "2026-12-31", "cells": list(diagnostics)}
