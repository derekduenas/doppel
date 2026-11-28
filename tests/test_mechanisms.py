import numpy as np
import orjson

from doppel.lab import mechanisms as mech
from doppel.lab.markout import NS, TIERS, MarketPath
from test_markout import S, write_tape

N_BOOT = 1000
NULL_BOOT = 400
NULL_SEEDS = 20
MAX_FALSE = 3          # Bonferroni at family alpha 0.05: expect ~1 false study-level effect in 20 seeds


def _p(start, books, outcome, fair=None, index=None, length=300, depth=1000.0):
    """books: (t, bid, ask) with ``depth`` contracts on one level each side, or (t, bid_levels, ask_levels)."""
    p = MarketPath(key=str(start), start=start, end=start + length, outcome=outcome,
                   cluster=start // 900, fair=fair, index=index)
    for t, b, a in books:
        bl = b if isinstance(b, tuple) else ((b, depth),)
        al = a if isinstance(a, tuple) else ((a, depth),)
        p.tws.append(int(round(t * NS)))
        p.bids.append(bl[0][0]); p.asks.append(al[0][0])
        p.bid_lv.append(bl); p.ask_lv.append(al)
    return p


def _false_effects(make, study, key):
    hits = []
    for s in range(NULL_SEEDS):
        r = mech.run_paths(make(1000 + s), (study,), NULL_BOOT)["studies"][study]
        hits.append(key(r))
    print(f"{study}: false effects {sum(hits)}/{NULL_SEEDS}")
    return sum(hits)


# ---------------------------------------------------------------- execution: L2 walk, latency, fees
def test_thin_book_makes_500_miss():
    st = S
    lv_b, lv_a = ((0.49, 80.0), (0.48, 40.0)), ((0.51, 100.0), (0.52, 50.0))
    p = _p(st, [(st - 1, lv_b, lv_a)], 1)
    assert mech.execute(p, st + 10, "buy", 25)[0] == 0.51
    assert abs(mech.execute(p, st + 10, "buy", 100)[0] - 0.51) < 1e-12
    assert mech.execute(p, st + 10, "buy", 500) is None                 # 150 shown: missed, not partial
    assert abs(mech.execute(p, st + 10, "sell", 100)[0] - (0.49 * 80 + 0.48 * 20) / 100) < 1e-12
    legs = mech._legs(p, st + 10, "buy", {"settle": 100.0}, "x")
    assert (legs["x_fill_25"], legs["x_fill_100"], legs["x_fill_500"]) == (1.0, 1.0, 0.0)
    assert all(legs[f"x_settle_500_{t}"] is None for t in TIERS)


def test_landing_worse_than_decision_vwap_is_missed():
    st = S
    p = _p(st, [(st - 1, 0.49, 0.51), (st + 10.1, 0.49, 0.52)], 1)
    assert mech.execute(p, st + 10, "buy", 25) is None                  # repriced within 250 ms
    assert mech.execute(p, st + 10, "sell", 25)[0] == 0.49               # same bid: fills


def test_fee_tiers_edge_15c_at_50c():
    """+1.5 c of edge at 50 c (100 contracts): positive under api_live_rebated and maker_0, negative
    under gateway_0095 (and api_base_0695)."""
    p = _p(S, [(S - 1, 0.49, 0.50)], 1)
    legs = mech._legs(p, S + 10, "buy", {"m": 51.5}, "x")
    net = {t: legs[f"x_m_100_{t}"] for t in TIERS}
    assert net["gateway_0095"] < 0 and net["api_base_0695"] < 0
    assert net["api_live_rebated"] > 0 and net["maker_0"] > 0
    assert abs(net["gateway_0095"] + 0.88) < 1e-9 and abs(net["api_live_rebated"] - 0.978) < 1e-9
    multi = mech.walk_cost(((0.50, 50.0), (0.51, 50.0)), 100)        # one cent-rounded fee per level
    assert multi is not None and abs(multi[1]["gateway_0095"] - 100 * (1.19 + 1.19) / 100) < 1e-9


# ---------------------------------------------------------------- (a) favourite-longshot
def _flb_paths(b, seed, n=900):
    r = np.random.default_rng(seed)
    out = []
    for i in range(n):
        st = S + 300 * i
        m = round(float(r.uniform(0.05, 0.95)), 2)
        q = float(np.clip(m + b * (m - 0.5), 0.0, 1.0))
        out.append(_p(st, [(st - 1, m - 0.01, m + 0.01)], int(r.random() < q)))
    return out


def test_flb_recovers_planted_slope():
    res = mech.run_paths(_flb_paths(0.3, 1), ("flb",), N_BOOT)["studies"]["flb"]
    assert res["effect_any"]
    for c in res["cells"].values():
        h = c["headline_slope"]
        assert c["effect"] and h["ci"][0] > 0 and abs(h["mean"] - 0.3) < 0.12
        assert c["capacity"]["buy_yes"]["500"]["fill_rate"] == 1.0
        assert set(c["capacity"]["buy_yes"]["100"]["net_c"]["settle"]) == set(TIERS)
    assert res["prereg"]["grid_size"] == 3
    bk = res["cells"]["0.5"]["buckets"]
    assert sum(v["n"] for v in bk.values()) == 900


def test_flb_null_false_effects_over_20_seeds():
    assert _false_effects(lambda s: _flb_paths(0.0, s, 400), "flb", lambda r: r["effect_any"]) <= MAX_FALSE


# ---------------------------------------------------------------- (b) final 60 s
def _final_paths(market_noise, fair_noise, seed, n=400):
    r = np.random.default_rng(seed)
    out = []
    for i in range(n):
        st = S + 300 * i
        q = float(r.uniform(0.1, 0.9))
        m = round(float(np.clip(q + r.normal(0, market_noise), 0.03, 0.97)), 2)
        fv = float(np.clip(q + r.normal(0, fair_noise), 0.01, 0.99))
        out.append(_p(st, [(st + 200, m - 0.01, m + 0.01)], int(r.random() < q), fair=lambda t, v=fv: v))
    return out


def test_final60_gap_pays_when_fair_is_right():
    res = mech.run_paths(_final_paths(0.12, 0.0, 2), ("final60",), N_BOOT)["studies"]["final60"]
    assert all(res["effect_any_by_tier"].values())
    c = res["cells"]["10"]
    assert all(c["effect_by_tier"].values())
    assert c["headline_trade_net_c_at_100"]["gateway_0095"]["mean"] > 3
    assert c["abs_gap_c"]["mean"] > 5 and c["brier_mid_minus_fair_x1e4"]["ci"][0] > 0
    assert res["prereg"]["grid_size"] == 12
    cap = c["capacity"]
    assert cap["500"]["by_tier"]["maker_0"]["fill_rate"] == 1.0 and cap["500"]["no_depth"] == 0


def test_final60_null_false_effects_over_20_seeds():
    # noise in fair: trading the gap does not pay under any tier
    assert _false_effects(lambda s: _final_paths(0.0, 0.12, s, 300), "final60",
                          lambda r: any(r["effect_any_by_tier"].values())) <= MAX_FALSE


def test_final60_gap_that_reprices_within_latency_is_missed():
    st = S
    t = st + 300 - 10
    p = _p(st, [(st + 200, 0.40, 0.42), (t + 0.1, 0.79, 0.81)], 1, fair=lambda _: 0.8)
    (r,) = [x for x in mech.final60_rows(p) if x["cell"] == 10]
    assert r["fired_100_gateway_0095"] == 1.0 and r["pnl_100_gateway_0095"] is None
    q = _p(st, [(st + 200, 0.40, 0.42)], 1, fair=lambda _: 0.8)
    (r2,) = [x for x in mech.final60_rows(q) if x["cell"] == 10]
    assert r2["pnl_100_gateway_0095"] is not None and r2["pnl_100_gateway_0095"] > 50


def test_final60_thin_book_no_depth_at_500():
    st = S
    p = _p(st, [(st + 200, 0.40, 0.42)], 1, fair=lambda _: 0.8, depth=200)
    (r,) = [x for x in mech.final60_rows(p) if x["cell"] == 10]
    assert r["nodepth_500"] == 1.0 and r["fired_500_maker_0"] == 0.0 and r["pnl_500_maker_0"] is None
    assert r["pnl_100_maker_0"] is not None


# ---------------------------------------------------------------- (c) overreaction
EPS = 1e-5


def _idx(st, jump):
    """BRTI with +-EPS alternating 1-s noise (sigma = EPS) and a step of ``jump`` at st + 100."""
    return lambda t: 100.0 * (1 + EPS * (int(t) % 2)) * (jump if t >= st + 100 else 1.0)


def _or_paths(revert, seed, n=300):
    r = np.random.default_rng(seed)
    out = []
    for i in range(n):
        st = S + 300 * i
        up = i % 2 == 0
        sg = 1 if up else -1
        m0, m1 = 0.5 - sg * 0.05, 0.5 + sg * 0.06
        m2 = m1 - sg * revert + r.normal(0, 0.02)
        books = [(st - 1, m0 - 0.01, m0 + 0.01), (st + 100, m1 - 0.01, m1 + 0.01),
                 (st + 130, round(m2 - 0.01, 3), round(m2 + 0.01, 3))]
        out.append(_p(st, books, int(r.random() < 0.5), index=_idx(st, 1.006 if up else 0.994)))
    return out


def test_overreaction_planted_reversal():
    res = mech.run_paths(_or_paths(0.05, 4), ("overreaction",), N_BOOT)["studies"]["overreaction"]
    for name in mech.TRIGGERS:
        c = res["cells"][name]
        assert c["n_events"] == 300 and c["effect"] and c["reading"] == "overreaction"
        assert abs(c["headline_dm_60s_c"]["mean"] + 5) < 0.5
        f60, d60 = c["fade"]["100"]["net_c"]["60s"], c["follow"]["100"]["net_c"]["60s"]
        assert f60["maker_0"]["mean"] > 0 > d60["maker_0"]["mean"]
        assert f60["gateway_0095"]["mean"] < f60["api_live_rebated"]["mean"] < f60["maker_0"]["mean"]
    assert res["prereg"]["grid_size"] == 3


def test_overreaction_null_false_effects_over_20_seeds():
    assert _false_effects(lambda s: _or_paths(0.0, s, 200), "overreaction", lambda r: r["effect_any"]) <= MAX_FALSE


def test_z_uses_pre_move_vol_and_remaining_time():
    st = S
    p = _p(st, [(st - 1, 0.49, 0.51)], 1, index=_idx(st, 1.006))
    zs = dict(mech.z_series(p))
    assert min(zs) == st + mech.OPEN_MASK_S and max(zs) == st + 300 - 61
    assert max(abs(z) for s, z in zs.items() if s < st + 100) < 0.2       # noise alone: no trigger
    z = zs[st + 100]
    assert abs(z - np.log(1.006) / (EPS * np.sqrt(200))) / z < 0.05      # sigma from before the move
    flat = _p(st, [(st - 1, 0.49, 0.51)], 1, index=lambda t: 100.0)       # sigma = 0: never a trigger
    assert mech.z_series(flat) == []


def test_small_move_does_not_trigger_z():
    st = S
    p = _p(st, [(st - 1, 0.47, 0.49), (st + 100, 0.55, 0.57)], 1, index=_idx(st, 1.0 + 2 * EPS))
    assert [r["cell"] for r in mech.overreaction_rows(p)] == ["cross50"]


# ---------------------------------------------------------------- (d) opening and the mask
def _open_paths(dev, seed, n=200):
    """Mid jumps ``dev`` away from fair-v1 at start+5 and returns to it at start+40 (or, dev=None,
    a martingale mid with uninformative fair)."""
    r = np.random.default_rng(seed)
    out = []
    for i in range(n):
        st = S + 300 * i
        sg = 1 if i % 2 == 0 else -1
        if dev is not None:
            f = 0.47 if sg > 0 else 0.53
            m = f + sg * (dev + r.normal(0, 0.01))
            books = [(st - 1, f - 0.01, f + 0.01), (st + 5, round(m - 0.01, 3), round(m + 0.01, 3)),
                     (st + 40, f - 0.01, f + 0.01), (st + 150, f - 0.01, f + 0.01)]
        else:
            f = float(np.clip(0.5 + r.normal(0, 0.05), 0.1, 0.9))
            m = 0.5
            m2 = m + r.normal(0, 0.02)
            books = [(st - 1, m - 0.01, m + 0.01), (st + 50, round(m2 - 0.01, 3), round(m2 + 0.01, 3))]
        out.append(_p(st, books, int(r.random() < 0.5), fair=lambda t, v=f: v))
    return out


def test_opening_mask_moves_planted_noise_from_overreaction_to_opening():
    paths = _open_paths(0.11, 6)
    over = mech.run_paths(paths, ("overreaction", "opening"), N_BOOT)["studies"]
    c = over["overreaction"]["cells"]["cross50"]
    assert c["n_events"] == 0 and not over["overreaction"]["effect_any"]
    unmasked = mech.summarize_overreaction([r for p in paths for r in mech.overreaction_rows(p, mask_s=0)], N_BOOT)
    u = unmasked["cells"]["cross50"]
    assert u["n_events"] == 200 and u["effect"] and u["reading"] == "overreaction"
    op = over["opening"]
    assert op["effect_any"] and op["prereg"]["grid_size"] == 2
    for o in ("10", "30"):
        oc = op["cells"][o]
        assert oc["effect"] and oc["reading"] == "overreaction" and oc["n_events"] == 200
        assert abs(oc["headline_dm_60s_c"]["mean"] + 11) < 1
        assert oc["abs_error_c"]["mean"] > 9 and oc["unknown_fair_or_book"] == 0
        assert oc["fade"]["100"]["net_c"]["60s"]["maker_0"]["mean"] > 0


def test_opening_null_false_effects_over_20_seeds():
    assert _false_effects(lambda s: _open_paths(None, s), "opening", lambda r: r["effect_any"]) <= MAX_FALSE


def test_opening_unknown_fair_is_counted():
    p = _p(S, [(S - 1, 0.49, 0.51)], 1, fair=lambda t: None)
    rows = mech.opening_rows(p)
    assert [r["unknown"] for r in rows] == [1.0, 1.0]
    assert mech.summarize_opening(rows, 100)["cells"]["10"]["unknown_fair_or_book"] == 1


# ---------------------------------------------------------------- CLI on a written tape
def test_cli_end_to_end(tmp_path, capsys):
    data = tmp_path / "data"
    write_tape(data, n_windows=6)
    out = tmp_path / "m.json"
    assert mech.main(["--data", str(data), "--from", str(S), "--to", str(S + 1800), "--horizons", "5m",
                      "--boot", "200", "--out", str(out)]) == 0
    doc = orjson.loads(out.read_bytes())
    assert doc["windows_read"] == 6
    st = doc["studies"]
    flb, fin = st["flb"], st["final60"]
    assert sum(v["n"] for v in flb["cells"]["0.5"]["buckets"].values()) == 6
    cap = flb["cells"]["0.5"]["capacity"]["buy_yes"]       # the tape shows 100 contracts per level
    assert cap["100"]["fill_rate"] is not None and cap["500"]["fill_rate"] == 0.0
    assert fin["cells"]["30"]["n_windows"] == 6                       # fair-v1 known in the final minute
    assert fin["cells"]["30"]["capacity"]["500"]["no_depth"] == 6
    assert set(st["overreaction"]["cells"]) == set(mech.TRIGGERS)
    assert set(st["opening"]["cells"]) == {"10", "30"}
    assert set(doc["fee_tiers"]) == set(TIERS)
    o = capsys.readouterr().out
    assert "flb[0.5] (gross):" in o and "final60[10]: gateway_0095=" in o and "opening[10]" in o
