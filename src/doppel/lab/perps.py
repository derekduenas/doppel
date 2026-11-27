"""BTC perpetual-futures tapes (Rust recorder ``perp_*`` streams) -> receive-time series.

Streams (crates/doppel-tape/src/perps.rs; decoders mirrored in crates/doppel-fair/src/perp.rs):

- ``perp_kalshi_btc``: Kalshi margin WS, KXBTCPERP. ``orderbook_snapshot`` ``{sid, seq, msg: {bid:
  [[px, count]], ask}}`` replaces the book; ``orderbook_delta`` ``{msg: {price, delta, side:
  bid|ask}}`` is a signed level change; ``seq`` is per sid. A gap on a delta resets the book until
  the next snapshot; a gap on the trade sid is counted. ``trade`` ``{trade_id, price, count,
  taker_side}``: ``bid`` = the taker bought, read from the docs ("Side of the taker"); NOT yet
  verified on tape (a 1 Hz REST book cannot tell, 2026-11-27), so Kalshi flow is provisional until
  checked against the WS book. ``ticker`` carries ``reference_price`` (CF Benchmarks
  index), ``settlement_mark_price`` and ``funding_rate.rate``.
- ``perp_kalshi_btc_rest``: the public margin REST, meta ``key`` = ``orderbook`` (replaces the book;
  a failed poll makes it unknown), ``trades`` (newest first, deduplicated by ``trade_id``; the first
  poll after a reset is the baseline, not flow), ``market`` (reference + mark), ``funding``.
- ``perp_cbde_btc``: Coinbase Advanced Trade ``level2`` + ``market_trades`` + ``heartbeats`` for
  BIP-20DEC30-CDE. ``sequence_num`` counts every frame on the connection; a jump resets the book
  until the next snapshot. ``market_trades`` ``side`` is the MAKER's side, so ``SELL`` = taker
  bought; the first ``snapshot`` event lists past trades and is skipped. The same L2 decoder reads
  ``spot_l2_coinbase`` (BTC-USD spot).
- ``perp_cbde_btc_rest``: public product, ``future_product_details.index_price`` / ``funding_rate``.

Units: price in USD per BTC, size in BTC (Kalshi contract 0.0001 BTC quoted in dollars per
contract; CBDE contract 0.01 BTC quoted per BTC). Every output is stamped with the record's
receive time ``tw`` (ns). Control records other than ``subscribe`` make the book unknown.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np
import orjson

from ..tape.segment import read_segment
from .kalshi_ws import segments

KALSHI_CONTRACT = 0.0001
CBDE_CONTRACT = 0.01
NS = 1_000_000_000
PX = 10_000
EPS = 1e-9
LOOKBACK_S = 3600
SEEN_CAP = 50_000
KALSHI_STREAMS = ("perp_kalshi_btc", "perp_kalshi_btc_rest")
COINBASE_STREAMS = {"perp_cbde_btc": CBDE_CONTRACT, "spot_l2_coinbase": 1.0}


@dataclass(frozen=True, slots=True)
class Top:
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def micro(self) -> float:
        """Microprice: the touch weighted toward the thinner side."""
        q = self.bid_qty + self.ask_qty
        return self.mid if q <= 0 else (self.bid * self.ask_qty + self.ask * self.bid_qty) / q

    @property
    def imbalance(self) -> float:
        q = self.bid_qty + self.ask_qty
        return 0.0 if q <= 0 else (self.bid_qty - self.ask_qty) / q


@dataclass(frozen=True, slots=True)
class Trade:
    tw: int
    id: str
    price: float
    qty: float                 # BTC
    taker_buy: bool | None


@dataclass(frozen=True, slots=True)
class Info:
    tw: int
    index: float | None = None
    mark: float | None = None
    funding: float | None = None


@dataclass
class Feed:
    """One stream decoded: book tops as of receive time (None = unknown), prints, info."""
    book: list[tuple[int, Top | None]] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    info: list[Info] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


class _Seen:
    def __init__(self) -> None:
        self.s: set[str] = set()
        self.order: list[str] = []

    def add(self, k: str) -> bool:
        if k in self.s:
            return False
        self.s.add(k)
        self.order.append(k)
        if len(self.order) > 2 * SEEN_CAP:
            for o in self.order[:SEEN_CAP]:
                self.s.discard(o)
            self.order = self.order[SEEN_CAP:]
        return True


class _Book:
    """Price -> size per side with a lazily maintained touch."""

    def __init__(self) -> None:
        self.bids: dict = {}
        self.asks: dict = {}
        self.bb = None
        self.ba = None
        self.valid = False

    def reset(self) -> None:
        self.bids, self.asks, self.bb, self.ba, self.valid = {}, {}, None, None, False

    def load(self, bids: dict, asks: dict) -> None:
        self.bids, self.asks = bids, asks
        self.bb = max(bids) if bids else None
        self.ba = min(asks) if asks else None
        self.valid = True

    def put(self, bid: bool, p, q: float) -> None:
        side = self.bids if bid else self.asks
        if q <= EPS:
            side.pop(p, None)
            if bid and p == self.bb:
                self.bb = max(side) if side else None
            elif not bid and p == self.ba:
                self.ba = min(side) if side else None
            return
        side[p] = q
        if bid and (self.bb is None or p > self.bb):
            self.bb = p
        elif not bid and (self.ba is None or p < self.ba):
            self.ba = p

    def top(self, px_scale: float, qty_scale: float) -> Top | None:
        if not self.valid or self.bb is None or self.ba is None:
            return None
        return Top(self.bb * px_scale, self.bids[self.bb] * qty_scale, self.ba * px_scale, self.asks[self.ba] * qty_scale)


def _ctl(rec: dict) -> str | None:
    return (rec.get("meta") or {}).get("ctl")


class KalshiPerp:
    """``perp_kalshi_btc`` / ``perp_kalshi_btc_rest`` decoder (see module doc)."""

    def __init__(self) -> None:
        self.cs = KALSHI_CONTRACT
        self.b = _Book()
        self.book_sid: int | None = None
        self.seq: dict[int, int] = {}
        self.seen = _Seen()
        self.primed = False
        self.stats = {"seq_gaps": 0, "trade_gaps": 0, "trade_overflows": 0, "negative_level": 0, "unparsed": 0}

    def _reset(self) -> None:
        self.b.reset()
        self.book_sid = None
        self.seq.clear()
        self.primed = False

    def top(self) -> Top | None:
        return self.b.top(1 / (PX * self.cs), self.cs)

    @staticmethod
    def _levels(xs) -> dict | None:
        out = {}
        for lv in xs or []:
            try:
                p, q = float(lv[0]), float(lv[1])
            except (TypeError, ValueError, IndexError):
                return None
            if q > EPS:
                out[round(p * PX)] = q
        return out

    def _trade(self, t: dict, tw: int) -> Trade | None:
        tid = t.get("trade_id")
        if not isinstance(tid, str) or not self.seen.add(tid):
            return None
        p, q = _f(t.get("price")), _f(t.get("count"))
        if p is None or q is None:
            return None
        side = {"bid": True, "ask": False}.get(str(t.get("taker_side")))
        return Trade(tw, tid, p / self.cs, q * self.cs, side)

    def feed(self, rec: dict) -> tuple[list[Trade], list[Info]]:
        trades: list[Trade] = []
        infos: list[Info] = []
        c = _ctl(rec)
        if c is not None:
            if c != "subscribe":
                self._reset()
            return trades, infos
        tw, meta = rec["tw"], rec.get("meta") or {}
        try:
            m = orjson.loads(rec["raw"])
        except orjson.JSONDecodeError:
            self.stats["unparsed"] += 1
            if meta.get("key") == "orderbook":
                self.b.reset()
            return trades, infos
        key = meta.get("key")
        if key is not None:
            ok = meta.get("status") == 200
            if key == "orderbook":
                ob = m.get("orderbook") if ok and isinstance(m, dict) else None
                b = self._levels(ob.get("bids")) if isinstance(ob, dict) else None
                a = self._levels(ob.get("asks")) if isinstance(ob, dict) else None
                if b is None or a is None:
                    self.b.reset()
                else:
                    self.b.load(b, a)
            elif key == "trades" and ok:
                primed, self.primed = self.primed, True
                lst = m.get("trades") or []
                fresh = [x for t in reversed(lst) if (x := self._trade(t, tw)) is not None]
                if primed:
                    if lst and len(fresh) == len(lst):
                        self.stats["trade_overflows"] += 1
                    trades.extend(fresh)
            elif key == "market" and ok:
                mk = m.get("market") or {}
                cs = _f(mk.get("contract_size"))
                if cs and cs > 0:
                    self.cs = cs
                infos.append(Info(tw, index=self._px(mk.get("reference_price")), mark=self._px(mk.get("settlement_mark_price"))))
            elif key == "funding" and ok:
                mp = _f(m.get("mark_price"))
                infos.append(Info(tw, mark=None if mp is None else mp / self.cs, funding=_f(m.get("funding_rate"))))
            return trades, infos
        if not isinstance(m, dict):
            return trades, infos
        typ, msg, sid, s = m.get("type"), m.get("msg") or {}, m.get("sid"), m.get("seq")
        if isinstance(sid, int) and isinstance(s, int):
            prev = self.seq.get(sid)
            if typ != "orderbook_snapshot" and prev is not None and s != prev + 1:
                if typ == "orderbook_delta":
                    self.stats["seq_gaps"] += 1
                    self._reset()
                    self.seq[sid] = s
                    return trades, infos
                self.stats["trade_gaps"] += 1
            self.seq[sid] = s
        if typ == "orderbook_snapshot":
            b, a = self._levels(msg.get("bid")), self._levels(msg.get("ask"))
            if b is None or a is None:
                self._reset()
            else:
                self.b.load(b, a)
                self.book_sid = sid
        elif typ == "orderbook_delta":
            if not self.b.valid or sid != self.book_sid:
                return trades, infos
            p, d, side = _f(msg.get("price")), _f(msg.get("delta")), msg.get("side")
            if p is None or d is None or side not in ("bid", "ask"):
                self._reset()
                return trades, infos
            k = round(p * PX)
            book = self.b.bids if side == "bid" else self.b.asks
            q = book.get(k, 0.0) + d
            if q < -EPS:
                self.stats["negative_level"] += 1
                self._reset()
                return trades, infos
            self.b.put(side == "bid", k, q)
        elif typ == "trade":
            if (t := self._trade(msg, tw)) is not None:
                trades.append(t)
        elif typ == "ticker":
            fr = msg.get("funding_rate")
            infos.append(Info(tw, index=self._px(msg.get("reference_price")), mark=self._px(msg.get("settlement_mark_price")),
                              funding=_f(fr.get("rate")) if isinstance(fr, dict) else None))
        return trades, infos

    def _px(self, o) -> float | None:
        p = _f(o.get("price")) if isinstance(o, dict) else None
        return None if p is None else p / self.cs


class CoinbaseL2:
    """Advanced Trade ``level2`` + ``market_trades`` (``perp_cbde_btc``, ``spot_l2_coinbase``)."""

    def __init__(self, contract: float) -> None:
        self.cs = contract
        self.b = _Book()
        self.seq: int | None = None
        self.seen = _Seen()
        self.stats = {"seq_gaps": 0, "unparsed": 0}

    def top(self) -> Top | None:
        return self.b.top(1.0, self.cs)

    def feed(self, rec: dict) -> tuple[list[Trade], list[Info]]:
        trades: list[Trade] = []
        c = _ctl(rec)
        if c is not None:
            if c != "subscribe":
                self.b.reset()
                self.seq = None
            return trades, []
        try:
            m = orjson.loads(rec["raw"])
        except orjson.JSONDecodeError:
            self.stats["unparsed"] += 1
            return trades, []
        if not isinstance(m, dict):
            return trades, []
        s = m.get("sequence_num")
        if isinstance(s, int):
            if self.seq is not None and s != self.seq + 1:
                self.stats["seq_gaps"] += 1
                self.b.reset()
            self.seq = s
        ch = m.get("channel")
        if ch == "l2_data":
            for ev in m.get("events") or []:
                snap = ev.get("type") == "snapshot"
                if snap:
                    self.b.reset()
                    self.b.valid = True
                elif not self.b.valid:
                    continue
                for u in ev.get("updates") or []:
                    p, q = _f(u.get("price_level")), _f(u.get("new_quantity"))
                    if p is not None and q is not None:
                        self.b.put(u.get("side") == "bid", p, q)
                if snap and (not self.b.bids or not self.b.asks):
                    self.b.reset()
        elif ch == "market_trades":
            for ev in m.get("events") or []:
                snap = ev.get("type") == "snapshot"
                for t in ev.get("trades") or []:
                    tid = t.get("trade_id")
                    if not isinstance(tid, str) or not self.seen.add(tid) or snap:
                        continue
                    p, q = _f(t.get("price")), _f(t.get("size"))
                    if p is None or q is None:
                        continue
                    side = {"SELL": True, "BUY": False}.get(t.get("side"))   # maker side
                    trades.append(Trade(rec["tw"], tid, p, q * self.cs, side))
        return trades, []


def cbde_product_info(rec: dict) -> Info | None:
    """``perp_cbde_btc_rest`` product body -> index price and funding rate."""
    meta = rec.get("meta") or {}
    if meta.get("key") != "product" or meta.get("status") != 200:
        return None
    try:
        f = orjson.loads(rec["raw"]).get("future_product_details") or {}
    except (orjson.JSONDecodeError, AttributeError):
        return None
    return Info(rec["tw"], index=_f(f.get("index_price")), funding=_f(f.get("funding_rate")))


def records(root: Path, stream: str, t0: float, t1: float) -> Iterator[dict]:
    """Records of ``stream`` with t0 <= tw/1e9 <= t1, in tape order."""
    for f in segments(root, stream, t0, t1):
        for rec in read_segment(f, verify=True):
            if t0 <= rec["tw"] / NS <= t1:
                yield rec


def decode(recs, stream: str) -> Feed:
    """Decode records of one stream. Book entries are emitted only when the top changes."""
    out = Feed()
    if stream == "perp_cbde_btc_rest":
        out.info = [i for r in recs if (i := cbde_product_info(r)) is not None]
        return out
    dec: KalshiPerp | CoinbaseL2
    if stream in KALSHI_STREAMS:
        dec = KalshiPerp()
    elif stream in COINBASE_STREAMS:
        dec = CoinbaseL2(COINBASE_STREAMS[stream])
    else:
        raise ValueError(f"no decoder for {stream}")
    last: object = ()
    for r in recs:
        tr, inf = dec.feed(r)
        out.trades.extend(tr)
        out.info.extend(inf)
        top = dec.top()
        if top != last:
            out.book.append((r["tw"], top))
            last = top
    out.stats = dict(dec.stats)
    return out


def load(root: Path, stream: str, t0: float, t1: float, lookback: float = LOOKBACK_S) -> Feed:
    """Decode ``stream`` over [t0, t1], reading ``lookback`` seconds earlier for the snapshot.
    Output before t0 is dropped except the book state in force at t0."""
    f = decode(records(root, stream, t0 - lookback, t1), stream)
    lo = int(t0 * NS)
    i = next((k for k, (tw, _) in enumerate(f.book) if tw > lo), len(f.book))
    f.book = f.book[max(i - 1, 0):]
    f.trades = [t for t in f.trades if t.tw >= lo]
    f.info = [x for x in f.info if x.tw >= lo]
    return f


def asof(t: np.ndarray, v: np.ndarray, grid: np.ndarray) -> np.ndarray:
    """Value of the last event with time <= each grid point (NaN before the first)."""
    i = np.searchsorted(t, grid, side="right") - 1
    return np.where(i >= 0, v[np.clip(i, 0, None)], np.nan) if len(t) else np.full(len(grid), np.nan)


def per_second(f: Feed, t0: int, t1: int) -> dict[str, np.ndarray]:
    """One row per second d in [t0, t1): book state as of d (receive time <= d), signed taker flow
    (BTC, + = taker bought) over (d - 1, d], and the latest index/mark/funding received by d."""
    sec = np.arange(t0, t1, dtype=np.int64)
    g = sec * NS
    bt = np.asarray([b[0] for b in f.book], dtype=np.int64)

    def col(fn) -> np.ndarray:
        return np.asarray([np.nan if b[1] is None else fn(b[1]) for b in f.book], dtype=float)

    out: dict[str, np.ndarray] = {"sec": sec}
    for name, fn in (("mid", lambda x: x.mid), ("micro", lambda x: x.micro), ("imbalance", lambda x: x.imbalance),
                     ("spread", lambda x: x.ask - x.bid)):
        out[name] = asof(bt, col(fn), g)
    flow = np.zeros(len(sec))
    unk = np.zeros(len(sec))
    for t in f.trades:
        k = int(-(-t.tw // NS)) - t0          # trade in (d-1, d] counts at second d
        if 0 <= k < len(sec):
            if t.taker_buy is None:
                unk[k] += t.qty
            else:
                flow[k] += t.qty if t.taker_buy else -t.qty
    out["flow"], out["flow_unknown"] = flow, unk
    for name in ("index", "mark", "funding"):
        xs = [(i.tw, getattr(i, name)) for i in f.info if getattr(i, name) is not None]
        out[name] = asof(np.asarray([x[0] for x in xs], dtype=np.int64), np.asarray([x[1] for x in xs], dtype=float), g)
    return out
