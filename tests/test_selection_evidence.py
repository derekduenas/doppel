"""Exercise the same on-chain reconstruction and receipt that the selection reader consumes."""
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from doppel.lab import absorb as ab
from doppel.lab.ordertape import Pool
from doppel.lab.selection_evidence import WEEK, window_profit
from test_absorb import AD, COND, NO, O, U, W, YES, FakeChain, _pages

START = 1002
END = START + WEEK


def metadata():
    return {t: {"token": t, "condition": "0x" + COND, "idx": i, "status": "resolved",
                "slug": "btc-updown-5m-1200", "end": "1970-01-01T00:25:00Z", "payout": 1 - i}
            for i, t in enumerate((YES, NO))}


@pytest.fixture
def chain(tmp_path):
    network = FakeChain(head=302401)
    network.open[ab.PUSD, W, "cash"] = 100 * U
    network.cache = tmp_path / "rpc.jsonl"
    return network


def absorb(chain, fills=(), pairs=None):
    client = ab.Chain(Pool(["local"], chain, rate=1e9, sleep=lambda _: None), chain.cache)
    return ab.absorb_wallet(client, W, list(fills), (1, 302400), (START, END), 302400,
                            _pages(W, START, END, fills),
                            {YES: (COND, 0), NO: (COND, 1)} if pairs is None else pairs)


def test_after_fee_resolved_profit_from_chain_excludes_deposit(chain):
    fills = [chain.fill("0x01", 101, "BUY", YES, 10, .4, fee=50_000),
             chain.fill("0x02", 103, "BUY", NO, 10, .45)]
    chain.cash("0x03", 108, O, W, 300 * U)
    report = absorb(chain, fills)
    assert report["status"] == "reconciled"
    assert report["cash"]["by_kind"] == {"buy": -8.55, "cash": 300.0}
    assert window_profit(report, metadata(), W, START, END) == {COND: 1_450_000}
    # A compensating edit moves cash between a scoped trade and an unrelated deposit.
    report["condition_cash"][0]["cash"] += U
    report["condition_cash"][-1]["cash"] -= U
    with pytest.raises(ValueError, match="cash or fee totals"):
        window_profit(report, metadata(), W, START, END)


@pytest.mark.parametrize("finish,profit", [("merge", 0), ("redeem", 0), ("sell", 2_450_000)])
def test_ctf_cash_counted_once_across_both_outcomes(chain, finish, profit):
    chain.cash("0x01", 101, W, ab.PUSD, 10 * U)
    chain.batch("0x01", 101, AD, W, [YES, NO], [10 * U, 10 * U])
    chain.ctf_event("0x01", 101, "split", AD, 10 * U)
    fills = []
    if finish == "sell":
        fills = [chain.fill("0x02", 102, "SELL", YES, 10, .75, fee=50_000),
                 chain.fill("0x03", 103, "SELL", NO, 10, .5)]
    else:
        chain.batch("0x02", 252, W, AD, [YES, NO], [10 * U, 10 * U])
        chain.ctf_event("0x02", 252, finish, AD, 10 * U)
        chain.cash("0x02", 252, ab.ZERO, W, 10 * U)
    report = absorb(chain, fills)
    assert window_profit(report, metadata(), W, START, END) == {COND: profit}


def test_no_trade_is_only_zero_if_coverage_proves_it(chain):
    report = absorb(chain)
    assert window_profit(report, {}, W, START, END) == {}
    for key in ("goldsky_complete", "chain_scan_complete", "goldsky_store_verified", "block_time_window_verified"):
        bad = deepcopy(report)
        bad["completeness"][key] = False
        with pytest.raises(ValueError, match="incomplete pagination"):
            window_profit(bad, {}, W, START, END)
    missing = deepcopy(report)
    missing["chain_legs"] = 2
    with pytest.raises(ValueError, match="chain legs disagree"):
        window_profit(missing, {}, W, START, END)
    del report["condition_cash"]
    with pytest.raises(ValueError, match="missing condition cash receipt"):
        window_profit(report, {}, W, START, END)


def test_wrong_week_wallet_report_old_position_or_bad_metadata_is_unscorable(chain):
    row = chain.fill("0x01", 101, "BUY", YES, 10, .4)
    chain.open[ab.CTF, W, NO] = U
    report = absorb(chain, [row])
    with pytest.raises(ValueError, match="opening inventory"):
        window_profit(report, metadata(), W, START, END)
    with pytest.raises(ValueError, match="one complete week"):
        window_profit(report, metadata(), W, START, END - 1)
    with pytest.raises(ValueError, match="wallet or weekly bounds"):
        window_profit(report, metadata(), O, START, END)
    for missed in (YES, NO):
        tmap = metadata()
        del tmap[missed]
        with pytest.raises(ValueError, match="missing token metadata"):
            window_profit(report, tmap, W, START, END)
    for key, value, message in (("payout", 1, "not complementary"), ("idx", 0, "indices disagree"),
                                ("status", "open", "unresolved"), ("end", "2020-01-01", "close disagrees"),
                                ("slug", "other", "inconsistent condition slug")):
        tmap = metadata()
        tmap[NO][key] = value
        with pytest.raises(ValueError, match=message):
            window_profit(report, tmap, W, START, END)


def test_week_boundary_and_out_of_scope_events(chain):
    report = absorb(chain, [chain.fill("0x01", 101, "BUY", YES, 10, .4)])
    tmap = metadata()
    for rec in tmap.values():
        rec.update(slug="btc-updown-5m-900", end=datetime.fromtimestamp(1200, UTC).isoformat())
    with pytest.raises(ValueError, match="crosses the weekly boundary"):
        window_profit(report, tmap, W, START, END)
    for rec in tmap.values():
        rec.update(slug="presidential-election", payout=None, status="open")
    assert window_profit(report, tmap, W, START, END) == {}


def test_unattributed_transfers_and_ctf_cash_are_unscorable(chain):
    chain.cash("0x01", 101, W, ab.PUSD, 10 * U)
    chain.cash("0x01", 101, O, W, 3 * U)  # unrelated payment in the split transaction
    chain.batch("0x01", 101, AD, W, [YES, NO], [10 * U, 10 * U])
    chain.ctf_event("0x01", 101, "split", AD, 10 * U)
    report = absorb(chain)
    assert report["status"] == "reconciled"
    with pytest.raises(ValueError, match="unexplained share or cash"):
        window_profit(report, metadata(), W, START, END)


def test_transfer_of_scoped_shares_cannot_be_ranked_as_profit(chain):
    chain.single("0x01", 101, O, W, YES, 10 * U)
    report = absorb(chain)
    assert report["status"] == "reconciled"
    with pytest.raises(ValueError, match="unsupported share transfer"):
        window_profit(report, metadata(), W, START, END)


def test_losing_outcome_and_incomplete_market_metadata(chain):
    report = absorb(chain, [chain.fill("0x01", 101, "BUY", NO, 10, .4, fee=20_000)])
    assert window_profit(report, metadata(), W, START, END) == {COND: -4_020_000}
    tmap = {k: v | {"slug": "other-market", "status": "unknown"} for k, v in metadata().items()}
    with pytest.raises(ValueError, match="unknown token metadata status"):
        window_profit(report, tmap, W, START, END)
    report["tokens"][NO]["path"][0][2] = 0
    with pytest.raises(ValueError, match="disagrees with token path"):
        window_profit(report, metadata(), W, START, END)


def test_ambiguous_cross_condition_cash_is_not_allocated(chain):
    other = "dd" * 32
    tmap = metadata()
    for t, i in [("13", 0), ("14", 1)]:
        tmap[t] = tmap[YES if i == 0 else NO] | {"token": t, "condition": other, "slug": "eth-updown-5m-1200"}
    chain.batch("0x01", 101, AD, W, [YES, "13"], [10 * U, 10 * U])
    chain.cash("0x01", 101, W, ab.PUSD, 10 * U)
    chain.ctf_event("0x01", 101, "split", AD, 10 * U)
    report = absorb(chain, pairs={k: (v["condition"].removeprefix("0x"), v["idx"]) for k, v in tmap.items()})
    with pytest.raises(ValueError, match="cross-condition"):
        window_profit(report, tmap, W, START, END)


def test_cash_swapped_between_same_kind_fills_does_not_transfer_profit_between_events(chain):
    other = "dd" * 32
    tmap = metadata()
    for t, i in [("13", 0), ("14", 1)]:
        tmap[t] = tmap[YES if i == 0 else NO] | {"token": t, "condition": other, "slug": "eth-updown-5m-1200"}
    fills = [chain.fill("0x01", 101, "BUY", YES, 10, .4, fee=20_000),
             chain.fill("0x02", 102, "BUY", "13", 10, .6)]
    report = absorb(chain, fills, pairs={k: (v["condition"].removeprefix("0x"), v["idx"]) for k, v in tmap.items()})
    assert window_profit(report, tmap, W, START, END) == {COND: 5_980_000, other: 4_000_000}
    conflicting = {k: v | {"slug": "btc-updown-5m-1200"} for k, v in tmap.items()}
    with pytest.raises(ValueError, match="multiple conditions for one event"):
        window_profit(report, conflicting, W, START, END)
    first, second = report["condition_cash"]
    first["cash"], second["cash"] = second["cash"], first["cash"]
    with pytest.raises(ValueError, match="per-token cash or fees disagree"):
        window_profit(report, tmap, W, START, END)
