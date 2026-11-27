import numpy as np
import orjson

from doppel.lab import mechanisms as mech
from doppel.lab.markout import NS, MarketPath
from test_markout import S, write_tape

FEE = mech.pmus_fee_c(0.095, 100)
N_BOOT = 1000


def _p(start, books, outcome, fair=None, index=None, length=300):
    p = MarketPath(key=str(start), start=start, end=start + length, outcome=outcome,
                   cluster=start // 900, fair=fair, index=index)
    for t, b, a in books:
        p.tws.append(int(round(t * NS))); p.bids.append(b); p.asks.append(a)
    return p


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
    res = mech.run_paths(_flb_paths(0.3, 1), FEE, ("flb",), N_BOOT)["studies"]["flb"]
    assert res["effect_any"]
    for c in res["cells"].values():
        h = c["headline_slope"]
        assert c["effect"] and h["ci"][0] > 0 and abs(h["mean"] - 0.3) < 0.12
    assert res["prereg"]["grid_size"] == 3
    bk = res["cells"]["0.5"]["buckets"]
    assert sum(v["n"] for v in bk.values()) == 900


def test_flb_null_reports_no_effect():
    hits = [mech.run_paths(_flb_paths(0.0, 10 + s), FEE, ("flb",), N_BOOT)["studies"]["flb"]["effect_any"]
            for s in range(5)]
    assert sum(hits) <= 1, hits


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
    res = mech.run_paths(_final_paths(0.12, 0.0, 2), FEE, ("final60",), N_BOOT)["studies"]["final60"]
    assert res["effect_any"]
    c = res["cells"]["10"]
    assert c["effect"] and c["headline_trade_net_c"]["mean"] > 3
    assert c["abs_gap_c"]["mean"] > 5 and c["brier_mid_minus_fair_x1e4"]["ci"][0] > 0


def test_final60_null_gap_does_not_pay():
    res = mech.run_paths(_final_paths(0.0, 0.12, 3), FEE, ("final60",), N_BOOT)["studies"]["final60"]
    assert not res["effect_any"]                 # noise in fair: trading the gap does not pay


# ---------------------------------------------------------------- (c) overreaction
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
        jump = 1.006 if up else 0.994
        out.append(_p(st, books, int(r.random() < 0.5),
                      index=lambda t, st=st, j=jump: 100.0 * (j if t >= st + 100 else 1.0)))
    return out


def test_overreaction_planted_reversal():
    res = mech.run_paths(_or_paths(0.05, 4), FEE, ("overreaction",), N_BOOT)["studies"]["overreaction"]
    for name in mech.TRIGGERS:
        c = res["cells"][name]
        assert c["n_events"] == 300 and c["effect"] and c["reading"] == "overreaction"
        assert abs(c["headline_dm_60s_c"]["mean"] + 5) < 0.5
        assert c["fade_60s_c"]["mean"] > 0 > c["follow_60s_c"]["mean"]
    assert res["prereg"]["grid_size"] == 3


def test_overreaction_null_martingale():
    res = mech.run_paths(_or_paths(0.0, 5), FEE, ("overreaction",), N_BOOT)["studies"]["overreaction"]
    assert not res["effect_any"]


def test_small_move_does_not_trigger_brti():
    p = _p(S, [(S - 1, 0.47, 0.49), (S + 100, 0.55, 0.57)], 1, index=lambda t: 100.0 * (1.001 if t >= S + 100 else 1))
    rows = mech.overreaction_rows(p, FEE)
    assert [r["cell"] for r in rows] == ["cross50"]


# ---------------------------------------------------------------- CLI on a written tape
def test_cli_end_to_end(tmp_path, capsys):
    data = tmp_path / "data"
    write_tape(data, n_windows=6)
    out = tmp_path / "m.json"
    assert mech.main(["--data", str(data), "--from", str(S), "--to", str(S + 1800), "--horizons", "5m",
                      "--boot", "200", "--out", str(out)]) == 0
    doc = orjson.loads(out.read_bytes())
    assert doc["windows_read"] == 6
    flb, fin = doc["studies"]["flb"], doc["studies"]["final60"]
    assert sum(v["n"] for v in flb["cells"]["0.5"]["buckets"].values()) == 6
    assert fin["cells"]["30"]["n_windows"] == 6                       # fair-v1 known in the final minute
    assert set(doc["studies"]["overreaction"]["cells"]) == set(mech.TRIGGERS)
    assert "flb: effect_any=" in capsys.readouterr().out


def test_final60_gap_that_reprices_within_latency_is_missed():
    st = S
    t = st + 300 - 10
    p = _p(st, [(st + 200, 0.40, 0.42), (t + 0.1, 0.79, 0.81)], 1, fair=lambda _: 0.8)
    (r,) = [x for x in mech.final60_rows(p, FEE) if x["cell"] == 10]
    assert r["pnl"] is None and r["missed"]
    q = _p(st, [(st + 200, 0.40, 0.42)], 1, fair=lambda _: 0.8)
    (r2,) = [x for x in mech.final60_rows(q, FEE) if x["cell"] == 10]
    assert r2["pnl"] is not None and r2["pnl"] > 50 and not r2["missed"]
