"""Absorption ledger on a synthetic Polygon: hand-computable positions, cash and pairing."""

import orjson
import pytest

import doppel.lab.absorb as ab
from doppel.lab.fillstore import compact
from doppel.lab.ordertape import Pool, RpcError
from doppel.tape.segment import SegmentWriter

W = "0x" + "aa" * 20  # the wallet
O = "0x" + "bb" * 20  # its counterparties
EX = "0xe111180000d2663c0091e4f400237545b87b996b"
AD = ab.ADAPTERS[0]
YES, NO = "11", "12"
COND = "cc" * 32
U = ab.U


def _w(*xs):
    return "0x" + "".join(f"{x:064x}" for x in xs)


def _a(addr):
    return int(addr, 16)


class FakeChain:
    """Logs, receipts and balances of a toy chain (block n has timestamp 1000 + 2n)."""

    def __init__(self, head=200):
        self.head, self.logs, self.receipts, self.calls = head, [], {}, []
        self.open: dict[tuple[str, str, str], int] = {}  # (contract, holder, id) -> balance at block 0

    # builders -----------------------------------------------------------
    def _log(self, address, topics, data, tx, block):
        li = sum(1 for lg in self.logs if lg["transactionHash"] == tx)
        lg = {"address": address, "topics": topics, "data": data, "transactionHash": tx,
              "blockNumber": hex(block), "logIndex": hex(li)}
        self.logs.append(lg)
        self.receipts.setdefault(tx, {"transactionHash": tx, "from": "0x" + "ee" * 20, "logs": []})
        self.receipts[tx]["logs"].append(lg)
        return lg

    def single(self, tx, block, frm, to, tok, v):
        self._log(ab.CTF, [ab.T_SINGLE, ab.topic(O), ab.topic(frm), ab.topic(to)], _w(int(tok), v), tx, block)

    def batch(self, tx, block, frm, to, toks, vals):
        n = len(toks)
        data = _w(64, 96 + 32 * n, n, *[int(t) for t in toks], n, *vals)
        self._log(ab.CTF, [ab.T_BATCH, ab.topic(O), ab.topic(frm), ab.topic(to)], data, tx, block)

    def cash(self, tx, block, frm, to, v, token=ab.PUSD):
        self._log(token, [ab.T_ERC20, ab.topic(frm), ab.topic(to)], _w(v), tx, block)

    def ctf_event(self, tx, block, kind, who, amount):
        if kind == "redeem":
            self._log(ab.CTF, [ab.T_REDEEM, ab.topic(who), ab.topic(ab.USDCE), "0x" + "0" * 64],
                      _w(int(COND, 16), 64, amount, 2, 1, 2), tx, block)
        else:
            t = ab.T_SPLIT if kind == "split" else ab.T_MERGE
            self._log(ab.CTF, [t, ab.topic(who), "0x" + "0" * 64, "0x" + COND],
                      _w(_a(ab.USDCE), 96, amount, 2, 1, 2), tx, block)

    def fill(self, tx, block, side, tok, shares, price, fee=0, oid=None):
        """A fill of W against O, as V2 settles it: tokens and collateral move peer to peer."""
        sh, usd = round(shares * U), round(shares * price * U)
        if side == "BUY":
            self.single(tx, block, O, W, tok, sh)
            self.cash(tx, block, W, O, usd)
            if fee:
                self.cash(tx, block, W, EX, fee)
            amounts = (0, usd, sh)
        else:
            self.single(tx, block, W, O, tok, sh)
            self.cash(tx, block, O, W, usd - fee)
            amounts = (1, sh, usd)
        oid = oid or f"{abs(hash((tx, tok))):064x}"[-64:]
        self._log(EX, [ab.T_FILLED, "0x" + oid, ab.topic(W), ab.topic(O)],
                  _w(amounts[0], int(tok), amounts[1], amounts[2], fee, 0, 0), tx, block)
        return {"id": f"{tx}-{tok}", "address": W, "event_type": "TRADE", "token_id": tok,
                "side": side, "amount_shares": shares, "amount_usdc": usd / U, "fee": fee / U,
                "block_timestamp": 1000 + 2 * block, "block_number": block, "order_type": "maker",
                "price": price, "order_id": "0x" + oid, "counterparty_address": O,
                "transaction_hash": tx}

    # balances -----------------------------------------------------------
    def balance(self, contract, holder, tok, block):
        b = self.open.get((contract, holder, tok), 0)
        for lg in self.logs:
            if int(lg["blockNumber"], 16) > block or lg["address"] != contract:
                continue
            for leg in ab.legs_of(lg, holder):
                if leg["asset"] == (tok if contract == ab.CTF else "cash"):
                    b += leg["delta"]
        return b

    # JSON-RPC -----------------------------------------------------------
    def __call__(self, url, method, params):
        self.calls.append(method)
        if method == "eth_blockNumber":
            return hex(self.head)
        if method == "eth_getBlockByNumber":
            return {"number": params[0], "timestamp": hex(1000 + 2 * int(params[0], 16))}
        if method == "eth_getTransactionReceipt":
            return self.receipts.get(params[0])
        if method == "eth_getCode":
            return "0x"
        if method == "eth_getLogs":
            f = params[0]
            lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
            addrs = f["address"] if isinstance(f["address"], list) else [f["address"]]
            out = []
            for lg in self.logs:
                if not lo <= int(lg["blockNumber"], 16) <= hi or lg["address"] not in addrs:
                    continue
                if all(t is None or (lg["topics"][i] in t if isinstance(t, list) else lg["topics"][i] == t)
                       for i, t in enumerate(f["topics"])):
                    out.append(lg)
            return out
        if method == "eth_call":
            to, data, block = params[0]["to"], params[0]["data"][2:], int(params[1], 16)
            w = [int(data[8 + i:8 + i + 64], 16) for i in range(0, len(data) - 8, 64)]
            if data.startswith(ab.SEL_BALANCE_OF_BATCH):
                n = w[2]
                holders, toks = w[3:3 + n], w[4 + n:4 + 2 * n]
                vals = [self.balance(ab.CTF, f"0x{h:040x}", str(t), block) for h, t in zip(holders, toks)]
                return _w(32, n, *vals)
            if data.startswith(ab.SEL_BALANCE_OF):
                return _w(self.balance(to, f"0x{w[0]:040x}", "cash", block))
        raise RpcError("unsupported")


def _pages(wallet, start, end, fills):
    return {(wallet, start, end): {"address": wallet, "start_time": start, "end_time": end,
                                   "complete": True, "cursor": None, "stop": "done", "pages": 1,
                                   "rows": len(fills), "trade_binding": ab.trade_binding(fills)}}


def _run(chain, fills, window=(10, 100), pages=None, pairs=None, max_requests=None, out=None):
    pool = Pool(["fake"], chain, rate=1e9, max_requests=max_requests, sleep=lambda s: None,
                backoff_s=0, bench_s=0)
    c = ab.Chain(pool, out or chain.cache)
    pages = _pages(W, 0, 10**6, fills) if pages is None else pages
    times = (1000 + 2 * window[0], 1000 + 2 * window[1] + 1)
    return ab.absorb_wallet(c, W, fills, window, times, 25, pages, {} if pairs is None else pairs)


@pytest.fixture
def chain(tmp_path):
    c = FakeChain()
    c.cache = tmp_path / "cache.jsonl"
    c.open[(ab.CTF, W, "cash")] = 0
    c.open[(ab.PUSD, W, "cash")] = 100 * U  # $100 of pUSD before the window
    return c


def _tok(r, t):
    return r["tokens"][t]


def test_complete_set_buy_pair_is_completing_not_directional(chain):
    fills = [chain.fill("0x01", 20, "BUY", NO, 10, 0.48), chain.fill("0x02", 30, "BUY", YES, 10, 0.49)]
    r = _run(chain, fills, pairs={YES: (COND, 0), NO: (COND, 1)})
    assert r["status"] == "reconciled" and r["causes"] == []
    assert _tok(r, YES)["close"] == 10 and _tok(r, NO)["close"] == 10
    assert r["cash"]["by_kind"] == {"buy": -9.7} and r["reconciliation"]["cash"]["residual"] == 0
    m = r["pairing"]["markets"][COND]
    assert (m["directional"], m["completing"]) == (10, 10)  # the YES buy closed the set
    assert m["one_sided_at_end"] == 0 and m["longest_one_sided_s"] == 20  # blocks 20 -> 30, 2 s each
    assert r["pairing"]["completing_share_of_buys"] == 0.5
    assert r["fill_check"]["order_filled"] == {"chain": 2, "matched": 2, "amount_mismatch": 0, "chain_only": 0, "goldsky_only": 0}


def test_split_then_sell_both_learns_the_pair_from_the_adapter_batch(chain):
    chain.cash("0x10", 20, W, ab.PUSD, 10 * U)  # pUSD into the collateral token, unwrapped
    chain.batch("0x10", 20, AD, W, [YES, NO], [10 * U, 10 * U])
    chain.ctf_event("0x10", 20, "split", AD, 10 * U)  # the adapter is the stakeholder
    fills = [chain.fill("0x11", 30, "SELL", YES, 10, 0.60, fee=50_000),
             chain.fill("0x12", 40, "SELL", NO, 10, 0.41)]
    r = _run(chain, fills)
    assert r["status"] == "reconciled", r["causes"]
    assert r["tx_kinds"] == {"split": 1}
    assert _tok(r, YES)["by_kind"] == {"split": 10, "sell": -10} and _tok(r, NO)["close"] == 0
    # -10 split + 6.00 - 0.05 fee + 4.10
    assert r["cash"]["by_kind"] == {"split": -10, "sell": 10.05} and r["cash"]["fees"] == 0.05
    m = r["pairing"]["markets"][COND]
    assert (m["unpair"], m["derisk"]) == (10, 10) and m["longest_one_sided_s"] == 20
    assert _tok(r, YES)["condition"] == COND and _tok(r, NO)["idx"] == 1
    assert r["identity"]["tx_senders"] == {"0x" + "ee" * 20: 1}


def test_direct_merge_of_opening_inventory(chain):
    chain.open[(ab.CTF, W, YES)] = chain.open[(ab.CTF, W, NO)] = 5 * U
    chain.batch("0x20", 50, W, ab.ZERO, [YES, NO], [5 * U, 5 * U])
    chain.cash("0x20", 50, ab.CTF, W, 5 * U, token=ab.USDCE)
    chain.ctf_event("0x20", 50, "merge", W, 5 * U)
    r = _run(chain, [])
    assert r["status"] == "reconciled", r["causes"]
    assert r["tx_kinds"] == {"merge": 1} and r["cash"]["by_kind"] == {"merge": 5}
    assert _tok(r, YES)["open"] == 5 and _tok(r, YES)["close"] == 0
    assert r["pairing"]["markets"][COND]["paired_share_s"] == 5 * 80  # 5 shares, blocks 10 -> 50


def test_redemption_after_resolution(chain):
    chain.open[(ab.CTF, W, YES)] = 7 * U
    chain.batch("0x30", 60, W, AD, [YES, NO], [7 * U, 0])
    chain.ctf_event("0x30", 60, "redeem", AD, 7 * U)
    chain.cash("0x30", 60, ab.ZERO, W, 7 * U)  # PMCT wrapped to the wallet
    r = _run(chain, [])
    assert r["status"] == "reconciled", r["causes"]
    assert r["tx_kinds"] == {"redeem": 1} and r["cash"]["by_kind"] == {"redeem": 7}
    assert _tok(r, YES)["by_kind"] == {"redeem": -7} and _tok(r, NO)["close"] == 0
    m = r["pairing"]["markets"][COND]
    assert m["one_sided_share_s"] == 7 * 100 and m["longest_one_sided_s"] == 100


def test_transfer_in_has_no_cash_and_reconciles(chain):
    chain.single("0x40", 15, O, W, YES, 3 * U)
    r = _run(chain, [])
    assert r["status"] == "reconciled" and r["tx_kinds"] == {"transfer": 1}
    assert _tok(r, YES)["by_kind"] == {"transfer": 3} and r["cash"]["by_kind"] == {"transfer": 0}
    assert r["pairing"]["tokens_without_pair"] == [YES]


def test_missing_goldsky_fill_is_an_unexplained_residual(chain):
    kept = chain.fill("0x50", 20, "BUY", YES, 10, 0.5)
    chain.fill("0x51", 30, "BUY", YES, 4, 0.5)  # on chain, absent from Goldsky
    r = _run(chain, [kept])
    assert r["status"] == "not_reconciled" and r["not_reconciled"]
    assert {"goldsky_missing_fill_txs", "share_residual", "cash_residual"} <= set(r["causes"])
    assert r["reconciliation"][YES]["residual"] == 4 and r["reconciliation"][YES]["log_residual"] == 0
    assert r["reconciliation"]["cash"]["residual"] == -2
    assert r["fill_check"]["examples_missing"] == ["0x51"]
    assert ab.cohort_row(r)["tokens_with_share_residual"] == 1


def test_fee_convention_mismatch_is_named(chain):
    f = chain.fill("0x60", 20, "BUY", YES, 10, 0.5, fee=30_000)
    f["fee"] = 0.0  # Goldsky without the fee
    r = _run(chain, [f])
    assert set(r["causes"]) == {"fill_transaction_legs_disagree", "fill_orders_disagree", "cash_residual_equals_fees"}
    assert r["reconciliation"]["cash"]["residual"] == -0.03
    assert r["fill_check"]["order_filled"]["amount_mismatch"] == 1


def test_scan_budget_stop_then_resume_without_duplicates(chain, tmp_path):
    fills = [chain.fill("0x01", 20, "BUY", NO, 10, 0.48), chain.fill("0x02", 80, "BUY", YES, 10, 0.49)]
    part = _run(chain, fills, max_requests=6)  # one chunk of five queries, then the budget ends
    assert part["status"] == "not_reconciled" and part["causes"][0] == "chain_scan_incomplete"
    assert part["completeness"]["chain_next_block"] == 35
    n = len(chain.calls)
    full = _run(chain, fills)
    assert full["status"] == "reconciled" and full["chain_legs"] == 4
    # 4 chunks x 5 queries; the stopped run got 6 answers, so the resumed run asks for 14
    assert chain.calls[n:].count("eth_getLogs") == 14
    one_shot = _run(chain, fills, out=tmp_path / "fresh.jsonl")
    assert {k: v for k, v in full.items() if k != "completeness"} == \
        {k: v for k, v in one_shot.items() if k != "completeness"}


def test_goldsky_incomplete_blocks_reconciliation(chain):
    fills = [chain.fill("0x01", 20, "BUY", NO, 10, 0.48)]
    r = _run(chain, fills, pages={(W, 0, 10**6): {"complete": False}})
    assert r["causes"] == ["goldsky_incomplete"] and r["status"] == "not_reconciled"
    assert ab.goldsky_complete(_pages(W, 0, 50, []) | _pages(W, 50, 99, []),
                               W, 10, 90)
    assert not ab.goldsky_complete(_pages(W, 0, 50, []) | _pages(W, 60, 99, []),
                                   W, 10, 90)


def test_block_at_finds_first_block_at_or_after(chain, tmp_path):
    c = ab.Chain(Pool(["fake"], chain, rate=1e9, sleep=lambda s: None), tmp_path / "c.jsonl")
    assert c.block_at(1000 + 2 * 137, 200) == 137
    assert c.block_at(1000 + 2 * 137 - 1, 200) == 137
    assert c.block_at(1000 + 2 * 137 + 1, 200) == 138


def test_cli_end_to_end_with_fill_store(chain, tmp_path):
    rows = [chain.fill("0x01", 20, "BUY", NO, 10, 0.48), chain.fill("0x02", 30, "BUY", YES, 10, 0.49)]
    w = SegmentWriter(tmp_path / "tape", "goldsky_activity")
    w.append(orjson.dumps({"data": rows}).decode(), meta={"job": "cohort-x", "status": 200})
    w.close()
    compact(tmp_path / "tape", tmp_path / "store")
    pages = tmp_path / "pagination.jsonl"
    pages.write_bytes(orjson.dumps(_pages(W, 0, 5000, rows)[W, 0, 5000]) + b"\n")
    tokens = tmp_path / "tokens.jsonl"
    tokens.write_bytes(b"".join(orjson.dumps({"token": t, "condition": "0x" + COND, "idx": i,
                                              "status": "open"}) + b"\n"
                                for i, t in enumerate((YES, NO))))
    out = tmp_path / "out"
    routed = []
    def transport(url, method, params):
        expected = "logs" if method == "eth_getLogs" else "state" if method in ("eth_call", "eth_getCode") else "general"
        assert url == expected
        routed.append(expected)
        return chain(url, method, params)

    assert ab.main(["--store", str(tmp_path / "store"), "--wallets", W, "--from", str(1000 + 2 * 10),
                    "--to", str(1000 + 2 * 101), "--out", str(out), "--rpc", "general",
                    "--log-rpc", "logs", "--state-rpc", "state",
                    "--pages", str(pages), "--tokens", str(tokens), "--rate", "1e9"],
                   transport=transport) == 0
    assert set(routed) == {"logs", "state", "general"}
    doc = orjson.loads((out / f"wallet-{W}.json").read_bytes())
    assert doc["window_blocks"] == [10, 100] and doc["status"] == "reconciled"
    assert doc["pairing"]["completing_share_of_buys"] == 0.5
    table = (out / "cohort.tsv").read_text().splitlines()
    assert table[0].startswith("wallet\tstatus") and table[1].split("\t")[:2] == [W, "reconciled"]

def test_compensating_bad_fills_block_reconciliation(chain):
    first = chain.fill("0x01", 20, "BUY", YES, 10, 0.4)
    second = chain.fill("0x02", 21, "BUY", YES, 10, 0.6)
    first["amount_usdc"], second["amount_usdc"] = second["amount_usdc"], first["amount_usdc"]
    result = _run(chain, [first, second])
    assert result["reconciliation"]["cash"]["residual"] == 0
    assert result["status"] == "not_reconciled"
    assert {"fill_transaction_legs_disagree", "fill_orders_disagree"} <= set(result["causes"])
    assert result["fill_check"]["order_filled"]["amount_mismatch"] == 2


def test_opening_complement_reconstructed_without_in_range_transfer(chain):
    chain.open[(ab.CTF, W, NO)] = 10 * U
    result = _run(chain, [chain.fill("0x01", 20, "BUY", YES, 10, 0.4)],
                  pairs={YES: (COND, 0), NO: (COND, 1)})
    assert result["status"] == "reconciled"
    assert result["tokens"][NO]["open"] == result["tokens"][NO]["close"] == 10
    assert result["pairing"]["completing_share_of_buys"] == 1


def test_one_sided_order_evidence_and_duplicate_order_hashes(chain):
    first = chain.fill("0x01", 20, "BUY", YES, 5, 0.4, oid="1" * 64)
    second = chain.fill("0x01", 20, "BUY", YES, 10, 0.4, oid="1" * 64)
    assert _run(chain, [first, second])["status"] == "reconciled"
    second["order_id"] = "0x" + "2" * 64
    result = _run(chain, [first, second])
    assert result["causes"] == ["fill_orders_disagree"]
    assert result["fill_check"]["order_filled"]["chain_only"] == 1
    assert result["fill_check"]["order_filled"]["goldsky_only"] == 1

def test_rpc_roles_share_budget_and_never_fall_back_to_incompatible_node(tmp_path):
    calls = []
    def transport(url, method, params):
        calls.append((url, method))
        if method == "eth_call":
            raise RpcError("unsupported_state")
        return []

    pool = Pool(["logs", "state"], transport, rate=1e9, max_requests=3, attempts=2,
                sleep=lambda _: None, backoff_s=0)
    chain = ab.Chain(pool, tmp_path / "cache", {"eth_getLogs": frozenset(["logs"]), "eth_call": frozenset(["state"])})
    chain.get("eth_getLogs", [])
    with pytest.raises(RpcError):
        chain.get("eth_call", [])
    with pytest.raises(ab.BudgetExhausted):
        chain.get("eth_getLogs", [{}])
    assert calls == [("logs", "eth_getLogs"), ("state", "eth_call"), ("state", "eth_call")]
