"""Concrete recorders for M0."""

from __future__ import annotations

import time

import aiohttp
import orjson

from ..universe import KALSHI, IntlUniverse, KalshiUniverse, pmus_targets
from .base import SSL_CTX, UA, PollSource, WsSource


class RtdsTrades(WsSource):
    """Polymarket RTDS ``activity/trades``: every intl match with wallet, role (fee>0 = taker),
    price, size, fee and tx hash. Run two instances (a/b) for redundancy and cross-checking."""

    url = "wss://ws-live-data.polymarket.com"
    stall_s = 10.0
    keepalive_msg = "ping"
    keepalive_s = 5.0

    async def on_open(self, ws) -> None:
        sub = {"action": "subscribe", "subscriptions": [{"topic": "activity", "type": "trades"}]}
        await ws.send(orjson.dumps(sub).decode())
        self.ctl("subscribe", sub=sub)

    def is_keepalive_reply(self, raw) -> bool:
        return raw in ("", "PONG", "pong", b"", b"PONG", b"pong")


class IntlClobMarket(WsSource):
    """Intl CLOB market channel (anonymous books, deltas and prints) for the rolling crypto window
    universe. New windows are added in place. Old ones expire on their own and are dropped at the
    next reconnect."""

    url = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    stall_s = 20.0
    keepalive_msg = "PING"
    keepalive_s = 10.0

    def __init__(self, name, writer, universe: IntlUniverse):
        super().__init__(name, writer)
        self.universe = universe
        self.subscribed: set[str] = set()
        self._session: aiohttp.ClientSession | None = None
        self._next_refresh = 0.0

    def expect_traffic(self) -> bool:
        return bool(self.subscribed)

    async def run(self, stop) -> None:
        try:
            await super().run(stop)
        finally:
            if self._session is not None and not self._session.closed:
                await self._session.close()

    async def _desired(self) -> set[str]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX),
                                                  timeout=aiohttp.ClientTimeout(total=10),
                                                  headers={"user-agent": UA})
        wins = await self.universe.current(self._session)
        self.stats.extra["windows"] = len(wins)
        return {t for w in wins for t in w.tokens}

    async def on_open(self, ws) -> None:
        self.subscribed = set()
        want = await self._desired()
        if want:
            msg = {"assets_ids": sorted(want), "type": "market"}
            await ws.send(orjson.dumps(msg).decode())
            self.subscribed = want
            self.ctl("subscribe", n=len(want), assets=sorted(want))
        self._next_refresh = time.monotonic() + 15

    async def on_tick(self, ws) -> None:
        if time.monotonic() < self._next_refresh:
            return
        self._next_refresh = time.monotonic() + 15
        try:
            want = await self._desired()
        except Exception as exc:  # noqa: BLE001
            self.stats.errors += 1
            self.stats.last_error = f"universe: {type(exc).__name__}: {str(exc)[:160]}"
            return
        stale = self.subscribed - want
        if stale:
            msg = {"assets_ids": sorted(stale), "operation": "unsubscribe"}
            await ws.send(orjson.dumps(msg).decode())
            self.subscribed -= stale
            self.ctl("unsubscribe", n=len(stale))
        new = want - self.subscribed
        if new:
            msg = {"assets_ids": sorted(new), "operation": "subscribe"}
            await ws.send(orjson.dumps(msg).decode())
            self.subscribed |= new
            self.ctl("subscribe_add", n=len(new), assets=sorted(new))
        self.stats.extra["subscribed"] = len(self.subscribed)


class CoinbaseTrades(WsSource):
    url = "wss://ws-feed.exchange.coinbase.com"
    stall_s = 30.0
    products = ("BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD")

    async def on_open(self, ws) -> None:
        await ws.send(orjson.dumps({"type": "subscribe", "product_ids": list(self.products),
                                    "channels": ["matches", "heartbeat"]}).decode())
        self.ctl("subscribe", products=list(self.products))


class KrakenTrades(WsSource):
    url = "wss://ws.kraken.com/v2"
    stall_s = 30.0

    async def on_open(self, ws) -> None:
        await ws.send(orjson.dumps({"method": "subscribe", "params": {
            "channel": "trade", "symbol": ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD"]}}).decode())
        self.ctl("subscribe")


class BitstampTrades(WsSource):
    url = "wss://ws.bitstamp.net"
    stall_s = 60.0

    async def on_open(self, ws) -> None:
        for pair in ("btcusd", "ethusd", "solusd", "xrpusd"):
            await ws.send(orjson.dumps({"event": "bts:subscribe",
                                        "data": {"channel": f"live_trades_{pair}"}}).decode())
        self.ctl("subscribe")


class PmusBooks(PollSource):
    """Polymarket US public book for the current 5m/15m windows (REST; WS needs an API key)."""

    interval_s = 1.0

    async def targets(self, session):
        return pmus_targets()


class KalshiBooks(PollSource):
    """Kalshi public orderbook for the nearest open 15m crypto markets (REST; WS needs a key)."""

    interval_s = 1.0

    def __init__(self, name, writer, limiter, universe: KalshiUniverse | None = None):
        super().__init__(name, writer, limiter)
        self.universe = universe or KalshiUniverse()

    async def targets(self, session):
        ts = await self.universe.tickers(session)
        return [(t, f"{KALSHI}/markets/{t}/orderbook") for t in ts]
