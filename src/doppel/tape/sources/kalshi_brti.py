"""Kalshi's CF Benchmarks BRTI values, polled with a read-only API profile (our own reference feed).

The profile JSON ``{"key_id": ..., "private_key_pem": ...}`` is handed to the service by systemd
``LoadCredential=kalshi-profile.json:...`` and read from ``$CREDENTIALS_DIRECTORY`` at start. It is
loaded in ``run`` (never in ``__init__``), so the recorder builds without it; a missing or unusable
profile is logged once and the source idles. Auth headers are never recorded, and exceptions on the
signed path are reduced to their type name (no ``str(exc)``).

Every ``interval_s`` (0.5 s): GET ``/trade-api/v2/cfbenchmarks/values?id=BRTI``, signed over
``timestamp_ms + "GET" + path`` (path without the query). The raw body is recorded with
``meta = {status, sent_tw, sent_tm, new}``; ``new`` lists the ``[time_ms, value]`` pairs not seen
before (bounded memory).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import orjson
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from .base import SSL_CTX, UA, Source

log = logging.getLogger("doppel.tape")
BRTI_URL = "https://external-api.kalshi.com/trade-api/v2/cfbenchmarks/values?id=BRTI"
CRED_NAME = "kalshi-profile.json"
SigningKey = Ed25519PrivateKey | RSAPrivateKey


def load_private_key(pem: str) -> SigningKey:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if isinstance(key, (Ed25519PrivateKey, RSAPrivateKey)):
        return key
    raise TypeError("unsupported key type")


def sign_request(key: SigningKey, ts_ms: str, method: str, path: str) -> str:
    """Kalshi request signature: base64(sign(timestamp_ms + METHOD + path))."""
    msg = f"{ts_ms}{method}{path}".encode()
    if isinstance(key, Ed25519PrivateKey):
        sig = key.sign(msg)
    else:
        sig = key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                       hashes.SHA256())
    return base64.b64encode(sig).decode()


def default_profile_path() -> Path | None:
    d = os.environ.get("CREDENTIALS_DIRECTORY")
    return Path(d) / CRED_NAME if d else None


class KalshiBrti(Source):
    interval_s = 5.0  # shared Kalshi key with PERELMAN/btc-window; each response carries ~120 s of values

    def __init__(self, name, writer, profile_path: str | Path | None = None, url: str = BRTI_URL,
                 seen_cap: int = 20000):
        super().__init__(name, writer)
        self.profile_path = Path(profile_path) if profile_path is not None else None
        self.url = url
        self.path = urlsplit(url).path
        self.seen_cap = seen_cap
        self._seen: dict[tuple[int, str], None] = {}  # insertion-ordered, oldest evicted first

    # -- credentials --------------------------------------------------------------------------
    def _load(self) -> tuple[str, SigningKey] | None:
        p = self.profile_path or default_profile_path()
        if p is None or not p.is_file():
            log.warning("%s: no credential file (%s); idling", self.name,
                        "CREDENTIALS_DIRECTORY unset" if p is None else p)
            self.ctl("idle", why="no_credentials")
            self.stats.extra["idle"] = "no_credentials"
            return None
        try:
            prof = orjson.loads(p.read_bytes())
            key_id = prof["key_id"]
            key = load_private_key(prof["private_key_pem"])
            if not isinstance(key_id, str) or not key_id:
                raise ValueError("key_id")
        except Exception as exc:  # noqa: BLE001 - never str() anything near key material
            log.warning("%s: credential file unusable (%s); idling", self.name, type(exc).__name__)
            self.ctl("idle", why="bad_credentials", err=type(exc).__name__)
            self.stats.extra["idle"] = "bad_credentials"
            return None
        return key_id, key

    # -- parsing ------------------------------------------------------------------------------
    def extract_new(self, body: str | bytes) -> list[list]:
        """``[time_ms, value]`` pairs in this body not seen in any earlier body, time-ascending."""
        try:
            d = orjson.loads(body)
        except orjson.JSONDecodeError:
            return []
        if isinstance(d, dict) and isinstance(d.get("data"), dict):
            d = d["data"]
        payload = d.get("payload") if isinstance(d, dict) else None
        if not isinstance(payload, list):
            return []
        pairs: list[tuple[int, str]] = []
        for p in payload:
            try:
                pairs.append((int(p["time"]), str(p["value"])))
            except (KeyError, TypeError, ValueError):
                continue
        new: list[tuple[int, str]] = []
        for k in sorted(pairs):  # insert time-ascending so eviction drops the oldest times
            if k not in self._seen:
                self._seen[k] = None
                new.append(k)
        while len(self._seen) > self.seen_cap:
            del self._seen[next(iter(self._seen))]
        return [[t, v] for t, v in new]

    # -- loop ---------------------------------------------------------------------------------
    async def run(self, stop: asyncio.Event) -> None:
        cred = self._load()
        if cred is None:
            await stop.wait()
            return
        key_id, key = cred
        timeout = aiohttp.ClientTimeout(total=5)
        conn = aiohttp.TCPConnector(ssl=SSL_CTX, limit=2)
        async with aiohttp.ClientSession(timeout=timeout, connector=conn,
                                         headers={"user-agent": UA}) as session:
            self.stats.connected = True
            try:
                while not stop.is_set():
                    cycle = time.monotonic()
                    await self._poll(session, key_id, key)
                    await asyncio.sleep(max(0.0, self.interval_s - (time.monotonic() - cycle)))
            finally:
                self.stats.connected = False

    async def _poll(self, session: aiohttp.ClientSession, key_id: str, key: SigningKey) -> None:
        sent_w, sent_m = time.time_ns(), time.monotonic_ns()
        ts = str(sent_w // 1_000_000)
        try:
            headers = {"KALSHI-ACCESS-KEY": key_id, "KALSHI-ACCESS-TIMESTAMP": ts,
                       "KALSHI-ACCESS-SIGNATURE": sign_request(key, ts, "GET", self.path)}
            async with session.get(self.url, headers=headers, allow_redirects=False) as resp:
                body = await resp.text()
                status = resp.status
                retry_after = resp.headers.get("Retry-After")
        except Exception as exc:  # noqa: BLE001 - type name only on the signed path
            self.stats.errors += 1
            self.stats.last_error = f"poll: {type(exc).__name__}"
            self.ctl("poll_error", err=type(exc).__name__)
            return
        new = self.extract_new(body) if status == 200 else []
        self.record(body, meta={"status": status, "sent_tw": sent_w, "sent_tm": sent_m,
                                "new": new})
        if status != 200:
            self.stats.extra[f"http_{status}"] = self.stats.extra.get(f"http_{status}", 0) + 1
        if status == 429:
            # Shared key (PERELMAN, btc-window): back off so we never starve them. No data is lost,
            # each successful response carries ~120 s of history.
            try:
                wait = min(30.0, max(2.0, float(retry_after))) if retry_after else 10.0
            except ValueError:
                wait = 10.0
            self.ctl("rate_limited", wait_s=wait)
            await asyncio.sleep(wait)
