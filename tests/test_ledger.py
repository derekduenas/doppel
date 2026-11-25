from doppel.lab.ledger import DAY, TokenMap, g0_compare, wallet_ledger

D = 1795046400  # a UTC midnight


def _r(i, tok, side, sh, usd, fee, ts):
    return {"id": f"r{i}", "token_id": tok, "side": side, "amount_shares": sh, "amount_usdc": usd,
            "fee": fee, "block_timestamp": ts, "block_number": i}


def test_ledger_pnl_fees_censoring(tmp_path):
    tm = TokenMap(tmp_path / "t.jsonl")
    tm.m = {"UP": {"token": "UP", "status": "resolved", "payout": 1.0, "end": "2026-11-19T12:05:00Z"},
            "DN": {"token": "DN", "status": "resolved", "payout": 0.0, "end": "2026-11-19T12:05:00Z"},
            "OLD": {"token": "OLD", "status": "resolved", "payout": 1.0, "end": "2026-11-19T12:00:00Z"},
            "V2": {"token": "V2", "status": "unknown"}}
    rows = [_r(1, "UP", "BUY", 10, 4.0, 0.1, D + 100), _r(2, "UP", "SELL", 2, 1.2, 0.0, D + 200),
            _r(3, "DN", "BUY", 10, 5.0, 0.0, D + 300),
            _r(4, "OLD", "SELL", 5, 4.0, 0.0, D + 400),  # sold shares we never saw bought
            _r(5, "V2", "BUY", 1, 0.5, 0.0, D + 500)]
    led = wallet_ledger(rows, tm)
    # UP: 8 shares x $1 + 1.2 - 4.0 = 5.2 ; DN: 0 - 5.0 = -5.0 -> +0.2 on 11-19
    assert round(led["day_pnl"][D], 6) == 0.2
    assert round(led["day_fees"][D], 6) == 0.1
    assert led["stats"]["censored"] == 1 and led["stats"]["unknown"] == 1


def test_g0_compare_days_and_tolerance():
    led = {"span": [D - 10, D + 2 * DAY + 10], "day_pnl": {D: 100.0, D + DAY: 1000.0},
           "day_fees": {D: 10.0, D + DAY: 0.0}, "day_notional": {D: 500.0, D + DAY: 900.0}}
    pts = [{"timestamp": D + k * DAY, "trade_pnl": [0, 120, 1121][k], "fees_paid": [0, -10, -10][k],
            "volume_usdc": [0, 500, 1400][k]} for k in range(3)]
    res = g0_compare(led, pts)
    assert res["n_days"] == 2
    assert res["days"][0]["pnl_ok"] and res["days"][1]["pnl_ok"]  # 100 vs 120 within $50; 1000 vs 1001
    assert res["pass"]
    pts[1]["trade_pnl"] = 400
    assert not g0_compare(led, pts)["pass"]
