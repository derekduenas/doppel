"""The twin's execution engine: one PM US Up/Down window at a time, event by event.

Clock: everything is OUR receive time (ns). Events at equal times run in this order: book gaps,
book snapshots, trade prints, order/cancel landings, decisions. So a decision at second d sees every
message received at or before d, and a print received at the instant our order lands cannot fill it.

Venue semantics (PM US, one YES = Up instrument): buy Up = BUY YES at the ask; buy Down = SELL YES
at the bid (a Down share costs 1 - bid). Settlement pays $1 per YES if Up, else 0. Inventory is net
YES contracts (Down counts negative). Fees per fill: coef * p * (1-p) * qty, rounded to the cent
half-even when ``fee_rounding == "pmus_cent_half_even"`` (taker coef for takes, maker coef for
resting fills).

Taker (IOC): a rule fires at decision d on the book received <= d. The order lands at d + L and
trades against the book received <= d + L, only at that book's best level, for at most the displayed
size there (less what we already took from the same unchanged level). If the best price is worse than
the decision price by more than ``limit_slippage_c``, nothing fills. Cooldown, max entries, the
in-flight order, inventory and notional caps all apply.

Maker (post-only, pessimistic queue, PERELMAN rules plus two stricter ones):
- Placement, replacement and cancel all land L after the decision. A replaced quote stays live
  until its cancel lands. A quote whose price would cross the book when it lands is rejected
  (post-only), never filled. A quote cannot be placed while the book is unknown.
- Queue ahead = displayed size at our price in the book as of landing. Strictly better than the
  best price -> no queue. An empty level inside the visible book -> no queue. A level beyond the
  visible depth -> unknown queue (prints AT our price never fill it).
- A print received before our quote is live, or executed (venue tradeTime) before it, cannot fill
  it. Prints AT our price first burn the queue ahead; only the remainder fills us. Cancellations
  never advance the queue. A print THROUGH our price (sell below our bid / buy above our ask)
  proves our level was exhausted: the queue is zero and we fill up to the print's size.
- Stricter than PERELMAN: (1) if the recorded book shows the opposite side at or through our live
  quote with no print (a marketable order that, with us present, would have hit us), we fill at our
  price from the newly crossing size: these are the adverse fills a quote-only replay would miss.
  (2) A print executed before our cancel landed but received after it still fills the cancelled
  quote (1 s grace).
- Fills are clipped to the inventory and notional room left. Our own fills never change the
  recorded book or prints (no market impact).

Risk: ``max_inventory`` caps |net YES|; ``max_notional_per_window`` caps the cash put at risk by
fills that open or extend exposure (p per YES bought, 1 - p per YES sold short); the runner stands a
window down when the UTC day's settled P&L before it is <= -``max_loss_per_day``, and blocks a flat
window from opening while ``max_open_windows`` earlier windows hold positions (approximate across
overlapping horizons: windows are simulated one at a time in start order).
"""

from __future__ import annotations

import bisect
import heapq
import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Callable, Iterable

from ..strategy.genome import Genome
from .features import FeatureConfig, Features, WindowRef
from .tape import NS, Book, Trade, WindowTape

EPS = 1e-9
MIN_QTY = 0.01
DYING_GRACE_NS = NS
# event priorities at equal timestamps
P_GAP, P_BOOK, P_TRADE, P_LAND, P_DECIDE = 0, 1, 2, 3, 4


@dataclass(frozen=True)
class SimConfig:
    latency_ms: float = 250.0           # decision -> order / cancel live at the venue
    brti_delay_s: int = 2               # BRTI print for second s is known at s + this (E29: receipt
                                        # lag p50 0.65 s, p95 1.04 s, p99 10.4 s; 1 s was optimistic)
    fair_lag_s: int = 0                 # fair-v1 alignment lag (E19 measured 0)
    rest_max_age_s: float = 3.0         # diagnostics only (REST is stale): older snapshot = no book
    markouts_s: tuple[int, ...] = (1, 5, 30)
    depth_levels: int = 10              # book levels kept per side
    max_inferred_share: float = 0.4     # E31: at most this share of volume may be inferred (unprinted)
    min_trade_coverage: float = 0.9     # maker windows need this share of sharesTraded seen as prints
                                        # PLUS inferred unprinted executions (E31);
                                        # below 0.9 the run is DIAGNOSTIC (missed prints are mostly
                                        # adverse fills, so low coverage flatters a maker)

    @property
    def latency_ns(self) -> int:
        return int(round(self.latency_ms * 1e6))

    def feature_config(self) -> FeatureConfig:
        return FeatureConfig(brti_delay_s=self.brti_delay_s, fair_lag_s=self.fair_lag_s)


def fee_usd(coef: float, price: float, qty: float, rounding: str = "pmus_cent_half_even") -> float:
    """Fee for ONE fill: coef * p * (1-p) * qty dollars, rounded to the cent half-even (exact)."""
    if coef == 0 or qty <= 0:
        return 0.0
    p = Decimal(repr(price))
    f = Decimal(repr(coef)) * p * (1 - p) * Decimal(repr(qty))
    if rounding == "pmus_cent_half_even":
        f = f.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return float(f)


def floor2(q: float) -> float:
    return math.floor(q * 100 + 1e-6) / 100 if q > 0 else 0.0


def queue_ahead(book: Book, side: str, price: float) -> tuple[float, bool]:
    """(contracts ahead of a new quote at ``price``, known?) from the book as of landing."""
    levels = book.bids if side == "bid" else book.asks
    if not levels:
        return 0.0, True
    best = levels[0][0]
    if (side == "bid" and price > best + EPS) or (side == "ask" and price < best - EPS):
        return 0.0, True
    for px, q in levels:
        if abs(px - price) < EPS:
            return q, True
    worst = levels[-1][0]
    if (side == "bid" and price > worst + EPS) or (side == "ask" and price < worst - EPS):
        return 0.0, True          # an empty level inside the visible book
    return math.inf, False        # beyond the visible depth: unknown


@dataclass
class Quote:
    side: str               # "bid" = resting BUY YES, "ask" = resting SELL YES
    price: float            # dollars
    size: float
    decided_ns: int
    ref_c: float
    inv_at: float
    remaining: float = 0.0
    live_ns: int | None = None
    dead_ns: int | None = None
    queue: float = 0.0
    queue_known: bool = True
    cross_seen: float = 0.0
    filled: float = 0.0


@dataclass
class TakerOrder:
    rule: int               # index into genome.taker, -1 = flatten
    side: str               # "buy" | "sell" (YES)
    qty: float
    limit: float | None
    decided_ns: int


@dataclass
class _Stats:
    taker: dict = field(default_factory=lambda: {"attempts": 0, "fills": 0, "slippage_miss": 0,
                                                 "no_book": 0, "depth_capped": 0, "closed": 0,
                                                 "risk_blocked": 0, "empty_side": 0})
    maker: dict = field(default_factory=lambda: {"placed": 0, "post_only_rejects": 0, "no_book_rejects": 0,
                                                 "beyond_depth": 0, "no_fair": 0, "cancels": 0,
                                                 "closed": 0, "self_cross": 0, "paused_s": 0,
                                                 "pause_episodes": 0})


class WindowSim:
    def __init__(self, g: Genome, wt: WindowTape, ref: WindowRef, cfg: SimConfig = SimConfig(),
                 features: Features | None = None, can_open: Callable[[int], bool] | None = None,
                 stood_down: bool = False):
        self.g, self.wt, self.ref, self.cfg = g, wt, ref, cfg
        self.L = cfg.latency_ns
        self.S, self.E = wt.start, wt.end
        self.E_ns = wt.end * NS
        self.rest = wt.source == "rest"
        self.max_age_ns = int(cfg.rest_max_age_s * NS)
        self.feats = features or Features(g, ref, cfg.feature_config())
        self.can_open = can_open or (lambda t: True)
        self.stood_down = stood_down
        # venue state as replayed
        self.book: Book | None = None
        self.book_ok = False
        # own state
        self.inv = 0.0
        self.cash = 0.0
        self.fees = 0.0
        self.notional = 0.0
        self.max_abs_inv = 0.0
        self.fills: list[dict] = []
        self.inv_path: list[list[float]] = []
        self.first_fill_ns: int | None = None
        self.live: dict[str, Quote | None] = {"bid": None, "ask": None}
        self.pending: dict[str, tuple[int, Quote | None]] = {}
        self.ver = {"bid": 0, "ask": 0}
        self.dying: list[Quote] = []
        self.quotes: list[Quote] = []
        self.entries = [0] * len(g.taker)
        self.last_attempt: list[int | None] = [None] * len(g.taker)
        self.inflight = [False] * len(g.taker)
        self.flatten_inflight = False
        self.flattening = False
        self.taken: dict[tuple[str, float], tuple[float, float]] = {}
        self._was_paused = False
        self.st = _Stats()
        self.decisions = 0
        self._heap: list = []
        self._seq = 0
        self._book_tws: list[int] = []

    # ------------------------------------------------------------------ plumbing
    def _push(self, t: int, prio: int, kind: str, payload) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (t, prio, self._seq, kind, payload))

    def valid_book(self, t_ns: int) -> Book | None:
        b = self.book
        if b is None or not self.book_ok or not b.open:
            return None
        if self.rest and t_ns - b.tw > self.max_age_ns:
            return None
        return b

    def run(self) -> dict:
        data: list = []
        for t in self.wt.gaps:
            data.append((t, P_GAP, len(data), "gap", None))
        for b in self.wt.books:
            data.append((b.tw, P_BOOK, len(data), "book", b))
        for tr in self.wt.trades:
            data.append((tr.tw, P_TRADE, len(data), "trade", tr))
        data.sort(key=lambda e: (e[0], e[1], e[2]))
        for d in range(self.S, self.E):
            self._push(d * NS, P_DECIDE, "decide", d)
        i = 0
        while i < len(data) or self._heap:
            if self._heap and (i >= len(data) or self._heap[0][:2] < data[i][:2]):
                t, _, _, kind, payload = heapq.heappop(self._heap)
            else:
                t, _, _, kind, payload = data[i]
                i += 1
            if kind == "book":
                self._on_book(payload)
            elif kind == "gap":
                self.book_ok = False
            elif kind == "trade":
                self._on_trade(payload)
            elif kind == "decide":
                self._on_decide(payload)
            elif kind == "ioc":
                self._on_ioc(t, payload)
            elif kind == "quote":
                self._on_quote_land(t, payload)
        for side in ("bid", "ask"):
            q = self.live[side]
            if q is not None and q.dead_ns is None:
                q.dead_ns = self.E_ns
        return self._result()

    # ------------------------------------------------------------------ market data
    def _on_book(self, b: Book) -> None:
        self.book, self.book_ok = b, True
        if not b.open:
            return
        for side in ("bid", "ask"):
            q = self.live[side]
            if q is None or q.remaining <= EPS or q.live_ns is None or q.live_ns >= b.tw:
                continue
            if b.tw >= self.E_ns:
                continue
            if side == "bid":
                crossing = sum(qty for px, qty in b.asks if px <= q.price + EPS)
            else:
                crossing = sum(qty for px, qty in b.bids if px >= q.price - EPS)
            new = crossing - q.cross_seen
            q.cross_seen = crossing
            if new <= EPS:
                continue
            q.queue = 0.0   # the opposite side reached our price: nothing at our level is ahead
            f = floor2(min(q.remaining, new, self._room("buy" if side == "bid" else "sell", q.price, self.inv)))
            if f >= MIN_QTY:
                self._maker_fill(b.tw, q, f, via="book_cross", ref_qty=new)

    def _on_trade(self, tr: Trade) -> None:
        tv = tr.t_venue
        if tv >= self.E_ns:
            return
        side = "bid" if tr.aggressor == "sell" else "ask"
        self.dying = [q for q in self.dying if q.dead_ns is not None and tr.tw - q.dead_ns <= DYING_GRACE_NS]
        cands = [q for q in self.dying if q.side == side and q.dead_ns is not None and tv < q.dead_ns]
        cur = self.live[side]
        if cur is not None:
            cands.append(cur)
        avail = tr.qty
        for q in cands:
            if avail <= EPS:
                break
            if q.remaining <= EPS or q.live_ns is None or not tv > q.live_ns:
                continue
            if side == "bid":
                at, through = abs(tr.price - q.price) < EPS, tr.price < q.price - EPS
            else:
                at, through = abs(tr.price - q.price) < EPS, tr.price > q.price + EPS
            if not (at or through):
                continue
            if through:
                q.queue = 0.0
            else:
                burn = min(q.queue, avail)
                q.queue -= burn
                avail -= burn
            f = floor2(min(q.remaining, avail, self._room("buy" if side == "bid" else "sell", q.price, self.inv)))
            if f >= MIN_QTY:
                self._maker_fill(tr.tw, q, f, via="inferred" if (tr.id or "").startswith("inferred:") else "trade",
                                 ref_qty=tr.qty)
                avail -= f

    # ------------------------------------------------------------------ decisions
    def _on_decide(self, d: int) -> None:
        t = d * NS
        if self.stood_down:
            return
        book = self.valid_book(t)
        x = self.feats.at(d, book, self.inv)
        self.decisions += 1
        ex = self.g.exit
        if (not ex.hold_to_settlement and ex.flatten_tte_s is not None and self.E - d <= ex.flatten_tte_s):
            self._flatten(t)
            return
        if self.g.mode == "taker":
            self._taker(d, t, x, book)
        elif self.g.mode == "maker":
            self._maker(t, x)

    def _room(self, side: str, px: float, inv: float) -> float:
        """Max contracts a fill may add on ``side`` under the inventory and notional caps."""
        r = self.g.risk
        if side == "buy":
            inv_room, closing, cost = r.max_inventory - inv, max(0.0, -inv), px
        else:
            inv_room, closing, cost = r.max_inventory + inv, max(0.0, inv), 1.0 - px
        notional_room = max(0.0, r.max_notional_per_window - self.notional) / max(cost, 1e-9)
        return max(0.0, min(inv_room, closing + notional_room))

    def _taker(self, d: int, t: int, x: dict, book: Book | None) -> None:
        fired = {id(r) for r in self.g.taker_actions(x)}
        if not fired:
            return
        for i, r in enumerate(self.g.taker):
            if id(r) not in fired or self.inflight[i]:
                continue
            la = self.last_attempt[i]
            if la is not None and d - la < r.cooldown_s:
                continue
            if self.entries[i] >= r.max_entries_per_window:
                continue
            ts = self.st.taker
            if book is None:
                ts["no_book"] += 1
                continue
            side = "buy" if r.side == "up" else "sell"
            px = book.best_ask if side == "buy" else book.best_bid
            if px is None:
                ts["empty_side"] += 1
                continue
            inflight = sum((o.qty if o.side == "buy" else -o.qty) for o in self._inflight_orders())
            qty = floor2(min(r.size, self._room(side, px, self.inv + inflight)))
            if qty < MIN_QTY or (self.inv == 0 and not self.can_open(t)):
                ts["risk_blocked"] += 1
                continue
            slip = r.limit_slippage_c / 100.0
            limit = px + slip if side == "buy" else px - slip
            self.last_attempt[i] = d
            self.inflight[i] = True
            ts["attempts"] += 1
            self._push(t + self.L, P_LAND, "ioc", TakerOrder(i, side, qty, limit, t))

    def _inflight_orders(self) -> list[TakerOrder]:
        return [e[4] for e in self._heap if e[3] == "ioc"]

    def _flatten(self, t: int) -> None:
        if not self.flattening:
            self.flattening = True
        for side in ("bid", "ask"):
            if self._working(side) is not None:
                self._schedule_quote(side, None, t)
        if abs(self.inv) >= MIN_QTY and not self.flatten_inflight:
            side = "sell" if self.inv > 0 else "buy"
            self.flatten_inflight = True
            self.st.taker["attempts"] += 1
            self._push(t + self.L, P_LAND, "ioc", TakerOrder(-1, side, abs(self.inv), None, t))

    def _on_ioc(self, t: int, o: TakerOrder) -> None:
        ts = self.st.taker
        if o.rule >= 0:
            self.inflight[o.rule] = False
        else:
            self.flatten_inflight = False
        if t >= self.E_ns:
            ts["closed"] += 1
            return
        book = self.valid_book(t)
        if book is None:
            ts["no_book"] += 1
            return
        levels = book.asks if o.side == "buy" else book.bids
        if not levels:
            ts["empty_side"] += 1
            return
        px, disp = levels[0]
        if o.limit is not None and ((o.side == "buy" and px > o.limit + EPS)
                                    or (o.side == "sell" and px < o.limit - EPS)):
            ts["slippage_miss"] += 1
            return
        # Our fills never change the recorded book, so liquidity we took at this best price stays taken
        # until the best price moves (audit A2-4: a size change used to reset it to the full display).
        prev = self.taken.get((o.side, 0.0))
        used = prev[1] if prev is not None and abs(prev[0] - px) < EPS else 0.0
        avail = disp - used
        want = o.qty if o.rule >= 0 else abs(self.inv)
        qty = floor2(min(want, max(0.0, avail), self._room(o.side, px, self.inv)))
        if o.rule < 0:
            qty = min(qty, floor2(abs(self.inv)))
        if qty < MIN_QTY:
            ts["depth_capped" if avail < want else "risk_blocked"] += 1
            return
        if qty < want - EPS and avail < want:
            ts["depth_capped"] += 1
        self.taken[(o.side, 0.0)] = (px, used + qty)
        self._fill(t, "taker", o.side, px, qty, via="ioc" if o.rule >= 0 else "flatten", ref_qty=disp)
        ts["fills"] += 1
        if o.rule >= 0:
            self.entries[o.rule] += 1

    # ------------------------------------------------------------------ maker
    def _working(self, side: str) -> Quote | None:
        if side in self.pending:
            return self.pending[side][1]
        q = self.live[side]
        return q if q is not None and q.remaining > EPS else None

    def _schedule_quote(self, side: str, q: Quote | None, t: int) -> None:
        self.ver[side] += 1
        self.pending[side] = (self.ver[side], q)
        self._push(t + self.L, P_LAND, "quote", (side, self.ver[side]))

    def _maker(self, t: int, x: dict) -> None:
        m = self.g.maker
        if m is None:
            return
        fair = x.get("fair_up", float("nan"))
        if fair != fair:
            want: tuple[float | None, float | None] = (None, None)
            self.st.maker["no_fair"] += 1
        elif self.inv == 0 and not self.can_open(t):
            want = (None, None)
        else:
            paused = self.g.maker_paused(x)
            self.st.maker["paused_s"] += int(paused)
            self.st.maker["pause_episodes"] += int(paused and not self._was_paused)
            self._was_paused = paused
            want = self.g.maker_quotes(x)
            if want[0] is not None and want[1] is not None and want[1] <= want[0]:
                want = (None, None)      # own quotes would cross: quote neither side
                self.st.maker["self_cross"] += 1
        ref_c = 100.0 * fair if m.quote_ref == "fair" else x.get("mid_up_c", float("nan"))
        for side, w in (("bid", want[0]), ("ask", want[1])):
            working = self._working(side)
            if w is None:
                if working is not None:
                    self._schedule_quote(side, None, t)
                continue
            price = round(float(w)) / 100.0
            size = floor2(min(m.size, self._room("buy" if side == "bid" else "sell", price, self.inv)))
            if size < MIN_QTY:
                if working is not None:
                    self._schedule_quote(side, None, t)
                continue
            if working is not None and abs(working.price - price) < EPS:
                continue
            if working is not None:
                age_s = (t - working.decided_ns) / NS
                if not (abs(ref_c - working.ref_c) >= m.requote_threshold_c
                        or abs(self.inv - working.inv_at) > EPS or age_s >= m.max_quote_age_s):
                    continue
            self._schedule_quote(side, Quote(side=side, price=price, size=size, decided_ns=t, ref_c=ref_c,
                                             inv_at=self.inv, remaining=size), t)

    def _on_quote_land(self, t: int, payload: tuple[str, int]) -> None:
        side, ver = payload
        pend = self.pending.get(side)
        if pend is None or pend[0] != ver:
            return                       # superseded by a later decision
        del self.pending[side]
        q = pend[1]
        old = self.live[side]
        if old is not None:
            old.dead_ns = t
            self.dying.append(old)
            self.live[side] = None
            self.st.maker["cancels"] += 1
        if q is None:
            return
        ms = self.st.maker
        if t >= self.E_ns:
            ms["closed"] += 1
            return
        book = self.valid_book(t)
        if book is None:
            ms["no_book_rejects"] += 1
            return
        if side == "bid" and book.best_ask is not None and q.price >= book.best_ask - EPS:
            ms["post_only_rejects"] += 1
            return
        if side == "ask" and book.best_bid is not None and q.price <= book.best_bid + EPS:
            ms["post_only_rejects"] += 1
            return
        q.queue, q.queue_known = queue_ahead(book, side, q.price)
        if not q.queue_known:
            ms["beyond_depth"] += 1
        q.live_ns = t
        self.live[side] = q
        self.quotes.append(q)
        ms["placed"] += 1

    def _maker_fill(self, t: int, q: Quote, f: float, via: str, ref_qty: float) -> None:
        q.remaining -= f
        q.filled += f
        self._fill(t, "maker", "buy" if q.side == "bid" else "sell", q.price, f, via=via, ref_qty=ref_qty)

    # ------------------------------------------------------------------ accounting
    def _fill(self, t: int, role: str, side: str, px: float, qty: float, via: str, ref_qty: float) -> None:
        c = self.g.costs
        coef = c.taker_fee_coef if role == "taker" else c.maker_fee_coef
        fee = fee_usd(coef, px, qty, c.fee_rounding)
        buy = side == "buy"
        closing = min(qty, max(0.0, -self.inv)) if buy else min(qty, max(0.0, self.inv))
        self.notional += (qty - closing) * (px if buy else 1.0 - px)
        self.cash += -px * qty if buy else px * qty
        self.inv += qty if buy else -qty
        self.fees += fee
        self.max_abs_inv = max(self.max_abs_inv, abs(self.inv))
        if self.first_fill_ns is None:
            self.first_fill_ns = t
        rel = round((t - self.S * NS) / NS, 3)
        self.fills.append({"t": rel, "t_ns": t, "role": role, "side": side, "px": px, "qty": qty,
                           "fee": fee, "via": via, "ref_qty": ref_qty})
        self.inv_path.append([rel, round(self.inv, 6)])

    def _mid_c(self, t: int, outcome: int) -> float | None:
        if t >= self.E_ns:
            return 100.0 * outcome
        books = self.wt.books
        j = bisect.bisect_right(self._book_tws, t) - 1
        if j < 0:
            return None
        b = books[j]
        k = bisect.bisect_right(self.wt.gaps, t) - 1
        if k >= 0 and self.wt.gaps[k] > b.tw:
            return None
        if not b.open or b.mid is None or (self.rest and t - b.tw > self.max_age_ns):
            return None
        return 100.0 * b.mid

    def _result(self) -> dict:
        wt, ref = self.wt, self.ref
        outcome = ref.outcome
        assert outcome is not None
        self._book_tws = [b.tw for b in wt.books]
        for f in self.fills:
            mo = {}
            for k in self.cfg.markouts_s:
                mid = self._mid_c(f["t_ns"] + k * NS, outcome)
                if mid is None:
                    mo[str(k)] = None
                else:
                    pc = 100.0 * f["px"]
                    mo[str(k)] = round(mid - pc if f["side"] == "buy" else pc - mid, 4)
            f["markout_c"] = mo
        adverse = {}
        for k in self.cfg.markouts_s:
            pairs = [(f["markout_c"][str(k)], f["qty"]) for f in self.fills
                     if f["role"] == "maker" and f["markout_c"][str(k)] is not None]
            den = sum(w for _, w in pairs)
            adverse[str(k)] = round(sum(v * w for v, w in pairs) / den, 4) if den else None
        gross = self.cash + self.inv * outcome
        net = gross - self.fees
        contracts = sum(f["qty"] for f in self.fills)
        quoted = sum(q.size for q in self.quotes)
        mfill = sum(q.filled for q in self.quotes)
        live_s = sum(((q.dead_ns or self.E_ns) - (q.live_ns or 0)) / NS for q in self.quotes)
        return {
            **window_head(wt, ref), "eligible": True, "excluded": None, "stood_down": self.stood_down,
            "decisions": self.decisions, "n_fills": len(self.fills), "contracts": round(contracts, 6),
            "contracts_taker": round(sum(f["qty"] for f in self.fills if f["role"] == "taker"), 6),
            "contracts_maker": round(sum(f["qty"] for f in self.fills if f["role"] == "maker"), 6),
            "fees_usd": round(self.fees, 6), "gross_usd": round(gross, 6), "net_usd": round(net, 6),
            "net_c": round(100.0 * net, 4), "notional_usd": round(self.notional, 6),
            "final_inventory": round(self.inv, 6), "max_abs_inventory": round(self.max_abs_inv, 6),
            "inventory_path": self.inv_path, "fills": [{k: v for k, v in f.items() if k != "t_ns"} for f in self.fills],
            "first_fill_ns": self.first_fill_ns,
            "taker": dict(self.st.taker),
            "maker": {**self.st.maker, "quoted_qty": round(quoted, 6), "fill_qty": round(mfill, 6),
                      "fill_rate": round(mfill / quoted, 6) if quoted else None,
                      "quotes_with_fill": sum(1 for q in self.quotes if q.filled > 0),
                      "quote_live_s": round(live_s, 3),
                      "mean_quote_live_s": round(live_s / len(self.quotes), 3) if self.quotes else None,
                      "adverse_selection_c": adverse},
        }


def window_head(wt: WindowTape, ref: WindowRef) -> dict:
    return {"slug": wt.slug, "asset": wt.asset, "horizon": wt.horizon, "start": wt.start, "end": wt.end,
            "source": wt.source, "stream": wt.stream, "tape": wt.stats, "ptb": ref.ptb,
            "settle": ref.settle, "outcome": ref.outcome}


MIN_TRADE_COVERAGE = 0.9   # maker windows need >= 90% of the venue's traded volume seen as prints


def exclusion(g: Genome, wt: WindowTape, ref: WindowRef, min_cov: float = MIN_TRADE_COVERAGE,
              max_inferred: float = 0.4) -> str | None:
    """Why a window cannot count toward a verdict (None = it counts).

    A maker is only scored where the trade feed is verifiably complete: missed prints are missed
    (mostly adverse) fills, and an unfillable window would drag a losing maker toward CONTINUE. So
    for maker genomes a window with any book/trade-feed gap inside [S, E) is ``feed_gap``, and one
    whose prints cover < MIN_TRADE_COVERAGE of the venue's sharesTraded change is
    ``incomplete_trade_feed``."""
    if wt.source == "rest":
        return "stale_rest_diagnostic"   # REST books are CDN-stale: diagnostics only, never a verdict
    if wt.source != "ws":
        return "no_ws_coverage"
    if ref.outcome is None:
        return "unsettled"
    if wt.stats.get("rtds_missing"):
        return "no_rtds"   # audit A3-2: a missing intl identity hour must not look like zero flow
    if g.mode == "maker":
        if not wt.has_trade_feed:
            return "no_trade_feed"
        if any(wt.start * NS <= t < wt.end * NS for t in wt.gaps) or (wt.stats.get("trade_gap_events") or 0) > 0:
            return "feed_gap"
        cov = wt.stats.get("trade_coverage_with_inferred", wt.stats.get("trade_coverage"))
        if (wt.stats.get("inferred_share") or 0.0) > max_inferred:
            return "unprinted_volume_high"
        if cov is None and (wt.stats.get("venue_volume") or 0) > 0:
            return "unknown_trade_coverage"   # audit A2-8: unknown coverage used to pass
        if cov is not None and cov < min_cov:
            return "incomplete_trade_feed"
    return None


def run_windows(g: Genome, items: Iterable[tuple[WindowTape, WindowRef]], cfg: SimConfig = SimConfig(),
                features_factory: Callable[[Genome, WindowRef, SimConfig], Features] | None = None) -> list[dict]:
    """Simulate windows in the given (chronological) order with the cross-window risk rules."""
    out: list[dict] = []
    settled: list[tuple[int, float]] = []          # (end, net_usd) of simulated windows
    spans: list[tuple[int, int]] = []              # (first fill ns, end ns) of windows with fills
    for wt, ref in items:
        why = exclusion(g, wt, ref, cfg.min_trade_coverage, cfg.max_inferred_share)
        if why == "stale_rest_diagnostic" and ref.outcome is not None:
            res = WindowSim(g, wt, ref, cfg, features=features_factory(g, ref, cfg) if features_factory else None).run()
            out.append({**res, "eligible": False, "excluded": why})
            continue
        if why is not None:
            out.append({**window_head(wt, ref), "eligible": False, "excluded": why})
            continue
        day = wt.start // 86400
        settled = [(e, n) for e, n in settled if (e - 1) // 86400 >= day]
        spans = [(a, b) for a, b in spans if b > wt.start * NS]
        realized = sum(n for e, n in settled if e <= wt.start and (e - 1) // 86400 == day)
        stood_down = realized <= -g.risk.max_loss_per_day + 1e-12

        def can_open(t: int, sp=tuple(spans)) -> bool:
            return sum(1 for a, b in sp if a <= t < b) < g.risk.max_open_windows

        feats = features_factory(g, ref, cfg) if features_factory else None
        res = WindowSim(g, wt, ref, cfg, features=feats, can_open=can_open, stood_down=stood_down).run()
        out.append(res)
        settled.append((wt.end, res["net_usd"]))
        if res["first_fill_ns"] is not None:
            spans.append((res["first_fill_ns"], wt.end * NS))
    return out
