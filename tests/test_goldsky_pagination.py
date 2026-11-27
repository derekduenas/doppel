"""Pagination state: a budget stop with pages left is recorded incomplete and resumes exactly."""

import orjson
import pytest
from aiohttp import web

import doppel.lab.goldsky as gs
from doppel.tape.segment import SegmentWriter, read_segment

# four pages of 3, 3, 3, 1 rows for wallet w over [10, 20]
PAGES = {None: ("c1", 0), "c1": ("c2", 3), "c2": ("c3", 6), "c3": (None, 9)}


@pytest.fixture
async def server(monkeypatch):
    seen: list[str | None] = []
    fail: set[str] = set()

    async def handler(req):
        cur = req.query.get("cursor")
        seen.append(cur)
        if cur in fail:
            fail.discard(cur)
            return web.Response(status=500)
        nxt, lo = PAGES[cur]
        n = 1 if nxt is None else 3
        return web.json_response({"data": [{"id": f"r{lo + i}"} for i in range(n)],
                                  "next_cursor": nxt})

    app = web.Application()
    app.router.add_get("/data/polymarket/activity", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    monkeypatch.setattr(gs, "BASE", f"http://127.0.0.1:{port}/data/polymarket/activity")
    monkeypatch.setattr(gs, "RPS", 1000.0)
    monkeypatch.setattr(gs, "PAGE", 3)  # requests ask for 3 rows, so reservations stay small
    yield seen, fail
    await runner.cleanup()


def _ids(root):
    out = []
    for f in sorted(root.glob("goldsky_activity/*/*.dtape.zst")):
        for r in read_segment(f):
            if r["raw"]:
                out += [x["id"] for x in orjson.loads(r["raw"])["data"]]
    return out


P = {"address": "W", "start_time": 10, "end_time": 20}


async def test_budget_stop_is_incomplete_then_retry_completes_without_duplicates(tmp_path, server):
    seen, _ = server
    pages = tmp_path / "goldsky_activity" / gs.PAGES
    pages.parent.mkdir()
    led = gs.Ledger(tmp_path / "l.jsonl", cap_rows=100)
    w = SegmentWriter(tmp_path, "goldsky_activity")
    n, truncated = await gs._pull_pages("j", P, "K", led, w, 6, pages=pages)
    assert (n, truncated) == (6, True)
    st = gs.pagination_state(pages)[("w", 10, 20)]
    assert st == st | {"pages": 2, "rows": 6, "cursor": "c2", "stop": "budget", "complete": False}
    n, truncated = await gs._pull_pages("j", P, "K", led, w, 100, pages=pages)
    w.close()
    assert (n, truncated) == (4, False)
    st = gs.pagination_state(pages)[("w", 10, 20)]
    assert st == st | {"pages": 4, "rows": 10, "cursor": None, "stop": "done", "complete": True}
    assert seen == [None, "c1", "c2", "c3"]  # resumed at the exact cursor
    ids = _ids(tmp_path)
    assert sorted(ids) == sorted(f"r{i}" for i in range(10)) and len(set(ids)) == 10
    # a complete interval is not fetched again
    assert await gs._pull_pages("j", P, "K", led, SegmentWriter(tmp_path, "x"), 100,
                                pages=pages) == (0, False)
    assert len(seen) == 4


async def test_ledger_cap_and_http_error_are_recorded_and_resumable(tmp_path, server):
    seen, fail = server
    pages = tmp_path / gs.PAGES
    w = SegmentWriter(tmp_path, "goldsky_activity")
    with pytest.raises(gs.BudgetExceeded):  # cap admits two pages of 3 only
        await gs._pull_pages("j", P, "K", gs.Ledger(tmp_path / "a", cap_rows=8), w, 100, pages=pages)
    st = gs.pagination_state(pages)[("w", 10, 20)]
    assert (st["stop"], st["cursor"], st["rows"], st["complete"]) == ("budget", "c2", 6, False)
    fail.add("c2")
    with pytest.raises(RuntimeError):
        await gs._pull_pages("j", P, "K", gs.Ledger(tmp_path / "b"), w, 100, pages=pages)
    st = gs.pagination_state(pages)[("w", 10, 20)]
    assert (st["stop"], st["cursor"], st["complete"]) == ("error", "c2", False)
    await gs._pull_pages("j", P, "K", gs.Ledger(tmp_path / "b"), w, 100, pages=pages)
    w.close()
    assert gs.pagination_state(pages)[("w", 10, 20)]["complete"]
    assert seen == [None, "c1", "c2", "c2", "c3"]
    assert len(_ids(tmp_path)) == 10


async def test_zero_row_budget_reads_nothing_and_reports_pages_remaining(tmp_path, server):
    seen, _ = server
    pages = tmp_path / gs.PAGES
    got = await gs._pull_pages("j", P, "K", gs.Ledger(tmp_path / "l"), SegmentWriter(tmp_path, "x"),
                               0, pages=pages)
    assert got == (0, True) and seen == []
    assert gs.pagination_state(pages)[("w", 10, 20)]["complete"] is False
