"""Skill-decay sensor on synthetic forecasts. Detector parameters were chosen on seeds 0-49 (a
scratch run); every assertion below uses seeds >= 1000 that were not looked at before."""

import numpy as np
import orjson

from doppel.lab import skilldecay as sd

WS = 100          # rows per window
N_WIN = 150


def gen(seed, k=None, post_noise=0.8, n_win=N_WIN):
    """True P(YES) q; market = q seen through logit noise 0.8; strategy = q (edge) until window k,
    then q seen through logit noise ``post_noise`` (0.8 = no edge left vs the market)."""
    r = np.random.default_rng(seed)
    n = n_win * WS
    z = r.normal(0, 1.5, n)
    q = 1 / (1 + np.exp(-z))
    y = (r.random(n) < q).astype(float)
    pm = 1 / (1 + np.exp(-(z + r.normal(0, 0.8, n))))
    p = q.copy()
    if k is not None:
        i = np.arange(n) // WS >= k
        p[i] = 1 / (1 + np.exp(-(z[i] + r.normal(0, post_noise, i.sum()))))
    return list(zip(np.arange(n), p, pm, y))


def run(seed, **kw):
    return sd.detect([w["dll"] for w in sd.window_scores(gen(seed, **kw), WS)])


def test_window_scores_logloss_and_brier():
    rows = [(0, 0.8, 0.5, 1), (1, 0.8, 0.5, 0)]
    (w,) = sd.window_scores(rows, 2)
    ll_s = -(np.log(0.8) + np.log(0.2)) / 2
    assert abs(w["ll_strat"] - ll_s) < 1e-6 and abs(w["ll_mkt"] - np.log(2)) < 1e-6
    assert abs(w["brier_strat"] - (0.04 + 0.64) / 2) < 1e-9 and w["brier_mkt"] == 0.25
    assert abs(w["dll"] - (np.log(2) - ll_s)) < 1e-6


def test_steady_edge_false_alarm_rate():
    alarms = sum(run(s)["status"] == "DECAY" for s in range(1000, 1200))
    assert alarms / 200 <= 0.02, alarms     # measured: 0 / 200


def test_edge_disappears_at_k_is_flagged_within_40_windows():
    k = 60
    delays = []
    for s in range(2000, 2100):
        r = run(s, k=k)
        delays.append(None if r["alarm_window"] is None else r["alarm_window"] - k)
    hit = [d for d in delays if d is not None and 0 <= d <= 40]
    assert len(hit) >= 90, delays            # stated delay: <= 40 windows in >= 90% of seeds
    assert all(d is None or d >= 0 for d in delays)   # never before the change on these seeds


def test_edge_reduced_but_still_positive_is_not_decay():
    # strategy noise 0.4 vs market 0.8: a smaller edge that is still well above delta sigma
    alarms = sum(run(s, k=60, post_noise=0.4)["status"] == "DECAY" for s in range(4000, 4100))
    assert alarms <= 5, alarms              # measured: 0 / 100


def test_streaming_state_matches_batch_and_latches():
    xs = [w["dll"] for w in sd.window_scores(gen(3000, k=40), WS)]
    s = None
    for x in xs:
        s = orjson.loads(orjson.dumps(sd.watchdog_update(s, x)))   # state survives JSON round trip
    assert s is not None
    b = sd.detect(xs)
    assert s["status"] == b["status"] == "DECAY" and s["alarm_k"] == b["alarm_window"]


def test_cli(tmp_path, capsys):
    p = tmp_path / "rows.jsonl"
    p.write_bytes(b"\n".join(orjson.dumps({"t": int(t), "p": float(a), "mid": float(100 * m), "y": int(y)})
                             for t, a, m, y in gen(3001, k=40, n_win=120)))
    out = tmp_path / "o.json"
    assert sd.main(["--input", str(p), "--window-size", str(WS), "--out", str(out)]) == 0
    doc = orjson.loads(out.read_bytes())
    assert len(doc["windows"]) == 120 and doc["detector"]["status"] == "DECAY"
    assert "status=DECAY" in capsys.readouterr().out
