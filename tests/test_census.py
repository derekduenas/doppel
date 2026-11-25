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
    fp = fingerprint(rows, tm, [D])
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
