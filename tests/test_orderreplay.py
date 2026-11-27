"""Order replay on synthetic frames where every twin fill is computable by hand."""

import orjson
import pytest

from doppel.lab import ordermatch
from doppel.lab.orderreplay import QuoteSim, boot_ci, main, run
from doppel.tape.segment import SegmentWriter
from doppel.twin.tape import Book, Trade

T0 = 1795629600  # hour-aligned
MS = T0 * 1000


def book(ms, asset, bids=(), asks=(), market="m1"):
    lv = lambda xs: [{"price": str(p), "size": str(s)} for p, s in xs]  # noqa: E731
    return ms, {"event_type": "book", "asset_id": asset, "market": market, "bids": lv(bids),
                "asks": lv(asks), "timestamp": str(MS + ms)}


def pc(ms, asset, price, size, side, market="m1"):
    return ms, {"event_type": "price_change", "market": market, "timestamp": str(MS + ms),
                "price_changes": [{"asset_id": asset, "price": str(price), "size": str(size), "side": side}]}


def trade(ms, asset, price, size, side, tx, market="m1"):
    return ms, {"event_type": "last_trade_price", "asset_id": asset, "market": market,
                "price": str(price), "size": str(size), "side": side, "timestamp": str(MS + ms),
                "transaction_hash": tx}


def write_tape(root, frames):
    w = SegmentWriter(root, "clob_btc_5m")
    for ms, m in frames:
        w.append(orjson.dumps([m]).decode(), tw=(MS + ms) * 1_000_000)
    w.close()


def append_received(writer, elapsed_ns, *events):
    """Write a single captured record, preserving each message's independent venue stamp."""
    writer.append(orjson.dumps([msg for _, msg in events]).decode(), tw=MS * 1_000_000 + elapsed_ns)


def orow(oid, side, price, size, placed, end, outcome, filled, fills_at, token="A", wrong=False):
    o, r = outcome.split(":")
    return {"order_id": oid, "token_id": token, "side": side, "price": price, "size": size,
            "placed_venue_ms": MS + placed, "placed_tw_ns": (MS + placed) * 1_000_000,
            "end_ms": None if end is None else MS + end, "outcome": o, "reason": r, "filled": filled,
            "fills_at": [[tx, None if t is None else MS + t, sh] for tx, t, sh in fills_at],
            "fill_before_placement": wrong, "placement_evidence": "unique_candidate",
            "placement_candidates": 1, "rest_known": end is not None}


def qrow(price, placed, end, size=97.0, outcome="cancelled", reason="exact_decrease"):
    return {"attribution": "fingerprint_candidate", "token_id": "A", "price": price, "side_view": "BUY",
            "venue_ms": MS + placed, "tw_ns": (MS + placed) * 1_000_000, "size": size,
            "outcome": outcome, "reason": reason, "end_ms": None if end is None else MS + end}


def write_dir(d, orders, quotes=None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "orders.jsonl").write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in orders))
    if quotes is not None:
        (d / "quotes.jsonl").write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in quotes))


FRAMES = [
    book(0, "A", bids=[(0.5, 100), (0.3, 10)], asks=[(0.6, 50)]),
    book(0, "B", bids=[(0.4, 50)], asks=[(0.5, 100), (0.7, 10)]),
    # q1: 50 behind a queue of 100; 80 of the queue cancel, then a 60 print: really 20 ahead + 40 ours
    pc(1000, "A", 0.5, 150, "BUY"),
    pc(1500, "A", 0.5, 70, "BUY"),
    trade(2000, "A", 0.5, 60, "SELL", "t1"),
    pc(2000, "A", 0.5, 10, "BUY"),
    pc(2500, "A", 0.5, 0, "BUY"),          # q1's last 10 cancelled
    # q2: alone at the level, 5 filled, cancelled, THEN a 200 print at its price
    pc(3000, "A", 0.5, 50, "BUY"),
    trade(3100, "A", 0.5, 5, "SELL", "t2"),
    pc(3100, "A", 0.5, 45, "BUY"),
    pc(3500, "A", 0.5, 0, "BUY"),
    trade(4000, "A", 0.5, 200, "SELL", "t3"),
    # q3: 50 behind 10 at 0.30; one execution of 30 printed on the mate (BUY B @ 0.70) AND on A
    pc(5000, "A", 0.3, 60, "BUY"),
    trade(5100, "B", 0.7, 30, "BUY", "t4"),
    trade(5100, "A", 0.3, 30, "SELL", "t4"),
    pc(5100, "A", 0.3, 30, "BUY"),
    pc(5600, "A", 0.3, 0, "BUY"),
    # q4: ask of 40 behind 50 at 0.60; a bid shows AT 0.60 with no print (25, then 35)
    pc(6000, "A", 0.6, 90, "SELL"),
    pc(6500, "A", 0.6, 25, "BUY"),
    pc(6600, "A", 0.6, 35, "BUY"),
    # c1: a never-filled 97 alone at 0.20 with a 10 print at its price while it rests
    pc(7000, "A", 0.2, 97, "BUY"),
    trade(7200, "A", 0.2, 10, "SELL", "z1"),
    pc(7500, "A", 0.2, 0, "BUY"),
    # c2: the same, but the print comes after it is gone
    pc(8000, "A", 0.2, 97, "BUY"),
    pc(8300, "A", 0.2, 0, "BUY"),
    trade(8400, "A", 0.2, 97, "SELL", "z2"),
    pc(9000, "A", 0.01, 1, "BUY"),
]
ORDERS = [
    orow("q1", "BUY", 0.5, 50.0, 1000, 2500, "cancelled:exact_decrease", 40.0, [("t1", 2000, 40.0)]),
    orow("q2", "BUY", 0.5, 50.0, 3000, 3500, "cancelled:exact_decrease", 5.0, [("t2", 3100, 5.0)]),
    orow("q3", "BUY", 0.3, 50.0, 5000, 5600, "cancelled:exact_decrease", 20.0, [("t4", 5100, 20.0)]),
    orow("q4", "SELL", 0.6, 40.0, 6000, None, "unknown:censored", 10.0, [("t9", None, 10.0)]),
    orow("bad", "BUY", 0.5, 50.0, 3000, 3500, "cancelled:exact_decrease", 5.0, [("t2", 2900, 5.0)], wrong=True),
    orow("lost", "BUY", 0.44, 50.0, 3000, 3500, "cancelled:exact_decrease", 5.0, [("tx", 3100, 5.0)]),
]
QUOTES = [qrow(0.2, 7000, 7500), qrow(0.2, 8000, 8300),
          qrow(0.2, 8000, 8400, outcome="unknown", reason="level_below")]


@pytest.fixture
def replayed(tmp_path):
    write_tape(tmp_path / "tape", FRAMES)
    write_dir(tmp_path / "om", ORDERS, QUOTES)
    doc, rows = run(tmp_path / "tape", tmp_path / "om", T0, T0 + 3600)
    return doc, {r["id"]: r for r in rows if r["kind"] == "order"}, [r for r in rows if r["kind"] == "quote"]


def test_quotesim_is_the_engine_queue_model():
    """QuoteSim only removes the genome: queue burn, through-prints and the cancel are the engine's."""
    s = QuoteSim("bid", 0.5, 50.0, live_ns=10, queue=100.0)
    s._on_trade(10, Trade(10, 10, 0.5, 500.0, "sell"))   # executed at the landing instant: no fill
    s._on_trade(20, Trade(20, 20, 0.5, 70.0, "buy"))     # wrong side
    s._on_trade(30, Trade(30, 30, 0.5, 70.0, "sell"))    # burns 70 of the queue
    assert s.got == [] and s.quote.queue == 30.0
    s._on_trade(40, Trade(40, 40, 0.5, 45.0, "sell"))    # 30 queue, 15 to us
    s._on_trade(50, Trade(50, 50, 0.49, 20.0, "sell"))   # through our price
    assert s.got == [(40, 15.0, "trade"), (50, 20.0, "trade")]
    s.cancel(60)
    s._on_trade(70, Trade(70, 70, 0.5, 100.0, "sell"))
    s._on_book(Book(tw=80, bids=(), asks=((0.5, 100.0),)))
    assert s.quote.filled == 35.0 and s.quote.dead_ns == 60 and s.live["bid"] is None


def test_behind_a_queue_pessimistic_misses_optimistic_overfills(replayed):
    _, o, _ = replayed
    q1 = o["q1"]
    assert (q1["queue_ahead"], q1["real_filled"], q1["real_first_fill_ms"]) == (100.0, 40.0, MS + 2000)
    # pessimistic: cancels ahead never advance the queue, so the 60 print only burns queue
    assert q1["pessimistic"]["filled"] == 0.0 and q1["pessimistic"]["missed"]
    assert q1["pessimistic"]["error"] == -40.0 and q1["pessimistic"]["first_fill_ms"] is None
    # optimistic: no queue, the print fills all 50: 10 more than the real order got
    opt = q1["optimistic"]
    assert opt["fills"] == [[MS + 2000, 50.0, "trade"]] and not opt["missed"]
    assert (opt["error"], opt["phantom_shares"], opt["first_fill_ms"]) == (10.0, 10.0, MS + 2000)


def test_cancelled_before_the_trade_fills_nothing_more(replayed):
    _, o, _ = replayed
    for m in ("pessimistic", "optimistic"):
        assert o["q2"][m]["fills"] == [[MS + 3100, 5.0, "trade"]]  # the 200 print at 4000 finds it gone
        assert o["q2"][m]["error"] == 0.0 and o["q2"][m]["phantom_shares"] == 0.0


def test_mate_print_is_mirrored_and_its_duplicate_dropped(replayed):
    doc, o, _ = replayed
    assert o["q3"]["pessimistic"]["fills"] == [[MS + 5100, 20.0, "trade"]]  # 30 - queue 10, once
    assert o["q3"]["optimistic"]["filled"] == 30.0
    assert doc["counts"]["mirror_dup_prints"] == 1
    assert (o["q3"]["fills_seen"], o["q3"]["fills_routed"]) == (1, 1)
    g = doc["adapter_gate"]
    assert (g["fills"], g["located_by_print"], g["print_seen_in_replay"],
            g["routed_to_order_price_and_side"], g["routed_share"]) == (4, 3, 3, 3, 1.0)
    assert (g["delivered_to_live_quote"], g["delivered_share"]) == (3, 1.0)


def test_book_cross_fill_and_unknown_end_kept_out_of_the_headline(replayed):
    doc, o, _ = replayed
    q4 = o["q4"]
    assert q4["pessimistic"]["fills"] == [[MS + 6500, 25.0, "book_cross"], [MS + 6600, 10.0, "book_cross"]]
    assert (q4["end_known"], q4["cut"], q4["pessimistic"]["phantom_shares"]) == (False, "tape_end", 25.0)
    assert doc["counts"]["twin_book_cross_fills"] == 2
    u = doc["end_unknown_orders"]["pessimistic"]
    assert (u["n"], u["real_shares"], u["twin_shares"], u["phantom"]) == (1, 10.0, 35.0, 1)
    assert doc["conditional_on_real_fill"]["pessimistic"]["n"] == 3  # q4 is not in it


def test_aggregate_and_phantom_accounting(replayed):
    doc, o, quotes = replayed
    assert set(o) == {"q1", "q2", "q3", "q4"}
    c = doc["counts"]
    assert (c["orders"], c["orders_wrong_match_skipped"], c["placement_not_found"]) == (6, 1, 1)
    assert (c["quotes"], c["quotes_not_cancelled_skipped"]) == (3, 1)
    p, q = doc["conditional_on_real_fill"]["pessimistic"], doc["conditional_on_real_fill"]["optimistic"]
    # real 40 + 5 + 20; pessimistic 0 + 5 + 20; optimistic 50 + 5 + 30
    assert (p["real_shares"], p["twin_shares"], p["ratio"]) == (65.0, 25.0, 0.3846)
    assert (p["exact"], p["underfilled"], p["missed"], p["phantom"], p["ratio_ci95"]) == (
        2, 1, 1, 0, None)  # one market: no CI
    assert doc["bracket_violations"]["pessimistic_overfilled"] == 0
    assert doc["bracket_violations"]["optimistic_underfilled"] == 0 and c["known_end_cut_by_reset"] == 0
    assert doc["optimistic_by_outcome"]["cancelled:exact_decrease"]["ratio"] == 1.3077
    assert (q["twin_shares"], q["ratio"], q["missed"], q["phantom"], q["phantom_shares"]) == (
        85.0, 1.3077, 0, 2, 20.0)
    assert q["first_fill_twin_minus_real_ms"] == {"p10": 0.0, "p50": 0.0, "p90": 0.0, "n": 3}
    b = doc["pessimistic_breakdown"]
    assert {k: v["n"] for k, v in b["by_twin_source"].items()} == {"none": 1, "trade": 2}
    assert {k: (v["n"], v["ratio"]) for k, v in b["by_queue_ahead"].items()} == {
        "0": (1, 1.0), "(0,100]": (2, 0.3333)}
    assert {k: v["n"] for k, v in b["by_price"].items()} == {"0.30-0.70": 3}
    # never-filled quotes: c1 is phantom-filled 10 by the print at its price, c2 was gone
    assert [r["pessimistic"]["filled"] for r in quotes] == [10.0, 0.0]
    nf = doc["never_filled_quotes_estimate"]
    for m in ("pessimistic", "optimistic"):
        assert (nf[m]["n"], nf[m]["twin_filled"], nf[m]["phantom_fill_rate"], nf[m]["phantom_shares"],
                nf[m]["quoted_shares"], nf[m]["by_twin_source"]) == (2, 1, 0.5, 10.0, 194.0, {"trade": 1})
    assert "ESTIMATE" in nf["note"] and "not a fill probability" in doc["limit"]


def test_bootstrap_resamples_markets():
    rows = [{"market": m, "real_filled": 10.0, "pessimistic": {"filled": f}}
            for m, f in (("a", 5.0), ("a", 5.0), ("b", 5.0), ("c", 5.0))]
    assert boot_ci(rows, "pessimistic") == [0.5, 0.5]
    rows[3]["pessimistic"]["filled"] = 10.0  # market c fills fully: the CI must open up
    lo, hi = boot_ci(rows, "pessimistic")
    assert 0.5 <= lo < 0.625 < hi <= 1.0
    assert boot_ci(rows[:2], "pessimistic") is None  # one market: no CI


def test_reset_cut_and_undelivered_fill_are_flagged_not_scored(tmp_path):
    frames = [
        book(0, "A", bids=[(0.5, 100)], asks=[(0.6, 50)]),
        pc(1000, "A", 0.5, 150, "BUY"),             # r1: 50 behind 100, then the connection drops
        pc(2000, "A", 0.4, 50, "BUY"),              # r2: alone at 0.40, filled 50 by tx u2 at 2500 ...
        pc(2600, "A", 0.01, 1, "BUY"),              # ... but a later-stamped frame precedes that print
    ]
    w = SegmentWriter(tmp_path / "tape", "clob_btc_5m")
    for ms, m in frames:
        w.append(orjson.dumps([m]).decode(), tw=(MS + ms) * 1_000_000)
    append_received(w, 3_600_000_001, trade(2500, "A", 0.4, 50, "SELL", "u2"))
    w.append("", tw=(MS + 4000) * 1_000_000, meta={"ctl": "disconnect"})
    w.close()
    write_dir(tmp_path / "om", [
        orow("r1", "BUY", 0.5, 50.0, 1000, 5000, "cancelled:exact_decrease", 10.0, [("u1", 4000, 10.0)]),
        orow("r2", "BUY", 0.4, 50.0, 2000, 2500, "filled:chain", 50.0, [("u2", 2500, 50.0)])])
    doc, rows = run(tmp_path / "tape", tmp_path / "om", T0, T0 + 3600)
    by = {r["id"]: r for r in rows}
    assert by["r1"]["cut"] == "reset" and doc["counts"]["known_end_cut_by_reset"] == 1
    assert doc["conditional_on_real_fill"]["optimistic"]["n"] == 1  # r1 is out of the headline
    # r2's own print was routed but the quote had already been cancelled: optimistic under-fill
    assert (by["r2"]["fills_routed"], by["r2"]["fills_delivered"], by["r2"]["optimistic"]["filled"]) == (1, 0, 0.0)
    assert doc["adapter_gate"]["delivered_share"] == 0.0
    assert doc["bracket_violations"]["optimistic_underfilled"] == 1


@pytest.mark.parametrize(("receipt_delta_ns", "delivered"), [
    (-1, 0), (0, 1), (1_000_000_000, 1), (1_000_000_001, 0),
])
def test_delayed_end_print_uses_first_crossing_receipt(tmp_path, receipt_delta_ns, delivered):
    tape, matches = tmp_path / "tape", tmp_path / "om"
    w = SegmentWriter(tape, "clob_btc_5m")
    append_received(w, 0, book(0, "A", bids=[(0.5, 10)]))
    append_received(w, 1_000_000_000, pc(1000, "A", 0.5, 20, "BUY"))
    crossing = 8_000_000_000  # network latency is independent of the one-second grace
    append_received(w, crossing, pc(2001, "A", 0.3, 10, "BUY"))
    if receipt_delta_ns > 10_000:
        append_received(w, crossing + 10_000, pc(2002, "A", 0.3, 20, "BUY"))
    append_received(w, crossing + receipt_delta_ns, trade(2000, "A", 0.5, 20, "SELL", "own"))
    w.close()
    write_dir(matches, [orow("o", "BUY", 0.5, 10, 1000, 2000, "filled:chain", 10, [("own", 2000, 10)])])
    doc, (r,) = run(tape, matches, T0, T0 + 12)
    assert r["fills_seen"] == r["fills_routed"] == 1
    assert r["fills_delivered"] == delivered
    assert r["pessimistic"]["filled"] == r["optimistic"]["filled"] == 10 * delivered
    assert doc["bracket_violations"]["optimistic_underfilled"] == 1 - delivered
    assert r["expiry_observed_tw_ns"] == MS * 1_000_000 + crossing
    assert r["fills_delivered_during_grace"] == doc["adapter_gate"]["delivered_during_receipt_grace"] == delivered
    assert doc["adapter_gate"]["delivered_before_expiry_observed"] == 0
    expected = {"own": {"venue_ms": MS + 2000, "tw_ns": MS * 1_000_000 + crossing + receipt_delta_ns}}
    assert r["late_deliveries"] == (expected if delivered else {})


@pytest.mark.parametrize(("candidate", "finalize"), [
    (False, "sample_end"), (True, "sample_end"), (True, "reset"), (True, "grace_exhausted"),
])
def test_late_public_prints_share_frozen_queue_and_mirror_dedup(tmp_path, candidate, finalize):
    tape, matches = tmp_path / "tape", tmp_path / "om"
    w = SegmentWriter(tape, "clob_btc_5m")
    append_received(w, 0, book(0, "A", bids=[(0.5, 30)]), book(0, "B"))
    append_received(w, 1_000_000_000, pc(1000, "A", 0.5, 70, "BUY"))
    # The crossing record's visible 40-share ask would clear the whole bid if admitted.
    append_received(w, 3_000_000_000, pc(2001, "A", 0.5, 40, "SELL"))
    # Even a book stamped inside the lifetime cannot retrospectively change this frozen queue.
    append_received(w, 3_010_000_000, pc(1990, "A", 0.5, 50, "SELL"))
    append_received(w, 3_020_000_000, trade(2002, "A", 0.5, 40, "SELL", "post_end"))
    append_received(w, 3_030_000_000, trade(999, "A", 0.5, 40, "SELL", "pre_placement"))
    append_received(w, 3_040_000_000, trade(1900, "B", 0.5, 20, "BUY", "unrelated"))
    append_received(w, 3_050_000_000, trade(1900, "A", 0.5, 20, "SELL", "unrelated"))
    append_received(w, 3_060_000_000, trade(2000, "A", 0.5, 25, "SELL", "own"))
    if finalize == "reset":
        w.append("", tw=(MS + 3200) * 1_000_000, meta={"ctl": "disconnect"})
    elif finalize == "grace_exhausted":
        append_received(w, 4_000_000_001, trade(2000, "A", 0.5, 25, "SELL", "expired"))
    w.close()
    orders = [orow("o", "BUY", 0.5, 40, 1000, 2000, "cancelled:exact_decrease", 15, [("own", 2000, 15)])]
    write_dir(matches, [] if candidate else orders, [qrow(0.5, 1000, 2000, size=40)] if candidate else [])
    doc, (r,) = run(tape, matches, T0, T0 + 5)
    assert doc["counts"]["mirror_dup_prints"] == 1
    assert r["pessimistic"]["by_source"] == {"trade": 15}  # 20 + 25 - 30 ahead
    assert r["optimistic"]["by_source"] == {"trade": 40}
    assert r["late_same_side_prints"] == 2
    if candidate:
        assert doc["never_filled_quotes_estimate"]["pessimistic"]["n"] == 1
    else:
        assert r["fills_delivered"] == 1


def test_placement_record_and_earlier_execution_are_ineligible(tmp_path):
    tape, matches = tmp_path / "tape", tmp_path / "om"
    w = SegmentWriter(tape, "clob_btc_5m")
    append_received(w, 0, book(0, "A"))
    append_received(w, 1_000_000_000, pc(1000, "A", 0.5, 40, "BUY"),
                    trade(1000, "A", 0.5, 11, "SELL", "placement_record"))
    # Separate records may have identical receipt and venue timestamps.
    append_received(w, 1_000_000_000, trade(999, "A", 0.5, 13, "SELL", "pre_placement"))
    append_received(w, 1_000_000_000, trade(1000, "A", 0.5, 7, "SELL", "next_record"))
    w.close()
    end = orow("o", "BUY", 0.5, 40, 1000, 2000, "cancelled:exact_decrease", 7, [("next_record", 1000, 7)])
    write_dir(matches, [end])
    _, (r,) = run(tape, matches, T0, T0 + 3)
    assert r["pessimistic"]["filled"] == r["optimistic"]["filled"] == 7
    assert r["fills_delivered"] == 1


@pytest.mark.parametrize("book_first", [False, True])
def test_first_post_end_event_freezes_queue_in_record_order(tmp_path, book_first):
    tape, matches = tmp_path / "tape", tmp_path / "om"
    w = SegmentWriter(tape, "clob_btc_5m")
    append_received(w, 0, book(0, "A"))
    append_received(w, 1_000_000_000, pc(1000, "A", 0.5, 40, "BUY"))
    cross = pc(1900, "A", 0.5, 20, "SELL")
    beyond_end = trade(2100, "A", 0.3, 5, "BUY", "unrelated")
    append_received(w, 2_500_000_000, *((cross, beyond_end) if book_first else (beyond_end, cross)))
    w.close()
    write_dir(matches, [orow("o", "BUY", 0.5, 40, 1000, 2000, "cancelled:exact_decrease", 0, [])])
    _, (r,) = run(tape, matches, T0, T0 + 3)
    for mode in ("pessimistic", "optimistic"):
        assert r[mode]["by_source"] == ({"book_cross": 20} if book_first else {})


@pytest.mark.parametrize("crossing", ["unchanged_book", "unchanged_price_change", "mixed_assets"])
def test_unchanged_post_end_book_starts_receipt_grace(tmp_path, crossing):
    tape, matches = tmp_path / "tape", tmp_path / "om"
    w = SegmentWriter(tape, "clob_btc_5m")
    append_received(w, 0, book(0, "A"), book(0, "B"))
    append_received(w, 1_000_000_000, pc(1000, "A", 0.5, 40, "BUY"))
    event = book(2100, "A", bids=[(0.5, 40)])
    if crossing != "unchanged_book":
        event = pc(2100, "A", 0.5, 40, "BUY")
        if crossing == "mixed_assets":
            _, other = pc(2100, "B", 0.4, 5, "BUY")
            event[1]["price_changes"].extend(other["price_changes"])
    append_received(w, 2_100_000_000, event)
    append_received(w, 3_400_000_000, trade(2000, "A", 0.5, 40, "SELL", "own"))
    w.close()
    write_dir(matches, [orow("o", "BUY", 0.5, 40, 1000, 2000, "filled:chain", 40, [("own", 2000, 40)])])
    _, (r,) = run(tape, matches, T0, T0 + 4)
    assert r["fills_seen"] == r["fills_routed"] == 1
    assert r["fills_delivered"] == r["optimistic"]["filled"] == 0
    assert r["expiry_observed_tw_ns"] == MS * 1_000_000 + 2_100_000_000


def test_exchange_clock_ahead_of_receipt_and_first_fill_inversion(tmp_path):
    tape, matches = tmp_path / "tape", tmp_path / "om"
    w = SegmentWriter(tape, "clob_btc_5m")
    append_received(w, 0, book(0, "A", bids=[(0.5, 20)]))
    append_received(w, 1_000_000_000, pc(1000, "A", 0.5, 60, "BUY"))
    append_received(w, 1_050_000_000, pc(1800, "A", 0.5, 5, "SELL"))
    append_received(w, 1_100_000_000, trade(1801, "A", 0.5, 8, "SELL", "after_cross"))
    append_received(w, 1_150_000_000, trade(1799, "A", 0.5, 25, "SELL", "before_cross"))
    w.close()
    write_dir(matches, [orow("o", "BUY", 0.5, 40, 1000, 2000, "cancelled:exact_decrease", 13,
                             [("after_cross", 1801, 8), ("before_cross", 1799, 5)])])
    _, (r,) = run(tape, matches, T0, T0 + 3)
    assert sorted(r["pessimistic"]["fills"]) == [
        [MS + 1799, 5, "trade"], [MS + 1800, 5, "book_cross"], [MS + 1801, 8, "trade"],
    ]
    assert r["pessimistic"]["first_fill_ms"] == r["optimistic"]["first_fill_ms"] == MS + 1799


@pytest.mark.parametrize("trade_ms", [1799, 1800, 1801])
def test_late_print_uses_venue_stamp_against_last_book_cross(tmp_path, trade_ms):
    tape, matches = tmp_path / "tape", tmp_path / "om"
    w = SegmentWriter(tape, "clob_btc_5m")
    append_received(w, 0, book(0, "A", bids=[(0.5, 20)]))
    append_received(w, 1_000_000_000, pc(1000, "A", 0.5, 60, "BUY"))
    append_received(w, 2_000_000_000, pc(1800, "A", 0.5, 5, "SELL"))
    append_received(w, 3_000_000_000, pc(2100, "A", 0.1, 5, "BUY"))
    append_received(w, 3_050_000_000, trade(trade_ms, "A", 0.5, 15, "SELL", "own"))
    w.close()
    write_dir(matches, [orow("o", "BUY", 0.5, 40, 1000, 2000, "cancelled:exact_decrease", 15,
                             [("own", trade_ms, 15)])])
    _, (r,) = run(tape, matches, T0, T0 + 4)
    assert r["fills_delivered"] == 1
    # The crossing observation filled 5 in either mode. At or before its stamp,
    # the late print still meets the old 20-share pessimistic queue.
    assert r["pessimistic"]["filled"] == (5 if trade_ms <= 1800 else 20)
    assert r["optimistic"]["filled"] == 20


@pytest.mark.parametrize("reset", [False, True])
def test_delayed_print_must_arrive_before_sample_end_and_reset(tmp_path, reset):
    tape, matches = tmp_path / "tape", tmp_path / "om"
    w = SegmentWriter(tape, "clob_btc_5m")
    append_received(w, 0, book(0, "A"))
    append_received(w, 1_000_000_000, pc(1000, "A", 0.5, 40, "BUY"))
    append_received(w, 2_500_000_000, pc(2100, "A", 0.1, 10, "BUY"))
    if reset:
        w.append("", tw=(MS + 2700) * 1_000_000, meta={"ctl": "disconnect"})
    append_received(w, 3_000_000_000, trade(2000, "A", 0.5, 40, "SELL", "own"))
    w.close()
    write_dir(matches, [orow("o", "BUY", 0.5, 40, 1000, 2000, "filled:chain", 40, [("own", 2000, 40)])])
    _, (r,) = run(tape, matches, T0, T0 + (4 if reset else 3))
    assert r["fills_seen"] == r["fills_routed"] == int(reset)
    assert r["fills_delivered"] == r["optimistic"]["filled"] == 0


def test_old_ordermatch_rows_are_refused(tmp_path):
    write_tape(tmp_path / "tape", FRAMES)
    write_dir(tmp_path / "om", [{k: v for k, v in ORDERS[0].items() if k != "fills_at"}])
    with pytest.raises(SystemExit, match="re-run doppel-ordermatch"):
        run(tmp_path / "tape", tmp_path / "om", T0, T0 + 3600)


def test_unqualified_receipts_and_upper_bound_lifetimes_cannot_score_the_twin(tmp_path):
    write_tape(tmp_path / "tape", FRAMES)
    write_dir(tmp_path / "om", [{k: v for k, v in ORDERS[0].items() if k != "placement_evidence"},
                                ORDERS[1] | {"fill_before_placement": None},
                                ORDERS[2] | {"rest_known": False}])
    doc, rows = run(tmp_path / "tape", tmp_path / "om", T0, T0 + 3600)
    assert len(rows) == 3 and doc["counts"]["unqualified_placements"] == 2
    assert doc["counts"]["headline_orders"] == 0 and doc["conditional_on_real_fill"]["optimistic"]["n"] == 0
    assert doc["adapter_gate"]["located_by_print"] == 1   # the qualified upper-bound order's fill is still observable


def test_ambiguous_placement_keeps_bracket_violation_visible(tmp_path):
    frames = [
        book(0, "A", bids=[(0.5, 100)]),
        pc(1000, "A", 0.5, 150, "BUY"),
        trade(1400, "A", 0.5, 130, "SELL", "other"),
        pc(1401, "A", 0.5, 20, "BUY"),
        pc(1600, "A", 0.5, 70, "BUY"),
        trade(2400, "A", 0.5, 10, "SELL", "own"),
        pc(2401, "A", 0.5, 60, "BUY"),
        pc(2600, "A", 0.5, 110, "BUY"),  # same size too late to be this order
        pc(3800, "A", 0.5, 0, "BUY"),
    ]
    tape_root = tmp_path / "tape"
    write_tape(tape_root, frames)
    path = tmp_path / "orders.jsonl"
    path.write_bytes(orjson.dumps({"order_id": "o1", "maker": "wallet", "token_id": "A", "side": "BUY",
                                  "limit": 0.5, "size": 50.0, "created_ms": MS + 900, "role": "maker",
                                  "maker_amount": 25_000_000, "taker_amount": 50_000_000,
                                  "tx": "own", "fill_amount": 5_000_000, "block_ts": T0 + 3, "verified": True}))
    _, (row,), _ = ordermatch.run(tape_root, path, T0, T0 + 3600, ["wallet"])
    assert row["placement_candidates"] == 2
    match_dir = tmp_path / "om"
    write_dir(match_dir, [row])
    doc, (out,) = run(tape_root, match_dir, T0, T0 + 3600)
    assert out["placement_candidates"] == 2 and out["pessimistic"]["filled"] == 40
    b = doc["bracket_violations"]
    assert b["pessimistic_overfilled"] == doc["counts"]["headline_orders"] == 0
    assert doc["counts"]["unqualified_placements"] == 1
    assert out["placement_evidence"] == "ambiguous" and out["end_known"] is False
    assert out["pessimistic"]["error"] == 30  # the hypothetical replay remains inspectable
    assert "ambiguous" not in b["by_placement_evidence"]   # it has no known upper end to score


def test_cli_after_ordermatch_end_to_end(tmp_path, capsys):
    """ordermatch's own output drives the replay: a FIFO fill behind a queue is reproduced exactly."""
    frames = [
        book(0, "A", bids=[(0.5, 100)], asks=[(0.6, 50)]),
        pc(1000, "A", 0.5, 150, "BUY"),             # the order: 50 behind 100
        trade(2000, "A", 0.5, 100, "SELL", "other"),  # executed first against the older queue
        trade(2000, "A", 0.5, 20, "SELL", "0xa"),     # its verified fill in a separate transaction
        pc(2000, "A", 0.5, 30, "BUY"),
        trade(3000, "A", 0.5, 30, "SELL", "0xb"),   # its last 30
        pc(3000, "A", 0.5, 0, "BUY"),
        pc(70_000, "A", 0.01, 1, "BUY"),
    ]
    write_tape(tmp_path / "tape", frames)
    rows = [{"order_id": "o1", "maker": "0xw", "token_id": "A", "side": "BUY", "size": 50.0, "limit": 0.5,
             "maker_amount": 25_000_000, "taker_amount": 50_000_000, "fill_amount": round(sh * 0.5 * 1e6),
             "created_ms": MS + 900, "role": "maker", "tx": tx, "block_ts": T0 + 4, "age_s": 3.1, "verified": True}
            for tx, sh in (("0xa", 20.0), ("0xb", 30.0))]
    (tmp_path / "orders.jsonl").write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in rows))
    span = ["--from", str(T0), "--to", str(T0 + 3600)]
    om = tmp_path / "om"
    assert ordermatch.main(["--data", str(tmp_path / "tape"), "--orders", str(tmp_path / "orders.jsonl"),
                            *span, "--out", str(om), "--min-orders", "1"]) == 0
    (r,) = map(orjson.loads, (om / "orders.jsonl").read_bytes().splitlines())
    assert r["outcome"] == "filled" and r["fills_at"] == [["0xa", MS + 2000, 20.0], ["0xb", MS + 3000, 30.0]]
    out = tmp_path / "out"
    assert main(["--data", str(tmp_path / "tape"), "--ordermatch-dir", str(om), *span, "--out", str(out)]) == 0
    doc = orjson.loads((out / "orderreplay.json").read_bytes())
    p = doc["conditional_on_real_fill"]["pessimistic"]
    assert (p["n"], p["ratio"], p["exact"], p["missed"], p["phantom"]) == (1, 1.0, 1, 0, 0)
    assert doc["adapter_gate"]["routed_share"] == 1.0
    (row,) = map(orjson.loads, (out / "orderreplay.jsonl").read_bytes().splitlines())
    assert row["pessimistic"]["fills"] == [[MS + 2000, 20.0, "trade"], [MS + 3000, 30.0, "trade"]]
    assert row["optimistic"]["fills"] == [[MS + 2000, 50.0, "trade"]]  # same total, all of it early
    text = capsys.readouterr().out
    assert "LIMIT: conditional on a real fill" in text and "adapter gate: 2/2" in text


@pytest.mark.parametrize("delayed", [False, True])
def test_mirrored_sweep_delivers_to_the_better_maker_level(tmp_path, delayed):
    """Apex regression for a 97-cent bid inside a 4-cent BUY aggregate on its mate."""
    tx = "0x" + "12" * 32
    order_id = "34" * 32
    condition = "56" * 32
    market = "0x" + condition
    tape = tmp_path / "tape"
    w = SegmentWriter(tape, "clob_btc_5m")
    append_received(w, 0, book(0, "101", bids=[(0.97, 342)], market=market))
    append_received(w, 0, book(0, "102", asks=[(0.03, 342)], market=market))
    append_received(w, 1_000_000_000, pc(1000, "101", 0.97, 442, "BUY", market))
    if delayed:
        append_received(w, 1_800_000_000, pc(1800, "101", 0.50, 10, "BUY", market))
    receipt_ns = 1_900_000_000 if delayed else 1_700_000_000
    append_received(w, receipt_ns, trade(1700, "102", 0.04, 1005.14, "BUY", tx, market))
    w.close()
    matched = tmp_path / "matched"
    write_dir(matched, [orow(order_id, "BUY", 0.97, 100, 1000, 1700, "filled:chain",
                             100, [(tx, 1700, 100)], "101")])
    _, (baseline,) = run(tape, matched, T0, T0 + 2)
    assert (baseline["fills_seen"], baseline["fills_routed"], baseline["fills_delivered"]) == (1, 0, 0)
    # Per maker level in the 97-cent bid's view: 419.45 at 97 cents, 585.69 at 96.
    common = {"tx": tx, "condition_id": condition, "n_makers": 3, "maker": "0x" + "78" * 20}
    rows = [
        ("taker", "102", "BUY", 0.04, 2500, 36_011_100, "01" * 32),
        ("maker", "101", "BUY", 0.97, 100, 97_000_000, order_id),
        ("maker", "102", "SELL", 0.03, 319.45, 319_450_000, "02" * 32),
        ("maker", "102", "SELL", 0.04, 585.69, 585_690_000, "03" * 32),
    ]
    orders = tmp_path / "orders.jsonl"
    with orders.open("wb") as file:
        for role, token, side, limit, qty, filled, oid in rows:
            cash, shares = round(limit * qty * 1e6), round(qty * 1e6)
            maker_amount, taker_amount = (cash, shares) if side == "BUY" else (shares, cash)
            file.write(orjson.dumps(common | {"role": role, "token_id": token, "side": side, "limit": limit,
                                   "size": qty, "fill_amount": filled, "order_id": oid, "maker_amount": maker_amount,
                                   "taker_amount": taker_amount, "verified": oid == order_id}) + b"\n")
    _, (resolved,) = run(tape, matched, T0, T0 + 2, orders=orders)
    assert (resolved["fills_seen"], resolved["fills_routed"], resolved["fills_delivered"]) == (1, 1, 1)
    assert (resolved["fills_raw_routed"], resolved["fills_allocated_routed"]) == (0, 1)
    assert resolved["fills_delivered_during_grace"] == int(delayed)
    assert resolved["pessimistic"]["fills"] == [[MS + 1700, 77.45, "trade"], [MS + 1700, 22.55, "trade"]]


def test_partial_and_repeated_public_frames_cannot_spend_a_call_twice(tmp_path):
    frames = [book(0, "A", market="f1"), book(0, "B", market="f1"),
              pc(900, "A", .5, 100, "BUY", "f1"),
              trade(1100, "A", .5, 10, "SELL", "tx", "f1"),
              trade(1200, "A", .5, 30, "SELL", "tx", "f1"),
              trade(1300, "B", .5, 30, "BUY", "tx", "f1"),
              trade(1400, "A", .5, 30, "SELL", "tx", "f1")]
    write_tape(tmp_path / "tape", frames)
    write_dir(tmp_path / "om", [orow("x", "BUY", .5, 100, 900, 1800, "cancelled:exact_decrease",
                                     30, [("tx", 1200, 30)])])
    tx = {"tx": "tx", "condition_id": "f1", "n_makers": 1, "verified": True, "token_id": "A"}
    rows = [tx | dict(role="maker", order_id="x", side="BUY", maker_amount=50_000_000,
                      taker_amount=100_000_000, fill_amount=15_000_000),
            tx | dict(role="taker", order_id="other", side="SELL", maker_amount=30_000_000,
                      taker_amount=15_000_000, fill_amount=30_000_000)]
    source = tmp_path / "decoded.jsonl"
    source.write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in rows))
    doc, (r,) = run(tmp_path / "tape", tmp_path / "om", T0, T0 + 2, orders=source)
    assert r["pessimistic"]["fills"] == [[MS + 1200, 30, "trade"]]
    assert r["fills_allocated_routed"] == 1 and r["unresolved_same_side_prints_while_live"] == 1
    assert doc["counts"]["public_prints:quantity_mismatch"] == 1
    assert doc["counts"]["mirror_dup_prints"] == doc["counts"]["repeat_allocated_prints"] == 1
    assert doc["bracket_violations"]["by_price_evidence"]["unresolved"]["orders"] == 1


def test_earlier_replay_end_excludes_later_known_cancel(tmp_path):
    write_tape(tmp_path / "tape", FRAMES)
    write_dir(tmp_path / "om", [ORDERS[0] | {"end_ms": MS + 3000}])
    doc, (r,) = run(tmp_path / "tape", tmp_path / "om", T0, T0 + 3)
    assert r["cut"] == "tape_end" and doc["counts"]["headline_orders"] == 0
