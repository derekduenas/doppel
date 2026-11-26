import math

import numpy as np
import pytest

from doppel.strategy import sequential as seq
from doppel.strategy.genome import Costs, Exit, Genome, MakerRule, Risk, Source, Universe

ALPHA = 0.05
B = 100.0          # bound (cents) for the simulations: tight, so the bets are aggressive
N_PATHS = 400
N_BLOCKS = 150


def _cross(values, bound=B, sign=+1):
    """Did the running max of the e-process ever reach 1/alpha (Ville rejection under peeking)?"""
    y = seq.block_means(values, seq.BLOCK)
    return seq._direction(y, bound, sign, ALPHA)["rejects"]


def _sim(draw, sign=+1, seed=0):
    rng = np.random.default_rng(seed)
    x = draw(rng, (N_PATHS, N_BLOCKS * seq.BLOCK))
    return float(np.mean([_cross(list(row), B, sign) for row in x]))


def _sym_normal(rng, shape):
    return np.clip(rng.normal(0.0, 30.0, shape), -B, B)           # symmetric -> mean exactly 0


def _maker_like(rng, shape):
    # +10c on 90% of windows, -90c on 10%: mean exactly 0, a fat left tail inside the loss bound
    return np.where(rng.random(shape) < 0.1, -90.0, 10.0)


def _ar1(rng, shape, phi=0.5):
    e = rng.normal(0.0, 30.0 * math.sqrt(1 - phi * phi), shape)
    x = np.empty(shape)
    x[:, 0] = rng.normal(0.0, 30.0, shape[0])
    for t in range(1, shape[1]):
        x[:, t] = phi * x[:, t - 1] + e[:, t]
    return np.clip(x, -B, B)                                         # symmetric -> mean 0


@pytest.mark.parametrize("draw", [_sym_normal, _maker_like, _ar1], ids=["normal", "maker_like", "ar1"])
def test_peeking_false_rejection_at_most_alpha(draw):
    # boundary null (mean exactly 0): both nulls hold; check after EVERY block (continuous peeking)
    assert _sim(draw, +1, seed=1) <= ALPHA
    assert _sim(draw, -1, seed=2) <= ALPHA


def test_naive_peeking_would_inflate():
    # the same data with a one-sided z-test at every block: the error the e-process prevents
    rng = np.random.default_rng(3)
    x = _sym_normal(rng, (N_PATHS, N_BLOCKS * seq.BLOCK))
    hits = 0
    for row in x:
        y = np.array(seq.block_means(list(row), seq.BLOCK))
        n = np.arange(1, len(y) + 1)
        m = np.cumsum(y) / n
        sd = np.array([y[:k].std(ddof=1) if k > 1 else np.inf for k in n])
        hits += bool(np.any((m / (sd / np.sqrt(n)))[2:] > 1.645))
    assert hits / N_PATHS > 3 * ALPHA


def test_family_e_holm_fwer_with_lock_in():
    # global null over a family of 6: a pass at ANY peek, locked in, counts as a family error
    rng = np.random.default_rng(4)
    m, n_paths, n_blocks = 6, 250, 100
    errors = 0
    for _ in range(n_paths):
        paths = [seq.betting_path([v / B for v in seq.block_means(list(_sym_normal(rng, (1, n_blocks * 12))[0]))])
                 for _ in range(m)]
        run = np.maximum.accumulate(np.exp(np.maximum(np.array(paths), 0.0)), axis=1)
        errors += any(min(seq.e_holm(list(run[:, t]))) <= ALPHA for t in range(n_blocks))
    assert errors / n_paths <= ALPHA


def test_power_early_pass_and_kill():
    rng = np.random.default_rng(5)
    up = np.clip(rng.normal(15.0, 30.0, (100, 100 * 12)), -B, B)
    assert np.mean([_cross(list(r), B, +1) for r in up]) >= 0.9
    assert np.mean([_cross(list(r), B, -1) for r in up]) == 0.0
    assert np.mean([_cross(list(-r), B, -1) for r in up]) >= 0.9


def test_predictable_bets_and_bounds():
    z = [0.3, -0.2, 0.5, 0.1, 0.4]
    p = seq.betting_path(z)
    assert p[0] == 0.0                                   # first bet is 0: no past data
    for k in range(1, len(z)):                           # changing z_k never changes earlier wealth
        z2 = z[:k] + [-1.0] * (len(z) - k)
        assert seq.betting_path(z2)[:k] == p[:k]
    assert seq.betting_path([1.0] * 50)[-1] < 50 * math.log(1.5) + 1e-9   # lambda capped at 1/2
    with pytest.raises(ValueError):
        seq.betting_path([1.5])


def test_clipping_and_violations():
    y = [250.0, -250.0, 10.0]
    s = seq._direction(y, B, +1, ALPHA)
    assert s["clipped_conservative"] == 1 and s["bound_violations"] == 1
    k = seq._direction(y, B, -1, ALPHA)
    assert k["clipped_conservative"] == 1 and k["bound_violations"] == 1
    assert seq.block_means(list(range(30)), 12) == [5.5, 17.5]          # partial block dropped
    r = seq.e_processes([1.0] * 30, B)
    assert r["n_blocks"] == 2 and r["windows_used"] == 24 and r["pass"]["e"] >= 1.0


def test_eval_windows_filter_and_order():
    rows = [{"start": 300, "end": 600, "eligible": True, "net_c": 1},
            {"start": 0, "end": 900, "eligible": True, "net_c": 2},       # 15m window ending later
            {"start": 0, "end": 300, "eligible": True, "net_c": 3},
            {"start": -300, "end": 0, "eligible": True, "net_c": 9},      # before the eval window
            {"start": 600, "end": 900, "eligible": False, "net_c": 9},    # not eligible
            {"start": 900, "end": 1200, "eligible": True, "net_c": 9}]    # after as_of
    got = seq.eval_windows(rows, 0, 3600, as_of_s=900)
    assert [r["net_c"] for r in got] == [3, 1, 2]


def _stats(pass_e=1.0, kill_e=1.0, pv=0, kv=0):
    def d(e, v):
        return {"e": e, "e_max": e, "rejects": e >= 1 / ALPHA, "first_cross_block": 5 if e >= 1 / ALPHA else None,
                "bound_violations": v}
    return {"pass": d(pass_e, pv), "kill": d(kill_e, kv)}


def test_decide_rules():
    assert seq.decide(_stats(70.0), 0.04, 400, 300)[0] == "EARLY_PASS"
    dec, why = seq.decide(_stats(25.0), 0.12, 400, 300)
    assert dec == "CONTINUE" and "family e-Holm" in why
    dec, why = seq.decide(_stats(70.0), 0.04, 100, 300)
    assert dec == "CONTINUE" and "min_windows" in why               # sealed min_windows honoured for PASS
    dec, why = seq.decide(_stats(70.0, pv=1), 0.04, 400, 300)
    assert dec == "CONTINUE" and "loss bound failed" in why
    assert seq.decide(_stats(kill_e=30.0), 1.0, 10, 300)[0] == "EARLY_KILL"  # kill needs no min_windows
    assert seq.decide(_stats(kill_e=30.0, kv=2), 1.0, 10, 300)[0] == "CONTINUE"
    assert seq.decide(_stats(), 1.0, 400, 300)[0] == "CONTINUE"


def test_e_holm_and_params():
    assert seq.e_holm([70.0, 1.0, 1.0]) == pytest.approx([3 / 70, 1.0, 1.0])
    assert seq.anytime_p(0.3) == 1.0 and seq.anytime_p(40.0) == pytest.approx(0.025)
    g = Genome(name="m", mode="maker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
               maker=MakerRule(), risk=Risk(max_notional_per_window=40.0))
    prm = seq.params(g)
    assert prm["bound_c"] == 4000.0 and prm["block"] == 12 and prm["alpha"] == 0.05   # zero-fee maker: exact
    flat = Genome(name="m", mode="maker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
                  maker=MakerRule(), risk=Risk(max_notional_per_window=40.0), costs=Costs(taker_fee_coef=0.1),
                  exit=Exit(hold_to_settlement=False, flatten_tte_s=10))
    assert seq.loss_bound_c(flat) == pytest.approx(4400.0)                           # crosses: fee slack
