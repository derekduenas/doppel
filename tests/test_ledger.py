from doppel.lab.ledger import DAY, TokenMap, g0_compare, g0_verdict, wallet_ledger

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
    assert res["n_days"] == 2 and res["pnl_ok"] and res["fees_ok"] and res["volume_ratio"] == 1.0
    stats = {"tokens": 10, "censored": 0, "total_notional": 1400.0, "resolved_notional": 1400.0}
    assert g0_verdict(stats, res) == "PASS"
    pts[2]["trade_pnl"] = 1500  # span total 1100 vs 1500 -> fail
    assert g0_verdict(stats, g0_compare(led, pts)) == "FAIL"
    assert g0_verdict({**stats, "resolved_notional": 100.0}, res) == "NOT_COMPARABLE:resolved_coverage"
    assert g0_verdict({**stats, "censored": 5}, res) == "NOT_COMPARABLE:opening_inventory"
    assert g0_verdict({**stats, "unresolved_exposure": 900.0}, res) == "NOT_COMPARABLE:unresolved_exposure"


def test_g0_uses_snapshot_blocks_when_available():
    led = {"span": [D - 10, D + 2 * DAY], "span_blocks": [100 - 1800, 400],
           "day_pnl": {}, "day_fees": {}, "day_notional": {},
           "fills_b": [(150, 1.0, 100.0), (210, 2.0, 50.0), (260, 0.5, 10.0)],
           "tok_pnl": [(D + 50, 7.0)], "bt": [(100, D), (400, D + 600)]}
    # snapshot taken at block 200 (not at midnight): fills at 210 and 260 belong to the next interval
    pts = [{"timestamp": D, "source_block": 120, "trade_pnl": 0, "fees_paid": 0, "volume_usdc": 0},
           {"timestamp": D + DAY, "source_block": 200, "trade_pnl": 7, "fees_paid": -1, "volume_usdc": 100},
           {"timestamp": D + 2 * DAY, "source_block": 300, "trade_pnl": 7, "fees_paid": -3.5, "volume_usdc": 160}]
    res = g0_compare(led, pts)
    assert res["n_days"] == 2
    assert res["days"][0]["ours"] == {"trade_pnl": 7.0, "fees": 1.0, "volume": 100.0,
                                      "boundary_exposure": 0.0}
    assert res["days"][1]["ours"]["volume"] == 60.0 and res["volume_ratio"] == 1.0


def test_boundary_exposure_counts_only_open_positions():
    from doppel.lab.ledger import boundary_exposure
    led = {"bt": [(100, D), (400, D + 600)],
           "tok_moves": {"A": [(110, 10.0)], "B": [(120, 5.0), (130, -5.0)], "C": [(150, 3.0)]},
           "tok_end": {"A": D + 1000, "B": D + 1000, "C": D + 60}}  # C ended at ~block 130
    assert boundary_exposure(led, 200) == 10.0  # A open, B flat, C already ended
    assert boundary_exposure(led, 105) == 0.0
