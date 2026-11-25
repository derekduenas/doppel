"""pmus_ws (signer-socket auth, rollover, redaction, no redirects) and ref_brti_live (Kalshi signing,
dedupe, missing credential). No network, no real keys."""

import asyncio
import base64
import logging
import os
import shutil
import tempfile
from pathlib import Path

import orjson
import pytest
import websockets
from aiohttp import web
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from websockets.datastructures import Headers
from websockets.http11 import Response

import doppel.tape.sources.base as base
from doppel.tape.segment import SegmentWriter, read_segment
from doppel.tape.sources.kalshi_brti import KalshiBrti, load_private_key, sign_request
from doppel.tape.sources.pmus_ws import (PmusMarketsWs, SignerError, fetch_signed_headers,
                                         HEADER_NAMES)
from doppel.tape.universe import pmus_slug

T0 = 1795622400  # 2026-11-25 16:00:00Z, a 15m (and 5m) boundary


# ------------------------------------------------------------------------------------------------
# helpers

@pytest.fixture
def short_dir(tmp_path):
    """AF_UNIX paths are capped near 104 bytes on macOS; pytest's tmp_path is often longer."""
    if len(str(tmp_path / "markets.sock")) < 100:
        yield tmp_path
        return
    d = tempfile.mkdtemp(prefix="dp", dir="/tmp")
    try:
        yield Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


class FakeSigner:
    """Unix-socket signer: answers each ``sign-market-ws`` request with a new header triple."""

    def __init__(self, path: Path):
        self.path = str(path)
        self.issued: list[tuple[str, str, str]] = []
        self.requests: list[bytes] = []
        self.server: asyncio.AbstractServer | None = None

    async def _handle(self, reader, writer):
        line = await reader.readline()
        self.requests.append(line)
        n = len(self.issued) + 1
        trip = (f"AKEYsecret{n:04d}", str(1795622400000 + n), f"SIGsecret{n:04d}+/=")
        self.issued.append(trip)
        writer.write(("\t".join(trip) + "\n").encode())
        await writer.drain()
        writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_unix_server(self._handle, path=self.path)
        return self

    async def __aexit__(self, *exc):
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    def secrets(self) -> list[str]:
        return [v for trip in self.issued for v in (trip[0], trip[2])]


def tape_text(root: Path, stream: str) -> str:
    out = []
    for f in sorted(root.glob(f"{stream}/*/*.dtape.zst")):
        for r in read_segment(f):
            out.append(orjson.dumps(r).decode())
    return "\n".join(out)


def tape_records(root: Path, stream: str) -> list[dict]:
    return [r for f in sorted(root.glob(f"{stream}/*/*.dtape.zst")) for r in read_segment(f)]


class FakeWs:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, s: str):
        self.sent.append(orjson.loads(s))


# ------------------------------------------------------------------------------------------------
# signer protocol

async def test_signer_protocol_and_errors(short_dir):
    async with FakeSigner(short_dir / "m.sock") as fs:
        h = await fetch_signed_headers(fs.path)
        assert fs.requests == [b"sign-market-ws\n"]
        assert list(h) == list(HEADER_NAMES)
        assert h["X-PM-Access-Key"] == fs.issued[0][0] and h["X-PM-Signature"] == fs.issued[0][2]
    with pytest.raises(SignerError) as ei:
        await fetch_signed_headers(str(short_dir / "missing.sock"))
    assert "FileNotFoundError" in str(ei.value) or "ConnectionRefused" in str(ei.value)

    async def bad(reader, writer):  # malformed reply: values must not leak into the error
        await reader.readline()
        writer.write(b"AKEYleak\tnotdigits\tSIGleak\n")
        await writer.drain()
        writer.close()
    srv = await asyncio.start_unix_server(bad, path=str(short_dir / "b.sock"))
    try:
        with pytest.raises(SignerError) as ei:
            await fetch_signed_headers(str(short_dir / "b.sock"))
        assert "leak" not in str(ei.value)
    finally:
        srv.close()
        await srv.wait_closed()

    async def silent(reader, writer):  # never answers -> 2 s style timeout (shortened)
        await reader.read()  # until the client gives up and closes
        writer.close()
    srv = await asyncio.start_unix_server(silent, path=str(short_dir / "s.sock"))
    try:
        with pytest.raises(SignerError, match="TimeoutError"):
            await fetch_signed_headers(str(short_dir / "s.sock"), timeout=0.3)
    finally:
        srv.close()
        await srv.wait_closed()


# ------------------------------------------------------------------------------------------------
# end-to-end against a local websockets server

async def test_pmus_ws_headers_subs_heartbeats_reconnect(tmp_path, short_dir, monkeypatch):
    monkeypatch.setattr(base, "SSL_CTX", None)
    seen_headers: list[dict] = []
    received: list[list[dict]] = []

    async def handler(ws):
        seen_headers.append({k: ws.request.headers.get(k) for k in HEADER_NAMES})
        msgs: list[dict] = []
        received.append(msgs)
        for _ in range(4):  # 2 horizons x (md, tr) for the current window
            msgs.append(orjson.loads(await ws.recv()))
        await ws.send('{"heartbeat": {}}')
        slug = msgs[0]["subscribe"]["marketSlugs"][0]
        await ws.send(orjson.dumps({"requestId": msgs[0]["subscribe"]["requestId"],
                                    "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                                    "marketData": {"marketSlug": slug, "bids": []}}).decode())
        await ws.send('{"heartbeat": {}}')
        if len(seen_headers) == 1:
            await asyncio.sleep(0.2)
            await ws.close()  # force a reconnect
            return
        await ws.wait_closed()

    async with FakeSigner(short_dir / "m.sock") as fs, \
            websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        src = PmusMarketsWs("pmus_ws", SegmentWriter(tmp_path, "pmus_ws"), socket_path=fs.path,
                            horizons=("5m", "15m"), clock=lambda: T0 + 10)
        src.url = f"ws://127.0.0.1:{port}"
        # only subscribe the current window so the server's 4 recv()s are exactly the current subs
        monkeypatch.setattr(src, "_desired", lambda now: [("5m", T0, T0 + 300),
                                                          ("15m", T0, T0 + 900)])
        stop = asyncio.Event()
        task = asyncio.create_task(src.run(stop))
        for _ in range(60):
            await asyncio.sleep(0.1)
            if len(seen_headers) >= 2 and src.stats.extra["heartbeats"] >= 4:
                break
        stop.set()
        await asyncio.wait_for(task, 5)
        src.writer.close()

    assert len(seen_headers) >= 2
    # every connect carried the three headers, freshly signed each time
    for i, h in enumerate(seen_headers[:2]):
        assert (h["X-PM-Access-Key"], h["X-PM-Timestamp"], h["X-PM-Signature"]) == fs.issued[i]
    assert fs.issued[0] != fs.issued[1]
    subs = [m["subscribe"] for m in received[0]]
    assert {s["subscriptionType"] for s in subs} == {"SUBSCRIPTION_TYPE_MARKET_DATA",
                                                     "SUBSCRIPTION_TYPE_TRADE"}
    md5 = next(s for s in subs if s["subscriptionType"].endswith("DATA") and "-5m-" in s["requestId"])
    assert md5["marketSlugs"] == [pmus_slug(a, "5m", T0) for a in ("btc", "eth", "sol", "xrp")]
    recs = tape_records(tmp_path, "pmus_ws")
    data = [r for r in recs if r["raw"]]
    assert data and all("heartbeat" not in r["raw"] for r in data)
    assert any("marketData" in r["raw"] for r in data)
    assert src.stats.extra["heartbeats"] >= 2
    ctls = [r["meta"]["ctl"] for r in recs if r.get("meta")]
    assert ctls.count("connect") >= 2 and "subscribe" in ctls
    blob = tape_text(tmp_path, "pmus_ws")
    for s in fs.secrets():
        assert s not in blob


async def test_pmus_ws_redacts_on_failure(tmp_path, short_dir, monkeypatch, caplog):
    """A connect failure that carries the header values must not leak them anywhere."""
    monkeypatch.setattr(base, "SSL_CTX", None)

    class Leaky:
        def __init__(self, url, **kw):
            raise RuntimeError(f"handshake to {url} failed with headers {kw['additional_headers']}")

    caplog.set_level(logging.WARNING, logger="doppel.tape")
    async with FakeSigner(short_dir / "m.sock") as fs:
        src = PmusMarketsWs("pmus_ws", SegmentWriter(tmp_path, "pmus_ws"), socket_path=fs.path)
        src.connect_cls = Leaky  # type: ignore[assignment]
        stop = asyncio.Event()
        task = asyncio.create_task(src.run(stop))
        await asyncio.sleep(0.8)
        stop.set()
        await asyncio.wait_for(task, 5)
        src.writer.close()
    assert src.stats.errors >= 1 and "RuntimeError" in (src.stats.last_error or "")
    assert "<redacted>" in (src.stats.last_error or "")
    sinks = (tape_text(tmp_path, "pmus_ws") + caplog.text
             + orjson.dumps(src.stats.as_dict()).decode())
    assert fs.secrets()
    for s in fs.secrets():
        assert s not in sinks


async def test_pmus_ws_real_401_and_no_redirect(tmp_path, short_dir, monkeypatch, caplog):
    monkeypatch.setattr(base, "SSL_CTX", None)
    caplog.set_level(logging.WARNING, logger="doppel.tape")
    other_hits: list[dict] = []

    async def other(ws):
        other_hits.append(dict(ws.request.headers))
        await ws.wait_closed()

    def other_req(conn, request):
        other_hits.append(dict(request.headers))
        return None

    mode = {"status": 302}

    def reject(conn, request):
        if mode["status"] == 302:
            return Response(302, "Found", Headers({"Location": other_url, "Content-Length": "0"}))
        return conn.respond(401, "unauthorized\n")

    async with FakeSigner(short_dir / "m.sock") as fs, \
            websockets.serve(other, "127.0.0.1", 0, process_request=other_req) as s2, \
            websockets.serve(other, "127.0.0.1", 0, process_request=reject) as s1:
        other_url = f"ws://127.0.0.1:{s2.sockets[0].getsockname()[1]}/elsewhere"
        src = PmusMarketsWs("pmus_ws", SegmentWriter(tmp_path, "pmus_ws"), socket_path=fs.path)
        src.url = f"ws://127.0.0.1:{s1.sockets[0].getsockname()[1]}"
        stop = asyncio.Event()
        task = asyncio.create_task(src.run(stop))

        async def until(code: str) -> None:
            for _ in range(100):
                if code in (src.stats.last_error or ""):
                    return
                await asyncio.sleep(0.05)
        await until("302")
        assert src.stats.errors >= 1 and "302" in (src.stats.last_error or "")
        mode["status"] = 401
        await until("401")
        stop.set()
        await asyncio.wait_for(task, 5)
        src.writer.close()
    assert other_hits == []  # the redirect target never saw a handshake (or our headers)
    assert "401" in (src.stats.last_error or "")
    assert len(fs.issued) >= 2  # fresh headers per attempt
    sinks = (tape_text(tmp_path, "pmus_ws") + caplog.text
             + orjson.dumps(src.stats.as_dict()).decode())
    for s in fs.secrets():
        assert s not in sinks


# ------------------------------------------------------------------------------------------------
# rollover with an injected clock

async def test_pmus_ws_rollover(tmp_path):
    now = [T0 + 10.0]
    src = PmusMarketsWs("pmus_ws", SegmentWriter(tmp_path, "pmus_ws"), socket_path="/nonexistent",
                        horizons=("5m",), assets=("btc", "eth"), clock=lambda: now[0],
                        linger_s=30.0, nodata_s=1e9)
    ws = FakeWs()
    await src.on_open(ws)
    subs = [m["subscribe"] for m in ws.sent]
    # current and next 5m window, market data and trades each
    assert {(s["subscriptionType"][18:], tuple(s["marketSlugs"])) for s in subs} == {
        (t, (pmus_slug("btc", "5m", st), pmus_slug("eth", "5m", st)))
        for t in ("MARKET_DATA", "TRADE") for st in (T0, T0 + 300)}
    first_rids = {s["requestId"] for s in subs if pmus_slug("btc", "5m", T0) in s["marketSlugs"]}

    ws.sent.clear()
    await src.on_tick(ws)
    assert ws.sent == []  # nothing new mid-window

    now[0] = T0 + 301  # next window is now current; the one after comes due
    await src.on_tick(ws)
    new = [m["subscribe"] for m in ws.sent if "subscribe" in m]
    assert {tuple(s["marketSlugs"]) for s in new} == {
        (pmus_slug("btc", "5m", T0 + 600), pmus_slug("eth", "5m", T0 + 600))}
    assert len(new) == 2 and not any("unsubscribe" in m for m in ws.sent)  # still lingering

    ws.sent.clear()
    now[0] = T0 + 300 + 31  # linger over: the old window is dropped by request id
    await src.on_tick(ws)
    unsubs = {m["unsubscribe"]["request_id"] for m in ws.sent if "unsubscribe" in m}
    assert unsubs == first_rids
    assert not any("subscribe" in m for m in ws.sent)
    assert src.stats.extra["subscribed"] == 4


async def test_pmus_ws_error_retry_and_nodata(tmp_path):
    now = [T0 + 10.0]
    src = PmusMarketsWs("pmus_ws", SegmentWriter(tmp_path, "pmus_ws"), socket_path="/nonexistent",
                        horizons=("5m",), assets=("btc",), clock=lambda: now[0], nodata_s=0.0)
    ws = FakeWs()
    await src.on_open(ws)
    nxt = next(m["subscribe"]["requestId"] for m in ws.sent
               if m["subscribe"]["marketSlugs"] == [pmus_slug("btc", "5m", T0 + 300)]
               and m["subscribe"]["subscriptionType"].endswith("DATA"))
    # next window not listed yet: error reply drops the request and schedules a retry (30 s)
    src.on_message(orjson.dumps({"request_id": nxt, "error": "market not found"}).decode())
    assert nxt not in src.subs and src.stats.extra["sub_errors"] == 1
    ws.sent.clear()
    cur_md = next(s for s in src.subs.values() if s.kind == "md" and s.start == T0)
    src.on_message(orjson.dumps({"requestId": cur_md.rid, "marketData":
                                 {"marketSlug": pmus_slug("btc", "5m", T0)}}).decode())
    await src.on_tick(ws)
    assert ws.sent == []  # retry not due; current window has data, so no no_data resubscribe
    src._retry_at = {k: 0.0 for k in src._retry_at}
    await src.on_tick(ws)
    assert [m["subscribe"]["marketSlugs"] for m in ws.sent] == [[pmus_slug("btc", "5m", T0 + 300)]]

    # a started window whose book request never produced data is re-sent (both types)
    now[0] = T0 + 300 + 5
    ws.sent.clear()
    await src.on_tick(ws)
    kinds = [next(iter(m)) for m in ws.sent]
    assert kinds.count("unsubscribe") == 2 and kinds.count("subscribe") >= 2
    assert src.stats.extra["resubscribes"] == 1


# ------------------------------------------------------------------------------------------------
# Kalshi BRTI

PATH = "/trade-api/v2/cfbenchmarks/values"


def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def test_kalshi_sign_rsa_pss():
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = load_private_key(_pem(k))
    sig = base64.b64decode(sign_request(key, "1795622400123", "GET", PATH))
    k.public_key().verify(sig, f"1795622400123GET{PATH}".encode(),
                          padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
                          hashes.SHA256())


def test_kalshi_sign_ed25519():
    k = ed25519.Ed25519PrivateKey.generate()
    key = load_private_key(_pem(k))
    sig = base64.b64decode(sign_request(key, "1795622400123", "GET", PATH))
    k.public_key().verify(sig, f"1795622400123GET{PATH}".encode())


def test_kalshi_extract_new_dedupes(tmp_path):
    src = KalshiBrti("ref_brti_live", SegmentWriter(tmp_path, "ref_brti_live"), seen_cap=3)
    b1 = orjson.dumps({"data": {"payload": [{"time": 2000, "value": "59519.22"},
                                            {"time": 1000, "value": "59518.00"}],
                                "serverTime": 2100}})
    assert src.extract_new(b1) == [[1000, "59518.00"], [2000, "59519.22"]]
    b2 = orjson.dumps({"payload": [{"time": 2000, "value": "59519.22"},
                                   {"time": 3000, "value": "59520.10"}]})  # unwrapped shape
    assert src.extract_new(b2) == [[3000, "59520.10"]]
    assert src.extract_new(b2) == []
    assert src.extract_new(b"not json") == []
    assert len(src._seen) == 3
    src.extract_new(orjson.dumps({"payload": [{"time": 4000, "value": "1"}]}))
    assert len(src._seen) == 3 and (1000, "59518.00") not in src._seen  # bounded, oldest evicted


async def test_kalshi_missing_credentials_idles(tmp_path, monkeypatch, caplog):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    caplog.set_level(logging.WARNING, logger="doppel.tape")
    src = KalshiBrti("ref_brti_live", SegmentWriter(tmp_path, "ref_brti_live"))
    stop = asyncio.Event()
    task = asyncio.create_task(src.run(stop))
    await asyncio.sleep(0.2)
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, 2)
    src.writer.close()
    assert src.stats.extra["idle"] == "no_credentials" and src.stats.errors == 0
    assert caplog.text.count("no credential file") == 1
    # a garbage profile file also idles, and nothing from the file is logged
    p = tmp_path / "kalshi-profile.json"
    p.write_text('{"key_id": "KIDsecret", "private_key_pem": "-----BEGIN junk PEMsecret"}')
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    src2 = KalshiBrti("ref_brti_live", SegmentWriter(tmp_path, "ref_brti_live"))
    stop2 = asyncio.Event()
    task2 = asyncio.create_task(src2.run(stop2))
    await asyncio.sleep(0.1)
    stop2.set()
    await asyncio.wait_for(task2, 2)
    src2.writer.close()
    assert src2.stats.extra["idle"] == "bad_credentials"
    assert "secret" not in caplog.text and "secret" not in tape_text(tmp_path, "ref_brti_live")


@pytest.mark.parametrize("kind", ["rsa", "ed25519"])
async def test_kalshi_brti_polls_signed(tmp_path, monkeypatch, kind):
    k = (rsa.generate_private_key(public_exponent=65537, key_size=2048) if kind == "rsa"
         else ed25519.Ed25519PrivateKey.generate())
    cred_dir = tmp_path / "creds"
    cred_dir.mkdir()
    (cred_dir / "kalshi-profile.json").write_bytes(
        orjson.dumps({"key_id": "kid-test-123", "private_key_pem": _pem(k)}))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(cred_dir))
    hits: list[dict] = []
    n = [0]

    async def values(request: web.Request):
        h = request.headers
        ts, sig = h["KALSHI-ACCESS-TIMESTAMP"], base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
        msg = f"{ts}GET{request.path}".encode()
        if kind == "rsa":
            k.public_key().verify(sig, msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),  # type: ignore[union-attr,call-arg]
                                                        salt_length=32), hashes.SHA256())
        else:
            k.public_key().verify(sig, msg)  # type: ignore[call-arg]
        hits.append({"key": h["KALSHI-ACCESS-KEY"], "id": request.query.get("id"),
                     "path": request.path})
        n[0] += 1
        t = 1795622400000 + n[0] * 1000
        return web.json_response({"data": {"payload": [{"time": x, "value": f"{x // 1000}.25"}
                                                       for x in (t - 1000, t)],
                                           "serverTime": t}})

    app = web.Application()
    app.router.add_get(PATH, values)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        src = KalshiBrti("ref_brti_live", SegmentWriter(tmp_path, "ref_brti_live"),
                         url=f"http://127.0.0.1:{port}{PATH}?id=BRTI")
        stop = asyncio.Event()
        task = asyncio.create_task(src.run(stop))
        await asyncio.sleep(1.3)
        stop.set()
        await asyncio.wait_for(task, 5)
        src.writer.close()
    finally:
        await runner.cleanup()
    assert len(hits) >= 2 and src.stats.errors == 0
    assert all(x == {"key": "kid-test-123", "id": "BRTI", "path": PATH} for x in hits)
    recs = [r for r in tape_records(tmp_path, "ref_brti_live") if r["raw"]]
    assert len(recs) == len(hits)
    assert set(recs[0]["meta"]) == {"status", "sent_tw", "sent_tm", "new"}
    assert recs[0]["meta"]["status"] == 200
    news = [tuple(p) for r in recs for p in r["meta"]["new"]]
    assert len(news) == len(set(news))  # each (time, value) reported once across polls
    assert len(recs[1]["meta"]["new"]) == 1
    blob = tape_text(tmp_path, "ref_brti_live")
    assert "KALSHI-ACCESS" not in blob and "kid-test-123" not in blob
    assert os.environ["CREDENTIALS_DIRECTORY"] == str(cred_dir)


async def test_pmus_ws_preopen_error_retries_right_after_open(tmp_path):
    """Next window errors 1 s before its open (not listed yet): the retry lands ~3 s after the open,
    not 30 s into the window."""
    now = [T0 + 299.0]
    src = PmusMarketsWs("pmus_ws", SegmentWriter(tmp_path, "pmus_ws"), socket_path="/nonexistent",
                        horizons=("5m",), assets=("btc",), clock=lambda: now[0], nodata_s=1e9)
    ws = FakeWs()
    await src.on_open(ws)
    nxt = [s for s in src.subs.values() if s.start == T0 + 300]
    assert len(nxt) == 2
    for sub in nxt:
        src.on_message(orjson.dumps({"request_id": sub.rid, "error": "not found"}).decode())
    target = [pmus_slug("btc", "5m", T0 + 300)]

    def retried() -> int:
        return sum(1 for m in ws.sent if m.get("subscribe", {}).get("marketSlugs") == target)
    ws.sent.clear()
    now[0] = T0 + 301.0
    await src.on_tick(ws)
    assert retried() == 0  # (the window after it comes due here and is subscribed normally)
    now[0] = T0 + 303.5  # retry was set for open + 3 s
    await src.on_tick(ws)
    assert retried() == 2  # book + trades, just after the open (not 30 s in)
