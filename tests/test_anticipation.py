import numpy as np
import orjson

from doppel.lab.anticipation import (LEADS, NONE, REACTS, TOO_FEW, analyze_wallet,
                                     btc_taker_fills, direction)
from doppel.lab.fair import to_array
from doppel.lab.ledger import TokenMap

T0 = 1795046400
N = 120_000
SLUG = f"btc-updown-5m-{T0 + 3600}"


def _walk(seed=3):
    rng = np.random.default_rng(seed)
    raw = 60000 + np.cumsum(rng.normal(0, 2.0, N))
    return {T0 + i: float(v) for i, v in enumerate(raw)}


def _tmap(tmp_path):
    tm = TokenMap(tmp_path / "t.jsonl")
    tm.m = {"U": {"token": "U", "status": "resolved", "payout": 1.0, "slug": SLUG, "idx": 0},
            "D": {"token": "D", "status": "resolved", "payout": 0.0, "slug": SLUG, "idx": 1},
            "EU": {"token": "EU", "status": "resolved", "payout": 1.0,
                   "slug": f"eth-updown-15m-{T0}", "idx": 0}}
    return tm


def _rows(ts, ds, rng):
    """Fill rows realizing direction d at second t, mixing BUY/SELL of Up/Down tokens."""
    out = []
    for i, (t, d) in enumerate(zip(ts, ds)):
        buy = bool(rng.integers(0, 2))
        tok = ("U" if buy else "D") if d > 0 else ("D" if buy else "U")
        out.append({"id": f"r{i}", "token_id": tok, "side": "BUY" if buy else "SELL",
                    "amount_shares": 10, "amount_usdc": 5.0, "fee": 0.0, "price": 0.5,
                    "block_timestamp": int(t), "block_number": i, "order_type": "taker"})
    return out


def _pick(a, sig, n, rng):
    """Up to n seconds with a large signed signal, spaced so triggers don't overlap."""
    idx = np.flatnonzero(np.abs(sig) > 8.0)
    idx = idx[(idx > 200) & (idx < len(a) - 200)]
    idx = np.sort(rng.permutation(idx)[:n])
    return idx, np.sign(sig[idx]).astype(int)


def test_direction_mapping():
    assert direction("BUY", 0) == 1
    assert direction("SELL", 1) == 1
    assert direction("BUY", 1) == -1
    assert direction("SELL", 0) == -1


def test_exclusions(tmp_path):
    tm = _tmap(tmp_path)
    base = {"amount_shares": 1, "amount_usdc": 0.5, "fee": 0.0, "price": 0.5, "block_number": 1,
            "block_timestamp": T0 + 500}
    rows = [{**base, "id": "a", "token_id": "U", "side": "BUY", "order_type": "taker"},
            {**base, "id": "b", "token_id": "U", "side": "BUY", "order_type": "maker"},
            {**base, "id": "c", "token_id": "EU", "side": "BUY", "order_type": "taker"},
            {**base, "id": "d", "token_id": "ZZ", "side": "BUY", "order_type": "taker"},
            {**base, "id": "e", "token_id": "D", "side": "BUY", "order_type": "taker"}]
    t, d, skip = btc_taker_fills(rows, tm)
    assert list(t) == [T0 + 500, T0 + 500] and list(d) == [1, -1]
    assert skip == {"maker": 1, "unknown_token": 1, "not_btc_window": 1}


def test_reactor_reacts_to_public_move(tmp_path):
    rng = np.random.default_rng(1)
    t0, a = to_array(_walk())
    # trigger = move over [t-4, t-1], one-second gap before the fill at t
    sig = np.zeros_like(a)
    sig[4:] = a[3:-1] - a[:-4]
    i, d = _pick(a, sig, 600, rng)
    res = analyze_wallet(_rows(t0 + i, d, rng), _tmap(tmp_path), t0, a)
    assert res["verdict"] == REACTS and res["verdict_t_minus_1"] == REACTS
    assert res["anchors"]["t-1"]["by_lag"]["3"]["pre"]["aligned"] == 1.0
    orjson.dumps(res)


def test_leader_leads_move(tmp_path):
    rng = np.random.default_rng(2)
    t0, a = to_array(_walk())
    # trigger = move over [t+1, t+4], one-second gap after the fill at t
    sig = np.zeros_like(a)
    sig[:-4] = a[4:] - a[1:-3]
    i, d = _pick(a, sig, 600, rng)
    res = analyze_wallet(_rows(t0 + i, d, rng), _tmap(tmp_path), t0, a)
    assert res["verdict"] == LEADS and res["verdict_t_minus_1"] == LEADS
    lag1 = res["anchors"]["t"]["by_lag"]["1"]["pre"]
    assert abs(lag1["aligned"] - 0.5) < 0.1
    orjson.dumps(res)


def test_random_timing_no_clear_timing(tmp_path):
    rng = np.random.default_rng(3)
    t0, a = to_array(_walk())
    i = np.sort(rng.integers(200, len(a) - 200, 1500))
    d = rng.choice([-1, 1], len(i))
    res = analyze_wallet(_rows(t0 + i, d, rng), _tmap(tmp_path), t0, a)
    assert res["verdict"] == NONE and res["verdict_t_minus_1"] == NONE
    for k, s in res["anchors"]["t"]["by_lag"].items():
        assert abs(s["placebo"]["aligned"] - 0.5) < 0.05, k
        lo, hi = s["placebo"]["ci95"]
        assert lo < s["placebo"]["aligned"] < hi
    orjson.dumps(res)


def test_missing_seconds_drop_fill_and_min_fills(tmp_path):
    t0, a = to_array(_walk())
    a[5000 - 60] = np.nan  # placebo second of the first fill
    rows = _rows([t0 + 5000, t0 + 6000], [1, -1], np.random.default_rng(0))
    res = analyze_wallet(rows, _tmap(tmp_path), t0, a, min_fills=30)
    assert res["n_fills"] == 2 and res["n_usable"] == 1 and res["dropped_missing_brti"] == 1
    assert res["verdict"] == TOO_FEW
    # a fill whose look-back runs off the start of the series is dropped, not wrapped around
    res = analyze_wallet(_rows([t0 + 10], [1], np.random.default_rng(0)), _tmap(tmp_path), t0, a)
    assert res["n_usable"] == 0
    orjson.dumps(res)


def test_clustered_fills_do_not_qualify(tmp_path):
    """40 same-direction fills packed into 2 seconds are 2 groups, not 40 samples."""
    t0, a = to_array(_walk())
    i = 5000
    d = 1 if a[i] - a[i - 3] > 0 else -1  # perfectly 'aligned' pre move
    rows = _rows([t0 + i] * 20 + [t0 + i + 1] * 20, [d] * 40, np.random.default_rng(0))
    res = analyze_wallet(rows, _tmap(tmp_path), t0, a, min_fills=30)
    assert res["n_usable"] == 40 and res["n_groups"] == 2
    assert res["verdict"] == TOO_FEW
    # with a low fill gate, the per-lag group gate still blocks a confident verdict
    from doppel.lab.anticipation import _verdict
    s = {"aligned": 1.0, "ci95": [1.0, 1.0], "nonzero_groups": 2}
    plac = {**s, "aligned": 0.5}
    by_lag = {str(k): {"pre": s, "post": s, "placebo": plac} for k in (1, 2, 3, 5, 10, 30)}
    assert _verdict(by_lag, 30) == NONE


def test_pre_above_threshold_but_not_placebo_is_not_leads():
    from doppel.lab.anticipation import _verdict

    def st(x):
        return {"aligned": x, "ci95": [x - 0.01, x + 0.01], "nonzero_groups": 1000}
    by_lag = {str(k): {"pre": st(0.6), "post": st(0.6), "placebo": st(0.7)}
              for k in (1, 2, 3, 5, 10, 30)}
    assert _verdict(by_lag, 30) == NONE
    for k in by_lag:
        by_lag[k]["pre"] = st(0.5)
    assert _verdict(by_lag, 30) == LEADS


def test_cli_end_to_end(tmp_path, capsys):
    from doppel.lab.anticipation import main
    from doppel.tape.segment import SegmentWriter
    data, lab = tmp_path / "tape", tmp_path / "lab"
    walk = _walk()
    w = SegmentWriter(data, "ref_brti_raw")
    for t in range(T0, T0 + 3000):
        w.append(orjson.dumps({"t_ms": t * 1000, "v": str(walk[t]), "received": 0}).decode(),
                 tw=t * 10**9)
    w.close()
    lab.mkdir()
    (lab / "tokens.jsonl").write_bytes(b"".join(orjson.dumps(v) + b"\n"
                                                for v in _tmap(tmp_path).m.values()))
    rng = np.random.default_rng(5)
    rows = _rows(T0 + np.arange(200, 2800, 50), rng.choice([-1, 1], 52), rng)
    g = SegmentWriter(data, "goldsky_activity")
    g.append(orjson.dumps({"data": [{**r, "event_type": "TRADE", "address": "0xaaa"}
                                    for r in rows]}).decode(),
             meta={"status": 200, "job": "cohort-x"})
    g.close()
    cohort = tmp_path / "cohort.json"
    cohort.write_bytes(orjson.dumps({"sha256": "ab" * 32, "candidates": ["0xaaa"],
                                     "controls": ["0xbbb"]}))
    assert main(["--data", str(data), "--lab", str(lab), "--cohort", str(cohort)]) == 0
    (out,) = (lab / "anticipation").glob(f"anticipation-{'ab' * 6}-*.json")
    rep = orjson.loads(out.read_bytes())
    assert rep["wallets"]["0xaaa"]["n_usable"] == 52
    assert rep["wallets"]["0xaaa"]["verdict"] in (REACTS, LEADS, NONE)
    assert rep["wallets"]["0xbbb"]["verdict"] == TOO_FEW
    assert "0xaaa" in capsys.readouterr().out
