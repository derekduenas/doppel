"""Kalshi market-data WebSocket tape (`kalshi_ws`, Rust recorder) -> per-market YES books and prints.

The Rust recorder (crates/doppel-tape/src/kalshi.rs) subscribes ONE connection to the channels
``orderbook_delta`` and ``trade`` for the nearest open crypto 15m markets, without
``use_yes_price``, and records every frame verbatim with our receive time ``tw`` (ns). Control
records carry ``meta.ctl``. Frame shapes (Kalshi AsyncAPI, fixed-point fields):

- ``{"type": "orderbook_snapshot", "sid", "seq", "msg": {"market_ticker", "yes_dollars_fp":
  [["0.0800", "300.00"], ...], "no_dollars_fp": [...]}}``. Levels are BIDS on each side; a missing
  array is an empty side; an empty snapshot is a valid (empty) book. A snapshot replaces the book.
- ``{"type": "orderbook_delta", "sid", "seq", "msg": {"market_ticker", "price_dollars": "0.9600",
  "delta_fp": "-54.00", "side": "yes"|"no"}}``: signed change at one bid level.
- ``{"type": "trade", "sid", "seq", "msg": {"market_ticker", "yes_price_dollars", "count_fp",
  "taker_side": "yes"|"no"}}``. ``taker_side == "no"`` means the taker bought NO (sold YES).
Integer-cent legacy fields (``price``, ``delta``, ``yes``, ``yes_price``, ``count``) are read as a
fallback only.

YES best bid = max YES bid; YES best ask = 1 - max NO bid.

Validity (only recorded frames and their ``tw``; nothing from the future):
- ``seq`` is per subscription id (sid) and one sid carries every subscribed market. A gap on a sid
  (seq != previous + 1) is recorded and makes EVERY book bound to that sid invalid until that
  market's next snapshot. Frames without ``seq`` are not checked (as in the Rust recorder).
- A delta applies only to a book whose snapshot came on the same sid; a delta for a market without
  a valid book is ignored (counted). A level driven below zero invalidates that book.
- Control records ``connect``/``disconnect``/``reconnect``/``stall``/``feedback_overflow``/
  ``backpressure_drop``/``subscribe``/``resubscribe``/``seq_gap`` invalidate every book and reset
  the sid/seq state (sids restart on a new connection; frames may be missing from the tape).
  ``unsubscribe`` invalidates the listed tickers. An ``error`` frame carrying a sid invalidates
  the books on that sid.
- Frames whose body cannot be parsed are counted (``unparsed``) and, for book frames, invalidate
  that market, so an empty result can never pass silently.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import orjson

from ..tape.segment import read_segment

STREAM = "kalshi_ws"
LOOKBACK_S = 3600          # read this much tape before the requested start (snapshots come at subscribe)
PX = 10_000                # prices are kept as integers in units of $0.0001
RESET_CTL = ("connect", "disconnect", "reconnect", "stall", "feedback_overflow", "backpressure_drop",
             "subscribe", "resubscribe", "seq_gap")

Top = tuple[float | None, float, float | None, float]   # yes bid, qty, yes ask, qty (None = side empty)


@dataclass(frozen=True, slots=True)
class Print:
    tw: int
    ticker: str
    yes_px: float
    qty: float
    taker_side: str          # "yes" (bought YES: lifted the YES ask) | "no" (sold YES: hit the YES bid)


def _px(dollars, cents) -> int | None:
    try:
        if dollars is not None:
            return round(float(dollars) * PX)
        if cents is not None:
            return round(float(cents) * PX / 100)
    except (TypeError, ValueError):
        return None
    return None


def _qty(fp, legacy) -> float | None:
    try:
        x = fp if fp is not None else legacy
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _levels(msg: dict, side: str) -> dict[int, float] | None:
    """Bid levels of one side of a snapshot; {} when absent, None when malformed."""
    if f"{side}_dollars_fp" in msg:
        rows, cents = msg[f"{side}_dollars_fp"], False
    elif f"{side}_dollars" in msg:
        rows, cents = msg[f"{side}_dollars"], False
    elif side in msg:
        rows, cents = msg[side], True
    else:
        return {}
    out: dict[int, float] = {}
    for r in rows or ():
        if not isinstance(r, (list, tuple)) or len(r) < 2:
            return None
        p = _px(None, r[0]) if cents else _px(r[0], None)
        q = _qty(r[1], None)
        if p is None or q is None:
            return None
        if q > 0:
            out[p] = out.get(p, 0.0) + q
    return out


@dataclass
class _Mkt:
    sid: int | None
    yes: dict[int, float]
    no: dict[int, float]
    valid: bool = True


@dataclass
class KalshiWsBooks:
    """Replays ``kalshi_ws`` records in tape order. ``feed`` returns the events for wanted markets:
    ("book", ticker, tw, Top | None) when the top of book changes or the book becomes invalid
    (None), and ("trade", ticker, tw, Print)."""
    want: Callable[[str], bool] = lambda t: True
    mkts: dict[str, _Mkt] = field(default_factory=dict)
    last_seq: dict[int, int] = field(default_factory=dict)
    tops: dict[str, Top | None] = field(default_factory=dict)
    gaps: list[dict] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=lambda: {
        "frames": 0, "snapshots": 0, "deltas": 0, "trades": 0, "seq_gaps": 0, "resets": 0,
        "unparsed": 0, "delta_without_book": 0, "delta_sid_mismatch": 0, "negative_level": 0})

    def _emit(self, out: list, t: str, tw: int) -> None:
        if not self.want(t):
            return
        m = self.mkts.get(t)
        top: Top | None = None
        if m is not None and m.valid:
            yb = max(m.yes) if m.yes else None
            nb = max(m.no) if m.no else None
            top = (None if yb is None else yb / PX, m.yes[yb] if yb is not None else 0.0,
                   None if nb is None else (PX - nb) / PX, m.no[nb] if nb is not None else 0.0)
        if t not in self.tops or self.tops[t] != top:
            self.tops[t] = top
            out.append(("book", t, tw, top))

    def _invalidate(self, out: list, tw: int, pred: Callable[[str, _Mkt], bool]) -> None:
        for t, m in self.mkts.items():
            if m.valid and pred(t, m):
                m.valid = False
                self._emit(out, t, tw)

    def feed(self, rec: dict) -> list:
        out: list = []
        tw = rec["tw"]
        ctl = (rec.get("meta") or {}).get("ctl")
        if ctl:
            if ctl in RESET_CTL:
                self.stats["resets"] += 1
                self.last_seq.clear()
                self._invalidate(out, tw, lambda t, m: True)
            elif ctl == "unsubscribe":
                gone = set((rec.get("meta") or {}).get("tickers") or ())
                self._invalidate(out, tw, lambda t, m: t in gone)
            return out
        raw = rec.get("raw")
        if not raw:
            return out
        try:
            d = orjson.loads(raw)
        except orjson.JSONDecodeError:
            self.stats["unparsed"] += 1
            return out
        if not isinstance(d, dict):
            return out
        typ = d.get("type")
        sid, seq = d.get("sid"), d.get("seq")
        if typ == "error":
            if isinstance(sid, int):
                self._invalidate(out, tw, lambda t, m: m.sid == sid)
            return out
        if typ not in ("orderbook_snapshot", "orderbook_delta", "trade"):
            return out
        self.stats["frames"] += 1
        if isinstance(sid, int) and isinstance(seq, int):
            prev = self.last_seq.get(sid)
            self.last_seq[sid] = seq
            if prev is not None and seq != prev + 1:
                self.stats["seq_gaps"] += 1
                self.gaps.append({"tw": tw, "sid": sid, "expected": prev + 1, "got": seq, "type": typ})
                self._invalidate(out, tw, lambda t, m: m.sid == sid)
        msg = d.get("msg")
        t = msg.get("market_ticker") if isinstance(msg, dict) else None
        if not isinstance(msg, dict) or not isinstance(t, str):
            self.stats["unparsed"] += 1
            return out
        if typ == "orderbook_snapshot":
            self.stats["snapshots"] += 1
            yes, no = _levels(msg, "yes"), _levels(msg, "no")
            if yes is None or no is None:
                self.stats["unparsed"] += 1
                self._invalidate(out, tw, lambda k, m: k == t)
                return out
            self.mkts[t] = _Mkt(sid if isinstance(sid, int) else None, yes, no)
            self._emit(out, t, tw)
        elif typ == "orderbook_delta":
            self.stats["deltas"] += 1
            m = self.mkts.get(t)
            if m is None or not m.valid:
                self.stats["delta_without_book"] += 1
                return out
            if m.sid != (sid if isinstance(sid, int) else None):
                self.stats["delta_sid_mismatch"] += 1
                return out
            p = _px(msg.get("price_dollars"), msg.get("price"))
            q = _qty(msg.get("delta_fp"), msg.get("delta"))
            side = msg.get("side")
            if p is None or q is None or side not in ("yes", "no"):
                self.stats["unparsed"] += 1
                m.valid = False
                self._emit(out, t, tw)
                return out
            book = m.yes if side == "yes" else m.no
            n = book.get(p, 0.0) + q
            if n < -1e-9:
                self.stats["negative_level"] += 1
                m.valid = False
            elif n <= 1e-9:
                book.pop(p, None)
            else:
                book[p] = n
            self._emit(out, t, tw)
        else:
            self.stats["trades"] += 1
            p = _px(msg.get("yes_price_dollars"), msg.get("yes_price"))
            q = _qty(msg.get("count_fp"), msg.get("count"))
            side = msg.get("taker_side")
            if p is None or q is None or q <= 0 or side not in ("yes", "no"):
                self.stats["unparsed"] += 1
                return out
            if self.want(t):
                out.append(("trade", t, tw, Print(tw, t, p / PX, q, side)))
        return out


def segments(root: Path, stream: str, t0: float, t1: float) -> list[Path]:
    """Hour segments of ``stream`` overlapping [t0, t1], in time order."""
    hours = set()
    h = int(t0 // 3600 * 3600)
    while h <= t1:
        hours.add(time.strftime("%Y%m%d/%H", time.gmtime(h)))
        h += 3600
    return [p for p in sorted((Path(root) / stream).glob("*/*.dtape.zst"))
            if f"{p.parent.name}/{p.name[:2]}" in hours]


def replay(root: Path, t0: float, t1: float, want: Callable[[str], bool] = lambda t: True,
           lookback: float = LOOKBACK_S, stream: str = STREAM) -> tuple[Iterator[tuple], KalshiWsBooks]:
    """(events, replayer) for records with t0 - lookback <= tw/1e9 <= t1, fed in tape order.
    Events are those of :meth:`KalshiWsBooks.feed`; ``replayer.stats``/``gaps`` fill as it runs."""
    kb = KalshiWsBooks(want=want)
    lo, hi = t0 - lookback, t1

    def gen() -> Iterator[tuple]:
        for f in segments(root, stream, lo, hi):
            for rec in read_segment(f, verify=True):
                if not lo <= rec["tw"] / 1e9 <= hi:
                    continue
                yield from kb.feed(rec)
    return gen(), kb
