"""Economic labels may establish a first execution. They cannot establish a placement."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from doppel.tape.universe import intl_slug

source = Path(__file__).resolve().parents[1] / "evidence/2026-11-27-wallet-rivals/real_study.py"
spec = spec_from_file_location("wallet_rivals_real_study", source)
study = module_from_spec(spec)
spec.loader.exec_module(study)


def test_real_execution_target_is_first_signed_receipt_with_unknowns_and_complete_market_denominators():
    start = study.START
    slug = intl_slug("btc", "5m", start)
    later_slug = intl_slug("btc", "5m", start + 300)
    cells = [{
        "cutoff_s": start + t,
        "capture": "gap" if t == 50 else "covered",
        "book": None if t == 60 else {"bid": .07 if t <= 30 else .08}
    } for t in range(0, 600, 10)]
    public = {"start": start, "end": start+600, "cells": cells}
    wallet = study.CANDIDATE
    f = {"side": "BUY", "role": "maker", "idx": 1, "slug": slug, "block_ts": start+800,
         "id": "first", "tx": "one"}
    fills = [f, f | {"tx": "partial"}, f | {"id": "ambiguous", "tx": "unknown"},
             f | {"role": "taker", "id": "taker"}, f | {"idx": 0, "id": "up"},
             f | {"side": "SELL", "id": "sell"}]
    # The two observed fills in one signed order supply one first execution target.
    # Its cutoff boundary is included; a print at that same time was outside the prior cell.
    observed = {"price": .07, "anchor_status": "located", "anchor": {"tw_ns": (start+10)*study.NS},
                "placement_identity": "unknown"}
    inspected = {(wallet, "first"): observed, (wallet, "ambiguous"): observed | {"anchor_status": "missing_print"}}
    labels, counts = study.wallet_targets(public, wallet, fills, inspected)
    assert [r["execution"] for r in labels[:7]] == [None, True, None, None, False, None, None]
    assert all(r["execution"] is False for r in labels[30:])
    assert all(r["action"] is None for r in labels)
    assert counts[slug]["fills_whole_reconciled_day"] == 6
    assert counts[slug]["fills_in_market_by_block_clock"] == 0
    assert counts[slug]["maker_orders"] == 4 and counts[slug]["down_buy_maker_orders"] == 2
    assert counts[slug]["located_qualifying_first_executions"] == counts[slug]["positive_cells"] == 1
    assert counts[later_slug]["fills_whole_reconciled_day"] == 0
    assert counts[later_slug]["observable_negative_cells"] == 30
