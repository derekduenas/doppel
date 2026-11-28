"""Replay-side cache validation and edge cases independent of the Rust writer."""

import hashlib

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from doppel.lab import book_cache as bc
from doppel.lab.intlbook import IntlBook, replay, replay_messages
from doppel.tape.segment import SegmentWriter, segment_path


START = 1_799_280_000
NS = 1_000_000_000
STREAM = "clob_btc_5m"
MARKET = "0xmarket"
TOKEN = "77"


def build_fixture(tmp_path):
    root, cache = tmp_path / "raw", tmp_path / "cache"
    w = SegmentWriter(root, STREAM)
    rows = []

    def record(offset, raw, messages=(), ctl=None):
        tw = (START + offset) * NS
        w.append(raw if isinstance(raw, str) else orjson.dumps(raw).decode(), tw=tw,
                 meta=None if ctl is None else {"ctl": ctl})
        index = record.count
        for no, message in enumerate(messages):
            rows.append({"record_no": index, "message_no": no, "seq": index + 1, "ts_ns": tw, **message})
        record.count += 1

    record.count = 0
    bid = [{"price": round(0.49 - i * 0.01, 2), "size": float(5 + i)} for i in range(8)]
    ask = [{"price": round(0.52 + i * 0.01, 2), "size": float(20 + i)} for i in range(8)]
    ms = START * 1000
    raw_book = {"event_type": "book", "timestamp": str(ms), "market": MARKET, "asset_id": TOKEN,
                "bids": bid, "asks": ask}
    cached_book = {"event_type": "book", "venue_ms": ms, "market_id": MARKET, "asset_id": TOKEN,
                   "bids": bid, "asks": ask}
    record(0, raw_book, [cached_book])
    record(1, "PONG", [{"event_type": "skip"}])
    changes = [{"price": 0.57, "size": 480.0, "side": "SELL", "asset_id": TOKEN}]
    raw_change = {"event_type": "price_change", "timestamp": str(ms + 2_000), "market": MARKET,
                  "price_changes": changes}
    cached_change = {"event_type": "price_change", "venue_ms": ms + 2_000, "market_id": MARKET, "changes": changes}
    raw_print = {"event_type": "last_trade_price", "timestamp": str(ms + 2_000), "market": MARKET,
                 "asset_id": TOKEN, "price": "0.52", "size": "12", "side": "BUY", "transaction_hash": "tx"}
    cached_print = {"event_type": "last_trade_price", "venue_ms": ms + 2_000, "market_id": MARKET,
                    "asset_id": TOKEN, "trade_px": 0.52, "trade_sz": 12.0, "trade_side": "BUY", "tx": "tx"}
    record(2, [raw_change, raw_print], [cached_change, cached_print])
    record(3, [raw_book | {"timestamp": str(ms + 3_000)}, {"event_type": "opaque"}],
           [{"event_type": "fallback", "fallback_raw": orjson.dumps(
               [raw_book | {"timestamp": str(ms + 3_000)}, {"event_type": "opaque"}]).decode()}])
    record(4, "", [{"event_type": "control", "ctl": "connect"}], ctl="connect")
    record(5, raw_change | {"timestamp": str(ms + 5_000)},
           [cached_change | {"venue_ms": ms + 5_000}])
    record(6, raw_book | {"timestamp": str(ms + 6_000)}, [cached_book | {"venue_ms": ms + 6_000}])
    w.close()

    source = segment_path(root, STREAM, START * NS)
    cache.mkdir()
    path = bc.BookCache(cache).path(source, STREAM)
    path.parent.mkdir(parents=True)
    levels = pa.list_(pa.struct([("price", pa.float64()), ("size", pa.float64())]))
    delta = pa.list_(pa.struct([("asset_id", pa.string()), ("price", pa.float64()), ("size", pa.float64()),
                                ("side", pa.string())]))
    fields = [("record_no", pa.int64()), ("message_no", pa.int32()), ("seq", pa.int64()), ("ts_ns", pa.int64()),
              ("event_type", pa.string()), ("venue_ms", pa.int64()), ("market_id", pa.string()),
              ("asset_id", pa.string()), ("bids", levels), ("asks", levels), ("changes", delta),
              ("trade_px", pa.float64()), ("trade_sz", pa.float64()), ("trade_side", pa.string()), ("tx", pa.string()),
              ("ctl", pa.string()), ("fallback_raw", pa.string())]
    raw = source.read_bytes()
    schema = pa.schema(fields, metadata={bc.SCHEMA: b"1", bc.SOURCE_HASH: hashlib.sha256(raw).hexdigest().encode(),
                                       bc.SOURCE_BYTES: str(len(raw)).encode(), bc.SOURCE_STREAM: STREAM.encode(),
                                       bc.SOURCE_HOUR: str(START * NS).encode(),
                                       bc.SOURCE_RECORDS: str(record.count).encode()})
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path, row_group_size=3, compression="zstd")
    return root, cache, source, path


@pytest.mark.parametrize("offset,until", [(0, 8), (1, 5), (3, 7), (0, 3603)])
def test_cache_preserves_capture_offsets_book_state_and_clocks(tmp_path, offset, until):
    root, cache, _, _ = build_fixture(tmp_path)
    lo, hi = START + offset, START + until
    for strict in (False, True):
        a, b = IntlBook(strict_clocks=strict), IntlBook(strict_clocks=strict)
        assert list(replay(root, STREAM, lo, hi, a, strict=strict)) == list(
            replay(root, STREAM, lo, hi, b, strict=strict, book_cache=cache))
        assert vars(a) == vars(b)
    a, b = IntlBook(), IntlBook()
    assert list(replay_messages(root, STREAM, lo, hi, a)) == list(
        replay_messages(root, STREAM, lo, hi, b, book_cache=cache))
    assert vars(a) == vars(b)


def test_selected_cache_refuses_missing_or_stale_source(tmp_path):
    root, cache, source, path = build_fixture(tmp_path)
    raw = source.read_bytes()
    source.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    with pytest.raises(ValueError, match="does not match its sealed source"):
        list(replay(root, STREAM, START, START + 8, IntlBook(), book_cache=cache))
    source.unlink()
    with pytest.raises(ValueError, match="source hour is missing"):
        list(replay(root, STREAM, START, START + 8, IntlBook(), book_cache=cache))
    source.write_bytes(raw)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        list(replay(root, STREAM, START, START + 8, IntlBook(), book_cache=cache))


def test_cache_refuses_whole_record_truncation(tmp_path):
    root, cache, _, path = build_fixture(tmp_path)
    table = pq.ParquetFile(path).read()
    pq.write_table(table.slice(0, table.num_rows - 1), path, compression="zstd")
    with pytest.raises(ValueError, match="source record count differs"):
        list(replay(root, STREAM, START, START + 8, IntlBook(), book_cache=cache))
