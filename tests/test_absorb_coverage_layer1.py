"""Layer 1 coverage from sealed activity, through disk compaction, to the normal ledger CLI."""
from urllib.parse import urlencode

import orjson
import pytest

from doppel.lab import absorb, goldsky
from doppel.lab.fillstore import compact
from doppel.lab.goldsky_proof import recover
from doppel.tape.segment import SegmentWriter
from test_absorb import NO, O, U, W, FakeChain, _pages


def test_smaller_idle_window_still_requires_the_matching_wallet_store(tmp_path):
    network = FakeChain()
    network.open[absorb.PUSD, W, "cash"] = 100 * U
    row = network.fill("0x01", 20, "BUY", NO, 2, 0.5)
    # A terminal reply can carry a row exactly at the upper bound. It belongs to the next interval.
    at_end = network.fill("0x02", 100, "BUY", NO, 4, 0.5)
    cohort = tmp_path / "cohort.json"
    cohort.write_bytes(orjson.dumps({"candidates": [W], "controls": []}))
    tape = tmp_path / "tape"
    writer = SegmentWriter(tape, "goldsky_activity")
    for cursor, rows, nxt in [(None, [row], "more"), ("more", [at_end], None)]:
        query = {"address": W, "start_time": 1010, "end_time": 1200, "limit": 1}
        if cursor:
            query["cursor"] = cursor
        writer.append(orjson.dumps({"data": rows, "next_cursor": nxt}).decode(),
                      meta={"url": goldsky.BASE + "?" + urlencode(query), "status": 200, "job": "cohort-x",
                            "sent_tw": 1795715999 * 10**9},
                      tw=1795716000 * 10**9)
    writer.close()
    proof = tmp_path / "recovered"
    recover(tape, cohort, 1010, 1200, ["20261126/18.dtape.zst"], proof)
    receipt = goldsky.pagination_state(proof / goldsky.PAGES)[W, 1010, 1200]
    assert receipt["rows"] == 2 and receipt["trade_binding"] == goldsky.trade_binding([row])

    def run(source, name, *, prefix="cohort-"):
        store, out = tmp_path / (name + "-store"), tmp_path / (name + "-out")
        compact(source, store, job_prefix=prefix)
        assert absorb.main(["--store", str(store), "--wallets", W, "--from", "1042", "--to", "1050",
                            "--pages", str(proof / goldsky.PAGES), "--rpc", "fixture", "--rate", "1e9",
                            "--out", str(out)], transport=network) == 0
        return orjson.loads((out / f"wallet-{W}.json").read_bytes())

    ok = run(tape, "matching")
    assert ok["status"] == "reconciled" and ok["goldsky_fills"] == 0
    assert ok["completeness"]["goldsky_store_verified"] is True
    assert ok["completeness"]["goldsky_queries"][0]["trade_binding"] == receipt["trade_binding"]
    assert ok["completeness"]["chain_window_timestamps"] == [1040, 1042, 1048, 1050]
    empty = run(tape, "empty", prefix="different-job-")
    assert empty["status"] == "not_reconciled" and empty["causes"] == ["goldsky_store_unverified"]

    other = tmp_path / "other"
    writer = SegmentWriter(other, "goldsky_activity")
    writer.append(orjson.dumps({"data": [row | {"price": 0.7}]}).decode(), meta={"status": 200, "job": "cohort-x"})
    writer.close()
    changed = run(other, "same-count")
    assert changed["status"] == "not_reconciled" and changed["causes"] == ["goldsky_store_unverified"]


@pytest.mark.parametrize("change", [{"complete": 1}, {"address": O}, {"cursor": "c1"}, {"pages": 0},
                                     {"stop": "budget"}, {"end_time": 31}, {"rows": False}])
def test_inconsistent_terminal_record_cannot_certify_a_window(change):
    pages = _pages(W, 10, 30, [])
    pages[W, 10, 30].update(change)
    assert not absorb.goldsky_complete(pages, W, 15, 20, fills=[])


def test_legacy_terminal_status_is_diagnostic_only():
    pages = _pages(W, 10, 30, [])
    del pages[W, 10, 30]["trade_binding"]
    assert absorb.goldsky_complete(pages, W, 15, 20)
    assert not absorb.goldsky_complete(pages, W, 15, 20, fills=[])


def test_complete_cursor_and_matching_rows_still_require_the_chain_time_window(tmp_path):
    network = FakeChain()
    chain = absorb.Chain(absorb.Pool(["fixture"], network, rate=1e9), tmp_path / "cache")
    pages = _pages(W, 1020, 1201, [])
    # The previous block (20) also lies in this UTC window. Scanning blocks 21–24 omits it.
    report = absorb.absorb_wallet(chain, W, [], (21, 24), (1040, 1050), 10, pages, {})
    assert report["completeness"]["goldsky_store_verified"] is True
    assert report["causes"] == ["block_time_window_unverified"]


def test_stale_checkpoint_and_filtered_query_fail_before_network(tmp_path):
    import asyncio

    pages = tmp_path / "pagination.jsonl"
    args = {"address": W, "start_time": 10, "end_time": 20}
    record = dict(_pages(W, 10, 20, [])[W, 10, 20], cursor="still-more")
    pages.write_bytes(orjson.dumps(record) + b"\n")
    writer = SegmentWriter(tmp_path / "tape", "goldsky_activity")
    ledger = goldsky.Ledger(tmp_path / "ledger", cap_rows=0)
    with pytest.raises(ValueError, match="terminal checkpoint"):
        asyncio.run(goldsky._pull_pages("x", args, "", ledger, writer, 1, pages=pages))
    with pytest.raises(ValueError, match="filtered query"):
        asyncio.run(goldsky._pull_pages("x", args | {"token_id": NO}, "", ledger, writer, 1, pages=pages))
    writer.close()
    assert ledger.summary()["rows"] == 0


async def test_live_cursor_resume_binds_each_page_and_requires_explicit_end(tmp_path):
    network = FakeChain()
    first = network.fill("0x10", 20, "BUY", NO, 1, 0.4)
    second = network.fill("0x11", 24, "BUY", NO, 3, 0.5)
    replies = iter([{"data": [first], "next_cursor": "second"}, {"data": [second], "next_cursor": None},
                    {"data": []}])

    class Response:
        status, headers = 200, {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def text(self):
            return orjson.dumps(next(replies)).decode()

    class Session:
        def get(self, *args, **kwargs):
            return Response()

    q = {"address": W, "start_time": 1010, "end_time": 1200}
    pages, writer = tmp_path / "pages.jsonl", SegmentWriter(tmp_path / "tape", "goldsky_activity")
    ledger = goldsky.Ledger(tmp_path / "ledger")
    for count, more in [(1, True), (2, False)]:
        result = await goldsky._pull_pages("cohort-x", q, "", ledger, writer, 1, Session(), pages)
        assert result == (1, more)
        rec = goldsky.pagination_state(pages)[W, 1010, 1200]
        assert rec["trade_binding"] == goldsky.trade_binding([first, second][:count])
    with pytest.raises(ValueError, match="continuation field"):
        await goldsky._pull_pages("cohort-y", q | {"end_time": 1100}, "", ledger, writer, 1, Session(), pages)
    assert not goldsky.pagination_state(pages)[W, 1010, 1100]["complete"]
    writer.close()

def test_future_interval_cannot_reserve_credit(tmp_path, monkeypatch):
    import asyncio

    monkeypatch.setattr(goldsky.time, "time_ns", lambda: 19 * 10**9)
    ledger = goldsky.Ledger(tmp_path / "ledger", cap_rows=1000)
    writer = SegmentWriter(tmp_path / "tape", "goldsky_activity")
    query = {"address": W, "start_time": 10, "end_time": 20}
    pages = tmp_path / "pages.jsonl"
    with pytest.raises(ValueError, match="already ended"):
        asyncio.run(goldsky._pull_pages("cohort-x", query, "", ledger, writer, 1, pages=pages))
    writer.close()
    assert not pages.exists() and ledger.summary()["reserved_rows"] == ledger.summary()["rows"] == 0
