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

from collections.abc import Iterator
from dataclasses import dataclass
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


Item = LevelEvent | Print | Reset


def px(x: object) -> float:
    """Price key: 4 decimals (ticks are 0.01 or 0.001)."""
    return round(float(x), 4)  # type: ignore[arg-type]


class IntlBook:
    """Level sizes of every asset on one connection (one stream)."""

    def __init__(self) -> None:
        self.levels: dict[str, dict[tuple[float, str], float]] = {}  # known assets only
        self.market_of: dict[str, str] = {}
        self.spans: dict[str, list[list[int]]] = {}  # asset -> [[from_ms, to_ms], ...]
        self.dropped = 0  # price_change rows for assets with unknown state

    def known(self, asset: str) -> bool:
        return asset in self.levels

    def size(self, asset: str, price: float, side: str) -> float | None:
        """Current size of a level; None while the asset's state is unknown."""
        lv = self.levels.get(asset)
        return None if lv is None else lv.get((px(price), side), 0.0)

    def reset(self) -> None:
        self.levels.clear()

    def _seen(self, asset: str, ms: int) -> None:
        span = self.spans[asset][-1]
        span[1] = max(span[1], ms)

    def apply(self, m: dict, tw_ns: int) -> list[LevelEvent | Print]:
        """One decoded market-channel message."""
        et = m.get("event_type")
        ts = m.get("timestamp")
        ms = int(ts) if ts not in (None, "") else tw_ns // 1_000_000
        mk = str(m.get("market") or "")
        out: list[LevelEvent | Print] = []
        if et == "book":
            a = m["asset_id"]
            new = {(px(lv["price"]), side): float(lv["size"])
                   for side, key in (("BUY", "bids"), ("SELL", "asks")) for lv in m.get(key) or []}
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
            for ch in m.get("price_changes") or []:
                a = ch.get("asset_id")
                lv = self.levels.get(a)
                if lv is None:
                    self.dropped += 1
                    continue
                k = (px(ch["price"]), str(ch["side"]))
                o, n = lv.get(k, 0.0), float(ch["size"])
                if n > 0:
                    lv[k] = n
                else:
                    lv.pop(k, None)
                self._seen(a, ms)
                if o != n:
                    out.append(LevelEvent(ms, tw_ns, a, k[0], k[1], o, n, "price_change", mk))
        elif et == "last_trade_price" and m.get("asset_id"):
            a = m["asset_id"]
            if mk:
                self.market_of.setdefault(a, mk)
            if a in self.levels:
                self._seen(a, ms)
            out.append(Print(ms, tw_ns, a, px(m["price"]), float(m["size"]), str(m.get("side")),
                             str(m.get("transaction_hash") or ""), mk))
        return out

    def feed(self, rec: dict) -> list[Item]:
        """One tape record (data frame or ctl). Returns what it produced, in order."""
        ctl = (rec.get("meta") or {}).get("ctl")
        if ctl is not None:
            if ctl in NO_RESET_CTL:
                return []
            self.reset()
            return [Reset(int(rec["tw"]), str(ctl))]
        raw = rec.get("raw") or ""
        if not raw or raw[0] not in "[{":
            return []  # PONG
        try:
            obj = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return []
        out: list[Item] = []
        for m in obj if isinstance(obj, list) else [obj]:
            if isinstance(m, dict):
                out += self.apply(m, int(rec["tw"]))
        return out

    def mates(self) -> dict[str, str]:
        """asset -> the other asset of its market (binary markets seen with both assets)."""
        by: dict[str, list[str]] = {}
        for a, mk in self.market_of.items():
            if mk:
                by.setdefault(mk, []).append(a)
        return {a: b for pair in by.values() if len(pair) == 2 for a, b in (pair, pair[::-1])}


def replay(root: Path, stream: str, t_from: int, t_to: int, book: IntlBook) -> Iterator[list[Item]]:
    """Feed ``book`` the stream's records with ``t_from <= tw < t_to`` (unix s) in tape order and
    yield the items of each record that produced any. A missing hour segment resets the book."""
    for hour in range(t_from - t_from % 3600, t_to, 3600):
        path = segment_path(Path(root), stream, hour * NS)
        if not path.exists():
            book.reset()
            yield [Reset(max(hour, t_from) * NS, "missing_segment")]
            continue
        for rec in read_segment(path, verify=True):
            if t_from * NS <= rec["tw"] < t_to * NS:
                items = book.feed(rec)
                if items:
                    yield items
