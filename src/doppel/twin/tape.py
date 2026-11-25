"""PM US tape -> per-window event lists for the twin.

Sources (all timestamps are OUR receive time ``tw`` in ns; venue times are kept only as a check):
- WebSocket streams ``pmus_ws_5m`` / ``pmus_ws_15m`` (earlier data: ``pmus_ws``). ``marketData``
  messages are full book snapshots; ``trade`` messages carry price, quantity, ``tradeTime`` and the
  taker (aggressor) side. Control records (``meta.ctl``) mark gaps: ``connect`` / ``disconnect`` /
  ``stall`` invalidate every book of that stream; ``unsubscribe`` / ``no_data`` / ``sub_error``
  invalidate the books of the window group named by the request id ``{kind}-{h}-{start}-{n}``.
- REST ``pmus_books`` is STALE and never feeds the twin by default: the lead measured (2026-11-25)
  that 88% of REST polls return a book matching an OLDER WS book (median 8 s, p90 20 s old; CDN
  cache). A window without WS book coverage is excluded as ``no_ws_coverage``. Only with the
  explicit diagnostics switch ``allow_stale_rest`` are REST snapshots read (``source == "rest"``);
  the engine then simulates such windows for inspection but marks them ineligible, so they never
  reach a verdict.

A window uses WS when a WS stream has at least one book snapshot for its slug. Trades are the union
of all WS streams, deduplicated by trade id.

Memory is bounded: the reader holds one hour of the relevant slugs at a time (plus the last
``PRE_S`` seconds of the previous hour and the first ``POST_S`` seconds of the next).
"""

from __future__ import annotations

import calendar
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import orjson

from ..tape.segment import _frames
from ..tape.universe import HORIZONS, pmus_slug

NS = 1_000_000_000
WS_STREAMS = ("pmus_ws_5m", "pmus_ws_15m", "pmus_ws")
WS_FOR = {"5m": ("pmus_ws_5m", "pmus_ws"), "15m": ("pmus_ws_15m", "pmus_ws")}
REST_STREAM = "pmus_books"
PRE_S = 60       # books received up to this long before the window start are kept (as-of state)
POST_S = 5       # trades received up to this long after the close (executed before it) are kept
STREAM_GAP_CTL = ("connect", "disconnect", "stall")
GROUP_GAP_CTL = ("unsubscribe", "no_data", "sub_error")
REST_GAP_CTL = ("poll_error", "rate_limited")
OPEN_STATES = ("MARKET_STATE_OPEN", None)


# ---------------------------------------------------------------------------- events
@dataclass(frozen=True, slots=True)
class Book:
    """One full YES-book snapshot. Prices are dollars (0-1), quantities contracts. Best first."""
    tw: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]
    shares_traded: float | None = None
    open: bool = True

    @property
    def best_bid(self) -> float | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2.0


@dataclass(frozen=True, slots=True)
class Trade:
    tw: int                 # receive ns
    t_exch: int | None      # venue tradeTime ns (only used to make fills harder, never easier)
    price: float
    qty: float
    aggressor: str          # "buy" (lifted the ask) | "sell" (hit the bid)
    id: str | None = None

    @property
    def t_venue(self) -> int:
        """Earliest plausible execution time: min(tradeTime, receive)."""
        return self.tw if self.t_exch is None else min(self.tw, self.t_exch)


@dataclass
class WindowTape:
    slug: str
    asset: str
    horizon: str
    start: int
    end: int
    source: str = "none"            # "ws" | "none" ("rest" only with allow_stale_rest diagnostics)
    stream: str | None = None
    books: list[Book] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    gaps: list[int] = field(default_factory=list)    # receive ns at which the book became unknown
    stats: dict = field(default_factory=dict)

    @property
    def has_trade_feed(self) -> bool:
        return self.source == "ws"


# ---------------------------------------------------------------------------- parsing
def iso_ns(s: str | None) -> int | None:
    """'2026-11-25T22:22:35.324593105Z' -> unix ns (UTC). None if absent or unparseable."""
    if not s or not isinstance(s, str):
        return None
    try:
        s = s.rstrip("Z")
        if "+" in s[10:]:
            s = s[:10] + s[10:].split("+")[0]
        base, _, frac = s.partition(".")
        sec = calendar.timegm((int(base[0:4]), int(base[5:7]), int(base[8:10]),
                               int(base[11:13]), int(base[14:16]), int(base[17:19]), 0, 0, 0))
        frac = (frac + "000000000")[:9] if frac else "0"
        return sec * NS + int(frac)
    except (ValueError, IndexError):
        return None


def _num(x) -> float | None:
    if isinstance(x, dict):
        x = x.get("value")
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _levels(rows, descending: bool, depth: int) -> tuple[tuple[float, float], ...]:
    out = []
    for r in rows or ():
        if not isinstance(r, dict):
            continue
        px, q = _num(r.get("px")), _num(r.get("qty"))
        if px is None or q is None or q <= 0 or not 0.0 < px < 1.0:
            continue
        out.append((round(px, 6), q))
    out.sort(key=lambda t: -t[0] if descending else t[0])
    return tuple(out[:depth])


def parse_book(md: dict, tw: int, depth: int = 10) -> Book:
    st = md.get("state")
    stats = md.get("stats") if isinstance(md.get("stats"), dict) else {}
    return Book(tw=tw, bids=_levels(md.get("bids"), True, depth), asks=_levels(md.get("offers"), False, depth),
                shares_traded=_num((stats or {}).get("sharesTraded")), open=st in OPEN_STATES)


AGGRESSOR = {"ORDER_SIDE_BUY": "buy", "ORDER_SIDE_SELL": "sell"}


def parse_trade(tr: dict, tw: int) -> Trade | None:
    """None for anything we cannot attribute (unknown aggressor side, missing price/qty)."""
    px, q = _num(tr.get("price")), _num(tr.get("quantity"))
    side = AGGRESSOR.get(((tr.get("taker") or {}).get("side")) or "")
    if px is None or q is None or q <= 0 or side is None:
        return None
    return Trade(tw=tw, t_exch=iso_ns(tr.get("tradeTime")), price=round(px, 6), qty=q, aggressor=side,
                 id=tr.get("id"))


def ws_event(rec: dict, depth: int = 10) -> tuple[str, str | None, object, str | None] | None:
    """One WS tape record -> ("book", slug, Book, None) | ("trade", slug, Trade | None, state) |
    ("ctl", None, meta, ctl) | None."""
    meta = rec.get("meta") or {}
    if meta.get("ctl"):
        return ("ctl", None, meta, meta["ctl"])
    raw = rec.get("raw")
    if not raw:
        return None
    try:
        d = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    if isinstance(d.get("marketData"), dict):
        md = d["marketData"]
        return ("book", md.get("marketSlug"), parse_book(md, rec["tw"], depth), None)
    if isinstance(d.get("trade"), dict):
        tr = d["trade"]
        return ("trade", tr.get("marketSlug"), parse_trade(tr, rec["tw"]), tr.get("state"))
    if d.get("error") is not None:  # a refused subscription (the recorder also writes ctl sub_error)
        return None
    return None


def rest_event(rec: dict, depth: int = 10) -> tuple[str, str, Book | None] | None:
    """One ``pmus_books`` record -> ("book", slug, Book) | ("gap", slug, None) | None."""
    meta = rec.get("meta") or {}
    key = meta.get("key")
    if meta.get("ctl"):
        return ("gap", key, None) if meta["ctl"] in REST_GAP_CTL and key else None
    if not key:
        return None
    if meta.get("status") != 200:
        return ("gap", key, None)
    try:
        d = orjson.loads(rec.get("raw") or "")
    except orjson.JSONDecodeError:
        return ("gap", key, None)
    md = d.get("marketData") if isinstance(d, dict) else None
    if not isinstance(md, dict):
        return ("gap", key, None)
    return ("book", key, parse_book(md, rec["tw"], depth))


def rid_group(rid: str | None) -> tuple[str, str, int] | None:
    """Request id '{kind}-{horizon}-{start}-{n}' -> (kind, horizon, start)."""
    if not rid or not isinstance(rid, str):
        return None
    p = rid.split("-")
    if len(p) < 4 or p[1] not in HORIZONS:
        return None
    try:
        return p[0], p[1], int(p[2])
    except ValueError:
        return None


# ---------------------------------------------------------------------------- reading
def hour_path(root: Path, stream: str, hour: int) -> Path:
    t = time.gmtime(hour)
    return root / stream / time.strftime("%Y%m%d", t) / f"{t.tm_hour:02d}.dtape.zst"


def _scan(path: Path, needles: tuple[bytes, ...], lo: int, hi: int, head_only: bool = False):
    """Records of a segment whose line mentions one of ``needles`` (or is a ctl record), with
    lo <= tw < hi. CRC verified. ``head_only`` stops once tw passes hi by 30 s (files are written
    in receive order)."""
    if not path.exists():
        return
    for chunk in _frames(path):
        for line in chunk.splitlines():
            if not line:
                continue
            if b'"ctl":"' not in line and not any(n in line for n in needles):
                continue
            rec = orjson.loads(line)
            tw = rec["tw"]
            if tw >= hi:
                if head_only and tw >= hi + 30 * NS:
                    return
                continue
            if tw < lo:
                continue
            if zlib.crc32(rec["raw"].encode("utf-8")) != rec["crc"]:
                raise ValueError(f"CRC mismatch seq={rec['seq']} in {path}")
            yield rec


def tape_span(root: Path) -> tuple[int, int] | None:
    """[first hour start, last hour end) covered by the PM US WS streams."""
    hours = []
    for s in WS_STREAMS:
        for f in (root / s).glob("*/*.dtape.zst"):
            try:
                day = calendar.timegm((int(f.parent.name[:4]), int(f.parent.name[4:6]),
                                       int(f.parent.name[6:8]), int(f.stem.split(".")[0]), 0, 0, 0, 0, 0))
            except ValueError:
                continue
            hours.append(day)
    if not hours:
        return None
    return min(hours), max(hours) + 3600


def window_list(assets, horizons, t_from: int, t_to: int) -> list[tuple[int, str, str]]:
    """(start, horizon, asset) of every window with t_from <= start and end <= t_to, ordered by
    start, then horizon, then asset."""
    out = []
    for h in horizons:
        L = HORIZONS[h]
        st = -(-t_from // L) * L
        while st + L <= t_to:
            for a in assets:
                out.append((st, h, a))
            st += L
    out.sort(key=lambda w: (w[0], HORIZONS[w[1]], w[2]))
    return out


def iter_windows(root: Path, assets, horizons, t_from: int, t_to: int, depth: int = 10,
                 allow_stale_rest: bool = False) -> Iterator[WindowTape]:
    """Yield a ``WindowTape`` for every window in [t_from, t_to], ordered by start. REST snapshots
    are read only when ``allow_stale_rest`` (diagnostics; see the module docstring)."""
    root = Path(root)
    wins = window_list(assets, horizons, t_from, t_to)
    by_hour: dict[int, list[tuple[int, str, str]]] = {}
    for w in wins:
        by_hour.setdefault(w[0] // 3600 * 3600, []).append(w)
    carry: dict[str, list[dict]] = {}
    carry_hour = None
    for hour in sorted(by_hour):
        hw = by_hour[hour]
        slugs = {pmus_slug(a, h, st) for st, h, a in hw}
        nxt = {pmus_slug(a, h, hour + 3600) for _, h, a in hw}   # first windows of the next hour
        needles = tuple(s.encode() for s in sorted(slugs | nxt))
        lo, hi = hour - PRE_S, hour + 3600 + POST_S
        recs: dict[str, list[dict]] = {}
        new_carry: dict[str, list[dict]] = {}
        streams = (*WS_STREAMS, REST_STREAM) if allow_stale_rest else WS_STREAMS
        for stream in streams:
            if carry_hour == hour:
                got = list(carry.get(stream, []))
            else:  # discontinuity: read the previous hour's tail for pre-start books
                got = list(_scan(hour_path(root, stream, hour - 3600), needles, lo * NS, hour * NS))
            for rec in _scan(hour_path(root, stream, hour), needles, lo * NS, hi * NS):
                got.append(rec)
                if rec["tw"] >= (hour + 3600 - PRE_S) * NS:
                    new_carry.setdefault(stream, []).append(rec)
            for rec in _scan(hour_path(root, stream, hour + 3600), needles, lo * NS, hi * NS,
                             head_only=True):
                got.append(rec)
            got.sort(key=lambda r: (r["tw"], r.get("seq", 0)))
            recs[stream] = got
        carry, carry_hour = new_carry, hour + 3600
        parsed = {s: _parse_stream(s, recs[s], depth) for s in recs}
        for st, h, a in hw:
            yield _assemble(parsed, a, h, st)


def _parse_stream(stream: str, recs: list[dict], depth: int) -> dict:
    """-> {"books": {slug: [Book]}, "trades": {slug: [(Trade|None, state)]}, "gaps": {slug: [tw]},
    "stream_gaps": [tw], "group_gaps": {(horizon, start): [tw]}}"""
    out: dict = {"books": {}, "trades": {}, "gaps": {}, "stream_gaps": [], "group_gaps": {}}
    for rec in recs:
        if stream == REST_STREAM:
            ev = rest_event(rec, depth)
            if ev is None:
                continue
            if ev[0] == "book":
                out["books"].setdefault(ev[1], []).append(ev[2])
            else:
                out["gaps"].setdefault(ev[1], []).append(rec["tw"])
            continue
        ev = ws_event(rec, depth)
        if ev is None:
            continue
        kind, slug, obj, extra = ev
        if kind == "ctl":
            if extra in STREAM_GAP_CTL:
                out["stream_gaps"].append(rec["tw"])
            elif extra in GROUP_GAP_CTL and isinstance(obj, dict):
                g = rid_group(obj.get("rid"))
                if g is not None:
                    out["group_gaps"].setdefault(g, []).append(rec["tw"])
        elif kind == "book" and slug:
            out["books"].setdefault(slug, []).append(obj)
        elif kind == "trade" and slug:
            out["trades"].setdefault(slug, []).append((obj, extra))
    return out


def _assemble(parsed: dict, asset: str, horizon: str, start: int) -> WindowTape:
    L = HORIZONS[horizon]
    end = start + L
    slug = pmus_slug(asset, horizon, start)
    wt = WindowTape(slug=slug, asset=asset, horizon=horizon, start=start, end=end)
    lo, hi = (start - PRE_S) * NS, (end + POST_S) * NS
    # book source: the WS stream with the most snapshots for this slug (per-horizon stream on ties)
    best, best_n = None, 0
    for s in WS_FOR[horizon]:
        n = sum(1 for b in parsed.get(s, {}).get("books", {}).get(slug, []) if lo <= b.tw < hi)
        if n > best_n:
            best, best_n = s, n
    if best is not None:
        p = parsed[best]
        wt.source, wt.stream = "ws", best
        wt.books = [b for b in p["books"].get(slug, []) if lo <= b.tw < hi]
        gaps = list(p["stream_gaps"]) + p["group_gaps"].get(("md", horizon, start), [])
        wt.gaps = sorted(t for t in gaps if lo <= t < hi)
        trade_gaps = p["group_gaps"].get(("tr", horizon, start), [])
    else:
        trade_gaps = []
        rp = parsed.get(REST_STREAM, {})
        books = [b for b in rp.get("books", {}).get(slug, []) if lo <= b.tw < hi]
        if books:
            wt.source, wt.stream = "rest", REST_STREAM
            wt.books = books
            wt.gaps = sorted(t for t in rp.get("gaps", {}).get(slug, []) if lo <= t < hi)
    # trades: union of all WS streams, deduplicated by id; only TRADE_STATE_NEW (or no state)
    seen: set = set()
    dup = skipped = 0
    trades: list[Trade] = []
    for s in WS_FOR[horizon]:
        for tr, state in parsed.get(s, {}).get("trades", {}).get(slug, []):
            if tr is None or state not in ("TRADE_STATE_NEW", None):
                skipped += 1
                continue
            if not lo <= tr.tw < hi:
                continue
            key = tr.id or (tr.t_exch, tr.price, tr.qty, tr.aggressor)
            if key in seen:
                dup += 1
                continue
            seen.add(key)
            trades.append(tr)
    trades.sort(key=lambda t: (t.tw, t.id or ""))
    wt.trades = trades if wt.source == "ws" else []
    in_win = [t for t in wt.trades if t.t_venue < end * NS]
    last_book = next((b for b in reversed(wt.books) if b.shares_traded is not None and b.tw < hi), None)
    # trade-feed completeness: prints seen between two snapshots vs the venue's sharesTraded change
    vb = [b for b in wt.books if b.shares_traded is not None]
    coverage = None
    if wt.source == "ws" and len(vb) >= 2 and (vb[-1].shares_traded or 0.0) - (vb[0].shares_traded or 0.0) > 0:
        dv = (vb[-1].shares_traded or 0.0) - (vb[0].shares_traded or 0.0)
        seen_q = sum(t.qty for t in wt.trades if vb[0].tw < t.tw <= vb[-1].tw)
        coverage = round(seen_q / dv, 4)
    wt.stats = {"books": len(wt.books), "trades": len(wt.trades), "trades_dup": dup,
                "trades_skipped": skipped, "gap_events": len(wt.gaps),
                "trade_gap_events": sum(1 for t in trade_gaps if lo <= t < hi),
                "first_book_s": round((wt.books[0].tw - start * NS) / NS, 3) if wt.books else None,
                "trade_volume": round(sum(t.qty for t in in_win), 4),
                "venue_volume": last_book.shares_traded if last_book else None,
                "trade_coverage": coverage,
                "ws_trades_without_book": len(trades) if wt.source != "ws" else 0}
    return wt
