from doppel.lab.clock0 import FEATURES, _iso, evaluate


def _row(i, informed):
    r = {f: 0.0 for f in FEATURES}
    r.update(tw=i, wallet="w", known=True, informed=informed, weight=1.0 if informed else 0.0,
             m_c=5.0 if informed else -1.0, log_usd=6.0 if informed else 2.0)
    return r


def test_iso_utc():
    assert _iso("2026-11-26T00:00:00Z") == 1795651200


def test_evaluate_learns_separable_signal_and_handles_small_samples():
    assert evaluate([_row(i, i % 5 == 0) for i in range(100)])["status"] == "insufficient"
    rows = [_row(i, i % 5 == 0) for i in range(2000)]
    res = evaluate(rows)
    assert res["status"] == "ok" and res["auc_model"] > 0.99 and res["auc_size_only"] > 0.99
