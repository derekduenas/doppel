"""Intl CLOB tape -> the twin's ``WindowTape``, so a genome can be replayed on the Polymarket INTL
book of an Up/Down window (clone fidelity, ``lab/fidelity.py``). Lives in ``lab`` so the PM US
twin's code hash (``twin/*.py``) does not move; ``twin/engine.py`` is driven unchanged.

One instrument per window, as the engine expects: the UP token. Buy Up = lift the Up ask; buy Down =
hit the Up bid (a Down share costs 1 - bid), which is what the venue's mirrored books mean.

ADAPTER (``clob_<asset>_<hz>`` -> ``Book`` / ``Trade`` / gaps, through ``lab.intlbook.IntlBook``)
- Levels: the Up token's OWN levels only. The venue already publishes every Down order mirrored
  into the Up book (a Down bid at q is an Up ask at 1 - q; see ``tests/fixtures_formats.json``), so
  adding mirrored Down levels would double the depth. One ``Book`` (best ``depth`` levels per side)
  after every record that changed the Up token, plus one as-of snapshot when the window's capture
  starts and one when the token becomes known again after a reset.
- Prints: every ``last_trade_price`` of the Up token, and every Down print mirrored (price 1 - p,
  aggressor flipped). If the same execution is printed on both tokens (same tx, size, mirrored
  price and side) the second copy is dropped (``mirror_dup_prints``): orderreplay's rule.
  ASSUMED, as in orderreplay: ``side`` is the taker's side on the print's own token. A check that
  can fail: the same (tx, size, Up price) printed on the two tokens with OPPOSITE Up aggressors
  contradicts it (``side_conflicts``).
- Gaps: an ``IntlBook`` reset (connect / disconnect / stall / drop / missing hour) is a gap; the
  book is unknown until the Up token's next ``book`` frame.
- CLOCK, the one difference from orderreplay: orderreplay runs real orders on the VENUE clock
  (venue ms forced increasing in tape order), because nothing there decides anything. Here the
  genome decides, so ``Book.tw`` and ``Trade.tw`` are OUR receive time (what is known when) and
  only ``Trade.t_exch`` is the print's venue ms (when it executed); the engine then applies its
  E33 rules unchanged. This needs the recorder and venue clocks to agree: ``print_lag_s``
  (receive - venue, per window) is reported.
- The replay of a stream starts one window length + ``PRE_S`` before the first window: a token's
  first ``book`` frame arrives when it is subscribed, about one window before it opens.

THE TARGET WALLET'S FOOTPRINT (``Footprint``). The tape was recorded WITH the wallet trading in it;
the clone must be replayed on the market as it would have been WITHOUT the wallet, or it queues
behind the wallet's own orders and is filled by the wallet's own orders.
- Resting orders: over each located order's life (ordermatch: placement record -> end) its remaining
  size (size - fills printed so far) is subtracted from its Up-book level in every ``Book``. A
  clone quoting there gets the wallet's queue position. An order whose end is unknown is
  subtracted only up to its last located fill and marks the window ``footprint_partial``.
- The wallet's TAKER executions: every print of a tx in which the wallet was the taker is dropped
  (its own aggression must not fill the clone).
- The wallet's MAKER executions are KEPT: such a print is a third party's marketable order. With
  the wallet's resting size removed it trades with whoever stands at that price, which is how a
  clone standing where the wallet stood recovers the wallet's fills. (Dropping these prints too
  would make every true maker clone score zero recall.) What keeps an always-on quoter from passing
  on this flow is the scorer's same-window null, not the tape.
- NOT removable: the wallet's quotes that never filled (they are not on chain; E39 estimates they
  are most of what a flickering maker posts) and the liquidity its taker orders consumed.

FIDELITY EXCLUSIONS (``run_intl``, both maker and taker genomes): a window with any gap inside
[S, E) (``feed_gap``) or whose first book arrives after the open (``late_book``) is not simulated.

NOT available on intl: a venue volume counter. The engine's maker coverage gates (prints vs
``sharesTraded``) pass vacuously; print completeness rests on E39 (612 of 614 fills had a print).

SETTLEMENT (``IntlRef``): outcome = the Up token's payout when the market is resolved (the dossier's
rule: ``tokens.jsonl`` ``payout`` with ``status == "resolved"``), else the BRTI basis A(E-1) >= A(S-1)
from ``ref_prices`` (E28: agrees with payouts in ~99% of windows), else unsettled (excluded).
priceToBeat / settle / raw BRTI for fair-v1 come from ``RefStore`` exactly as on PM US. Fees are the
genome's ``costs``.

``intl_*`` flow features, when the genome uses them, are computed WITHOUT the target wallet's own
RTDS rows (``flow_without``): a clone must not be steered by the trades it is meant to reproduce.
"""

from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field, fields
from pathlib import Path

import numpy as np

from ..strategy.genome import Genome
from ..tape.segment import read_segment, segment_path
from ..tape.universe import HORIZONS, intl_slug
from ..twin.engine import SimConfig, run_windows, window_head
from ..twin.features import FeatureConfig, Features, RefStore, WindowRef
from ..twin.tape import NS, POST_S, PRE_S, Book, Trade, WindowTape, window_list
from .census import window_of
from .intlbook import IntlBook, LevelEvent, Print, Reset, px
from .ledger import TokenMap

MS_NS = 1_000_000
AGGR = {"BUY": "buy", "SELL": "sell"}
FLIP = {"buy": "sell", "sell": "buy"}
TOL = 0.02   # shares
Key = tuple[str, str, int]   # asset, horizon, start


@dataclass(frozen=True)
class IntlWindow:
    asset: str
    horizon: str
    start: int
    up: str                      # token ids
    down: str
    payout_up: float | None      # resolved payout of the Up token, None while unresolved

    @property
    def key(self) -> Key:
        return (self.asset, self.horizon, self.start)

    @property
    def end(self) -> int:
        return self.start + HORIZONS[self.horizon]


@dataclass
class IntlRef(WindowRef):
    payout_up: float | None = None

    @property
    def brti_outcome(self) -> int | None:
        if self.ptb is None or self.settle is None:
            return None
        return 1 if self.settle >= self.ptb else 0

    @property
    def outcome(self) -> int | None:
        if self.payout_up is not None:
            return 1 if self.payout_up >= 0.5 else 0
        return self.brti_outcome


@dataclass(frozen=True)
class Resting:
    """One located resting order of the target wallet, in UP-book terms."""
    side: str            # level side on the Up token: "BUY" (bid) | "SELL" (ask)
    price: float
    size: float
    placed_tw: int       # receive ns of the placement record: subtracted from that record on
    end_ms: int          # venue ms from which nothing is subtracted any more
    fills: tuple[tuple[int, float], ...] = ()   # (venue ms, shares), sorted

    def remaining(self, ms: int) -> float:
        return self.size - sum(q for t, q in self.fills if t <= ms)


@dataclass
class Footprint:
    taker_txs: set[str] = field(default_factory=set)             # prints of these txs are dropped
    resting: dict[Key, list[Resting]] = field(default_factory=dict)


def windows_from_tokens(tmap: TokenMap, assets, horizons, t_from: int, t_to: int
                        ) -> tuple[list[IntlWindow], list[Key]]:
    """Every window of the universe inside [t_from, t_to] whose two tokens are in the token map,
    ordered by start, and the keys of the windows that are not (they cannot be replayed)."""
    by_slug: dict[str, dict[int, dict]] = {}
    for info in tmap.m.values():
        if window_of(info) and info.get("idx") in (0, 1):
            by_slug.setdefault(info["slug"], {})[int(info["idx"])] = info
    wins, missing = [], []
    for start, h, a in window_list(assets, horizons, t_from, t_to):
        toks = by_slug.get(intl_slug(a, h, start), {})
        if 0 not in toks or 1 not in toks:
            missing.append((a, h, start))
            continue
        up = toks[0]
        pay = up.get("payout") if up.get("status") == "resolved" else None
        wins.append(IntlWindow(a, h, start, up["token"], toks[1]["token"],
                               None if pay is None else float(pay)))
    return wins, missing


def to_book(levels: dict[tuple[float, str], float], tw: int, depth: int,
            sub: dict[tuple[float, str], float] | None = None) -> Book:
    """Best ``depth`` levels per side; ``sub`` = size to take off a level (the wallet's own)."""
    if sub:
        levels = dict(levels)
        for k, q in sub.items():
            left = levels.get(k, 0.0) - q
            if left > TOL:
                levels[k] = left
            else:
                levels.pop(k, None)
    bids = heapq.nlargest(depth, ((p, s) for (p, side), s in levels.items() if side == "BUY"))
    asks = heapq.nsmallest(depth, ((p, s) for (p, side), s in levels.items() if side == "SELL"))
    return Book(tw=tw, bids=tuple(bids), asks=tuple(asks))


@dataclass
class _Acc:
    w: IntlWindow
    wt: WindowTape
    lo: int
    hi: int
    todo: deque[Resting]                       # the wallet's orders not yet placed, by placed_tw
    live: list[Resting] = field(default_factory=list)
    known: bool = False
    dup: dict[tuple, list[int]] = field(default_factory=dict)
    sides: dict[tuple, set[tuple[str, bool]]] = field(default_factory=dict)
    lags: list[float] = field(default_factory=list)
    n_dup: int = 0
    bad_side: int = 0
    conflicts: int = 0
    dropped: int = 0

    def sub(self, tw: int, ms: int) -> dict[tuple[float, str], float]:
        """The wallet's resting size per Up-book level as of this record."""
        while self.todo and self.todo[0].placed_tw <= tw:
            self.live.append(self.todo.popleft())
        self.live = [r for r in self.live if ms < r.end_ms]
        out: dict[tuple[float, str], float] = {}
        for r in self.live:
            q = r.remaining(ms)
            if q > TOL:
                out[(r.price, r.side)] = out.get((r.price, r.side), 0.0) + q
        return out


def _records(root: Path, stream: str, t_from: int, t_to: int):
    """(record | None, tw ns) in tape order; None marks a missing hour segment."""
    for hour in range(t_from - t_from % 3600, t_to, 3600):
        path = segment_path(root, stream, hour * NS)
        if not path.exists():
            yield None, max(hour, t_from) * NS
            continue
        for rec in read_segment(path, verify=True):
            if t_from * NS <= rec["tw"] < t_to * NS:
                yield rec, int(rec["tw"])


def _finish(a: _Acc) -> WindowTape:
    wt, S = a.wt, a.w.start
    if wt.books:
        wt.source = "ws"
    q = [round(float(v), 3) for v in np.quantile(a.lags, (0.1, 0.5, 0.9))] if a.lags else None
    wt.stats = {"books": len(wt.books), "trades": len(wt.trades), "gap_events": len(wt.gaps),
                "trade_gap_events": 0, "mirror_dup_prints": a.n_dup, "prints_bad_side": a.bad_side,
                "side_conflicts": a.conflicts, "wallet_taker_prints_dropped": a.dropped,
                "first_book_s": round((wt.books[0].tw - S * NS) / NS, 3) if wt.books else None,
                "trade_volume": round(sum(t.qty for t in wt.trades if t.t_venue < a.w.end * NS), 4),
                "venue_volume": None, "trade_coverage": None, "print_lag_s": q}
    return wt


def iter_stream(root: Path, wins: list[IntlWindow], depth: int = 10, watch: set[str] | None = None,
                tx_ms: dict[tuple[str, Key], int] | None = None, fp: Footprint | None = None
                ) -> Iterator[WindowTape]:
    """``WindowTape`` of each window of ONE (asset, horizon), in start order, from one pass over
    the stream. ``tx_ms[(tx, window key)]`` gets the earliest venue ms of a print of each tx in
    ``watch`` on either token of a window (prints before the open and after the close included).
    ``fp`` removes the target wallet's footprint (module docstring)."""
    wins = sorted(wins, key=lambda w: w.start)
    if not wins:
        return
    fp = fp or Footprint()
    asset, hz = wins[0].asset, wins[0].horizon
    stream = f"clob_{asset}_{hz}"
    by_tok = {t: (w, up) for w in wins for t, up in ((w.up, True), (w.down, False))}
    book = IntlBook()
    pending = deque(wins)
    active: dict[Key, _Acc] = {}
    t0, t1 = wins[0].start - HORIZONS[hz] - PRE_S, wins[-1].end + POST_S

    def seed(tw: int):
        """Open the windows whose capture has started; close (yield) the ones that are over."""
        while pending and (pending[0].start - PRE_S) * NS <= tw:
            w = pending.popleft()
            lo, hi = (w.start - PRE_S) * NS, (w.end + POST_S) * NS
            a = _Acc(w, WindowTape(slug=intl_slug(*w.key), asset=asset, horizon=hz, start=w.start,
                                   end=w.end, stream=stream), lo, hi,
                     deque(sorted(fp.resting.get(w.key, ()), key=lambda r: r.placed_tw)))
            if book.known(w.up):   # as-of state from records received before lo
                a.wt.books.append(to_book(book.levels[w.up], lo, depth, a.sub(lo, lo // MS_NS)))
                a.known = True
            active[w.key] = a
        for k in [k for k, a in active.items() if a.hi <= tw]:
            yield _finish(active.pop(k))

    for rec, tw in _records(Path(root), stream, t0, t1):
        yield from seed(tw)
        if rec is None:
            book.reset()
            items: list = [Reset(tw, "missing_segment")]
        else:
            items = book.feed(rec)
        changed: set[Key] = set()
        ms = tw // MS_NS
        for e in items:
            if isinstance(e, Reset):
                for a in active.values():
                    a.wt.gaps.append(tw)
                    a.known = False
                continue
            ms = e.venue_ms
            hit = by_tok.get(e.asset)
            if hit is None:
                continue
            w, is_up = hit
            if isinstance(e, LevelEvent):
                if is_up and w.key in active:
                    changed.add(w.key)
                continue
            assert isinstance(e, Print)
            if tx_ms is not None and watch and e.tx in watch:
                k = (e.tx, w.key)
                tx_ms[k] = min(tx_ms.get(k, e.venue_ms), e.venue_ms)
            a = active.get(w.key)
            if a is None:
                continue
            aggr = AGGR.get(e.side)
            if aggr is None:
                a.bad_side += 1
                continue
            price, aggr = (e.price, aggr) if is_up else (px(1 - e.price), FLIP[aggr])
            if e.tx:
                seen = a.sides.setdefault((e.tx, e.size, price), set())
                if (FLIP[aggr], not is_up) in seen:    # the other token says the opposite side
                    a.conflicts += 1
                seen.add((aggr, is_up))
                # the same execution printed on both tokens: keep the first copy
                c = a.dup.setdefault((e.tx, e.size, price, aggr), [0, 0])
                c[not is_up] += 1
                if c[not is_up] <= c[is_up]:
                    a.n_dup += 1
                    continue
            a.lags.append((e.tw_ns - e.venue_ms * MS_NS) / NS)
            if e.tx in fp.taker_txs:
                a.dropped += 1
                continue
            a.wt.trades.append(Trade(tw=e.tw_ns, t_exch=e.venue_ms * MS_NS, price=price, qty=e.size,
                                     aggressor=aggr, id=e.tx or None))
        for k, a in active.items():
            placed = bool(a.todo) and a.todo[0].placed_tw <= tw   # the wallet's order landed here
            if book.known(a.w.up) and (k in changed or not a.known or placed):
                a.wt.books.append(to_book(book.levels[a.w.up], tw, depth, a.sub(tw, ms)))
                a.known = True
    yield from seed(t1 * NS + 1)


def iter_windows(root: Path, wins: list[IntlWindow], depth: int = 10, watch: set[str] | None = None,
                 tx_ms: dict[tuple[str, Key], int] | None = None, fp: Footprint | None = None
                 ) -> Iterator[tuple[IntlWindow, WindowTape]]:
    """All windows in the twin's order (start, horizon length, asset), one pass per stream."""
    groups: dict[tuple[str, str], list[IntlWindow]] = {}
    for w in wins:
        groups.setdefault((w.asset, w.horizon), []).append(w)

    def one(ws: list[IntlWindow]):
        ws = sorted(ws, key=lambda w: w.start)
        return zip(ws, iter_stream(root, ws, depth, watch, tx_ms, fp))
    return heapq.merge(*(one(ws) for ws in groups.values()),
                       key=lambda p: (p[0].start, HORIZONS[p[0].horizon], p[0].asset))


def intl_ref(store: RefStore, w: IntlWindow, vol_window_s: int) -> IntlRef:
    r = store.window_ref(w.asset, w.start, w.end, vol_window_s)
    return IntlRef(**{f.name: getattr(r, f.name) for f in fields(WindowRef)}, payout_up=w.payout_up)


def strict_exclusion(wt: WindowTape) -> str | None:
    """Fidelity runs only: the book must be known from the open to the close."""
    if wt.source != "ws":
        return None   # the engine reports no_ws_coverage
    if any(wt.start * NS <= t < wt.end * NS for t in wt.gaps):
        return "feed_gap"
    if (wt.stats.get("first_book_s") or 0.0) > 0:
        return "late_book"
    return None


def flow_without(root: Path, asset: str, hz: str, start: int, scores, wallet: str | None) -> dict[int, dict]:
    """``lab.flow.window_flow_features`` with the RTDS rows of ``wallet`` left out."""
    from .dossier import build_index_files
    from .flow import LOOKBACKS, features_from_fills, hours_between, iter_rtds
    end, slug = start + HORIZONS[hz], intl_slug(asset, hz, start)
    fills = [f for _, fs in iter_rtds(root, hours_between(start - max(LOOKBACKS) - 300, end))
             for f in fs if f.slug == slug and f.wallet.lower() != wallet]
    s = f"clob_{asset}_{hz}"
    idx = build_index_files([(s, segment_path(root, s, h * NS)) for h in hours_between(start - 10, end)])
    return features_from_fills(fills, start, end, scores, idx, None)


class _Flow(Features):
    def __init__(self, g: Genome, ref: WindowRef, cfg: FeatureConfig, root: Path, cache, wallet: str | None):
        super().__init__(g, ref, cfg)
        hz = f"{(ref.end - ref.start) // 60}m"
        self._flow = flow_without(root, ref.asset, hz, ref.start, cache.scores(ref.start), wallet)

    def at(self, d, book, inventory_up):
        x = super().at(d, book, inventory_up)
        for k, v in (self._flow.get(d) or {}).items():
            if k in x and v is not None:
                x[k] = float(v)
        return x


def run_intl(root: Path, g: Genome, wins: list[IntlWindow], cfg: SimConfig = SimConfig(),
             lab: Path | None = None, watch: set[str] | None = None,
             tx_ms: dict[tuple[str, Key], int] | None = None, fp: Footprint | None = None,
             wallet: str | None = None, others: dict[Key, list[tuple]] | None = None) -> list[dict]:
    """Replay ``g`` on the intl book of ``wins`` with the engine's own runner (cross-window risk
    rules included). Each result also carries ``outcome_src`` and ``brti_outcome``.
    ``others[key]`` gets the window's kept prints executed in [S, E): (unix s, Up price, shares,
    aggressor, tx), the raw material of the scorer's null."""
    root = Path(root)
    store = RefStore(root)
    if wins:
        lo, hi = min(w.start for w in wins) - 1, max(w.end for w in wins)
        for a in {w.asset for w in wins}:
            store.load_boundaries(a, lo, hi)
    from ..twin.flowfeat import ScoreCache, rtds_hours_present, uses_intl
    factory = None
    need_rtds = uses_intl(g)
    if need_rtds:
        if lab is None:
            raise ValueError("genome uses intl_* features: pass the lab dir (informedness scores)")
        cache = ScoreCache(root, lab)

        def make(gg, ref, c, cache=cache):
            return _Flow(gg, ref, c.feature_config(), root, cache, wallet)
        factory = make
    refs: list[IntlRef] = []
    skipped: list[dict] = []

    def tag(res: dict, ref: IntlRef) -> dict:
        res["outcome_src"] = ("payout" if ref.payout_up is not None else
                              "brti" if ref.brti_outcome is not None else None)
        res["brti_outcome"] = ref.brti_outcome
        return res

    def items():
        for w, wt in iter_windows(root, wins, cfg.depth_levels, watch, tx_ms, fp):
            if need_rtds and not rtds_hours_present(root, w.start - 120, w.end):
                wt.stats["rtds_missing"] = True
            ref = intl_ref(store, w, g.fair.vol_window_s)
            why = strict_exclusion(wt)
            if why:
                skipped.append(tag({**window_head(wt, ref), "eligible": False, "excluded": why}, ref))
                continue
            if others is not None:
                others[w.key] = [(t.t_venue / NS, t.price, t.qty, t.aggressor, t.id) for t in wt.trades
                                 if w.start * NS <= t.t_venue < w.end * NS]
            refs.append(ref)
            yield wt, ref
    out = run_windows(g, items(), cfg, features_factory=factory)
    return [tag(res, ref) for res, ref in zip(out, refs)] + skipped
