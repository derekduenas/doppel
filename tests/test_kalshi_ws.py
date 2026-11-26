"""kalshi_ws book replay on synthetic frames shaped like the Rust recorder writes them (raw Kalshi
WS v2 frames, verbatim, plus `meta.ctl` control records; see crates/doppel-tape/src/kalshi.rs)."""

from pathlib import Path

import orjson
import pytest

from doppel.lab import structural as st
from doppel.lab.kalshi_ws import KalshiWsBooks, Print, replay
from doppel.tape.segment import SegmentWriter

NS = 10**9
T0 = 1795622400  # 2026-11-25 16:00Z


def snap(sid, seq, tk, yes=None, no=None):
    msg: dict = {"market_ticker": tk, "market_id": "m-" + tk, "clock": 1}
    if yes is not None:
        msg["yes_dollars_fp"] = [[f"{p:.4f}", f"{q:.2f}"] for p, q in yes]
    if no is not None:
        msg["no_dollars_fp"] = [[f"{p:.4f}", f"{q:.2f}"] for p, q in no]
    return {"type": "orderbook_snapshot", "sid": sid, "seq": seq, "msg": msg}


def delta(sid, seq, tk, side, p, d):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": tk, "market_id": "m-" + tk, "clock": 2, "price_dollars": f"{p:.4f}",
                    "delta_fp": f"{d:.2f}", "side": side, "ts_ms": 1}}


def trade(sid, seq, tk, yes_px, n, taker):
    return {"type": "trade", "sid": sid, "seq": seq,
            "msg": {"trade_id": "x", "market_ticker": tk, "yes_price_dollars": f"{yes_px:.4f}",
                    "no_price_dollars": f"{1 - yes_px:.4f}", "count_fp": f"{n:.2f}", "taker_side": taker,
                    "ts_ms": 1}}


def rec(d, t, meta=None):
    return {"tw": int(t * NS), "raw": orjson.dumps(d).decode() if d is not None else "", "meta": meta}


def ctl(name, t, **kw):
    return rec(None, t, {"ctl": name, **kw})


def feed_all(kb, recs):
    out = []
    for r in recs:
        out += kb.feed(r)
    return out


def tops(evs, tk):
    return [e[3] for e in evs if e[0] == "book" and e[1] == tk]


def test_snapshot_delta_top_of_book():
    kb = KalshiWsBooks()
    evs = feed_all(kb, [
        rec({"id": 1, "type": "subscribed", "msg": {"channel": "orderbook_delta", "sid": 1}}, 0),
        rec(snap(1, 1, "A", yes=[(0.40, 10), (0.42, 5)], no=[(0.55, 7)]), 1),
        rec(delta(1, 2, "A", "yes", 0.43, 3), 2),          # new best YES bid
        rec(delta(1, 3, "A", "no", 0.56, 4), 3),           # new best NO bid -> YES ask 0.44
        rec(delta(1, 4, "A", "yes", 0.43, -3), 4),         # level removed -> back to 0.42
        rec(delta(1, 5, "A", "yes", 0.30, 1), 5),          # deep level: top unchanged, no event
    ])
    assert tops(evs, "A") == [(0.42, 5.0, 0.45, 7.0), (0.43, 3.0, 0.45, 7.0), (0.43, 3.0, 0.44, 4.0),
                              (0.42, 5.0, 0.44, 4.0)]
    assert kb.stats["snapshots"] == 1 and kb.stats["deltas"] == 4 and kb.stats["seq_gaps"] == 0


def test_empty_and_one_sided_snapshot():
    kb = KalshiWsBooks()
    evs = feed_all(kb, [rec(snap(1, 1, "A", yes=[(0.40, 10)]), 1), rec(snap(1, 2, "A"), 2)])
    assert tops(evs, "A") == [(0.40, 10.0, None, 0.0), (None, 0.0, None, 0.0)]


def test_seq_gap_invalidates_every_book_on_the_sid_until_snapshot():
    kb = KalshiWsBooks()
    evs = feed_all(kb, [
        rec(snap(1, 1, "A", yes=[(0.40, 10)], no=[(0.55, 7)]), 1),
        rec(snap(1, 2, "B", yes=[(0.20, 1)], no=[(0.70, 1)]), 1),
        rec(snap(2, 1, "C", yes=[(0.10, 1)], no=[(0.80, 1)]), 1),   # another sid: untouched by the gap
        rec(delta(1, 4, "A", "yes", 0.41, 2), 2),                    # seq 3 missing
        rec(delta(1, 5, "A", "yes", 0.42, 2), 3),                    # ignored: no valid book
        rec(snap(1, 6, "A", yes=[(0.45, 1)], no=[(0.50, 1)]), 4),    # A recovers; B still unknown
    ])
    assert tops(evs, "A") == [(0.40, 10.0, 0.45, 7.0), None, (0.45, 1.0, 0.50, 1.0)]
    assert tops(evs, "B") == [(0.20, 1.0, 0.30, 1.0), None]
    assert tops(evs, "C") == [(0.10, 1.0, 0.20, 1.0)]
    assert kb.gaps == [{"tw": 2 * NS, "sid": 1, "expected": 3, "got": 4, "type": "orderbook_delta"}]
    assert kb.stats["delta_without_book"] == 2


def test_trade_gap_does_not_touch_books_and_prints_parse():
    kb = KalshiWsBooks()
    evs = feed_all(kb, [
        rec(snap(1, 1, "A", yes=[(0.40, 10)], no=[(0.55, 7)]), 1),
        rec(trade(2, 1, "A", 0.40, 3, "no"), 2),
        rec(trade(2, 3, "A", 0.45, 1, "yes"), 3),            # gap on the trade sid
    ])
    assert [e[3] for e in evs if e[0] == "trade"] == [Print(2 * NS, "A", 0.40, 3.0, "no"),
                                                      Print(3 * NS, "A", 0.45, 1.0, "yes")]
    assert tops(evs, "A") == [(0.40, 10.0, 0.45, 7.0)]
    assert kb.stats["seq_gaps"] == 1


def test_ctl_resets_and_sid_restart_after_connect():
    kb = KalshiWsBooks()
    evs = feed_all(kb, [
        rec(snap(1, 5, "A", yes=[(0.40, 10)], no=[(0.55, 7)]), 1),
        ctl("disconnect", 2), ctl("connect", 3, url="wss://x"),
        rec(snap(1, 1, "A", yes=[(0.30, 1)], no=[(0.60, 1)]), 4),   # seq restarts at 1: not a gap
        rec(delta(1, 2, "A", "yes", 0.31, 1), 5),
        ctl("unsubscribe", 6, tickers=["A"]),
    ])
    assert tops(evs, "A") == [(0.40, 10.0, 0.45, 7.0), None, (0.30, 1.0, 0.40, 1.0), (0.31, 1.0, 0.40, 1.0), None]
    assert kb.stats["seq_gaps"] == 0 and kb.stats["resets"] == 2


def test_backpressure_drop_and_negative_level_invalidate():
    kb = KalshiWsBooks()
    evs = feed_all(kb, [
        rec(snap(1, 1, "A", yes=[(0.40, 10)], no=[(0.55, 7)]), 1),
        rec(delta(1, 2, "A", "yes", 0.40, -11), 2),           # below zero: book corrupt
        rec(snap(1, 3, "A", yes=[(0.40, 10)], no=[(0.55, 7)]), 3),
        ctl("backpressure_drop", 4, count=3),
    ])
    assert tops(evs, "A") == [(0.40, 10.0, 0.45, 7.0), None, (0.40, 10.0, 0.45, 7.0), None]
    assert kb.stats["negative_level"] == 1


def test_legacy_cent_fields_and_unparsed_counted():
    kb = KalshiWsBooks()
    evs = feed_all(kb, [
        rec({"type": "orderbook_snapshot", "sid": 1, "seq": 1, "msg": {"market_ticker": "A", "yes": [[40, 10]],
                                                                       "no": [[55, 7]]}}, 1),
        rec({"type": "orderbook_delta", "sid": 1, "seq": 2, "msg": {"market_ticker": "A", "price": 41, "delta": 2,
                                                                    "side": "yes"}}, 2),
        rec({"type": "trade", "sid": 2, "seq": 1, "msg": {"market_ticker": "A", "yes_price": 41, "count": 2,
                                                          "taker_side": "yes"}}, 3),
        rec({"type": "orderbook_delta", "sid": 1, "seq": 3, "msg": {"market_ticker": "A", "side": "yes"}}, 4),
        rec({"type": "orderbook_snapshot", "sid": 1, "seq": 4, "msg": {"market_ticker": "B",
                                                                       "yes_dollars_fp": [["x", "1"]]}}, 5),
    ])
    assert tops(evs, "A") == [(0.40, 10.0, 0.45, 7.0), (0.41, 2.0, 0.45, 7.0), None]
    assert [e[3].yes_px for e in evs if e[0] == "trade"] == [0.41]
    assert kb.stats["unparsed"] == 2


def write_tape(root: Path, recs: list[dict]) -> None:
    w = SegmentWriter(root, "kalshi_ws")
    for r in recs:
        w.append(r["raw"], tw=r["tw"], meta=r["meta"])
    w.close()


def test_replay_reads_snapshot_from_previous_hour(tmp_path: Path):
    write_tape(tmp_path, [
        rec(snap(1, 1, "KXBTC15M-A", yes=[(0.40, 10)], no=[(0.55, 7)]), T0 - 600),   # previous hour file
        rec(snap(1, 2, "KXETH15M-Z", yes=[(0.10, 1)]), T0 - 500),                     # other series: seq only
        rec(delta(1, 3, "KXBTC15M-A", "yes", 0.41, 1), T0 + 10),
    ])
    evs, kb = replay(tmp_path, T0, T0 + 60, want=lambda t: t.startswith("KXBTC15M-"))
    got = [(e[2] // NS - T0, e[3]) for e in evs]
    assert got == [(-600, (0.40, 10.0, 0.45, 7.0)), (10, (0.41, 1.0, 0.45, 7.0))]
    assert kb.stats["seq_gaps"] == 0
    evs, kb = replay(tmp_path, T0, T0 + 60, lookback=60)   # too short a look-back: no snapshot, no book
    assert list(evs) == [] and kb.stats["delta_without_book"] == 1


def test_structural_load_books_kalshi_ws(tmp_path: Path):
    write_tape(tmp_path, [
        rec(snap(1, 1, "KX-1", yes=[(0.30, 4)], no=[(0.60, 9), (0.65, 2)]), T0 - 100),
        rec(delta(1, 3, "KX-1", "yes", 0.31, 1), T0 + 2),     # seq gap: book unknown from here
    ])
    books = st.load_books(tmp_path, {"kalshi": {"KX-1"}}, T0, T0 + 5, kalshi_stream="kalshi_ws")
    kb = books[("kalshi", "KX-1")]
    assert kb.fresh is True
    assert kb.leg(T0, "yes") == (pytest.approx(0.35), 2.0, T0 - 100)
    assert kb.leg(T0, "no") == (pytest.approx(0.70), 4.0, T0 - 100)
    assert kb.leg(T0 + 3, "yes") is None and kb.leg(T0 + 3, "no") is None   # not resurrected
