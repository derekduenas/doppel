import calendar
import math
import time
from pathlib import Path

import numpy as np
import orjson
import pytest

from doppel.lab.fair import fair_up_raw
from doppel.strategy.genome import (Condition, Costs, Exit, Genome, KillRule, MakerRule, Risk, Source, TakerRule,
                                    Universe, fee_c, to_dict)
from doppel.tape.segment import SegmentWriter
from doppel.tape.universe import pmus_slug
from doppel.twin import cli as twin_cli
from doppel.twin.engine import SimConfig, WindowSim, fee_usd, queue_ahead, run_windows
from doppel.twin.features import Features, WindowRef
from doppel.twin.tape import (NS, Book, Trade, WindowTape, iso_ns, iter_windows, rest_event, rid_group,
                              ws_event)
from doppel.twin.verdict import block_bootstrap_ci, decide, max_drawdown, verdict

FIX = orjson.loads((Path(__file__).parent / "fixtures_formats.json").read_bytes())
T0 = 1795046400          # UTC midnight
S = T0 + 3600            # a window start on an hour boundary
MS = 1_000_000


# ---------------------------------------------------------------- helpers
def _book(t_s, bids, asks, off_ms=0, vol=None):
    return Book(tw=int(t_s * NS) + off_ms * MS, bids=tuple(bids), asks=tuple(asks), shares_traded=vol)


def _trade(t_s, px, qty, aggr, off_ms=0, t_exch=None, tid=None):
    return Trade(tw=int(t_s * NS) + off_ms * MS, t_exch=t_exch, price=px, qty=qty, aggressor=aggr, id=tid)


def _wt(books, trades=(), gaps=(), start=S, source="ws"):
    return WindowTape(slug=pmus_slug("btc", "5m", start), asset="btc", horizon="5m", start=start, end=start + 300,
                      source=source, books=list(books), trades=list(trades), gaps=list(gaps))


def _ref(start=S, up=True, tie=False):
    settle = 100.0 if tie else (101.0 if up else 99.0)
    return WindowRef(asset="btc", start=start, end=start + 300, ptb=100.0, settle=settle)


class FairPath(Features):
    """Features with a scripted fair value (everything book-derived is computed for real)."""

    def __init__(self, g, ref, fair):
        super().__init__(g, ref)
        self._fair = fair

    def fair(self, d):
        return float(self._fair(d)) if callable(self._fair) else float(self._fair)


def _taker(side="up", when=None, size=10.0, slip=2.0, cooldown=5, max_entries=1, **kw):
    when = when or (Condition("tau_s", "between", 100, 100),)
    return Genome(name="t", mode="taker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
                  taker=(TakerRule(side=side, when=when, size=size, limit_slippage_c=slip, cooldown_s=cooldown,
                                   max_entries_per_window=max_entries),),
                  kill=KillRule(min_windows=30), created_tw=0, **kw)


def _maker(sides="bid_only", half=2.0, size=10.0, **kw):
    return Genome(name="m", mode="maker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
                  maker=MakerRule(quote_ref="fair", half_spread_c=half, size=size, sides=sides,
                                  requote_threshold_c=1.0, max_quote_age_s=30),
                  kill=KillRule(min_windows=30), created_tw=0, **kw)


def _sim(g, wt, ref=None, fair=0.5, cfg=SimConfig()):
    ref = ref or _ref()
    return WindowSim(g, wt, ref, cfg, features=FairPath(g, ref, fair)).run()


BASE = [_book(S - 1, [(0.48, 100.0)], [(0.52, 100.0)])]


# ---------------------------------------------------------------- parsing real shapes
def _ws_rec(r):
    if "msg" in r:
        return {"tw": r["tw"], "raw": orjson.dumps(r["msg"]).decode()}
    return {"tw": r["tw"], "raw": "", "meta": r["meta"]}


def test_parse_ws_fixture_book_trade_ctl():
    evs = [ws_event(_ws_rec(r)) for r in FIX["pmus_ws"]]
    books = [e for e in evs if e and e[0] == "book"]
    trades = [e for e in evs if e and e[0] == "trade"]
    ctls = [e[3] for e in evs if e and e[0] == "ctl"]
    assert books[0][1] == "cpc-btc-updown-5m-2026-11-25-2220z"
    b = books[0][2]
    assert isinstance(b, Book)
    assert b.best_bid == 0.42 and b.bids[0][1] == 1.0 and b.best_ask == 0.43 and b.asks[0][1] == 455.19
    assert b.bids[1] == (0.41, 1325.26) and b.open and b.shares_traded == 85770.2
    assert len(trades) == 2
    t1, t2 = trades[0][2], trades[1][2]
    assert isinstance(t1, Trade) and isinstance(t2, Trade)
    assert (t1.price, t1.qty, t1.aggressor, t1.id) == (0.42, 1.0, "sell", "DYPJ3JF055Q1")
    assert (t2.price, t2.qty, t2.aggressor) == (0.41, 71.27, "sell")
    assert trades[0][3] == "TRADE_STATE_NEW"
    exp = calendar.timegm((2026, 11, 25, 22, 22, 35, 0, 0, 0)) * NS + 324593105
    assert t1.t_exch == exp and t1.t_venue == min(exp, t1.tw)
    assert ctls == ["connect", "subscribe", "unsubscribe", "sub_error", "disconnect"]
    assert rid_group("md-5m-1795645200-1") == ("md", "5m", 1795645200)
    assert rid_group("garbage") is None


def test_parse_rest_fixture():
    r = FIX["pmus_book_rest"]
    rec = {"tw": 1795642222700000000, "raw": orjson.dumps(r["body"]).decode(), "meta": r["meta"]}
    ev = rest_event(rec)
    assert ev is not None and ev[0] == "book" and ev[1] == "cpc-btc-updown-5m-2026-11-25-2130z"
    b = ev[2]
    assert b is not None and b.best_bid == 0.50 and b.best_ask == 0.51 and b.asks[0][1] == 404.0
    bad = {**rec, "meta": {**r["meta"], "status": 503}}
    assert rest_event(bad) == ("gap", r["meta"]["key"], None)
    assert iso_ns("2026-11-25T21:30:02Z") == calendar.timegm((2026, 11, 25, 21, 30, 2, 0, 0, 0)) * NS


# ---------------------------------------------------------------- fees
def test_fee_rounding_half_even_per_fill():
    assert fee_usd(0.1, 0.5, 1) == 0.02          # 0.025 -> 0.02 (half to even)
    assert fee_usd(0.1, 0.5, 3) == 0.08          # 0.075 -> 0.08
    assert fee_usd(0.095, 0.5, 1) == 0.02        # 0.02375
    assert fee_usd(0.095, 0.51, 7) == 0.17       # 0.16618...
    assert fee_usd(0.0, 0.5, 100) == 0.0
    assert fee_usd(0.095, 0.5, 1, "none") == pytest.approx(0.02375)


# ---------------------------------------------------------------- taker
def test_taker_fills_at_post_latency_ask_with_fee():
    books = BASE + [_book(S + 99, [(0.48, 100.0)], [(0.50, 100.0)], off_ms=1),
                    _book(S + 100, [(0.48, 100.0)], [(0.51, 7.0)], off_ms=100)]   # arrives during our latency
    r = _sim(_taker(), _wt(books))
    assert r["n_fills"] == 1
    f = r["fills"][0]
    assert (f["role"], f["side"], f["px"], f["qty"], f["fee"]) == ("taker", "buy", 0.51, 7.0, 0.17)
    assert r["taker"]["depth_capped"] == 1
    assert r["net_usd"] == pytest.approx(7 * 0.49 - 0.17)
    assert r["final_inventory"] == 7.0


def test_taker_no_fill_when_price_moved_past_limit():
    books = BASE + [_book(S + 99, [(0.48, 100.0)], [(0.50, 100.0)], off_ms=1),
                    _book(S + 100, [(0.48, 100.0)], [(0.53, 100.0)], off_ms=200)]
    r = _sim(_taker(slip=2.0), _wt(books))
    assert r["n_fills"] == 0 and r["taker"]["slippage_miss"] == 1 and r["net_usd"] == 0.0
    # the same move inside the limit fills at the worse price
    r2 = _sim(_taker(slip=3.0), _wt(books))
    assert r2["fills"][0]["px"] == 0.53


def test_taker_book_after_landing_is_not_used():
    books = BASE + [_book(S + 99, [(0.48, 100.0)], [(0.50, 100.0)], off_ms=1),
                    _book(S + 100, [(0.48, 100.0)], [(0.60, 100.0)], off_ms=300)]   # after d + 250 ms
    r = _sim(_taker(slip=0.0), _wt(books))
    assert r["fills"][0]["px"] == 0.50


def test_down_is_sell_yes_at_bid():
    books = BASE + [_book(S + 90, [(0.40, 50.0)], [(0.42, 50.0)])]
    g = _taker(side="down")
    r = _sim(g, _wt(books), ref=_ref(up=False))
    f = r["fills"][0]
    assert (f["side"], f["px"], f["qty"]) == ("sell", 0.40, 10.0)
    assert r["final_inventory"] == -10.0
    assert r["notional_usd"] == pytest.approx(6.0)               # a Down share costs 1 - bid
    assert r["gross_usd"] == pytest.approx(4.0) and r["fees_usd"] == 0.23
    assert r["net_usd"] == pytest.approx(3.77)
    r_up = _sim(g, _wt(books), ref=_ref(up=True))
    assert r_up["gross_usd"] == pytest.approx(-6.0)


def test_settlement_tie_is_up_and_unsettled_is_excluded():
    books = BASE + [_book(S + 90, [(0.48, 100.0)], [(0.50, 100.0)])]
    ref = _ref(tie=True)
    assert ref.outcome == 1
    r = _sim(_taker(), _wt(books), ref=ref)
    assert r["gross_usd"] == pytest.approx(10 * 0.50)
    g = _taker()
    res = run_windows(g, [(_wt(books), WindowRef("btc", S, S + 300, ptb=100.0, settle=None))])
    assert res[0]["eligible"] is False and res[0]["excluded"] == "unsettled"


def test_taker_respects_inventory_and_cooldown():
    books = BASE + [_book(S + 1, [(0.48, 1000.0)], [(0.50, 1000.0)])]
    g = _taker(when=(Condition("tau_s", ">=", 10),), size=20, cooldown=10, max_entries=5,
               risk=Risk(max_inventory=50, max_notional_per_window=1000))
    r = _sim(g, _wt(books))
    assert [f["qty"] for f in r["fills"]] == [20.0, 20.0, 10.0]        # capped at |inv| <= 50
    assert [round(f["t"]) for f in r["fills"]] == [10, 20, 30]
    g2 = _taker(when=(Condition("tau_s", ">=", 10),), size=20, cooldown=10, max_entries=5,
                risk=Risk(max_inventory=50, max_notional_per_window=15))
    r2 = _sim(g2, _wt(books))
    assert r2["notional_usd"] <= 15 + 1e-9 and r2["contracts"] == 30.0


# ---------------------------------------------------------------- maker queue
def test_queue_ahead_rules():
    b = _book(S, [(0.48, 100.0), (0.47, 50.0), (0.45, 20.0)], [(0.52, 70.0)])
    assert queue_ahead(b, "bid", 0.48) == (100.0, True)
    assert queue_ahead(b, "bid", 0.49) == (0.0, True)       # better than best
    assert queue_ahead(b, "bid", 0.46) == (0.0, True)       # empty level inside the visible book
    assert queue_ahead(b, "bid", 0.40) == (math.inf, False)  # beyond visible depth
    assert queue_ahead(b, "ask", 0.52) == (70.0, True)


def test_maker_no_fill_while_queue_ahead_then_fills():
    trades = [_trade(S + 10, 0.48, 60.0, "sell"), _trade(S + 20, 0.48, 50.0, "sell"),
              _trade(S + 25, 0.48, 500.0, "buy")]                 # a BUY never fills our bid
    r = _sim(_maker(), _wt(BASE, trades))
    assert r["maker"]["placed"] >= 1
    assert len(r["fills"]) == 1
    f = r["fills"][0]
    assert (f["role"], f["side"], f["px"], f["qty"], f["via"]) == ("maker", "buy", 0.48, 10.0, "trade")
    assert round(f["t"]) == 20 and f["fee"] == 0.0
    # only 60 traded: still 40 ahead -> nothing
    r2 = _sim(_maker(), _wt(BASE, trades[:1]))
    assert r2["n_fills"] == 0


def test_maker_cancellations_do_not_advance_queue():
    books = BASE + [_book(S + 5, [(0.48, 10.0)], [(0.52, 100.0)])]     # 90 cancelled ahead of us
    trades = [_trade(S + 10, 0.48, 20.0, "sell")]
    r = _sim(_maker(), _wt(books, trades))
    assert r["n_fills"] == 0


def test_maker_trade_before_live_cannot_fill():
    books = [_book(S - 1, [(0.47, 100.0)], [(0.52, 100.0)])]             # our 0.48 bid is alone at the top
    trades = [_trade(S, 0.47, 1000.0, "sell", off_ms=100),              # before the quote lands (+250 ms)
              _trade(S, 0.47, 1000.0, "sell", off_ms=250),              # received exactly at landing
              _trade(S, 0.47, 1000.0, "sell", off_ms=300, t_exch=S * NS + 200 * MS)]  # executed before
    r = _sim(_maker(), _wt(books, trades))
    assert r["n_fills"] == 0
    r2 = _sim(_maker(), _wt(books, trades + [_trade(S + 1, 0.47, 4.0, "sell")]))
    assert [(f["qty"], f["px"]) for f in r2["fills"]] == [(4.0, 0.48)]   # through our price, no queue


def test_maker_through_print_fills_despite_queue():
    trades = [_trade(S + 10, 0.47, 30.0, "sell")]       # sweep below our level: the level was exhausted
    r = _sim(_maker(), _wt(BASE, trades))
    assert [(f["qty"], f["px"]) for f in r["fills"]] == [(10.0, 0.48)]


def test_maker_book_cross_fills_once():
    crossed = [(0.47, 4.0), (0.52, 100.0)]
    books = BASE + [_book(S + 5, [(0.46, 100.0)], crossed), _book(S + 6, [(0.46, 100.0)], crossed)]
    r = _sim(_maker(), _wt(books))
    assert [(f["qty"], f["via"]) for f in r["fills"]] == [(4.0, "book_cross")]


def test_maker_post_only_reject():
    r = _sim(_maker(), _wt(BASE), fair=0.55)       # bid 53 would cross the 52 ask
    assert r["maker"]["post_only_rejects"] > 0 and r["maker"]["placed"] == 0 and r["n_fills"] == 0


def test_replaced_quote_stays_live_until_cancel_lands():
    books = [_book(S - 1, [(0.47, 100.0)], [(0.55, 100.0)])]
    fair = lambda d: 0.5 if d < S + 10 else 0.40            # noqa: E731 - requote to 0.38 at S+10
    trades = [_trade(S + 10, 0.47, 5.0, "sell", off_ms=100),   # old 0.48 bid still live
              _trade(S + 10, 0.47, 5.0, "sell", off_ms=400)]   # after the replace landed: 0.38 is not hit
    r = _sim(_maker(), _wt(books, trades), fair=fair)
    assert [(f["qty"], f["px"]) for f in r["fills"]] == [(5.0, 0.48)]
    # a print executed before the cancel landed but received after it still fills the old quote
    late = [_trade(S + 10, 0.47, 5.0, "sell", off_ms=400, t_exch=S * NS + 10 * NS + 200 * MS)]
    r2 = _sim(_maker(), _wt(books, late), fair=fair)
    assert [(f["qty"], f["px"]) for f in r2["fills"]] == [(5.0, 0.48)]


def test_maker_needs_fair_and_trade_feed():
    r = _sim(_maker(), _wt(BASE, [_trade(S + 10, 0.47, 30.0, "sell")]), fair=float("nan"))
    assert r["n_fills"] == 0 and r["maker"]["no_fair"] > 0
    res = run_windows(_maker(), [(_wt(BASE, source="rest"), _ref()), (_wt([], source="none"), _ref())])
    assert [x["excluded"] for x in res] == ["stale_rest_diagnostic", "no_ws_coverage"]
    assert all(x["eligible"] is False for x in res)


def test_maker_windows_need_a_complete_trade_feed():
    g = _maker()
    ok = _wt(BASE, [_trade(S + 10, 0.47, 30.0, "sell")])
    ok.stats = {"trade_coverage": 0.97, "trade_gap_events": 0}
    gap = _wt(BASE, [_trade(S + 10, 0.47, 30.0, "sell")], gaps=[(S + 100) * NS])
    thin = _wt(BASE, [_trade(S + 10, 0.47, 30.0, "sell")])
    thin.stats = {"trade_coverage": 0.4}
    tgap = _wt(BASE)
    tgap.stats = {"trade_gap_events": 1}
    res = run_windows(g, [(ok, _ref()), (gap, _ref()), (thin, _ref()), (tgap, _ref())],
                      features_factory=lambda g_, ref, cfg: FairPath(g_, ref, 0.5))
    assert [r["excluded"] for r in res] == [None, "feed_gap", "incomplete_trade_feed", "feed_gap"]
    assert res[0]["maker"]["adverse_selection_c"]["1"] is not None
    # a taker is still scored on a window with a book gap (the book is just unknown during it)
    assert run_windows(_taker(), [(gap, _ref())])[0]["eligible"] is True


def test_markouts_use_settlement_after_close():
    books = BASE + [_book(S + 90, [(0.48, 100.0)], [(0.50, 100.0)]),
                    _book(S + 101, [(0.58, 100.0)], [(0.60, 100.0)])]
    g = _taker(when=(Condition("tau_s", "between", 100, 100),))
    r = _sim(g, _wt(books))
    mo = r["fills"][0]["markout_c"]
    assert mo["1"] == pytest.approx(59 - 50) and mo["30"] == pytest.approx(59 - 50)
    g2 = _taker(when=(Condition("tau_s", "between", 290, 290),))
    r2 = _sim(g2, _wt(books))
    assert r2["fills"][0]["markout_c"]["30"] == pytest.approx(100 - 60)     # past the close: settlement


# ---------------------------------------------------------------- synthetic markets with a true fair
def _synthetic(n, market, seed=3, probs=None):
    rng = np.random.default_rng(seed)
    base = T0 + 7200
    t0 = base - 1000
    raw = 90000 + np.cumsum(rng.normal(0, 3.0, n * 300 + 1200))
    c = np.concatenate([[0.0], np.cumsum(raw)])

    def A(t):
        i = t - t0
        return float((c[i + 1] - c[i - 59]) / 60.0)

    items = []
    for k in range(n):
        st = base + k * 300
        end = st + 300
        ptb, settle = A(st - 1), A(end - 1)
        books = []
        for s in range(st - 2, end):
            if market == "flat":
                bid, ask = 0.49, 0.51
            else:
                p = fair_up_raw(raw, t0, ptb, s, end)
                m = 50 if p is None else int(round(100 * p))
                if probs is not None:
                    probs[s] = 0.5 if p is None else p
                bid_c = min(97, max(1, m - 1))
                bid, ask = bid_c / 100, (bid_c + 2) / 100
            books.append(Book(tw=s * NS + 10 * MS, bids=((bid, 1000.0),), asks=((ask, 1000.0),)))
        r0 = st - 300 - 120
        ref = WindowRef("btc", st, end, ptb=ptb, settle=settle, raw=raw[r0 - t0:end - t0 + 1].copy(), raw_t0=r0)
        items.append((_wt(books, start=st), ref))
    return items


def test_edge_genome_on_mispriced_market_with_true_fair_is_positive():
    g = Genome(name="edge", mode="taker", universe=Universe(venue="pmus", tte_min_s=10), source=Source(kind="manual"),
               taker=(TakerRule(side="up", when=(Condition("edge_buy_up_c", ">", 4.0),), size=10, max_entries_per_window=1),
                      TakerRule(side="down", when=(Condition("edge_buy_down_c", ">", 4.0),), size=10,
                                max_entries_per_window=1)),
               risk=Risk(max_loss_per_day=1e9), kill=KillRule(min_windows=30), created_tw=0)
    res = run_windows(g, _synthetic(80, "flat"))
    v = verdict(g, res, block=4, n_boot=500)
    assert v["windows_traded"] >= 30
    assert v["total_net_usd"] > 0 and v["net_c_per_contract"]["value"] > 5
    assert v["net_c_per_window"]["ci95"][0] > 0 and v["decision"] == "PASS"


def test_random_entry_on_fair_market_loses_spread_and_fees_only():
    g = Genome(name="coin", mode="taker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
               taker=(TakerRule(side="up", when=(Condition("brti_move_1s", ">", 0.0),
                                                 Condition("mid_up_c", "between", 20, 80),
                                                 Condition("tau_s", "between", 30, 270)),
                                size=10, cooldown_s=40, max_entries_per_window=3),),
               risk=Risk(max_loss_per_day=1e9), kill=KillRule(min_windows=30), created_tw=0)
    probs: dict = {}
    res = run_windows(g, _synthetic(150, "fair", seed=5, probs=probs))
    ev = [r for r in res if r["contracts"] > 0]
    # zero-noise check: each fill priced against the generator's true P(Up) at the book it hit
    # (books arrive at s + 10 ms, so the fill at time t used second floor(t - 0.01))
    exp_c = [(100 * probs[math.floor(r["start"] + f["t"] - 0.01)] - 100 * f["px"], f["qty"])
             for r in ev for f in r["fills"]]
    exp_pc = sum(v * w for v, w in exp_c) / sum(w for _, w in exp_c)
    assert abs(exp_pc - (-1.0)) < 0.5                    # pays exactly the half spread, no leak
    gross_c = np.array([100 * r["gross_usd"] for r in ev])
    ctr = np.array([r["contracts"] for r in ev])
    gpc = gross_c.sum() / ctr.sum()
    se = (gross_c / ctr).std(ddof=1) / math.sqrt(len(ev))
    assert len(ev) > 100
    assert abs(gpc - (-1.0)) < 4 * se                     # no edge: lose half the 2c spread
    fees = sum(r["fees_usd"] for r in ev)
    assert fees > 0
    v = verdict(g, res, block=4, n_boot=500)
    assert v["net_c_per_contract"]["value"] == pytest.approx(gpc - 100 * fees / ctr.sum(), abs=1e-4)
    assert v["decision"] != "PASS"


# ---------------------------------------------------------------- verdict logic
def test_decide_pass_kill_continue():
    g = _taker()   # min_windows 30, pass > 0, kill < 0, max dd 25
    assert decide(g, 100, (1.0, 3.0), 5.0)[0] == "PASS"
    assert decide(g, 100, (-3.0, -1.0), 5.0)[0] == "KILL"
    assert decide(g, 100, (-1.0, 1.0), 5.0)[0] == "CONTINUE"
    assert decide(g, 10, (1.0, 3.0), 5.0)[0] == "CONTINUE"      # too few traded windows
    assert decide(g, 10, (1.0, 3.0), 30.0)[0] == "KILL"         # drawdown kills at any time
    assert decide(g, 100, (None, None), 0.0)[0] == "CONTINUE"


def test_bootstrap_and_drawdown():
    v = np.arange(100, dtype=float)
    lo, hi = block_bootstrap_ci(v, None, block=10, n_boot=400, seed=1)
    assert lo is not None and hi is not None and lo < 49.5 < hi
    assert block_bootstrap_ci(v, None, 10, 400, 1) == (lo, hi)            # seeded
    assert block_bootstrap_ci(np.full(20, 3.0)) == (3.0, 3.0)
    lo2, hi2 = block_bootstrap_ci(np.full(20, 6.0), np.full(20, 2.0))
    assert lo2 == pytest.approx(3.0) and hi2 == pytest.approx(3.0)
    assert max_drawdown([1, -3, 2, -5, 10]) == pytest.approx(6.0)
    assert max_drawdown([-2, 1]) == pytest.approx(2.0)


def test_determinism():
    g = Genome(name="edge", mode="taker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
               taker=(TakerRule(side="up", when=(Condition("edge_buy_up_c", ">", 4.0),), size=10),),
               kill=KillRule(min_windows=30), created_tw=0)
    a = run_windows(g, _synthetic(12, "flat", seed=9))
    b = run_windows(g, _synthetic(12, "flat", seed=9))
    assert orjson.dumps(a) == orjson.dumps(b)
    assert orjson.dumps(verdict(g, a, n_boot=300)) == orjson.dumps(verdict(g, b, n_boot=300))


def test_daily_stop_stands_down():
    books = BASE + [_book(S + 90, [(0.48, 100.0)], [(0.50, 100.0)])]
    g = _taker(size=100, risk=Risk(max_inventory=100, max_notional_per_window=100, max_loss_per_day=25))
    w2 = S + 300
    books2 = [Book(tw=b.tw + 300 * NS, bids=b.bids, asks=b.asks) for b in books]
    res = run_windows(g, [(_wt(books), _ref(up=False)), (_wt(books2, start=w2), _ref(start=w2, up=True))])
    assert res[0]["net_usd"] < -25 and res[1]["stood_down"] is True and res[1]["n_fills"] == 0


# ---------------------------------------------------------------- tape files end to end
def _md(slug, bid, ask, vol=0.0):
    return {"requestId": "md-5m-x-1", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
            "marketData": {"marketSlug": slug, "bids": [{"px": {"value": f"{bid:.4f}", "currency": "USD"}, "qty": "100.0000"}],
                           "offers": [{"px": {"value": f"{ask:.4f}", "currency": "USD"}, "qty": "100.0000"}],
                           "state": "MARKET_STATE_OPEN", "stats": {"sharesTraded": f"{vol:.4f}"}}}


def _tr(slug, px, qty, side, tid, t_s):
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_s)) + ".000000000Z"
    return {"requestId": "tr-5m-x-2", "subscriptionType": "SUBSCRIPTION_TYPE_TRADE",
            "trade": {"marketSlug": slug, "price": {"value": f"{px:.4f}"}, "quantity": {"value": f"{qty:.4f}"},
                      "tradeTime": iso, "maker": {"side": "ORDER_SIDE_BUY"}, "taker": {"side": side}, "id": tid,
                      "state": "TRADE_STATE_NEW"}}


def _write_tape(data):
    rng = np.random.default_rng(4)
    lo, hi = S - 1500, S + 1300
    raw = {t: 90000 + float(v) for t, v in zip(range(lo, hi), np.cumsum(rng.normal(0, 3, hi - lo)))}
    w = SegmentWriter(data, "ref_brti_raw")
    for t, v in raw.items():
        w.append(orjson.dumps({"t_ms": t * 1000, "v": str(v)}).decode(), tw=t * NS)
    w.close()
    series = {t: sum(raw[s] for s in range(t - 59, t + 1)) / 60 for t in range(lo + 60, hi)}
    w = SegmentWriter(data, "ref_prices")
    w.append(orjson.dumps({"series": [{"candles": [{"timestamp": t, "close": v} for t, v in series.items()]}]}).decode(),
             tw=hi * NS, meta={"asset": "crypto:btc", "status": 200, "from": lo + 60, "to": hi - 1})
    w.close()
    s1, s2 = pmus_slug("btc", "5m", S), pmus_slug("btc", "5m", S + 300)
    w = SegmentWriter(data, "pmus_ws_5m")
    w.append("", tw=(S - 400) * NS, meta={"ctl": "connect", "url": "wss://x"})
    w.append(orjson.dumps(_md(s1, 0.48, 0.52)).decode(), tw=(S - 20) * NS)       # previous hour's file
    for s in range(S, S + 300, 10):
        w.append(orjson.dumps(_md(s1, 0.48, 0.52, vol=30.0 if s > S + 50 else 0.0)).decode(),
                 tw=s * NS + 5 * MS)
    w.append(orjson.dumps(_tr(s1, 0.47, 30.0, "ORDER_SIDE_SELL", "T1", S + 50)).decode(), tw=(S + 50) * NS + 7 * MS)
    w.close()
    w = SegmentWriter(data, "pmus_ws")      # legacy stream replays the same trade id: deduplicated
    w.append(orjson.dumps(_tr(s1, 0.47, 30.0, "ORDER_SIDE_SELL", "T1", S + 50)).decode(), tw=(S + 50) * NS + 9 * MS)
    w.close()
    w = SegmentWriter(data, "pmus_books")   # window 2 only on (stale) REST
    for s in range(S + 300, S + 600):
        w.append(orjson.dumps({"marketData": {"marketSlug": s2, "bids": [{"px": {"value": "0.4800"}, "qty": "50"}],
                                              "offers": [{"px": {"value": "0.5000"}, "qty": "50"}],
                                              "state": "MARKET_STATE_OPEN"}}).decode(),
                 tw=s * NS + 300 * MS, meta={"key": s2, "url": "u", "status": 200, "sent_tw": s * NS})
    w.close()


def test_iter_windows_ws_preferred_rest_gated_and_prestart_book(tmp_path):
    data = tmp_path / "data"
    _write_tape(data)
    for t_from in (S - 300, S):          # carry path and discontinuity path both see the pre-start book
        ws = {w.start: w for w in iter_windows(data, ("btc",), ("5m",), t_from, S + 600)}
        w1, w2 = ws[S], ws[S + 300]
        assert w1.source == "ws" and w1.stream == "pmus_ws_5m"
        assert w1.books[0].tw == (S - 20) * NS and len(w1.books) == 31
        assert len(w1.trades) == 1 and w1.stats["trades_dup"] == 1
        assert w1.stats["trade_coverage"] == 1.0 and w1.stats["venue_volume"] == 30.0
        assert w1.gaps == []
        assert w2.source == "none" and w2.books == []
    w2r = [w for w in iter_windows(data, ("btc",), ("5m",), S, S + 600, allow_stale_rest=True)][1]
    assert w2r.source == "rest" and len(w2r.books) == 300


def test_cli_end_to_end(tmp_path, capsys):
    data = tmp_path / "data"
    _write_tape(data)
    g = _maker(sides="bid_only", risk=Risk(max_inventory=50), costs=Costs())
    gp = tmp_path / "g.json"
    gp.write_bytes(orjson.dumps(to_dict(g)))
    out = tmp_path / "out.json"
    rc = twin_cli.main(["--data", str(data), "--genome", str(gp), "--from", str(S), "--to", str(S + 600),
                        "--out", str(out), "--boot", "200"])
    assert rc == 0
    doc = orjson.loads(out.read_bytes())
    assert doc["genome"]["id"] == g.id and len(doc["twin"]["code_sha256"]) == 64
    w1, w2 = doc["windows"]
    assert w1["eligible"] and w1["source"] == "ws" and w2["excluded"] == "no_ws_coverage"
    assert doc["verdict"]["decision"] == "CONTINUE" and doc["verdict"]["excluded"] == {"no_ws_coverage": 1}
    printed = capsys.readouterr().out
    assert "QUOTE bid_only" in printed and "CONTINUE" in printed
    # same input, same output
    out2 = tmp_path / "out2.json"
    twin_cli.main(["--data", str(data), "--genome", str(gp), "--from", str(S), "--to", str(S + 600),
                   "--out", str(out2), "--boot", "200"])
    assert out.read_bytes() == out2.read_bytes()


def test_features_are_as_of_delayed_brti():
    items = _synthetic(1, "flat", seed=2)
    wt, ref = items[0]
    g = _taker()
    f = Features(g, ref)
    d = ref.start + 120
    x = f.at(d, wt.books[0], 0.0)
    assert ref.raw is not None
    exp = fair_up_raw(ref.raw, ref.raw_t0, ref.ptb, d - 1, ref.end, 0, 300)
    assert x["fair_up"] == pytest.approx(exp)
    assert x["brti_move_1s"] == pytest.approx(ref.raw[d - 1 - ref.raw_t0] - ref.raw[d - 2 - ref.raw_t0])
    assert x["edge_buy_up_c"] == pytest.approx(100 * (exp - 0.51) - fee_c(0.51, 0.095))
    assert x["edge_buy_down_c"] == pytest.approx(100 * ((1 - exp) - 0.51) - fee_c(0.51, 0.095))
    assert x["mid_up_c"] == pytest.approx(50.0) and x["spread_c"] == pytest.approx(2.0)
    eth = Features(g, WindowRef("eth", ref.start, ref.end, ptb=1.0, settle=1.0)).at(d, wt.books[0], 0.0)
    assert math.isnan(eth["fair_up"]) and math.isnan(eth["edge_buy_up_c"]) and eth["mid_up_c"] == 50.0


def test_uses_intl_detects_flow_features():
    from doppel.strategy.genome import Condition, Genome, MakerRule, Source, Universe
    from doppel.twin.flowfeat import uses_intl
    base = dict(name="m", mode="maker", universe=Universe(venue="pmus"), source=Source(kind="manual"))
    assert not uses_intl(Genome(**base, maker=MakerRule()))
    g = Genome(**base, maker=MakerRule(pause_when=(Condition("intl_informed_flow_30s", ">", 50.0),)))
    assert uses_intl(g)


def test_placebo_u_is_uniform_and_deterministic():
    from doppel.twin.features import placebo_u
    xs = [placebo_u(1795622400, d) for d in range(1795622400, 1795622400 + 20000)]
    assert xs[:5] == [placebo_u(1795622400, d) for d in range(1795622400, 1795622405)]
    assert all(0 <= x < 1 for x in xs)
    rate = sum(x < 0.1742 for x in xs) / len(xs)
    assert abs(rate - 0.1742) < 0.01
