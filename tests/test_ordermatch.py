"""Order match on synthetic frames where every answer is computable by hand."""

import orjson
import pytest

from doppel.lab.intlbook import IntlBook, LevelEvent, Print, Reset
from doppel.lab.tradeallocation import Allocation
from doppel.lab.ordermatch import (LevelTape, Order, Params, attribute, classify, filled_shares,
                                   fingerprint, load_orders, main, match_placements, observable, run,
                                   _placements, _fill_prints, _evidence, qualified_placement)
from doppel.tape.segment import SegmentWriter

T0 = 1795629600  # hour-aligned
MS = T0 * 1000
P = Params()
W, X = "0xw", "0xx"


def book(ms, asset, bids=(), asks=(), market="m1"):
    lv = lambda xs: [{"price": str(p), "size": str(s)} for p, s in xs]  # noqa: E731
    return ms, {"event_type": "book", "asset_id": asset, "market": market, "bids": lv(bids),
                "asks": lv(asks), "timestamp": str(MS + ms)}


def pc(ms, *chs, market="m1"):
    return ms, {"event_type": "price_change", "market": market, "timestamp": str(MS + ms),
                "price_changes": [{"asset_id": a, "price": str(p), "size": str(s), "side": side}
                                  for a, p, s, side in chs]}


def trade(ms, asset, price, size, tx, market="m1", side="BUY"):
    return ms, {"event_type": "last_trade_price", "asset_id": asset, "market": market,
                "price": str(price), "size": str(size), "side": side, "timestamp": str(MS + ms),
                "transaction_hash": tx}


def tape_of(msgs) -> LevelTape:
    b, t = IntlBook(), LevelTape()
    for ms, m in msgs:
        for it in b.apply(m, (MS + ms) * 1_000_000):
            t.add_print(it) if isinstance(it, Print) else t.add(it)
    t.spans, t.mate = b.spans, b.mates()
    return t


def order(oid, token, side, price, size, created, maker=W, fills=None):
    return Order(oid, maker, token, side, price, size, MS + created, fills or {}, verified=True)


def orow(o: Order, role="maker"):
    """ordertape rows for an Order (amounts as on chain: fill in maker-amount units)."""
    out = []
    for tx, (sh, bts) in o.fills.items():
        ma, ta = (o.price * o.size, o.size) if o.side == "BUY" else (o.size, o.price * o.size)
        fa = sh * o.price if o.side == "BUY" else sh
        out.append({"order_id": o.order_id, "maker": o.maker, "token_id": o.token, "side": o.side,
                    "size": o.size, "limit": o.price, "maker_amount": round(ma * 1e6),
                    "taker_amount": round(ta * 1e6), "fill_amount": round(fa * 1e6),
                    "created_ms": o.created_ms, "role": role, "tx": tx, "block_ts": bts,
                    "age_s": round(bts - o.created_ms / 1000, 3)})
    return out


def test_jsonl_excludes_explicitly_unverified_rows(tmp_path):
    base = orow(order("legacy", "101", "BUY", 0.5, 20, 1000, fills={"t": (5, T0 + 2)}))[0]
    path = tmp_path / "sample.jsonl"
    variants = [base, base | {"order_id": "good", "verified": True},
                base | {"order_id": "other", "verified": False}, base | {"order_id": "unknown", "verified": None}]
    path.write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in variants))
    orders, _, stats = load_orders(path, T0, T0 + 3)
    assert {o.order_id for o in orders} == {"good"}
    assert stats["rows_not_verified"] == 3
    path.write_bytes(orjson.dumps(base) + b"\n")
    assert [o.order_id for o in load_orders(path, T0, T0 + 3)[0]] == ["legacy"]


# ---------------------------------------------------------------- reconstruction
def test_reconstruction_snapshot_diff_and_reset():
    b = IntlBook()
    tw = lambda ms: (MS + ms) * 1_000_000  # noqa: E731
    assert b.apply(pc(5, ("A", 0.5, 7, "BUY"))[1], tw(5)) == [] and b.dropped == 1  # unknown state
    assert b.size("A", 0.5, "BUY") is None
    assert b.apply(book(10, "A", bids=[(0.5, 100)])[1], tw(10)) == []  # first snapshot: no events
    (e,) = b.apply(pc(20, ("A", 0.5, 198, "BUY"))[1], tw(20))
    assert isinstance(e, LevelEvent)
    assert (e.venue_ms, e.tw_ns, e.asset, e.price, e.side, e.old_size, e.new_size, e.src) == (
        MS + 20, tw(20), "A", 0.5, "BUY", 100.0, 198.0, "price_change")
    assert e.delta == 98.0
    # a later snapshot is diffed: 0.50 shrinks, 0.49 appears, an absent level would go to 0
    evs = b.apply(book(30, "A", bids=[(0.5, 150), (0.49, 10)])[1], tw(30))
    assert [(x.price, x.old_size, x.new_size, x.src) for x in evs if isinstance(x, LevelEvent)] == [
        (0.49, 0.0, 10.0, "book"), (0.5, 198.0, 150.0, "book")]
    assert b.feed({"tw": tw(35), "raw": "", "meta": {"ctl": "subscribe_add"}}) == []
    assert b.size("A", 0.5, "BUY") == 150.0
    assert b.feed({"tw": tw(40), "raw": "", "meta": {"ctl": "disconnect"}}) == [Reset(tw(40), "disconnect")]
    assert b.size("A", 0.5, "BUY") is None
    assert b.apply(pc(50, ("A", 0.5, 1, "BUY"))[1], tw(50)) == [] and b.dropped == 2
    assert b.feed({"tw": tw(60), "raw": orjson.dumps([book(60, "A", bids=[(0.5, 3)])[1]]).decode()}) == []
    (p,) = b.feed({"tw": tw(70), "raw": orjson.dumps(trade(70, "A", 0.5, 2, "0xt")[1]).decode()})
    assert isinstance(p, Print) and (p.price, p.size, p.tx) == (0.5, 2.0, "0xt")
    assert b.feed({"tw": tw(80), "raw": "PONG"}) == []
    assert b.spans["A"] == [[MS + 10, MS + 30], [MS + 60, MS + 70]]  # known spans end at the last frame
    b.apply(book(90, "B", market="m1")[1], tw(90))
    assert b.mates() == {"A": "B", "B": "A"}


# ---------------------------------------------------------------- placement
def test_placement_is_one_to_one_and_read_against_placebo():
    t = tape_of([
        book(0, "A", bids=[(0.5, 100)]),
        pc(10_150, ("A", 0.5, 198, "BUY")),  # +98
        pc(10_400, ("A", 0.5, 296, "BUY")),  # +98
        pc(10_500, ("A", 0.5, 300, "BUY")),  # +4: wrong size
        pc(70_200, ("A", 0.5, 398, "BUY")),  # +98 a minute later: someone else
        pc(80_000, ("A", 0.01, 1, "BUY")),   # last frame: state known to here
    ])
    o1 = order("o1", "A", "BUY", 0.5, 98.0, 10_000)
    o2 = order("o2", "A", "BUY", 0.5, 98.0, 10_100)
    o3 = order("o3", "A", "BUY", 0.5, 98.0, 79_000)  # placebo window runs past the known span
    o4 = order("o4", "A", "BUY", 0.5, 97.0, 10_000)  # no +97 anywhere
    k = ("A", 0.5, "BUY")
    m = match_placements([o1, o2, o3, o4], t, P)
    # o2 is 50 ms from the first increase, o1 150 ms: o2 takes it, o1 gets the second
    assert m == {"o2": (k, 0), "o1": (k, 1)}
    pl = match_placements([o1, o2, o3, o4], t, P, P.placebo_ms)
    assert pl == {"o2": (k, 3)}  # one chance increase, one order may claim it
    assert [observable(o, t, P) and observable(o, t, P, P.placebo_ms) for o in (o1, o2, o3, o4)] == [
        True, True, False, True]
    assert match_placements([o4], t, Params(size_tol_rel=0.02)) == {"o4": (k, 0)}  # E37 tolerance


def evidence(orders, tape, shift=0):
    prints = {o.order_id: _fill_prints(o, tape) for o in orders}
    search = _placements(orders, tape, P, shift, prints)
    return {o.order_id: _evidence(o, tape, P, search, prints[o.order_id], shift) for o in orders}


def test_one_physical_add_cannot_support_two_orders_including_mirrors():
    tape = tape_of([book(0, "A"), book(0, "B"), pc(1150, ("A", 0.4, 10, "BUY"), ("B", 0.6, 10, "SELL")),
                    trade(1500, "A", 0.4, 1, "tx", side="SELL"),
                    pc(5000, ("A", 0.3, 1, "BUY"), ("B", 0.7, 1, "SELL"))])
    own = order("o", "A", "BUY", 0.4, 10, 1000, fills={"tx": (1, T0 + 2)})
    peer = order("p", "B", "SELL", 0.6, 10, 1100, maker=X, fills={"tx": (1, T0 + 2)})
    assert evidence([own], tape) == {"o": "unique_candidate"}
    assert evidence([own, peer], tape) == {"o": "ambiguous", "p": "ambiguous"}
    assert len(match_placements([own, peer], tape, P)) == 1


def test_distinct_times_in_same_tx_do_not_locate_a_fill():
    o = order("o", "A", "BUY", 0.4, 10, 1000, fills={"tx": (3, T0 + 2)})
    messages = [book(0, "A"), pc(1150, ("A", 0.4, 10, "BUY")),
                trade(1300, "A", 0.4, 3, "tx", side="BUY"),  # same token, wrong taker side
                trade(1400, "A", 0.45, 3, "tx", side="SELL"), trade(1500, "A", 0.4, 2, "tx", side="SELL"),
                pc(5000, ("A", 0.3, 1, "BUY"))]
    assert evidence([o], tape_of(messages))["o"] == "fill_time_unknown"
    exact = trade(1550, "A", 0.4, 3, "tx", side="SELL")
    assert evidence([o], tape_of(messages + [exact]))["o"] == "fill_time_unknown"
    clean = messages[:2] + [exact, messages[-1]]
    assert evidence([o], tape_of(clean))["o"] == "unique_candidate"
    assert evidence([o], tape_of(clean + [trade(1551, "A", 0.4, 3, "tx", side="SELL")]))["o"] == "fill_time_unknown"
    assert evidence([o], tape_of(clean + [trade(150_000, "A", 0.4, 3, "tx", side="SELL")]))["o"] == "fill_time_unknown"
    o.fills = {"tx": (3, T0 + 130)}
    assert evidence([o], tape_of(clean))["o"] == "fill_time_unknown"


def test_strict_matcher_depth_clock_and_prints():
    b = IntlBook(strict_clocks=True)
    b.apply(book(1000, "A")[1], 1)
    msg = pc(1250, ("A", 0.5, 1, "BUY"))[1]
    b.apply(msg, 2)
    b.apply(trade(9000, "A", 0.5, 1, "tx")[1], 3)
    assert b.spans["A"][-1][1] == MS + 1250
    assert b.apply(msg | {"timestamp": str(MS + 1050)}, 4) == [] and not b.known("A")
    assert b.clock_counts["reset:asset_venue_time_reversal"] == 1
    b.apply(book(2500, "A")[1], 5)
    assert b.apply(msg | {"timestamp": None}, 6) == [] and not b.known("A")
    assert b.clock_counts["reset:invalid_venue_time"] == 1
    assert b.apply(trade(3000, "A", 0.5, 1, "tx")[1] | {"timestamp": None}, 7) == []
    assert b.clock_counts["unclocked_prints"] == 1


@pytest.mark.parametrize(("event", "expected"), [
    (pc(900, ("A", 0.4, 10, "BUY")), "chronology_conflict"),
    (pc(2001, ("A", 0.4, 10, "BUY")), "chronology_conflict"),
    (book(1150, "A", bids=[(0.4, 10)]), "snapshot_only"),
    (pc(1150, ("A", 0.4, 10, "BUY")), "unique_candidate"),
])
def test_signed_and_fill_clocks_apply_symmetrically_to_placebo(event, expected):
    o = order("o", "A", "BUY", 0.4, 10, 1000, fills={"tx": (3, T0 + 2)})
    ms, packet = event
    later = (ms + P.placebo_ms, packet | {"timestamp": str(MS + ms + P.placebo_ms)})
    t = tape_of([book(0, "A"), event, trade(2000, "A", 0.4, 3, "tx", side="SELL"),
                 pc(3000, ("A", 0.4, 0, "BUY")), later, pc(70000, ("A", 0.3, 1, "BUY"))])
    assert evidence([o], t)["o"] == evidence([o], t, P.placebo_ms)["o"] == expected


def test_partial_fill_dust_is_not_a_full_order_even_with_large_book_tolerance():
    o = order("o", "A", "SELL", 0.4, 10.0, 1000, fills={"tx": (9.999, T0 + 2)})
    o.maker_amount, o.filled_amount = 10_000_000, 9_999_000
    t = tape_of([book(0, "A"), pc(1100, ("A", 0.4, 10, "SELL")),
                 trade(1500, "A", 0.4, 9.999, "tx"), pc(5000, ("A", 0.5, 1, "BUY"))])
    search = _placements([o], t, P, 0, {"o": _fill_prints(o, t)})
    row = classify(o, *search.selected["o"], t, set(), P, search=search)
    assert qualified_placement(row) and row["outcome"] == "unknown" and row["rest_known"] is False


def test_saved_age_and_jsonl_order_cannot_hide_earlier_fill(tmp_path):
    rows = orow(order("o", "A", "BUY", 0.4, 10, 1000, fills={"later": (1, T0 + 2), "earlier": (3, T0)}))
    path = tmp_path / "orders.jsonl"
    path.write_bytes(b"".join(orjson.dumps(r | {"age_s": 1, "verified": True}) + b"\n" for r in rows))
    os_, _, stats = load_orders(path, T0, T0 + 10)
    assert os_ == [] and stats["screened:bad_age"] == 1


def test_full_call_allocation_can_locate_a_maker_inside_an_aggregate_print():
    o = order("o", "A", "BUY", 0.4, 10, 1000, fills={"tx": (3, T0 + 2)})
    t = tape_of([book(0, "A"), book(0, "B"), pc(1100, ("A", 0.4, 10, "BUY")),
                 trade(1500, "A", 0.3, 5, "tx", side="SELL"), pc(5000, ("A", 0.5, 1, "BUY"))])
    assert evidence([o], t)["o"] == "fill_time_unknown"
    t.allocations["tx"] = Allocation("m1", "A", "B", "SELL", 0.3, ((0.45, 2), (0.4, 3)), 5, 2e-6)
    assert evidence([o], t)["o"] == "unique_candidate"


def test_missing_segment_is_reported_but_corrupt_segment_fails(tmp_path):
    from doppel.lab.ordermatch import build_tape

    s = "clob_btc_5m"
    t = build_tape(tmp_path, [s], T0, T0 + 5, {"A"}, [], P)
    assert t.counts["reset:missing_segment"] == 1 and not t.spans
    w = SegmentWriter(tmp_path, s)
    ms, frame = book(0, "A")
    w.append(orjson.dumps(frame).decode(), tw=(MS + ms) * 1_000_000)
    w.close()
    path, = (tmp_path / s).glob("*/*.dtape.zst")
    path.write_bytes(path.read_bytes()[:-2])
    with pytest.raises(ValueError):
        build_tape(tmp_path, [s], T0, T0 + 5, {"A"}, [], P)


def test_overlapping_or_identical_placebo_window_refused():
    with pytest.raises(ValueError, match="disjoint"):
        Params(lo_ms=0, hi_ms=60000, placebo_ms=60000)


# ---------------------------------------------------------------- follow-forward
FILLED = order("f", "A", "BUY", 0.4, 50.0, 1_000, fills={"0xf1": (50.0, T0 + 4)})
CANC = order("c", "E", "SELL", 0.7, 97.0, 1_100, fills={"0xc1": (1.5, T0 + 3)})
AMBIG = order("u", "E", "BUY", 0.3, 97.0, 1_200, fills={"0xu1": (1.5, T0 + 3)})
CENS = order("z", "E", "BUY", 0.2, 97.0, 1_250, fills={"0xz1": (1.0, T0 + 4)})
PRINTED = order("p", "E", "BUY", 0.25, 97.0, 1_300, fills={"0xp1": (1.5, T0 + 3)})
CASES = [FILLED, CANC, AMBIG, CENS, PRINTED]
FRAMES = [
    book(0, "A"), book(0, "B"),
    book(0, "E", bids=[(0.3, 200), (0.2, 500), (0.25, 100)], market="m3"),
    pc(1_100, ("A", 0.4, 50, "BUY")),
    pc(1_200, ("E", 0.7, 97, "SELL"), market="m3"),
    pc(1_300, ("E", 0.3, 297, "BUY"), market="m3"),
    pc(1_350, ("E", 0.2, 597, "BUY"), market="m3"),
    pc(1_400, ("E", 0.25, 197, "BUY"), market="m3"),
    trade(2_000, "E", 0.7, 1.5, "0xc1", "m3"), trade(2_000, "E", 0.3, 1.5, "0xu1", "m3", "SELL"),
    trade(2_000, "E", 0.25, 1.5, "0xp1", "m3", "SELL"),
    pc(2_005, ("E", 0.7, 95.5, "SELL"), ("E", 0.3, 295.5, "BUY"), ("E", 0.25, 195.5, "BUY"), market="m3"),
    pc(2_600, ("E", 0.7, 0, "SELL"), market="m3"),  # the 95.5 left, 1.5 printed nearby: cancel
    trade(3_000, "B", 0.6, 50, "0xf1"),             # FILLED's fill prints on the mate at 1 - 0.4
    book(3_010, "A"),                               # ... and leaves the book via a snapshot
    pc(4_000, ("E", 0.3, 50, "BUY"), market="m3"),  # -245.5: cannot hold AMBIG's 95.5 any more
    pc(5_000, ("E", 0.25, 100, "BUY"), market="m3"),  # -95.5 exactly, but ...
    trade(5_010, "E", 0.25, 95.5, "0xother", "m3"),   # ... 95.5 printed there: maybe a fill
    pc(9_000, ("E", 0.01, 5, "BUY"), market="m3"), pc(9_001, ("A", 0.01, 1, "BUY")),
]


def _classified(t):
    search = _placements(CASES, t, P, 0, {o.order_id: _fill_prints(o, t) for o in CASES})
    m = search.selected
    assert set(m) == {o.order_id for o in CASES}
    claimed = set()
    return {o.order_id: classify(o, *m[o.order_id], t, claimed, P, search=search) for o in CASES}


def test_fill_vs_cancel_vs_ambiguous():
    t = tape_of(FRAMES)
    assert t.events[("A", 0.4, "BUY")][-1].src == "book"  # the fill is visible only as a snapshot diff
    r = _classified(t)
    get = lambda i, *ks: tuple(r[i][k] for k in ks)  # noqa: E731
    rest = ("outcome", "reason", "rest_min_ms", "rest_max_ms", "rest_known")
    # fully filled on chain; print (by tx, on the mate) dates the end: 3000 - 1100
    assert get("f", *rest) == ("filled", "chain", 1900, 1900, True)
    assert get("f", "sign_to_book_ms", "fills_located", "print_price_ok", "print_size_ok") == (100, 1, 1, 1)
    # 1.5 filled, the remaining 95.5 removed in one step with only 1.5 printed near: 2600 - 1200
    assert get("c", *rest) == ("unknown", "level_zero", 800, 1400, False)
    assert get("c", "remainder", "alone", "prints_near", "end_src") == (95.5, True, 1, "price_change")
    assert not any(x["fill_before_placement"] for x in r.values())
    # level fell below the remainder with no exact decrease: existed to its fill (2000), gone by 4000
    assert get("u", *rest) == ("unknown", "level_below", 700, None, False)
    # fill has no print (block time only) and nothing else happens at the level
    assert get("z", *rest) == ("unknown", "fill_time_unknown", 0, None, False)
    assert get("z", "fills_located", "filled") == (0, 1.0)
    # an exact decrease that prints explain is not called a cancel
    assert get("p", *rest) == ("unknown", "censored", 600, None, False)


def test_wrong_match_and_level_gone_before_the_fill_is_known():
    t = tape_of([
        book(0, "A", bids=[(0.5, 10)]),
        trade(500, "A", 0.5, 97, "0xa", side="SELL"),  # the fill prints ...
        pc(1_000, ("A", 0.5, 107, "BUY")),       # ... before the increase taken as its placement
        pc(1_100, ("A", 0.3, 97, "BUY")),
        pc(2_000, ("A", 0.3, 1, "BUY")),         # level left with 1 while the fill is only block-timed
        pc(9_000, ("A", 0.01, 1, "BUY")),
    ])
    bad = order("bad", "A", "BUY", 0.5, 97.0, 900, fills={"0xa": (97.0, T0 + 2)})
    gone = order("gone", "A", "BUY", 0.3, 97.0, 1_000, fills={"0xnoprint": (2.0, T0 + 5)})
    m = match_placements([bad, gone], t, P)
    assert "bad" not in m  # the transaction already printed before the proposed increase
    # no event after block time 5000, but the level already cannot hold the 95 that should remain
    rg = classify(gone, *m["gone"], t, set(), P)
    assert rg["outcome"] == "unknown" and rg["rest_max_ms"] is None and not qualified_placement(rg)


def test_fill_amount_is_in_maker_amount_units():
    """Real tx 0x4f5bc2 (tests/fixtures_ordertape): the 5-share bid at 0.23 fills 1.15 USDC = all of
    it; the 97-share bid fills 0.343506 USDC = 1.4935 shares."""
    base = {"side": "BUY", "limit": 0.23}
    assert filled_shares(base | {"maker_amount": 1150000, "taker_amount": 5000000, "fill_amount": 1150000}) == 5.0
    got = filled_shares(base | {"maker_amount": 22310000, "taker_amount": 97000000, "fill_amount": 343506})
    assert round(got, 4) == 1.4935
    assert filled_shares({"side": "SELL", "limit": 0.7, "fill_amount": 1500000}) == 1.5


# ---------------------------------------------------------------- fingerprint
def test_fingerprint_sizes():
    assert fingerprint([98.0] * 9 + [13.0]) == [98.0]
    assert fingerprint([97.0] * 3 + [98.0] * 4 + [99.0] * 2 + [5.0]) == [97.0, 98.0, 99.0]
    assert fingerprint([float(i) for i in range(20)]) == []  # diffuse: no fingerprint
    assert fingerprint([]) == []


def test_attribution_shows_the_known_false_positive_and_the_base_rate():
    H = 3_600_000
    t = tape_of([
        book(0, "A"), book(0, "B"), book(0, "C", market="m2"), book(0, "D", market="m2"),
        # the wallet's bid: shows as BUY A 0.50 and as SELL B 0.50
        pc(1_100, ("A", 0.5, 98, "BUY"), ("B", 0.5, 98, "SELL")),
        # the wallet's ask on A at 0.60: its BUY view is a bid on B at 0.40
        pc(1_200, ("A", 0.6, 98, "SELL"), ("B", 0.4, 98, "BUY")),
        # another maker's decoded 98 bid: a proven false positive
        pc(1_300, ("A", 0.45, 98, "BUY"), ("B", 0.55, 98, "SELL")),
        # two 98s nobody decoded: candidates
        pc(2_000, ("A", 0.44, 98, "BUY"), ("B", 0.56, 98, "SELL")),
        pc(2_100, ("B", 0.3, 98, "BUY"), ("A", 0.7, 98, "SELL")),
        pc(2_800, ("A", 0.44, 0, "BUY"), ("B", 0.56, 0, "SELL")),  # first candidate pulled after 800 ms
        # a 98 in a market the wallet never traded: base rate
        pc(3_000, ("C", 0.5, 98, "BUY"), ("D", 0.5, 98, "SELL"), market="m2"),
        pc(3_100, ("C", 0.5, 198, "BUY"), ("D", 0.5, 198, "SELL"), market="m2"),  # +100: not the size
        pc(H, ("A", 0.01, 1, "BUY"), ("B", 0.99, 1, "SELL")),
        pc(H, ("C", 0.01, 1, "BUY"), ("D", 0.99, 1, "SELL"), market="m2"),
    ])
    orders = [order("w1", "A", "BUY", 0.5, 98.0, 1_000), order("w2", "A", "SELL", 0.6, 98.0, 1_100),
              order("x1", "A", "BUY", 0.45, 98.0, 1_200, maker=X),
              order("w3", "A", "BUY", 0.2, 98.0, 50_000)]  # decoded, but no increase found for it
    m = match_placements(orders, t, P)
    assert set(m) == {"w1", "w2", "x1"}
    doc, rows = attribute(W, [98.0], orders, m, {"A"}, t, set(), P)
    tot = doc["total"]
    # own 2 (the SELL order's mirror twin is not double-counted as a candidate), 1 proven other
    assert (tot["own"], tot["other_known"], tot["candidates"], tot["control"]) == (2, 1, 2, 1)
    assert (doc["active_token_hours"], doc["control_token_hours"]) == (2.0, 2.0)
    # 1 control hit / 2 token-h = 0.5/h -> 1 expected by chance in 2 active token-h; one more
    # candidate may be the wallet's own unmatched filled order -> net 2 - 1 - 1 = 0
    assert (tot["control_per_token_hour"], tot["expected_by_chance"]) == (0.5, 1.0)
    assert (tot["own_unmatched"], tot["net_estimate"]) == (1, 0.0)
    assert tot["known_match_own_share"] == 0.667 and tot["identity_precision_upper"] is None
    assert doc["per_size"]["98.0"] == tot
    by = {(r["token_id"], r["price"]): r for r in rows}
    assert set(by) == {("A", 0.44), ("B", 0.3)}
    assert all(r["attribution"] == "fingerprint_candidate" for r in rows)
    a, b = by[("A", 0.44)], by[("B", 0.3)]
    assert (a["outcome"], a["rest_max_ms"], a["rest_known"]) == ("unknown", 800, False)
    assert (b["outcome"], b["reason"], b["rest_max_ms"], b["rest_known"]) == ("unknown", "censored", None, False)


# ---------------------------------------------------------------- end to end
def test_cli_end_to_end(tmp_path, capsys):
    w = SegmentWriter(tmp_path / "tape", "clob_btc_5m")
    w.append("", tw=MS * 1_000_000, meta={"ctl": "connect", "url": "wss://x"})
    w.append("", tw=MS * 1_000_000, meta={"ctl": "subscribe", "n": 3})
    for ms, m in FRAMES:
        w.append(orjson.dumps([m]).decode(), tw=(MS + ms) * 1_000_000)
    w.append("PONG", tw=(MS + 9_500) * 1_000_000)
    w.close()
    rows = [r for o in CASES for r in orow(o)]
    late = order("late", "E", "BUY", 0.3, 97.0, 4_000_000, fills={"0xl": (1.0, T0 + 4001)})  # after --to
    skew = order("skew", "E", "BUY", 0.3, 97.0, 1_000, fills={"0xs": (1.0, T0 - 5)})  # stamped after its block
    rows += orow(late) + orow(skew) + orow(order("tk", "E", "BUY", 0.3, 5.0, 1_000, fills={"0xc1": (5.0, T0 + 3)}), "taker")
    op = tmp_path / "orders.jsonl"
    op.write_bytes(b"".join(orjson.dumps(r | {"verified": True}) + b"\n" for r in rows))
    orders, active, stats = load_orders(op, T0, T0 + 3600)
    assert [o.order_id for o in orders] == ["f", "c", "u", "z", "p"] and active == {W: {"A", "E"}}
    assert dict(stats) == {"screened:out_of_range": 1, "screened:bad_age": 1, "maker_orders": 5}
    assert round(orders[0].filled, 6) == 50.0  # BUY fill converted from USDC

    out = tmp_path / "out"
    rc = main(["--data", str(tmp_path / "tape"), "--orders", str(op), "--from", str(T0),
               "--to", str(T0 + 3600), "--out", str(out), "--min-orders", "1"])
    assert rc == 0
    doc = orjson.loads((out / "ordermatch.json").read_bytes())
    d = doc["wallets"][W]
    # z's and p's placebo windows end after the last frame (9 s of tape): nothing is observable
    assert (d["maker_orders"], d["observable"], d["matched_all"], d["diagnostic_rows"]) == (5, 0, 4, 1)
    assert d["outcomes"] == {"filled:chain": 1, "unknown:level_zero": 1, "unknown:level_below": 1,
                             "unknown:censored": 1}
    assert d["rest_ms_known"]["cancelled"] is None and d["rest_censored"] == 2
    assert d["fingerprint"]["sizes"] == [50.0, 97.0]  # 97 alone covers 4 of 5 < 90 %
    assert d["fingerprint"]["total"]["own"] == 3  # censored fill timing and SELL without a BUY view
    got = {r["order_id"]: r["outcome"] for r in map(orjson.loads, (out / "orders.jsonl").read_bytes().splitlines())}
    assert got == {"f": "filled", "c": "unknown", "u": "unknown", "z": "unknown", "p": "unknown"}
    assert (out / "quotes.jsonl").exists()
    assert "placement:" in capsys.readouterr().out
    # same answers as the in-memory tape
    doc2, rows2, _ = run(tmp_path / "tape", op, T0, T0 + 3600, [W])
    assert doc2["tape"]["reset:connect"] == 1 and {r["order_id"]: r["outcome"] for r in rows2} == got
