"""doppel.lab.perps decoders and doppel.lab.leadlag on fixtures shared with the Rust decoder
(crates/doppel-fair/tests/perp_fixtures.rs) and on a tiny synthetic tape."""

import math
from pathlib import Path

import numpy as np
import orjson
import pytest

from doppel.lab import leadlag as ll
from doppel.lab import perps
from doppel.tape.segment import SegmentWriter

FIX = Path(__file__).parent / "fixtures_perps"
NS = 10**9
S = 1795622400


def recs(name):
    return [orjson.loads(line) for line in (FIX / name).read_text().splitlines()]


def tops_after_each(dec, rs):
    out = []
    for r in rs:
        dec.feed(r)
        t = dec.top()
        out.append(None if t is None else [t.bid, t.bid_qty, t.ask, t.ask_qty])
    return out


def same_rows(got, exp):
    assert len(got) == len(exp)
    for g, e in zip(got, exp):
        assert len(g) == len(e) and all(x == pytest.approx(y) if isinstance(y, float) else x == y for x, y in zip(g, e)), (g, e)


def close(a, b):
    if a is None or b is None:
        return a is b
    return all(math.isclose(x, y, rel_tol=1e-12, abs_tol=1e-12) for x, y in zip(a, b))


def test_kalshi_ws_fixture():
    exp = orjson.loads((FIX / "kalshi_ws.expected.json").read_bytes())
    rs = recs("kalshi_ws.jsonl")
    dec = perps.KalshiPerp()
    got = tops_after_each(dec, rs)
    assert len(got) == len(exp["tops"])
    for i, (g, e) in enumerate(zip(got, exp["tops"])):
        assert close(g, e), (i, g, e)
    f = perps.decode(rs, "perp_kalshi_btc")
    same_rows([[t.tw, t.id, t.price, t.qty, t.taker_buy] for t in f.trades], exp["trades"])
    same_rows([[i.tw, i.index, i.mark, i.funding] for i in f.info], exp["info"])
    assert (f.stats["seq_gaps"], f.stats["trade_gaps"]) == (exp["seq_gaps"], exp["trade_gaps"])
    # decode() keeps only top changes
    assert [b[1] is None for b in f.book] == [True, False, False, False, True, False]


def test_cbde_ws_fixture_matches_expected():
    exp = orjson.loads((FIX / "cbde_ws.expected.json").read_bytes())
    rs = recs("cbde_ws.jsonl")
    got = tops_after_each(perps.CoinbaseL2(perps.CBDE_CONTRACT), rs)
    assert len(got) == len(exp["tops"])
    for i, (g, e) in enumerate(zip(got, exp["tops"])):
        assert close(g, e), (i, g, e)
    f = perps.decode(rs, "perp_cbde_btc")
    same_rows([[t.tw, t.id, t.price, t.qty, t.taker_buy] for t in f.trades], exp["trades"])
    # hand check: the first snapshot's touch (contracts x 0.01 BTC)
    assert exp["tops"][3] == [59820.0, 5.96, 59835.0, 1.82]
    assert f.stats["seq_gaps"] == 0


def test_coinbase_gap_and_snapshot_trades_skipped():
    def l2(seq, typ, ups):
        return {"tw": seq, "raw": orjson.dumps({"channel": "l2_data", "sequence_num": seq, "events": [
            {"type": typ, "product_id": "BIP-20DEC30-CDE",
             "updates": [{"side": s, "price_level": p, "new_quantity": q} for s, p, q in ups]}]}).decode()}

    def mt(seq, typ, ts):
        return {"tw": seq, "raw": orjson.dumps({"channel": "market_trades", "sequence_num": seq, "events": [
            {"type": typ, "trades": [{"trade_id": i, "price": "100", "size": "2", "side": s} for i, s in ts]}]}).decode()}

    d = perps.CoinbaseL2(1.0)
    d.feed(l2(0, "snapshot", [("bid", "99", "1"), ("offer", "101", "3")]))
    assert d.top() == perps.Top(99.0, 1.0, 101.0, 3.0)
    assert d.top().micro == pytest.approx((99 * 3 + 101 * 1) / 4)
    assert d.top().imbalance == pytest.approx(-0.5)
    tr, _ = d.feed(mt(1, "snapshot", [("a", "SELL")]))
    assert tr == []
    tr, _ = d.feed(mt(2, "update", [("a", "SELL"), ("b", "SELL"), ("c", "BUY")]))
    assert [(t.id, t.taker_buy, t.qty) for t in tr] == [("b", True, 2.0), ("c", False, 2.0)]
    d.feed(l2(3, "update", [("offer", "101", "0"), ("offer", "102", "1")]))
    assert d.top().ask == 102.0
    d.feed(l2(5, "update", [("bid", "100", "1")]))               # gap: 4 missing
    assert d.top() is None and d.stats["seq_gaps"] == 1
    d.feed(l2(6, "update", [("bid", "100", "1")]))
    assert d.top() is None                                       # until the next snapshot
    d.feed(l2(7, "snapshot", [("bid", "98", "1"), ("offer", "99", "1")]))
    assert d.top().bid == 98.0


def test_kalshi_rest_baseline_and_failed_poll():
    d = perps.KalshiPerp()

    def r(tw, key, body, status=200):
        return {"tw": tw, "raw": orjson.dumps(body).decode(), "meta": {"key": key, "status": status}}

    d.feed(r(1, "orderbook", {"orderbook": {"bids": [["5.9931", "4.00"]], "asks": [["5.9933", "2.00"], ["6.1", "1.00"]]}}))
    assert d.top() == perps.Top(pytest.approx(59931.0), pytest.approx(0.0004), pytest.approx(59933.0), pytest.approx(0.0002))
    d.feed(r(2, "orderbook", {"error": {"code": "too_many_requests"}}, 429))
    assert d.top() is None

    def trades(ids):
        return {"trades": [{"trade_id": i, "price": "6.0000", "count": "10.00", "taker_side": "ask"} for i in ids]}

    assert d.feed(r(3, "trades", trades(["b", "a"])))[0] == []          # baseline
    tr, _ = d.feed(r(4, "trades", trades(["d", "c", "b"])))
    assert [(t.id, t.price, t.qty, t.taker_buy) for t in tr] == [("c", 60000.0, 0.001, False), ("d", 60000.0, 0.001, False)]
    _, inf = d.feed(r(5, "funding", {"funding_rate": 0.0001, "mark_price": "5.9828"}))
    assert inf[0].funding == 0.0001 and inf[0].mark == pytest.approx(59828.0)


def test_per_second_is_asof():
    f = perps.Feed(book=[(S * NS + 500_000_000, perps.Top(100, 1, 102, 1)), ((S + 2) * NS, None)],
                   trades=[perps.Trade(S * NS + 1, "x", 101, 0.5, True), perps.Trade((S + 1) * NS, "y", 101, 0.2, False),
                           perps.Trade((S + 1) * NS + 1, "z", 101, 0.3, None)],
                   info=[perps.Info((S + 1) * NS, index=99.0, funding=1e-5)])
    p = perps.per_second(f, S, S + 4)
    # second S: the book arrives at S + 0.5 s, after the boundary
    assert np.isnan(p["mid"][0]) and p["mid"][1] == 101 and np.isnan(p["mid"][2])
    # flow over (d-1, d]: x (S+1ns) -> second S+1; y exactly at S+1 -> S+1; z -> S+2
    assert p["flow"].tolist() == pytest.approx([0.0, 0.3, 0.0, 0.0])
    assert p["flow_unknown"].tolist() == pytest.approx([0.0, 0.0, 0.3, 0.0])
    assert np.isnan(p["index"][0]) and p["index"][1] == 99.0 and p["funding"][3] == 1e-5


# ------------------------------------------------------------------------------------- leadlag
def write_stream(root, stream, recs_):
    w = SegmentWriter(root, stream)
    for tw, raw, meta in recs_:
        w.append(raw, tw=tw, tm=tw, meta=meta)
    w.close()


def l2(seq, typ, bid, ask, prev=None):
    """A one-level book at (bid, ask); an update first removes the previous touch ``prev``."""
    ups = [] if prev is None else [{"side": "bid", "price_level": str(prev[0]), "new_quantity": "0"},
                                   {"side": "offer", "price_level": str(prev[1]), "new_quantity": "0"}]
    ups += [{"side": "bid", "price_level": str(bid), "new_quantity": "1"},
            {"side": "offer", "price_level": str(ask), "new_quantity": "1"}]
    return orjson.dumps({"channel": "l2_data", "sequence_num": seq, "events": [{"type": typ, "updates": ups}]}).decode()


def test_leadlag_spot_leads_perp_by_300ms(tmp_path):
    """Coinbase spot steps; the CBDE perp copies each step 300 ms later. Expected: both the
    cross-correlation peak and the event follow say spot leads, with the peak at -0.3 s for
    X = perp (Y = spot moves BEFORE X)."""
    rng = np.random.default_rng(1)
    steps = np.cumsum(rng.choice([-6.0, 6.0], size=200))          # one step per second
    base = 60000.0
    spot, perp = [], []
    spot.append((S * NS, l2(0, "snapshot", base - 1, base + 1), None))
    perp.append((S * NS, l2(0, "snapshot", base - 1, base + 1), None))
    prev = (base - 1, base + 1)
    for i, d in enumerate(steps):
        m = base + d * 3                                           # 18 USD steps, 3 bps
        t = (S + 1 + i) * NS
        spot.append((t, l2(i + 1, "update", m - 1, m + 1, prev), None))
        perp.append((t + 300_000_000, l2(i + 1, "update", m - 1, m + 1, prev), None))
        prev = (m - 1, m + 1)
    write_stream(tmp_path, "spot_l2_coinbase", spot)
    write_stream(tmp_path, "perp_cbde_btc", perp)
    res = ll.run(tmp_path, S, S + 200, block_s=50, move=2.0, resp=1.0)
    assert set(res["pairs"]) == {"cbde_perp__coinbase_spot"}
    p = res["pairs"]["cbde_perp__coinbase_spot"]
    assert p["peak_abs_corr_lag_s"] == pytest.approx(-0.3)
    assert p["lead_score_1s"] < 0
    # every perp move answers a spot move of the last second: none is initiating, so the
    # follow difference is undefined; spot moves are followed by the perp after 0.3 s
    assert p["x_to_y"]["events"] == 0 and p["net_follow_diff_x_minus_y"]["1s"]["est"] is None
    assert p["y_to_x"]["events"] > 50 and p["y_to_x"]["by_horizon"]["1s"]["net"] == 1.0
    assert p["y_to_x"]["by_horizon"]["0.25s"]["net"] == 0.0
    assert p["y_to_x"]["median_delay_s"] == pytest.approx(0.3)
    assert abs(p["basis_bps_p50"]) < 1e-6
    assert res["sources"]["brti"]["seconds"] == 0
    assert ll.summary_lines(res)
