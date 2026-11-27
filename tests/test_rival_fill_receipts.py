from doppel.lab.intlbook import Print
from doppel.lab.ordermatch import PRINT_BLOCK_TOL_MS
from doppel.lab.rulefit import Inventory, bind_fill_receipts
from doppel.lab.tradeallocation import Allocation


def test_mirror_requires_prior_public_pair_and_conflict_only_changes_later_inventory():
    call = Allocation("m", "up", "down", "BUY", .8, ((.8, 10),), 10, .000002)
    fills = [{"tx": "tx", "token": "up", "role": "taker", "side": "BUY", "price": .8,
              "shares": 10, "block_ts": 0}]
    mates = {"up": "down", "down": "up"}
    mirror = Print(50, 100, "down", .2, 10, "SELL", "tx", "m")
    native = Print(50, 200, "up", .8, 10, "BUY", "tx", "m")
    later = Print(51, 400, "up", .8, 10, "BUY", "tx", "m")
    prints = {"tx": [mirror, native]}
    bind_fill_receipts(fills, prints, mates, {"tx": call}, {"down": 100})
    assert fills[0]["receipt_tw"] == 200
    bind_fill_receipts(fills, prints, mates, {"tx": call}, {"down": 99})
    assert fills[0]["receipt_tw"] == 100
    prints["tx"].append(later)
    bind_fill_receipts(fills, prints, mates, {"tx": call}, {"down": 99})
    row, = fills
    assert (row["receipt_tw"], row["first_compatible_tw"], row["conflict_tw"]) == (None, 100, 400)
    inv = Inventory(fills, {"up": {"slug": "m", "idx": 0}}, {})
    assert inv.at("m", 100) == 0 and inv.at("m", 400) == 10 and inv.at("m", 401) is None
    assert inv.window_notional({"m"}, 400) == [8] and inv.window_notional({"m"}, 401) == []


def test_native_fill_uses_same_venue_to_block_window_as_order_matching():
    fill = {"tx": "tx", "token": "up", "role": "maker", "side": "BUY", "shares": 2, "price": .4, "block_ts": 1000}
    boundary = fill["block_ts"] * 1000 + PRINT_BLOCK_TOL_MS
    inside = Print(boundary, 100, "up", .4, 2, "SELL", "tx", "m")
    outside = Print(boundary + 1, 90, "up", .4, 2, "SELL", "tx", "m")
    bind_fill_receipts([fill], {"tx": [outside, inside]}, {}, {})
    assert fill["first_compatible_tw"] == fill["receipt_tw"] == 100
    bind_fill_receipts([fill], {"tx": [outside]}, {}, {})
    assert fill["first_compatible_tw"] is None and fill["receipt_tw"] is None
    fill["block_ts"] = None
    bind_fill_receipts([fill], {"tx": [inside]}, {}, {})
    assert fill["receipt_tw"] is None
