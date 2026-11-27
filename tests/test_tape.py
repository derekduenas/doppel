import asyncio
import time
import zlib

import orjson
import pytest
import websockets

from doppel.tape.receipts import build_receipt, rtds_tx_set
from doppel.tape.segment import SegmentWriter, read_segment, segment_path
from doppel.tape.sources.base import RateLimiter, WsSource
from doppel.tape.universe import intl_slug, pmus_slug, pmus_targets, window_starts

H = 3600 * 10**9
T0 = 1795622400 * 10**9  # 2026-11-25 16:00:00Z


def test_roundtrip_and_crc(tmp_path):
    w = SegmentWriter(tmp_path, "s")
    for i in range(5):
        w.append(f"msg{i}", tw=T0 + i)
    w.close()
    recs = list(read_segment(segment_path(tmp_path, "s", T0)))
    assert [r["raw"] for r in recs] == [f"msg{i}" for i in range(5)]
    assert [r["seq"] for r in recs] == [1, 2, 3, 4, 5]
    assert all(r["crc"] == zlib.crc32(r["raw"].encode()) for r in recs)
    man = orjson.loads((tmp_path / "s" / "manifest.jsonl").read_bytes().splitlines()[0])
    assert man["records"] == 5 and man["last_seq"] == 5


def test_hour_rotation_and_seq_resume(tmp_path):
    w = SegmentWriter(tmp_path, "s")
    w.append("a", tw=T0)
    w.append("b", tw=T0 + H)  # next hour -> new file, first one manifested
    w.close()
    assert segment_path(tmp_path, "s", T0) != segment_path(tmp_path, "s", T0 + H)
    w2 = SegmentWriter(tmp_path, "s")  # restart continues sequence numbers
    assert w2.append("c", tw=T0 + H + 1) == 3
    w2.close()
    lines = (tmp_path / "s" / "manifest.jsonl").read_bytes().splitlines()
    assert len(lines) == 3


def test_truncated_tail_frame_is_tolerated(tmp_path):
    w = SegmentWriter(tmp_path, "s")
    w.append("one", tw=T0)
    w.flush()
    w.append("two", tw=T0 + 1)
    w.flush()
    w.close()
    p = segment_path(tmp_path, "s", T0)
    data = p.read_bytes()
    p.write_bytes(data[:-5])  # simulate crash mid-frame
    assert [r["raw"] for r in read_segment(p)] == ["one"]
    with pytest.raises(ValueError, match="truncated zstd"):
        list(read_segment(p, strict=True))
    assert build_receipt(tmp_path, "20261125", 16)["streams"]["s"]["crc_ok"] is False


def test_verify_empty_and_self_hashed_corrupt_segment(tmp_path):
    from doppel.tape.cli import verify
    import hashlib
    assert verify(tmp_path) == 1
    w = SegmentWriter(tmp_path, "s")
    w.append("one", tw=T0)
    w.close()
    path = segment_path(tmp_path, "s", T0)
    with path.open("ab") as handle:
        handle.write(b"invalid frame")
    man = path.parent.parent / "manifest.jsonl"
    row = orjson.loads(man.read_bytes())
    row["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    man.write_bytes(orjson.dumps(row) + b"\n")
    assert verify(tmp_path) == 1


def test_frames_stream_across_read_chunks(tmp_path):
    from doppel.tape.segment import _frames
    w = SegmentWriter(tmp_path, "s")
    for i in range(40):
        w.append(f"m{i}" * (i + 1), tw=T0 + i)
        if i % 3 == 0:
            w.flush()
    w.close()
    p = segment_path(tmp_path, "s", T0)
    whole = list(_frames(p))
    assert len(whole) == 14
    for n in (1, 7, 64):  # frame boundaries land mid-chunk and chunks split frames
        assert list(_frames(p, read_size=n)) == whole
    p.write_bytes(p.read_bytes()[:-5])  # truncated tail frame is dropped, whole frames kept
    assert list(_frames(p, read_size=7)) == whole[:-1]


def test_crc_mismatch_raises(tmp_path):
    import zstandard
    p = tmp_path / "bad.dtape.zst"
    rec = {"seq": 1, "stream": "s", "tw": 1, "tm": 1, "crc": 123, "raw": "x"}
    p.write_bytes(zstandard.ZstdCompressor().compress(orjson.dumps(rec) + b"\n"))
    with pytest.raises(ValueError):
        list(read_segment(p))


def test_close_if_stale(tmp_path):
    w = SegmentWriter(tmp_path, "s")
    w.append("a", tw=T0)
    assert not w.close_if_stale(T0 + 10)
    assert w.close_if_stale(T0 + H)


def test_window_and_slugs():
    assert window_starts(1795622555, 300, ahead=1) == [1795622400, 1795622700]
    assert intl_slug("btc", "5m", 1795622700) == "btc-updown-5m-1795622700"
    assert pmus_slug("btc", "5m", 1795622400) == "cpc-btc-updown-5m-2026-11-25-1600z"
    t = pmus_targets(1795622555, assets=("btc",), horizons=("5m", "15m"))
    assert t[0][0] == "cpc-btc-updown-5m-2026-11-25-1600z"
    assert t[1][0] == "cpc-btc-updown-15m-2026-11-25-1600z"
    assert pmus_slug("eth", "5m", 1795622400) == "cpceth-eth-updown-5m-2026-11-25-1600z"


def test_receipt_rtds_pair(tmp_path):
    def trade(tx):
        return orjson.dumps({"topic": "activity", "payload": {"transactionHash": tx}}).decode()
    a, b = SegmentWriter(tmp_path, "rtds_a"), SegmentWriter(tmp_path, "rtds_b")
    for tx in ("0x1", "0x2", "0x3"):
        a.append(trade(tx), tw=T0 + 1)
    for tx in ("0x2", "0x3", "0x4"):
        b.append(trade(tx), tw=T0 + 2)
    a.append("", tw=T0 + 3, meta={"ctl": "stall"})
    a.close(), b.close()
    rec = build_receipt(tmp_path, "20261125", 16)
    assert rec["rtds_pair"] == {"tx_a": 3, "tx_b": 3, "tx_both": 2, "tx_union": 4,
                                "a_coverage": 0.75, "b_coverage": 0.75}
    assert rec["streams"]["rtds_a"]["ctl"] == {"stall": 1}
    assert rec["chain_completeness"] == "not_measured_until_M1"
    assert rtds_tx_set(segment_path(tmp_path, "rtds_b", T0)) == {"0x2", "0x3", "0x4"}


async def test_rate_limiter_paces():
    lim = RateLimiter(20.0, burst=1)
    t = time.monotonic()
    for _ in range(5):
        await lim.take()
    assert time.monotonic() - t >= 0.15


async def test_watchdog_reconnects_on_silent_stall(tmp_path, monkeypatch):
    """A server that accepts, sends one message, then goes silent must trigger a stall + reconnect."""
    conns = 0

    async def handler(ws):
        nonlocal conns
        conns += 1
        await ws.send("hello")
        await ws.wait_closed()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]

        class Local(WsSource):
            url = f"ws://127.0.0.1:{port}"
            stall_s = 1.5

        import doppel.tape.sources.base as base
        monkeypatch.setattr(base, "SSL_CTX", None)
        src = Local("t", SegmentWriter(tmp_path, "t"))
        stop = asyncio.Event()
        task = asyncio.create_task(src.run(stop))
        await asyncio.sleep(4.5)
        stop.set()
        await asyncio.wait_for(task, 5)
        src.writer.close()
    assert src.stats.stalls >= 1
    assert conns >= 2
    ctls = [r["meta"]["ctl"] for r in read_segment(next(tmp_path.glob("t/*/*.dtape.zst")))
            if r.get("meta")]
    assert "stall" in ctls and ctls.count("connect") >= 2
