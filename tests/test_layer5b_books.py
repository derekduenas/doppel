import orjson
import pytest

from doppel.lab.layer5b_books import book_witnesses
from doppel.tape.segment import SegmentWriter
from doppel.tape.universe import pmus_slug
from doppel.twin.tape import NS

START = 1_800_000_000


def snapshot(slug, bid=".48", ask=".53"):
    return orjson.dumps({"marketData": {"marketSlug": slug, "state": "MARKET_STATE_OPEN",
                                       "bids": [{"px": bid, "qty": "500"}],
                                       "offers": [{"px": ask, "qty": "400"}]}})


def test_native_book_witness_uses_strict_receipts_and_clears_stalled_state(tmp_path):
    market = pmus_slug("btc", "5m", START)
    eth = pmus_slug("eth", "5m", START)
    writer = SegmentWriter(tmp_path, "pmus_ws_5m")
    tw = START * NS
    writer.append(snapshot(market), tw=tw + NS // 4)
    writer.append(snapshot(eth, ".25", ".26"), tw=tw + NS // 3)
    writer.append("", tw=tw + 2 * NS, meta={"ctl": "stall"})
    writer.append(snapshot(market, ".67", ".63"), tw=tw + 3 * NS)
    writer.append(snapshot(market, ".58", ".61"), tw=tw + 4 * NS)
    writer.close()

    values = list(book_witnesses(tmp_path, "5m", START, START + 300, step_ms=250))
    assert len(values) == 1200
    assert all(row["market_id"] == market for row in values)
    assert values[0]["book"] is values[1]["book"] is None
    assert values[2]["book"] == {"tw_ns": tw + NS // 4, "up_bid": .48, "up_ask": .53,
                                 "down_bid": .47, "down_ask": .52}
    assert values[8]["book"] == values[2]["book"]  # the equal-time gap is still future
    assert all(values[i]["book"] is None for i in (9, 13, 16, 29))
    assert values[17]["book"]["up_bid"] == .58
    assert values[28]["book"]["tw_ns"] == tw + 4 * NS  # exactly three seconds old


def test_native_source_and_window_calendar_are_required(tmp_path):
    with pytest.raises(ValueError, match="complete 5m or 15m"):
        list(book_witnesses(tmp_path, "5m", START + 1, START + 300))
    with pytest.raises(FileNotFoundError):
        next(book_witnesses(tmp_path, "15m", START, START + 900))
