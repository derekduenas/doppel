"""Adversarial ledger cases where aggregate weekly balances can still agree."""
from copy import deepcopy

import pytest

from doppel.lab import absorb as ab
from test_absorb import AD, COND, NO, O, U, W, YES, FakeChain, _run


def network(tmp_path, cls=FakeChain):
    chain = cls()
    chain.cache = tmp_path / "cache.jsonl"
    chain.open[ab.PUSD, W, "cash"] = 100 * U
    return chain


def test_opposite_omitted_collateral_logs_do_not_cancel(tmp_path):
    class OmittedWrapperLogs(FakeChain):
        def __call__(self, url, method, params):
            rows = super().__call__(url, method, params)
            if method == "eth_getLogs":
                return [r for r in rows if r["transactionHash"] != "0x02"]
            return rows

    chain = network(tmp_path, OmittedWrapperLogs)
    chain.cash("0x02", 12, W, O, 5 * U, token=ab.PUSD)
    chain.cash("0x02", 12, O, W, 5 * U, token=ab.USDCE)
    result = _run(chain, [])
    assert result["reconciliation"]["cash"]["residual"] == 0
    assert result["status"] == "not_reconciled"
    assert "collateral_logs_do_not_explain_balances" in result["causes"]
    collateral = result["collateral_reconciliation"]
    assert collateral[ab.PUSD]["residual_micro"] == -5 * U
    assert collateral[ab.USDCE]["residual_micro"] == 5 * U


def test_same_tx_extra_opposing_share_movements_are_visible(tmp_path):
    chain = network(tmp_path)
    fills = [chain.fill("0x01", 20, "BUY", YES, 10, 0.4)]
    chain.single("0x01", 20, O, W, YES, 2 * U)
    chain.single("0x01", 20, W, O, YES, 2 * U)
    result = _run(chain, fills)
    assert result["reconciliation"][YES]["residual"] == 0
    assert result["fill_check"]["tx_asset_mismatches"] == 0
    assert result["fill_check"]["unexplained_gross_share_movements"] == 1
    assert result["status"] == "not_reconciled"


def test_same_tx_extra_opposing_cash_movements_are_visible(tmp_path):
    chain = network(tmp_path)
    fills = [chain.fill("0x01", 20, "BUY", YES, 10, 0.4)]
    chain.cash("0x01", 20, O, W, 2 * U)
    chain.cash("0x01", 20, W, O, 2 * U)
    result = _run(chain, fills)
    assert result["reconciliation"]["cash"]["residual"] == 0
    assert result["fill_check"]["tx_asset_mismatches"] == 0
    assert result["fill_check"]["unexplained_gross_cash_movements"] == 1
    assert "fill_transaction_has_unexplained_cash_movements" in result["causes"]


def test_same_block_fills_follow_exchange_log_index(tmp_path):
    chain = network(tmp_path)
    buy = chain.fill("0xff", 20, "BUY", YES, 10, 0.4)
    sell = chain.fill("0x01", 20, "SELL", YES, 10, 0.6)
    for index, log in enumerate(chain.logs):
        log["logIndex"] = hex(index)
    result = _run(chain, [sell, buy])
    assert result["status"] == "reconciled", result["causes"]
    assert [(e["tx"], e["chain_log_index"]) for e in result["condition_cash"]] == [
        ("0xff", 2), ("0x01", 5)]
    assert result["tokens"][YES]["min_position"] == 0


@pytest.mark.parametrize("fault", ["wrong_transaction", "missing_transfer", "foreign_event", "malformed_event"])
def test_receipt_must_match_observed_wallet_transfers(tmp_path, fault):
    chain = network(tmp_path)
    chain.batch("0x10", 20, AD, W, [YES, NO], [3 * U, 3 * U])
    chain.ctf_event("0x10", 20, "split", AD, 3 * U)
    receipt = deepcopy(chain.receipts["0x10"])
    if fault == "wrong_transaction":
        receipt["transactionHash"] = "0x99"
    elif fault == "missing_transfer":
        receipt["logs"].pop(0)
    elif fault == "foreign_event":
        receipt["logs"][1]["transactionHash"] = "0x99"
    else:
        receipt["logs"][1]["data"] = "0x"
    chain.receipts["0x10"] = receipt
    result = _run(chain, [])
    assert "unclassified_chain_txs" in result["causes"]
    assert result["tx_kinds"] == {"unclassified": 1}


def test_other_stakeholders_operation_cannot_label_wallet_transfer(tmp_path):
    chain = network(tmp_path)
    chain.batch("0x03", 20, O, W, [YES, NO], [3 * U, 3 * U])
    chain.ctf_event("0x03", 20, "split", O, 3 * U)
    result = _run(chain, [])
    assert result["status"] == "reconciled"
    assert result["tx_kinds"] == {"transfer": 1}
    assert sorted(result["pairing"]["tokens_without_pair"]) == sorted([YES, NO])


def test_one_wallet_cannot_change_caller_pair_metadata(tmp_path):
    chain = network(tmp_path)
    chain.cash("0x10", 20, W, ab.PUSD, 3 * U)
    chain.batch("0x10", 20, AD, W, [YES, NO], [3 * U, 3 * U])
    chain.ctf_event("0x10", 20, "split", AD, 3 * U)
    pairs = {}
    assert _run(chain, [], pairs=pairs)["tokens"][YES]["condition"] == COND
    assert pairs == {}


def test_fill_order_with_wrong_block_fails_even_if_window_balances_agree(tmp_path):
    chain = network(tmp_path)
    row = chain.fill("0x01", 20, "BUY", YES, 10, 0.4)
    row.update(block_number=21, block_timestamp=1042)
    result = _run(chain, [row])
    assert result["reconciliation"][YES]["residual"] == 0
    assert result["fill_check"]["order_filled"]["matched"] == 0
    assert "fill_orders_disagree" in result["causes"]


@pytest.mark.parametrize("fault", ["removed_log", "out_of_range", "wrong_filter", "conflicting_log",
                                   "conflicting_index", "malformed_abi", "not_a_list"])
def test_rpc_logs_must_match_the_query_and_each_other(tmp_path, fault):
    class BrokenLogs(FakeChain):
        def __call__(self, url, method, params):
            rows = super().__call__(url, method, params)
            if method != "eth_getLogs" or not rows:
                return rows
            if fault == "not_a_list":
                return {"bad": True}
            row = deepcopy(rows[0])
            if fault == "removed_log":
                row["removed"] = True
            elif fault == "out_of_range":
                row["blockNumber"] = "0x10000"
            elif fault == "wrong_filter":
                row["topics"][0] = ab.T_ERC20
            elif fault == "malformed_abi":
                row["data"] = row["data"][:-1]
            elif fault == "conflicting_index":
                row["transactionHash"] = "0x02"
                return [*rows, row]
            else:
                row["data"] = row["data"][:-1] + "f"
                return [*rows, row]
            return [row]

    chain = network(tmp_path, BrokenLogs)
    chain.single("0x01", 20, O, W, YES, 2 * U)
    result = _run(chain, [])
    assert result["completeness"]["chain_scan_complete"] is False
    assert "chain_scan_incomplete" in result["causes"]
    assert "chain_scan_incomplete" in _run(chain, [])["causes"]  # also fails on cache replay


@pytest.mark.parametrize("ret", ["0x0", "0x" + "g" * 64])
def test_short_or_invalid_balance_read_never_becomes_zero(tmp_path, ret):
    class ShortBalance(FakeChain):
        def __call__(self, url, method, params):
            if method == "eth_call" and params[0]["to"] == ab.PUSD:
                return ret
            return super().__call__(url, method, params)

    report = _run(network(tmp_path, ShortBalance), [])
    assert "balances_unavailable" in report["causes"]
