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
