"""Source base classes: a WebSocket stream with a message-gap watchdog, and a rate-limited REST poller.

Every source writes raw payloads to its SegmentWriter exactly as received. Control events
(connect, disconnect, stall, subscribe, error) are written to the same stream with ``raw=""`` and
``meta={"ctl": ...}``, so gaps are explicit in the tape. A gap can never pass for a quiet market.
"""

from __future__ import annotations

import asyncio
import logging
import random
import ssl
import time
from dataclasses import dataclass, field

import aiohttp
import certifi
import websockets

from ..segment import SegmentWriter

log = logging.getLogger("doppel.tape")
SSL_CTX = ssl.create_default_context(cafile=certifi.where())
UA = "doppel-tape/0.1 (research recorder)"


@dataclass
class SourceStats:
    msgs: int = 0
    reconnects: int = 0
    stalls: int = 0
    errors: int = 0
    connected: bool = False
    last_msg_tw: int | None = None
    last_error: str | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        now = time.time_ns()
        age = None if self.last_msg_tw is None else round((now - self.last_msg_tw) / 1e9, 3)
        return {"msgs": self.msgs, "reconnects": self.reconnects, "stalls": self.stalls,
                "errors": self.errors, "connected": self.connected, "last_msg_age_s": age,
                "last_error": self.last_error, **self.extra}


class Source:
    name: str

    def __init__(self, name: str, writer: SegmentWriter):
        self.name = name
        self.writer = writer
        self.stats = SourceStats()

    def ctl(self, event: str, **kw) -> None:
        self.writer.append("", meta={"ctl": event, **kw})

    def record(self, raw: str | bytes, meta: dict | None = None) -> None:
        tw = time.time_ns()
        self.writer.append(raw, tw=tw, meta=meta)
        self.stats.msgs += 1
        self.stats.last_msg_tw = tw

    async def run(self, stop: asyncio.Event) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class WsSource(Source):
    """Reconnecting WebSocket recorder.

    Subclasses set ``url`` and implement ``on_open`` (send subscriptions). The watchdog forces a
    reconnect when no message arrives for ``stall_s`` while ``expect_traffic()`` is true. That
    catches the silent stall the official RTDS client misses (it only pings).
    """

    url: str
    stall_s: float = 15.0
    keepalive_s: float = 5.0
    keepalive_msg: str | None = None  # app-level ping text, if the venue wants one
    # Connect factory; a subclass may swap in a ``websockets.connect`` subclass (e.g. one that
    # refuses redirects so handshake headers are never replayed to another host).
    connect_cls = websockets.connect

    def expect_traffic(self) -> bool:
        return True

    async def connect_headers(self) -> dict[str, str] | None:
        """Extra handshake headers, fetched fresh before EVERY (re)connect. None means no extra
        headers (the default). Values are treated as secrets: see ``redact``."""
        return None

    def redact(self, text: str) -> str:
        """Scrub secrets from text bound for the tape, logs or status.json. Default: identity."""
        return text

    def on_message(self, raw: str | bytes) -> None:
        """Handle one non-keepalive frame. Default: record it verbatim."""
        self.record(raw)

    async def on_open(self, ws) -> None:
        pass

    async def on_tick(self, ws) -> None:
        """Called roughly once a second while connected (for dynamic resubscription)."""

    def is_keepalive_reply(self, raw: str | bytes) -> bool:
        return raw in ("PONG", "pong", b"PONG", b"pong")

    async def run(self, stop: asyncio.Event) -> None:
        backoff = 0.5
        while not stop.is_set():
            started = time.monotonic()
            try:
                extra: dict = {}
                headers = await self.connect_headers()
                if headers is not None:
                    extra["additional_headers"] = headers
                async with self.connect_cls(self.url, ssl=SSL_CTX, max_size=None,
                                            open_timeout=15, ping_interval=20, ping_timeout=20,
                                            user_agent_header=UA, **extra) as ws:
                    self.stats.connected = True
                    self.ctl("connect", url=self.url)
                    await self.on_open(ws)
                    await self._loop(ws, stop)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - recorder must survive anything
                self.stats.errors += 1
                self.stats.last_error = self.redact(f"{type(exc).__name__}: {exc}")[:200]
                log.warning("%s error: %s", self.name, self.stats.last_error)
            finally:
                if self.stats.connected:
                    self.ctl("disconnect")
                self.stats.connected = False
            if stop.is_set():
                break
            self.stats.reconnects += 1
            if time.monotonic() - started > 60:
                backoff = 0.5
            await asyncio.sleep(backoff * (0.5 + random.random()))
            backoff = min(backoff * 2, 30.0)

    async def _loop(self, ws, stop: asyncio.Event) -> None:
        """Receive with ``async for`` (no per-message timer); a side task handles keepalive,
        dynamic subscription ticks, the stall watchdog and shutdown."""
        self._last_rx = time.monotonic()

        async def side() -> None:
            last_ka = time.monotonic()
            while True:
                await asyncio.sleep(1.0)
                now = time.monotonic()
                if stop.is_set():
                    await ws.close()
                    return
                if self.keepalive_msg and now - last_ka >= self.keepalive_s:
                    await ws.send(self.keepalive_msg)
                    last_ka = now
                try:
                    await self.on_tick(ws)
                except Exception as exc:  # noqa: BLE001 - never let a tick kill the watchdog
                    self.stats.errors += 1
                    self.stats.last_error = self.redact(f"tick: {type(exc).__name__}: {exc}")[:200]
                if self.expect_traffic() and now - self._last_rx > self.stall_s:
                    self.stats.stalls += 1
                    self.ctl("stall", silent_s=round(now - self._last_rx, 3))
                    log.warning("%s stalled %.1fs; reconnecting", self.name, now - self._last_rx)
                    await ws.close()
                    return

        task = asyncio.create_task(side())
        try:
            async for raw in ws:
                self._last_rx = time.monotonic()
                if not self.is_keepalive_reply(raw):
                    self.on_message(raw)
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


class RateLimiter:
    """Token bucket shared by pollers that hit one host."""

    def __init__(self, rate_per_s: float, burst: int = 1):
        self.rate = rate_per_s
        self.tokens = float(burst)
        self.burst = burst
        self.t = time.monotonic()
        self._lock = asyncio.Lock()

    async def take(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.burst, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class PollSource(Source):
    """Polls a dynamic set of URLs at ``interval_s`` each, under a shared rate limit.

    Each response is recorded with request send and receive clocks. REST timestamps from venues
    are not used as freshness evidence (a book ``timestamp`` can be the last-change time)."""

    interval_s: float = 1.0

    def __init__(self, name: str, writer: SegmentWriter, limiter: RateLimiter):
        super().__init__(name, writer)
        self.limiter = limiter

    async def targets(self, session: aiohttp.ClientSession) -> list[tuple[str, str]]:
        """Return (key, url) pairs to poll this cycle."""
        raise NotImplementedError

    async def run(self, stop: asyncio.Event) -> None:
        timeout = aiohttp.ClientTimeout(total=10)
        conn = aiohttp.TCPConnector(ssl=SSL_CTX, limit=8)
        async with aiohttp.ClientSession(timeout=timeout, connector=conn,
                                         headers={"user-agent": UA}) as session:
            self.stats.connected = True
            while not stop.is_set():
                cycle = time.monotonic()
                try:
                    tg = await self.targets(session)
                except Exception as exc:  # noqa: BLE001
                    self.stats.errors += 1
                    self.stats.last_error = f"targets: {type(exc).__name__}: {str(exc)[:200]}"
                    tg = []
                self.stats.extra["targets"] = len(tg)
                await asyncio.gather(*(self._poll(session, k, u) for k, u in tg))
                await asyncio.sleep(max(0.0, self.interval_s - (time.monotonic() - cycle)))
            self.stats.connected = False

    async def _poll(self, session: aiohttp.ClientSession, key: str, url: str) -> None:
        await self.limiter.take()
        sent_w, sent_m = time.time_ns(), time.monotonic_ns()
        try:
            async with session.get(url) as resp:
                body = await resp.text()
                status = resp.status
        except Exception as exc:  # noqa: BLE001
            self.stats.errors += 1
            self.stats.last_error = f"{key}: {type(exc).__name__}: {str(exc)[:160]}"
            self.ctl("poll_error", key=key, err=self.stats.last_error)
            return
        if status == 429:
            self.stats.extra["http_429"] = self.stats.extra.get("http_429", 0) + 1
            self.ctl("rate_limited", key=key)
            await asyncio.sleep(1.0)
            return
        self.record(body, meta={"key": key, "url": url, "status": status,
                                "sent_tw": sent_w, "sent_tm": sent_m})
