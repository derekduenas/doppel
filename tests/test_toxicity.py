from doppel.lab.toxicity import clustered, ledger


def _doc(fills_by_window):
    return {"genome": {"id": "g"}, "windows": [
        {"eligible": True, "slug": f"w{i}", "start": 0, "end": 300, "fills": fs}
        for i, fs in enumerate(fills_by_window)]}


def _f(t, px, qty, mo, via="trade", role="maker", side="buy"):
    return {"t": t, "px": px, "qty": qty, "role": role, "side": side, "via": via,
            "markout_c": {"1": mo, "5": mo, "30": mo}}


def test_weighted_mean_clustered_se_and_attribution():
    doc = _doc([[_f(10, 0.5, 2, -2.0), _f(290, 0.9, 1, -8.0, via="cross")],
                [_f(100, 0.5, 1, 1.0), _f(5, 0.5, 5, None, role="taker")]])
    res = ledger([doc])
    a = res["all"]["30"]
    assert res["n_fills"] == 3 and a["fills"] == 3 and a["windows"] == 2
    assert a["markout_c"] == round((2 * -2 + 1 * -8 + 1 * 1) / 4, 4)          # -2.75
    assert a["markout_usd"] == round(-2.75 * 4 / 100, 4)
    # ratio-estimator SE over 2 windows: y=(-12,1), w=(3,1)
    m = -2.75
    se = (2 / 1 * ((-12 - m * 3) ** 2 + (1 - m * 1) ** 2)) ** 0.5 / 4
    assert a["se_c"] == round(se, 4)
    assert res["by"]["via"]["cross"]["30"]["markout_c"] == -8.0
    assert set(res["by"]["tte"]) == {"tte 0-30", "tte >=240", "tte 120-240"}
    assert "|p-.5| >=0.4" in res["by"]["px"]


def test_ineligible_windows_and_missing_markouts_skipped():
    doc = _doc([[_f(10, 0.5, 1, None)]])
    doc["windows"].append({"eligible": False, "slug": "x", "start": 0, "end": 300, "fills": [_f(1, 0.5, 9, -50.0)]})
    s = ledger([doc])["all"]["1"]
    assert s["fills"] == 0 and s["markout_c"] is None
    assert clustered([], "1")["windows"] == 0
