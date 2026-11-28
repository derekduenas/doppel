import orjson

from doppel.lab.layer5b_panel import prepare_rows
from doppel.lab.lead_witnesses import BINANCE, HYPERLIQUID
from doppel.tape.segment import SegmentWriter, segment_path
from doppel.tape.universe import pmus_slug
from doppel.twin.tape import NS


def test_panel_joins_all_native_sources_only_after_their_receipts(tmp_path):
    start = 1_800_000_000
    clock = start * NS
    ref = SegmentWriter(tmp_path, "ref_brti_live")
    ref.append("{}", tw=clock - 1, meta={"status": 200, "new": [
        [s * 1000, 100_000 + s % 17] for s in range(start - 302, start)]})
    ref.append("{}", tw=clock + 2 * NS, meta={"status": 200, "new": [[start * 1000, 100_060]]})
    ref.close()

    books = SegmentWriter(tmp_path, "pmus_ws_5m")
    market = pmus_slug("btc", "5m", start)
    books.append(orjson.dumps({"marketData": {"marketSlug": market, "bids": [{"px": ".49", "qty": "100"}],
                                             "offers": [{"px": ".53", "qty": "100"}]}}), tw=clock + 2 * NS)
    books.close()
    native = {
        BINANCE: {"lastUpdateId": 42, "bids": [["99999", "1"]], "asks": [["100001", "2"]]},
        HYPERLIQUID: {"channel": "l2Book", "data": {"coin": "BTC", "time": start * 1000,
                                                   "levels": [[{"px": "99998", "sz": "3"}],
                                                              [{"px": "100002", "sz": "4"}]]}},
    }
    clocks = {BINANCE: clock + 2 * NS, HYPERLIQUID: clock + 2 * NS + NS // 4}
    for stream, doc in native.items():
        writer = SegmentWriter(tmp_path, stream)
        writer.append(orjson.dumps(doc), tw=clocks[stream])
        writer.close()
    inputs = {s: [segment_path(tmp_path, s, tw)] for s, tw in clocks.items()}
    rows = list(prepare_rows(tmp_path, "5m", start, start + 300,
                             binance=inputs[BINANCE], hyperliquid=inputs[HYPERLIQUID],
                             predict_lead_up=lambda fair, pair: fair - pair.hyperliquid.bid_size / 100))
    assert rows[8]["book"] is rows[8]["fair"] is None
    assert rows[9]["fair"] and rows[9]["book"] and rows[9]["lead"] is None
    assert rows[10]["lead"] == {
        "tw_ns": clocks[HYPERLIQUID], "binance_tw_ns": clocks[BINANCE],
        "hyperliquid_tw_ns": clocks[HYPERLIQUID], "p_up": rows[10]["fair"]["p_up"] - .03}
