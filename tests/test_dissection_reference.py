"""Receipt-clock regressions for decision-time reference, inventory and taker inputs."""
import math

import orjson
import pytest

from doppel.lab.brti_import import load_raw_rcv
from doppel.lab.intlbook import NS, Print
from doppel.lab.rulefit import Fair, bind_fill_receipts, fit_taker
from doppel.tape.segment import SegmentWriter
from doppel.twin.features import RefStore

START = 1795629600


def test_late_boundary_revisions_stay_out_of_decision_features(tmp_path):
    raw = SegmentWriter(tmp_path, "ref_brti_raw")
    for t in range(START - 350, START + 100):
        raw.append(orjson.dumps({"t_ms": t*1000, "v": 100+(t%3), "received": (t+0.1)*1000}).decode(), tw=t*NS)
    raw.close()
    pub = SegmentWriter(tmp_path, "ref_prices")

    def boundary(t, price, seen):
        pub.append(orjson.dumps({"series": [{"candles": [{"timestamp": t, "close": price}]}]}).decode(),
                   tw=seen, meta={"asset": "crypto:btc", "status": 200, "from": t, "to": t})

    receipt = (START+10)*NS
    boundary(START-1, 1000, (START-1)*NS)  # cannot have received the final candle before its close
    boundary(START-1, 101, receipt)
    boundary(START-1, 110, receipt+20*NS)
    boundary(START-1, 109, receipt+20*NS)  # contradictory revisions at one receipt
    boundary(START-1, 103, receipt+30*NS)
    boundary(START+299, 102, (START+400)*NS)
    pub.close()
    fair = Fair(tmp_path, START, START+300)
    info = {"asset": "btc", "start": START, "end": START+300}
    model = fair._features(info)
    assert model.ref.ptb == 103 and model.ref.outcome == 0
    assert model.ref.opening_at(receipt-1) == (None, None)
    assert model.ref.opening_at(receipt+20*NS) == (receipt+20*NS, None)
    assert model.ref.opening_at(receipt) == (receipt, 101)
    assert fair.evidence(info, receipt)["fair_c"] is None
    assert math.isnan(model.at(START+9, None, 0)["dist_to_open_usd"])
    prior = fair.evidence(info, receipt+1)
    assert prior["fair_c"] is not None and prior["opening_value"] == 101 and prior["raw_seconds_known"] == 301
    assert math.isfinite(model.at(START+10, None, 0)["dist_to_open_usd"])


def test_raw_import_needs_documented_receipt_and_uses_earliest_observation(tmp_path):
    raw = SegmentWriter(tmp_path, "ref_brti_raw")
    for offset, recv in [(0, None), (1, None), (2, START-1), (3, START+8)]:
        t = START + offset
        raw.append(orjson.dumps({"t_ms": t*1000, "v": 110, "received": recv}).decode(), tw=t*NS)
    raw.close()
    live = SegmentWriter(tmp_path, "ref_brti_live")
    live.append("", tw=(START+5)*NS, meta={"new": [[(START+1)*1000, 100], [(START+3)*1000, 105]]})
    live.close()
    got = load_raw_rcv(tmp_path)
    values, clocks = RefStore(tmp_path).brti_rcv(START, START+3)
    for i in range(4):
        assert got[START+i][0] == values[i]
        if i in (0, 2):
            assert math.isnan(got[START+i][1]) and math.isnan(clocks[i])
        else:
            assert got[START+i] == (100 if i == 1 else 105, START+5)
            assert clocks[i] == START+5


def test_only_matching_execution_print_can_supply_fill_time():
    row = {"tx": "tx", "token": "down", "condition": "market", "role": "maker", "side": "BUY",
           "price": 0.07, "shares": 60}
    def event(t, **changes):
        args = {"venue_ms": 1000, "tw_ns": t, "asset": "down", "price": 0.07, "size": 60,
                "side": "SELL", "tx": "tx", "market": "market"}
        return Print(**(args | changes))
    invalid = [event(1, asset="other"), event(2, market="other"), event(3, size=200),
               event(4, price=0.08), event(5, side="BUY")]
    bind_fill_receipts([row], {"tx": invalid}, {}, {})
    assert row["receipt_tw"] is None
    bind_fill_receipts([row], {"tx": invalid + [event(8), event(7)]}, {}, {})
    assert row["receipt_tw"] == 7


@pytest.mark.parametrize("row,reason", [
    ({"located": False, "tw": None}, "print receipts"),
    ({"located": True, "tw": NS, "identity_observed": False}, "market identity"),
])
def test_taker_abstains_on_uncertain_time_or_market(row, reason):
    result = fit_taker({"takes": [row], "tok": {}, "res": {"grid": {}}, "fair": None})
    assert result["verdict"] is None and not result["found"] and reason in result["reason"]
