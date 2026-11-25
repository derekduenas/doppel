"""The M0 recording universe: short-dated crypto up/down windows on three venues.

Window conventions, checked live 2026-11-25:
- intl event slug ``{asset}-updown-{5m|15m}-{start_unix}``; tokens come from Gamma ``clobTokenIds``.
- PM US market slug keyed by window START (UTC): ``cpc-btc-updown-{h}-{YYYY-MM-DD-HHMM}z`` for BTC but
  ``cpc{asset}-{asset}-updown-{h}-...`` for eth/sol/xrp (verified via /v1/search). A window is listed
  a few seconds after it opens; early 404s are recorded as such.
- Kalshi: open markets of series such as KXBTC15M, listed through the public REST API.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import aiohttp
import orjson

ASSETS = ("btc", "eth", "sol", "xrp")
HORIZONS = {"5m": 300, "15m": 900}
GAMMA = "https://gamma-api.polymarket.com"
PMUS = "https://gateway.polymarket.us/v1"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M")


def window_starts(now_s: float, length_s: int, ahead: int = 1) -> list[int]:
    """Start times of the current window plus ``ahead`` upcoming windows."""
    cur = int(now_s // length_s * length_s)
    return [cur + i * length_s for i in range(ahead + 1)]


def intl_slug(asset: str, horizon: str, start: int) -> str:
    return f"{asset}-updown-{horizon}-{start}"


PMUS_PREFIX = {"btc": "cpc-btc", "eth": "cpceth-eth", "sol": "cpcsol-sol", "xrp": "cpcxrp-xrp"}


def pmus_slug(asset: str, horizon: str, start: int) -> str:
    t = time.gmtime(start)
    return f"{PMUS_PREFIX[asset]}-updown-{horizon}-{time.strftime('%Y-%m-%d-%H%M', t)}z"


@dataclass
class IntlWindow:
    slug: str
    start: int
    end: int
    tokens: tuple[str, ...]


class IntlUniverse:
    """Resolves intl window slugs to CLOB token ids via Gamma, with caching and negative caching."""

    def __init__(self, assets=ASSETS, horizons=("5m", "15m"), ahead: int = 1):
        self.assets = assets
        self.horizons = horizons
        self.ahead = ahead
        self._cache: dict[str, IntlWindow | None] = {}
        self._miss_at: dict[str, float] = {}
        self.lookups = 0
        self.misses = 0

    async def _resolve(self, session: aiohttp.ClientSession, slug: str, start: int, length: int):
        if slug in self._cache and self._cache[slug] is not None:
            return self._cache[slug]
        if time.monotonic() - self._miss_at.get(slug, -1e9) < 20:
            return None
        self.lookups += 1
        async with session.get(f"{GAMMA}/events", params={"slug": slug}) as r:
            data = orjson.loads(await r.read()) if r.status == 200 else []
        if not data or not data[0].get("markets"):
            self.misses += 1
            self._miss_at[slug] = time.monotonic()
            return None
        toks = tuple(orjson.loads(data[0]["markets"][0]["clobTokenIds"]))
        w = IntlWindow(slug, start, start + length, toks)
        self._cache[slug] = w
        return w

    async def current(self, session: aiohttp.ClientSession, now_s: float | None = None):
        now_s = time.time() if now_s is None else now_s
        jobs = []
        for h in self.horizons:
            L = HORIZONS[h]
            for st in window_starts(now_s, L, self.ahead):
                for a in self.assets:
                    jobs.append(self._resolve(session, intl_slug(a, h, st), st, L))
        res = await asyncio.gather(*jobs, return_exceptions=True)
        out = [w for w in res if isinstance(w, IntlWindow) and w.end > now_s]
        # drop expired cache entries
        for k in [k for k, v in self._cache.items() if v is not None and v.end < now_s - 60]:
            del self._cache[k]
        return out


def pmus_targets(now_s: float | None = None, assets=ASSETS, horizons=("5m", "15m")):
    now_s = time.time() if now_s is None else now_s
    out = []
    for h in horizons:
        L = HORIZONS[h]
        st = int(now_s // L * L)
        for a in assets:
            s = pmus_slug(a, h, st)
            out.append((s, f"{PMUS}/markets/{s}/book"))
    return out


class KalshiUniverse:
    def __init__(self, series=KALSHI_SERIES, refresh_s: float = 20.0):
        self.series = series
        self.refresh_s = refresh_s
        self._tickers: list[str] = []
        self._at = -1e9

    async def tickers(self, session: aiohttp.ClientSession) -> list[str]:
        if time.monotonic() - self._at < self.refresh_s:
            return self._tickers
        found: list[str] = []
        for s in self.series:
            async with session.get(f"{KALSHI}/markets",
                                   params={"series_ticker": s, "status": "open", "limit": 20}) as r:
                if r.status != 200:
                    continue
                data = orjson.loads(await r.read())
            mk = sorted(data.get("markets", []), key=lambda m: m.get("close_time", ""))
            found += [m["ticker"] for m in mk[:2]]
        self._tickers, self._at = found, time.monotonic()
        return found
