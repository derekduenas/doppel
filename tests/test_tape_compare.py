import orjson

from doppel.tape.compare import compare
from doppel.tape.segment import SegmentWriter

H = 1795651200
NS = 1_000_000_000


def _tape(root, n, gaps=0, lag_ms=50):
    w = SegmentWriter(root, "clob_btc_5m")
    for i in range(n):
        ts = (H + i % 3000) * 1000
        w.append(orjson.dumps({"event_type": "book", "timestamp": str(ts)}).decode(), tw=(ts + lag_ms) * 1_000_000)
    for _ in range(gaps):
        w.append("", tw=(H + 10) * NS, meta={"ctl": "gap"})
    w.close()


def test_cutover_rule(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    _tape(a, 2000, gaps=1, lag_ms=60)
    _tape(b, 2000, gaps=1, lag_ms=40)              # same records, faster
    _tape(c, 1900, gaps=5, lag_ms=400)             # fewer records, more gaps, slower
    ok = compare(a, b, H, H + 3600)
    assert ok["pass"] and ok["streams"]["clob_btc_5m"]["b"]["data"] == 2000
    bad = compare(a, c, H, H + 3600)
    r = bad["streams"]["clob_btc_5m"]
    assert not bad["pass"] and len(r["reasons"]) == 3


def test_cutover_cannot_pass_on_missing_stream_or_empty_range(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _tape(a, 2000)
    _tape(b, 2000)
    assert compare(a, b, H, H + 3600, ["clob_btc_5m"])["pass"]
    miss = compare(a, b, H, H + 3600, ["clob_btc_5m", "kalshi_ws"])
    assert not miss["pass"] and miss["problems"] == ["expected stream kalshi_ws missing or empty"]
    assert not compare(a, b, H, H)["pass"]                       # --from == --to
    assert not compare(a, b, H + 7200, H + 10800)["pass"]        # hours with no data
