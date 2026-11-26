import json
from pathlib import Path

import orjson
import pyarrow.parquet as pq
import pytest

import doppel.lab.ordertape as ot
from doppel.lab.fillstore import _connect, compact
from doppel.lab.ordertape import decode_match
from doppel.tape.segment import SegmentWriter

FX = json.loads((Path(__file__).parent / "fixtures_ordertape" / "match_0x4f5bc2.json").read_text())


def test_decoded_order_hashes_to_the_goldsky_order_id():
    """Real tx 0x4f5bc2…f878: the maker order of cohort wallet 0x0cb0… must hash to the order_id
    Goldsky reports for its fill, and expose the full size the fill hides."""
    orders = decode_match(FX["input"], FX["to"])
    assert orders is not None and [o["role"] for o in orders] == ["taker", "maker", "maker"]
    m = orders[2]
    assert m["maker"] == "0x0cb038487586d1119b165466072e9baf666f3a90"
    assert m["order_id"] == "dc1c320ccdba22d0474d2f04aef10028fb1d1342367ed34ffac09560a5381762"
    assert (m["side"], m["size"], round(m["limit"], 6), m["fill_amount"]) == ("BUY", 97.0, 0.23, 343506)
    assert m["created_ms"] == 1795633584086 and m["sig_type"] == 0
    t = orders[0]
    assert (t["side"], round(t["limit"], 4), t["n_makers"]) == ("BUY", 0.87, 2)


def test_other_calls_and_other_contracts_are_not_decoded():
    assert decode_match("0xdeadbeef" + "00" * 64, FX["to"]) is None
    assert decode_match(FX["input"], "0x" + "11" * 20) is None


# ---------------------------------------------------------------- cohort scale (no network)
TX = FX["tx"]
BLOCK, BLOCK_TS = 97952077, 1795633585  # the fixture tx's block, read from chain 2026-11-26
OID = "dc1c320ccdba22d0474d2f04aef10028fb1d1342367ed34ffac09560a5381762"
W0, WA, WB = "0x0cb038487586d1119b165466072e9baf666f3a90", "0xaa", "0xbb"
A, B = "https://a.example", "https://b.example"


class Clock:
    """Fake time: sleeping advances it, so rate limits and backoff run instantly."""

    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _chain(calls=None, fail=lambda url, method, params: None):
    """Fake transport: every tx is the fixture call in block BLOCK. ``fail`` returns an error
    kind to raise, ``calls`` collects (time is added by the caller's clock)."""
    def transport(url, method, params):
        if calls is not None:
            calls.append((url, method, params[0]))
        kind = fail(url, method, params)
        if kind:
            raise ot.RpcError(kind)
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(BLOCK_TS)}
        return {"input": FX["input"], "to": FX["to"], "blockNumber": hex(BLOCK)}
    return transport


def _pool(transport, urls=(A,), **kw):
    c = Clock()
    return ot.Pool(list(urls), transport, now=c.now, sleep=c.sleep, rng=lambda: 0.5, **kw), c


def _fill(i, addr, tx, ts, oid=None, bn=BLOCK):
    return {"id": f"r{i}", "address": addr, "event_type": "TRADE", "token_id": "T", "side": "BUY",
            "amount_shares": 1.0, "amount_usdc": 0.5, "fee": 0.0, "block_timestamp": ts,
            "block_number": bn, "order_type": "maker", "price": 0.5, "order_id": oid,
            "transaction_hash": tx}


def _store(tmp_path, fills):
    w = SegmentWriter(tmp_path / "tape", "goldsky_activity")
    w.append(orjson.dumps({"data": fills}).decode(), tw=1795046400 * 10**9,
             meta={"job": "cohort-a", "status": 200, "rows": len(fills)})
    w.close()
    compact(tmp_path / "tape", tmp_path / "fills")
    con = _connect(tmp_path / "fills", "256MB", 1)[0]
    assert con is not None
    return con


def _rows(out):
    return [r for p in sorted(out.glob("day=*/*.parquet")) for r in pq.read_table(p).to_pylist()]


def test_selection_from_the_fill_store(tmp_path):
    con = _store(tmp_path, [
        _fill(1, WA, "0x01", 100), _fill(2, WA, "0x01", 100),  # two fills, one tx
        _fill(3, WA, "0x02", 200), _fill(4, WA, "0x03", 300), _fill(5, WA, None, 400),
        _fill(6, WB, "0x03", 300), _fill(7, WB, "0x04", 50), _fill(8, "0xcc", "0x05", 500)])
    try:
        assert ot.select_txs(con, [WA, WB]) == [  # distinct, newest first, other wallets and null tx out
            ("0x03", 300, BLOCK), ("0x02", 200, BLOCK), ("0x01", 100, BLOCK), ("0x04", 50, BLOCK)]
        assert [t[0] for t in ot.select_txs(con, [WA, WB], 100, 300)] == ["0x02", "0x01"]  # [from, to)
        # cap 2 per wallet keeps each wallet's most recent: WA 0x03, 0x02; WB 0x03, 0x04
        assert [t[0] for t in ot.select_txs(con, [WA, WB], max_per_wallet=2)] == ["0x03", "0x02", "0x04"]
    finally:
        con.close()


def test_store_block_time_saves_the_block_request():
    calls = []
    pool, _ = _pool(_chain(calls))
    r = ot.fetch_tx(pool, (TX, BLOCK_TS, BLOCK), {})
    assert r["status"] == "decoded" and [c[1] for c in calls] == ["eth_getTransactionByHash"]
    assert [o["age_s"] for o in r["rows"]] == [1.174, 682.083, 0.914]  # E34
    assert [o["ts_flag"] for o in r["rows"]] == [None, None, None]
    # no store time, or a store block that is not the chain's: ask the chain (once per block)
    blocks: dict = {}
    assert ot.fetch_tx(pool, (TX, None, None), blocks)["rows"][0]["block_ts"] == BLOCK_TS
    assert ot.fetch_tx(pool, (TX, BLOCK_TS - 60, BLOCK - 1), blocks)["rows"][0]["block_ts"] == BLOCK_TS
    assert [c[1] for c in calls].count("eth_getBlockByNumber") == 1


def test_resume_never_duplicates(tmp_path, monkeypatch):
    todo = [(f"0x{i:02x}", BLOCK_TS, BLOCK) for i in range(10)]
    out = tmp_path / "orders"
    key = lambda rs: sorted((r["tx"], r["order_id"], r["role"]) for r in rs)  # noqa: E731

    # run 1: the request budget stops it cleanly after 5 txs (2 flushes of 3)
    s = ot.run(todo, _pool(_chain(), max_requests=5)[0], out, workers=2, flush_txs=3)
    assert (s["stopped"], s["txs_decoded"], s["txs_not_attempted"], s["requests"]) == ("max_requests", 5, 5, 5)
    assert len(_rows(out)) == 15

    # run 2: crashes after writing the parts of its first flush, before the manifest
    def boom(*a):
        raise RuntimeError("crash")
    with monkeypatch.context() as m, pytest.raises(RuntimeError):
        m.setattr(ot, "_commit", boom)
        ot.run(todo, _pool(_chain())[0], out, workers=2, flush_txs=3)
    assert len(_rows(out)) == 24  # the orphan part is on disk

    # run 3: drops the orphan, fetches only what is not committed
    calls = []
    s = ot.run(todo, _pool(_chain(calls))[0], out, workers=2, flush_txs=3)
    assert (s["txs_already_done"], s["txs_decoded"], s["stopped"]) == (5, 5, None)
    assert sorted(c[2] for c in calls) == [t[0] for t in todo[5:]]
    rows = _rows(out)
    assert len(rows) == 30 and len(set(key(rows))) == 30 and {r["tx"] for r in rows} == {t[0] for t in todo}
    assert not list(out.rglob("*.tmp"))

    # run 4: nothing left, no requests
    s = ot.run(todo, _pool(_chain(calls))[0], out)
    assert (s["txs_already_done"], s["requests"], len(_rows(out))) == (10, 0, 30)


def test_missing_and_failed_txs_are_retried_by_the_next_run(tmp_path):
    def transport(url, method, params):
        if params[0] == "0x01":
            return None  # the node does not have it
        if params[0] == "0x02":
            raise ot.RpcError("http_500")
        if params[0] == "0x03":
            return {"input": "0xdeadbeef", "to": FX["to"], "blockNumber": hex(BLOCK)}
        return _chain()(url, method, params)
    todo = [(f"0x{i:02x}", BLOCK_TS, BLOCK) for i in range(4)]
    s = ot.run(todo, _pool(transport, attempts=2)[0], tmp_path)
    assert (s["txs_decoded"], s["txs_missing"], s["txs_failed"], s["txs_not_match_orders"]) == (1, 1, 1, 1)
    assert ot.load_done(tmp_path)[0] == {"0x00", "0x03"}


def test_backoff_and_benching():
    calls = []
    pool, c = _pool(_chain(fail=lambda u, m, p: calls.append((u, c.t)) or ("http_429" if u == A else None)),
                    urls=(A, B), rate=5.0, bench_after=3, bench_s=60.0, backoff_s=0.5)
    for _ in range(40):
        assert pool.call("eth_getTransactionByHash", ["0x01"])[1] == B  # A's failures are retried on B
    ta = [t for u, t in calls if u == A]
    tb = [t for u, t in calls if u == B]
    # A: first try, then backoff 0.5 s, then 1 s (jitter factor fixed at 1), then benched 60 s
    assert [round(t - ta[0], 3) for t in ta] == [0.0, 0.5, 1.5]
    assert c.t - ta[-1] < 60.0
    assert all(b - a >= 0.2 - 1e-9 for a, b in zip(tb, tb[1:]))  # B never above 5 req/s
    st = pool.stats()
    assert st[A] == {"requests": 3, "benched": 1, "errors": {"http_429": 3}}
    assert st[B] == {"requests": 40, "benched": 0, "errors": {}}
    # after the bench A is tried again; one more failure streak benches it for twice as long
    c.t = ta[-1] + 60.0
    for _ in range(40):
        pool.call("eth_getTransactionByHash", ["0x01"])
    ta = [t for u, t in calls if u == A]
    assert len(ta) == 6 and pool.eps[0].bench_until == ta[-1] + 120.0


def test_a_recovered_endpoint_is_used_again():
    bad = {A}
    pool, c = _pool(_chain(fail=lambda u, m, p: "timeout" if u in bad else None), urls=(A, B))
    for _ in range(10):
        pool.call("m", ["x"])
    bad.clear()
    c.t += 61.0
    urls = {pool.call("m", ["x"])[1] for _ in range(10)}
    assert urls == {A, B} and pool.eps[0].benches == 0


def test_dead_endpoints_stop_the_run_instead_of_knocking_forever(tmp_path):
    pool, c = _pool(_chain(fail=lambda u, m, p: "http_503"), urls=(A, B))
    t0 = c.t
    s = ot.run([(f"0x{i:02x}", BLOCK_TS, BLOCK) for i in range(50)], pool, tmp_path, workers=1)
    assert s["stopped"] == "endpoints_down" and s["txs_decoded"] == 0 and not _rows(tmp_path)
    assert s["requests"] == 18  # 2 endpoints x 3 benches x 3 failures
    assert c.t - t0 > 60 + 120  # it waited out the benches rather than hammering
    assert s["endpoints"][A]["errors"] == {"http_503": 9} and s["endpoints"][A]["benched"] == 3


def test_verification_against_the_fill_store(tmp_path):
    con = _store(tmp_path, [
        _fill(1, W0, TX, BLOCK_TS, "0x" + OID.upper()),  # same id, other spelling
        _fill(2, W0, "0x0b", BLOCK_TS, OID), _fill(3, W0, "0x0b", BLOCK_TS, None),
        _fill(4, WB, "0x0b", BLOCK_TS, "ab" * 32),  # an id no decoded order has
        _fill(5, WB, "0x0c", BLOCK_TS, "cd" * 32)])  # a tx with no matching order at all
    ws = [W0, WB]
    try:
        todo = ot.select_txs(con, ws) + [("0x0d", None, None)]  # 0x0d: not in the store
        s = ot.run(todo, _pool(_chain())[0], tmp_path / "o", expected=lambda txs: ot.goldsky_ids(con, ws, txs))
    finally:
        con.close()
    v = s["verify"]
    assert (v["fills_expected"], v["fills_matched"], v["share"], v["txs_no_matching_order"]) == (4, 2, 0.5, 1)
    assert v["per_wallet"][W0] == {"fills_expected": 2, "fills_matched": 2, "share": 1.0}
    assert v["per_wallet"][WB] == {"fills_expected": 2, "fills_matched": 0, "share": 0.0}
    by = {(r["tx"], r["order_id"]): r["verified"] for r in _rows(tmp_path / "o")}
    assert by[(TX, OID)] is True and by[("0x0b", OID)] is True and by[("0x0c", OID)] is False
    assert by[("0x0d", OID)] is None  # no reference: unknown, not false
    assert sum(x is True for x in by.values()) == 2 and (s["orders"], s["txs_decoded"]) == (12, 4)


def test_timestamp_flags(tmp_path):
    f = ot.ts_flag
    assert f(BLOCK_TS * 1000 - 914, BLOCK_TS) is None
    assert f(BLOCK_TS * 1000 + 60, BLOCK_TS) == "after_block"  # E36: clock skew after the block
    assert f(BLOCK_TS - 5, BLOCK_TS) == "seconds" and f(0, BLOCK_TS) == "zero"
    assert f((BLOCK_TS - 31 * 86400) * 1000, BLOCK_TS) == "old"
    assert f((BLOCK_TS - 31 * 86400) * 1000, BLOCK_TS, 40 * 86400) is None
    assert f(None, BLOCK_TS) == "overflow"

    # rows are kept and flagged: the same real orders against a block 1 s and 40 days away
    def transport(url, method, params):
        if method == "eth_getBlockByNumber":
            return {"timestamp": hex(BLOCK_TS - 1 if params[0] == hex(1) else BLOCK_TS + 40 * 86400)}
        return {"input": FX["input"], "to": FX["to"], "blockNumber": hex(1 if params[0] == "0x01" else 2)}
    s = ot.run([("0x01", None, None), ("0x02", None, None)], _pool(transport)[0], tmp_path)
    assert s["ts_flags"] == {"after_block": 1, "old": 3} and s["orders"] == 6
    got = {(r["tx"], r["maker"]): (r["ts_flag"], r["age_s"]) for r in _rows(tmp_path)}
    assert got[("0x01", W0)] == ("after_block", -0.086)


def test_uint256_garbage_cannot_poison_a_flush(tmp_path):
    """created_ms and the amounts are signer-controlled uint256: a value past int64 is stored as
    null and flagged, it must not make the parquet write fail on every resume."""
    o = ot.decode_match(FX["input"], FX["to"])
    assert o is not None
    r = ot._row(o[0] | {"created_ms": 2**200, "taker_amount": 2**64}, "0x01", BLOCK, BLOCK_TS, ot.MAX_AGE_S)
    assert (r["created_ms"], r["taker_amount"], r["age_s"], r["ts_flag"]) == (None, None, None, "overflow")
    ot._write_parts(tmp_path, 1, [r])
    assert _rows(tmp_path)[0]["ts_flag"] == "overflow"
    # a maker count of 2**256-1 must be refused, not looped over; fetch_tx counts it, the run goes on
    bad = "0x" + ot.SELECTOR + "00" * 64 + (96).to_bytes(32, "big").hex() + "ff" * 32
    with pytest.raises(ValueError):
        ot.decode_match(bad, FX["to"])
    pool = _pool(lambda u, m, p: {"input": bad, "to": FX["to"], "blockNumber": hex(BLOCK)})[0]
    assert ot.fetch_tx(pool, ("0x01", BLOCK_TS, BLOCK), {})["status"] == "undecodable"
