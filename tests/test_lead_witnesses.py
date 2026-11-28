import json

import pytest

from doppel.lab.lead_witnesses import BINANCE, HYPERLIQUID, LeadWitnesses, MAX_AGE_NS
from doppel.tape.segment import SegmentWriter, segment_path


START = 1_795_800_000_000_000_000

BID_DEPTH = [["50099.0", "9.9"], ["50100.0", "1.2"]]
BINANCE_BOOK = {"lastUpdateId": 160, "bids": BID_DEPTH, "asks": [["50101.0", "2.3"]]}
HYPERLIQUID_BOOK = {"channel": "l2Book", "data": {"coin": "BTC", "time": START // 1_000_000 + 9_000,
                                                "levels": [[{"px": "50101", "sz": "2", "n": 1}],
                                                           [{"px": "50102", "sz": "3", "n": 2}]]}}


def test_native_segments_preserve_source_clocks_gaps_and_strict_cutoffs(tmp_path):
    binance = SegmentWriter(tmp_path, BINANCE)
    hyperliquid = SegmentWriter(tmp_path, HYPERLIQUID)
    binance.append(json.dumps(BINANCE_BOOK), START + 10, 100)
    hyperliquid.append(json.dumps(HYPERLIQUID_BOOK), START + 12, 102)
    binance.append("", START + 20, 110, {"ctl": "backpressure_drop", "count": 1})
    binance.append(json.dumps({**BINANCE_BOOK, "asks": [["50101.0", "NaN"]]}), START + 25, 115)
    binance.append(json.dumps(BINANCE_BOOK), START + 30, 120)
    hyperliquid.append("", START + 32, 122, {"ctl": "disconnect"})
    hyperliquid.append(json.dumps(HYPERLIQUID_BOOK), START + 32, 122)
    binance.close()
    hyperliquid.close()

    books = LeadWitnesses.from_segments([segment_path(tmp_path, BINANCE, START)],
                                       [segment_path(tmp_path, HYPERLIQUID, START)])
    assert books.at(START + 12) is None
    first = books.at(START + 13)
    assert first is not None
    assert (first.tw_ns, first.binance.tw_ns, first.hyperliquid.tw_ns) == (START + 12, START + 10, START + 12)
    assert (first.binance.bid, first.binance.bid_size, first.binance.ask, first.binance.ask_size) == (
        50100.0, 1.2, 50101.0, 2.3,
    )
    assert (first.binance.tm_ns, first.binance.seq, first.binance.update_id) == (100, 1, 160)
    assert first.hyperliquid.exchange_ts_ms == HYPERLIQUID_BOOK["data"]["time"]
    assert books.at(START + 20) == first
    assert books.at(START + 21) is None
    assert books.at(START + 29) is None
    assert books.at(START + 30) is None
    next_book = books.at(START + 32)
    assert next_book is not None and next_book.hyperliquid == first.hyperliquid
    recovered = books.at(START + 33)
    assert recovered is not None and recovered.hyperliquid.tw_ns == START + 32
    assert books.at(START + 30 + MAX_AGE_NS) == recovered
    assert books.at(START + 30 + MAX_AGE_NS + 1) is None


def record(stream, stamp, seq, book):
    return {"stream": stream, "tw": START + stamp, "tm": stamp, "seq": seq, "raw": json.dumps(book)}


def test_invalid_future_snapshot_cannot_change_prior_witness_and_resets_after_arrival():
    binance = [record(BINANCE, 10, 1, BINANCE_BOOK), record(BINANCE, 20, 2, BINANCE_BOOK)]
    invalid = record(BINANCE, 20, 2, {**BINANCE_BOOK, "asks": [["50100.0", "2"]]})
    hyperliquid = [record(HYPERLIQUID, 10, 1, HYPERLIQUID_BOOK)]
    original = LeadWitnesses(binance, hyperliquid)
    perturbed = LeadWitnesses([binance[0], invalid], hyperliquid)
    assert original.at(START + 20) == perturbed.at(START + 20)
    assert original.at(START + 21) is not None
    assert perturbed.at(START + 21) is None
    with pytest.raises(ValueError, match="nondecreasing"):
        perturbed.at(START + 20)
