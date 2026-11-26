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


def orow(oid, side, price, size, placed, end, outcome, filled, fills_at, token="A", wrong=False):
    o, r = outcome.split(":")
    return {"order_id": oid, "token_id": token, "side": side, "price": price, "size": size,
            "placed_venue_ms": MS + placed, "placed_tw_ns": (MS + placed) * 1_000_000,
            "end_ms": None if end is None else MS + end, "outcome": o, "reason": r, "filled": filled,
            "fills_at": [[tx, None if t is None else MS + t, sh] for tx, t, sh in fills_at],
            "fill_before_placement": wrong}


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
        trade(2500, "A", 0.4, 50, "SELL", "u2"),
    ]
    w = SegmentWriter(tmp_path / "tape", "clob_btc_5m")
    for ms, m in frames:
        w.append(orjson.dumps([m]).decode(), tw=(MS + ms) * 1_000_000)
    w.append("", tw=(MS + 3000) * 1_000_000, meta={"ctl": "disconnect"})
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


def test_old_ordermatch_rows_are_refused(tmp_path):
    write_tape(tmp_path / "tape", FRAMES)
    write_dir(tmp_path / "om", [{k: v for k, v in ORDERS[0].items() if k != "fills_at"}])
    with pytest.raises(SystemExit, match="re-run doppel-ordermatch"):
        run(tmp_path / "tape", tmp_path / "om", T0, T0 + 3600)


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
        pc(3800, "A", 0.5, 70, "BUY"),
    ]
    tape_root = tmp_path / "tape"
    write_tape(tape_root, frames)
    p = ordermatch.Params()
    tape = ordermatch.build_tape(tape_root, ["clob_btc_5m"], T0, T0 + 3600, {"A"}, [], p)
    order = ordermatch.Order("o1", "wallet", "A", "BUY", 0.5, 50.0, MS + 900, {"own": (10.0, T0 + 3)})
    matches = ordermatch.match_placements([order], tape, p)
    row = ordermatch.classify(order, *matches["o1"], tape, set(), p)
    assert row["placement_candidates"] == 2
    match_dir = tmp_path / "om"
    write_dir(match_dir, [row])
    doc, (out,) = run(tape_root, match_dir, T0, T0 + 3600)
    assert out["placement_candidates"] == 2 and out["pessimistic"]["filled"] == 40
    b = doc["bracket_violations"]
    assert b["pessimistic_overfilled"] == 1
    assert b["by_placement_evidence"]["multiple_candidates"] == {
        "orders": 1, "pessimistic_overfilled": 1, "optimistic_underfilled": 0}


def test_cli_after_ordermatch_end_to_end(tmp_path, capsys):
    """ordermatch's own output drives the replay: a FIFO fill behind a queue is reproduced exactly."""
    frames = [
        book(0, "A", bids=[(0.5, 100)], asks=[(0.6, 50)]),
        pc(1000, "A", 0.5, 150, "BUY"),             # the order: 50 behind 100
        trade(2000, "A", 0.5, 120, "SELL", "0xa"),  # 100 queue + 20 of it
        pc(2000, "A", 0.5, 30, "BUY"),
        trade(3000, "A", 0.5, 30, "SELL", "0xb"),   # its last 30
        pc(3000, "A", 0.5, 0, "BUY"),
        pc(70_000, "A", 0.01, 1, "BUY"),
    ]
    write_tape(tmp_path / "tape", frames)
    rows = [{"order_id": "o1", "maker": "0xw", "token_id": "A", "side": "BUY", "size": 50.0, "limit": 0.5,
             "maker_amount": 25_000_000, "taker_amount": 50_000_000, "fill_amount": round(sh * 0.5 * 1e6),
             "created_ms": MS + 900, "role": "maker", "tx": tx, "block_ts": T0 + 4, "age_s": 3.1}
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


def test_mirrored_sweep_delivers_to_the_better_maker_level(tmp_path):
    """Apex regression for a 97-cent bid inside a 4-cent BUY aggregate on its mate."""
    tx = "0x" + "12" * 32
    order_id = "34" * 32
    condition = "56" * 32
    market = "0x" + condition
    tape = tmp_path / "tape"
    write_tape(tape, [
        book(0, "101", bids=[(0.97, 342)], market=market),
        book(0, "102", asks=[(0.03, 342)], market=market),
        pc(1000, "101", 0.97, 442, "BUY", market),
        trade(1700, "102", 0.04, 1005.14, "BUY", tx, market),
    ])
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
