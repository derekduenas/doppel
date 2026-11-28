"""Answer key for latent fair-value inversion (docs/INVERSION.md): scripted wallets with known beliefs."""
import pytest

from doppel.lab import inversion as inv
from doppel.lab.xvenue import pmus_fee

TRUE_BETA, TRUE_T = 1.0, 0.25


@pytest.fixture(scope="module")
def key():
    return inv.answer_key(seeds=(1,))[1]


def covers(ci, v):
    return ci[0] <= v <= ci[1]


def test_taker_recovers_beta_within_ci(key):
    r = key["taker"]["filtered"]
    assert r["verdict"] == "IDENTIFIED"
    assert covers(r["lead"]["ci95"], TRUE_BETA)
    assert covers(r["temperature"]["ci95"], TRUE_T)
    assert r["claimed"] == ["lead"]            # the decoy imbalance is not claimed
    assert r["accuracy"] > r["acc_fair"] and r["accuracy"] > r["acc_mid"]


def test_random_wallet_not_identified(key):
    r = key["random"]["filtered"]
    assert r["verdict"] == "NOT_IDENTIFIED" and r["claimed"] == []
    assert any("margin slope" in x for x in r["reasons"])


def test_pure_fair_wallet_claims_no_feature(key):
    r = key["fair"]["filtered"]
    assert r["claimed"] == [] and covers(r["lead"]["ci95"], 0.0)
    assert abs(r["lead"]["value"]) < 0.05
    assert r["verdict"] == "NOT_IDENTIFIED"     # nothing beyond fair_v1 to identify


def test_capital_limit_bias_and_position_filter(key):
    unf, filt = key["capped"]["unfiltered"], key["capped"]["filtered"]
    bias_unf = abs(unf["temperature"]["value"] - TRUE_T)
    bias_filt = abs(filt["temperature"]["value"] - TRUE_T)
    assert not covers(unf["temperature"]["ci95"], TRUE_T)   # forced passes read as decision noise
    assert bias_filt <= 0.5 * bias_unf and covers(filt["temperature"]["ci95"], TRUE_T)
    assert filt["dropped"]["position_filter"] > 0
    assert covers(filt["lead"]["ci95"], TRUE_BETA)


def test_maker_only_real_shapes_not_identified():
    rulefit_row = {"kind": "order", "window": "btc-updown-5m-1", "up_side": "bid", "px_c": 48.0, "bb_c": 48.0,
                   "ba_c": 50.0, "depth_bid": 10, "depth_ask": 5, "fair_c": 51.0, "tte_s": 120.0, "inv": 0.0}
    layer3_row = {"wallet": "0xabc", "order_id": "0x1", "market": "m", "token": "t", "price": 0.48, "side": "BUY",
                  "anchor_status": "located", "context": {"signed": {"state": {"best_bid": 0.48, "best_ask": 0.5}}}}
    r = inv.invert([rulefit_row, layer3_row], venue="pmus", n_boot=10)
    assert r["verdict"] == "NOT_IDENTIFIED" and r["inputs"]["maker_posts_censored"] == 2
    assert "action" in r["missing_fields"] and r["maker_posts"]["fitted"] is False


def test_fee_models():
    f, coef = inv.fee_fn("pmus", None)
    assert f(0.5, 10) == pmus_fee(0.5, 10) / 10 and coef > 0
    with pytest.raises(ValueError):
        inv.fee_fn("intl", None)
