"""Strategy simulator on a small synthetic tape: the candidate path equals answerkey's public path, a
known midpoint offset is recovered, an inert parameter is flagged, random and hidden-signal subjects
are not explained, runs are deterministic and memory is bounded by one window of orders."""

import math
import resource
import sys

import pytest

from doppel.lab import simulator as S
from test_answerkey import T0, synth

MIN = 10
T1 = T0 + MIN * 60
STREAM = "clob_btc_5m"
MID = S.Family("midpoint_maker", "a", {"h_c": S.U(0.5, 3.5), "dummy": S.U(0.0, 1.0)},
               {"h_c": "price", "dummy": "exit"}, {"sizes": [20.0], "timer_s": 5.0, "move_c": 1.0})
FAMS = {"midpoint_maker": MID, "null": S.FAMILIES["null"]}


@pytest.fixture(scope="module")
def tape(tmp_path_factory):
    return synth(tmp_path_factory.mktemp("sim") / "tape", minutes=MIN, seed=11, rate_hz=2)


@pytest.fixture(scope="module")
def table(tape):
    return S.build_table(tape, tape / "tokens.jsonl", STREAM, T0, T1, FAMS, 50, 5, workers=2)


def observe(tape, tmp_path, fam, params, seed):
    d = tmp_path / f"subject-{seed}"
    wallet = S.make_subject(fam, params, seed, tape, tape / "tokens.jsonl", STREAM, T0, T1, d)
    pub = d / "public"
    return S.observed_footprint(pub / "tape", STREAM, pub / "orders.jsonl", pub / "lab" / "tokens.jsonl", wallet, T0, T1)


def same(a, b):
    return a == b or (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b))


def test_candidate_path_equals_the_public_evidence_path(tape, tmp_path):
    """Same rule and seed: the in-memory candidate and answerkey's written public evidence give the
    same order-tape rows and the same footprint (touch at fill from the replayed public tape)."""
    fam = S.Family("midpoint_maker", "a", {}, {}, {})
    obs = observe(tape, tmp_path, fam, {}, 9)
    wins = S.universe(tape / "tokens.jsonl", STREAM, T0, T1)
    cand = S.simulate(fam, {}, 9, tape, STREAM, wins, T0, T1)
    fp = S.footprint(cand["rows"], cand["touch"], wins, T0, T1)
    assert obs["n_fills"] == fp["n_fills"] > 0
    assert all(same(obs["x"][k], fp["x"][k]) for k in S.FEATURES), \
        {k: (obs["x"][k], fp["x"][k]) for k in S.FEATURES if not same(obs["x"][k], fp["x"][k])}
    assert not math.isnan(fp["x"]["touch_q50"]) and not math.isnan(fp["x"]["mid_q50"])


def test_recovers_a_midpoint_offset_and_flags_an_inert_parameter(tape, tmp_path, table):
    truth = {"h_c": 1.7, "dummy": 0.3}
    obs = observe(tape, tmp_path, MID, truth, 123457)
    res = S.infer({}, table, obs["x"], FAMS, accept=0.2, seed=5)
    assert res["explained"], res["reasons"]
    assert res["best_family"] == "midpoint_maker"
    h = res["families"]["midpoint_maker"]["params"]["h_c"]
    assert abs(h["median"] - truth["h_c"]) <= 0.75 and h["ci90"][0] <= truth["h_c"] <= h["ci90"][1], h
    assert h["flag"] in ("IDENTIFIED", "PARTIAL")
    assert res["families"]["midpoint_maker"]["params"]["dummy"]["flag"] == "UNIDENTIFIED"
    rec = res["recovery"]
    assert set(rec["concentration"]) == {"price", "exit"} and 0 <= rec["score"] <= 1


@pytest.mark.parametrize("who", ["random", "hidden_signal"])
def test_random_and_hidden_signal_subjects_are_not_explained(tape, tmp_path, table, who):
    if who == "random":
        obs = observe(tape, tmp_path, S.FAMILIES["null"], {"rate_per_s": 0.3, "taker_share": 0.4, "life_s": 8.0}, 31337)
    else:
        obs = observe(tape, tmp_path, None, {"accuracy": 0.85, "size": 25.0}, 4243)
    res = S.infer({}, table, obs["x"], FAMS, accept=0.2, seed=5)
    assert not res["explained"] and res["verdict"] == "not explained by observed inputs"
    assert all("params" not in f and "posterior_samples" not in f for f in res["families"].values())


def test_table_is_deterministic_whatever_the_worker_count(tape):
    a = S.build_table(tape, tape / "tokens.jsonl", STREAM, T0, T1, FAMS, 2, 8, workers=1)
    b = S.build_table(tape, tape / "tokens.jsonl", STREAM, T0, T1, FAMS, 2, 8, workers=2)
    strip = [{k: v for k, v in r.items() if k != "sim_s"} for r in a]
    assert strip == [{k: v for k, v in r.items() if k != "sim_s"} for r in b]
    assert S.infer({}, a, dict(zip(S.FEATURES, a[0]["x"])), FAMS, 0.5, 3) == \
        S.infer({}, a, dict(zip(S.FEATURES, a[0]["x"])), FAMS, 0.5, 3)


def test_finished_windows_are_folded_and_memory_is_bounded(tape, monkeypatch):
    sims = []

    class Spy(S.FootSim):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            sims.append(self)

    monkeypatch.setattr(S, "FootSim", Spy)
    wins = S.universe(tape / "tokens.jsonl", STREAM, T0, T1)
    r = S.simulate(S.FAMILIES["touch_ladder"], {"levels_c": [0, 1, 2], "sizes": [50.0], "max_age_s": 5.0,
                                                "band_c": 40.0}, 77, tape, STREAM, wins, T0, T1)
    assert r["rows"] and sims[0].orders and all(o is S._DONE for o in sims[0].orders)
    assert not sims[0].touch      # every recorded touch moved into the folded output
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024)
    assert rss < 1500


def test_reserved_exam_seeds_are_refused(tape, tmp_path):
    for seed in S.RESERVED_SEEDS:
        with pytest.raises(ValueError):
            S.build_table(tape, tape / "tokens.jsonl", STREAM, T0, T1, FAMS, 1, seed)
        with pytest.raises(ValueError):
            S.make_subject(MID, {}, seed, tape, tape / "tokens.jsonl", STREAM, T0, T1, tmp_path / str(seed))
    assert all(S.derive_seed(1, "candidate", "x", i) not in S.RESERVED_SEEDS for i in range(1000))


def test_gates_band_and_cancel_age():
    """band_c: outside the band everything is cancelled; max_age_s: an old quote is cancelled."""
    import numpy as np

    from doppel.lab.answerkey import Ctx
    from doppel.lab.intltwin import IntlWindow

    pol = S.make_policy(S.FAMILIES["touch_ladder"], {"levels_c": [0], "sizes": [50.0], "max_age_s": 2.0,
                                                     "band_c": 20.0}, 1)
    w = IntlWindow("btc", "5m", 0, "U", "D", None)
    old = S.Order("o1", "maker", "U", True, "s", 0.4, 50.0, "", 0, 0, 0, {}, 0.0)
    far = Ctx(0, 10_000, w, {"U": (0.80, 0.82), "D": (0.18, 0.20)}, [old], 0.0, None, np.random.default_rng(0))
    assert pol.decide(far) == [("cancel", "o1")]
    near = Ctx(0, 10_000, w, {"U": (0.40, 0.42), "D": (0.58, 0.60)}, [old], 0.0, None, np.random.default_rng(0))
    acts = pol.decide(near)
    assert ("cancel", "o1") in acts and acts.count(("cancel", "o1")) == 1


def test_regression_adjustment_removes_the_linear_dependence_on_the_statistics():
    """theta depends linearly (in the logit coordinate) on a statistic: the adjusted particles collapse
    onto the value at the observed statistic."""
    import numpy as np

    prior = S.U(0.0, 10.0)
    rng = np.random.default_rng(3)
    z = rng.uniform(-1, 1, 200)
    vals = [prior.from_u(0.4 + 1.5 * v + rng.normal(0, 0.05)) for v in z]
    adj = S.adjust(prior, vals, z[:, None], np.ones(200))
    assert np.std(adj) < 0.2 * np.std(vals) and abs(np.median(adj) - prior.from_u(0.4)) < 0.1


def test_identified_needs_a_narrow_untruncated_interval_and_a_passing_self_check():
    import numpy as np

    prior = S.U(0.0, 10.0)
    ref = [9.0] * 10                      # a random subset of the prior spans ~9
    mid = np.linspace(4.0, 5.0, 30)       # narrow, far from the bounds
    edge = np.linspace(9.0, 10.0, 30)     # narrow, touching the upper bound
    assert S.param_summary(prior, list(mid), ref, mid, None, True)["flag"] == "IDENTIFIED"
    s = S.param_summary(prior, list(mid), ref, mid, None, False)
    assert s["flag"] == "PARTIAL" and s["capped_by"] == ["self_check"] and s["narrow"]
    s = S.param_summary(prior, list(edge), ref, edge, None, True)
    assert s["flag"] == "PARTIAL" and s["capped_by"] == ["prior_edge"] and not s["narrow"]


def test_self_check_reports_eligibility_per_parameter(table):
    cal = S.calibrate(table, FAMS, 4, 0.2, 5)
    assert set(cal["params"]) <= {"midpoint_maker.h_c", "midpoint_maker.dummy"}
    for v in cal["params"].values():
        assert v["eligible"] == (v["n_narrow"] >= S.CAL_MIN_N and v["coverage_narrow"] >= S.CAL_COVER)
    assert not cal["params"].get("midpoint_maker.dummy", {}).get("eligible")


def test_validate_discards_subject_tapes_unless_work_is_given_and_rescore_reproduces_it(tape, table, tmp_path):
    meta = {"stream": STREAM, "from": T0, "to": T1, "seed": 5, "n": 50, "features": S.FEATURES,
            "families": {f: {k: p.spec() for k, p in fam.prior.items()} for f, fam in FAMS.items()}}
    S.write_table(tmp_path / "table.jsonl", table, meta)
    kw = dict(n=50, per_family=1, hidden=1, seed=77, workers=1, accept=0.2, table_path=tmp_path / "table.jsonl",
              families=FAMS, calib_rows=3)
    card = S.validate(tape, tape / "tokens.jsonl", STREAM, T0, T1, tmp_path / "a", **kw)
    assert not list((tmp_path / "a").rglob("public"))
    assert len(list((tmp_path / "a" / "subjects").glob("*/inference.json"))) == len(card["subjects"]) == 3
    S.validate(tape, tape / "tokens.jsonl", STREAM, T0, T1, tmp_path / "b", work=tmp_path / "w", **kw)
    assert len(list((tmp_path / "w").glob("*/public/orders.jsonl"))) == 3
    rs = S.rescore(tmp_path / "table.jsonl", tmp_path / "a" / "calibration.json", [(tmp_path / "a", None)], FAMS)
    assert rs["runs"][str(tmp_path / "a")]["seed"] == 77
    assert rs["pooled"] == card["totals"]
