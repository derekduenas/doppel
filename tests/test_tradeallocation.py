"""Offline decoded-call allocation, conservation, and provenance."""

from dataclasses import replace

import pytest

from doppel.lab.intlbook import Print
from doppel.lab.tradeallocation import Allocation, allocation


def order(oid, role, token, side, price, shares, verified=False):
    cash = round(price * shares * 1_000_000)
    qty = shares * 1_000_000
    give, take = (cash, qty) if side == "BUY" else (qty, cash)
    return dict(order_id=oid, role=role, token_id=token, side=side, n_makers=3, maker_amount=give,
                taker_amount=take, fill_amount=give, verified=verified, condition_id="f1")


ROWS = [
    order("t", "taker", "A", "SELL", .05, 30),
    order("m1", "maker", "A", "BUY", .08, 10),
    order("m2", "maker", "B", "SELL", .92, 10, verified=True),
    order("m3", "maker", "A", "BUY", .07, 10),
]
EVENT = Print(123, 456, "A", .07, 30, "SELL", "tx", "0xf1")


def test_normalizes_signed_maker_amounts_in_order_of_execution():
    split = allocation(ROWS)
    assert isinstance(split, Allocation)
    assert split.levels == ((.08, 20), (.07, 10)) and split.match(EVENT, "B") == "allocated"
    assert split.match(replace(EVENT, asset="B", side="BUY", price=.93), "A") == "allocated"
    assert split.match(replace(EVENT, side="BUY"), "B") == "side_mismatch"
    assert split.match(replace(EVENT, market="0xaf"), "B") == "market_mismatch"
    assert split.match(EVENT, "other") == "asset_mismatch"
    assert split.match(replace(EVENT, price=.071), "B") == "allocated"
    assert split.match(replace(EVENT, price=.05), "B") == "allocated"
    assert split.match(replace(EVENT, price=.99), "B") == "price_mismatch"
    assert split.match(replace(EVENT, size=float("nan")), "B") == "quantity_mismatch"
    assert split.match(replace(EVENT, size=29.99), "B") == "quantity_mismatch"


@pytest.mark.parametrize(("rows", "reason"), [
    (ROWS[:-1], "incomplete_call"),
    (ROWS + ROWS, "incomplete_call"),
    ([r | {"verified": False} for r in ROWS], "unverified_call"),
    (ROWS[:-1] + [ROWS[1]], "duplicate_order"),
    (ROWS[:-1] + [ROWS[-1] | {"side": "SELL"}], "side_mismatch"),
    (ROWS[:-1] + [ROWS[-1] | {"maker_amount": 0}], "invalid_amount"),
    (ROWS[:-1] + [ROWS[-1] | {"condition_id": "af"}], "market_mismatch"),
])
def test_incomplete_or_inconsistent_call_cannot_supply_level_volume(rows, reason):
    assert allocation(rows) == reason
