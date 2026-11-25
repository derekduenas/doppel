from doppel.lab.census import fingerprint, label
from doppel.lab.ledger import TokenMap

D = 1795046400
S = D + 3600  # window start


def test_fingerprint_locked_vs_directional(tmp_path):
    tm = TokenMap(tmp_path / "t.jsonl")
    slug = f"btc-updown-5m-{S}"
    tm.m = {"U": {"token": "U", "status": "resolved", "payout": 1.0, "slug": slug, "idx": 0},
            "D": {"token": "D", "status": "resolved", "payout": 0.0, "slug": slug, "idx": 1}}
    rows = [{"token_id": "U", "side": "BUY", "amount_shares": 10, "amount_usdc": 4.8, "price": 0.48,
             "fee": 0.0, "order_type": "maker", "block_timestamp": S + 100},
            {"token_id": "D", "side": "BUY", "amount_shares": 10, "amount_usdc": 4.9, "price": 0.49,
             "fee": 0.0, "order_type": "maker", "block_timestamp": S + 110}]
    fp = fingerprint(rows, tm, lambda w: True)
    assert fp["gross"] == 0.3 and fp["locked"] == 0.3 and fp["directional"] == 0.0
    assert fp["paired_windows"] == 1 and label(fp) == "COMPLETE_SET_MAKER"


def test_labels():
    base = {"px_ge95_share": 0.0, "maker_share": 0.1, "taker_tte_p50": 120, "windows": 10,
            "paired_windows": 0}
    assert label(base) == "LATE_TAKER"
    assert label({**base, "taker_tte_p50": 700}) == "EARLY_TAKER"
    assert label({**base, "px_ge95_share": 0.8}) == "ENDGAME"
    assert label({**base, "maker_share": 0.9}) == "DIRECTIONAL_MAKER"
    assert label({**base, "maker_share": 0.5}) == "MIXED"


def test_rebates_attributed_by_window_share(tmp_path):
    from doppel.lab.census import attribute_income
    tm = TokenMap(tmp_path / "t.jsonl")
    tm.m = {"W": {"token": "W", "status": "resolved", "payout": 1.0, "idx": 0,
                  "slug": f"btc-updown-5m-{S}"},
            "X": {"token": "X", "status": "resolved", "payout": 1.0, "idx": 0, "slug": "nba-game"}}
    rows = [{"token_id": "W", "amount_usdc": 100.0, "block_number": 150},
            {"token_id": "X", "amount_usdc": 900.0, "block_number": 160},
            {"token_id": "W", "amount_usdc": 50.0, "block_number": 999}]  # outside the interval
    ivs = [{"sa": 100, "sb": 200, "api": {"volume": 1000.0, "trade_pnl": 40.0},
            "api_income": {"maker_rebate": 30.0, "taker_rebate": 10.0, "reward_income": 0.0}}]
    inc, wv, av, real = attribute_income(rows, tm, ivs)
    assert wv == 100.0 and av == 1000.0 and real == 40.0
    assert inc == {"maker_rebate": 3.0, "taker_rebate": 1.0, "reward_income": 0.0}  # 10 % share
