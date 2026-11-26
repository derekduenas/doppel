import json
from pathlib import Path

from doppel.lab.ordertape import decode_match

FX = json.loads((Path(__file__).parent / "fixtures_ordertape" / "match_0x4f5bc2.json").read_text())


def test_decoded_order_hashes_to_the_goldsky_order_id():
    """Real tx 0x4f5bc2…f878: the maker order of cohort wallet 0x0cb0… must hash to the order_id
    Goldsky reports for its fill, and expose the full size the fill hides."""
    orders = decode_match(FX["input"], FX["to"])
    assert orders is not None and [o["role"] for o in orders] == ["taker", "maker", "maker"]
    m = orders[2]
    assert m["maker"] == "0x0cb038487586d1119b165466072e9baf666f3a90"
    assert m["order_id"] == "dc1c320ccdba22d0474d2f04aef10028fb1d1342367ed34ffac09560a5381762"
    assert (m["side"], m["size"], round(m["limit"], 6), m["fill_amount"]) == ("BUY", 97.0, 0.23, 343506)
    assert m["created_ms"] == 1795633584086 and m["sig_type"] == 0
    t = orders[0]
    assert (t["side"], round(t["limit"], 4), t["n_makers"]) == ("BUY", 0.87, 2)


def test_other_calls_and_other_contracts_are_not_decoded():
    assert decode_match("0xdeadbeef" + "00" * 64, FX["to"]) is None
    assert decode_match(FX["input"], "0x" + "11" * 20) is None
