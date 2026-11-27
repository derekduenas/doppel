"""Order replay: the twin's maker FILL MODEL against real resting orders (intl CLOB).

Every row of ``doppel-ordermatch``'s ``orders.jsonl`` is a hash-verified maker order located in
the public intl book: known price, size, placement event, real fills (quantity from chain, time
from the tx-hash print) and, when classified, its real end. Each one is replayed as a single
quote through the twin's own maker code and the twin's fills are compared with what happened.

SELECTION (read every number with this): only orders that really filled at least once are
visible on chain. So the order comparison measures the fill model's bias CONDITIONAL ON A REAL
FILL (how much, how early, given that the order did trade). It is NOT a fill probability and says
nothing about quotes that never traded. The only view of those is the fingerprint candidates
(``quotes.jsonl``), which are an attribution ESTIMATE, not identified orders.

HOW THE TWIN IS DRIVEN (no re-implementation, ``twin/engine.py`` untouched)
``QuoteSim`` subclasses ``WindowSim`` and replaces only what needs a genome: the constructor,
``_room`` (no inventory / notional cap) and ``_fill`` (records the fill instead of booking P&L and
fees). The fill decisions are the engine's unchanged ``_on_book`` (book-cross fills),
``_on_trade`` (queue burn, at / through prints) and ``_on_quote_land`` (the cancel). ``run()``,
decisions, features, latency and the post-only check are not used: the real order was accepted,
and latency is not what is tested.

ADAPTER (intl tape -> twin events, per asset of a live quote)
- Clocks: records arrive in capture order. Venue milliseconds decide whether a public print
  executed within the inferred lifetime. Receipt nanoseconds bound delayed print reconciliation.
- ``Book``: one full-depth snapshot (bids = BUY levels, asks = SELL levels) after each message
  that changed the asset, only while a quote is live on it.
- ``Trade``: one per print, with separate receipt and venue times. The aggressor is the
  print's taker ``side``. A print on the MATE asset is mirrored into the quote's asset (price
  1 - p, aggressor flipped): the book is mirrored, a bid on A is hit by buyers of B. If the same
  execution is printed on both assets (same tx, mirrored price, same size, mirrored side) the
  second copy is dropped and counted (``mirror_dup_prints``).
- With decoded ordertape, reconcile the public transaction's condition, token view, aggressor
  and total quantity to all signed maker legs, normalizing mirrored contracts. Feed conserved
  levels best price first, once per transaction. A conflicting partial frame is withheld.
  Unavailable or invalid calls retain the public estimate and flag live same-side exposure.
  The fully allocated bracket requires every own fill allocated and delivered plus zero such
  exposure. Calldata attribution is retrospective and is never an input to a strategy.
- ASSUMED, and checked by ``adapter_gate``: the print ``side`` is the taker's side on the print's
  own asset. Each real fill's tx-hash print must route to the order's price with the aggressor
  that hits its side; if ``routed_share`` is low the numbers below measure the adapter, not the
  fill model.

REPLAY RULES
- Live at the matched placement event (same token, price, side, venue ms and receive ns as
  ordermatch reported; nearest size), not at created_ms + latency. A print in the placement's own
  record cannot fill it (the engine's rule for a print at the landing instant).
- Queue ahead, ``pessimistic`` (the twin's convention): the level's size just before the
  placement increase. ``optimistic`` bound: 0. Cancels ahead never advance the queue (engine).
- End: the first relevant event stamped after ordermatch's inclusive ``end_ms`` freezes the
  quote's book state. For the next DYING_GRACE_NS of receipt time the adapter can deliver
  executions stamped inside the original lifetime. Only eligible trades advance the frozen queue.
  Reset and tape end discard pending reconciliation. A filled order's inferred end can come
  from its own last fill. This replay is conditional on that observed boundary.

DEFINITIONS (per order and queue mode; shares)
- real = chain-filled shares; twin = shares the twin filled; error = twin - real.
- missed: real > 0.02 and twin filled nothing. phantom_shares: max(0, twin - real) when above
  0.02 (the twin filled beyond the real quantity). A twin fill AFTER the real order was gone
  cannot happen for ``filled`` / ``cancelled`` orders (the quote is cancelled then), so for them
  phantom = over-fill while the real order was live.
- The headline requires a qualified placement and a known end inside uninterrupted tape.
  ``unknown`` orders are reported apart: their chain fills are a lower bound and the quote may
  outlive the order, so their excess is only a POSSIBLE phantom. Orders with
  ``fill_before_placement`` (a proven wrong match) are not replayed.
- Ceiling: a ``filled`` order has real = size, so twin / real <= 1 there in BOTH modes; over-fill
  can only show on ``cancelled`` orders. Read the ratio per outcome, not only pooled.
- Bracket: with the right placement, FIFO and price-level print sizes,
  pessimistic <= real <= optimistic. A pessimistic
  over-fill or an optimistic under-fill (``bracket_violations``) is therefore a data, adapter or
  match problem, not model bias. ``adapter_gate.delivered_share`` says how many of the real fills'
  own prints reached the quote within its venue lifetime. The report separates deliveries
  received during the grace period from those made before expiry was observed.
  The report retains every violation and gives its placement candidate count.
- ratio = sum(twin) / sum(real); CI = bootstrap over markets (the market of the placement event
  = one window), ratio of sums.
- Twin fill source: ``trade`` or ``book_cross``. A real resting order keeps the opposite side
  from reaching its price, so ``book_cross`` fills should be ~0 here; any is a diagnostic (wrong
  match or lifetime), and the engine's book-cross rule is NOT validated by this replay.
- Never-filled quotes (ESTIMATE): an anonymous candidate with a cancellation-compatible level
  disappearance is replayed through that upper bound assuming zero real fills. Identity, lifetime
  and fill count are hypotheses. These rows contribute only to the candidate estimate.
  ``quotes.jsonl`` is read line by line into one small tuple per candidate; its quote object
  exists only while it is live. With ``run(reduce_quotes=True)`` a finished candidate is reduced
  to counts at once (total and per wallet, ``never_filled_by_wallet``) and no row is kept for
  it: a quote-flickering wallet has ~10^5 candidates per hour.

Analysis only: reads the future of the tape by design; nothing here is a decision input.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import orjson

from ..twin.engine import DYING_GRACE_NS, MIN_QTY, Quote, WindowSim, _Stats
from ..twin.tape import Book, Trade
from .flow import _ts
from .intlbook import BookObservation, IntlBook, LevelEvent, Print, Reset, px, replay_messages
from .ordermatch import _q, _r, qualified_placement
from .tradeallocation import Allocation, load_allocations

FOREVER = 1 << 62
MS_NS = 1_000_000
TOL = 0.02  # shares
MODES = ("pessimistic", "optimistic")
SIDE = {"BUY": "bid", "SELL": "ask"}
AGGR = {"BUY": "buy", "SELL": "sell"}
FLIP = {"buy": "sell", "sell": "buy"}
HIT_BY = {"bid": "sell", "ask": "buy"}
N_BOOT = 2000
LIMIT = ("conditional on a real fill: every replayed order filled at least once, so this is the "
         "fill model's bias given a fill, not a fill probability")


class IntlTrade(Trade):
    """Archived intl execution and receipt clocks can have different offsets."""

    __slots__ = ()

    @property
    def t_venue(self) -> int:
        return self.t_exch if self.t_exch is not None else self.tw


class QuoteSim(WindowSim):
    """One externally specified resting quote on the engine's maker fill logic."""

    def __init__(self, side: str, price: float, size: float, live_ns: int, queue: float):
        self.E_ns = FOREVER
        self.rest = False
        self.max_age_ns = 0
        self.book = None
        self.book_ok = False
        self.inv = 0.0
        self.live = {"bid": None, "ask": None}
        self.pending = {}
        self.dying = []
        self.st = _Stats()
        self.got: list[tuple[int, float, str]] = []  # (venue ns, shares, via)
        self.trade_venue_ns = 0
        self.quote = Quote(side=side, price=price, size=size, decided_ns=live_ns, ref_c=0.0,
                           inv_at=0.0, remaining=size, live_ns=live_ns, queue=queue)
        self.live[side] = self.quote

    def _room(self, side: str, px: float, inv: float) -> float:
        return math.inf

    def _fill(self, t: int, role: str, side: str, px: float, qty: float, via: str, ref_qty: float) -> None:
        self.got.append((t if via == "book_cross" else self.trade_venue_ns, qty, via))

    def _on_trade(self, t: int, tr: Trade) -> None:
        self.trade_venue_ns = tr.t_venue
        super()._on_trade(t, tr)

    def cancel(self, t: int) -> None:
        side = self.quote.side
        self.pending[side] = (0, None)
        self._on_quote_land(t, (side, 0))


@dataclass
class Spec:
    kind: str  # "order" | "quote" (fingerprint candidate)
    id: str
    token: str
    side: str  # BUY | SELL
    price: float
    size: float
    placed_ms: int
    placed_tw: int
    end_ms: int | None
    outcome: str
    real: float
    fills_at: list = field(default_factory=list)  # [tx, venue ms | None, shares]
    market: str = ""
    queue0: float | None = None
    sims: dict[str, QuoteSim] = field(default_factory=dict)
    cut: str | None = None  # why the replay stopped before the quote's end, if it did
    tx_seen: set[str] = field(default_factory=set)
    tx_routed: set[str] = field(default_factory=set)
    tx_delivered: set[str] = field(default_factory=set)  # routed AND handed to the live quote
    raw_routed: set[str] = field(default_factory=set)
    allocated_routed: set[str] = field(default_factory=set)
    unresolved_live: int = 0
    wallet: str | None = None  # fingerprint candidates: whose list it is on
    placement_candidates: int | None = None
    placement_evidence: str = "legacy_unqualified"
    fill_before_placement: bool | None = None
    rest_known: bool = False
    placed_record: int = -1
    expiry_tw_ns: int | None = None
    late_same_side_prints: int = 0
    late_deliveries: dict[str, tuple[int, int]] = field(default_factory=dict)  # tx -> venue ms, receipt ns

    @property
    def key(self) -> tuple:
        return (self.token, self.price, self.side, self.placed_ms, self.placed_tw)


QuoteKey = tuple[str, float, str, int, int]  # token, price, side, placement venue ms, receive ns


def _lines(p: Path):
    with open(p, "rb") as fh:
        for line in fh:
            if line.strip():
                yield orjson.loads(line)


def load_specs(d: Path) -> tuple[list[Spec], dict[QuoteKey, list[tuple]], Counter]:
    """Orders from an ordermatch dir, and (if ``quotes.jsonl`` is there) its cancelled
    fingerprint candidates as placement key -> [(size, end_ms, wallet)]."""
    specs: list[Spec] = []
    quotes: dict[QuoteKey, list[tuple]] = defaultdict(list)
    stats: Counter = Counter()
    for r in _lines(d / "orders.jsonl"):
        stats["orders"] += 1
        if "fills_at" not in r:
            raise SystemExit("orders.jsonl has no 'fills_at' (written by an older doppel-ordermatch): "
                             "re-run doppel-ordermatch, then replay")
        if r.get("fill_before_placement"):
            stats["orders_wrong_match_skipped"] += 1
            continue
        specs.append(Spec("order", r["order_id"], r["token_id"], r["side"], px(r["price"]),
                          float(r["size"]), r["placed_venue_ms"], r["placed_tw_ns"], r["end_ms"],
                          f"{r['outcome']}:{r['reason']}", float(r["filled"]), r["fills_at"],
                          placement_candidates=r.get("placement_candidates"),
                          placement_evidence=r.get("placement_evidence") or "legacy_unqualified",
                          fill_before_placement=r.get("fill_before_placement"),
                          rest_known=r.get("rest_known") is True))
    qp = d / "quotes.jsonl"
    if qp.exists():
        for r in _lines(qp):
            stats["quotes"] += 1
            legacy = (r["outcome"], r["reason"]) == ("cancelled", "exact_decrease")
            bound = (r["outcome"], r["reason"]) == ("unknown", "level_zero") and r.get("cancel_compatible") is True
            if not (legacy or bound) or r.get("end_ms") is None:
                stats["quotes_not_cancelled_skipped"] += 1
                continue
            quotes[(r["token_id"], px(r["price"]), "BUY", r["venue_ms"], r["tw_ns"])].append(
                (float(r["size"]), r["end_ms"], r.get("wallet")))
    return specs, quotes, stats


def to_book(levels: dict[tuple[float, str], float], t: int) -> Book:
    bids = sorted(((p, s) for (p, side), s in levels.items() if side == "BUY"), reverse=True)
    asks = sorted((p, s) for (p, side), s in levels.items() if side == "SELL")
    return Book(tw=t, bids=tuple(bids), asks=tuple(asks))


def replay_stream(root: Path, stream: str, t_from: int, t_to: int, pending: dict[tuple, list[Spec]],
                  wanted: dict[tuple[str, str], list[Spec]], stats: Counter,
                  quotes: dict[QuoteKey, list[tuple]], done: Callable[[Spec], object],
                  allocations: dict[str, Allocation | str] | None = None) -> None:
    """Drive the quotes placed on this stream. Mutates the specs (``sims``, ``queue0``, ...).
    A fingerprint candidate becomes a Spec at its placement event and is handed to ``done`` when
    its replay is over (cancelled at its end, cut by a reset, or the stream ended)."""
    book = IntlBook()
    receipt_high_water = 0
    active: dict[str, list[Spec]] = defaultdict(list)  # quote's own token -> live specs
    mates: dict[str, str] = {}
    n_mk = 0
    dup: dict[tuple, list[int]] = {}  # execution -> prints seen on [first, second] asset of the pair

    allocated: set[str] = set()  # a complete decoded match is charged exactly once on this stream

    def eligible(asset: str, ms: int, tw: int, *, trade: bool = False) -> list[Spec]:
        """Apply expiry in receipt order. Only prints may advance a queue after its book freezes."""
        specs = active.get(asset)
        if not specs:
            return []
        keep, ready = [], []
        for s in specs:
            past_end = s.end_ms is not None and ms > s.end_ms
            if past_end and s.expiry_tw_ns is None:
                s.expiry_tw_ns = tw
            if s.expiry_tw_ns is not None and receipt_high_water - s.expiry_tw_ns > DYING_GRACE_NS:
                for sim in s.sims.values():
                    sim.cancel(tw)
                if s.kind == "quote":
                    done(s)
                continue
            keep.append(s)
            if past_end or ms < s.placed_ms or s.placed_record == record_no:
                continue
            if s.expiry_tw_ns is None or (trade and tw >= s.expiry_tw_ns):
                ready.append(s)
        if keep:
            active[asset] = keep
        else:
            del active[asset]
        return ready

    for record_no, items in replay_messages(root, stream, t_from, t_to, book):
        receipt_high_water = max(receipt_high_water, items[0].tw_ns)
        evs = [it for it in items if not isinstance(it, Reset)]
        if len(evs) < len(items):  # book state lost: nothing more is known about live quotes
            for specs in active.values():
                for s in specs:
                    if s.expiry_tw_ns is None:
                        s.cut = "reset"
                    if s.kind == "quote":
                        done(s)
            active.clear()
        if not evs:
            continue
        if len(book.market_of) != n_mk:
            mates, n_mk = book.mates(), len(book.market_of)
        changed: dict[str, int] = {}
        for e in evs:
            if isinstance(e, BookObservation):
                eligible(e.asset, e.venue_ms, e.tw_ns)
                continue
            if not isinstance(e, LevelEvent):
                continue
            changed[e.asset] = max(changed.get(e.asset, 0), e.venue_ms)
            key = (e.asset, e.price, e.side, e.venue_ms, e.tw_ns)
            cands = pending.get(key)
            qc = quotes.get(key)
            s = None
            if cands and e.delta > 0:
                s = min(cands, key=lambda c, d=e.delta: abs(c.size - d))
                cands.remove(s)
            elif qc and e.delta > 0:
                size, end, wallet = qc.pop(min(range(len(qc)), key=lambda i, d=e.delta, q=qc: abs(q[i][0] - d)))
                if not qc:
                    del quotes[key]
                s = Spec("quote", f"{e.asset}:{e.price}:{e.venue_ms}:{e.tw_ns}", e.asset, "BUY", e.price,
                         size, e.venue_ms, e.tw_ns, end, "hypothetical:cancelled_without_fill", 0.0, wallet=wallet)
            if s is not None:
                s.market, s.queue0 = e.market or e.asset, e.old_size
                s.placed_record = record_no
                s.sims = {m: QuoteSim(SIDE[s.side], s.price, s.size, e.venue_ms * MS_NS, q)
                          for m, q in zip(MODES, (e.old_size, 0.0))}
                active[s.token].append(s)
        for a, ms in changed.items():
            specs = eligible(a, ms, evs[0].tw_ns)
            if specs:
                # The extra nanosecond admits subsequent records in the landing millisecond.
                # The placement record itself was excluded above. Same-ms crosses and trades tie.
                b = to_book(book.levels.get(a, {}), ms * MS_NS + 1)
                for s in specs:
                    for sim in s.sims.values():
                        sim._on_book(b)
        for e in evs:
            if not isinstance(e, Print):
                continue
            aggr = AGGR.get(e.side)
            m = mates.get(e.asset)
            if not m or aggr is None:
                # A same-market print without a provable token view or side may reach either queue.
                for token in tuple(active):
                    unknown_view = token != e.asset and m is None
                    if (unknown_view or aggr is None) and (
                            token in (e.asset, m) or active[token][0].market == e.market):
                        for s in eligible(token, e.venue_ms, e.tw_ns, trade=True):
                            s.unresolved_live += 1
            if aggr is None:
                stats["prints_bad_side"] += 1
                continue
            views = [(e.asset, e.price, aggr)]
            if m:
                views.append((m, px(1 - e.price), FLIP[aggr]))
            # Even a duplicate can be the first observation beyond a quote's venue end.
            eligible_views = {a: eligible(a, e.venue_ms, e.tw_ns, trade=True) for a, _, _ in views}
            if m and e.tx:
                first = e.asset < m
                c = dup.setdefault((e.tx, e.size) + (views[0] if first else views[1]), [0, 0])
                c[not first] += 1
                if c[not first] <= c[first]:  # the other asset already printed this execution
                    stats["mirror_dup_prints"] += 1
                    continue
            split = allocations.get(e.tx) if allocations is not None else None
            if e.tx in allocated:
                stats["repeat_allocated_prints"] += 1
                continue
            reason = (split.match(e, m) if isinstance(split, Allocation) else
                      split or ("no_decoded_call" if allocations is not None else "public_only"))
            resolved = isinstance(split, Allocation) and reason == "allocated"
            if resolved:
                allocated.add(e.tx)
            stats["public_prints:" + reason] += 1
            for asset, price, ag in views:
                watches = wanted.get((e.tx, asset), ())
                for s in watches:
                    s.tx_seen.add(e.tx)
                    if price == s.price and ag == HIT_BY[SIDE[s.side]]:
                        s.raw_routed.add(e.tx)
                specs = eligible_views[asset]
                if not (specs or watches):
                    continue
                for s in specs:
                    if s.expiry_tw_ns is not None and ag == HIT_BY[SIDE[s.side]]:
                        s.late_same_side_prints += 1
                if resolved and isinstance(split, Allocation):
                    levels = (split.levels if asset == split.token else
                              tuple((px(1 - p), q) for p, q in split.levels))
                else:
                    for s in specs:
                        if ag == HIT_BY[SIDE[s.side]]:
                            s.unresolved_live += 1
                    # Never spend a partial or inconsistent print and later replay the full call.
                    # Without a usable call retain the legacy public-print estimate, marked unresolved.
                    levels = () if isinstance(split, Allocation) else ((price, e.size),)
                for level, qty in levels:
                    if qty <= 0:
                        continue
                    for s in watches:
                        if level == s.price and ag == HIT_BY[SIDE[s.side]]:
                            s.tx_routed.add(e.tx)
                            if resolved:
                                s.allocated_routed.add(e.tx)
                    tr = IntlTrade(e.tw_ns, e.venue_ms * MS_NS + 1, level, qty, ag, e.tx or None)
                    for s in specs:
                        if level == s.price:
                            hit = ag == HIT_BY[SIDE[s.side]]
                            stats["prints_at_quote_price:" + ("hit_side" if hit else "other_side")] += 1
                            if hit and e.tx in s.tx_routed:
                                if s.expiry_tw_ns is not None and e.tx not in s.tx_delivered:
                                    s.late_deliveries[e.tx] = (e.venue_ms, e.tw_ns)
                                s.tx_delivered.add(e.tx)
                        for sim in s.sims.values():
                            sim._on_trade(e.tw_ns, tr)
    for specs in active.values():
        for s in specs:
            if s.end_ms is None or s.end_ms >= t_to * 1000:  # end not observed inside this replay
                s.cut = "tape_end"
            if s.kind == "quote":
                done(s)


# ---------------------------------------------------------------- results
def result(s: Spec) -> dict:
    located = [f[1] for f in s.fills_at if f[1] is not None]
    row: dict = {"kind": s.kind, "id": s.id, "token_id": s.token, "market": s.market, "side": s.side,
                 "price": s.price, "size": s.size, "outcome": s.outcome,
                 "end_known": s.rest_known and s.end_ms is not None
                              and s.outcome.split(":")[0] in ("filled", "cancelled"),
                 "placed_ms": s.placed_ms, "end_ms": s.end_ms, "cut": s.cut,
                 "placement_candidates": s.placement_candidates,
                 "placement_evidence": s.placement_evidence,
                 "fill_before_placement": s.fill_before_placement,
                 "queue_ahead": s.queue0, "real_filled": round(s.real, 6),
                 "real_first_fill_ms": min(located, default=None),
                 "fills": len(s.fills_at), "fills_located": len(located),
                 "fills_seen": len(s.tx_seen), "fills_routed": len(s.tx_routed),
                 "fills_delivered": len(s.tx_delivered), "fills_raw_routed": len(s.raw_routed),
                 "fills_allocated_routed": len(s.allocated_routed),
                 "unresolved_same_side_prints_while_live": s.unresolved_live,
                 "expiry_observed_tw_ns": s.expiry_tw_ns, "late_same_side_prints": s.late_same_side_prints,
                 "fills_delivered_during_grace": len(s.late_deliveries),
                 "late_deliveries": {tx: {"venue_ms": ms, "tw_ns": tw} for tx, (ms, tw) in s.late_deliveries.items()}}
    for m, sim in s.sims.items():
        tw = sum(q for _, q, _ in sim.got)
        src: dict[str, float] = defaultdict(float)
        for _, q, via in sim.got:
            src[via] += q
        ex = tw - s.real
        row[m] = {"filled": round(tw, 6), "error": round(ex, 6),
                  "first_fill_ms": min(t for t, _, _ in sim.got) // MS_NS if sim.got else None,
                  "by_source": {k: round(v, 6) for k, v in sorted(src.items())},
                  "missed": s.real > TOL and tw < MIN_QTY,
                  "phantom_shares": round(ex, 6) if ex > TOL else 0.0,
                  "fills": [[t // MS_NS, q, via] for t, q, via in sim.got]}
    return row


def boot_ci(rows: list[dict], mode: str, seed: int = 0) -> list[float] | None:
    """95 % CI of sum(twin) / sum(real), resampling markets (ratio of sums)."""
    by: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        by[r["market"]][0] += r[mode]["filled"]
        by[r["market"]][1] += r["real_filled"]
    a = np.array(list(by.values()))
    if len(a) < 2:
        return None
    idx = np.random.default_rng(seed).integers(0, len(a), (N_BOOT, len(a)))
    tw, re = a[idx, 0].sum(1), a[idx, 1].sum(1)
    ok = re > 0
    lo, hi = np.quantile(tw[ok] / re[ok], (0.025, 0.975))
    return [round(float(lo), 4), round(float(hi), 4)]


def agg(rows: list[dict], mode: str, ci: bool = False) -> dict:
    real = sum(r["real_filled"] for r in rows)
    tw = sum(r[mode]["filled"] for r in rows)
    err = [r[mode]["error"] for r in rows]
    d = {"n": len(rows), "markets": len({r["market"] for r in rows}), "real_shares": _r(real),
         "twin_shares": _r(tw), "ratio": _r(tw / real, 4) if real > 0 else None,
         "error_mean": _r(np.mean(err)) if err else None, "error_shares": _q(err),
         "exact": sum(abs(e) <= TOL for e in err), "underfilled": sum(e < -TOL for e in err),
         "missed": sum(r[mode]["missed"] for r in rows),
         "phantom": sum(r[mode]["phantom_shares"] > 0 for r in rows),
         "phantom_shares": _r(sum(r[mode]["phantom_shares"] for r in rows))}
    if ci:
        d["ratio_ci95"] = boot_ci(rows, mode)
    return d


def _bucket(x: float, edges: tuple[float, ...], names: tuple[str, ...]) -> str:
    return names[sum(x >= e for e in edges)]


Q_NAMES = ("0", "(0,100]", "(100,1000]", ">1000")
P_NAMES = ("<0.10", "0.10-0.30", "0.30-0.70", "0.70-0.90", ">=0.90")


def groups(rows: list[dict], mode: str) -> dict:
    def split(fn, names=None) -> dict:
        by: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by[fn(r)].append(r)
        return {k: agg(by[k], mode) for k in (names or sorted(by)) if k in by}

    return {
        "by_outcome": split(lambda r: r["outcome"]),
        "by_twin_source": split(lambda r: "+".join(r[mode]["by_source"]) or "none"),
        "by_queue_ahead": split(lambda r: Q_NAMES[0] if r["queue_ahead"] <= 0 else _bucket(
            r["queue_ahead"], (100.0001, 1000.0001), Q_NAMES[1:]), Q_NAMES),
        "by_price": split(lambda r: _bucket(r["price"], (0.1, 0.3, 0.7, 0.9), P_NAMES), P_NAMES),
    }


def summarize(rows: list[dict], stats: Counter) -> dict:
    replayed = [r for r in rows if r["kind"] == "order"]
    orders = [r for r in replayed if qualified_placement(r)]
    head = [r for r in orders if r["end_known"] and not r["cut"]]
    unk = [r for r in orders if not r["end_known"]]
    quotes = [r for r in rows if r["kind"] == "quote"]
    located = sum(r["fills_located"] for r in orders)
    seen = sum(r["fills_seen"] for r in orders)
    routed = sum(r["fills_routed"] for r in orders)
    deliv = sum(r["fills_delivered"] for r in orders)
    late = sum(r.get("fills_delivered_during_grace", 0) for r in orders)
    cond = {m: agg(head, m, ci=True) | {
        "first_fill_twin_minus_real_ms": _q([r[m]["first_fill_ms"] - r["real_first_fill_ms"] for r in head
                                             if r[m]["first_fill_ms"] is not None
                                             and r["real_first_fill_ms"] is not None])} for m in MODES}
    by_placement: dict[str, Counter] = defaultdict(Counter)
    by_prices: dict[str, Counter] = defaultdict(Counter)
    for r in replayed:
        if r["end_ms"] is None or r["cut"]:
            continue
        key = r.get("placement_evidence") or "legacy_unqualified"
        values = dict(orders=1, pessimistic_overfilled=int(r["pessimistic"]["error"] > TOL),
                      optimistic_underfilled=int(r["optimistic"]["error"] < -TOL))
        by_placement[key].update(values)
        if not qualified_placement(r) or not r["end_known"]:
            continue
        complete = (r.get("unresolved_same_side_prints_while_live") == 0 and r["fills"] > 0
                    and r.get("fills_allocated_routed") == r["fills"] == r["fills_delivered"])
        by_prices["fully_allocated" if complete else "unresolved"].update(values)
    doc: dict = {
        "limit": LIMIT, "counts": dict(stats) | {
            "orders_replayed": len(replayed), "headline_orders": len(head),
            "unqualified_placements": len(replayed) - len(orders),
            "known_end_cut_by_reset": sum(bool(r["end_known"] and r["cut"]) for r in orders),
            "cut_short": dict(Counter(r["cut"] for r in rows if r["cut"])),
            "twin_book_cross_fills": sum(v == "book_cross" for r in rows for _, _, v in r["pessimistic"]["fills"])},
        "adapter_gate": {"fills": sum(r["fills"] for r in orders), "located_by_print": located,
                         "print_seen_in_replay": seen, "routed_to_order_price_and_side": routed,
                         "raw_print_price_and_side": sum(r.get("fills_raw_routed", 0) for r in orders),
                         "allocated_price_and_side": sum(r.get("fills_allocated_routed", 0) for r in orders),
                         "routed_share": _r(routed / seen, 4) if seen else None,
                         "delivered_to_live_quote": deliv,
                         "delivered_before_expiry_observed": deliv - late,
                         "delivered_during_receipt_grace": late,
                         "delivered_share": _r(deliv / routed, 4) if routed else None,
                         "note": "low routed_share = the print side/mirror mapping is wrong; low "
                                 "delivered_share = the quote was not live when its own fill's print "
                                 "came (lifetime / tape order). Either way the bias numbers then "
                                 "measure the adapter, not the fill model"},
        "conditional_on_real_fill": cond,
        "bracket_violations": {
            "pessimistic_overfilled": cond["pessimistic"]["phantom"],
            "optimistic_underfilled": cond["optimistic"]["underfilled"],
            "by_placement_evidence": {k: dict(v) for k, v in sorted(by_placement.items())},
            "by_price_evidence": {k: dict(v) for k, v in sorted(by_prices.items())},
            "note": "Placement-evidence groups include diagnostic simulations run to an observed boundary. "
                    "The headline and price groups require a qualifying placement. A unique anonymous "
                    "candidate can still belong to another order."},
        "pessimistic_breakdown": groups(head, "pessimistic"),
        "optimistic_by_outcome": groups(head, "optimistic")["by_outcome"],
        "end_unknown_orders": {m: agg(unk, m) for m in MODES} | {
            "note": "real fills are a lower bound and the quote may outlive the order: excess is "
                    "a POSSIBLE phantom, not in the headline"},
    }
    if quotes:
        doc["never_filled_quotes_estimate"] = {m: {
            "n": len(quotes), "twin_filled": (n := sum(r[m]["filled"] > 0 for r in quotes)),
            "phantom_fill_rate": _r(n / len(quotes), 5),
            "quoted_shares": _r(sum(r["size"] for r in quotes)),
            "phantom_shares": _r(sum(r[m]["filled"] for r in quotes)),
            "by_twin_source": dict(Counter("+".join(r[m]["by_source"]) for r in quotes if r[m]["by_source"])),
        } for m in MODES} | {"note": NF_NOTE}
    return doc


NF_NOTE = ("ESTIMATE: hypothetical anonymous quotes, assuming zero real fills and continued presence "
           "until a cancellation-compatible level decrease. Their owners and actual lifetimes are unknown.")


def nf_add(tally: dict[str, Any], s: Spec) -> None:
    """Add one finished candidate to per-mode counts (flat: n, twin_filled, quoted_shares,
    phantom_shares, book_cross_fills, ``src:<twin sources>``), so tallies add up with ``+``."""
    for m, sim in s.sims.items():
        c = tally[m]
        tw = sum(q for _, q, _ in sim.got)
        c["n"] += 1
        c["twin_filled"] += tw > 0
        c["quoted_shares"] += s.size
        c["phantom_shares"] += tw
        c["book_cross_fills"] += sum(via == "book_cross" for _, _, via in sim.got)
        if sim.got:
            c["src:" + "+".join(sorted({via for _, _, via in sim.got}))] += 1


def nf_doc(tally: dict[str, Any]) -> dict | None:
    """Per-mode counts -> the shape of ``never_filled_quotes_estimate``; None without candidates."""
    if not all(tally.get(m, {}).get("n") for m in MODES):
        return None
    return {m: {"n": (t := tally[m])["n"], "twin_filled": t.get("twin_filled", 0),
                "phantom_fill_rate": _r(t.get("twin_filled", 0) / t["n"], 5),
                "quoted_shares": _r(t.get("quoted_shares", 0)), "phantom_shares": _r(t.get("phantom_shares", 0)),
                "by_twin_source": {k[4:]: v for k, v in t.items() if k.startswith("src:")}}
            for m in MODES} | {"note": NF_NOTE}


def run(root: Path, match_dir: Path, t_from: int, t_to: int, streams: list[str] | None = None,
        reduce_quotes: bool = False, *, orders: Path | None = None) -> tuple[dict, list[dict]]:
    """Returns (summary doc, per-quote rows: orders first, then fingerprint candidates). With
    ``reduce_quotes`` candidates are counted and dropped as they finish: no candidate rows, and
    the doc carries ``never_filled_by_wallet`` (flat counts per wallet and mode)."""
    specs, quotes, stats = load_specs(match_dir)
    kept: list[Spec] = []
    tally: dict[str, dict[str, Any]] = defaultdict(lambda: {m: Counter() for m in MODES})

    def done(s: Spec) -> None:
        if reduce_quotes:
            nf_add(tally[s.wallet or ""], s)
            nf_add(tally["*"], s)
        else:
            kept.append(s)

    pending: dict[tuple, list[Spec]] = defaultdict(list)
    wanted: dict[tuple[str, str], list[Spec]] = defaultdict(list)
    for s in specs:
        pending[s.key].append(s)
        for tx, _, _ in s.fills_at:
            wanted[(tx, s.token)].append(s)
    streams = streams or sorted(d.name for d in Path(root).iterdir() if d.name.startswith("clob_"))
    allocations = load_allocations(orders) if orders is not None else None
    for st in streams:
        replay_stream(root, st, t_from, t_to, pending, wanted, stats, quotes, done, allocations)
    stats["placement_not_found"] = sum(not s.sims for s in specs) + sum(len(v) for v in quotes.values())
    rows = [result(s) for s in specs if s.sims] + [result(s) for s in kept]
    doc = {"params": {"from": t_from, "to": t_to, "streams": streams, "tol_shares": TOL,
                      "expiry_receipt_grace_ns": DYING_GRACE_NS,
                      "n_boot": N_BOOT, "decoded_orders": str(orders) if orders is not None else None}} | summarize(rows, stats)
    if reduce_quotes:
        total = tally.pop("*", {})
        doc["counts"]["candidate_book_cross_fills"] = total["pessimistic"]["book_cross_fills"] if total else 0
        if nf := nf_doc(total):
            doc["never_filled_quotes_estimate"] = nf
        doc["never_filled_by_wallet"] = {w: {m: dict(c) for m, c in t.items()} for w, t in sorted(tally.items())}
    return doc, rows


def card(doc: dict) -> str:
    g = doc["adapter_gate"]
    L = [f"LIMIT: {doc['limit']}", f"counts: {doc['counts']}",
         f"adapter gate: {g['routed_to_order_price_and_side']}/{g['print_seen_in_replay']} real fill "
         f"prints routed to the order's price and side (share {g['routed_share']}); "
         f"{g['delivered_to_live_quote']} delivered to the live quote (share {g['delivered_share']}); "
         f"{g['located_by_print']}/{g['fills']} fills located by a print",
         f"bracket violations (should be 0): {doc['bracket_violations']['pessimistic_overfilled']} "
         f"pessimistic over-fills, {doc['bracket_violations']['optimistic_underfilled']} optimistic under-fills"]
    for status, counts in doc["bracket_violations"]["by_placement_evidence"].items():
        L.append(f"  {status}: {counts['orders']} orders, {counts['pessimistic_overfilled']} pessimistic "
                 f"over-fills, {counts['optimistic_underfilled']} optimistic under-fills")
    for m in MODES:
        d = doc["conditional_on_real_fill"][m]
        L += [f"\n== {m} queue, orders with a known end (n {d['n']}, {d['markets']} markets)",
              f"twin {d['twin_shares']} vs real {d['real_shares']} shares: ratio {d['ratio']} "
              f"CI95 {d.get('ratio_ci95')} | mean error {d['error_mean']} sh/order {d['error_shares']}",
              f"exact {d['exact']}, under-filled {d['underfilled']}, fully missed {d['missed']}, phantom {d['phantom']} orders "
              f"({d['phantom_shares']} shares) | first fill twin - real ms {d['first_fill_twin_minus_real_ms']}"]
    for name, by in (doc["pessimistic_breakdown"] | {"optimistic by_outcome": doc["optimistic_by_outcome"]}).items():
        L.append(f"{name}: " + "; ".join(
            f"{k} n={v['n']} ratio={v['ratio']} missed={v['missed']} phantom={v['phantom']}"
            for k, v in by.items()))
    u = doc["end_unknown_orders"]["pessimistic"]
    L.append(f"end unknown (POSSIBLE phantom only): n {u['n']} ratio {u['ratio']} phantom {u['phantom']}")
    nf = doc.get("never_filled_quotes_estimate")
    if nf:
        L.append("never-filled quotes (ESTIMATE, fingerprint candidates): " + "; ".join(
            f"{m} {nf[m]['twin_filled']}/{nf[m]['n']} phantom-filled (rate {nf[m]['phantom_fill_rate']}, "
            f"{nf[m]['phantom_shares']} of {nf[m]['quoted_shares']} shares)" for m in MODES))
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-orderreplay")
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--ordermatch-dir", type=Path, required=True,
                    help="doppel-ordermatch output: orders.jsonl[, quotes.jsonl]")
    ap.add_argument("--from", dest="t_from", type=_ts, required=True, help="unix s or ISO UTC")
    ap.add_argument("--to", dest="t_to", type=_ts, required=True)
    ap.add_argument("--out", type=Path, required=True, help="directory")
    ap.add_argument("--orders", type=Path, help="decoded ordertape for retrospective price-level allocation")
    a = ap.parse_args(argv)
    doc, rows = run(a.data, a.ordermatch_dir, a.t_from, a.t_to, orders=a.orders)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "orderreplay.json").write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
    with open(a.out / "orderreplay.jsonl", "wb") as f:
        for r in rows:
            f.write(orjson.dumps(r) + b"\n")
    print(card(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
