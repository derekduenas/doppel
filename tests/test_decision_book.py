"""Receipt-clock cutoffs, capture exclusions and flow evidence on the production rulefit path."""
import orjson
import pytest

from doppel.lab.rulefit import scan
from doppel.tape.segment import SegmentWriter
from test_rulefit import MS, T0, book, pc, trade

STREAM = "clob_btc_5m"


def write_tape(root, rows, gap_at=None):
    w = SegmentWriter(root, STREAM)
    for n, (ms, msg) in enumerate(rows):
        if n == gap_at:
            w._seq += 1  # model a dropped envelope
        stamp = (MS + ms) * 1_000_000
        if isinstance(msg, str):
            w.append("", tw=stamp, meta={"ctl": msg})
        else:
            w.append(orjson.dumps(msg), tw=stamp)
    w.close()


def query(root, cuts, end=60, grid=False):
    asks = [((MS + ms) * 1_000_000, token, (ms, token)) for ms, token in cuts]
    by_start = {T0: [("U", "w", 0)]} if grid else {}
    return scan(root, STREAM, T0, T0 + end, asks, {}, {"tx"}, by_start, 300, details=True,
                query_levels={key: (.40, "BUY") for _, _, key in asks})


def opening(ms, token="U"):
    return book(ms, token, [(.40, 7), (.38, 13)], [(.42, 18)], "m")


def test_cutoff_excludes_all_embedded_messages_and_equal_time_packets_and_traces_depth(tmp_path):
    first = [opening(0), opening(1, "D"), trade(150, "U", .4, 4, "tx", "m"),
             pc(750, "U", .40, 11, "BUY", "m"), trade(1100, "U", .4, 6, "tx", "m")]
    cut = (1150, "U")
    for name, changed in [("a", 25), ("b", 250)]:
        frame = [pc(1150, "U", .4, changed, "BUY", "m")[1],
                 trade(1150, "U", .4, changed, "tx", "m")[1]]
        rows = first + [(1150, frame), pc(1150, "U", .41, changed, "BUY", "m"), opening(2000)]
        write_tape(tmp_path / name, rows)
    cuts = [cut, (1150, "D"), (1151, "U")]
    a, b = [query(tmp_path / name, cuts) for name in ("a", "b")]
    result = a["detail"][cut]
    assert result == {"status": "known", "known": True, "best_bid": .4, "bid_size": 11,
                      "best_ask": .42, "ask_size": 18, "level_size": 11, "last_depth_seq": 4,
                      "last_depth_tw_ns": (MS + 750) * 1_000_000, "last_depth_venue_ms": MS + 750,
                      "flow_status": "covered", "prior_1s_sell_shares": 10, "prior_1s_buy_shares": 0,
                      "prior_1s_print_count": 2}
    assert orjson.dumps(result) == orjson.dumps(b["detail"][cut])
    assert a["detail"][(1151, "U")]["level_size"] != b["detail"][(1151, "U")]["level_size"]
    assert a["detail"][(1151, "U")]["prior_1s_sell_shares"] == 31
    assert a["detail"][(1150, "D")]["prior_1s_print_count"] == 0
    assert a["mate_known_tw"] == {"U": (MS + 1) * 1_000_000, "D": (MS + 1) * 1_000_000}


@pytest.mark.parametrize("gap_kind", ["sequence", "disconnect", "bad_frame", "reversed_depth_clock"])
def test_later_discontinuity_only_changes_retrospective_mask_until_it_is_received(tmp_path, gap_kind):
    cuts = [(1000, "U"), (1501, "U"), (1511, "U"), (2001, "U")]
    common = [opening(0), pc(100, "U", .4, 10, "BUY", "m")]
    payload = {"sequence": pc(1500, "U", .4, 8, "BUY", "m"), "disconnect": (1500, "disconnect"),
               "bad_frame": (1500, {"event_type": "unknown"}),
               "reversed_depth_clock": pc(1500, "U", .4, 8, "BUY", "m", ts=99)}[gap_kind]
    tail = [pc(1510, "U", .4, 9, "BUY", "m"), opening(2000), opening(3000)]
    write_tape(tmp_path / "ok", common + [opening(1500)] + tail)
    write_tape(tmp_path / "gap", common + [payload] + tail, gap_at=2 if gap_kind == "sequence" else None)
    a, b = query(tmp_path / "ok", cuts), query(tmp_path / "gap", cuts)
    key = cuts[0]
    assert orjson.dumps(a["detail"][key]) == orjson.dumps(b["detail"][key])
    assert a["state_integrity"][key] == "covered" and b["state_integrity"][key] == "unbracketed_gap"
    assert [b["detail"][k]["status"] for k in cuts[1:]] == ["uninitialized", "uninitialized", "known"]
    assert b["detail"][cuts[-1]]["flow_status"] == "incomplete"


def test_silent_span_right_edge_and_per_token_age_have_distinct_observations(tmp_path):
    rows = [opening(0), opening(2000, "D"), opening(4000, "D"),
            pc(25000, "U", .4, 9, "BUY", "m"), opening(25100)]
    write_tape(tmp_path, rows)
    cuts = [(2000, "U"), (3001, "U"), (4500, "D"), (25001, "U"), (25101, "U"), (29000, "U")]
    got = query(tmp_path, cuts, grid=True)
    assert [got["detail"][k]["status"] for k in cuts] == [
        "known", "stale_depth", "known", "uninitialized", "known", "stale_depth"]
    assert [got["state_integrity"][k] for k in cuts] == [
        "covered", "covered", "unbracketed_gap", "covered", "right_censored", "right_censored"]
    assert got["grid"][("w", T0, 0)] is None and got["grid_integrity"][("w", T0 + 10, 0)] == "unbracketed_gap"


def test_an_incremental_update_renews_a_quiet_token_only_on_an_unbroken_stream(tmp_path):
    write_tape(tmp_path, [opening(0), opening(2100, "D"), trade(3100, "U", .4, 8, "tx", "m"),
                          pc(4000, "U", .4, 12, "BUY", "m"), opening(6000, "D")])
    cuts = [(3000, "U"), (3101, "U"), (4000, "U"), (4001, "U"), (5001, "U")]
    got = query(tmp_path, cuts)
    assert [got["detail"][k]["level_size"] for k in cuts] == [7, None, None, 12, 12]
    assert [got["detail"][k]["flow_status"] for k in cuts[-2:]] == ["incomplete", "covered"]


def test_cancel_diagnostic_excludes_entire_tied_receipt_and_stale_preceding_depth(tmp_path):
    write_tape(tmp_path, [opening(1), pc(1000, "U", .41, 20, "BUY", "m"),
                          (1000, [pc(1000, "U", .39, 100, "SELL", "m")[1],
                                  pc(1000, "U", .40, 2, "BUY", "m")[1]]),
                          pc(4800, "U", .38, 3, "BUY", "m")])
    cancels = {("U", .4, "BUY", MS + 1000): [("once", 5)], ("U", .38, "BUY", MS + 4800): [("stale", 10)]}
    got = scan(tmp_path, STREAM, T0, T0 + 10, [], cancels, set(), {}, 300)
    assert got["at_cancel"] == {"once": (.4, 7, .42, 18)}


@pytest.mark.parametrize("change", [{"timestamp": None}, {"side": "UNKNOWN"}, {"size": "-1"}, {"price": "nan"}])
def test_invalid_print_makes_native_flow_unknown_without_changing_depth_or_locating_tx(tmp_path, change):
    ts, bad = trade(1100, "U", .4, 8, "tx", "m")
    write_tape(tmp_path, [opening(0), (ts, bad | change), opening(1500), opening(2500)])
    cuts = [(1200, "U"), (2100, "U"), (2101, "U")]
    got = query(tmp_path, cuts)
    assert not got["fill_tw"] and not got["fill_prints"]
    assert [got["detail"][k]["level_size"] for k in cuts] == [7, 7, 7]
    assert [got["detail"][k]["prior_1s_print_count"] for k in cuts] == [None, None, 0]


def test_missing_segment_and_malformed_depth_need_new_snapshots(tmp_path):
    write_tape(tmp_path, [opening(3599999), pc(7200001, "U", .4, 6, "BUY", "m"), opening(7200002),
                          (7200003, opening(7200003)[1] | {"asks": [{"price": ".5", "size": "-20"}]}),
                          pc(7200004, "U", .4, 6, "BUY", "m"), opening(7200005)])
    cuts = [(3600000, "U"), (7200002, "U"), (7200003, "U"), (7200005, "U"), (7200006, "U")]
    got = query(tmp_path, cuts, end=7201)
    assert [got["detail"][k]["status"] for k in cuts] == [
        "known", "uninitialized", "known", "uninitialized", "known"]
    assert got["state_integrity"][cuts[0]] == "unbracketed_gap"


def test_receive_clock_reversal_rejects_the_input(tmp_path):
    write_tape(tmp_path, [opening(10), opening(5)])
    with pytest.raises(ValueError, match="non-monotonic receive clock"):
        query(tmp_path, [(7, "U")])
