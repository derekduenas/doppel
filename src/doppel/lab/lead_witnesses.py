"""Causal BTC top-of-book observations from the native offshore recorder."""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ..tape.segment import read_segment
from .taker_entries import MAX_AGE_NS

BINANCE = "lead_binance_spot_l2"
HYPERLIQUID = "lead_hyperliquid_l2"
MAX_BOOK_CHARS = 64 * 1024


@dataclass(frozen=True, slots=True)
class LeadQuote:
    tw_ns: int
    tm_ns: int
    seq: int
    bid: float
    bid_size: float
    ask: float
    ask_size: float
    update_id: int | None
    exchange_ts_ms: int | None


@dataclass(frozen=True, slots=True)
class LeadWitness:
    binance: LeadQuote
    hyperliquid: LeadQuote

    @property
    def tw_ns(self) -> int:
        return max(self.binance.tw_ns, self.hyperliquid.tw_ns)


def _segment_rows(paths: Iterable[Path]) -> Iterator[dict]:
    for path in paths:
        yield from read_segment(path, verify=True, strict=True)


def _positive_decimal(text: object) -> float:
    if not isinstance(text, str):
        raise ValueError("expected a native decimal string")
    number = float(text)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("expected positive finite price and size")
    return number


def _touch(rows: object, *, bid: bool, binance: bool) -> tuple[float, float]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("both book sides must be present")
    levels = {}
    for row in rows:
        if binance and isinstance(row, list) and len(row) == 2:
            price, size = row
        elif not binance and isinstance(row, dict):
            price, size = row.get("px"), row.get("sz")
        else:
            raise ValueError("unexpected native level shape")
        px, qty = _positive_decimal(price), _positive_decimal(size)
        if px in levels:
            raise ValueError("ambiguous size at duplicated price")
        levels[px] = qty
    px = max(levels) if bid else min(levels)
    return px, levels[px]


def _snapshot(stream: str, record: dict) -> LeadQuote | None:
    raw = record["raw"]
    if len(raw) > MAX_BOOK_CHARS:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("expected a native book snapshot")
        binance = stream == BINANCE
        update_id = exchange_ts_ms = None
        if binance:
            update_id = data.get("lastUpdateId")
            if type(update_id) is not int or update_id < 0:
                raise ValueError("missing Binance update id")
            bids, asks = data.get("bids"), data.get("asks")
        else:
            book = data.get("data")
            if data.get("channel") != "l2Book" or not isinstance(book, dict) or book.get("coin") != "BTC":
                raise ValueError("expected a Hyperliquid BTC book snapshot")
            exchange_ts_ms = book.get("time")
            levels = book.get("levels")
            if type(exchange_ts_ms) is not int or exchange_ts_ms < 0 or not isinstance(levels, list) or len(levels) != 2:
                raise ValueError("missing Hyperliquid book sides or timestamp")
            bids, asks = levels
        bid, bid_size = _touch(bids, bid=True, binance=binance)
        ask, ask_size = _touch(asks, bid=False, binance=binance)
        if bid >= ask:
            raise ValueError("locked or crossed book")
    except (ValueError, RecursionError):
        return None
    return LeadQuote(record["tw"], record["tm"], record["seq"], bid, bid_size, ask, ask_size, update_id, exchange_ts_ms)


class _BookCursor:
    def __init__(self, stream: str, records: Iterable[dict]) -> None:
        self.stream = stream
        self.records = iter(records)
        self.pending: dict | None = None
        self.tw_ns = self.seq = 0
        self.quote: LeadQuote | None = None

    def before(self, cutoff_ns: int) -> LeadQuote | None:
        if self.pending is None:
            self.pending = next(self.records, None)
        while self.pending is not None:
            record = self.pending
            tw = record.get("tw")
            if type(tw) is not int or tw <= 0 or tw < self.tw_ns:
                raise ValueError(f"invalid or regressing native receipt clock: {self.stream}")
            if tw >= cutoff_ns:
                break
            seq, tm, raw = record.get("seq"), record.get("tm"), record.get("raw")
            if (record.get("stream") != self.stream or type(seq) is not int or seq <= self.seq
                    or type(tm) is not int or tm < 0 or not isinstance(raw, str)):
                raise ValueError(f"invalid native recorder envelope: {self.stream}")
            if raw:
                self.quote = _snapshot(self.stream, record)
            else:
                control = record.get("meta")
                if not isinstance(control, dict) or not isinstance(control.get("ctl"), str) or not control["ctl"]:
                    raise ValueError(f"missing native recorder control: {self.stream}")
                self.quote = None
            self.tw_ns, self.seq = tw, seq
            self.pending = next(self.records, None)
        if self.quote and cutoff_ns - self.quote.tw_ns <= MAX_AGE_NS:
            return self.quote
        return None


class LeadWitnesses:
    """Sample both native streams at nondecreasing decision cutoffs."""

    def __init__(self, binance: Iterable[dict], hyperliquid: Iterable[dict]) -> None:
        self._binance = _BookCursor(BINANCE, binance)
        self._hyperliquid = _BookCursor(HYPERLIQUID, hyperliquid)
        self._cutoff_ns = 0

    @classmethod
    def from_segments(cls, binance: Iterable[Path], hyperliquid: Iterable[Path]) -> LeadWitnesses:
        """Read staged segment paths in chronological order with complete-frame and CRC checks."""
        return cls(_segment_rows(binance), _segment_rows(hyperliquid))

    def at(self, cutoff_ns: int) -> LeadWitness | None:
        if type(cutoff_ns) is not int or cutoff_ns <= 0 or cutoff_ns < self._cutoff_ns:
            raise ValueError("decision cutoffs must be positive, nondecreasing Unix nanoseconds")
        self._cutoff_ns = cutoff_ns
        binance = self._binance.before(cutoff_ns)
        hyperliquid = self._hyperliquid.before(cutoff_ns)
        if binance is None or hyperliquid is None:
            return None
        return LeadWitness(binance, hyperliquid)
