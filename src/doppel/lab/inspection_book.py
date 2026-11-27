"""Stream sealed CLOB files into the sparse evidence needed to inspect filled orders."""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import re

import orjson

from ..tape.segment import read_segment, segment_path
from .intlbook import NS, px

Key = tuple[str, float, str]
SIZE_TOLERANCE = 0.02


@dataclass(slots=True)
class Span:
    span_id: int
    start_ms: int
    end_ms: int
    start_tw: int
    end_tw: int


@dataclass(slots=True, frozen=True)
class LevelMark:
    venue_ms: int
    tw_ns: int
    seq: int
    ordinal: int
    span_id: int
    old_size: float
    new_size: float
    src: str

    @property
    def delta(self) -> float:
        return self.new_size - self.old_size


@dataclass(slots=True, frozen=True)
class TradeMark:
    venue_ms: int
    tw_ns: int
    seq: int
    ordinal: int
    span_id: int | None
    asset: str
    market: str
    price: float
    side: str
    size: float
    tx: str


@dataclass
class InspectionBook:
    events: dict[Key, list[LevelMark]] = field(default_factory=lambda: defaultdict(list))
    empty: dict[Key, list[LevelMark]] = field(default_factory=lambda: defaultdict(list))
    rises: dict[Key, list[LevelMark]] = field(default_factory=lambda: defaultdict(list))
    prints: dict[str, list[TradeMark]] = field(default_factory=lambda: defaultdict(list))
    spans: dict[str, list[Span]] = field(default_factory=lambda: defaultdict(list))
    sources: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    counts: Counter = field(default_factory=Counter)


def _amount(value) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid book size")
    n = float(value)
    if not math.isfinite(n) or n < 0:
        raise ValueError("invalid book size")
    return n


def _price(value) -> float:
    n = _amount(value)
    if n > 1 or abs(px(n) - n) > 1e-10:
        raise ValueError("invalid book price or unsupported tick size")
    return px(n)


class _Projection:
    def __init__(self, signatures: dict[Key, set[float]], wanted_txs: set[str], pairs: dict[str, str]):
        self.out = InspectionBook()
        self.wanted = {tx.lower() for tx in wanted_txs}
        self.sizes: dict[Key, list[float]] = {}
        self.by_token: dict[str, list[Key]] = defaultdict(list)
        for (asset, price, side), sizes in signatures.items():
            if not asset or side not in ("BUY", "SELL") or not sizes or any(_amount(s) <= 0 for s in sizes):
                raise ValueError("invalid order signature")
            key = (asset, _price(price), side)
            self.sizes[key] = sorted(sizes)
            self.by_token[asset].append(key)
        self.tokens = set(self.by_token) | set(pairs) | set(pairs.values())
        self.active: dict[str, Span] = {}
        self.depth: dict[Key, float] = {}
        self.last_zero: dict[Key, LevelMark] = {}
        self.next_span = 1

    def reset(self, reason: str) -> None:
        self.out.counts['reset:' + reason] += 1
        self.active.clear()
        self.depth.clear()
        self.last_zero.clear()

    def observe(self, asset: str, ms: int, tw: int, snapshot: bool) -> tuple[Span | None, bool]:
        span = self.active.get(asset)
        if span is not None and (ms < span.end_ms or tw < span.end_tw):
            del self.active[asset]
            span = None
            self.out.counts['reset:asset_clock_reversal'] += 1
        if span is None:
            if not snapshot:
                return None, False
            span = Span(self.next_span, ms, ms, tw, tw)
            self.next_span += 1
            self.active[asset] = span
            self.out.spans[asset].append(span)
            return span, True
        span.end_tw, span.end_ms = tw, ms
        return span, False

    def level(self, key: Key, new: float, span: Span, seq: int, ordinal: int, src: str, initial: bool) -> None:
        old = self.depth.get(key, 0.0)
        self.depth[key] = new
        # A first snapshot initializes inventory. Snapshot differences bound state, but a
        # depth increase can also aggregate several orders. Keep the source on each mark.
        if new == 0:
            zero = LevelMark(span.end_ms, span.end_tw, seq, ordinal, span.span_id, new if initial else old, 0, src)
            self.last_zero[key] = zero
            if initial or old > 0:
                self.out.empty[key].append(zero)
        if initial or new <= old:
            return
        mark = LevelMark(span.end_ms, span.end_tw, seq, ordinal, span.span_id, old, new, src)
        if old == 0:
            self.out.rises[key].append(mark)
            # A snapshot only tells us the new level at snapshot time. Its last explicit
            # zero may have been observed earlier. Incremental adds expose the old state.
            zero = self.last_zero[key]
            if src == 'price_change':
                zero = LevelMark(span.end_ms, span.end_tw, seq, ordinal, span.span_id, 0, 0, 'before_price_change')
            if self.out.empty[key][-1] != zero:
                self.out.empty[key].append(zero)
        sizes = self.sizes[key]
        increase = new - old
        low = bisect_left(sizes, increase - SIZE_TOLERANCE - 1e-10)
        if low < len(sizes) and abs(sizes[low] - increase) <= SIZE_TOLERANCE + 1e-10:
            self.out.events[key].append(mark)

    def message(self, msg: dict, tw: int, seq: int, ordinal: int) -> None:
        kind = msg.get('event_type')
        if kind not in ('book', 'price_change', 'last_trade_price'):
            if kind not in ('tick_size_change', 'best_bid_ask', 'new_market', 'market_resolved'):
                self.reset('unknown_message')
            return
        token = msg.get('asset_id')
        tx = str(msg.get('transaction_hash') or '').lower() if kind == 'last_trade_price' else ''
        if kind == 'last_trade_price' and tx not in self.wanted:
            return
        if kind != 'price_change' and token not in self.tokens and tx not in self.wanted:
            return
        # Receive time is not interchangeable with a missing venue timestamp.
        raw_time = msg.get('timestamp')
        ms = 0
        if type(raw_time) is int or (isinstance(raw_time, str) and raw_time.isascii() and raw_time.isdecimal()):
            ms = int(raw_time)
        if ms <= 0:
            if kind == 'last_trade_price':
                self.out.counts['unclocked_prints'] += 1
            else:
                self.reset('invalid_venue_time')
            return
        if kind == 'price_change':
            for i, row in enumerate(msg['price_changes']):
                asset = row['asset_id']
                if asset not in self.tokens:
                    continue
                span, _ = self.observe(asset, ms, tw, False)
                if span is None:
                    self.out.counts['uninitialized_level_rows'] += 1
                    continue
                side = row['side']
                if side not in ('BUY', 'SELL'):
                    raise ValueError('invalid book side')
                key = (asset, _price(row['price']), side)
                if key in self.sizes:
                    self.level(key, _amount(row['size']), span, seq, ordinal + i, kind, False)
            return
        if not isinstance(token, str) or not token:
            raise ValueError('missing book token')
        if kind == 'book':
            span, initial = self.observe(token, ms, tw, True)
            assert span is not None
            values = {}
            for name, side in (('bids', 'BUY'), ('asks', 'SELL')):
                if not isinstance(msg[name], list):
                    raise ValueError('invalid book snapshot')
                for row in msg[name]:
                    key = (token, _price(row['price']), side)
                    if key in self.sizes:
                        if key in values:
                            raise ValueError('duplicate snapshot level')
                        values[key] = _amount(row['size'])
            for key in self.by_token.get(token, ()):
                self.level(key, values.get(key, 0.0), span, seq, ordinal, kind, initial)
        elif tx in self.wanted:
            # Prints label executions. Their delivery need not be ordered against depth and
            # does not prove depth continuity. Bind to the capture epoch only. The caller
            # checks explicit snapshot/delta coverage independently on both clocks.
            span = self.active.get(token)
            side = msg['side']
            if side not in ('BUY', 'SELL'):
                raise ValueError('invalid print side')
            self.out.prints[tx].append(TradeMark(ms, tw, seq, ordinal, span.span_id if span else None,
                                                 token, str(msg.get('market') or ''), _price(msg['price']),
                                                 side, _amount(msg['size']), tx))

    def record(self, rec: dict) -> None:
        ctl = (rec.get('meta') or {}).get('ctl')
        if ctl is not None:
            if ctl != 'subscribe_add':
                self.reset(str(ctl))  # unsubscribe records do not identify the removed tokens
            return
        raw = rec['raw']
        if raw in ('PONG', 'PING', ''):
            return
        payload = orjson.loads(raw)
        ordinal = 0
        for msg in payload if isinstance(payload, list) else [payload]:
            if not isinstance(msg, dict):
                raise ValueError('invalid market feed message')
            self.message(msg, rec['tw'], rec['seq'], ordinal)
            # Every embedded price change can identify a different level in the same record.
            ordinal += max(1, len(msg.get('price_changes') or []))


def read_book(root: Path, stream: str, start: int, end: int, signatures: dict[Key, set[float]],
              wanted_txs: set[str], token_pairs: dict[str, str]) -> InspectionBook:
    """Read [start,end) by local receipt time. Positive marks and empties carry their span IDs.

    All present hourly files must match a published seal and decode strictly through EOF,
    including file portions outside the requested interval. Missing hours split coverage.
    Only a new full snapshot can start a token span. Price-change sizes are absolute levels.
    """
    if not re.fullmatch(r'clob_[a-z0-9]+_(?:5|15)m', stream) or end <= start:
        raise ValueError('invalid CLOB stream or interval')
    root = Path(root)
    base = root / stream
    manifest = base / 'manifest.jsonl'
    seals = {r['file']: r for line in manifest.read_bytes().splitlines() if line and (r := orjson.loads(line))}
    state = _Projection(signatures, wanted_txs, token_pairs)
    prev_seq = prev_tw = None
    for hour in range(start - start % 3600, end, 3600):
        path = segment_path(root, stream, hour * NS)
        name = path.relative_to(base).as_posix()
        if not path.is_file():
            state.out.missing.append(name)
            state.reset('missing_segment')
            prev_seq = prev_tw = None
            continue
        before = path.stat()
        seal = seals.get(name)
        with path.open('rb') as fh:
            if not seal or before.st_size != seal['bytes'] or hashlib.file_digest(fh, 'sha256').hexdigest() != seal['sha256']:
                raise ValueError(f'unsealed or changed book segment {stream}/{name}')
        first = last = None
        n = selected = 0
        for rec in read_segment(path, verify=True, strict=True):
            tw, seq = rec['tw'], rec['seq']
            if (rec.get('stream') != stream or type(seq) is not int or seq < 1 or type(tw) is not int
                    or not hour * NS <= tw < (hour + 3600) * NS):
                raise ValueError(f'invalid record scope in {stream}/{name}')
            if prev_seq is not None and seq != prev_seq + 1:
                state.reset('sequence_discontinuity')
            if prev_tw is not None and tw < prev_tw:
                state.reset('receive_time_reversal')
            prev_seq, prev_tw = seq, tw
            first = tw if first is None else first
            last = tw
            n += 1
            if start * NS <= tw < end * NS:
                selected += 1
                state.record(rec)
        stat = path.stat()
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if any(getattr(before, f) != getattr(stat, f) for f in fields):
            raise ValueError(f'book segment changed while reading {stream}/{name}')
        state.out.sources.append({'file': f'{stream}/{name}', 'sha256': seal['sha256'], 'bytes': before.st_size,
                                  'records': n, 'selected_records': selected, 'first_tw': first, 'last_tw': last})
    return state.out
