import numpy as np

from doppel.lab.fair import calibrate, fair_up, to_array


def _rolling60(raw):
    c = np.cumsum(np.insert(raw, 0, 0.0))
    return (c[60:] - c[:-60]) / 60.0


def test_fair_up_known_cases():
    t0 = 0
    a = np.full(4000, 100.0)
    a[3000:] = 101.0  # after open the average moved up 1.0 with zero historical variance
    assert fair_up(a, t0, 2999, 3100, 3299) > 0.99  # +1.0 above the open, tiny historical spread
    assert fair_up(a, t0, 3001, 3100, 3301) == 0.5  # no movement since open, nonzero spread
    assert fair_up(np.full(4000, 100.0), t0, 3001, 3100, 3301) == 1.0  # flat: sign rule


def test_calibration_on_synthetic_random_walk_is_reliable():
    rng = np.random.default_rng(7)
    raw = 60000 + np.cumsum(rng.normal(0, 2.0, 3 * 86400))
    a = _rolling60(raw)
    series = {1795000000 + i: float(v) for i, v in enumerate(a)}
    res = calibrate(series, "5m")
    late = res["by_tau"]["240"]
    assert late["skill"] > 0.3  # series-only model has skill late in the window...
    # ...but it cannot see raw-vs-average lag, so it is NOT expected to be perfectly calibrated.
    t0, arr = to_array(series)
    assert len(arr) == len(a)


def test_raw_model_is_calibrated_on_synthetic_walk():
    from doppel.lab.fair import fair_up_raw
    rng = np.random.default_rng(11)
    n = 3 * 86400
    raw = 60000 + np.cumsum(rng.normal(0, 2.0, n))
    a = _rolling60(raw)  # a[i] = mean(raw[i : i+60]) -> label at i+59
    t0 = 0
    preds, ys = [], []
    for start in range(1200, n - 400, 300):
        end = start + 300
        ptb = a[start - 1 - 59]
        y = 1 if a[end - 1 - 59] >= ptb else 0
        p = fair_up_raw(raw, t0, ptb, start + 240, end)
        preds.append(p)
        ys.append(y)
    p, y = np.array(preds), np.array(ys)
    for lo, hi in ((0.1, 0.3), (0.3, 0.7), (0.7, 0.9)):
        k = (p >= lo) & (p < hi)
        if k.sum() >= 40:
            assert abs(p[k].mean() - y[k].mean()) < 0.08


def test_fair_asof_ignores_brti_received_after_the_decision():
    """Astra audit 2026-11-26: changing one BTC tick received 10 s after the decision flipped a lab
    scorecard. fair_up_asof must not move when only later-received seconds change."""
    import numpy as np

    from doppel.lab.fair import fair_up_asof
    t0, end = 1_800_000_000, 1_800_000_000 + 900
    raw = 100_000 + np.cumsum(np.random.default_rng(0).normal(0, 5, end - t0 + 1))
    rcv = np.arange(t0, end + 1) + 0.7
    d = end - 100.0
    base = fair_up_asof(raw, rcv, t0, 100_000.0, d, end)
    late = int(d) - 3 - t0                      # printed before d but received 10 s after it
    rcv2 = rcv.copy(); rcv2[late] = d + 10
    raw2 = raw.copy(); raw2[late] += 5_000
    assert base is not None
    assert fair_up_asof(raw2, rcv2, t0, 100_000.0, d, end) in (None, base)
    raw3 = raw.copy(); raw3[int(d) + 5 - t0] += 5_000   # a future second never matters
    assert fair_up_asof(raw3, rcv, t0, 100_000.0, d, end) == base


def test_adverse_uses_receipt_time_for_window_boundaries(tmp_path, monkeypatch):
    from doppel.lab import adverse

    start, end = 1000, 1300
    row = {"start": start, "end": end, "price": 0.6, "qty": 1, "taker_side": "ORDER_SIDE_SELL"}
    monkeypatch.setattr(adverse, "load_series", lambda *a: {start - 1: 100.0, end - 1: 101.0})
    monkeypatch.setattr(adverse, "load_raw_rcv", lambda *a: {start: (100.0, start)})
    monkeypatch.setattr(adverse, "fair_up_asof", lambda *a: 0.5)
    monkeypatch.setattr(adverse, "iter_prints", lambda *a: [
        row | {"recv": start + 0.25}, row | {"recv": end + 0.25}])
    doc = adverse.study(tmp_path)
    assert doc["total"]["n"] == 1
    assert doc["skipped"] == {"outside_window": 1}
    assert doc["by_tte"]["240-900s"]["n"] == 1
