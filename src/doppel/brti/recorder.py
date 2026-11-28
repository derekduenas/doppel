# Vendored from PERELMAN recorder.py at commit 9b55eae3cb7a2030989d18086d7bdee20f51f7ad
# (github.com/derekduenas/perelman), sha256 65e81e9834c5d182e5a51fdf60fb04ae4a054028b8124d2ecb5cdcf49483ff7f.
# Every record keeps PERELMAN's kind, keys, key order and json.dumps(separators=(",", ":")) bytes,
# and files keep the "%Y-%m-%dT%H.jsonl[.gz]" names, so the frozen doppel-brti-import reads this
# stream unchanged. Changes are listed in docs/BRTI-ABSORB.md ("What changed from PERELMAN").
"""DOPPEL BRTI recorder: published BRTI with receive stamps, plus PERELMAN's replica and side feeds.

Every whole second T it computes the replica from the public constituent books and records it with
timing. In parallel it polls the published BRTI (Kalshi's CF Benchmarks passthrough, read-scoped
key) every 500 ms and records when each published second was first seen.

Read-only: no order or account-write endpoint exists anywhere in this code. The Kalshi profile is
read from ``$CREDENTIALS_DIRECTORY/kalshi-profile.json`` (systemd ``LoadCredential``); it is never
logged, and errors are reduced to their type name.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import gzip
import json
import os
import shutil
import sys
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path
from statistics import median
from typing import Any

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

from . import books, fastbrti

SOURCE_COMMIT = "9b55eae3cb7a2030989d18086d7bdee20f51f7ad"
BRTI = fastbrti.brti  # PERELMAN prefers an optional Rust build (within $0.01); DOPPEL uses numpy
RETENTION_DAYS = 14
SILENCE_RECONNECT = 30
PE_OUT, PE_BACK = 0.05, 0.025   # Section 5.3 potentially erroneous data, with re-instatement
BRTI_PATH = "/trade-api/v2/cfbenchmarks/values"
BRTI_POLL = 0.5
BRTI_BACKOFF_MAX = 8.0          # each response carries ~120 s of values, so a backoff loses nothing
DIVERGE_LIMIT = 0.0008          # 8 bps from the other venues' median mid
DIVERGE_SECONDS = 3
RESYNC_COOLDOWN = 60
CALIBRATION_WINDOW = 300        # matched seconds in the rolling bias estimate
CALIBRATION_MIN = 30
MIN_VENUES_FOR_CALIBRATION = 5
INCREMENTAL_FEEDS = {"coinbase", "kraken"}
HOURLY_STRIKES = 8
OTHER_INDEXES = ("ETHUSD_RTI", "SOLUSD_RTI", "XRPUSD_RTI", "DOGEUSD_RTI")
FIFTEEN_MIN_SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M")
HOURLY_SERIES = (("KXBTCD", "BRTI"), ("KXETHD", "ETHUSD_RTI"), ("KXSOLD", "SOLUSD_RTI"), ("KXXRPD", "XRPUSD_RTI"))
RECENTER_FRAC = 0.0025   # re-centre the ladder after a 0.25% move
AUDIT_SECONDS = 30
AUDIT_LIMIT = 0.0008            # mids can differ by real movement during the REST round trip
# Feed groups that --without can switch off. The published BRTI poll is always on.
GROUPS = ("replica", "lmax", "rti", "kalshi", "hl", "bn")
AUDIT_URLS = {
    "coinbase": "https://api.exchange.coinbase.com/products/BTC-USD/ticker",
    "kraken": "https://api.kraken.com/0/public/Depth?pair=XBTUSD&count=1",
    "bitstamp": "https://www.bitstamp.net/api/v2/ticker/btcusd/",
    "gemini": "https://api.gemini.com/v1/book/btcusd?limit_bids=1&limit_asks=1",
    "cryptocom": "https://api.crypto.com/exchange/v1/public/get-book?instrument_name=BTC_USD&depth=10",
    "bullish": "https://api.exchange.bullish.com/trading-api/v1/markets/BTCUSD/orderbook/hybrid",
    "lmax": "https://public-data-api.london-digital.lmax.com/v1/orderbook/btc-usd",
}

FEEDS = {
    "coinbase": ("wss://advanced-trade-ws.coinbase.com",
                 [{"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "level2"},
                  {"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "heartbeats"}]),
    "kraken": ("wss://ws.kraken.com/v2",
               {"method": "subscribe", "params": {"channel": "book", "symbol": ["BTC/USD"], "depth": 1000}}),
    "bitstamp": ("wss://ws.bitstamp.net", {"event": "bts:subscribe", "data": {"channel": "order_book_btcusd"}}),
    "gemini": ("wss://api.gemini.com/v1/marketdata/BTCUSD?heartbeat=true&trades=false&auctions=false", None),
    "cryptocom": ("wss://stream.crypto.com/exchange/v1/market",
                  {"id": 1, "method": "subscribe", "params": {"channels": ["book.BTC_USD.150"]}}),
    "lmax": ("rest", None),
    "bullish": ("wss://api.exchange.bullish.com/trading-api/v1/market-data/orderbook",
                {"jsonrpc": "2.0", "type": "command", "method": "subscribe",
                 "params": {"topic": "l2Orderbook", "symbol": "BTCUSD"}, "id": "1"}),
}
REST_FEEDS = {
    "gemini": ("https://api.gemini.com/v1/book/btcusd?limit_bids=500&limit_asks=500",
               lambda j: ([(x["price"], x["amount"]) for x in j["bids"]],
                          [(x["price"], x["amount"]) for x in j["asks"]])),
    "lmax": ("https://public-data-api.london-digital.lmax.com/v1/orderbook/btc-usd",
             lambda j: ([(x["price"], x["quantity"]) for x in j["bids"]],
                        [(x["price"], x["quantity"]) for x in j["asks"]])),
}
HANDLERS = {"coinbase": books.coinbase_at, "kraken": books.kraken, "gemini": books.gemini, "bitstamp": books.bitstamp,
            "cryptocom": books.cryptocom, "bullish": books.bullish}


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _trim_torn_tail(path: Path) -> None:
    """Cut a line a crash left unfinished. The frozen importer rejects a malformed line once it is
    followed by a newline (an append after restart, or the hour's gzip), which would stop every import."""
    with path.open("rb+") as fh:
        size = fh.seek(0, os.SEEK_END)
        if not size:
            return
        fh.seek(size - 1)
        if fh.read(1) == b"\n":
            return
        keep = 0
        step = 1 << 16
        pos = size
        while pos > 0:
            start = max(0, pos - step)
            fh.seek(start)
            chunk = fh.read(pos - start)
            i = chunk.rfind(b"\n")
            if i >= 0:
                keep = start + i + 1
                break
            pos = start
        fh.truncate(keep)
        os.fsync(fh.fileno())


def _compress(path: Path) -> Path:
    """X.jsonl -> X.jsonl.gz: written under a name no reader globs, fsynced, then renamed."""
    gz = path.with_name(path.name + ".gz")
    tmp = path.with_name(path.name + ".gz.tmp")
    with path.open("rb") as src, tmp.open("wb") as raw:
        with gzip.GzipFile(filename=gz.name, mode="wb", fileobj=raw) as dst:
            shutil.copyfileobj(src, dst)
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(tmp, gz)
    _fsync_dir(path.parent)
    path.unlink()
    _fsync_dir(path.parent)
    return gz


class Sink:
    """Hourly JSONL files (UTC hour of the write), gzipped when the hour rolls; old files pruned.

    Gzip runs on a thread so the event loop never stalls on it. On the first open, a crash's leftovers
    are repaired: a torn last line is cut, earlier hours still in ``.jsonl`` are gzipped, and a
    half-written ``.gz.tmp`` is discarded."""

    def __init__(self, root: Path, clock=time.time):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.hour: str | None = None
        self.fh = None
        self.threads: list[threading.Thread] = []

    def write(self, record):
        hour = time.strftime("%Y-%m-%dT%H", time.gmtime(self.clock()))
        if hour != self.hour:
            self._roll(hour)
        assert self.fh is not None
        self.fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.fh.flush()

    def _recover(self, current: str) -> None:
        for tmp in self.root.glob("*.jsonl.gz.tmp"):
            tmp.unlink()
        for f in sorted(self.root.glob("*.jsonl")):
            if f.name == f"{current}.jsonl":
                continue
            if f.with_name(f.name + ".gz").exists():
                f.unlink()  # the .gz is renamed into place only after it is complete and fsynced
            else:
                _trim_torn_tail(f)
                _compress(f)

    def _finish(self, done: Path) -> None:
        _compress(done)
        cutoff = time.time() - RETENTION_DAYS * 86400
        for old in self.root.glob("*.jsonl.gz"):
            if old.stat().st_mtime < cutoff:
                old.unlink()

    def _roll(self, hour: str) -> None:
        if self.fh:
            self.sync()
            self.fh.close()
            done = self.root / f"{self.hour}.jsonl"
            self.threads = [t for t in self.threads if t.is_alive()]
            t = threading.Thread(target=self._finish, args=(done,), name="brti-sink-gzip")
            t.start()
            self.threads.append(t)
        else:
            self._recover(hour)
        self.hour = hour
        path = self.root / f"{hour}.jsonl"
        if path.exists():
            _trim_torn_tail(path)
        self.fh = path.open("a")

    def sync(self) -> None:
        if self.fh:
            self.fh.flush()
            os.fsync(self.fh.fileno())

    def join(self) -> None:
        for t in self.threads:
            t.join()
        self.threads = []

    def close(self) -> None:
        if self.fh:
            self.sync()
            self.fh.close()
            self.fh = None
        self.join()


def load_kalshi_key(cred_dir: str | None):
    """(key_id, private key) from the systemd credential. Raises without echoing any of it."""
    if not cred_dir:
        raise FileNotFoundError("CREDENTIALS_DIRECTORY")
    profile = json.loads((Path(cred_dir) / "kalshi-profile.json").read_text())
    key_id = profile["key_id"]
    key = serialization.load_pem_private_key(profile["private_key_pem"].encode(), password=None)
    del profile
    return key_id, key


class Recorder:
    def __init__(self, out: Path, without: frozenset[str] = frozenset(), sink: Sink | None = None):
        self.out = out
        self.without = without
        self.books = {v: books.Book() for v in FEEDS}
        self.connected = {v: False for v in FEEDS}
        self.excluded: set[str] = set()
        self.sink = sink or Sink(out)
        self.brti_seen: dict[int, float] = {}
        self.stats: dict[str, int] = {"replica": 0, "replica_fail": 0, "brti_polls": 0, "brti_errors": 0,
                                      "reconnects": 0, "resyncs": 0}
        self.last_replica: tuple[int, float] | None = None
        self.last_brti = None
        self.sockets: dict[str, Any] = {}
        self.diverged = {v: 0 for v in FEEDS}
        self.last_resync = {v: 0.0 for v in FEEDS}
        self.flagged: set[str] = set()
        self.replica_hist: dict[int, float] = {}
        self.residuals: deque[float] = deque(maxlen=CALIBRATION_WINDOW)
        self.rti_last: dict[str, list] = {}
        self._kid: str | None = None
        self._kkey: Any = None

    def health(self):
        """Self-healing books. A venue whose mid sits more than DIVERGE_LIMIT from the
        median of the other venues for DIVERGE_SECONDS straight, or whose own book is
        crossed, is excluded from the replica and resubscribed for a fresh snapshot.
        After a resync it rejoins as soon as it agrees again; a venue that still
        diverges on a fresh book is a real dislocation and is resynced at most once
        per RESYNC_COOLDOWN."""
        mids = {}
        for v, b in self.books.items():
            bid, ask = b.best()
            if not (self.connected[v] and b.valid) or bid is None or ask is None:
                continue
            if bid >= ask:
                # Snapshot feeds deliver the venue's own crossed book: CF section 5.2.1
                # disregards it for that calculation. Only incremental books can be
                # repaired by resubscribing.
                if v in INCREMENTAL_FEEDS:
                    self.resync(v, "crossed")
                continue
            mids[v] = (bid + ask) / 2
        for v, mid in mids.items():
            others = [m for u, m in mids.items() if u != v]
            if len(others) < 2:
                continue
            if abs(mid / median(others) - 1) > DIVERGE_LIMIT:
                self.diverged[v] += 1
                if self.diverged[v] >= DIVERGE_SECONDS:
                    self.flagged.add(v)
                    self.resync(v, "diverged")
            else:
                self.diverged[v] = 0
                self.flagged.discard(v)

    def resync(self, venue, reason):
        now = time.time()
        if now - self.last_resync[venue] < RESYNC_COOLDOWN:
            return
        self.last_resync[venue] = now
        self.stats["resyncs"] += 1
        self.sink.write({"kind": "resync", "venue": venue, "reason": reason, "at": now})
        ws = self.sockets.get(venue)
        if ws is not None:
            asyncio.create_task(ws.close())

    def bias(self):
        return median(self.residuals) if len(self.residuals) >= CALIBRATION_MIN else 0.0

    async def rest_poll(self, venue):
        """1 Hz REST books. Gemini: its WebSockets lagged ~5 s on 2026-11-24 while REST
        was current. LMAX Digital: the 7th BRTI constituent, public REST only. Per the
        CF guide a REST book's Retrieval Time is the request time."""
        book = self.books[venue]
        url, parse = REST_FEEDS[venue]
        backoff = 0.0

        def get():
            req = urllib.request.Request(url, headers={"User-Agent": "research-client"})
            sent = time.time()
            with urllib.request.urlopen(req, timeout=3) as r:
                return sent, json.loads(r.read(8 * 1024 * 1024))
        while True:
            tick = time.time()
            try:
                sent, j = await asyncio.to_thread(get)
                bids, asks = parse(j)
                book.replace(bids, asks)
                book.venue_ts, book.last_msg = sent, time.time()
                self.connected[venue] = True
                backoff = 0.0
            except Exception as error:
                self.connected[venue] = False
                book.reset()
                code = getattr(error, "code", None)
                self.sink.write({"kind": "feed_error", "venue": venue, "at": time.time(),
                                 "error": type(error).__name__, "code": code})
                # Rate limited (429) or failing: back off exponentially up to 5 min so a
                # temporary block is not kept alive by our own retries.
                backoff = min(max(2 * backoff, 5.0), 300.0)
            await asyncio.sleep(max(0.05, 1.0 - (time.time() - tick)) + backoff)

    async def feed(self, venue):
        if venue in REST_FEEDS:
            return await self.rest_poll(venue)
        url, sub = FEEDS[venue]
        book, backoff = self.books[venue], 1
        while True:
            try:
                async with websockets.connect(url, open_timeout=10, max_size=2**25, ping_interval=15) as ws:
                    for s in (sub if isinstance(sub, list) else [sub] if sub else []):
                        await ws.send(json.dumps(s))
                    self.sockets[venue] = ws
                    self.connected[venue] = True
                    backoff = 1
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), SILENCE_RECONNECT)
                        book.last_msg = time.time()
                        m = json.loads(raw)
                        if venue == "cryptocom" and m.get("method") == "public/heartbeat":
                            await ws.send(json.dumps({"id": m["id"], "method": "public/respond-heartbeat"}))
                            continue
                        HANDLERS[venue](book, m)
            except Exception as error:  # any failure: drop the book, reconnect with backoff
                self.sink.write({"kind": "feed_error", "venue": venue, "at": time.time(),
                                 "error": type(error).__name__})
            self.connected[venue] = False
            self.sockets.pop(venue, None)
            self.diverged[venue] = 0
            book.reset()
            self.stats["reconnects"] += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    def usable(self):
        """Section 5: connected + valid books, then the 5% / 2.5% mid screen."""
        live = {v: b for v, b in self.books.items()
                if self.connected[v] and b.valid and v not in self.flagged}
        mids = {}
        for v, b in live.items():
            bid, ask = b.best()
            if bid is not None and ask is not None and bid < ask:
                mids[v] = (bid + ask) / 2
        if not mids:
            return {}
        med = median(mids.values())
        for v, mid in mids.items():
            dev = abs(mid / med - 1)
            if v in self.excluded and dev < PE_BACK:
                self.excluded.discard(v)
            elif dev > PE_OUT:
                self.excluded.add(v)
        return {v: live[v] for v in mids if v not in self.excluded}

    async def replica_loop(self):
        """Replica every 200 ms, on the same boundaries CF uses (methodology v17.0 section
        6.2). Whole seconds get the full record used for settlement matching; the
        sub-second ticks get a compact "live" record, the continuous fair value."""
        while True:
            now = time.time()
            boundary = (int(now * 5) + 1) / 5
            await asyncio.sleep(boundary - now)
            if time.time() - boundary > 0.15:   # fell behind: skip to the next boundary
                self.stats["live_skipped"] = self.stats.get("live_skipped", 0) + 1
                continue
            if round(boundary * 5) % 5:
                await self.live_tick(boundary)
                continue
            t = int(round(boundary))
            self.health()
            usable = self.usable()
            frozen = {v: (dict(b.bids), dict(b.asks)) for v, b in usable.items()}
            frozen_at = time.time()
            venues = {v: {"bid": b.best()[0], "ask": b.best()[1], "nb": len(b.bids), "na": len(b.asks),
                          "age": round(frozen_at - b.last_msg, 3),
                          "lag": round(b.last_msg - b.venue_ts, 3) if b.venue_ts else None,
                          "connected": self.connected[v],
                          "valid": b.valid} for v, b in self.books.items()}
            record: dict[str, Any] = {"kind": "replica", "t": t, "frozen_at": frozen_at, "venues": venues,
                                      "used": sorted(frozen), "excluded": sorted(self.excluded),
                                      "flagged": sorted(self.flagged)}
            try:
                value, diag = await asyncio.to_thread(BRTI, list(frozen.values()))
                bias = self.bias()  # uses only published values already received: causal
                record.update(value=value, nowcast=value + bias, bias=bias,
                              calc_done=time.time(), diag=diag)
                self.stats["replica"] += 1
                self.last_replica = (t, value)
                if len(frozen) >= MIN_VENUES_FOR_CALIBRATION:
                    self.replica_hist[t] = value
                    for old in [k for k in self.replica_hist if k < t - 600]:
                        del self.replica_hist[old]
            except Exception as error:
                record.update(value=None, error=type(error).__name__)
                self.stats["replica_fail"] += 1
            self.sink.write(record)

    async def live_tick(self, boundary):
        frozen = {v: (dict(b.bids), dict(b.asks)) for v, b in self.usable().items()}
        frozen_at = time.time()
        try:
            value, diag = await asyncio.to_thread(BRTI, list(frozen.values()))
        except Exception:
            self.stats["live_fail"] = self.stats.get("live_fail", 0) + 1
            return
        self.stats["live"] = self.stats.get("live", 0) + 1
        self.sink.write({"kind": "live", "tick": round(boundary, 1), "frozen_at": frozen_at,
                         "calc_done": time.time(), "value": value, "nowcast": value + self.bias(),
                         "n": len(frozen)})

    def load_credentials(self, cred_dir: str | None) -> None:
        self._kid, self._kkey = load_kalshi_key(cred_dir)

    def kalshi_headers(self, path):
        if self._kkey is None:
            raise RuntimeError("Kalshi credential not loaded")
        stamp = str(int(time.time() * 1000))
        msg = (stamp + "GET" + path).encode()
        sig = self._kkey.sign(msg) if isinstance(self._kkey, ed25519.Ed25519PrivateKey) else self._kkey.sign(
            msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
        return {"KALSHI-ACCESS-KEY": self._kid, "KALSHI-ACCESS-TIMESTAMP": stamp,
                "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}

    def on_brti(self, sent, received, body):
        """One published-BRTI response -> PERELMAN's ``brti`` record (or nothing if no new second)."""
        self.stats["brti_polls"] += 1
        data = body.get("data", body)
        fresh = []
        for row in data.get("payload", []):
            at = int(row["time"])
            if at not in self.brti_seen and at >= (sent - 120) * 1000:
                self.brti_seen[at] = received
                fresh.append([at, row["value"]])
                if at // 1000 in self.replica_hist:
                    self.residuals.append(float(row["value"]) - self.replica_hist[at // 1000])
        if fresh:
            self.last_brti = fresh[-1]
            self.sink.write({"kind": "brti", "sent": sent, "received": received,
                             "server_time": data.get("serverTime"), "new": fresh})
        if len(self.brti_seen) > 20000:
            for k in sorted(self.brti_seen)[:10000]:
                del self.brti_seen[k]

    async def brti_loop(self):
        def get():
            req = urllib.request.Request("https://external-api.kalshi.com" + BRTI_PATH + "?id=BRTI",
                                         headers=self.kalshi_headers(BRTI_PATH))
            sent = time.time()
            with urllib.request.urlopen(req, timeout=5) as r:
                body = json.loads(r.read(4 * 1024 * 1024))
            return sent, time.time(), body

        errors = 0
        while True:
            tick = time.time()
            try:
                sent, received, body = await asyncio.to_thread(get)
                self.on_brti(sent, received, body)
                errors = 0
            except Exception as error:
                errors += 1
                self.stats["brti_errors"] += 1
                self.sink.write({"kind": "brti_error", "at": time.time(), "error": type(error).__name__})
            wait = BRTI_POLL if not errors else min(BRTI_POLL * 2 ** min(errors, 5), BRTI_BACKOFF_MAX)
            await asyncio.sleep(max(0.05, wait - (time.time() - tick)))

    async def kalshi_hourly_loop(self, series="KXBTCD", index="BRTI"):
        """KXBTCD hourly ladder: the HOURLY_STRIKES strikes nearest the replica for
        the next close, same settlement index as KXBTC15M. Records top-of-book
        changes per ticker as "kq" with series "KXBTCD"; re-selects strikes each hour.
        KXBTCD needs the replica group (its strikes centre on the replica)."""
        def ladder():
            url = ("https://api.elections.kalshi.com/trade-api/v2/markets"
                   f"?series_ticker={series}&status=open&limit=1000")
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research-client"}),
                                        timeout=10) as r:
                markets = json.loads(r.read())["markets"]
            now = time.time()
            live = [m for m in markets if books.iso(m["close_time"]) > now + 30 and m.get("floor_strike")]
            px = self.price(index)
            if not live or px is None:
                return None, []
            # The close with the most traded volume, among those within 6 hours.
            vol = {}
            for m in live:
                c = books.iso(m["close_time"])
                if c - now <= 6 * 3600:
                    vol[c] = vol.get(c, 0.0) + float(m.get("volume_fp") or 0)
            if not vol:
                return None, []
            close = max(vol, key=lambda c: vol[c])
            same = [m for m in live if books.iso(m["close_time"]) == close]
            same.sort(key=lambda m: abs(float(m["floor_strike"]) - px))
            return close, same[:HOURLY_STRIKES]

        while True:
            try:
                close, chosen = await asyncio.to_thread(ladder)
                if not chosen or close is None:
                    await asyncio.sleep(10)
                    continue
                tickers = [m["ticker"] for m in chosen]
                for m in chosen:
                    self.sink.write({"kind": "kmarket", "series": series, "ticker": m["ticker"], "close": close,
                                     "strike": m.get("floor_strike"), "at": time.time()})
                path = "/trade-api/ws/v2"
                async with websockets.connect("wss://api.elections.kalshi.com" + path,
                                              additional_headers=self.kalshi_headers(path),
                                              open_timeout=10, max_size=2**24, ping_interval=15) as ws:
                    await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                        "channels": ["orderbook_delta", "trade"], "market_tickers": tickers}}))
                    yes: dict[Any, dict[float, float]] = {}
                    no: dict[Any, dict[float, float]] = {}
                    last: dict[Any, tuple] = {}
                    center = self.price(index)
                    while time.time() < close:
                        # Re-center the ladder when price drifts two strike steps away.
                        now_px = self.price(index)
                        if now_px is not None and center is not None and abs(now_px - center) > RECENTER_FRAC * center:
                            self.sink.write({"kind": "kladder_recenter", "at": time.time(), "from": center,
                                             "to": now_px, "series": series})
                            break
                        raw = await asyncio.wait_for(ws.recv(), SILENCE_RECONNECT)
                        recv = time.time()
                        m = json.loads(raw)
                        body = m.get("msg") or {}
                        tk = body.get("market_ticker")
                        if m.get("type") == "trade":
                            self.sink.write({"kind": "ktrade", "series": series, "ticker": tk, "recv": recv,
                                             "yes_price": body.get("yes_price_dollars"),
                                             "count": body.get("count_fp"), "taker": body.get("taker_side")})
                            continue
                        if m.get("type") == "orderbook_snapshot":
                            yes[tk] = {float(p): float(q) for p, q in body.get("yes_dollars_fp", [])}
                            no[tk] = {float(p): float(q) for p, q in body.get("no_dollars_fp", [])}
                        elif m.get("type") == "orderbook_delta" and tk in yes:
                            side = yes[tk] if body["side"] == "yes" else no[tk]
                            p = float(body["price_dollars"])
                            side[p] = side.get(p, 0.0) + float(body["delta_fp"])
                            if side[p] <= 1e-9:
                                del side[p]
                        else:
                            continue
                        yb = max(yes[tk]) if yes[tk] else None
                        nb = max(no[tk]) if no[tk] else None
                        # Top of book with queue sizes: YES bid size, and the NO bid size that
                        # makes up the YES ask. The maker fill model needs both.
                        top = (yb, round(1 - nb, 4) if nb is not None else None,
                               round(yes[tk][yb], 2) if yb is not None else None,
                               round(no[tk][nb], 2) if nb is not None else None)
                        if top != last.get(tk):
                            last[tk] = top
                            ts = body.get("ts")
                            self.sink.write({"kind": "kq", "series": series, "ticker": tk, "recv": recv,
                                             "venue_ts": books.iso(ts) if ts else None, "yb": top[0], "ya": top[1],
                                             "ybs": top[2], "yas": top[3]})
                            self.stats["kalshi_hourly_quotes"] = self.stats.get("kalshi_hourly_quotes", 0) + 1
            except Exception as error:
                self.sink.write({"kind": "feed_error", "venue": "kalshi_hourly_" + series, "at": time.time(),
                                 "error": type(error).__name__})
                await asyncio.sleep(5)

    async def kalshi_loop(self, series="KXBTC15M"):
        """KXBTC15M quotes over Kalshi's WebSocket (read-only orderbook_delta channel).
        Writes a record only when the best YES bid or ask changes, stamped with
        Kalshi's own event time and our receipt time."""
        def open_market():
            url = f"https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker={series}&status=open&limit=20"
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research-client"}),
                                        timeout=8) as r:
                markets = json.loads(r.read())["markets"]
            now = time.time()
            live = [m for m in markets if books.iso(m["open_time"]) <= now < books.iso(m["close_time"]) - 2]
            return min(live, key=lambda m: m["close_time"]) if live else None

        while True:
            try:
                market = await asyncio.to_thread(open_market)
                if not market:
                    await asyncio.sleep(5)
                    continue
                ticker, close = market["ticker"], books.iso(market["close_time"])
                self.sink.write({"kind": "kmarket", "series": series, "ticker": ticker, "close": close,
                                 "strike": market.get("floor_strike"), "at": time.time()})
                path = "/trade-api/ws/v2"
                async with websockets.connect("wss://api.elections.kalshi.com" + path,
                                              additional_headers=self.kalshi_headers(path),
                                              open_timeout=10, max_size=2**24, ping_interval=15) as ws:
                    await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
                        "channels": ["orderbook_delta"], "market_tickers": [ticker]}}))
                    yes: dict[float, float] = {}
                    no: dict[float, float] = {}
                    last = None
                    while time.time() < close:
                        raw = await asyncio.wait_for(ws.recv(), SILENCE_RECONNECT)
                        recv = time.time()
                        m = json.loads(raw)
                        body = m.get("msg") or {}
                        if m.get("type") == "orderbook_snapshot":
                            yes = {float(p): float(q) for p, q in body.get("yes_dollars_fp", [])}
                            no = {float(p): float(q) for p, q in body.get("no_dollars_fp", [])}
                        elif m.get("type") == "orderbook_delta":
                            side = yes if body["side"] == "yes" else no
                            p = float(body["price_dollars"])
                            side[p] = side.get(p, 0.0) + float(body["delta_fp"])
                            if side[p] <= 1e-9:
                                del side[p]
                        else:
                            continue
                        yb = max(yes) if yes else None
                        nb = max(no) if no else None
                        top = (yb, round(1 - nb, 4) if nb is not None else None,
                               round(yes[yb], 2) if yb is not None else None,
                               round(no[nb], 2) if nb is not None else None)
                        if top != last:
                            last = top
                            ts = body.get("ts")
                            self.sink.write({"kind": "kq", "series": series, "ticker": ticker, "recv": recv,
                                             "venue_ts": books.iso(ts) if ts else None,
                                             "sent_ms": m.get("sending_ts_ms"), "yb": top[0], "ya": top[1],
                                             "ybs": top[2], "yas": top[3], "seq": m.get("seq")})
                            self.stats["kalshi_quotes"] = self.stats.get("kalshi_quotes", 0) + 1
            except Exception as error:
                self.sink.write({"kind": "feed_error", "venue": "kalshi", "at": time.time(),
                                 "error": type(error).__name__})
                await asyncio.sleep(3)

    async def hyperliquid_loop(self):
        """Hyperliquid BTC perp best bid/offer, read-only public data, as a candidate
        leading signal for the KXBTC15M market makers. One record per BBO change."""
        while True:
            try:
                async with websockets.connect("wss://api.hyperliquid.xyz/ws", open_timeout=10,
                                              ping_interval=20) as ws:
                    await ws.send(json.dumps({"method": "subscribe",
                                              "subscription": {"type": "bbo", "coin": "BTC"}}))
                    last = None
                    while True:
                        m = json.loads(await asyncio.wait_for(ws.recv(), SILENCE_RECONNECT))
                        if m.get("channel") != "bbo":
                            continue
                        bid, ask = m["data"]["bbo"]
                        mid = (float(bid["px"]) + float(ask["px"])) / 2
                        if mid != last:
                            last = mid
                            self.sink.write({"kind": "hl", "recv": time.time(), "ts": m["data"]["time"] / 1000,
                                             "mid": mid, "bid_sz": float(bid["sz"]), "ask_sz": float(ask["sz"])})
                            self.stats["hl"] = self.stats.get("hl", 0) + 1
            except Exception as error:
                self.sink.write({"kind": "feed_error", "venue": "hyperliquid", "at": time.time(),
                                 "error": type(error).__name__})
                await asyncio.sleep(3)

    async def binance_loop(self):
        """Binance BTCUSDT spot best bid/offer from data-stream.binance.vision, the
        market-data-only domain Binance itself serves to US addresses (checked
        2026-11-24; api.binance.com and fapi return 451). Read-only, private research
        use, no redistribution. Candidate leading signal for the KXBTC15M makers."""
        url = "wss://data-stream.binance.vision/ws/btcusdt@bookTicker"
        while True:
            try:
                async with websockets.connect(url, open_timeout=10, ping_interval=20) as ws:
                    last = None
                    while True:
                        m = json.loads(await asyncio.wait_for(ws.recv(), SILENCE_RECONNECT))
                        if "b" not in m:
                            continue
                        mid = (float(m["b"]) + float(m["a"])) / 2
                        if mid != last:
                            last = mid
                            self.sink.write({"kind": "bn", "recv": time.time(), "mid": mid, "u": m.get("u")})
                            self.stats["bn"] = self.stats.get("bn", 0) + 1
            except Exception as error:
                self.sink.write({"kind": "feed_error", "venue": "binance", "at": time.time(),
                                 "error": type(error).__name__})
                await asyncio.sleep(3)

    def price(self, index):
        """Latest price for a CF index: the BRTI replica for BTC, else the published value."""
        if index == "BRTI":
            return self.last_replica[1] if self.last_replica else None
        last = self.rti_last.get(index)
        return float(last[1]) if last else None

    async def rti_loop(self):
        """Published CF indexes for other coins (ETH, SOL, XRP, DOGE) via Kalshi's
        read-only passthrough, each polled every ~0.5 s. First-seen receipt times
        are recorded the same way as for BRTI."""
        seen: dict[str, set[int]] = {i: set() for i in OTHER_INDEXES}

        def get(index):
            path = BRTI_PATH
            req = urllib.request.Request("https://external-api.kalshi.com" + path + "?id=" + index,
                                         headers=self.kalshi_headers(path))
            sent = time.time()
            with urllib.request.urlopen(req, timeout=5) as r:
                body = json.loads(r.read(4 * 1024 * 1024))
            return sent, time.time(), body

        while True:
            for index in OTHER_INDEXES:
                # One request every 0.5 s, round-robin: each coin every 2 s. BTC's own
                # 2 Hz BRTI poll takes priority within Kalshi's read budget.
                tick = time.time()
                try:
                    sent, received, body = await asyncio.to_thread(get, index)
                    data = body.get("data", body)
                    fresh = []
                    for row in data.get("payload", []):
                        at = int(row["time"])
                        if at not in seen[index] and at >= (sent - 120) * 1000:
                            seen[index].add(at)
                            fresh.append([at, row["value"]])
                    if fresh:
                        self.rti_last[index] = fresh[-1]
                        self.sink.write({"kind": "rti", "id": index, "sent": sent, "received": received,
                                         "new": fresh})
                    if len(seen[index]) > 20000:
                        seen[index] = set(sorted(seen[index])[-10000:])
                except Exception as error:
                    self.sink.write({"kind": "rti_error", "id": index, "at": time.time(),
                                     "error": type(error).__name__, "code": getattr(error, "code", None)})
                await asyncio.sleep(max(0.05, 0.5 - (time.time() - tick)))

    async def audit_loop(self):
        """Independent check of every WebSocket book against the venue's own REST
        book. This is the check that exposed the broken Gemini v2 feed. Two
        consecutive mid disagreements above AUDIT_LIMIT trigger a resync."""
        strikes = {v: 0 for v in FEEDS}

        def top(venue) -> tuple[float, float]:
            def get(url):
                req = urllib.request.Request(url, headers={"User-Agent": "research-client"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    return json.loads(r.read(2 * 1024 * 1024))
            j = get(AUDIT_URLS[venue])
            if venue == "coinbase":
                return float(j["bid"]), float(j["ask"])
            if venue == "kraken":
                k = next(iter(j["result"].values()))
                return float(k["bids"][0][0]), float(k["asks"][0][0])
            if venue == "bitstamp":
                return float(j["bid"]), float(j["ask"])
            if venue == "gemini":
                return float(j["bids"][0]["price"]), float(j["asks"][0]["price"])
            if venue == "cryptocom":
                d = j["result"]["data"][0]
                return float(d["bids"][0][0]), float(d["asks"][0][0])
            if venue == "bullish":
                return float(j["bids"][0]["price"]), float(j["asks"][0]["price"])
            if venue == "lmax":
                return float(j["bids"][0]["price"]), float(j["asks"][0]["price"])
            raise KeyError(venue)

        while True:
            await asyncio.sleep(AUDIT_SECONDS)
            for venue in FEEDS:
                book = self.books[venue]
                if not (self.connected[venue] and book.valid):
                    continue
                try:
                    sent = time.time()
                    rb, ra = await asyncio.to_thread(top, venue)
                    got = time.time()
                except Exception as error:
                    self.sink.write({"kind": "audit_error", "venue": venue, "error": type(error).__name__})
                    continue
                wb, wa = book.best()
                if wb is None or wa is None:
                    continue
                gap = abs((wb + wa) / 2 / ((rb + ra) / 2) - 1)
                self.sink.write({"kind": "audit", "venue": venue, "sent": sent, "received": got,
                                 "ws": [wb, wa], "rest": [rb, ra], "gap_bps": round(gap * 1e4, 2)})
                strikes[venue] = strikes[venue] + 1 if gap > AUDIT_LIMIT else 0
                if strikes[venue] >= 2:
                    strikes[venue] = 0
                    self.resync(venue, "audit")

    def write_status(self) -> None:
        status = {"at": time.time(), "stats": self.stats, "connected": self.connected,
                  "valid": {v: b.valid for v, b in self.books.items()},
                  "excluded": sorted(self.excluded), "flagged": sorted(self.flagged),
                  "bias": round(self.bias(), 3), "calibration_n": len(self.residuals),
                  "last_replica": self.last_replica,
                  "last_brti": self.last_brti,
                  "source_commit": SOURCE_COMMIT, "without": sorted(self.without)}
        tmp = self.out / "status.json.tmp"
        tmp.write_text(json.dumps(status))
        tmp.replace(self.out / "status.json")

    async def status_loop(self):
        while True:
            self.sink.sync()  # at most ~1 s of records can be lost to a host crash
            self.write_status()
            await asyncio.sleep(1)

    def tasks(self):
        w = self.without
        jobs = [self.brti_loop(), self.status_loop()]
        if "replica" not in w:
            jobs += [self.feed(v) for v in FEEDS if not (v == "lmax" and "lmax" in w)]
            jobs += [self.replica_loop(), self.audit_loop()]
        if "rti" not in w:
            jobs.append(self.rti_loop())
        if "kalshi" not in w:
            jobs += [self.kalshi_loop(x) for x in FIFTEEN_MIN_SERIES]
            jobs += [self.kalshi_hourly_loop(x, i) for x, i in HOURLY_SERIES]
        if "hl" not in w:
            jobs.append(self.hyperliquid_loop())
        if "bn" not in w:
            jobs.append(self.binance_loop())
        return jobs

    async def run(self):
        try:
            await asyncio.gather(*self.tasks())
        finally:
            self.sink.close()


def parse_without(text: str) -> frozenset[str]:
    groups = frozenset(x.strip() for x in text.split(",") if x.strip())
    unknown = groups - set(GROUPS)
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown feed groups: {sorted(unknown)} (known: {', '.join(GROUPS)})")
    return groups


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-brti-record", description="DOPPEL BRTI recorder (absorbed from PERELMAN)")
    ap.add_argument("--out", type=Path, required=True, help="stream directory (hourly *.jsonl[.gz] + status.json)")
    ap.add_argument("--without", type=parse_without, default=frozenset(),
                    help=f"comma-separated feed groups to skip: {', '.join(GROUPS)} (published BRTI is always on)")
    a = ap.parse_args(argv)
    rec = Recorder(a.out, a.without)
    try:
        rec.load_credentials(os.environ.get("CREDENTIALS_DIRECTORY"))
    except Exception as error:
        print(f"doppel-brti-record: Kalshi credential unavailable ({type(error).__name__})", file=sys.stderr)
        return 2
    asyncio.run(rec.run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
