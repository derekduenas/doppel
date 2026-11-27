"""Use optional RPC log times without silently overriding a header or wallet fill."""
import pytest

from doppel.lab import absorb
from test_absorb import COND, NO, O, U, W, YES, FakeChain, _run

PAIR = {YES: (COND, 0), NO: (COND, 1)}


def network(tmp_path):
    net = FakeChain()
    net.cache = tmp_path / "cache"
    net.open[absorb.PUSD, W, "cash"] = 100 * U
    return net


def test_log_timestamp_replaces_interpolation_without_a_header_request(tmp_path):
    net = network(tmp_path)
    # Deliberately uneven block intervals. The receipt records 14 seconds between the fills.
    net.single("0x01", 20, O, W, YES, 10 * U)
    net.logs[-1]["blockTimestamp"] = hex(1046)
    rows = [net.fill("0x02", 30, "BUY", NO, 10, 0.4)]
    result = _run(net, rows, pairs=PAIR)
    assert result["status"] == "reconciled"
    assert net.calls.count("eth_getBlockByNumber") == 4
    pair = result["pairing"]
    assert pair["interpolated_event_blocks"] == 0 and pair["log_timestamp_event_blocks"] == 1
    assert pair["markets"][COND]["longest_one_sided_s"] == 14


def test_unstamped_event_falls_back_to_counted_interpolation(tmp_path):
    net = network(tmp_path)
    net.single("0x01", 20, O, W, YES, 10 * U)
    rows = [net.fill("0x02", 30, "BUY", NO, 10, 0.4)]
    result = _run(net, rows, pairs=PAIR)
    assert result["status"] == "reconciled"
    assert result["pairing"]["interpolated_event_blocks"] == 1
    assert result["pairing"]["markets"][COND]["longest_one_sided_s"] == 20


@pytest.mark.parametrize("fault", ["same_block_time", "same_block_hash", "wrong_fill_time", "wrong_header_time",
                                   "descending", "outside", "malformed", "malformed_hash", "cached_header"])
def test_bad_log_anchors_cannot_certify_holding_time(tmp_path, fault):
    net = network(tmp_path)
    rows = [net.fill("0x01", 20, "BUY", YES, 10, 0.4)]
    net.single("0x02", 30, O, W, NO, 2 * U)
    if fault == "same_block_time":
        net.logs[0]["blockTimestamp"] = hex(1040)
        net.logs[1]["blockTimestamp"] = hex(1041)
    elif fault == "same_block_hash":
        net.logs[0]["blockHash"] = "0x" + "11" * 32
        net.logs[1]["blockHash"] = "0x" + "22" * 32
    elif fault == "wrong_fill_time":
        net.logs[0]["blockTimestamp"] = hex(1041)
    elif fault in ("wrong_header_time", "cached_header"):
        if fault == "cached_header":
            chain = absorb.Chain(absorb.Pool(["fixture"], net, rate=1e9), net.cache)
            assert chain.block_ts(30) == 1060
            net.logs[-1]["blockTimestamp"] = hex(1061)
        else:
            net.single("0x03", 100, O, W, NO, U)
            net.logs[-1]["blockTimestamp"] = hex(1199)
    elif fault == "descending":
        net.logs[-1]["blockTimestamp"] = hex(1038)
    elif fault == "outside":
        net.logs[-1]["blockTimestamp"] = hex(1202)
    elif fault == "malformed_hash":
        net.logs[-1]["blockHash"] = "0x12"
    else:
        net.logs[-1]["blockTimestamp"] = "1060"
    r = _run(net, rows, pairs=PAIR)
    assert r["status"] == "not_reconciled" and "block_time_evidence_disagrees" in r["causes"]
    assert r["pairing"]["event_time_anchors_consistent"] is False
    assert r["pairing"]["longest_one_sided_s"] is None
    assert all(m["paired_share_s"] is None for m in r["pairing"]["markets"].values())
