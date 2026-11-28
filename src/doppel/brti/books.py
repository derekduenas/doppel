# Vendored verbatim from PERELMAN books.py at commit 9b55eae3cb7a2030989d18086d7bdee20f51f7ad
# (github.com/derekduenas/perelman), sha256 76c1741267adc10d61cb3020f7af108556223597afe61716fbe45bf8537993e4.
# Do not edit: replica parity with PERELMAN depends on it. (BitstampSync is unused here.)
# pyright: reportOperatorIssue=false
"""Order-book state per BRTI constituent, built from each venue's public WebSocket.

Each handler takes one decoded frame and mutates a Book. Formats were verified
against live frames from atlas-do on 2026-11-24 (see probe_ws.py).
"""
from dataclasses import dataclass, field
from datetime import datetime

KRAKEN_DEPTH = 1000


def iso(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


@dataclass
class Book:
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    valid: bool = False
    last_msg: float = 0.0
    snapshots: int = 0
    updates: int = 0
    venue_ts: float = 0.0   # exchange timestamp of the latest applied message, seconds

    def reset(self):
        self.bids.clear()
        self.asks.clear()
        self.valid = False

    def put(self, side, price, size):
        levels = self.bids if side == "bid" else self.asks
        p, s = float(price), float(size)
        if s <= 0:
            levels.pop(p, None)
        else:
            levels[p] = s

    def replace(self, bids, asks):
        self.bids = {float(p): float(s) for p, s in bids if float(s) > 0}
        self.asks = {float(p): float(s) for p, s in asks if float(s) > 0}
        self.valid = bool(self.bids and self.asks)
        self.snapshots += 1

    def best(self):
        if not self.bids or not self.asks:
            return None, None
        return max(self.bids), min(self.asks)


def coinbase(book, m):
    t = m.get("type")
    if t == "snapshot":
        book.replace([x[:2] for x in m["bids"]], [x[:2] for x in m["asks"]])
    elif t == "l2update" and book.valid:
        for side, price, size in m["changes"]:
            book.put("bid" if side == "buy" else "ask", price, size)
        book.updates += 1
    if m.get("time") and t in ("snapshot", "l2update"):
        book.venue_ts = iso(m["time"])


def coinbase_at(book, m):
    """Coinbase Advanced Trade level2 (public, ~7 ms median lag vs ~26 ms for the
    exchange level2_batch feed, measured 2026-11-24)."""
    if m.get("channel") != "l2_data":
        return
    for ev in m.get("events", []):
        if ev.get("type") == "snapshot":
            book.bids.clear()
            book.asks.clear()
        elif not book.valid:
            continue
        for u in ev.get("updates", []):
            book.put("bid" if u["side"] == "bid" else "ask", u["price_level"], u["new_quantity"])
        if ev.get("type") == "snapshot":
            book.valid = bool(book.bids and book.asks)
            book.snapshots += 1
        else:
            book.updates += 1
    if m.get("timestamp"):
        book.venue_ts = iso(m["timestamp"])


def kraken(book, m):
    if m.get("channel") != "book":
        return
    d = m["data"][0]
    if m.get("type") == "snapshot":
        book.replace([(x["price"], x["qty"]) for x in d["bids"]], [(x["price"], x["qty"]) for x in d["asks"]])
    elif m.get("type") == "update" and book.valid:
        for x in d.get("bids", []):
            book.put("bid", x["price"], x["qty"])
        for x in d.get("asks", []):
            book.put("ask", x["price"], x["qty"])
        # Kraken requires truncating the local book to the subscribed depth.
        if len(book.bids) > KRAKEN_DEPTH:
            for p in sorted(book.bids, reverse=True)[KRAKEN_DEPTH:]:
                del book.bids[p]
        if len(book.asks) > KRAKEN_DEPTH:
            for p in sorted(book.asks)[KRAKEN_DEPTH:]:
                del book.asks[p]
        book.updates += 1
    if d.get("timestamp"):
        book.venue_ts = iso(d["timestamp"])


def bitstamp(book, m):
    """Bitstamp order_book channel: a full top-100 snapshot on every change.
    The diff channel left crossed books after every resync on 2026-11-24."""
    d = m.get("data") or {}
    if m.get("event") == "data" and "bids" in d:
        book.replace(d["bids"], d["asks"])
        book.venue_ts = int(d["microtimestamp"]) / 1e6


class BitstampSync:
    """Bitstamp diffs need a REST snapshot; diffs at or before it are discarded."""

    def __init__(self):
        self.pending = []
        self.snapshot_us = None

    def diff(self, book, m):
        d = m.get("data") or {}
        if "microtimestamp" not in d:
            return
        if self.snapshot_us is None:
            self.pending.append(d)
            return
        self._apply(book, d)

    def _apply(self, book, d):
        if int(d["microtimestamp"]) <= self.snapshot_us:
            return
        for p, s in d.get("bids", []):
            book.put("bid", p, s)
        for p, s in d.get("asks", []):
            book.put("ask", p, s)
        book.updates += 1

    def snapshot(self, book, rest):
        book.replace(rest["bids"], rest["asks"])
        self.snapshot_us = int(rest["microtimestamp"])
        for d in self.pending:
            self._apply(book, d)
        self.pending.clear()


def gemini(book, m):
    """Gemini v1 market data. The v2 l2 feed was tested on 2026-11-24 and its book
    disagreed with Gemini's own REST book by $10-115 from the first frame; v1
    matched REST exactly. Each change carries the absolute remaining size."""
    if m.get("type") != "update":
        return
    events = [e for e in m.get("events", []) if e.get("type") == "change"]
    initial = any(e.get("reason") == "initial" for e in events)
    if not initial and not book.valid:
        return
    for e in events:
        book.put("bid" if e["side"] == "bid" else "ask", e["price"], e["remaining"])
    if initial:
        book.valid = bool(book.bids and book.asks)
        book.snapshots += 1
    else:
        book.updates += 1
    if m.get("timestampms"):
        book.venue_ts = m["timestampms"] / 1000


def cryptocom(book, m):
    r = m.get("result") or {}
    ch = r.get("channel")
    if ch in ("book", "book.update") and r.get("data") and r["data"][0].get("t"):
        book.venue_ts = r["data"][0]["t"] / 1000
    if ch == "book":
        d = r["data"][0]
        book.replace([x[:2] for x in d["bids"]], [x[:2] for x in d["asks"]])
    elif ch == "book.update" and book.valid:
        u = r["data"][0].get("update", r["data"][0])
        for x in u.get("bids", []):
            book.put("bid", x[0], x[1])
        for x in u.get("asks", []):
            book.put("ask", x[0], x[1])
        book.updates += 1


def bullish(book, m):
    if m.get("dataType") != "V1TALevel2":
        return
    d = m["data"]
    if d.get("timestamp"):
        try:
            book.venue_ts = int(d["timestamp"]) / 1000
        except (TypeError, ValueError):
            pass
    flat = lambda xs: list(zip(xs[0::2], xs[1::2]))
    if m.get("type") == "snapshot":
        book.replace(flat(d["bids"]), flat(d["asks"]))
    elif book.valid:
        for p, s in flat(d.get("bids", [])):
            book.put("bid", p, s)
        for p, s in flat(d.get("asks", [])):
            book.put("ask", p, s)
        book.updates += 1
