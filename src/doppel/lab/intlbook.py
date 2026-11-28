"""Intl CLOB L2 reconstruction from the raw market-channel tape (``clob_<asset>_<hz>`` streams).

Frames (see ``tests/fixtures_formats.json``):
- ``book``: full snapshot of one ``asset_id`` (``bids``/``asks`` = [{price, size}]).
- ``price_change``: ``price_changes`` = [{asset_id, price, size, side}], ``size`` is the NEW TOTAL
  size of that level (0 removes it).
- ``last_trade_price``: one print (asset_id, price, size, taker side, transaction_hash).

``IntlBook`` keeps per-asset level sizes and turns frames into ``LevelEvent`` (a level went from
``old_size`` to ``new_size``) and ``Print``. Rules:
- An asset's levels are unknown until its first ``book``. ``price_change`` rows for an unknown
  asset are dropped (counted in ``dropped``): a delta against an unknown size is not a delta.
- A ``book`` for a known asset is diffed against the state and every changed level is emitted
  (``src="book"``; levels missing from the snapshot go to 0). A fill may only ever show up this
  way. The first ``book`` after unknown state emits nothing.
- Control records (``raw == ""``, ``meta.ctl``): everything except the subscription notes
  (``subscribe``, ``subscribe_add``, ``unsubscribe``) means frames may have been lost (``connect``,
  ``disconnect``, ``stall``, the Rust recorder's ``backpressure_drop``, anything unrecognised), so
  ALL assets go back to unknown until their next ``book``. A missing hour segment does the same.
- ``unsubscribe`` does not say which assets were dropped, so an expired window just stops
  producing frames. ``spans`` therefore ends each known interval at the asset's LAST FRAME, not at
  the reset: [first book, last frame seen] in venue ms. Nothing is claimed about an asset outside
  its spans.

Records are applied strictly in tape order, one at a time, and an event only depends on frames
already applied, so a twin adapter can drive ``IntlBook.feed`` live without look-ahead.
``venue_ms`` is the frame's own ``timestamp`` (receive time when it has none); ``tw_ns`` is ours.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
import math
from pathlib import Path

import orjson

from ..tape.segment import read_segment, segment_path

NO_RESET_CTL = frozenset({"subscribe", "subscribe_add", "unsubscribe"})
NS = 1_000_000_000


@dataclass(frozen=True, slots=True)
class LevelEvent:
    venue_ms: int
    tw_ns: int
    asset: str
    price: float
    side: str  # BUY (bid level) | SELL (ask level)
    old_size: float
    new_size: float
    src: str  # "price_change" | "book" (snapshot diff)
    market: str

    @property
    def delta(self) -> float:
        return self.new_size - self.old_size


@dataclass(frozen=True, slots=True)
class Print:
    venue_ms: int
    tw_ns: int
    asset: str
    price: float
    size: float
    side: str  # taker side
    tx: str
    market: str


@dataclass(frozen=True, slots=True)
class Reset:
    tw_ns: int
    why: str


@dataclass(frozen=True, slots=True)
class BookObservation:
    """A venue-stamped book message that produced no level change on this asset."""
    venue_ms: int
    tw_ns: int
    asset: str


Item = LevelEvent | Print | Reset
_SnapshotKey = tuple[object, int, int]


@dataclass(frozen=True, slots=True)
class _CachedSnapshot:
    """A complete snapshot with an optional patch from a specific earlier snapshot."""

    message: dict
    key: _SnapshotKey
    previous: _SnapshotKey | None
    delta: list[dict] | None
    load_levels: Callable[[], tuple[list, list]]


def px(x: object) -> float:
    """Price key: 4 decimals (ticks are 0.01 or 0.001)."""
    return round(float(x), 4)  # type: ignore[arg-type]


class IntlBook:
    """Level sizes of every asset on one connection (one stream)."""

    def __init__(self, *, strict_clocks: bool = False) -> None:
        self.levels: dict[str, dict[tuple[float, str], float]] = {}  # known assets only
        self.market_of: dict[str, str] = {}
        self.spans: dict[str, list[list[int]]] = {}  # asset -> [[from_ms, to_ms], ...]
        self.dropped = 0  # price_change rows for assets with unknown state
        self.strict_clocks = strict_clocks
        self.clock_counts: Counter = Counter()
        self._snapshot_anchors: dict[str, _SnapshotKey] = {}

    def known(self, asset: str) -> bool:
        return asset in self.levels

    def size(self, asset: str, price: float, side: str) -> float | None:
        """Current size of a level; None while the asset's state is unknown."""
        lv = self.levels.get(asset)
        return None if lv is None else lv.get((px(price), side), 0.0)

    def reset(self) -> None:
        self.levels.clear()
        self._snapshot_anchors.clear()

    def _seen(self, asset: str, ms: int) -> None:
        span = self.spans[asset][-1]
        span[1] = max(span[1], ms)

    def _check_depth_time(self, asset: str, ms: int) -> None:
        if self.strict_clocks and asset in self.levels and ms < self.spans[asset][-1][1]:
            del self.levels[asset]
            self._snapshot_anchors.pop(asset, None)
            self.clock_counts["reset:asset_venue_time_reversal"] += 1

    def _level(self, row: dict, side: str) -> tuple[tuple[float, str], float]:
        price, size = px(row["price"]), float(row["size"])
        if self.strict_clocks and (side not in ("BUY", "SELL") or not 0 <= price <= 1
                                   or not math.isfinite(size) or size < 0
                                   or isinstance(row["price"], bool) or isinstance(row["size"], bool)):
            raise ValueError("invalid CLOB price, size or side")
        return (price, side), size

    def apply(self, m: dict, tw_ns: int) -> list[LevelEvent | Print]:
        """One decoded market-channel message."""
        et = m.get("event_type")
        ts = m.get("timestamp")
        if self.strict_clocks and et in ("book", "price_change", "last_trade_price"):
            valid = type(ts) is int or (isinstance(ts, str) and ts.isascii() and ts.isdecimal())
            if not valid or ts is None or int(ts) <= 0:
                if et == "last_trade_price":
                    self.clock_counts["unclocked_prints"] += 1
                else:
                    self.reset()
                    self.clock_counts["reset:invalid_venue_time"] += 1
                return []
        ms = int(ts) if ts not in (None, "") else tw_ns // 1_000_000
        mk = str(m.get("market") or "")
        out: list[LevelEvent | Print] = []
        if et == "book":
            a = m["asset_id"]
            self._snapshot_anchors.pop(a, None)
            self._check_depth_time(a, ms)
            if self.strict_clocks and (not a or not isinstance(m.get("bids"), list) or not isinstance(m.get("asks"), list)):
                raise ValueError("incomplete book snapshot")
            new = dict(self._level(lv, side)
                       for side, key in (("BUY", "bids"), ("SELL", "asks")) for lv in m.get(key) or [])
            if self.strict_clocks and len(new) != len(m["bids"]) + len(m["asks"]):
                raise ValueError("duplicate book level")
            new = {k: v for k, v in new.items() if v > 0}
            old = self.levels.get(a)
            if old is None:
                self.spans.setdefault(a, []).append([ms, ms])
            else:
                for k in sorted(old.keys() | new.keys()):
                    o, n = old.get(k, 0.0), new.get(k, 0.0)
                    if o != n:
                        out.append(LevelEvent(ms, tw_ns, a, k[0], k[1], o, n, "book", mk))
            self.levels[a] = new
            self.market_of[a] = mk
            self._seen(a, ms)
        elif et == "price_change":
            if self.strict_clocks and not isinstance(m.get("price_changes"), list):
                raise ValueError("missing price changes")
            for ch in m.get("price_changes") or []:
                a = ch.get("asset_id")
                if self.strict_clocks and not a:
                    raise ValueError("missing changed token")
                self._snapshot_anchors.pop(a, None)
                self._check_depth_time(a, ms)
                lv = self.levels.get(a)
                if lv is None:
                    self.dropped += 1
                    continue
                k, n = self._level(ch, str(ch["side"]))
                o = lv.get(k, 0.0)
                if n > 0:
                    lv[k] = n
                else:
                    lv.pop(k, None)
                self._seen(a, ms)
                if o != n:
                    out.append(LevelEvent(ms, tw_ns, a, k[0], k[1], o, n, "price_change", mk))
        elif et == "last_trade_price" and m.get("asset_id"):
            a = m["asset_id"]
            (price, side), size = self._level(m, str(m.get("side")))
            if self.strict_clocks and size == 0:
                raise ValueError("zero print size")
            if mk:
                self.market_of.setdefault(a, mk)
            if not self.strict_clocks and a in self.levels:
                self._seen(a, ms)
            out.append(Print(ms, tw_ns, a, price, size, side,
                             str(m.get("transaction_hash") or ""), mk))
        return out

    def _patch_snapshot(self, snapshot: _CachedSnapshot, tw_ns: int) -> list[LevelEvent | Print] | None:
        msg = snapshot.message
        asset, ms = msg["asset_id"], msg["timestamp"]
        if (snapshot.delta is None or snapshot.previous is None or not asset
                or self._snapshot_anchors.get(asset) != snapshot.previous
                or type(ms) is not int or ms <= 0 or asset not in self.levels
                or (self.strict_clocks and ms < self.spans[asset][-1][1])):
            return None
        old = self.levels[asset]
        new = old.copy()
        market = str(msg.get("market") or "")
        last = None
        events: list[LevelEvent | Print] = []
        for row in snapshot.delta:
            price, size, side = row.get("price"), row.get("size"), row.get("side")
            if (not isinstance(price, (int, float)) or isinstance(price, bool)
                    or not 0 <= price <= 1 or px(price) != price
                    or not isinstance(size, (int, float)) or isinstance(size, bool)
                    or not math.isfinite(size) or size < 0
                    or side not in ("BUY", "SELL")):
                return None
            key = (price, side)
            if last is not None and key <= last:
                return None
            last = key
            before = old.get(key, 0.0)
            if before == size:
                return None
            if size > 0:
                new[key] = size
            else:
                new.pop(key, None)
            events.append(LevelEvent(ms, tw_ns, asset, price, side, before, size, "book", market))
        self.levels[asset] = new
        self.market_of[asset] = market
        self._seen(asset, ms)
        return events

    def _apply_cached_snapshot(self, snapshot: _CachedSnapshot, tw_ns: int) -> list[LevelEvent | Print]:
        events = self._patch_snapshot(snapshot, tw_ns)
        if events is None:
            bids, asks = snapshot.load_levels()
            events = self.apply({**snapshot.message, "bids": bids, "asks": asks}, tw_ns)
        asset = snapshot.message["asset_id"]
        if asset in self.levels:
            self._snapshot_anchors[asset] = snapshot.key
        return events

    def feed_messages(self, rec: dict, *, cached: bool = False) -> Iterator[tuple[dict, Sequence[Item]]]:
        """Apply one embedded message at a time, exposing its own book state before the next."""
        ctl = (rec.get("meta") or {}).get("ctl")
        if ctl is not None:
            self._snapshot_anchors.clear()
            if ctl == "subscribe_add" or (not self.strict_clocks and ctl in NO_RESET_CTL):
                return
            self.reset()
            yield {}, [Reset(int(rec["tw"]), str(ctl))]
            return
        if cached and "_book_messages" in rec:
            obj = rec["_book_messages"]
        else:
            if cached and rec.get("_book_cache_barrier"):
                self._snapshot_anchors.clear()
            raw = rec.get("raw") or ""
            if raw in ("", "PING", "PONG") or (not self.strict_clocks and raw[0] not in "[{"):
                return  # PONG
            try:
                obj = orjson.loads(raw)
            except orjson.JSONDecodeError:
                if self.strict_clocks:
                    self.reset()
                    yield {}, [Reset(int(rec["tw"]), "malformed_message")]
                return
        for obj_message in obj if isinstance(obj, list) else [obj]:
            snapshot = obj_message if cached and isinstance(obj_message, _CachedSnapshot) else None
            m = snapshot.message if snapshot is not None else obj_message
            if self.strict_clocks and (not isinstance(m, dict) or m.get("event_type") not in
                    ("book", "price_change", "last_trade_price", "tick_size_change", "best_bid_ask",
                     "new_market", "market_resolved")):
                self.reset()
                yield {}, [Reset(int(rec["tw"]), "unknown_message")]
            elif isinstance(m, dict):
                try:
                    tw_ns = int(rec["tw"])
                    items = (self.apply(m, tw_ns) if snapshot is None else
                             self._apply_cached_snapshot(snapshot, tw_ns))
                except (KeyError, ValueError, TypeError, OverflowError, AttributeError):
                    if not self.strict_clocks:
                        raise
                    if m.get("event_type") == "last_trade_price":
                        self.clock_counts["malformed_prints"] += 1
                        yield m, []
                    else:
                        self.reset()
                        yield {}, [Reset(int(rec["tw"]), "malformed_message")]
                else:
                    yield m, items

    def feed(self, rec: dict, *, cached: bool = False) -> list[Item]:
        """One tape record (data frame or ctl). Returns what it produced, in order."""
        return [item for _, items in self.feed_messages(rec, cached=cached) for item in items]

    def mates(self) -> dict[str, str]:
        """asset -> the other asset of its market (binary markets seen with both assets)."""
        by: dict[str, list[str]] = {}
        for a, mk in self.market_of.items():
            if mk:
                by.setdefault(mk, []).append(a)
        return {a: b for pair in by.values() if len(pair) == 2 for a, b in (pair, pair[::-1])}


def _records(root: Path, stream: str, t_from: int, t_to: int, *, strict: bool = False,
             book_cache: Path | None = None) -> Iterator[dict]:
    cache = None
    if book_cache is not None:
        from .book_cache import BookCache

        cache = BookCache(book_cache)
    prev_seq = prev_tw = None
    for hour in range(t_from - t_from % 3600, t_to, 3600):
        path = segment_path(Path(root), stream, hour * NS)
        if not path.exists():
            if cache is not None and cache.path(path, stream).exists():
                raise ValueError(f"book cache source hour is missing: {path}")
            prev_seq = prev_tw = None
            yield {"tw": max(hour, t_from) * NS, "meta": {"ctl": "missing_segment"}}
            continue
        source = (read_segment(path, verify=True, strict=strict) if cache is None else
                  cache.records(path, stream, hour * NS, t_from * NS, t_to * NS))
        for rec in source:
            if strict:
                tw, seq = rec["tw"], rec["seq"]
                if (rec.get("stream") != stream or type(seq) is not int or seq < 1 or type(tw) is not int
                        or not hour * NS <= tw < (hour + 3600) * NS):
                    raise ValueError(f"invalid record scope in {path}")
                if prev_seq is not None and seq != prev_seq + 1:
                    yield {"tw": tw, "meta": {"ctl": "sequence_discontinuity"}}
                if prev_tw is not None and tw < prev_tw:
                    yield {"tw": tw, "meta": {"ctl": "receive_time_reversal"}}
                prev_seq, prev_tw = seq, tw
            if t_from * NS <= rec["tw"] < t_to * NS:
                yield rec


def replay(root: Path, stream: str, t_from: int, t_to: int, book: IntlBook, *, strict: bool = False,
           book_cache: Path | None = None) -> Iterator[list[Item]]:
    """Yield events grouped by captured record in [t_from, t_to). Missing segments reset the book."""
    for rec in _records(root, stream, t_from, t_to, strict=strict, book_cache=book_cache):
        items = book.feed(rec, cached=book_cache is not None)
        if items:
            yield items


def replay_messages(root: Path, stream: str, t_from: int, t_to: int, book: IntlBook, *,
                    book_cache: Path | None = None) -> Iterator[tuple[int, list[Item | BookObservation]]]:
    """Yield each message with the ordinal of its parent record, preserving intermediate book states."""
    for record_no, rec in enumerate(_records(root, stream, t_from, t_to, book_cache=book_cache)):
        tw = int(rec["tw"])
        for msg, items in book.feed_messages(rec, cached=book_cache is not None):
            events: list[Item | BookObservation] = list(items)
            kind = msg.get("event_type")
            if book_cache is not None and kind == "book":
                # A snapshot can only touch its own asset. The batch has no further
                # asset membership work when the snapshot already emitted a level change.
                if not events:
                    ts = msg.get("timestamp")
                    ms = int(ts) if ts not in (None, "") else tw // 1_000_000
                    events.append(BookObservation(ms, tw, msg["asset_id"]))
            elif kind in ("book", "price_change"):
                touched = ({msg["asset_id"]} if kind == "book" else
                           {c["asset_id"] for c in msg.get("price_changes") or [] if c.get("asset_id")})
                changed = {e.asset for e in items if isinstance(e, LevelEvent)}
                for asset in sorted(touched - changed):
                    ts = msg.get("timestamp")
                    ms = int(ts) if ts not in (None, "") else tw // 1_000_000
                    events.append(BookObservation(ms, tw, asset))
            if events:
                yield record_no, events
