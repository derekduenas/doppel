"""Promotion gate: known-answer DSR, BH/Holm on a textbook list, HAC on AR(1), the CLI's refusals."""

import math

import numpy as np
import orjson
import pytest

from doppel.lab import promotion as P


# ---------------------------------------------------------------- DSR
def test_dsr_reproduces_the_published_example():
    # Bailey & Lopez de Prado (2014), worked example: N = 100 trials, V[SR] = 1/2 annualised, T = 1250
    # daily returns, SR = 2.5 annualised, skew -3, kurtosis 10 -> SR0 = 0.1132 daily, DSR = 0.9004.
    sr0 = P.expected_max_sr(100, 0.5 / 250)
    assert sr0 == pytest.approx(0.1132, abs=1e-4)
    assert P.psr(2.5 / math.sqrt(250), sr0, 1250, -3.0, 10.0) == pytest.approx(0.9004, abs=5e-4)


def test_dsr_hand_computed_small_case():
    # x = 1, 2, 3, 4, 10: mean 4, sd sqrt(12.5), population m2 = 10, m3 = 180/5 = 36, m4 = 1394/5 = 278.8
    x = [1.0, 2.0, 3.0, 4.0, 10.0]
    m, sd, g3, g4 = P.moments(x)
    assert (m, sd) == (4.0, pytest.approx(math.sqrt(12.5)))
    assert g3 == pytest.approx(36 / 10 ** 1.5) and g4 == pytest.approx(2.788)
    sr = 4 / math.sqrt(12.5)
    # N = 1: no deflation, DSR = PSR against 0
    d = P.deflated_sharpe(x, 1)
    z = sr * 2 / math.sqrt(1 - g3 * sr + (g4 - 1) / 4 * sr * sr)
    assert d["sr0"] == 0 and d["dsr"] == pytest.approx(0.5 * math.erfc(-z / math.sqrt(2)), abs=1e-6)
    # N = 10 with the null variance 1/(T-1) = 1/4
    d10 = P.deflated_sharpe(x, 10)
    sr0 = 0.5 * ((1 - P.EULER) * 1.2815515655446004 + P.EULER * P.Z.inv_cdf(1 - 1 / (10 * math.e)))
    assert d10["sr0"] == pytest.approx(sr0, abs=1e-6) and d10["sr_variance_source"] == "null 1/(T-1)"
    assert d10["dsr"] < d["dsr"]
    # a recorded cross-trial variance below the null never deflates less than the null
    assert P.deflated_sharpe(x, 10, 0.01)["sr0"] == d10["sr0"]
    assert P.deflated_sharpe(x, 10, 1.0)["sr_variance_source"] == "trials ledger"


def test_dsr_is_null_when_undefined_and_falls_with_more_trials():
    assert P.deflated_sharpe([1.0, 1.0, 1.0], 5)["dsr"] is None
    assert P.deflated_sharpe([1.0, 2.0], 5)["dsr"] is None
    x = np.random.default_rng(0).normal(0.2, 1.0, 500)
    ds = [P.deflated_sharpe(x, n)["dsr"] for n in (1, 10, 100, 1000)]
    assert ds == sorted(ds, reverse=True)


# ---------------------------------------------------------------- FDR
BH95 = [0.0001, 0.0004, 0.0019, 0.0095, 0.0201, 0.0278, 0.0298, 0.0344, 0.0459, 0.3240, 0.4262,
        0.5719, 0.6528, 0.7590, 1.0000]   # Benjamini & Hochberg (1995), section 4


def test_bh_and_holm_on_the_textbook_list():
    q, keep = P.bh(BH95)
    assert keep == [True] * 4 + [False] * 11            # BH rejects 4 at q = 0.05
    assert q[3] == pytest.approx(0.0095 * 15 / 4)
    assert q[4] == pytest.approx(min(0.0201 * 15 / 5, 0.0459 * 15 / 9, 0.0298 * 15 / 7, 0.0278 * 15 / 6,
                                     0.0344 * 15 / 8))
    assert sum(a <= 0.05 for a in P.holm(BH95)) == 3     # Holm rejects 3
    # input order is kept
    rev = list(reversed(BH95))
    assert P.bh(rev)[1] == list(reversed(keep))


# ---------------------------------------------------------------- HAC
def ar1(phi: float, n: int, seed: int) -> np.ndarray:
    e = np.random.default_rng(seed).normal(0, 1, n)
    x = np.empty(n)
    x[0] = e[0] / math.sqrt(1 - phi * phi)
    for i in range(1, n):
        x[i] = phi * x[i - 1] + e[i]
    return x


def test_hac_var_is_the_bartlett_sum_by_definition():
    x = np.array([1.0, -2.0, 3.0, 0.5, 4.0, -1.0, 2.0])
    c = x - x.mean()
    g = [sum(c[t] * c[t - j] for t in range(j, 7)) / 7 for j in range(7)]
    assert P.hac_var(x, 3.0) == pytest.approx(g[0] + 2 * (2 / 3 * g[1] + 1 / 3 * g[2]))
    assert P.hac_var(x, 1.0) == pytest.approx(g[0])


def test_hac_on_ar1_matches_the_analytic_long_run_variance():
    phi, n = 0.5, 200_000
    x = ar1(phi, n, 1)
    bw, rho = P.andrews_bandwidth(x)
    assert rho == pytest.approx(phi, abs=0.01)
    a = 4 * phi ** 2 / ((1 - phi) ** 2 * (1 + phi) ** 2)
    assert bw == pytest.approx(1.1447 * (a * n) ** (1 / 3), rel=0.05)
    assert P.hac_var(x, bw) == pytest.approx(1 / (1 - phi) ** 2, rel=0.1)   # sigma^2 / (1 - phi)^2 = 4


def test_hac_t_corrects_the_naive_t_on_a_persistent_series():
    x = ar1(0.8, 5000, 2) + 0.05
    nw = P.newey_west(x)
    naive = x.mean() / (x.std(ddof=1) / math.sqrt(len(x)))
    assert abs(nw["t"]) < abs(naive) / 2                 # sqrt((1+phi)/(1-phi)) = 3 in theory
    iid = np.random.default_rng(3).normal(0.05, 1, 5000)
    assert P.newey_west(iid)["t"] == pytest.approx(iid.mean() / (iid.std() / math.sqrt(5000)), rel=0.1)


# ---------------------------------------------------------------- gate + CLI
def twin_doc(x, decision="PASS", gid="g1", p=None) -> dict:
    wins = [{"eligible": True, "start": 300 * i, "end": 300 * i + 300, "net_c": float(v)} for i, v in enumerate(x)]
    wins.append({"eligible": False, "start": 0, "end": 300, "net_c": 99.0})
    return {"genome": {"id": gid}, "range": [0, 300 * len(x)], "twin": {"diagnostic": False},
            "verdict": {"decision": decision, "reason": "x", "p_value": p,
                        "bootstrap": {"block_windows": 12, "n_boot": 500, "seed": 0}},
            "windows": list(reversed(wins))}


STRONG = np.random.default_rng(5).normal(40.0, 60.0, 400)


def test_a_strong_pass_with_few_trials_is_promoted():
    rec = P.promote([("a", twin_doc(STRONG))], {"n_trials": 3}, variants=variants_with(STRONG, 2))
    c = rec["candidates"][0]
    assert c["windows"] == 400 and c["decision"] == "PROMOTE", c["failures"]
    assert rec["trials"]["padded_with_p1"] == 2 and c["fdr"]["bh_survives"]
    assert c["pbo"]["pbo"] < 0.15 and c["pbo"]["combinations"] == 12870
    # more than one trial and no variants matrix: PBO is not measured and the gate holds
    c = P.promote([("a", twin_doc(STRONG))], {"n_trials": 3})["candidates"][0]
    assert c["decision"] == "HOLD" and c["binding"].startswith("PBO None")
    with pytest.raises(ValueError, match="must count every variant"):
        P.promote([("a", twin_doc(STRONG))], {"n_trials": 2}, variants=variants_with(STRONG, 2))


# ---------------------------------------------------------------- PBO
def variants_with(edge, n_noise: int, seed: int = 9) -> dict:
    rng = np.random.default_rng(seed)
    vs = [{"id": "edge", "net_c": list(map(float, edge))}]
    vs += [{"id": f"noise{i}", "net_c": list(map(float, rng.normal(0, 60.0, len(edge))))} for i in range(n_noise)]
    return {"variants": vs}


def test_pbo_is_about_one_half_when_every_variant_is_noise():
    pbos = [P.pbo(np.random.default_rng(s).normal(0, 1, (800, 20)))["pbo"] for s in range(5)]
    assert abs(np.mean(pbos) - 0.5) < 0.1
    r = P.pbo(np.random.default_rng(0).normal(0, 1, (800, 20)))
    assert r["combinations"] == 12870 and r["windows_used"] == 800 and sum(r["logit"]["hist_counts"]) == 12870


def test_pbo_is_low_with_one_true_edge_among_noise():
    X = np.random.default_rng(1).normal(0, 1, (800, 20))
    X[:, 7] += 0.3
    r = P.pbo(X)
    assert r["pbo"] < 0.05 and r["logit"]["quantiles"]["0.5"] > 2


def test_pbo_input_checks():
    with pytest.raises(ValueError, match="M >= 2"):
        P.pbo(np.zeros((100, 1)))
    with pytest.raises(ValueError, match="even"):
        P.pbo(np.zeros((100, 3)), 5)
    with pytest.raises(ValueError, match="at least 32"):
        P.pbo(np.zeros((20, 3)))
    with pytest.raises(ValueError, match="aligned"):
        P.load_variants({"variants": [{"id": "a", "net_c": [1, 2]}, {"id": "b", "net_c": [1]}]})


def test_the_gate_never_overrides_a_non_pass_verdict():
    c = P.promote([("a", twin_doc(STRONG, "CONTINUE"))], {"n_trials": 1})["candidates"][0]
    assert c["decision"] == "HOLD" and c["binding"].startswith("base verdict CONTINUE")
    d = twin_doc(STRONG)
    d["twin"]["diagnostic"] = True
    assert P.promote([("a", d)], {"n_trials": 1})["candidates"][0]["base_decision"] == "DIAGNOSTIC"


def test_many_trials_deflate_a_modest_edge_and_bh_counts_unreported_trials():
    x = np.random.default_rng(6).normal(6.0, 60.0, 400)   # SR 0.1 per window, t ~ 2
    few = P.promote([("a", twin_doc(x))], {"n_trials": 1})["candidates"][0]
    many = P.promote([("a", twin_doc(x))], {"n_trials": 5000})["candidates"][0]
    assert many["dsr"]["dsr"] < few["dsr"]["dsr"] and many["decision"] == "HOLD"
    assert many["binding"].startswith("DSR")
    assert many["fdr"]["bh_q"] >= min(1.0, few["fdr"]["p"] * 5000) - 1e-9   # BH with m = 5000, rank 1


def test_hac_and_bootstrap_must_agree():
    x = ar1(0.95, 400, 7) * 30 + 8      # persistent: naive tests over-reject
    c = P.promote([("a", twin_doc(x))], {"n_trials": 1})["candidates"][0]
    if c["hac"]["p_one_sided"] > 0.05 or c["bootstrap"]["p_one_sided"] > 0.05:
        assert any("HAC" in f for f in c["failures"])
    neg = P.promote([("a", twin_doc(-STRONG))], {"n_trials": 1})["candidates"][0]
    assert any("HAC" in f for f in neg["failures"])


def test_decide_needs_hac_and_bootstrap_to_agree_and_names_the_first_failure():
    ok = {"base_decision": "PASS", "dsr": {"dsr": 0.99}, "pbo": {"pbo": 0.05},
          "fdr": {"bh_survives": True, "bh_q": 0.01},
          "hac": {"t": 3.0, "p_one_sided": 0.001}, "bootstrap": {"mean": 5.0, "p_one_sided": 0.002}}
    assert P.decide(ok) == ("PROMOTE", None, [])
    hac_weak = ok | {"hac": {"t": 1.0, "p_one_sided": 0.16}}
    dec, binding, fails = P.decide(hac_weak)
    assert dec == "HOLD" and binding.startswith("HAC") and len(fails) == 1
    boot_weak = ok | {"bootstrap": {"mean": 5.0, "p_one_sided": 0.2}}
    assert P.decide(boot_weak)[1].startswith("HAC")
    assert P.decide(ok | {"pbo": {"pbo": 0.15}})[1].startswith("PBO 0.15")
    assert P.decide(ok | {"pbo": {"pbo": None, "not_applicable": True}})[0] == "PROMOTE"
    all_bad = boot_weak | {"base_decision": "KILL", "dsr": {"dsr": 0.5}, "pbo": {"pbo": 0.6},
                           "fdr": {"bh_survives": False, "bh_q": 0.3}}
    dec, binding, fails = P.decide(all_bad)
    assert binding.startswith("base verdict KILL") and len(fails) == 5
    assert [f.split()[0] for f in fails] == ["base", "DSR", "PBO", "BH", "HAC"]


def test_trials_ledger_is_required_and_checked():
    with pytest.raises(ValueError, match="n_trials"):
        P.promote([("a", twin_doc(STRONG))], {"trials": []})
    with pytest.raises(ValueError, match="lists 2 trials"):
        P.promote([("a", twin_doc(STRONG))], {"n_trials": 1, "trials": [{"id": "x"}, {"id": "y"}]})
    with pytest.raises(ValueError, match="candidates"):
        P.promote([("a", twin_doc(STRONG)), ("b", twin_doc(STRONG, gid="g2"))], {"n_trials": 1})
    # a listed trial that is the candidate is not double counted; a ledger's rows join the family
    led = {"rows": [{"genome_id": "h1", "p_value": 0.2}, {"genome_id": "h2", "p_value": None}]}
    rec = P.promote([("a", twin_doc(STRONG)), ("led", led)],
                    {"n_trials": 4, "trials": [{"id": "g1", "p_value": 0.001}, {"id": "w9", "p_value": 0.5}]})
    assert [m["id"] for m in rec["family"]["members"]] == ["candidate:a", "trial:w9", "led:h1"]
    assert rec["trials"]["padded_with_p1"] == 1


def test_cli_refuses_without_trials_and_writes_a_record(tmp_path, capsys):
    res = tmp_path / "twin.json"
    res.write_bytes(orjson.dumps(twin_doc(STRONG)))
    out = tmp_path / "out"
    assert P.main(["--results", str(res), "--out", str(out)]) == 2
    assert "REFUSED" in capsys.readouterr().err and not out.exists()
    bad = tmp_path / "bad.json"
    bad.write_bytes(orjson.dumps({"study": "s"}))
    assert P.main(["--results", str(res), "--trials", str(bad), "--out", str(out)]) == 2
    tr = tmp_path / "trials.json"
    tr.write_bytes(orjson.dumps({"study": "s", "n_trials": 2}))
    vf = tmp_path / "variants.json"
    vf.write_bytes(orjson.dumps(variants_with(STRONG, 1)))
    assert P.main(["--results", str(res), "--trials", str(tr), "--variants", str(vf), "--out", str(out)]) == 0
    rec = orjson.loads((out / "promotion.json").read_bytes())
    assert rec["candidates"][0]["decision"] == "PROMOTE" and set(rec["sources"]) == {str(res), str(tr), str(vf)}
    assert "PROMOTE" in capsys.readouterr().out
