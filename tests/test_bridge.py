"""doppel-bridge: lead-lag events, S5 hedge-miss accounting, S6 rows, end to end on a tiny tape.
Every expected number below is computed by hand in the comments."""

from pathlib import Path

import numpy as np
import orjson
import pytest

from doppel.lab import bridge as br
from doppel.lab.bridge import Cfg, Series
from doppel.lab.kalshi_ws import Print
from doppel.tape.segment import SegmentWriter
from doppel.tape.universe import pmus_slug

NS = 10**9
S = 1795622400                      # 2026-11-25 16:00Z, a 15m window start
TK = "KXBTC15M-26NOV251115-15"      # closes 11:15 EST = 16:15Z


def at(t):
    return S * NS + round(t * NS)


def top(bid, ask, q=100.0):
    return (bid, q, ask, q)


def test_ticker_is_eastern_close():
    assert br.kalshi_start(TK) == S
    assert br.kalshi_start("KXBTC15M-26NOV251945-45") == 1795653000       # test_structural: open 00:30Z
    assert br.kalshi_start("KXBTC15M-26JUL011200-00") == 1782921600 - 900  # EDT: 12:00 EDT = 16:00Z
    assert br.kalshi_start("KXBTC15M-26NOV251115-30") is None
    assert br.kalshi_start("KXETH15M-26NOV251115-15") is None


def test_fee_and_quote():
    c = Cfg()  # size 10, coef 0.0695, rebate 0.70
    # fee_usd(0.0695, 0.5, 10) = 0.17375 -> 0.17; x 0.3 / 10 = 0.0051 per contract
    assert c.pm_fee(0.5) == pytest.approx(0.0051)
    assert br.quote_px(top(0.50, 0.52), 0, c) == 0.49       # floor(0.50 - 0.0051) = 0.49
    assert br.quote_px(top(0.50, 0.52), 2, c) == 0.47
    assert br.quote_px(top(0.01, 0.52), 0, c) is None       # below 1c
    assert br.quote_px(None, 0, c) is None
    assert Cfg(size=1).pm_fee(0.5) == pytest.approx(0.006)  # 1 lot: 0.0174 -> 0.02 x 0.3


# ------------------------------------------------------------------------------- lead-lag
def test_follow_events_kalshi_leads():
    k = Series([(at(0), top(0.49, 0.51)), (at(10), top(0.50, 0.52)), (at(20), top(0.49, 0.51))])
    p = Series([(at(0), top(0.49, 0.51)), (at(10.3), top(0.50, 0.52)), (at(20.9), top(0.48, 0.50)),
                (at(21.5), top(0.52, 0.54))])
    kp = br.follow_events(k, p, at(5), at(900))
    # K +1c at 10: P follows at 10.3 (0.3 s). K -1c at 20: P down at 20.9 (0.9 s), then up 2c over ref at 21.5
    assert [(e["dir"], e["same"], e["opp"]) for e in kp] == [(1.0, pytest.approx(0.3), None),
                                                             (-1.0, pytest.approx(0.9), pytest.approx(1.5))]
    pk = br.follow_events(p, k, at(5), at(900))
    # P +1c at 10.3 is a response (K moved up within the prior 1 s): excluded.
    # P -2c at 20.9 (K already moved down at 20, within the prior 1 s): excluded.
    # P +4c at 21.5: K did not move in [20.5, 21.5] and never follows.
    assert [(e["dir"], e["same"], e["opp"]) for e in pk] == [(1.0, None, None)]
    summ, n, same, opp = br._follow_table([kp])
    assert summ["events"] == 2 and summ["by_horizon"]["0.25s"]["p_same"] == 0.0
    assert summ["by_horizon"]["1s"] == {"p_same": 1.0, "p_opp": 0.0, "net": 1.0}
    assert summ["by_horizon"]["2s"] == {"p_same": 1.0, "p_opp": 0.5, "net": 0.5}
    assert summ["median_delay_s"] == pytest.approx(0.6)


def test_follow_events_stop_at_unknown_book():
    k = Series([(at(0), top(0.49, 0.51)), (at(10), top(0.50, 0.52))])
    p = Series([(at(0), top(0.49, 0.51)), (at(10.1), None), (at(10.2), top(0.50, 0.52))])
    assert [e["same"] for e in br.follow_events(k, p, at(5), at(900))] == [None]


def test_xcorr_peak_at_lead():
    rng = np.random.default_rng(1)
    mk = np.cumsum(rng.choice([-0.01, 0.0, 0.01], size=3000, p=[0.1, 0.8, 0.1]))
    mp = np.concatenate([np.full(3, mk[0]), mk[:-3]])          # PM US = Kalshi 0.3 s later
    c = br._corr(br.xcorr_sums(mk, mp))
    assert int(np.nanargmax(c)) - br.LAGS == 3 and c[br.LAGS + 3] == pytest.approx(1.0)
    assert br.lead_score(c, 10) > 0.7
    c2 = br._corr(br.xcorr_sums(mp, mk))
    assert int(np.nanargmax(c2)) - br.LAGS == -3 and br.lead_score(c2, 10) < -0.7


# ------------------------------------------------------------------------------- S5
def _s5_inputs():
    # PM US: bid 0.50 / ask 0.52 until 40.1 s, then bid 0.47 / ask 0.49
    pm = Series([(at(6), top(0.50, 0.52)), (at(40.1), top(0.47, 0.49))])
    # Kalshi: bid 0.48 / ask 0.51 throughout
    ks = Series([(at(0), top(0.48, 0.51))])
    kp = [Print(at(20), TK, 0.48, 5, "no"),      # through our 0.49 bid: fill 1
          Print(at(20.05), TK, 0.47, 5, "no"),   # same sweep, before re-arm at 20.1: ignored
          Print(at(30), TK, 0.49, 5, "no"),      # at our level: fill only under --fill touch
          Print(at(40), TK, 0.48, 5, "no")]      # fill 2, PM US bid drops 0.1 s later
    return pm, ks, kp


def test_s5_hand_computed():
    pm, ks, kp = _s5_inputs()
    fills, cnt = br.s5_window(pm, ks, kp, S, S + 900, 0, Cfg())
    assert [(f["t"], f["q"], f["via"], f["stale"]) for f in fills] == [(at(20), 0.49, "print", False),
                                                                     (at(40), 0.49, "print", False)]
    # fill 1: hedge at PM bid 0.50: net = 0.50 - 0.49 - 0.0051 = +0.49c (hit)
    # fill 2: hedge at PM bid 0.47: net = 0.47 - 0.49 - fee(0.47) [0.17324 -> 0.17 x 0.3 / 10 = 0.0051] = -2.51c
    assert fills[0]["hedge"][0.25]["net"] == pytest.approx(0.0049)
    assert fills[1]["hedge"][0.25]["net"] == pytest.approx(-0.0251)
    assert fills[1]["hedge"][1.0]["net"] == pytest.approx(-0.0251)
    assert cnt["placed"] == 4 and cnt["would_cross"] == 0   # 6.1, re-arm 20.1, re-arm 40.1, 40.2 (0.46)
    r = br.s5_summary([fills], 0, 0.25)
    assert (r["fills"], r["known"], r["hedge_unknown"], r["fills_via_print"]) == (2, 2, 0, 2)
    assert r["miss_rate"] == 0.5 and r["slip_given_miss_c"] == pytest.approx(2.51)
    assert r["expectancy_c"] == pytest.approx(-1.01)                 # (0.49 - 2.51) / 2
    assert r["net_given_hit_c"] == pytest.approx(0.49)
    assert r["expectancy_formula_c"] == pytest.approx(-1.255)        # (1 - .5) * 0 - .5 * 2.51
    assert r["depth_ok_share"] == 1.0 and r["miss_rate_ci"] is None  # one window: no cluster CI


def test_s5_touch_counts_at_level_print():
    pm, ks, kp = _s5_inputs()
    fills, _ = br.s5_window(pm, ks, kp, S, S + 900, 0, Cfg(fill="touch"))
    assert [f["t"] for f in fills] == [at(20), at(30), at(40)]


def test_s5_book_fill_cross_skip_and_unknown_hedge():
    # Kalshi ask 0.49 at the first decision (6.1): our 0.49 bid would take -> not placed (would_cross)
    # ask moves to 0.51 at 10 (no PM change, so no new decision) ...
    # PM US update at 30 re-decides: placed at 30.1. Kalshi ask drops to 0.49 at 50: book fill.
    # The re-arm at 50.1 would cross again (would_cross 2); PM US unknown from 50.1.
    ks = Series([(at(0), top(0.48, 0.49)), (at(10), top(0.48, 0.51)), (at(50), top(0.48, 0.49))])
    pm = Series([(at(6), top(0.50, 0.52)), (at(30), top(0.50, 0.53)), (at(50.1), None)])
    fills, cnt = br.s5_window(pm, ks, [], S, S + 900, 0, Cfg())
    assert cnt["would_cross"] == 2 and cnt["placed"] == 1
    assert [(f["t"], f["via"]) for f in fills] == [(at(50), "book")]
    # PM US book unknown from 50.1: hedge at 50.25 / 51 is unknown, neither hit nor miss
    assert fills[0]["hedge"] == {0.25: None, 1.0: None}
    r = br.s5_summary([fills], 0, 1.0)
    assert (r["fills"], r["known"], r["hedge_unknown"], r["miss_rate"], r["expectancy_c"]) == (1, 0, 1, None, None)


def test_s5_no_side_via_mirror():
    pm = Series([(at(6), top(0.50, 0.52))])
    ks = Series([(at(0), top(0.49, 0.54))])
    kp = [Print(at(20), TK, 0.54, 5, "yes")]   # YES buyer at 0.54, through our YES-ask 0.53
    fills, _ = br.s5_window(br._mirror(pm), br._mirror(ks), br._mirror_prints(kp), S, S + 900, 0, Cfg())
    # mirrored PM bid = 1 - 0.52 = 0.48 -> NO bid n_K = floor(0.48 - 0.0051) = 0.47 (YES-ask 0.53)
    # hedge: buy YES at PM ask 0.52: net = 1 - 0.47 - 0.52 - fee(0.52) [0.17347 -> 0.17] = +0.49c
    assert len(fills) == 1 and fills[0]["q"] == 0.47
    assert fills[0]["hedge"][0.25]["net"] == pytest.approx(0.0049)


def test_s5_kalshi_unknown_cancels_quote():
    pm = Series([(at(6), top(0.50, 0.52))])
    ks = Series([(at(0), top(0.48, 0.51)), (at(10), None), (at(11), top(0.48, 0.51))])
    fills, cnt = br.s5_window(pm, ks, [Print(at(20), TK, 0.40, 1, "no")], S, S + 900, 0, Cfg())
    assert fills == [] and cnt["cancel_k_unknown"] == 1


# ------------------------------------------------------------------------------- S6
def test_s6_rows():
    ks = Series([(at(0), top(0.48, 0.51)), (at(30.2), top(0.49, 0.50))])
    prints = [(at(30), 0.52, 3.0, "buy"), (at(40), 0.47, 1.0, "sell")]
    rows = {(r["print_side"], r["delta_s"]): r for r in br.s6_rows([(prints, ks)], [(S, S + 900)], Cfg())}
    assert rows[("buy", 0.0)]["rel_mean_c"] == pytest.approx(1.0)     # 0.52 - ask 0.51
    assert rows[("buy", 0.25)]["rel_mean_c"] == pytest.approx(2.0)    # 0.52 - ask 0.50 (book at 30.2)
    assert rows[("sell", 1.0)]["rel_mean_c"] == pytest.approx(2.0)    # bid 0.49 - 0.47
    # Kalshi taker fee at 0.50, 10 lots: ceil(0.07*10*0.25*100)/100/10 = 0.018 -> 1.8c < 2c
    assert rows[("buy", 0.25)]["share_profitable_after_kalshi_taker_fee"] == 1.0
    assert rows[("buy", 0.0)]["share_profitable_after_kalshi_taker_fee"] == 0.0
    assert rows[("buy", 5.0)]["n"] == 1 and rows[("buy", 5.0)]["windows"] == 1


# ------------------------------------------------------------------------------- end to end
def _md(slug, bid, ask):
    lv = lambda p: [{"px": {"value": f"{p:.4f}", "currency": "USD"}, "qty": "100.0000"}]  # noqa: E731
    return {"requestId": "md-15m-x-1", "marketData": {"marketSlug": slug, "bids": lv(bid), "offers": lv(ask),
                                                     "state": "MARKET_STATE_OPEN"}}


def _write(root: Path):
    slug = pmus_slug("btc", "15m", S)
    w = SegmentWriter(root, "pmus_ws_15m")
    for t, b, a in [(-30, 0.49, 0.51), (100.3, 0.50, 0.52), (200.4, 0.49, 0.51)]:
        w.append(orjson.dumps(_md(slug, b, a)).decode(), tw=at(t))
    w.append(orjson.dumps({"trade": {"marketSlug": slug, "price": {"value": "0.5200"}, "quantity": {"value": "2"},
                                     "taker": {"side": "ORDER_SIDE_BUY"}, "id": "t1",
                                     "state": "TRADE_STATE_NEW"}}).decode(), tw=at(300))
    w.close()
    k = SegmentWriter(root, "kalshi_ws")
    snap = {"type": "orderbook_snapshot", "sid": 1, "seq": 1,
            "msg": {"market_ticker": TK, "yes_dollars_fp": [["0.4900", "50.00"]], "no_dollars_fp": [["0.4900", "50.00"]]}}
    k.append(orjson.dumps(snap).decode(), tw=at(-700))
    seq = 2
    # Kalshi: mid .50 -> .505 -> .51 at 100 (bid .50, then NO .49 pulled: ask .52); back to .50 by 200.1
    for t, side, p, d in [(100, "yes", 0.50, 10), (100, "no", 0.48, 10), (100, "no", 0.49, -50),
                          (200, "no", 0.49, 50), (200.1, "yes", 0.50, -10)]:
        k.append(orjson.dumps({"type": "orderbook_delta", "sid": 1, "seq": seq,
                               "msg": {"market_ticker": TK, "price_dollars": f"{p:.4f}", "delta_fp": f"{d:.2f}",
                                       "side": side}}).decode(), tw=at(t))
        seq += 1
    # a YES seller at 0.48 on the trade sid: through our 0.49 bid (PM bid 0.50 -> q 0.49)
    k.append(orjson.dumps({"type": "trade", "sid": 2, "seq": 1,
                           "msg": {"market_ticker": TK, "yes_price_dollars": "0.4800", "no_price_dollars": "0.5200",
                                   "count_fp": "3.00", "taker_side": "no", "ts_ms": 1}}).decode(), tw=at(150))
    k.close()


def test_run_end_to_end(tmp_path: Path, capsys):
    _write(tmp_path)
    out = tmp_path / "o.json"
    assert br.main(["--data", str(tmp_path), "--from", str(S), "--to", "2026-11-25T16:15:00Z", "--out", str(out)]) == 0
    res = orjson.loads(out.read_bytes())
    assert [w["ticker"] for w in res["windows"]] == [TK] and res["skipped"] == []
    w = res["windows"][0]
    assert w["mid_gap_abs_c_p50"] == 0.0 and w["kalshi_prints"] == 1 and w["pm_prints"] == 1
    assert w["kalshi_crossed_share"] == 0.0
    assert res["kalshi_ws"]["stats"]["seq_gaps"] == 0 and res["kalshi_ws"]["stats"]["unparsed"] == 0
    ll = res["leadlag"]
    # Kalshi +1c at 100.0 (mid .50 -> .51 after both deltas), PM US follows at 100.3; down 1c at 200.1 -> PM 200.4
    assert ll["kalshi_to_pmus"]["events"] == 2 and ll["kalshi_to_pmus"]["median_delay_s"] == pytest.approx(0.3, abs=1e-6)
    assert ll["kalshi_to_pmus"]["by_horizon"]["1s"]["net"] == 1.0
    assert ll["pmus_to_kalshi"]["events"] == 0
    assert ll["peak_abs_corr_lag_s"] == 0.3 and ll["verdict_1s"].startswith("undetermined")
    assert {(r["side"], r["m_c"], r["L_ms"]) for r in res["s5"]} == {
        (s, m, L) for s in ("yes", "no") for m in (0, 1, 2) for L in (250, 1000)}
    s5 = {(r["side"], r["m_c"], r["L_ms"]): r for r in res["s5"]}
    # hedge at PM bid 0.50: net = 0.50 - 0.49 - 0.0051 = +0.49c
    assert (s5[("yes", 0, 250)]["fills"], s5[("yes", 0, 250)]["miss_rate"]) == (1, 0.0)
    assert s5[("yes", 0, 250)]["expectancy_c"] == pytest.approx(0.49)
    assert s5[("yes", 1, 250)]["fills"] == 0                          # q 0.48: print not through
    assert all(s5[("no", m, L)]["fills"] == 0 for m in (0, 1, 2) for L in (250, 1000))
    s6 = {(r["print_side"], r["delta_s"]): r for r in res["s6"]}
    assert s6[("buy", 0.0)]["rel_mean_c"] == pytest.approx(1.0)          # 0.52 - Kalshi ask 0.51
    assert "lead-lag" in capsys.readouterr().out


def test_s5_quotes_from_window_start_on_a_pre_window_book():
    pm = Series([(at(-30), top(0.50, 0.52))])     # last PM US change before the window
    ks = Series([(at(-30), top(0.48, 0.51))])
    fills, cnt = br.s5_window(pm, ks, [Print(at(20), TK, 0.48, 1, "no")], S, S + 900, 0, Cfg())
    assert cnt["placed"] == 2 and [f["t"] for f in fills] == [at(20)]   # placed at 5.0, re-armed at 20.1


def test_s5_dedup_one_fill_per_hedge_horizon():
    pm = Series([(at(6), top(0.50, 0.52))])
    ks = Series([(at(6), top(0.48, 0.51))])
    kp = [Print(at(20 + 0.15 * i), TK, 0.48, 1, "no") for i in range(4)]   # 20.0, 20.15, 20.3, 20.45
    fills, _ = br.s5_window(pm, ks, kp, S, S + 900, 0, Cfg())
    assert len(fills) == 4                                   # re-armed 100 ms after each fill
    r = br.s5_summary([fills], 0, 0.25)
    assert r["fills"] == 2 and r["raw"]["fills"] == 4        # counted: 20.0, 20.3
    assert br.s5_summary([fills], 0, 1.0)["fills"] == 1
