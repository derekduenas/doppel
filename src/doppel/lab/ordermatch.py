"""Order match: find decoded signed orders in the anonymous intl book, follow them, and estimate
the quotes that never filled (E37 productized).

Inputs: the intl CLOB tape (``intlbook``) and ``doppel-ordertape`` rows (signed orders decoded
from ``matchOrders`` calldata, hash-verified). Only maker rows are matched; rows of one
``order_id`` are merged (fills summed per tx). Screen as in the E37 probe: ``created_ms`` inside
[--from, --to) and 0 <= age_s < 600 at the first fill.

``--orders`` is a JSONL sample (in file order) or the cohort parquet directory.
Managed tapes use committed flush parts and require verified rows. Legacy exports without
verification flags can be read standalone. If the source contains any verification flags,
only rows marked true count.
Directory rows are read oldest fill first, so "first fill" does not depend on the
order the parts were written in. A cohort run verifies the cohort's own fills only, so other
makers in the same txs are ``verified == False`` and are left out: ``other_known`` then counts
only other loaded wallets, and true identity precision is unmeasured and ``others_pooled`` holds cohort wallets only. Maker rows created outside [--from, --to) are dropped while reading and
counted in rows (``rows_out_of_range``), not in orders.

Placement and timing evidence
-----------------------------
The signed clock is screened against the earliest fill block. Search the signed window for
positive changes at the order token, maker side and price within the requested size tolerance.
Choose a representative add for diagnostics. Only a verified order with one uncontested add
from a price-change message, complete book coverage and located fills can receive
`unique_candidate`. Mirrored observations at the same venue time and size compete for the
same physical add. The label is conditional on the available orders and anonymous book.
Every qualified add must precede the bound execution on both venue and receive clocks.
A full bound transaction print can locate the execution even when its total exceeds the
individual maker fill. Otherwise a direct leg needs the correct token or mirror, aggressor,
price and filled quantity. Multiple plausible venue times leave execution time unknown.

The placebo shifts the order and the fill-relative cutoffs together. The two rates use the
same verified, print-anchored orders with continuous coverage across both requests. Each
order row records its candidate and contender counts. Unqualified representatives remain
diagnostic and do not contribute to validated timing and match rates. Missing hour files reset
coverage. Included frames are read strictly, with CRC verification.

Fully consumed signed maker quantity ends at the last compatible transaction print. For a
partial fill, a later zero-sized level bounds the end from above. A crowded decline,
snapshot update or exact-sized removal cannot prove a wallet cancellation. The lower
rest bound is the last located fill relative to the candidate add. A disconnect or unknown
continuity censors the upper bound. These bounds assume the proposed placement is correct.

3. Fingerprint attribution of quotes that never filled. Fingerprint = the fewest sizes (2 dp)
   covering >= 90 % of the wallet's decoded maker orders (none if that takes more than 8 sizes).
   Counted on the BUY view only (each quote shows once as a bid on one token and once as an ask
   on its mate; counting both would double it): bid-level increases equal to a fingerprint size.
   In the wallet's active tokens (any decoded order of the wallet, plus mates) each is
   ``own`` (matched to the wallet's decoded order), ``other_known`` (matched to another maker's
   decoded order: a matching physical add for another observed maker) or ``candidate``. In control tokens (no decoded order
   of the wallet) the count gives a base rate per token-hour of known book. Reported side by
   side, total and per size: candidates, expected by chance (base rate x active token-hours),
   own_unmatched (the wallet's decoded fingerprint-size orders that found no placement: filled
   quotes hiding among the candidates), net = candidates - own_unmatched - expected, and
   own share among matched orders = own / (own + other_known).
   NOT IDENTIFIED: which candidates are the wallet's. ``net`` is an estimate under the
   assumption that other participants post these sizes at the control rate in the wallet's
   markets. The base rate is contaminated upward (the wallet may quote in control tokens without
   filling, and "no fills" is judged from a partial sample), the decoded sample excludes
   other makers outside the fetched transactions, and round sizes are
   generic. Candidates are followed like remainders (R = the increase); their rows carry
   ``attribution: "fingerprint_candidate"`` and are never facts about the wallet.

Analysis only: placement uses created_ms and follow-forward reads the future of the tape by
design, so none of these outputs is a decision input at the time of the quote.
"""

from __future__ import annotations

import argparse
from array import array
import math
import sys
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import orjson
import pyarrow.parquet as pq

from .flow import _ts
from .intlbook import NS, IntlBook, Item, LevelEvent, Print, px, replay
from .ordertape import STATE_DIR, committed_parts
from .tradeallocation import Allocation, load_allocations

Key = tuple[str, float, str]  # asset, price, side
MAX_AGE_S = 600.0
PRINT_BLOCK_TOL_MS = 120_000
PLACEMENT_EVIDENCE_VERSION = 2
FP_COVER = 0.9
FP_MAX_SIZES = 8
SLICE_S = 600  # default --slice-s


@dataclass(frozen=True)
class Params:
    lo_ms: int = -300
    hi_ms: int = 2500
    size_tol: float = 0.02  # shares
    size_tol_rel: float = 0.0
    placebo_ms: int = 60_000
    print_win_ms: int = 1000

    def __post_init__(self):
        if self.hi_ms < self.lo_ms or self.placebo_ms <= self.hi_ms - self.lo_ms:
            raise ValueError("real and forward-shifted search windows must be distinct and disjoint")
        if self.print_win_ms < 0 or not all(math.isfinite(x) and x >= 0 for x in (self.size_tol, self.size_tol_rel)):
            raise ValueError("use finite nonnegative tolerances")

    def tol(self, size: float) -> float:
        return max(self.size_tol, self.size_tol_rel * size)

    def near(self, x: float, size: float) -> bool:
        return abs(x - size) <= self.tol(size)


@dataclass
class Order:
    order_id: str
    maker: str
    token: str
    side: str
    price: float
    size: float
    created_ms: int
    fills: dict[str, tuple[float, int | None]] = field(default_factory=dict)  # tx -> shares, block_ts
    verified: bool = False
    maker_amount: int | None = None
    filled_amount: int = 0

    @property
    def key(self) -> Key:
        return (self.token, self.price, self.side)

    @property
    def filled(self) -> float:
        return sum(f[0] for f in self.fills.values())


def filled_shares(row: dict) -> float:
    """Shares filled in one row. ``fill_amount`` is in maker-amount units (USDC for BUY)."""
    fa = row["fill_amount"]
    if row["side"] == "SELL":
        return fa / 1e6
    if row.get("maker_amount"):
        return fa * row["taker_amount"] / row["maker_amount"] / 1e6
    return fa / 1e6 / row["limit"]


PARQUET_COLS = ("tx", "block_ts", "role", "order_id", "maker", "token_id", "side", "size", "limit",
                "maker_amount", "taker_amount", "fill_amount", "created_ms", "age_s", "verified")


def _jsonl_rows(path: Path):
    with open(path, "rb") as fh:
        for line in fh:
            if line.strip():
                yield orjson.loads(line)


def _parquet_rows(path: Path, t_from: int, t_to: int, active: dict[str, set[str]],
                  stats: Counter) -> list[dict]:
    """Maker rows of an ordertape directory created in [t_from, t_to), oldest fill first. Only
    ``verified == True`` rows count (when the column exists); every counted row, whatever its role
    or time, adds its token to ``active``. One part is in memory at a time."""
    keep: list[dict] = []
    managed = (path / STATE_DIR).is_dir()
    schemas = {p: pq.read_schema(p).names for p in committed_parts(path, allow_legacy=True)}
    flagged = any("verified" in names for names in schemas.values())
    for p, names in schemas.items():
        if managed and "verified" not in names:
            raise ValueError(f"committed order tape part lacks verification flags: {p}")
        for r in pq.read_table(p, columns=[c for c in PARQUET_COLS if c in names]).to_pylist():
            if flagged and r.get("verified") is not True:
                stats["rows_not_verified"] += 1
                continue
            active[r["maker"].lower()].add(r["token_id"])
            if r["role"] != "maker":
                continue
            c = r["created_ms"]
            if c is None or not t_from * 1000 <= c < t_to * 1000:
                stats["rows_out_of_range"] += 1
                continue
            if not managed:
                r["verified"] = False  # Standalone parquet has no published ordertape flush.
                stats["legacy_diagnostic_rows"] += 1
            keep.append(r)
    keep.sort(key=lambda r: (r["block_ts"], r["tx"]))
    return keep


def load_orders(path: Path, t_from: int, t_to: int) -> tuple[list[Order], dict[str, set[str]], Counter]:
    """Screened maker orders (one per order_id), every maker's tokens (any role), counts.
    ``path`` is a JSONL file or an ordertape parquet directory (see the module docstring)."""
    orders: dict[str, Order] = {}
    active: dict[str, set[str]] = defaultdict(set)
    stats: Counter = Counter()
    skip: set[str] = set()
    path = Path(path)
    if path.is_dir():
        rows = _parquet_rows(path, t_from, t_to, active, stats)
        flagged = False  # parquet rows already screened, including non-makers in active
    else:
        flagged = any("verified" in r for r in _jsonl_rows(path))
        rows = _jsonl_rows(path)
    for r in rows:
        if flagged and r.get("verified") is not True:
            stats["rows_not_verified"] += 1
            continue
        active[r["maker"].lower()].add(r["token_id"])
        if r.get("role") != "maker":
            continue
        oid = r["order_id"]
        if oid in skip:
            continue
        c = r.get("created_ms")
        if type(c) is not int or not t_from * 1000 <= c < t_to * 1000:
            stats["screened:out_of_range"] += 1
            skip.add(oid)
            orders.pop(oid, None)
            continue
        o = orders.get(oid)
        if not r.get("limit"):
            stats["screened:no_limit"] += 1
            skip.add(oid)
            orders.pop(oid, None)
            continue
        if o is None:
            o = orders[oid] = Order(oid, r["maker"].lower(), r["token_id"], r["side"],
                                    px(r["limit"]), float(r["size"]), c,
                                    verified=r.get("verified") is True, maker_amount=r.get("maker_amount"))
        elif ((o.maker, o.token, o.side, o.price, o.size, o.created_ms, o.maker_amount) !=
              (r["maker"].lower(), r["token_id"], r["side"], px(r["limit"]), float(r["size"]), c, r.get("maker_amount"))):
            stats["screened:conflicting_signed_fields"] += 1
            skip.add(oid)
            del orders[oid]
            continue
        sh, _ = o.fills.get(r["tx"], (0.0, None))
        o.fills[r["tx"]] = (sh + filled_shares(r), r.get("block_ts"))
        o.filled_amount += r["fill_amount"]
    for oid, o in list(orders.items()):
        # JSONL need not arrive oldest-first. A saved age is derived data; use signed
        # time and the earliest block directly, including when orders span many fills.
        stamps = [stamp for _, stamp in o.fills.values() if type(stamp) is int]
        if len(stamps) != len(o.fills) or not 0 <= min(stamps) * 1000 - o.created_ms < MAX_AGE_S * 1000:
            stats["screened:bad_age"] += 1
            del orders[oid]
    stats["maker_orders"] = len(orders)
    return sorted(orders.values(), key=lambda o: (o.created_ms, o.order_id)), dict(active), stats


# ---------------------------------------------------------------- tape
class LevelTape:
    """Level events per (asset, price, side) in tape order, prints, known spans and mates."""

    def __init__(self) -> None:
        self.events: dict[Key, list[LevelEvent]] = defaultdict(list)
        self.prints: dict[tuple[str, float], list[Print]] = defaultdict(list)
        self.prints_tx: dict[str, list[Print]] = defaultdict(list)
        self.allocations: dict[str, Allocation | str] = {}
        self.spans: dict[str, list[list[int]]] = {}
        self.mate: dict[str, str] = {}
        self.counts: Counter = Counter()

    def add(self, e: LevelEvent) -> None:
        self.events[(e.asset, e.price, e.side)].append(e)

    def add_print(self, p: Print) -> None:
        self.prints[(p.asset, p.price)].append(p)
        if p.tx:
            self.prints_tx[p.tx].append(p)

    def span_end(self, asset: str, ms: int) -> int | None:
        """End of the known span containing ``ms``; None when the state is unknown at ``ms``."""
        ends = [b for a, b in self.spans.get(asset, ()) if a <= ms <= b]
        return ends[0] if len(ends) == 1 else None

    def known_over(self, asset: str, lo: int, hi: int) -> bool:
        spans = [(a, b) for a, b in self.spans.get(asset, ()) if a <= hi and b >= lo]
        return lo <= hi and len(spans) == 1 and spans[0][0] <= lo and hi <= spans[0][1]

    def hours(self, assets: set[str]) -> float:
        return sum(b - a for t in assets for a, b in self.spans.get(t, ())) / 3.6e6

    def sides(self, asset: str, price: float) -> list[tuple[str, float]]:
        """The level and its mirror on the mate asset."""
        m = self.mate.get(asset)
        return [(asset, price)] + ([(m, px(1 - price))] if m else [])

    def printed(self, asset: str, price: float, ms: int, win: int) -> tuple[float, int]:
        """Volume and count of prints at the level or its mirror within +-win ms."""
        ps = [p for k in self.sides(asset, price) for p in self.prints.get(k, ())
              if abs(p.venue_ms - ms) <= win]
        return sum(p.size for p in ps), len(ps)


def _kept(batches: Iterable[list[Item]], tokens: set[str], fp_sizes: list[float], p: Params,
          markets: set[str], counts: Counter) -> Iterator[LevelEvent | Print]:
    """The prints and kept level events of ``build_tape`` in tape order; resets are counted."""
    for items in batches:
        markets.update(it.market for it in items
                       if isinstance(it, (LevelEvent, Print)) and it.asset in tokens)
        for it in items:
            if isinstance(it, Print):
                yield it
            elif isinstance(it, LevelEvent):
                if it.market in markets or it.asset in tokens or (
                        it.side == "BUY" and any(p.near(it.delta, z) for z in fp_sizes)):
                    yield it
            else:
                counts[f"reset:{it.why}"] += 1


def build_tape(root: Path, streams: list[str], t_from: int, t_to: int, tokens: set[str],
               fp_sizes: list[float], p: Params) -> LevelTape:
    """Replay the streams. Every level event is kept for the markets of ``tokens``; elsewhere
    only bid increases of a fingerprint size (the base-rate counts), to bound memory."""
    tape = LevelTape()
    markets: set[str] = set()
    for s in streams:
        book = IntlBook(strict_clocks=True)
        for it in _kept(replay(root, s, t_from, t_to, book, strict=True), tokens, fp_sizes, p, markets, tape.counts):
            tape.add_print(it) if isinstance(it, Print) else tape.add(it)
        tape.counts["price_change_unknown_state"] += book.dropped
        tape.counts.update(book.clock_counts)
        for a, sp in book.spans.items():
            tape.spans.setdefault(a, []).extend(sp)
        tape.mate.update(book.mates())
    return tape


# ---------------------------------------------------------------- placement
def _fill_prints(o: Order, tape: LevelTape) -> dict[str, Print | None]:
    """Bind a fill time to a unique venue instant with compatible transaction evidence."""
    out: dict[str, Print | None] = {}
    for tx, (shares, block_ts) in o.fills.items():
        allocation = tape.allocations.get(tx)
        compatible = []
        records = tape.prints_tx.get(tx, ())
        for q in records:
            if block_ts is None or abs(q.venue_ms - block_ts * 1000) > PRINT_BLOCK_TOL_MS:
                continue
            if isinstance(allocation, Allocation):
                valid = allocation.match(q, tape.mate.get(q.asset)) == "allocated"
            else:
                mirror = tape.mate.get(o.token) == q.asset
                valid = (allocation in (None, "incomplete_call") and (q.asset == o.token or mirror)
                         and q.price == (px(1 - o.price) if mirror else o.price)
                         and q.side == (o.side if mirror else ("SELL" if o.side == "BUY" else "BUY"))
                         and abs(q.size - shares) <= 2e-6)
            if valid:
                compatible.append(q)
        # All reports of the transaction must agree. One favorable print cannot
        # locate an execution when another copy contradicts its terms or clock.
        out[tx] = (min(compatible, key=lambda q: q.tw_ns)
                   if len(compatible) == len(records) and len({q.venue_ms for q in compatible}) == 1 else None)
    return out


@dataclass
class Search:
    selected: dict[str, tuple[Key, int]]
    candidates: Counter
    contenders: dict[str, int]
    rejected_chronology: Counter


def _placements(orders: list[Order], tape: LevelTape, p: Params, shift_ms: int,
                prints: dict[str, dict[str, Print | None]]) -> Search:
    by_key: dict[Key, list[Order]] = defaultdict(list)
    for o in orders:
        by_key[o.key].append(o)
    pairs = []
    counts: Counter = Counter()
    rejected: Counter = Counter()
    owners: dict[tuple, set[str]] = defaultdict(set)
    for k, os_ in by_key.items():
        evs = tape.events.get(k) or []
        # Book levels are reconstructed in receive order. Index separately for venue-time search.
        ordered = sorted((e.venue_ms, i) for i, e in enumerate(evs))
        stamps = [ms for ms, _ in ordered]
        canonical = _canonical(k, tape)
        for o in os_:
            anchor = o.created_ms + shift_ms
            window = ordered[bisect_left(stamps, anchor + p.lo_ms):bisect_right(stamps, anchor + p.hi_ms)]
            _order_pairs(o, k, ((i, evs[i]) for _, i in window), canonical, p, shift_ms, prints[o.order_id],
                         pairs, counts, rejected, owners)
    selected: dict[str, tuple[Key, int]] = {}
    contenders: dict[str, int] = {}
    _greedy(pairs, owners, selected, contenders)
    return Search(selected, counts, contenders, rejected)


def _canonical(k: Key, tape: LevelTape) -> Key:
    mirror = tape.mate.get(k[0])
    return min(k, (mirror, px(1 - k[1]), "SELL" if k[2] == "BUY" else "BUY")) if mirror else k


def _order_pairs(o: Order, k: Key, indexed: Iterable[tuple[int, LevelEvent]], canonical: Key, p: Params,
                 shift_ms: int, prints: dict[str, Print | None], pairs: list, counts: Counter,
                 rejected: Counter, owners: dict[tuple, set[str]]) -> None:
    """Candidate adds of one order among (index, event) of its key, appended to ``pairs``."""
    anchor = o.created_ms + shift_ms
    located = [q for q in prints.values() if q]
    cutoff_ms = min((q.venue_ms + shift_ms for q in located), default=None)
    cutoff_tw = min((q.tw_ns + shift_ms * 1_000_000 for q in located), default=None)
    for i, e in indexed:
        ms = e.venue_ms
        if not anchor + p.lo_ms <= ms <= anchor + p.hi_ms or e.delta <= 0 or not p.near(e.delta, o.size):
            continue
        if (cutoff_ms is not None and ms > cutoff_ms) or (cutoff_tw is not None and e.tw_ns >= cutoff_tw):
            rejected[o.order_id] += 1
            continue
        physical = (canonical, ms, round(e.delta, 6))
        pairs.append((abs(ms - anchor), o.order_id, k, i, physical))
        counts[o.order_id] += 1
        owners[physical].add(o.order_id)


def _greedy(pairs: list, owners: dict[tuple, set[str]], selected: dict[str, tuple[Key, int]],
            contenders: dict[str, int]) -> None:
    """Nearest pair first: one physical add per order and one order per physical add."""
    used = set()
    for _, oid, k, i, physical in sorted(pairs):
        if oid not in selected and physical not in used:
            selected[oid] = (k, i)
            contenders[oid] = len(owners[physical])
            used.add(physical)


def match_placements(orders: list[Order], tape: LevelTape, p: Params,
                     shift_ms: int = 0) -> dict[str, tuple[Key, int]]:
    """One representative per order and physical book change for diagnostics."""
    return _placements(orders, tape, p, shift_ms, {o.order_id: _fill_prints(o, tape) for o in orders}).selected


def qualified_placement(row: dict) -> bool:
    return (row.get("placement_evidence") == "unique_candidate"
            and type(row.get("placement_candidates")) is int and row["placement_candidates"] == 1
            and row.get("fill_before_placement") is False)


def _evidence(o: Order, tape: LevelTape, p: Params, search: Search, located: dict[str, Print | None],
              shift: int = 0) -> str:
    head = _evidence_head(o, tape, p, search, located, shift)
    if isinstance(head, str):
        return head
    evs = tape.events[head.key]
    for next_index in range(head.index + 1, len(evs)):
        if (res := head.step(evs[next_index])) is not None:
            return res
    return head.end()


@dataclass(slots=True)
class _EvScan:
    """The forward part of ``_evidence``: does the level empty before the last located fill?"""
    key: Key
    index: int
    lo_ms: int
    hi_ms: int
    cut_tw: int

    def step(self, change: LevelEvent) -> str | None:
        if change.tw_ns >= self.cut_tw:
            return self.end()
        if change.new_size == 0 and self.lo_ms <= change.venue_ms < self.hi_ms:
            return "level_empty_before_fill"
        return None

    def end(self) -> str:
        return "unique_candidate"


def _evidence_head(o: Order, tape: LevelTape, p: Params, search: Search, located: dict[str, Print | None],
                   shift: int = 0) -> str | _EvScan:
    chosen = search.selected.get(o.order_id)
    if not chosen:
        if search.candidates[o.order_id]:
            return "ambiguous"
        return "chronology_conflict" if search.rejected_chronology[o.order_id] else "no_candidate"
    if search.candidates[o.order_id] != 1 or search.contenders[o.order_id] != 1:
        return "ambiguous"
    if not o.verified:
        return "unverified_order"
    if o.maker_amount is not None and not 0 < o.filled_amount <= o.maker_amount:
        return "invalid_order_amount"
    if not located or not all(located.values()):
        return "fill_time_unknown"
    key, index = chosen
    e = tape.events[key][index]
    if e.venue_ms < o.created_ms + shift:
        return "chronology_conflict"
    if e.src != "price_change":
        return "snapshot_only"
    if not observable(o, tape, p, shift):
        return "coverage_incomplete"
    last_fill = max((q for q in located.values() if q), key=lambda q: (q.venue_ms, q.tw_ns))
    last_ms = last_fill.venue_ms + shift
    if not tape.known_over(o.token, e.venue_ms, last_ms):
        return "coverage_incomplete"
    return _EvScan(key, index, e.venue_ms, last_ms, last_fill.tw_ns + shift * 1_000_000)


def observable(o: Order, tape: LevelTape, p: Params, shift_ms: int = 0) -> bool:
    t = o.created_ms + shift_ms
    return tape.known_over(o.token, t + p.lo_ms, t + p.hi_ms)


def placement_candidate_count(events: list[LevelEvent], created_ms: int, size: float, p: Params,
                              first_fill_ms: int | None) -> int:
    """Count plausible book increases before applying one-to-one assignment."""
    lo = created_ms + p.lo_ms
    hi = created_ms + p.hi_ms
    if first_fill_ms is not None:
        hi = min(hi, first_fill_ms)
    return sum(lo <= e.venue_ms <= hi and p.near(e.delta, size) for e in events)


# ---------------------------------------------------------------- follow-forward
def follow(tape: LevelTape, key: Key, i0: int, r: float, start_ms: int,
           claimed: set[tuple[Key, int]], p: Params) -> dict:
    """Bound the end of a placement hypothesis by subsequent visible level evidence."""
    evs = tape.events[key]
    f = _Follow(tape, key, evs[i0], r, start_ms, p)
    if (res := f.first()) is not None:
        return res
    for j in range(i0 + 1, len(evs)):
        if (res := f.step(evs[j])) is not None:
            return res
    return f.end()


class _Follow:
    """``follow`` fed one later event of the key at a time, so a sliced run can carry it."""
    __slots__ = ("tape", "key", "start", "r", "start_ms", "p", "span_end", "prev_ms", "below")

    def __init__(self, tape: LevelTape, key: Key, start: LevelEvent, r: float, start_ms: int, p: Params) -> None:
        self.tape, self.key, self.start, self.r, self.start_ms, self.p = tape, key, start, r, start_ms, p
        self.span_end = tape.span_end(key[0], start.venue_ms)
        self.prev_ms = start.venue_ms
        self.below = False

    def first(self) -> dict | None:
        if self.span_end is None or self.start_ms > self.span_end:
            return {"outcome": "unknown", "reason": "censored", "end_ms": None}
        return None

    def step(self, e: LevelEvent) -> dict | None:
        key, p, r, span_end = self.key, self.p, self.r, self.span_end
        assert span_end is not None
        if e.venue_ms > span_end:
            return self.end()
        if e.venue_ms < self.prev_ms:
            return {"outcome": "unknown", "reason": "venue_time_inversion", "end_ms": None}
        self.prev_ms = e.venue_ms
        if e.venue_ms < self.start_ms:
            return None
        if e.new_size == 0:
            if not self.tape.known_over(key[0], self.start.venue_ms, e.venue_ms):
                return {"outcome": "unknown", "reason": "censored", "end_ms": None}
            # There is no resting size left by this observation. A level total does not
            # identify the owner who withdrew it or the instant their own order ended.
            vol, n = self.tape.printed(key[0], key[1], e.venue_ms, p.print_win_ms)
            return {"outcome": "unknown", "reason": "level_zero", "end_ms": e.venue_ms,
                    "end_src": e.src, "alone": p.near(e.old_size, r), "prints_near": n,
                    "cancel_compatible": p.near(-e.delta, r) and vol < -e.delta - p.tol(r)}
        self.below |= e.new_size < r - p.tol(r)
        return None

    def end(self) -> dict:
        return {"outcome": "unknown", "reason": "level_below" if self.below else "censored", "end_ms": None}


def classify(o: Order, key: Key, i0: int, tape: LevelTape, claimed: set[tuple[Key, int]],
             p: Params, *, search: Search | None = None, prints: dict[str, Print | None] | None = None) -> dict:
    """A representative placement hypothesis and the evidence supporting its timing."""
    place = tape.events[key][i0]
    prints = _fill_prints(o, tape) if prints is None else prints
    evidence = _evidence(o, tape, p, search, prints) if search else "competition_unchecked"
    row, end = _classified(o, key, place, tape, p, search, prints, evidence)
    if end is None:
        return row
    return _followed(row, follow(tape, key, i0, o.size - o.filled, end, claimed, p), end, place)


def _followed(row: dict, res: dict, end: int, place: LevelEvent) -> dict:
    return row | res | {"rest_min_ms": end - place.venue_ms,
                        "rest_max_ms": None if res["end_ms"] is None else res["end_ms"] - place.venue_ms,
                        "rest_known": False}


def _before(place: LevelEvent, prints: dict[str, Print | None]) -> bool:
    return any(q and (q.venue_ms < place.venue_ms or q.tw_ns <= place.tw_ns) for q in prints.values())


def _full(o: Order) -> bool:
    return o.filled_amount == o.maker_amount if o.maker_amount is not None else abs(o.size - o.filled) < 1e-6


def _classified(o: Order, key: Key, place: LevelEvent, tape: LevelTape, p: Params, search: Search | None,
                prints: dict[str, Print | None], evidence: str) -> tuple[dict, int | None]:
    """``classify`` up to follow-forward: (finished row, None) or (row, the follow start ms)."""
    stamp = [q.venue_ms for q in prints.values() if q]
    fills_at = [[tx, q.venue_ms if q else None, round(o.fills[tx][0], 6)] for tx, q in prints.items()]
    before = _before(place, prints)
    if before:
        evidence = "chronology_conflict"
    r = o.size - o.filled
    full = _full(o)
    end = max(stamp, default=place.venue_ms)
    row = {"order_id": o.order_id, "maker": o.maker, "token_id": o.token, "side": o.side,
           "price": o.price, "size": o.size, "created_ms": o.created_ms,
           "placed_venue_ms": place.venue_ms, "placed_tw_ns": place.tw_ns, "placed_src": place.src,
           "sign_to_book_ms": place.venue_ms - o.created_ms, "placement_evidence": evidence,
           "placement_candidates": (search.candidates[o.order_id] if search else placement_candidate_count(
               tape.events[key], o.created_ms, o.size, p, min(stamp, default=None))),
           "placement_contenders": search.contenders[o.order_id] if search else None,
           "filled": round(o.filled, 6), "remainder": round(r, 6), "n_fills": len(o.fills),
           "fills_located": len(stamp), "fill_before_placement": bool(before), "fills_at": fills_at,
           "print_price_ok": sum(bool(q and (q.asset, q.price) in tape.sides(o.token, o.price)) for q in prints.values()),
           "print_size_ok": sum(bool(q and abs(q.size - o.fills[tx][0]) <= 2e-6) for tx, q in prints.items())}
    if not qualified_placement(row):
        return row | {"outcome": "filled" if full else "unknown", "reason": evidence, "end_ms": None,
                      "rest_min_ms": 0, "rest_max_ms": None, "rest_known": False}, None
    if full:
        return row | {"outcome": "filled", "reason": "chain", "end_ms": end,
                      "rest_min_ms": end - place.venue_ms, "rest_max_ms": end - place.venue_ms,
                      "rest_known": True}, None
    return row, end


# ---------------------------------------------------------------- fingerprint
def fingerprint(sizes: list[float]) -> list[float]:
    """Fewest sizes (2 dp) covering FP_COVER of the orders; [] when that needs > FP_MAX_SIZES."""
    c = Counter(round(s, 2) for s in sizes)
    out: list[float] = []
    got = 0
    for s, n in c.most_common():
        if got >= FP_COVER * len(sizes):
            break
        out.append(s)
        got += n
    return sorted(out) if sizes and len(out) <= FP_MAX_SIZES else []


def attribute(wallet: str, sizes: list[float], orders: list[Order],
              matches: dict[str, tuple[Key, int]], active: set[str], tape: LevelTape,
              claimed: set[tuple[Key, int]], p: Params,
              sink: Callable[[dict], object] | None = None) -> tuple[dict, list[dict]]:
    """Fingerprint-size bid increases split into own / other_known / candidate / control.
    With ``sink`` each candidate row is handed to it and not kept (the returned list is empty):
    a quote-flickering wallet has ~10^5 candidates per hour."""
    active = active | {tape.mate[t] for t in active if t in tape.mate}
    known = _known(wallet, orders, {oid: (k, tape.events[k][i].venue_ms) for oid, (k, i) in matches.items()}, tape)
    per = _own_unmatched(wallet, sizes, orders, matches, p)
    rows: list[dict] = []
    emit = sink or rows.append
    outcomes: Counter = Counter()
    for key in sorted(k for k in tape.events if k[2] == "BUY"):
        for i, e in enumerate(tape.events[key]):
            s = next((z for z in sizes if p.near(e.delta, z)), None)
            if s is None:
                continue
            if key[0] not in active:
                per[s]["control"] += 1
                continue
            cid = (key[0], key[1], e.venue_ms)
            if known[True][cid] > 0:
                known[True][cid] -= 1
                per[s]["own"] += 1
            elif known[False][cid] > 0:
                known[False][cid] -= 1
                per[s]["other_known"] += 1
            else:
                per[s]["candidates"] += 1
                res = follow(tape, key, i, e.delta, e.venue_ms, claimed, p)
                outcomes[f"{res['outcome']}:{res['reason']}"] += 1
                emit(_candidate_row(wallet, key, e, s, res))
    return _fp_doc(sizes, per, dict(outcomes), active, tape), rows


def _candidate_row(wallet: str, key: Key, e: LevelEvent, s: float, res: dict) -> dict:
    hi = res["end_ms"]
    return {"attribution": "fingerprint_candidate", "wallet": wallet,
            "token_id": key[0], "price": key[1], "side_view": "BUY",
            "venue_ms": e.venue_ms, "tw_ns": e.tw_ns, "size": round(e.delta, 6),
            "fp_size": s} | res | {
                "rest_min_ms": 0,
                "rest_max_ms": None if hi is None else hi - e.venue_ms,
                "rest_known": False}


def _known(wallet: str, orders: list[Order], placed: dict[str, tuple[Key, int]],
           tape: LevelTape) -> dict[bool, Counter]:
    """own? -> BUY-view (asset, price, venue ms) of the matched placements (``placed``: key, venue ms)."""
    known: dict[bool, Counter] = {True: Counter(), False: Counter()}
    for o in orders:
        if o.order_id in placed:
            k, ms = placed[o.order_id]
            a, pr = (k[0], k[1]) if k[2] == "BUY" else tape.sides(k[0], k[1])[-1]
            known[o.maker == wallet][(a, pr, ms)] += 1
    return known


def _own_unmatched(wallet: str, sizes: list[float], orders: list[Order], matches: dict,
                   p: Params) -> dict[float, Counter]:
    per: dict[float, Counter] = {s: Counter() for s in sizes}
    for o in orders:  # the wallet's own decoded orders that found no placement still made an increase
        s = next((z for z in sizes if p.near(o.size, z)), None)
        if o.maker == wallet and o.order_id not in matches and s is not None:
            per[s]["own_unmatched"] += 1
    return per


def _fp_doc(sizes: list[float], per: dict[float, Counter], outcomes: dict, active: set[str],
            tape: LevelTape) -> dict:
    h_act = tape.hours(active)
    h_ctl = tape.hours(set(tape.spans) - active)

    def line(c: Counter) -> dict:
        rate = c["control"] / h_ctl if h_ctl > 0 else None
        exp = None if rate is None else rate * h_act
        ident = c["own"] + c["other_known"]
        return {"own": c["own"], "other_known": c["other_known"], "candidates": c["candidates"],
                "own_unmatched": c["own_unmatched"],
                "control": c["control"], "control_per_token_hour": _r(rate),
                "expected_by_chance": _r(exp),
                "net_estimate": None if exp is None else _r(
                    max(0.0, c["candidates"] - c["own_unmatched"] - exp)),
                "known_match_own_share": _r(c["own"] / ident) if ident else None,
                "identity_precision_upper": None}

    total: Counter = Counter()
    for c in per.values():
        total.update(c)
    return {"sizes": sizes, "active_tokens": len(active), "active_token_hours": _r(h_act),
            "control_token_hours": _r(h_ctl), "total": line(total),
            "per_size": {str(s): line(c) for s, c in per.items()},
            "candidate_outcomes": outcomes,
            "candidate_rest_ms_cancelled": None,
            "note": "candidates are NOT identified as the wallet's; read net_estimate against "
                    "expected_by_chance. True identity precision is unmeasured."}


# ---------------------------------------------------------------- report
def _r(x, n: int = 3):
    return None if x is None else round(float(x), n)


def _q(xs: list, qs=(0.1, 0.5, 0.9)) -> dict | None:
    if not xs:
        return None
    a = np.asarray(xs, dtype=float)
    return {f"p{int(q * 100)}": _r(np.quantile(a, q), 1) for q in qs} | {"n": len(xs)}


def summarize(name: str, orders: list[Order], candidates: dict, placebo: dict, rows: list[dict],
              comparable: set[str]) -> dict:
    """Match rate vs placebo, latency and outcomes for one set of maker orders."""
    obs = [o for o in orders if o.order_id in comparable]
    hit = [o for o in obs if candidates[o.order_id] == "unique_candidate"]
    pl = sum(placebo[o.order_id] == "unique_candidate" for o in obs)
    mine = {o.order_id for o in orders}
    selected = [r for r in rows if r["order_id"] in mine]
    rs = [r for r in selected if qualified_placement(r)]
    canc = [r for r in rs if r["outcome"] == "cancelled"]
    return {
        "wallet": name, "maker_orders": len(orders), "observable": len(obs), "matched": len(hit),
        "match_rate": _r(len(hit) / len(obs)) if obs else None,
        "placebo_matched": pl, "placebo_rate": _r(pl / len(obs)) if obs else None,
        "match_rate_by_side": {s: _r(sum(o.side == s for o in hit) / n) for s in ("BUY", "SELL")
                               if (n := sum(o.side == s for o in obs))},
        "matched_all": len(rs),
        "diagnostic_rows": len(selected) - len(rs),
        "placement_evidence_counts": dict(Counter(candidates[o.order_id] for o in orders)),
        "placebo_evidence_counts": dict(Counter(placebo[o.order_id] for o in obs)),
        "sign_to_book_ms": _q([r["sign_to_book_ms"] for r in rs]),
        "tw_minus_created_ms": _q([r["placed_tw_ns"] / 1e6 - r["created_ms"] for r in rs]),
        "outcomes": dict(Counter(f"{r['outcome']}:{r['reason']}" for r in rs)),
        "cancelled_alone": sum(r["alone"] for r in canc),
        "cancelled_no_prints_near": sum(r["prints_near"] == 0 for r in canc),
        "cancelled_seen_as_price_change": sum(r["end_src"] == "price_change" for r in canc),
        "fill_before_placement": sum(r["fill_before_placement"] for r in selected),
        "fills": {"n": sum(r["n_fills"] for r in rs), "located_by_print": sum(r["fills_located"] for r in rs),
                  "print_at_level": sum(r["print_price_ok"] for r in rs),
                  "print_size_equal": sum(r["print_size_ok"] for r in rs)},
        "rest_ms_known": {k: _q([r["rest_max_ms"] for r in rs if r["rest_known"] and r["outcome"] == k])
                          for k in ("filled", "cancelled")},
        "rest_not_known": sum(not r["rest_known"] for r in rs),
        "rest_censored": sum(r["rest_max_ms"] is None for r in rs),
    }


def run(root: Path, orders_path: Path, t_from: int, t_to: int, wallets: list[str] | None = None,
        p: Params = Params(), streams: list[str] | None = None, min_orders: int = 30, *,
        min_fp_orders: int = 0, focus: bool = False,
        quote_sink: Callable[[dict], object] | None = None) -> tuple[dict, list[dict], list[dict]]:
    """Returns (doc, per-order rows, fingerprint-candidate rows).

    Memory controls for a cohort run (defaults keep the tool's behaviour):
    ``min_fp_orders``: a chosen wallet with fewer maker orders in the range gets no fingerprint,
    and ``attribute`` is not run for it (a fingerprint from a handful of orders is noise).
    ``focus``: full level events are kept only for the markets of the CHOSEN wallets' tokens
    (other makers' orders are still loaded, so placements stay one-to-one and their matches in
    those markets still count as ``other_known``); ``others_pooled`` is then partial.
    ``quote_sink``: candidate rows go to it one at a time and the returned list is empty."""
    orders, active, stats = load_orders(orders_path, t_from, t_to)
    by_maker, chosen, fps = _chosen(orders, wallets, min_orders, min_fp_orders)
    streams = streams or _streams(root)
    tape = build_tape(root, streams, t_from, t_to, _tokens(active, chosen, focus), _fp_sizes(fps), p)
    if not orders_path.is_dir() or (orders_path / STATE_DIR).is_dir():
        tape.allocations = load_allocations(orders_path, {tx for o in orders for tx in o.fills})
    prints = {o.order_id: _fill_prints(o, tape) for o in orders}
    search = _placements(orders, tape, p, 0, prints)
    shifted = _placements(orders, tape, p, p.placebo_ms, prints)
    claimed: set[tuple[Key, int]] = set()
    rows = [classify(o, *search.selected[o.order_id], tape, claimed, p, search=search, prints=prints[o.order_id])
            for o in orders if o.order_id in search.selected]
    candidates = {o.order_id: _evidence(o, tape, p, search, prints[o.order_id]) for o in orders}
    candidates.update({r["order_id"]: r["placement_evidence"] for r in rows})
    placebo = {o.order_id: _evidence(o, tape, p, shifted, prints[o.order_id], p.placebo_ms) for o in orders}
    matches = {r["order_id"]: search.selected[r["order_id"]] for r in rows if qualified_placement(r)}
    level_keys = len(tape.events)
    quotes: list[dict] = []
    fp_docs = {}
    for w in chosen:
        if fps[w]:
            fp_docs[w], q = attribute(w, fps[w], orders, matches, active.get(w, set()), tape, claimed, p, quote_sink)
            quotes += q
    doc = _report(orders, by_maker, chosen, stats, tape, p, t_from, t_to, streams, prints, rows, candidates,
                  placebo, fp_docs, level_keys, sum(len(v) for v in tape.prints.values()))
    return doc, rows, quotes


def _streams(root: Path) -> list[str]:
    return sorted(d.name for d in Path(root).iterdir() if d.name.startswith("clob_"))


def _chosen(orders: list[Order], wallets: list[str] | None, min_orders: int,
            min_fp_orders: int) -> tuple[dict[str, list[Order]], list[str], dict[str, list[float]]]:
    by_maker: dict[str, list[Order]] = defaultdict(list)
    for o in orders:
        by_maker[o.maker].append(o)
    chosen = ([w.lower() for w in wallets] if wallets else
              sorted(w for w, os_ in by_maker.items() if len(os_) >= min_orders))
    fps = {w: fingerprint([o.size for o in by_maker.get(w, [])])
           if len(by_maker.get(w, [])) >= min_fp_orders else [] for w in chosen}
    return by_maker, chosen, fps


def _tokens(active: dict[str, set[str]], chosen: list[str], focus: bool) -> set[str]:
    return {t for w, ts in active.items() if not focus or w in chosen for t in ts}


def _fp_sizes(fps: dict[str, list[float]]) -> list[float]:
    return sorted({s for f in fps.values() for s in f})


def _report(orders: list[Order], by_maker: dict[str, list[Order]], chosen: list[str], stats: Counter,
            tape: LevelTape, p: Params, t_from: int, t_to: int, streams: list[str],
            prints: dict[str, dict[str, Print | None]], rows: list[dict], candidates: dict, placebo: dict,
            fp_docs: dict[str, dict], level_keys: int, n_prints: int) -> dict:
    """ordermatch.json from the per-order results (totals are counted over all orders at once)."""
    comparable = set()
    for o in orders:
        located = prints[o.order_id]
        if not o.verified or not located or not all(located.values()):
            continue
        last = max(q.venue_ms for q in located.values() if q)
        if all(tape.known_over(o.token, o.created_ms + p.lo_ms + shift,
                               max(o.created_ms + p.hi_ms, last) + shift) for shift in (0, p.placebo_ms)):
            comparable.add(o.order_id)
    doc = {"params": {"from": t_from, "to": t_to, **p.__dict__, "max_age_s": MAX_AGE_S,
                      "streams": streams, "placement_evidence_version": PLACEMENT_EVIDENCE_VERSION},
           "orders": dict(stats), "tape": dict(tape.counts) | {
               "level_keys": level_keys, "assets": len(tape.spans),
               "prints": n_prints,
               "integrity": "strict_segment_frames_and_crc", "sealed_manifest_checked": False},
           "wallets": {}}
    for w in chosen:
        d = summarize(w, by_maker.get(w, []), candidates, placebo, rows, comparable)
        d["fingerprint"] = fp_docs.get(w)
        doc["wallets"][w] = d
    rest = [o for o in orders if o.maker not in chosen]
    if rest:
        doc["others_pooled"] = summarize("others", rest, candidates, placebo, rows, comparable)
    return doc


# ---------------------------------------------------------------- sliced run
# The same result as ``run`` with level events held only while an order needs them. Three
# replays of the one stream, each through ``_kept`` so all see the same events in the same
# order (global per-key index included):
# 1. survey: spans, mates, counts, print times and sizes per level, prints of the decoded txs,
#    and per slice and asset the min/max venue ms of the kept level events.
# 2. match: an order pins its key's events from the last slice boundary before which every event
#    of its token was earlier than its search window, until it is placed: its window is complete
#    once every later slice's events are past the placebo window. One-to-one placement is the
#    same nearest-first greedy, run per connected group of orders sharing a physical add once no
#    other order can still reach those adds. The forward scans (`_EvScan`, `_Follow`) are then
#    fed the remaining events one at a time and carry their state across slice boundaries.
# 3. attribute (only with fingerprints): the known matches are final, so every fingerprint-size
#    bid increase is counted in tape order and a candidate is followed like in pass 2.
# Memory: the pinned events (about one slice plus the placebo window), the open scans, per-order
# rows, and the survey (spans plus 16 bytes per print). Only one stream: the unsliced per-key
# event order is stream-major, so several streams fall back to ``run``.
class _Window:
    """The events of one key from global index ``base`` on (earlier ones are freed)."""
    __slots__ = ("buf", "base")

    def __init__(self, base: int) -> None:
        self.buf: list[LevelEvent] = []
        self.base = base

    def __getitem__(self, i: int) -> LevelEvent:
        if i < self.base:
            raise IndexError(f"level event {i} was already freed (window starts at {self.base})")
        return self.buf[i - self.base]

    def __len__(self) -> int:
        return self.base + len(self.buf)


class _SlicedTape(LevelTape):
    """LevelTape without stored level events (``events`` holds the pinned windows only)."""

    def __init__(self, n_slices: int) -> None:
        super().__init__()
        self.windows: dict[Key, _Window] = {}
        self.events = self.windows  # type: ignore[assignment]  # read by index only
        self.level_prints: dict[tuple[str, float], tuple[array, array]] = {}  # venue ms, size in tape order
        self.n_prints = 0
        self.level_keys = 0
        self.vmin: list[dict[str, int]] = [{} for _ in range(n_slices)]
        self.vmax: list[dict[str, int]] = [{} for _ in range(n_slices)]

    def printed(self, asset: str, price: float, ms: int, win: int) -> tuple[float, int]:
        sizes = [s for k in self.sides(asset, price) for v, s in zip(*self.level_prints.get(k, ((), ())))
                 if abs(v - ms) <= win]
        return sum(sizes), len(sizes)  # sum() as LevelTape.printed: 3.12 sums floats compensated


def _slicer(t_from: int, slice_s: int) -> Callable[[int], int]:
    last = [0]

    def at(tw_ns: int) -> int:  # never decreases, so a receive-time reversal stays in its slice
        last[0] = max(last[0], (tw_ns // NS - t_from) // slice_s)
        return last[0]
    return at


def _survey(root: Path, stream: str, t_from: int, t_to: int, tokens: set[str], fp_sizes: list[float], p: Params,
            txs: set[str], slice_s: int) -> _SlicedTape:
    tape = _SlicedTape(-(-(t_to - t_from) // slice_s))
    book = IntlBook(strict_clocks=True)
    at = _slicer(t_from, slice_s)
    keys: set[Key] = set()
    for it in _kept(replay(root, stream, t_from, t_to, book, strict=True), tokens, fp_sizes, p, set(), tape.counts):
        b = at(it.tw_ns)
        if isinstance(it, Print):
            tape.n_prints += 1
            vs, ss = tape.level_prints.setdefault((it.asset, it.price), (array("q"), array("d")))
            vs.append(it.venue_ms)
            ss.append(it.size)
            if it.tx and it.tx in txs:
                tape.prints_tx[it.tx].append(it)
        else:
            keys.add((it.asset, it.price, it.side))
            lo, hi = tape.vmin[b], tape.vmax[b]
            lo[it.asset] = min(lo.get(it.asset, it.venue_ms), it.venue_ms)
            hi[it.asset] = max(hi.get(it.asset, it.venue_ms), it.venue_ms)
    tape.counts["price_change_unknown_state"] += book.dropped
    tape.counts.update(book.clock_counts)
    for a, sp in book.spans.items():
        tape.spans.setdefault(a, []).extend(sp)
    tape.mate.update(book.mates())
    tape.level_keys = len(keys)
    return tape


class _Task:
    """One forward scan of an order (evidence, placebo evidence or follow) on its key."""
    __slots__ = ("oid", "name", "scan", "result")

    def __init__(self, oid: str, name: str, scan: _EvScan | _Follow) -> None:
        self.oid, self.name, self.scan = oid, name, scan
        self.result: str | dict | None = None


def _match_sliced(orders: list[Order], tape: _SlicedTape, p: Params, prints: dict[str, dict[str, Print | None]],
                  events: Iterator[LevelEvent | Print], at: Callable[[int], int]
                  ) -> tuple[list[dict], dict[str, str], dict[str, str]]:
    """Pass 2: (rows, placement evidence, placebo evidence), as ``run`` computes them."""
    shifts = (0, p.placebo_ms)
    n_slices = len(tape.vmin)
    inf = float("inf")
    by_id = {o.order_id: o for o in orders}
    reg_at: dict[int, list[Order]] = defaultdict(list)
    done_at: dict[int, list[Order]] = defaultdict(list)
    bounds: dict[str, tuple[list[float], list[float]]] = {}
    for a in {o.token for o in orders}:
        pre: list[float] = [-inf]  # max venue ms of the slices before boundary b
        for b in range(n_slices):
            pre.append(max(pre[-1], tape.vmax[b].get(a, -inf)))
        suf: list[float] = [inf] * (n_slices + 1)  # min venue ms from slice b on
        for b in reversed(range(n_slices)):
            suf[b] = min(suf[b + 1], tape.vmin[b].get(a, inf))
        bounds[a] = (pre, suf)
    for o in orders:
        pre, suf = bounds[o.token]
        done = bisect_right(suf, o.created_ms + p.placebo_ms + p.hi_ms)
        reg = min(bisect_left(pre, o.created_ms + p.lo_ms) - 1, done)
        reg_at[reg].append(o)
        done_at[done].append(o)
    canon = {o.order_id: _canonical(o.key, tape) for o in orders}
    by_canon: dict[Key, list[Order]] = defaultdict(list)
    for o in orders:  # created order
        by_canon[canon[o.order_id]].append(o)
    created = {c: [o.created_ms for o in os_] for c, os_ in by_canon.items()}
    complete: set[str] = set()
    searches = [Search({}, Counter(), {}, Counter()) for _ in shifts]
    pending: list[dict[str, list]] = [{} for _ in shifts]  # oid -> its pairs, until its group is placed
    owners: list[dict[tuple, set[str]]] = [defaultdict(set) for _ in shifts]
    resolved: list[set[str]] = [set() for _ in shifts]  # placed in that search, not yet settled
    ready: list[str] = []
    pins: dict[Key, dict[str, int]] = defaultdict(dict)
    count: Counter = Counter()  # events seen per key = the next global index
    live: dict[Key, list[_Task]] = defaultdict(list)
    open_tasks: dict[str, list[_Task]] = {}
    heads: dict[str, tuple[str | _EvScan, str | _EvScan, LevelEvent | None]] = {}
    rows: dict[str, dict] = {}
    cand: dict[str, str] = {}
    plc: dict[str, str] = {}

    def mark(si: int, oid: str) -> None:
        resolved[si].add(oid)
        if oid in resolved[1 - si]:
            resolved[0].discard(oid)
            resolved[1].discard(oid)
            ready.append(oid)

    def reachable_by_open(ph: tuple, shift: int) -> bool:
        os_, cs = by_canon[ph[0]], created[ph[0]]
        lo = bisect_left(cs, ph[1] - shift - p.hi_ms)
        hi = bisect_right(cs, ph[1] - shift - p.lo_ms)
        return any(o.order_id not in complete for o in os_[lo:hi])

    def place_groups(si: int) -> None:
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for oids in owners[si].values():
            first, *more = oids
            for x in more:
                parent[find(x)] = find(first)
        groups: dict[str, tuple[list[str], list[tuple]]] = defaultdict(lambda: ([], []))
        for oid in pending[si]:
            groups[find(oid)][0].append(oid)
        for ph, oids in owners[si].items():
            groups[find(next(iter(oids)))][1].append(ph)
        for oids, phs in groups.values():
            if any(reachable_by_open(ph, shifts[si]) for ph in phs):
                continue
            _greedy([x for oid in oids for x in pending[si][oid]], owners[si],
                    searches[si].selected, searches[si].contenders)
            for oid in oids:
                del pending[si][oid]
                mark(si, oid)
            for ph in phs:
                del owners[si][ph]

    def finish(oid: str) -> None:
        o = by_id[oid]
        head0, head_p, place = heads.pop(oid)
        got = {t.name: t.result for t in open_tasks.pop(oid, [])}
        ev0 = head0 if isinstance(head0, str) else got["evidence"]
        assert isinstance(ev0, str)
        if place is not None:
            row, end = _classified(o, o.key, place, tape, p, searches[0], prints[oid], ev0)
            if end is not None:
                res = got["follow"]
                assert isinstance(res, dict)
                row = _followed(row, res, end, place)
            rows[oid] = row
            cand[oid] = row["placement_evidence"]
        else:
            cand[oid] = ev0
        ev_p = head_p if isinstance(head_p, str) else got["placebo"]
        assert isinstance(ev_p, str)
        plc[oid] = ev_p

    def resolve(t: _Task, res: str | dict) -> None:
        t.result = res
        if all(x.result is not None for x in open_tasks[t.oid]):
            finish(t.oid)

    def settle(o: Order) -> None:
        """Both searches placed: start the forward scans from the order's chosen adds."""
        oid, k, located = o.order_id, o.key, prints[o.order_id]
        w = tape.windows[k]
        chosen = searches[0].selected.get(oid)
        place = w[chosen[1]] if chosen else None
        head0 = _evidence_head(o, tape, p, searches[0], located, 0)
        head_p = _evidence_head(o, tape, p, searches[1], located, p.placebo_ms)
        heads[oid] = (head0, head_p, place)
        tasks: list[tuple[str, _EvScan | _Follow, int]] = []
        if isinstance(head0, _EvScan):
            tasks.append(("evidence", head0, head0.index))
            if place is not None and not _full(o) and not _before(place, located):
                start_ms = max((q.venue_ms for q in located.values() if q), default=place.venue_ms)
                tasks.append(("follow", _Follow(tape, k, place, o.size - o.filled, start_ms, p), head0.index))
        if isinstance(head_p, _EvScan):
            tasks.append(("placebo", head_p, head_p.index))
        open_tasks[oid] = [_Task(oid, name, scan) for name, scan, _ in tasks]
        for t, (_, scan, i0) in zip(open_tasks[oid], tasks):
            res = scan.first() if isinstance(scan, _Follow) else None
            for j in range(i0 + 1, len(w)):
                if res is not None:
                    break
                res = scan.step(w[j])
            if res is None:
                live[k].append(t)
            else:
                t.result = res
        if all(t.result is not None for t in open_tasks[oid]):
            finish(oid)
        del pins[k][oid]

    def boundary(b: int) -> None:
        for o in reg_at.pop(b, ()):
            k = o.key
            if not pins[k]:
                tape.windows[k] = _Window(count[k])
            pins[k][o.order_id] = count[k]
        for o in done_at.pop(b, ()):
            oid, k = o.order_id, o.key
            complete.add(oid)
            w, i0 = tape.windows[k], pins[k][oid]
            for si, shift in enumerate(shifts):
                pairs: list = []
                _order_pairs(o, k, ((i, w[i]) for i in range(i0, len(w))), canon[oid], p, shift, prints[oid],
                             pairs, searches[si].candidates, searches[si].rejected_chronology, owners[si])
                if pairs:
                    pending[si][oid] = pairs
                else:
                    mark(si, oid)
        for si in range(len(shifts)):
            place_groups(si)
        while ready:
            settle(by_id[ready.pop()])
        for k in [k for k, ps in pins.items() if not ps]:
            del pins[k]
            tape.windows.pop(k, None)
        for k, ps in pins.items():
            w = tape.windows[k]
            cut = min(ps.values())
            del w.buf[:cut - w.base]
            w.base = cut

    b = 0
    boundary(0)
    for e in events:
        sl = at(e.tw_ns)  # prints too, as in the survey
        if not isinstance(e, LevelEvent):
            continue
        while b < sl:
            b += 1
            boundary(b)
        k = (e.asset, e.price, e.side)
        if k in live:
            keep = []
            for t in live[k]:
                res = t.scan.step(e)
                if res is None:
                    keep.append(t)
                else:
                    resolve(t, res)
            if keep:
                live[k] = keep
            else:
                del live[k]
        if k in pins:
            tape.windows[k].buf.append(e)
        count[k] += 1
    while b < n_slices:
        b += 1
        boundary(b)
    for ts in list(live.values()):
        for t in ts:
            resolve(t, t.scan.end())
    assert len(cand) == len(orders) and not pins, "sliced match left orders unsettled"
    return [rows[o.order_id] for o in orders if o.order_id in rows], cand, plc


def _attribute_sliced(wallets: list[tuple[str, list[float], set[str]]], orders: list[Order],
                      placed: dict[str, tuple[Key, int]], tape: _SlicedTape, p: Params,
                      events: Iterator[LevelEvent | Print], emit: Callable[[int, tuple, dict], object]
                      ) -> list[dict]:
    """Pass 3: ``attribute`` for every fingerprinted wallet in one replay; rows go to ``emit``
    (wallet index, (key, index), row) as their follow ends."""
    state = []
    for w, sizes, act in wallets:
        act = act | {tape.mate[t] for t in act if t in tape.mate}
        state.append((w, sizes, act, _known(w, orders, placed, tape), _own_unmatched(w, sizes, orders, placed, p),
                      {}))
    count: Counter = Counter()
    live: dict[Key, list[tuple[_Follow, LevelEvent, tuple, list[tuple[int, float]]]]] = defaultdict(list)

    def done(e: LevelEvent, pos: tuple, waiting: list[tuple[int, float]], res: dict) -> None:
        label = f"{res['outcome']}:{res['reason']}"
        for wi, s in waiting:
            outs = state[wi][5]
            first, n = outs.get(label, (pos, 0))
            outs[label] = (min(first, pos), n + 1)
            emit(wi, pos, _candidate_row(state[wi][0], pos[0], e, s, res))

    for e in events:
        if not isinstance(e, LevelEvent):
            continue
        key = (e.asset, e.price, e.side)
        i = count[key]
        count[key] += 1
        if key in live:
            keep = []
            for f, e0, pos, waiting in live[key]:
                res = f.step(e)
                if res is None:
                    keep.append((f, e0, pos, waiting))
                else:
                    done(e0, pos, waiting, res)
            if keep:
                live[key] = keep
            else:
                del live[key]
        if key[2] != "BUY":
            continue
        waiting: list[tuple[int, float]] = []
        for wi, (_, sizes, act, known, per, _) in enumerate(state):
            s = next((z for z in sizes if p.near(e.delta, z)), None)
            if s is None:
                continue
            if key[0] not in act:
                per[s]["control"] += 1
                continue
            cid = (key[0], key[1], e.venue_ms)
            if known[True][cid] > 0:
                known[True][cid] -= 1
                per[s]["own"] += 1
            elif known[False][cid] > 0:
                known[False][cid] -= 1
                per[s]["other_known"] += 1
            else:
                per[s]["candidates"] += 1
                waiting.append((wi, s))
        if waiting:
            f = _Follow(tape, key, e, e.delta, e.venue_ms, p)
            if (res := f.first()) is not None:
                done(e, (key, i), waiting, res)
            else:
                live[key].append((f, e, (key, i), waiting))
    for entries in live.values():
        for f, e0, pos, waiting in entries:
            done(e0, pos, waiting, f.end())
    return [_fp_doc(sizes, per, {lb: n for lb, (_, n) in sorted(outs.items(), key=lambda kv: kv[1][0])}, act, tape)
            for _, sizes, act, _, per, outs in state]


def run_sliced(root: Path, orders_path: Path, t_from: int, t_to: int, wallets: list[str] | None = None,
               p: Params = Params(), streams: list[str] | None = None, min_orders: int = 30, *,
               slice_s: int = 1800, min_fp_orders: int = 0, focus: bool = False,
               quote_sink: Callable[[dict], object] | None = None) -> tuple[dict, list[dict], list[dict]]:
    """``run`` with level events held one slice at a time (see the comment above ``_Window``).
    Same doc and rows. Fingerprint rows are returned in ``run``'s order; with ``quote_sink`` they
    go to it in the order their follow ends (the same rows)."""
    if slice_s <= 0:
        raise ValueError("slice_s must be positive")
    streams = streams or _streams(root)
    if len(streams) != 1:
        print(f"doppel-ordermatch: {len(streams)} streams; slicing needs one, running unsliced", file=sys.stderr)
        return run(root, orders_path, t_from, t_to, wallets, p, streams, min_orders, min_fp_orders=min_fp_orders,
                   focus=focus, quote_sink=quote_sink)
    orders, active, stats = load_orders(orders_path, t_from, t_to)
    by_maker, chosen, fps = _chosen(orders, wallets, min_orders, min_fp_orders)
    tokens, fp_sizes = _tokens(active, chosen, focus), _fp_sizes(fps)

    def events() -> Iterator[LevelEvent | Print]:
        book = IntlBook(strict_clocks=True)
        return _kept(replay(root, streams[0], t_from, t_to, book, strict=True), tokens, fp_sizes, p, set(), Counter())

    tape = _survey(root, streams[0], t_from, t_to, tokens, fp_sizes, p, {tx for o in orders for tx in o.fills},
                   slice_s)
    if not orders_path.is_dir() or (orders_path / STATE_DIR).is_dir():
        tape.allocations = load_allocations(orders_path, {tx for o in orders for tx in o.fills})
    prints = {o.order_id: _fill_prints(o, tape) for o in orders}
    rows, candidates, placebo = _match_sliced(orders, tape, p, prints, events(), _slicer(t_from, slice_s))
    placed = {r["order_id"]: ((r["token_id"], r["price"], r["side"]), r["placed_venue_ms"])
              for r in rows if qualified_placement(r)}
    fp_wallets = [w for w in chosen if fps[w]]
    quotes: list[tuple[int, tuple, dict]] = []
    fp_docs = {}
    if fp_wallets:
        emit = ((lambda wi, pos, row: quote_sink(row)) if quote_sink else
                (lambda wi, pos, row: quotes.append((wi, pos, row))))
        docs = _attribute_sliced([(w, fps[w], active.get(w, set())) for w in fp_wallets], orders, placed, tape, p,
                                 events(), emit)
        fp_docs = dict(zip(fp_wallets, docs))
    doc = _report(orders, by_maker, chosen, stats, tape, p, t_from, t_to, streams, prints, rows, candidates,
                  placebo, fp_docs, tape.level_keys, tape.n_prints)
    return doc, rows, [row for _, _, row in sorted(quotes, key=lambda q: (q[0], q[1]))]


def card(doc: dict) -> str:
    L = [f"orders: {doc['orders']}", f"tape: {doc['tape']}"]
    for d in list(doc["wallets"].values()) + ([doc["others_pooled"]] if "others_pooled" in doc else []):
        L += [f"\n== {d['wallet']}",
              f"placement: {d['matched']}/{d['observable']} observable orders matched "
              f"(rate {d['match_rate']}) vs placebo {d['placebo_matched']} (rate {d['placebo_rate']}); "
              f"by side {d['match_rate_by_side']}",
              f"placement evidence: {d['placement_evidence_counts']}; diagnostic rows {d['diagnostic_rows']}",
              f"sign-to-book ms (venue - created): {d['sign_to_book_ms']}; tw - created: {d['tw_minus_created_ms']}",
              f"outcomes: {d['outcomes']} | cancelled alone {d['cancelled_alone']}, "
              f"no prints near {d['cancelled_no_prints_near']}, as price_change "
              f"{d['cancelled_seen_as_price_change']} | WRONG MATCHES (fill printed before placement): "
              f"{d['fill_before_placement']}",
              f"fills: {d['fills']}",
              f"resting ms where known: {d['rest_ms_known']}; not known {d['rest_not_known']} "
              f"(censored {d['rest_censored']})"]
        fp = d.get("fingerprint")
        if fp:
            t = fp["total"]
            L += [f"fingerprint sizes {fp['sizes']} (ESTIMATE, not identification): "
                  f"own {t['own']}, other makers matched {t['other_known']}, candidates {t['candidates']} "
                  f"(own filled-but-unmatched hiding among them: {t['own_unmatched']})",
                  f"  chance: control {t['control']} in {fp['control_token_hours']} token-h -> expected "
                  f"{t['expected_by_chance']} in {fp['active_token_hours']} active token-h; "
                  f"net {t['net_estimate']}; own share among matched orders {t['known_match_own_share']}",
                  f"  per size: { {s: (v['candidates'], v['expected_by_chance'], v['other_known']) for s, v in fp['per_size'].items()} }"
                  " (candidates, expected, other_known)",
                  f"  candidate outcomes {fp['candidate_outcomes']}; resting ms of cancelled {fp['candidate_rest_ms_cancelled']}"]
        elif "fingerprint" in d:
            L.append("fingerprint: none (sizes too diffuse)")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-ordermatch")
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--orders", type=Path, required=True,
                    help="doppel-ordertape output: JSONL sample, or the cohort parquet directory "
                         "(verified rows only)")
    ap.add_argument("--wallet", nargs="+", action="extend", help="maker address(es) (default: makers "
                    "with >= --min-orders orders)")
    ap.add_argument("--from", dest="t_from", type=_ts, required=True, help="unix s or ISO UTC")
    ap.add_argument("--to", dest="t_to", type=_ts, required=True)
    ap.add_argument("--out", type=Path, required=True, help="directory")
    ap.add_argument("--min-orders", type=int, default=30)
    ap.add_argument("--lo-ms", type=int, default=Params.lo_ms)
    ap.add_argument("--hi-ms", type=int, default=Params.hi_ms)
    ap.add_argument("--size-tol", type=float, default=Params.size_tol, help="shares")
    ap.add_argument("--size-tol-rel", type=float, default=Params.size_tol_rel)
    ap.add_argument("--slice-s", type=int, default=SLICE_S,
                    help="hold level events one slice of this many seconds at a time (same results; "
                         "quotes.jsonl rows in the order their follow ends); 0 = whole span in memory")
    a = ap.parse_args(argv)
    if a.slice_s < 0:
        ap.error("--slice-s must be >= 0")
    p = Params(lo_ms=a.lo_ms, hi_ms=a.hi_ms, size_tol=a.size_tol, size_tol_rel=a.size_tol_rel)
    a.out.mkdir(parents=True, exist_ok=True)
    with open(a.out / "quotes.jsonl", "wb") as qf:
        def sink(r: dict) -> None:
            qf.write(orjson.dumps(r) + b"\n")
        if a.slice_s:
            doc, rows, _ = run_sliced(a.data, a.orders, a.t_from, a.t_to, a.wallet, p, min_orders=a.min_orders,
                                      slice_s=a.slice_s, quote_sink=sink)
        else:
            doc, rows, quotes = run(a.data, a.orders, a.t_from, a.t_to, a.wallet, p, min_orders=a.min_orders)
            for r in quotes:
                sink(r)
    (a.out / "ordermatch.json").write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
    with open(a.out / "orders.jsonl", "wb") as f:
        for r in rows:
            f.write(orjson.dumps(r) + b"\n")
    print(card(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
