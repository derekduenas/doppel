"""Absorption of the Polymarket V2 system (PositionManager + Exchange) from real Polygon logs.

``fixtures_absorb/pm-v2-polygon.json`` holds, unmodified, the receipt of fill tx 0x0189c270...0c13
(Exchange 0xe3333700...; a MINT match: 0x433f buys YES, 0x0dbd buys NO, 6 shares each), the
receipt of a Router redemption on the BinaryModule, and one real ``PositionsMerged`` and one real
PositionManager ``TransferBatch`` log. The Goldsky rows are synthesized from the chain's own
``OrderFilled`` amounts (no Goldsky call): the test proves the chain reading, not that real
Goldsky rows carry the same order hash and fee.
"""

from pathlib import Path

import orjson
import pytest

import doppel.lab.absorb as ab
from doppel.lab.ordertape import Pool, RpcError

FIX = orjson.loads((Path(__file__).parent / "fixtures_absorb" / "pm-v2-polygon.json").read_bytes())
FILL = FIX["fill_receipt"]
REDEEM = FIX["redeem_receipt"]
TX = "0x0189c27086445efa3e166977a350aecb00c3c49bc0d75969cb243652bf300c13"
TAKER = "0x0dbd0e1d5999a401263dc26cbf1597f2be0cfcd5"  # bought NO (..01) and paid the fee
MAKER = "0x433f62d1f91768a0877171eee70e172f0bf7a0dc"  # bought YES (..00)
REDEEMER = "0x3c16402bdd413903f5875a885a534c108de1b2cf"
V2_YES = str(0x019e43f758b85f9a3498c1574d2133e204000000000000000000000000000000)
V2_NO = str(int(V2_YES) + 1)
CLASSIC = "424242"  # a classic CTF token of a synthetic V2-exchange (CTFExchangeV2) fill
CEX = "0xe111180000d2663c0091e4f400237545b87b996b"
OTHER = "0x" + "bb" * 20
U = ab.U


def _w(*xs):
    return "0x" + "".join(f"{x:064x}" for x in xs)


class Net:
    """JSON-RPC over a fixed log set: block n has timestamp ts0 + 2 (n - b)."""

    def __init__(self, b, ts0, logs, receipts, block_hash=None):
        self.b, self.ts0, self.hash = b, ts0, block_hash
        self.logs, self.receipts = list(logs), dict(receipts)
        self.open: dict[tuple[str, str, str], int] = {}
        self.calls: list[tuple[str, dict | list]] = []

    def ts(self, n):
        return self.ts0 + 2 * (n - self.b)

    def balance(self, contract, holder, asset, block):
        v = self.open.get((contract, holder, asset), 0)
        for lg in self.logs:
            if int(lg["blockNumber"], 16) <= block and lg["address"] == contract:
                v += sum(g["delta"] for g in ab.legs_of(lg, holder) if g["asset"] in (asset, "cash"))
        return v

    def __call__(self, url, method, params):
        self.calls.append((method, params))
        if method == "eth_getBlockByNumber":
            n = int(params[0], 16)
            return {"number": params[0], "timestamp": hex(self.ts(n))} | (
                {"hash": self.hash} if n == self.b and self.hash else {})
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_getCode":
            return "0x"
        if method == "eth_getLogs":
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            addrs = f["address"] if isinstance(f["address"], list) else [f["address"]]
            return [lg for lg in self.logs if lo <= int(lg["blockNumber"], 16) <= hi and lg["address"] in addrs
                    and all(t is None or lg["topics"][i] in (t if isinstance(t, list) else [t])
                            for i, t in enumerate(f["topics"]))]
        if method == "eth_call":
            to, data, block = params[0]["to"], params[0]["data"][2:], int(params[1], 16)
            w = [int(data[8 + i:8 + i + 64], 16) for i in range(0, len(data) - 8, 64)]
            if data.startswith(ab.SEL_BALANCE_OF_BATCH):
                n = w[2]
                holders, toks = w[3:3 + n], w[4 + n:4 + 2 * n]
                return _w(32, n, *[self.balance(to, f"0x{h:040x}", str(t), block) for h, t in zip(holders, toks)])
            if data.startswith(ab.SEL_BALANCE_OF):
                return _w(self.balance(to, f"0x{w[0]:040x}", "cash", block))
        raise RpcError("unsupported")


def _row(wallet, of, ts):
    """A Goldsky row with the amounts of a chain OrderFilled (BUY only, as in the fixture)."""
    o = ab.order_filled(of)
    assert o["maker"] == wallet and o["side"] == "BUY"
    return {"id": f"{o['tx']}-{o['li']}", "address": wallet, "event_type": "TRADE", "token_id": o["token"],
            "side": "BUY", "amount_shares": o["shares"] / U, "amount_usdc": o["usdc"] / U, "fee": o["fee"] / U,
            "block_timestamp": ts, "block_number": o["block"], "order_type": "maker",
            "price": o["usdc"] / o["shares"], "order_id": "0x" + o["order_id"], "transaction_hash": o["tx"]}


def _filled(receipt, maker):
    return next(lg for lg in receipt["logs"] if lg["topics"][0] == ab.T_FILLED
                and ab._addr(lg["topics"][2]) == maker)


def _classic_fill(net, wallet, block):
    """A classic CTFExchangeV2 P2P buy of 4 shares at 0.5 by ``wallet``, on ConditionalTokens."""
    tx = "0x" + "c1" * 32
    def log(address, topics, data):
        li = 1000 + sum(lg["transactionHash"] == tx for lg in net.logs)
        lg = {"address": address, "topics": topics, "data": data, "transactionHash": tx,
              "blockNumber": hex(block), "logIndex": hex(li), "removed": False}
        net.logs.append(lg)
        return lg
    log(ab.CTF, [ab.T_SINGLE, ab.topic(CEX), ab.topic(OTHER), ab.topic(wallet)], _w(int(CLASSIC), 4 * U))
    log(ab.PUSD, [ab.T_ERC20, ab.topic(wallet), ab.topic(OTHER)], _w(2 * U))
    of = log(CEX, [ab.T_FILLED, "0x" + "0d" * 32, ab.topic(wallet), ab.topic(OTHER)],
             _w(0, int(CLASSIC), 2 * U, 4 * U, 0, 0, 0))
    return _row(wallet, of, net.ts(block))


def _absorb(net, wallet, fills, tmp_path, half=50):
    pool = Pool(["fake"], net, rate=1e9, sleep=lambda s: None, backoff_s=0, bench_s=0)
    chain = ab.Chain(pool, tmp_path / f"cache-{wallet}.jsonl")
    window = (net.b - half, net.b + half)
    times = (net.ts(window[0]), net.ts(window[1]) + 1)
    pages = {(wallet, 0, 2**40): {"address": wallet, "start_time": 0, "end_time": 2**40, "complete": True,
                                   "cursor": None, "stop": "done", "pages": 1, "rows": len(fills),
                                   "trade_binding": ab.trade_binding(fills)}}
    return ab.absorb_wallet(chain, wallet, fills, window, times, 25, pages, {})


@pytest.fixture
def net():
    b, ts0 = int(FILL["blockNumber"], 16), int(FIX["fill_block"]["timestamp"], 16)
    n = Net(b, ts0, FILL["logs"], {TX: FILL}, FIX["fill_block"]["hash"])
    for w in (TAKER, MAKER):
        n.open[(ab.PUSD, w, "cash")] = 10 * U
    return n


def test_v2_ids_and_events_decode_from_real_logs():
    assert int(V2_YES) >> 248 == ab.PM_BINARY_MODULE_ID
    assert ab.pm_pairs([V2_NO]) == {V2_YES: (f"{int(V2_YES) >> 8:062x}", 0), V2_NO: (f"{int(V2_YES) >> 8:062x}", 1)}
    split, = ab.pm_events(FILL)  # the Exchange's MINT match splits on the BinaryModule
    assert split["kind"] == "split" and split["recipients"] == [ab.PM_EXCHANGE] * 2
    assert split["condition"] == f"{int(V2_YES) >> 8:062x}" and split["amount"] == 6 * U
    merge, = ab.pm_events({"logs": [FIX["merge_log"]]})
    assert merge["kind"] == "merge" and merge["recipients"] == [ab.PM_EXCHANGE] and merge["amount"] == 0x21B8D0
    redeem, = ab.pm_events(REDEEM)
    assert (redeem["kind"], redeem["recipients"], redeem["amount"]) == ("redeem", [REDEEMER], 0x29810)
    module = ab._addr(FIX["batch_log"]["topics"][2])  # a real PositionManager batch burn
    legs = ab.legs_of(FIX["batch_log"], module)
    assert len(legs) == 2 and all(g["delta"] == -0x1F6AE860 and g["contract"] == ab.PM for g in legs)


def test_classic_only_reader_reproduces_the_cohort_failure(net, tmp_path, monkeypatch):
    """The three causes seen on Apex, from the real receipt, when the V2 contracts are not read."""
    monkeypatch.setattr(ab, "PM", "0x" + "01" * 20)
    monkeypatch.setattr(ab, "PM_EXCHANGE", "0x" + "02" * 20)
    r = _absorb(net, TAKER, [_row(TAKER, _filled(FILL, TAKER), net.ts0)], tmp_path)
    assert {"fill_transaction_legs_disagree", "fill_orders_disagree", "share_residual"} <= set(r["causes"])
    assert r["fill_check"]["order_filled"]["goldsky_only"] == 1
    assert r["fill_check"]["chain_fill_txs_missing_in_goldsky"] == 0


@pytest.mark.parametrize("wallet,token,cash", [(TAKER, V2_NO, -2_834_940), (MAKER, V2_YES, -3_180_000)])
def test_v2_fill_reconciles(net, tmp_path, wallet, token, cash):
    r = _absorb(net, wallet, [_row(wallet, _filled(FILL, wallet), net.ts0)], tmp_path)
    assert r["status"] == "reconciled", r["causes"]
    assert r["fill_check"]["order_filled"]["matched"] == 1 and r["fill_check"]["tx_asset_mismatches"] == 0
    assert r["reconciliation"][token]["observed"] == 6 and r["reconciliation"]["cash"]["observed"] == cash / U
    assert r["pairing"]["coverage"] == "complete"  # the sibling is read from the id, balance 0
    assert set(r["tokens"]) == {V2_YES, V2_NO} and r["tokens"][token]["idx"] == int(token) & 1


def test_mixed_classic_and_v2_wallet_reconciles(net, tmp_path):
    classic = _classic_fill(net, TAKER, net.b + 10)
    r = _absorb(net, TAKER, [_row(TAKER, _filled(FILL, TAKER), net.ts0), classic], tmp_path)
    assert r["status"] == "reconciled", r["causes"]
    assert r["fill_check"]["order_filled"]["matched"] == 2
    assert r["reconciliation"][CLASSIC]["observed"] == 4 and r["reconciliation"][V2_NO]["observed"] == 6
    assert r["reconciliation"]["cash"]["observed"] == -4.83494


def test_pure_classic_wallet_reads_no_v2_balances(net, tmp_path):
    w = "0x" + "aa" * 20
    net.open[(ab.PUSD, w, "cash")] = 10 * U
    r = _absorb(net, w, [_classic_fill(net, w, net.b + 10)], tmp_path)
    assert r["status"] == "reconciled", r["causes"]
    assert set(r["tokens"]) == {CLASSIC}
    assert not [p for m, p in net.calls if m == "eth_call" and p[0]["to"] == ab.PM]


def test_residuals_still_fail_in_either_system(net, tmp_path):
    _classic_fill(net, TAKER, net.b + 10)  # on chain, absent from Goldsky
    r = _absorb(net, TAKER, [_row(TAKER, _filled(FILL, TAKER), net.ts0)], tmp_path)
    assert r["status"] == "not_reconciled"
    assert {"goldsky_missing_fill_txs", "share_residual", "cash_residual"} <= set(r["causes"])
    assert r["reconciliation"][CLASSIC]["residual"] == 4 and r["reconciliation"][V2_NO]["residual"] == 0
    r = _absorb(net, MAKER, [], tmp_path)  # the V2 fill itself missing from Goldsky
    assert {"goldsky_missing_fill_txs", "share_residual"} <= set(r["causes"])
    assert r["reconciliation"][V2_YES]["residual"] == 6


def test_v2_router_redemption_is_classified(tmp_path):
    b = int(REDEEM["blockNumber"], 16)
    n = Net(b, int(REDEEM["logs"][0]["blockTimestamp"], 16), REDEEM["logs"], {REDEEM["transactionHash"]: REDEEM})
    pid = str(int(REDEEM["logs"][0]["data"][2:66], 16))
    n.open[(ab.PM, REDEEMER, pid)] = 0x29810
    r = _absorb(n, REDEEMER, [], tmp_path)
    assert r["status"] == "reconciled", r["causes"]
    assert r["tx_kinds"] == {"redeem": 1} and r["cash"]["by_kind"] == {"redeem": 0x29810 / U}
    assert r["tokens"][pid]["close"] == 0
