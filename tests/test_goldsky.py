import orjson
import pytest
from aiohttp import web

import doppel.lab.goldsky as gs
from doppel.tape.segment import SegmentWriter, read_segment


def test_ledger_persists_and_caps(tmp_path):
    led = gs.Ledger(tmp_path / "l.jsonl", cap_rows=2500)
    led.add("j", 1000, "u")
    led.add("j", 1000, "u")
    led2 = gs.Ledger(tmp_path / "l.jsonl", cap_rows=2500)  # restart recomputes spend
    assert led2.total == 2000
    led2.check(500)
    with pytest.raises(gs.BudgetExceeded):
        led2.check(501)
    assert led2.summary()["usd_est"] == 0.02


async def test_pull_pages_ledgers_and_stops_at_cap(tmp_path):
    pages = {None: ("c1", 3), "c1": ("c2", 3), "c2": (None, 2)}
    seen_keys = []

    async def handler(req):
        seen_keys.append(req.headers.get("X-API-Key"))
        nxt, n = pages[req.query.get("cursor")]
        n = min(n, int(req.query["limit"]))
        return web.json_response({"data": [{"i": i} for i in range(n)], "next_cursor": nxt})

    app = web.Application()
    app.router.add_get("/data/polymarket/activity", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    gs.BASE = f"http://127.0.0.1:{port}/data/polymarket/activity"
    gs.RPS = 1000.0
    try:
        led = gs.Ledger(tmp_path / "l.jsonl", cap_rows=100)
        w = SegmentWriter(tmp_path, "goldsky_activity")
        n = await gs.pull("job1", {"start_time": 1, "end_time": 2}, "KEY", led, w, max_rows=50)
        w.close()
        assert n == 8 and led.total == 8 and set(seen_keys) == {"KEY"}
        recs = [r for f in tmp_path.glob("goldsky_activity/*/*.dtape.zst") for r in read_segment(f)]
        assert sum(r["meta"]["rows"] for r in recs) == 8
        assert all("KEY" not in r["meta"]["url"] for r in recs)  # key never written to tape
        # budget guard: a cap smaller than one page refuses before any request
        led_small = gs.Ledger(tmp_path / "small.jsonl", cap_rows=5)
        before = len(seen_keys)
        with pytest.raises(gs.BudgetExceeded):
            await gs.pull("job2", {"start_time": 1, "end_time": 2}, "KEY", led_small,
                          SegmentWriter(tmp_path, "x"), max_rows=50)
        assert len(seen_keys) == before
    finally:
        await runner.cleanup()


def test_completeness_compare(tmp_path):
    from doppel.lab.completeness import compare
    g = SegmentWriter(tmp_path, "goldsky_activity")
    rows = [{"id": f"r{i}", "block_timestamp": 100, "event_type": "TRADE", "transaction_hash": tx,
             "address": addr, "order_type": role}
            for i, (tx, addr, role) in enumerate([("0xA", "w1", "taker"), ("0xA", "w2", "maker"),
                                                  ("0xB", "w3", "taker"), ("0xC", "w4", "taker")])]
    g.append(orjson.dumps({"data": rows}).decode(), meta={"status": 200, "job": "window",
             "url": gs.BASE + "?start_time=0&end_time=200&limit=100"})
    g.close()
    a = SegmentWriter(tmp_path, "rtds_a")
    for tx, w in (("0xA", "W1"), ("0xA", "w2"), ("0xB", "w3")):
        a.append(orjson.dumps({"payload": {"transactionHash": tx, "proxyWallet": w,
                                           "timestamp": 100}}).decode())
    a.close()
    res = compare(tmp_path, 0, 200)
    assert res["goldsky"]["trade_tx"] == 3 and res["goldsky"]["roles"] == {"taker": 3, "maker": 1}
    assert res["rtds_a"]["tx_coverage_of_chain"] == round(2 / 3, 5)
    assert res["rtds_a"]["pair_coverage_of_chain"] == 0.75
    assert res["rtds_b"]["tx"] == 0


def test_holds_survive_interrupted_requests_and_concurrent_clients(tmp_path):
    p = tmp_path / "rows"
    a, b = gs.Ledger(p, 5), gs.Ledger(p, 5)
    rid = a.reserve("first", 4, "page")
    with pytest.raises(gs.BudgetExceeded):
        b.reserve("second", 4, "page")
    assert gs.Ledger(p, 5).summary()["remaining_rows"] == 1
    a.settle(rid, 2, "first", "page")
    b.reserve("second", 3, "page")
    assert a.summary()["remaining_rows"] == 0
    assert a.summary()["rows"] == 2


def test_zero_cohort_run_budget_preserves_all_credit(tmp_path, monkeypatch):
    monkeypatch.setenv("GOLDSKY_FEEDS_API_KEY", "test")
    cohort = tmp_path / "cohort.json"
    cohort.write_bytes(orjson.dumps({"candidates": ["w"], "controls": [], "sha256": "digest"}))
    assert gs.main(["--data", str(tmp_path), "--ledger", str(tmp_path/"ledger"), "cohort",
                    "--cohort", str(cohort), "--run-budget-rows", "0", "--execute"]) == 0
    assert not (tmp_path / "goldsky_activity").exists()


def test_incomplete_queries_cannot_supply_a_chain_denominator(tmp_path):
    from doppel.lab.completeness import compare
    g = SegmentWriter(tmp_path, "goldsky_activity")
    row = {"id": "f", "event_type": "TRADE", "transaction_hash": "tx", "address": "w",
           "order_type": "taker", "block_timestamp": 100}
    base = gs.BASE + "?start_time=0&end_time=200"
    def page(url, next_cursor=None):
        g.append(orjson.dumps({"data": [row], "next_cursor": next_cursor}).decode(),
                 meta={"status": 200, "job": "audit", "url": url})
        g.flush()
    result = compare(tmp_path, 0, 200)
    assert result["rtds_union"]["tx_not_on_chain"] is None
    page(base + "&address=w")
    page(base, "second")
    page(base + "&cursor=second", "third")
    page(base + "&cursor=fourth")
    result = compare(tmp_path, 0, 200)
    assert result["goldsky"]["trade_tx"] == 1
    assert not result["goldsky"]["full_market_window_verified"]
    assert result["rtds_union"]["tx_coverage_of_chain"] is None
    page(base + "&cursor=third")
    g.close()
    result = compare(tmp_path, 0, 200)
    assert result["goldsky"]["full_market_window_verified"]
    assert result["rtds_union"]["tx_coverage_of_chain"] == 0
