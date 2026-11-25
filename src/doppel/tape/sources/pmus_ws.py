"""Polymarket US markets WebSocket (book + trades) for the rolling crypto up/down windows.

Auth without DOPPEL holding a key: a signer daemon on the host owns the PM US market-data key and
listens on a Unix socket. Before EVERY (re)connect we send ``sign-market-ws\\n`` and read one line
``access_key\\ttimestamp_ms\\tsignature\\n``; the three values become the ``X-PM-*`` handshake
headers. They are secrets for our purposes: never written to the tape, logs, status.json or
exception text (``redact``), and never replayed to another host (redirects are refused).

Subscriptions (docs.polymarket.us/api-reference/websocket/markets.md + overview.md): one request
per (type, horizon, window start) covering the four assets, so each window can be dropped alone
with the documented ``{"unsubscribe": {"request_id": ...}}`` and a not-yet-listed next window
cannot fail the current window's request. Rollover runs every tick: the current and next window of
each horizon are subscribed as soon as they are due; a window is unsubscribed ``linger_s`` after it
ends. A request answered with ``error`` is retried 5 s later once its window has started; before the
open, at most 30 s later and no later than 3 s after the open.
A market-data request whose window has started but which has produced no message for some of its
slugs within ``nodata_s`` is unsubscribed and re-sent (ctl ``no_data``), with doubling delay up to
120 s; this covers a venue that accepts unlisted slugs silently. No timed reconnect: unsubscribe is
documented, so there is no deliberate gap.

Server ``{"heartbeat": {}}`` frames count as liveness for the stall watchdog but are not recorded.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

import orjson
import websockets

from ..universe import ASSETS, HORIZONS, pmus_slug, window_starts
from .base import WsSource

SIGNER_SOCK = "/run/pmus-riskd-market-signer/markets.sock"
PMUS_WS_URL = "wss://api.polymarket.us/v1/ws/markets"
SUB_TYPES = {"md": "SUBSCRIPTION_TYPE_MARKET_DATA", "tr": "SUBSCRIPTION_TYPE_TRADE"}
HEADER_NAMES = ("X-PM-Access-Key", "X-PM-Timestamp", "X-PM-Signature")
REDACTED = "<redacted>"


class SignerError(Exception):
    """Signer failure. The message never contains anything the signer returned."""


async def fetch_signed_headers(sock_path: str, timeout: float = 2.0) -> dict[str, str]:
    """Ask the host signer for fresh market-WS handshake headers (one request, one line)."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(sock_path), timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        raise SignerError(f"signer connect failed: {type(exc).__name__}") from None
    try:
        writer.write(b"sign-market-ws\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError) as exc:
        raise SignerError(f"signer read failed: {type(exc).__name__}") from None
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
    parts = line.rstrip(b"\r\n").split(b"\t")
    if not line.endswith(b"\n") or len(parts) != 3 or not all(parts) or not parts[1].isdigit():
        raise SignerError("signer reply malformed")
    try:
        vals = [p.decode("ascii") for p in parts]
    except UnicodeDecodeError:
        raise SignerError("signer reply not ascii") from None
    if any(not v.isprintable() for v in vals):
        raise SignerError("signer reply has control characters")
    return dict(zip(HEADER_NAMES, vals))


class NoRedirectConnect(websockets.connect):
    """``websockets.connect`` that never follows a redirect (the handshake carries auth)."""

    def process_redirect(self, exc: Exception) -> Exception | str:
        return exc


@dataclass
class _Sub:
    rid: str
    kind: str          # "md" | "tr"
    horizon: str
    start: int
    end: int
    slugs: tuple[str, ...]
    sent: float        # self.clock() at send
    seen: set[str] = field(default_factory=set)

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.kind, self.horizon, self.start)


class PmusMarketsWs(WsSource):
    url = PMUS_WS_URL
    stall_s = 30.0
    connect_cls = NoRedirectConnect

    def __init__(self, name, writer, socket_path: str = SIGNER_SOCK, assets=ASSETS,
                 horizons=("5m", "15m"), clock: Callable[[], float] = time.time,
                 linger_s: float = 30.0, nodata_s: float = 20.0, signer_timeout: float = 2.0):
        super().__init__(name, writer)
        self.socket_path = socket_path
        self.assets = tuple(assets)
        self.horizons = tuple(horizons)
        self.clock = clock
        self.linger_s = linger_s
        self.nodata_s = nodata_s
        self.signer_timeout = signer_timeout
        self.subs: dict[str, _Sub] = {}
        self._retry_at: dict[tuple[str, str, int], float] = {}
        self._nodata_n: dict[tuple[str, int], int] = {}
        self._n = 0
        self._secrets: deque[str] = deque(maxlen=12)  # last few header sets, for redaction
        for k in ("heartbeats", "sub_errors", "resubscribes", "subscribed"):
            self.stats.extra[k] = 0

    # -- auth ---------------------------------------------------------------------------------
    async def connect_headers(self) -> dict[str, str]:
        headers = await fetch_signed_headers(self.socket_path, self.signer_timeout)
        self._secrets.extend(headers.values())
        return headers

    def redact(self, text: str) -> str:
        for s in self._secrets:
            if s:
                text = text.replace(s, REDACTED)
        return text

    # -- frames -------------------------------------------------------------------------------
    def is_keepalive_reply(self, raw) -> bool:
        if len(raw) > 64:
            return False
        try:
            d = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return False
        if isinstance(d, dict) and len(d) == 1 and "heartbeat" in d:
            self.stats.extra["heartbeats"] += 1
            return True
        return False

    def on_message(self, raw) -> None:
        self.record(raw)
        try:
            d = orjson.loads(raw)
        except orjson.JSONDecodeError:
            return
        if isinstance(d, dict):
            self._track(d)

    def _track(self, d: dict) -> None:
        rid = d.get("requestId") or d.get("request_id")
        if "error" in d:
            self.stats.extra["sub_errors"] += 1
            sub = self.subs.pop(rid, None) if isinstance(rid, str) else None
            self.ctl("sub_error", rid=rid, error=str(d.get("error"))[:300])
            if sub is not None:
                now = self.clock()
                # before the open, retry just after it (PM US lists a window a few s after it opens)
                delay = 5.0 if now >= sub.start else min(30.0, max(1.0, sub.start - now + 3.0))
                self._retry_at[sub.key] = now + delay
            self._count()
            return
        kind = "md" if ("marketData" in d or "marketDataLite" in d) else "tr" if "trade" in d else None
        if kind is None:
            return
        body = d.get("marketData") or d.get("marketDataLite") or d.get("trade")
        slug = body.get("marketSlug") if isinstance(body, dict) else None
        if not slug:
            return
        for sub in self.subs.values():
            if sub.kind == kind and slug in sub.slugs:
                sub.seen.add(slug)

    # -- subscriptions ------------------------------------------------------------------------
    def expect_traffic(self) -> bool:
        return True  # heartbeats keep an idle but healthy socket alive

    async def on_open(self, ws) -> None:
        self.subs = {}
        self._retry_at = {}
        self._nodata_n = {}
        await self._sync(ws)

    async def on_tick(self, ws) -> None:
        await self._sync(ws)

    def _desired(self, now: float) -> list[tuple[str, int, int]]:
        out = []
        for h in self.horizons:
            L = HORIZONS[h]
            for st in window_starts(now, L, ahead=1):
                out.append((h, st, st + L))
        return out

    async def _subscribe(self, ws, kind: str, h: str, st: int, end: int) -> None:
        self._n += 1
        rid = f"{kind}-{h}-{st}-{self._n}"
        slugs = tuple(pmus_slug(a, h, st) for a in self.assets)
        msg = {"subscribe": {"requestId": rid, "subscriptionType": SUB_TYPES[kind],
                             "marketSlugs": list(slugs)}}
        await ws.send(orjson.dumps(msg).decode())
        self.subs[rid] = _Sub(rid, kind, h, st, end, slugs, self.clock())
        self._retry_at.pop((kind, h, st), None)
        self.ctl("subscribe", rid=rid, type=SUB_TYPES[kind], slugs=list(slugs))

    async def _unsubscribe(self, ws, sub: _Sub, why: str) -> None:
        self.subs.pop(sub.rid, None)
        await ws.send(orjson.dumps({"unsubscribe": {"request_id": sub.rid}}).decode())
        self.ctl("unsubscribe", rid=sub.rid, why=why)

    async def _sync(self, ws) -> None:
        now = self.clock()
        for sub in list(self.subs.values()):
            if now >= sub.end + self.linger_s:
                await self._unsubscribe(ws, sub, "expired")
                self._nodata_n.pop((sub.horizon, sub.start), None)
        # a started window whose book request is missing slugs: re-send the whole group
        for sub in list(self.subs.values()):
            if sub.kind != "md" or now < sub.start or now >= sub.end:
                continue
            missing = set(sub.slugs) - sub.seen
            g = (sub.horizon, sub.start)
            wait = min(self.nodata_s * 2 ** self._nodata_n.get(g, 0), 120.0)
            since = min(now - sub.sent, now - sub.start)  # since sent AND since start
            if missing and since >= wait:
                self._nodata_n[g] = self._nodata_n.get(g, 0) + 1
                self.stats.extra["resubscribes"] += 1
                self.ctl("no_data", rid=sub.rid, missing=sorted(missing), waited_s=round(since, 1))
                for other in [s for s in self.subs.values() if (s.horizon, s.start) == g]:
                    await self._unsubscribe(ws, other, "no_data")
        active = {s.key for s in self.subs.values()}
        for h, st, end in self._desired(now):
            for kind in SUB_TYPES:
                key = (kind, h, st)
                if key in active or self._retry_at.get(key, 0.0) > now:
                    continue
                await self._subscribe(ws, kind, h, st, end)
        self._count()

    def _count(self) -> None:
        self.stats.extra["subscribed"] = len(self.subs)
