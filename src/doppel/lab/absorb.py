"""Absorption ledger: a wallet's history made provably complete and economically correct.

For each wallet and block window ``[b0, b1]`` it joins three independent sources:

1. Goldsky fills (the fill store), with the pagination record of the pull that produced them
   (``goldsky_activity/pagination.jsonl``). Goldsky counts as complete only when a record for that
   wallet with ``complete`` true (source returned no ``next_cursor``) covers the window.
2. Chain events from free public Polygon JSON-RPC ``eth_getLogs`` in chunks of ``chunk`` blocks:
   ERC-1155 ``TransferSingle``/``TransferBatch`` on the ConditionalTokens contract from or to the
   wallet (every share movement, mints and burns included), ERC-20 ``Transfer`` of the two
   collateral tokens from or to the wallet (every cash movement), and exchange V2 ``OrderFilled``
   with the wallet as order maker (fill cross-check). Each non-fill transaction that moves shares
   is classified from its receipt (``PositionSplit`` / ``PositionsMerge`` / ``PayoutRedemption``
   of the ConditionalTokens contract), which also pairs the two outcome tokens of a condition.
3. End balances by ``eth_call`` at ``b0 - 1`` and ``b1``: ``balanceOfBatch`` for every token the
   wallet touched and ``balanceOf`` of both collateral tokens.

Addresses (Polygon, chainId 137), and where each was established:
- ConditionalTokens ``0x4D97DCd97eC945f40cF65F87097ACe5EA0476045``: returned on chain by
  ``CtfCollateralAdapter.CONDITIONAL_TOKENS()`` (2026-11-26).
- USDC.e ``0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174``: ``CtfCollateralAdapter.USDCE()`` on chain.
- Polymarket collateral token "PMCT"/pUSD ``0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB`` (6 decimals):
  Polymarket/ctf-exchange-v2 README deployment table; on chain it is
  ``CTFExchangeV2.getCollateral()`` and ``CtfCollateralAdapter.COLLATERAL_TOKEN()``. V2 trades in it.
- CTFExchangeV2 ``0xE111180000d2663C0091e4f400237545B87B996B``, NegRiskCtfExchangeV2
  ``0xe2222d279d744050d28e00520010520000310F59``, CtfCollateralAdapter
  ``0xADa100874d00e3331D00F2007a9c336a65009718``, NegRiskCtfCollateralAdapter
  ``0xAdA200001000ef00D07553cEE7006808F895c6F1``: the same README table.
Event signatures: ``OrderFilled(bytes32,address,address,uint8,uint256,uint256,uint256,uint256,
bytes32,bytes32)`` with topics (orderHash, maker, taker), from ctf-exchange-v2
``src/exchange/mixins/Events.sol``; ``PositionSplit``/``PositionsMerge(address indexed stakeholder,
address collateralToken, bytes32 indexed parentCollectionId, bytes32 indexed conditionId, uint[]
partition, uint amount)`` and ``PayoutRedemption(address indexed redeemer, address indexed
collateralToken, bytes32 indexed parentCollectionId, bytes32 conditionId, uint[] indexSets, uint
payout)`` from gnosis/conditional-tokens-contracts ``contracts/ConditionalTokens.sol``; ERC-1155
and ERC-20 transfer events are the standards. V1 exchanges and the V1 NegRiskAdapter are not
covered: their fills appear as unclassified share movements, never as fills.

Why receipts and not stakeholder filters: V2 splits, merges and redemptions go through the
collateral adapter (``CtfCollateralAdapter.sol``), so the CTF events name the adapter, and the
wallet only sees a batch transfer from or to it. Exchange MINT/MERGE matches split inside a fill
transaction with the exchange as stakeholder; those are fills, never the wallet's splits.

Definitions (all amounts are integer micro-units: 1 share = 1 USDC = 10**6):
- fill transaction: a tx hash with a Goldsky fill of the wallet or an ``OrderFilled`` naming it.
  In it Goldsky rows are the reconstruction (BUY: +shares, cash -(usdc + fee); SELL: -shares,
  cash +(usdc - fee), the V2 ``Trading.sol`` convention); the chain legs are the check.
- other transactions are reconstructed from chain legs: ``split`` / ``merge`` / ``redeem`` (receipt
  event), ``transfer`` (shares moved with no CTF event), ``cash`` (collateral only: deposits,
  withdrawals, wraps), ``unclassified`` (receipt not fetched).
- residual, per token and for cash (pUSD + USDC.e): ``(end - start balance) - reconstructed``.
  Each collateral contract must independently balance against its own transfers. In a fill tx
  the gross share movements and the maximum collateral outflow (including fees) are also checked.
- ``reconciled``: Goldsky complete, chain scan complete, balances read, every residual within one
  micro-unit per Goldsky row, every receipt classified. Otherwise ``not_reconciled`` with causes;
  such a wallet must be excluded from profit rankings.
- pairing (per condition, from opening balances on): imbalance ``I = pos(idx0) - pos(idx1)``.
  A fill moving ``I`` by ``d`` is ``completing`` for ``min(|d|, |I|)`` shares when it moves
  ``I`` toward zero, else ``directional``; sells split the same way into ``derisk``/``unpair``.
  Time-weighted: paired share-seconds ``min(pos)``, one-sided share-seconds ``|I|``, and the
  longest spell with ``|I|`` of at least one share. Within a block, matched fills use their
  exchange log index and other transactions use the first wallet transfer log index. Unmatched
  fills have an unknown index and fail reconciliation.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import orjson

from ..tape.segment import atomic_write
from .fillstore import iter_wallet_fills
from .goldsky import pagination_state, terminal_page, trade_binding
from .ordertape import (DEFAULT_RPCS, EXCHANGES, BudgetExhausted, EndpointsDown, Pool, RpcError,
                        http_transport)

CTF = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"
USDCE = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
PUSD = "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb"
CASH = (PUSD, USDCE)
ADAPTERS = ("0xada100874d00e3331d00f2007a9c336a65009718",
            "0xada200001000ef00d07553cee7006808f895c6f1")
ZERO = "0x" + "0" * 40
T_SINGLE = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
T_BATCH = "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb"
T_ERC20 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
T_FILLED = "0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee"
T_SPLIT = "0x2e6bb91f8cbcda0c93623c54d0403a43514fabc40084ec96b6d5379a74786298"
T_MERGE = "0x6f13ca62553fcc2bcd2372180a43949c1e4cebba603901ede2f4e14f36b282ca"
T_REDEEM = "0x2682012a4a4f1973119f1c9b90745d1bd91fa2bab387344f044cb3586864d18d"
SEL_BALANCE_OF = "70a08231"
SEL_BALANCE_OF_BATCH = "4e1273f4"
SEL_GET_OWNERS = "a0e67e2b"  # Gnosis Safe getOwners()
U = 1_000_000
SPELL_MIN = U  # a one-sided spell needs at least one share of imbalance
DEFAULT_CHUNK = 200
DEFAULT_LOG_RPC, DEFAULT_STATE_RPC = DEFAULT_RPCS


# ---------------------------------------------------------------- ABI helpers
def topic(addr: str) -> str:
    return "0x" + addr[2:].lower().rjust(64, "0")


def _addr(word: str) -> str:
    return "0x" + word[-40:].lower()


def _words(data: str) -> list[int]:
    h = data[2:]
    if not data.startswith("0x") or len(h) % 64 or not re.fullmatch(r"[0-9a-fA-F]*", h):
        raise ValueError("invalid ABI words")
    return [int(h[i:i + 64], 16) for i in range(0, len(h), 64)]


def _array(w: list[int], byte_off: int) -> list[int]:
    i = byte_off // 32
    if byte_off % 32 or i >= len(w) or w[i] > len(w) - i - 1:
        raise ValueError("invalid ABI array")
    return w[i + 1:i + 1 + w[i]]


def _enc(*words: int) -> str:
    return "".join(f"{x:064x}" for x in words)


def legs_of(log: dict, wallet: str) -> list[dict]:
    """The wallet's signed share or cash movements in one transfer log."""
    t, addr = [word.lower() for word in log["topics"]], log["address"].lower()
    base = {"tx": log["transactionHash"].lower(), "block": int(log["blockNumber"], 16),
            "li": int(log["logIndex"], 16)}
    if addr == CTF and t[0] in (T_SINGLE, T_BATCH):
        frm, to = _addr(t[2]), _addr(t[3])
        if wallet not in (frm, to):
            return []
        w = _words(log["data"])
        if t[0] == T_SINGLE and len(w) != 2:
            raise ValueError("invalid TransferSingle data")
        ids, vals = ([w[0]], [w[1]]) if t[0] == T_SINGLE else (_array(w, w[0]), _array(w, w[1]))
        if t[0] == T_BATCH and (len(ids) != len(vals) or w[0] != 64 or w[1] != 96 + 32 * len(ids)
                                or len(w) != 4 + 2 * len(ids)):
            raise ValueError("inconsistent ERC1155 batch")
        batch = [str(i) for i in ids] if t[0] == T_BATCH else None
        out = []
        for i, v in zip(ids, vals):
            d = (v if to == wallet else 0) - (v if frm == wallet else 0)
            out.append(base | {"asset": str(i), "delta": d, "cp": frm if to == wallet else to,
                               "batch": batch})
        return out
    if addr in CASH and t[0] == T_ERC20:
        frm, to = _addr(t[1]), _addr(t[2])
        if wallet not in (frm, to):
            return []
        v, = _words(log["data"])
        d = (v if to == wallet else 0) - (v if frm == wallet else 0)
        return [base | {"asset": "cash", "contract": addr, "delta": d, "cp": frm if to == wallet else to,
                        "batch": None}]
    return []


def order_filled(log: dict) -> dict:
    """One V2 OrderFilled: amounts from the order maker's side (shares, usdc, fee)."""
    w = _words(log["data"])
    if len(w) != 7:
        raise ValueError("invalid OrderFilled data")
    side = "BUY" if w[0] == 0 else "SELL"
    usdc, shares = (w[2], w[3]) if side == "BUY" else (w[3], w[2])
    if w[0] not in (0, 1):
        raise ValueError("unknown OrderFilled side")
    return {"tx": log["transactionHash"].lower(), "block": int(log["blockNumber"], 16),
            "li": int(log["logIndex"], 16),
            "order_id": log["topics"][1][2:].lower(),
            "maker": _addr(log["topics"][2]), "side": side, "token": str(w[1]),
            "shares": shares, "usdc": usdc, "fee": w[4]}


def ctf_events(receipt: dict) -> list[dict]:
    """Split / merge / redemption events of the ConditionalTokens contract in a receipt."""
    out = []
    for lg in receipt.get("logs") or []:
        if lg["address"].lower() != CTF or not lg["topics"]:
            continue
        t = [word.lower() for word in lg["topics"]]
        if t[0] not in (T_SPLIT, T_MERGE, T_REDEEM):
            continue
        w = _words(lg["data"])
        if len(t) != 4 or len(w) < 4 or w[1] != 96 or len(w) != 4 + w[3]:
            raise ValueError("invalid CTF operation")
        if t[0] in (T_SPLIT, T_MERGE):
            out.append({"kind": "split" if t[0] == T_SPLIT else "merge",
                        "stakeholder": _addr(t[1]), "condition": t[3][2:].lower(), "amount": w[2],
                        "index_sets": w[4:]})
        elif t[0] == T_REDEEM:
            out.append({"kind": "redeem", "stakeholder": _addr(t[1]),
                        "condition": f"{w[0]:064x}", "amount": w[2], "index_sets": w[4:]})
    return out


def _receipt_matches(receipt: dict, tx: str, wallet: str, scanned: list[dict]) -> bool:
    """The receipt used for classification must contain the same wallet movements as getLogs."""
    try:
        if (receipt["transactionHash"].lower() != tx or receipt["status"] != "0x1"
                or int(receipt["blockNumber"], 16) != scanned[0]["block"]):
            return False
        for log in receipt["logs"]:
            if (log["transactionHash"].lower() != tx or log.get("removed")
                    or int(log["blockNumber"], 16) != scanned[0]["block"]):
                return False
        ctf_events(receipt)  # a malformed operation cannot be used to classify wallet movements
        receipt_legs = [leg for log in receipt["logs"] for leg in legs_of(log, wallet)]

        def movements(legs):
            return Counter((g["tx"], g["block"], g["li"], g["asset"], g.get("contract"), g["delta"],
                            g["cp"], tuple(g["batch"] or ())) for g in legs)
        return movements(scanned) == movements(receipt_legs)
    except (KeyError, IndexError, AttributeError, TypeError, ValueError):
        return False


# ---------------------------------------------------------------- chain access
class Chain:
    """RPC reads through ``ordertape.Pool``, every answer cached in ``cache`` (a JSONL file), so a
    rerun repeats no request. Errors and budget exhaustion propagate to the caller."""

    def __init__(self, pool: Pool, cache: Path, routes: dict[str, frozenset[str]] | None = None):
        self.pool, self.cache = pool, Path(cache)
        self.routes = routes or {}
        self.mem: dict[str, Any] = {}
        if self.cache.exists():
            for line in self.cache.read_bytes().splitlines():
                if line.strip():
                    k, v = orjson.loads(line)
                    self.mem[k] = v

    def get(self, method: str, params: list) -> Any:
        k = orjson.dumps([method, params]).decode()
        if k not in self.mem:
            res, _ = self.pool.call(method, params, allowed=self.routes.get(method))
            if res is None and method != "eth_getCode":
                raise RpcError("null_result")
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            with self.cache.open("ab") as fh:
                fh.write(orjson.dumps([k, res]) + b"\n")
            self.mem[k] = res
        return self.mem[k]

    def block_ts(self, n: int) -> int:
        block = self.get("eth_getBlockByNumber", [hex(n), False])
        if not isinstance(block, dict):
            raise RpcError("invalid_block_header")
        try:
            number, stamp = int(block["number"], 16), int(block["timestamp"], 16)
        except (KeyError, TypeError, ValueError) as exc:
            raise RpcError("invalid_block_header") from exc
        if number != n or stamp < 0:
            raise RpcError("invalid_block_header")
        return stamp

    def block_at(self, ts: int, head: int) -> int:
        """First block with timestamp >= ts (interpolation search, bisection fallback)."""
        lo, hi = 0, head
        tlo, thi = self.block_ts(lo), self.block_ts(hi)
        if ts <= tlo:
            return lo
        if ts > thi:
            raise ValueError("timestamp after the chain head")
        step = 0
        while hi - lo > 1:  # invariant: ts(lo) < ts <= ts(hi)
            step += 1
            if step % 2:  # interpolation, then bisection: fast and never worse than O(log n)
                g = min(max(lo + int((ts - tlo) * (hi - lo) / max(thi - tlo, 1)), lo + 1), hi - 1)
            else:
                g = (lo + hi) // 2
            tg = self.block_ts(g)
            if tg < ts:
                lo, tlo = g, tg
            else:
                hi, thi = g, tg
        return hi

    def call(self, to: str, data: str, block: int) -> str:
        return self.get("eth_call", [{"to": to, "data": "0x" + data}, hex(block)])


def scan(chain: Chain, wallet: str, b0: int, b1: int, chunk: int) -> dict:
    """Transfer and OrderFilled logs of ``wallet`` over [b0, b1]; stops at the first failure."""
    if chunk < 1 or b0 < 1 or b1 < b0:
        raise ValueError("use a positive chunk and a nonempty block interval with an opening block")
    wt = topic(wallet)
    queries = [(CTF, [[T_SINGLE, T_BATCH], None, wt]), (CTF, [[T_SINGLE, T_BATCH], None, None, wt]),
               (list(CASH), [T_ERC20, wt]), (list(CASH), [T_ERC20, None, wt]),
               (sorted(EXCHANGES), [T_FILLED, None, wt])]
    logs: dict[tuple[str, int], dict] = {}
    at_index: dict[tuple[int, int], str] = {}
    nxt, error = b0, None
    while nxt <= b1:
        hi = min(nxt + chunk - 1, b1)
        try:
            for address, topics in queries:
                batch = chain.get("eth_getLogs", [{"fromBlock": hex(nxt), "toBlock": hex(hi),
                                                   "address": address, "topics": topics}])
                if not isinstance(batch, list):
                    raise RpcError("invalid_log_response")
                addresses = {address} if isinstance(address, str) else set(address)
                for lg in batch:
                    block = int(lg["blockNumber"], 16)
                    ts = [word.lower() for word in lg["topics"]]
                    lg = {**lg, "topics": ts}
                    if (lg.get("removed") or not nxt <= block <= hi or lg["address"].lower() not in addresses
                            or len(ts) != (3 if lg["address"].lower() in CASH else 4)
                            or not all(re.fullmatch(r"0x[0-9a-f]{64}", t) for t in ts)
                            or any(want is not None and ts[i] not in
                                (want if isinstance(want, list) else [want]) for i, want in enumerate(topics))):
                        raise RpcError("unexpected_log")
                    key = (lg["transactionHash"].lower(), int(lg["logIndex"], 16))
                    if key[1] < 0 or at_index.setdefault((block, key[1]), key[0]) != key[0]:
                        raise RpcError("conflicting_log_index")
                    old = logs.get(key)
                    if old is not None and any(old.get(k) != lg.get(k) for k in
                            ("blockNumber", "blockHash", "address", "topics", "data")):
                        raise RpcError("conflicting_log")
                    if old is None:
                        if ts[0].lower() == T_FILLED:
                            order_filled(lg)
                        else:
                            legs_of(lg, wallet)
                    logs[key] = lg
        except (IndexError, KeyError, TypeError, ValueError, AttributeError):
            error = "invalid_log_response"
            break
        except (BudgetExhausted, EndpointsDown, RpcError) as e:
            error = type(e).__name__ if not isinstance(e, RpcError) else e.kind
            break
        nxt = hi + 1
    return {"logs": [logs[k] for k in sorted(logs, key=lambda k: (int(logs[k]["blockNumber"], 16), k[1]))],
            "next_block": nxt if nxt <= b1 else None, "complete": nxt > b1, "error": error}


def balances(chain: Chain, wallet: str, tokens: list[str], block: int) -> dict[str, int]:
    """Shares per token and cash (pUSD + USDC.e) of ``wallet`` at ``block``."""
    out: dict[str, int] = {}
    if tokens:
        n = len(tokens)
        data = (SEL_BALANCE_OF_BATCH + _enc(64, 96 + 32 * n, n) + _enc(*[int(wallet, 16)] * n)
                + _enc(n, *[int(t) for t in tokens]))
        encoded = chain.call(CTF, data, block)
        if not isinstance(encoded, str) or len(encoded) != 2 + 64 * (n + 2) or not encoded.startswith("0x"):
            raise RpcError("invalid_balance_batch")
        try:
            vals = _words(encoded)
        except ValueError as exc:
            raise RpcError("invalid_balance_batch") from exc
        if vals[:2] != [32, n]:
            raise RpcError("invalid_balance_batch")
        out = dict(zip(tokens, vals[2:], strict=True))
    out["cash"] = 0
    for contract in CASH:
        encoded = chain.call(contract, SEL_BALANCE_OF + _enc(int(wallet, 16)), block)
        if not isinstance(encoded, str) or len(encoded) != 66 or not encoded.startswith("0x"):
            raise RpcError("invalid_cash_balance")
        try:
            out[contract], = _words(encoded)
        except ValueError as exc:
            raise RpcError("invalid_cash_balance") from exc
        out["cash"] += out[contract]
    return out


# ---------------------------------------------------------------- reconstruction
def _micro(x) -> int:
    return round(float(x or 0.0) * U)


def goldsky_complete(pages: dict, wallet: str, t0: int, t1: int, fills: list[dict] | None = None) -> bool:
    """Check terminal wallet queries covering [t0,t1), optionally binding their full spans to the store."""
    if t1 <= t0:
        return False
    ivs = []
    for (a, s, e), r in pages.items():
        if (a != wallet or type(s) is not int or type(e) is not int or s >= t1 or e <= t0 or s >= e or
                r.get("address") != a or
                r.get("start_time") != s or r.get("end_time") != e or not terminal_page(r)):
            continue
        if fills is not None:
            binding = r.get("trade_binding")
            if binding is None:
                continue  # a legacy cursor receipt cannot attest store contents
            if (not isinstance(binding, dict) or type(binding.get("trades")) is not int
                    or not 0 <= binding["trades"] <= r["rows"]
                    or trade_binding(f for f in fills if s <= f["block_timestamp"] < e) != binding):
                return False
        ivs.append((s, e))
    cov = t0
    for s, e in sorted(ivs):
        if s > cov:
            break
        cov = max(cov, e)
    return cov >= t1


def _ts_fn(known: dict[int, int]) -> Callable[[int], float]:
    pts = sorted(known.items())

    def f(b: int) -> float:
        import bisect
        i = bisect.bisect_left(pts, (b, -1))
        if i < len(pts) and pts[i][0] == b:
            return pts[i][1]
        (b0, t0), (b1, t1) = (pts[max(i - 1, 0)], pts[min(i, len(pts) - 1)])
        return t0 if b1 == b0 else t0 + (t1 - t0) * (b - b0) / (b1 - b0)
    return f


def reconstruct(wallet: str, fills: list[dict], legs: list[dict], filled: list[dict],
                receipts: dict[str, dict | None], start: dict[str, int] | None,
                end: dict[str, int] | None, pairs: dict[str, tuple[str, int]],
                block_ts: dict[int, int], window: tuple[int, int], *, times: tuple[int, int] | None = None) -> dict:
    """Pure core: positions, cash, pairing and reconciliation of one wallet (see module doc)."""
    b0, b1 = window
    pairs = dict(pairs)  # inferences from one wallet's receipts are local to that reconstruction
    fill_txs = {f["transaction_hash"].lower() for f in fills} | {o["tx"] for o in filled}
    by_tx: dict[str, list[dict]] = defaultdict(list)
    for lg in legs:
        by_tx[lg["tx"]].append(lg)
    known_ts = dict(block_ts) | {int(f["block_number"]): int(f["block_timestamp"]) for f in fills}
    ts = _ts_fn(known_ts)

    # Bind each public fill to its particular on-chain OrderFilled, including in-block order.
    ch_keys = [(o["tx"], o["order_id"], o["block"], o["side"], o["token"], o["shares"], o["usdc"], o["fee"])
               for o in filled]
    order_locations = defaultdict(deque)
    for key, o in sorted(zip(ch_keys, filled), key=lambda item: item[1]["li"]):
        order_locations[key].append(o["li"])
    gs_orders: Counter = Counter()
    # economic events: Goldsky in fill txs, chain legs elsewhere
    events: list[dict] = []
    for f in fills:
        sh, usd, fee = _micro(f["amount_shares"]), _micro(f["amount_usdc"]), _micro(f.get("fee"))
        buy = f["side"] == "BUY"
        key = (f["transaction_hash"].lower(), (f.get("order_id") or "").lower().removeprefix("0x"),
               int(f["block_number"]), f["side"], f["token_id"], sh, usd, fee)
        gs_orders[key] += 1
        location = order_locations[key].popleft() if order_locations[key] else None
        events.append({"block": int(f["block_number"]), "tx": f["transaction_hash"].lower(),
                       "chain_log_index": location,
                       "kind": "buy" if buy else "sell", "src": "goldsky",
                       "shares": {f["token_id"]: sh if buy else -sh},
                       "cash": -(usd + fee) if buy else usd - fee, "fee": fee,
                       "role": f.get("order_type"), "price": f.get("price")})
    kinds: Counter[str] = Counter()
    senders: Counter[str] = Counter()
    for tx, ls in by_tx.items():
        if tx in fill_txs:
            continue
        shares: dict[str, int] = defaultdict(int)
        for lg in ls:
            if lg["asset"] != "cash":
                shares[lg["asset"]] += lg["delta"]
        rec = receipts.get(tx)
        ctf: list[dict] = []
        if not shares:
            kind = "cash"
        elif rec is None or not _receipt_matches(rec, tx, wallet, ls):
            kind = "unclassified"
        else:
            if rec.get("from"):
                senders[rec["from"].lower()] += 1
            share_cps = {lg["cp"] for lg in ls if lg["asset"] != "cash"}
            ctf = [e for e in ctf_events(rec) if e["stakeholder"] == wallet or
                   (e["stakeholder"] in ADAPTERS and e["stakeholder"] in share_cps)]
            ev = {e["kind"] for e in ctf}
            if not ev:
                kind = "transfer"
            elif (len(ev) != 1 or len({e["condition"] for e in ctf}) != 1 or
                  not share_cps <= ({ZERO} | {e["stakeholder"] for e in ctf if e["stakeholder"] in ADAPTERS})):
                kind = "unclassified"
            else:
                kind = next(iter(ev))
            if len(ctf) == 1 and ctf[0]["kind"] == kind and sorted(ctf[0]["index_sets"]) == [1, 2]:
                event = ctf[0]
                batches = {tuple(lg["batch"]) for lg in ls if lg["batch"]}
                if len(batches) == 1:
                    batch = next(iter(batches))
                    if len(batch) == len(shares) == 2 and set(batch) == set(shares):
                        inferred = {t: (event["condition"], mask - 1)
                                    for t, mask in zip(batch, event["index_sets"], strict=True)}
                        if all(t not in pairs or pairs[t] == value for t, value in inferred.items()):
                            pairs.update(inferred)
        kinds[kind] += 1
        events.append({"block": ls[0]["block"], "tx": tx, "kind": kind, "src": "chain",
                       "chain_log_index": min(lg["li"] for lg in ls),
                       "shares": dict(shares), "fee": 0, "role": None, "price": None,
                       "cash": sum(lg["delta"] for lg in ls if lg["asset"] == "cash")})
        if kind in ("split", "merge", "redeem"):
            events[-1]["ctf"] = ctf
            events[-1]["share_counterparties"] = sorted({lg["cp"] for lg in ls if lg["asset"] != "cash"})
    events.sort(key=lambda e: (e["block"], e["chain_log_index"] is None, e["chain_log_index"], e["tx"]))

    tokens = sorted(({t for e in events for t in e["shares"]} | {lg["asset"] for lg in legs}
                     | set(start or {}) | set(end or {})) - {"cash", *CASH})
    open_ = {t: (start or {}).get(t, 0) for t in tokens} | {"cash": (start or {}).get("cash", 0)}

    # per-token path and totals
    tok: dict[str, dict] = {t: {"open": open_[t], "pos": open_[t], "path": [], "by_kind": Counter(),
                                "cash": 0, "fees": 0, "min_pos": open_[t]} for t in tokens}
    cash_by_kind: Counter[str] = Counter()
    fees = 0
    for e in events:
        cash_by_kind[e["kind"]] += e["cash"]
        fees += e["fee"]
        for t, d in e["shares"].items():
            s = tok[t]
            s["pos"] += d
            s["by_kind"][e["kind"]] += d
            s["path"].append([e["block"], e["kind"], d, s["pos"]])
            if len(e["shares"]) == 1:
                s["cash"] += e["cash"]
                s["fees"] += e["fee"]
            s["min_pos"] = min(s["min_pos"], s["pos"])

    # fill cross-checks: Goldsky vs chain legs per (tx, token) and per tx cash; OrderFilled
    gs_tx: dict[tuple[str, str], int] = defaultdict(int)
    gs_gross: Counter = Counter()
    max_cash_out: Counter = Counter()
    for e in events:
        if e["src"] == "goldsky":
            for t, d in e["shares"].items():
                gs_tx[(e["tx"], t)] += d
                gs_gross[(e["tx"], t)] += abs(d)
            gs_tx[(e["tx"], "cash")] += e["cash"]
            # A seller may receive net proceeds or receive gross and pay the fee separately.
            max_cash_out[e["tx"]] += -e["cash"] if e["kind"] == "buy" else e["fee"]
    ch_tx: dict[tuple[str, str], int] = defaultdict(int)
    ch_gross: Counter = Counter()
    cash_out: Counter = Counter()
    other_collateral_txs: set[str] = set()
    for tx in fill_txs:
        for lg in by_tx.get(tx, []):
            ch_tx[(tx, lg["asset"])] += lg["delta"]
            if lg["asset"] != "cash":
                ch_gross[(tx, lg["asset"])] += abs(lg["delta"])
            else:
                if lg["delta"] and lg.get("contract") != PUSD:
                    other_collateral_txs.add(tx)
                if lg["delta"] < 0:
                    cash_out[tx] -= lg["delta"]
    mism = sorted(k for k in set(gs_tx) | set(ch_tx) if gs_tx.get(k, 0) != ch_tx.get(k, 0))
    hidden = sorted(k for k in gs_gross.keys() | ch_gross.keys()
                    if gs_tx[k] == ch_tx[k] and gs_gross[k] != ch_gross[k])
    hidden_cash = sorted(tx for tx in fill_txs if gs_tx[(tx, "cash")] == ch_tx[(tx, "cash")]
                         and cash_out[tx] > max_cash_out[tx])
    # Match a multiset: within one transaction an order can appear on more than one log.
    ch_orders = Counter(ch_keys)
    matched = (gs_orders & ch_orders).total()
    gs_left = Counter()
    ch_left = Counter()
    for key, n in (gs_orders - ch_orders).items():
        gs_left[key[:2]] += n
    for key, n in (ch_orders - gs_orders).items():
        ch_left[key[:2]] += n
    amount_mismatch = (gs_left & ch_left).total()
    of = {"chain": len(filled), "matched": matched, "amount_mismatch": amount_mismatch,
          "chain_only": ch_left.total() - amount_mismatch, "goldsky_only": gs_left.total() - amount_mismatch}
    gs_fill_txs = {f["transaction_hash"].lower() for f in fills}
    chain_fill_txs = {o["tx"] for o in filled}
    missing_in_goldsky = sorted(chain_fill_txs - gs_fill_txs)
    no_legs = sorted(tx for tx in gs_fill_txs if tx not in by_tx)

    # reconciliation against balances
    rows_per: Counter[str] = Counter()
    for f in fills:
        rows_per[f["token_id"]] += 1
        rows_per["cash"] += 1
    recon: dict[str, dict] = {}
    chain_sum: dict[str, int] = defaultdict(int)
    for lg in legs:
        chain_sum[lg["asset"]] += lg["delta"]
    for a in [*tokens, "cash"]:
        built = (tok[a]["pos"] - tok[a]["open"]) if a != "cash" else sum(e["cash"] for e in events)
        r = {"reconstructed": built, "chain_legs": chain_sum.get(a, 0), "tolerance": rows_per[a]}
        if start is not None and end is not None:
            obs = end.get(a, 0) - start.get(a, 0)
            r |= {"start": start.get(a, 0), "end": end.get(a, 0), "observed": obs,
                  "residual": obs - built, "log_residual": obs - chain_sum.get(a, 0)}
        recon[a] = r
    causes: list[str] = []
    collateral = {}
    if start is not None and end is not None:
        for contract in CASH:
            if contract not in start or contract not in end:
                causes.append("collateral_balances_unavailable")
                break
            moved = sum(lg["delta"] for lg in legs if lg.get("contract") == contract)
            observed = end[contract] - start[contract]
            collateral[contract] = {"open_micro": start[contract], "close_micro": end[contract],
                                    "observed_micro": observed, "chain_legs_micro": moved,
                                    "residual_micro": observed - moved}
        if any(row["residual_micro"] for row in collateral.values()):
            causes.append("collateral_logs_do_not_explain_balances")
    if mism:
        causes.append("fill_transaction_legs_disagree")
    if hidden:
        causes.append("fill_transaction_has_unexplained_share_movements")
    if hidden_cash:
        causes.append("fill_transaction_has_unexplained_cash_movements")
    if other_collateral_txs:
        causes.append("v2_fill_has_other_collateral_movements")
    if matched != len(fills) or matched != len(filled):
        causes.append("fill_orders_disagree")
    if start is None or end is None:
        causes.append("balances_unavailable")
    else:
        bad = [a for a, r in recon.items() if abs(r["residual"]) > r["tolerance"]]
        if any(recon[a]["log_residual"] for a in recon):
            causes.append("chain_logs_do_not_explain_balances")
        if missing_in_goldsky:
            causes.append("goldsky_missing_fill_txs")
        if no_legs:
            causes.append("goldsky_fill_without_chain_legs")
        if any(a != "cash" for a in bad):
            causes.append("share_residual")
        if "cash" in bad:
            r = recon["cash"]["residual"]
            fee_sets = {fees, sum(o["fee"] for o in filled)} - {0}  # Goldsky's or the chain's fees
            causes.append("cash_residual_equals_fees"
                          if any(abs(abs(r) - f) <= rows_per["cash"] for f in fee_sets)
                          else "cash_residual")
    if kinds["unclassified"]:
        causes.append("unclassified_chain_txs")

    pairing = _pairing(events, open_, pairs, ts, times or (ts(b0), ts(b1)))
    pairing["interpolated_event_blocks"] = len({e["block"] for e in events if e["shares"] and e["block"] not in known_ts})
    to_f = lambda x: round(x / U, 6)  # noqa: E731
    out_tokens = {}
    for t, s in tok.items():
        out_tokens[t] = {"condition": (pairs.get(t) or (None,))[0], "idx": (pairs.get(t) or (0, None))[1],
                         "open": to_f(s["open"]), "close": to_f(s["pos"]),
                         "min_position": to_f(s["min_pos"]),
                         "by_kind": {k: to_f(v) for k, v in s["by_kind"].items()},
                         "fill_cash": to_f(s["cash"]), "fees": to_f(s["fees"]),
                         "path": [[b, k, to_f(d), to_f(p)] for b, k, d, p in s["path"]]}
    return {"wallet": wallet, "window_blocks": [b0, b1], "goldsky_fills": len(fills),
            "chain_legs": len(legs), "tx_kinds": dict(kinds), "tokens": out_tokens,
            "collateral_reconciliation": collateral,
            # Keep cash attached to its original legs. Splits, merges and redemptions can
            # touch both outcomes; assigning the same cash to each token would double count.
            "condition_cash": [{k: e[k] for k in ("block", "chain_log_index", "tx", "kind", "shares", "cash", "fee",
                                                   "ctf", "share_counterparties") if k in e} for e in events],
            "cash": {"open": to_f(open_["cash"]), "by_kind": {k: to_f(v) for k, v in cash_by_kind.items()},
                     "fees": to_f(fees)},
            "fill_check": {"tx_asset_mismatches": len(mism), "examples": [list(k) for k in mism[:20]],
                           "unexplained_gross_share_movements": len(hidden),
                           "examples_unexplained_gross_shares": [list(k) for k in hidden[:20]],
                           "unexplained_gross_cash_movements": len(hidden_cash),
                           "examples_unexplained_gross_cash": hidden_cash[:20],
                           "other_collateral_fill_txs": len(other_collateral_txs),
                           "examples_other_collateral_fill_txs": sorted(other_collateral_txs)[:20],
                           "order_filled": of, "chain_fill_txs_missing_in_goldsky": len(missing_in_goldsky),
                           "goldsky_fill_txs_without_chain_legs": len(no_legs),
                           "examples_missing": missing_in_goldsky[:20]},
            "reconciliation": {a: {k: (to_f(v) if k != "tolerance" else v) for k, v in r.items()}
                               for a, r in recon.items()},
            "pairing": pairing, "tx_senders": dict(senders), "causes": causes}


def _pairing(events: list[dict], open_: dict[str, int], pairs: dict[str, tuple[str, int]],
             ts: Callable[[int], float], span: tuple[float, float]) -> dict:
    markets: dict[str, dict[int, str]] = defaultdict(dict)
    for t, (cond, idx) in pairs.items():
        if t in open_:
            markets[cond][idx] = t
    unpaired = sorted(t for t in open_ if t != "cash" and
                      (t not in pairs or set(markets[pairs[t][0]]) != {0, 1}))
    out: dict[str, dict] = {}
    tot: defaultdict[str, float] = defaultdict(float)
    for cond, sides in markets.items():
        if set(sides) != {0, 1}:
            continue
        a, b = sides[0], sides[1]
        pos = {a: open_[a], b: open_[b]}
        m: defaultdict[str, float] = defaultdict(float)
        m["max_one_sided"] = abs(pos[a] - pos[b])
        t_prev, longest = span[0], 0.0
        spell_start = span[0] if abs(pos[a] - pos[b]) >= SPELL_MIN else None

        def advance(t: float) -> None:  # integrate the current state over [t_prev, t)
            nonlocal t_prev, longest
            dt = max(t - t_prev, 0.0)
            m["paired_share_s"] += min(max(pos[a], 0), max(pos[b], 0)) * dt
            m["one_sided_share_s"] += abs(pos[a] - pos[b]) * dt
            if spell_start is not None:
                longest = max(longest, t - spell_start)
            t_prev = max(t, t_prev)

        for e in events:
            if a not in e["shares"] and b not in e["shares"]:
                continue
            advance(ts(e["block"]))
            for t, d in e["shares"].items():
                if t not in pos:
                    continue
                i = pos[a] - pos[b]
                di = d if t == a else -d
                toward = min(abs(di), abs(i)) if i * di < 0 else 0
                if e["kind"] in ("buy", "sell"):
                    good, bad = ("completing", "directional") if d > 0 else ("derisk", "unpair")
                    m[good] += toward
                    m[bad] += abs(di) - toward
                pos[t] += d
            # A split or merge moves both siblings in the same operation.
            m["max_one_sided"] = max(m["max_one_sided"], abs(pos[a] - pos[b]))
            if abs(pos[a] - pos[b]) < SPELL_MIN:
                spell_start = None
            elif spell_start is None:
                spell_start = t_prev
        advance(span[1])
        m["one_sided_at_end"] = abs(pos[a] - pos[b])
        for k, v in m.items():
            tot[k] += v
        out[cond] = {"tokens": [a, b], "longest_one_sided_s": round(longest, 1),
                     **{k: round(v / U, 6) for k, v in m.items()}}
    buys = tot["completing"] + tot["directional"]
    held = tot["paired_share_s"] + tot["one_sided_share_s"]
    return {"markets": out, "tokens_without_pair": unpaired,
            "completing_share_of_buys": round(tot["completing"] / buys, 4) if buys else None,
            "paired_share_of_holding_time": round(tot["paired_share_s"] / held, 4) if held else None,
            "longest_one_sided_s": max((x["longest_one_sided_s"] for x in out.values()), default=None)}


# ---------------------------------------------------------------- per wallet
def load_pairs(path: Path | None) -> dict[str, tuple[str, int]]:
    """token -> (condition id without 0x, outcome index) from a ledger ``tokens.jsonl`` cache."""
    out: dict[str, tuple[str, int]] = {}
    slots = {}
    if path and Path(path).exists():
        with Path(path).open("rb") as fh:
            for line in fh:
                if not line.strip():
                    continue
                d = orjson.loads(line)
                if d.get("condition") and d.get("idx") is not None:
                    token, idx = d["token"], d["idx"]
                    if type(idx) is not int or idx < 0:
                        raise ValueError(f"invalid venue outcome index for token {token}")
                    slot = (d["condition"].lower().removeprefix("0x"), idx)
                    if out.setdefault(token, slot) != slot or slots.setdefault(slot, token) != token:
                        raise ValueError(f"conflicting venue outcome mapping for token {token}")
    return out


def identities(chain: Chain | None, wallet: str, orders: Path | None, block: int, *,
               first_block: int, filled: list[dict], scan_complete: bool) -> dict:
    """Attribute signer fields only to verified order hashes observed on-chain in this interval."""
    wanted = {(o["tx"], o["block"], o["order_id"], o["token"], o["side"])
              for o in filled if o["maker"] == wallet}
    observations: dict[tuple, set[tuple[str, int]]] = {}
    invalid_tape = None
    if orders is not None and wanted:
        import pyarrow.dataset as ds
        from .ordertape import committed_parts

        columns = ["tx", "block", "order_id", "token_id", "side", "signer", "sig_type"]
        try:
            parts = committed_parts(orders)
            if parts:
                data = ds.dataset([str(path) for path in parts], format="parquet")
                mask = ((ds.field("maker") == wallet) & (ds.field("verified") == True)
                        & (ds.field("block") >= first_block) & (ds.field("block") <= block))
                for batch in data.to_batches(columns=columns, filter=mask, batch_size=4096):
                    for row in batch.to_pylist():
                        key = (row["tx"], row["block"], row["order_id"], row["token_id"], row["side"])
                        if key not in wanted:
                            continue
                        signer, kind = row["signer"], row["sig_type"]
                        if (isinstance(signer, str) and re.fullmatch(r"0x[0-9a-f]{40}", signer)
                                and signer != ZERO and type(kind) is int and 0 <= kind <= 255):
                            observations.setdefault(key, set()).add((signer, kind))
        except (OSError, ValueError, KeyError) as err:
            invalid_tape = str(err)
            observations.clear()
    claims = [next(iter(claim)) for claim in observations.values() if len(claim) == 1]
    conflicts = sum(len(claim) > 1 for claim in observations.values())
    status = ("invalid_order_tape" if invalid_tape else "conflicting" if conflicts else
              "chain_scan_incomplete" if not scan_complete else "no_orders_in_interval" if not wanted else
              "complete" if len(claims) == len(wanted) else "partial" if claims else "unknown")
    out: dict = {"trading_account": wallet, "signers": dict(Counter(s for s, _ in claims)),
                 "sig_types": dict(Counter(str(k) for _, k in claims)), "is_contract": None,
                 "safe_owners": None, "signer_coverage": {"status": status, "chain_orders": len(wanted),
                 "verified_orders": len(claims), "conflicting_orders": conflicts}}
    if invalid_tape:
        out["signer_coverage"]["error"] = invalid_tape
    if chain is not None:
        try:
            code = chain.get("eth_getCode", [wallet, hex(block)])
            if isinstance(code, str) and re.fullmatch(r"0x(?:[a-fA-F0-9]{2})*", code):
                out["is_contract"] = code != "0x"
            if out["is_contract"] and "2" in out["sig_types"]:
                raw = chain.call(wallet, SEL_GET_OWNERS, block)
                if isinstance(raw, str) and re.fullmatch(r"0x(?:[a-fA-F0-9]{64}){3,}", raw):
                    words = _words(raw)
                    owners = words[2:]
                    if (words[:2] == [32, len(owners)] and len(set(owners)) == len(owners)
                            and all(0 < addr < 2**160 for addr in owners)):
                        out["safe_owners"] = [f"0x{addr:040x}" for addr in owners]
        except (BudgetExhausted, EndpointsDown, RpcError):
            pass
    return out


def signer_links(identities_by_wallet: dict) -> list[dict]:
    """Shared signing keys are observable links between accounts, without inferring beneficial ownership."""
    accounts: dict[str, list[str]] = defaultdict(list)
    for wallet, identity in identities_by_wallet.items():
        for signer in identity["signers"]:
            accounts[signer].append(wallet)
    return [{"signer": signer, "trading_accounts": sorted(wallets)}
            for signer, wallets in sorted(accounts.items()) if len(wallets) > 1]


def absorb_wallet(chain: Chain, wallet: str, fills: list[dict], window: tuple[int, int],
                  times: tuple[int, int], chunk: int, pages: dict, pairs: dict,
                  orders: Path | None = None) -> dict:
    b0, b1 = window
    if times[1] <= times[0]:
        raise ValueError("use a nonempty time interval")
    pagination_ok = goldsky_complete(pages, wallet, *times)
    store_ok = pagination_ok and goldsky_complete(pages, wallet, *times, fills=fills)
    row_bounds_ok = all((b0 <= f["block_number"] <= b1) == (times[0] <= f["block_timestamp"] < times[1])
                        and ("address" not in f or f["address"].lower() == wallet) for f in fills)
    fills = [f for f in fills if b0 <= int(f["block_number"]) <= b1]
    sc = scan(chain, wallet, b0, b1, chunk)
    legs = [lg for log in sc["logs"] for lg in legs_of(log, wallet)]
    filled = [order_filled(log) for log in sc["logs"]
              if log["address"].lower() in EXCHANGES and log["topics"][0] == T_FILLED]
    fill_txs = {f["transaction_hash"].lower() for f in fills} | {o["tx"] for o in filled}
    receipts: dict[str, dict | None] = {}
    err = sc["error"]
    for tx in sorted({lg["tx"] for lg in legs if lg["asset"] != "cash"} - fill_txs):
        try:
            receipts[tx] = chain.get("eth_getTransactionReceipt", [tx])
        except (BudgetExhausted, EndpointsDown, RpcError) as e:
            receipts[tx] = None
            err = err or type(e).__name__
    touched = {lg["asset"] for lg in legs if lg["asset"] != "cash"} | {f["token_id"] for f in fills}
    # A buy can complete a position opened before this window, even if its opposite token never moves.
    markets = {pairs[t][0] for t in touched if t in pairs}
    tokens = sorted(touched | {t for t, (condition, _) in pairs.items() if condition in markets})
    start = end = None
    bts: dict[int, int] = {}
    boundaries = {}
    try:
        bts = {b0: chain.block_ts(b0), b1: chain.block_ts(b1)}
        boundaries = {b0 - 1: chain.block_ts(b0 - 1), b1 + 1: chain.block_ts(b1 + 1)}
        start = balances(chain, wallet, tokens, b0 - 1)
        end = balances(chain, wallet, tokens, b1)
    except (BudgetExhausted, EndpointsDown, RpcError) as e:
        err = err or type(e).__name__
        start = end = None
    if not bts:
        bts = {b0: times[0], b1: times[1]}
    res = reconstruct(wallet, fills, legs, filled, receipts, start, end, pairs, bts, window, times=times)
    bounds_ok = bool(boundaries and boundaries[b0 - 1] < times[0] <= bts[b0]
                     and bts[b0] <= bts[b1] < times[1] <= boundaries[b1 + 1])
    causes = res.pop("causes")
    if not bounds_ok:
        causes.insert(0, "block_time_window_unverified")
    if not row_bounds_ok:
        causes.insert(0, "goldsky_fill_scope_disagrees")
    if not pagination_ok:
        causes.insert(0, "goldsky_incomplete")
    elif not store_ok:
        causes.insert(0, "goldsky_store_unverified")
    if not sc["complete"]:
        causes.insert(0, "chain_scan_incomplete")
    pairing = res["pairing"]
    pairing["coverage"] = ("unreconciled" if causes else "incomplete" if pairing["tokens_without_pair"]
                           else "complete" if pairing["markets"] else "no_positions_observed")
    if pairing["coverage"] != "complete":
        for key in ("completing_share_of_buys", "paired_share_of_holding_time", "longest_one_sided_s"):
            pairing[key] = None
    res = {"status": "not_reconciled" if causes else "reconciled", "not_reconciled": bool(causes),
           "causes": causes, "window_time": list(times),
           "completeness": {"goldsky_complete": store_ok, "goldsky_pagination_complete": pagination_ok,
                            "goldsky_store_verified": store_ok, "block_time_window_verified": bounds_ok,
                            "goldsky_queries": [
                                {k: rec.get(k) for k in ("start_time", "end_time", "pages", "rows", "stop", "trade_binding")}
                                for (addr, lo, hi), rec in sorted(pages.items())
                                if addr == wallet and lo < times[1] and hi > times[0]],
                            "chain_window_timestamps": (
                                [boundaries[b0 - 1], bts[b0], bts[b1], boundaries[b1 + 1]] if boundaries else None),
                            "chain_scan_complete": sc["complete"],
                            "chain_next_block": sc["next_block"], "chain_error": err,
                            "receipts_missing": sum(r is None for r in receipts.values())},
           "identity": identities(chain, wallet, orders, b1, first_block=b0, filled=filled,
                                  scan_complete=sc["complete"])} | res
    res["identity"]["tx_senders"] = res.pop("tx_senders")
    return res


def cohort_row(r: dict) -> dict:
    rec = r["reconciliation"]
    share_res = [abs(v.get("residual", 0)) for k, v in rec.items() if k != "cash"]
    p = r["pairing"]
    return {"wallet": r["wallet"], "status": r["status"], "causes": ",".join(r["causes"]),
            "fills": r["goldsky_fills"], "tokens": len(r["tokens"]),
            "tokens_with_share_residual": sum(x > 1e-6 for x in share_res),
            "max_share_residual": max(share_res, default=0.0),
            "cash_residual": rec["cash"].get("residual"),
            "completing_share_of_buys": p["completing_share_of_buys"],
            "paired_share_of_holding_time": p["paired_share_of_holding_time"],
            "longest_one_sided_s": p["longest_one_sided_s"],
            "splits": r["tx_kinds"].get("split", 0), "merges": r["tx_kinds"].get("merge", 0),
            "redeems": r["tx_kinds"].get("redeem", 0), "transfers": r["tx_kinds"].get("transfer", 0)}


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None, transport: Callable = http_transport) -> int:
    ap = argparse.ArgumentParser(prog="doppel-absorb", description="Absorption ledger per wallet")
    ap.add_argument("--store", type=Path, required=True, help="Parquet fill store (fillstore)")
    ap.add_argument("--wallets", nargs="+", required=True)
    ap.add_argument("--from", dest="t_from", type=int, help="window start, unix seconds")
    ap.add_argument("--to", dest="t_to", type=int, help="window end, unix seconds")
    ap.add_argument("--from-block", type=int, help="instead of --from")
    ap.add_argument("--to-block", type=int, help="instead of --to")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--rpc", nargs="+", help="general RPC endpoints; also overrides the built-in role defaults")
    ap.add_argument("--log-rpc", nargs="+", help="only endpoints serving eth_getLogs")
    ap.add_argument("--state-rpc", nargs="+", help="only endpoints serving historical eth_call and eth_getCode")
    ap.add_argument("--max-requests", type=int, default=None, help="HTTP requests, retries included")
    ap.add_argument("--rate", type=float, default=2.0, help="requests per second per endpoint")
    ap.add_argument("--chunk-blocks", type=int, default=DEFAULT_CHUNK, help="eth_getLogs block span")
    ap.add_argument("--pages", type=Path, help="goldsky_activity/pagination.jsonl")
    ap.add_argument("--tokens", type=Path, help="ledger tokens.jsonl (token -> condition, idx)")
    ap.add_argument("--orders", type=Path, help="ordertape output directory (signers)")
    a = ap.parse_args(argv)
    if (a.t_from is None) == (a.from_block is None) or (a.t_to is None) == (a.to_block is None):
        ap.error("give --from or --from-block, and --to or --to-block")
    if a.chunk_blocks < 1 or a.rate <= 0 or (a.max_requests is not None and a.max_requests < 0):
        ap.error("use positive chunk-blocks and rate and a nonnegative request budget")
    if a.t_from is not None and a.t_to is not None and a.t_to <= a.t_from:
        ap.error("the end time must follow the start")
    if a.from_block is not None and (a.from_block < 1 or a.to_block is not None and a.to_block < a.from_block):
        ap.error("invalid block interval")
    wallets = [w.lower() for w in a.wallets]
    if len(set(wallets)) != len(wallets) or any(not re.fullmatch(r"0x[0-9a-f]{40}", w) for w in wallets):
        ap.error("wallets must be distinct 20-byte addresses")
    a.out.mkdir(parents=True, exist_ok=True)
    general = a.rpc or list(DEFAULT_RPCS)
    log_rpc = a.log_rpc or a.rpc or [DEFAULT_LOG_RPC]
    state_rpc = a.state_rpc or a.rpc or [DEFAULT_STATE_RPC]
    routes = {"eth_getLogs": frozenset(log_rpc), "eth_call": frozenset(state_rpc),
              "eth_getCode": frozenset(state_rpc)}
    for method in ("eth_blockNumber", "eth_getBlockByNumber", "eth_getTransactionReceipt"):
        routes[method] = frozenset(general)
    pool = Pool([*general, *log_rpc, *state_rpc], transport, rate=a.rate, max_requests=a.max_requests)
    chain = Chain(pool, a.out / "_chain" / "rpc-cache.jsonl", routes)
    try:
        # A retry at the same output path can observe a later head. Only historical reads are reusable.
        head = 0
        if a.from_block is None or a.to_block is None:
            head_hex, _ = pool.call("eth_blockNumber", [], allowed=routes["eth_blockNumber"])
            if not isinstance(head_hex, str):
                raise RpcError("invalid_chain_head")
            head = int(head_hex, 16)
        b0, b1 = a.from_block, a.to_block
        if b0 is None:
            assert a.t_from is not None  # CLI enforces exactly one bound
            b0 = chain.block_at(a.t_from, head)
        if b1 is None:
            assert a.t_to is not None
            b1 = chain.block_at(a.t_to, head) - 1
        if b0 < 1 or b1 < b0:
            ap.error("empty block interval or unavailable opening block")
        # When --to-block is inclusive, require pagination coverage of its final UTC second.
        times = (a.t_from if a.t_from is not None else chain.block_ts(b0),
                 a.t_to if a.t_to is not None else chain.block_ts(b1) + 1)
    except (BudgetExhausted, EndpointsDown, RpcError, ValueError) as e:
        print(f"cannot resolve the window to blocks: {e}", file=sys.stderr)
        return 2
    pages = pagination_state(a.pages) if a.pages else {}
    pairs = load_pairs(a.tokens)
    by_wallet = dict(iter_wallet_fills(a.store, wallets))
    rows = []
    wallet_identities = {}
    for w in wallets:
        r = absorb_wallet(chain, w, by_wallet.get(w, []), (b0, b1), times, a.chunk_blocks, pages,
                          pairs, a.orders)
        atomic_write(a.out / f"wallet-{w}.json", orjson.dumps(r, option=orjson.OPT_INDENT_2))
        wallet_identities[w] = r["identity"]
        rows.append(cohort_row(r))
        print(orjson.dumps(rows[-1]).decode(), flush=True)
    cols = list(rows[0]) if rows else []
    lines = ["\t".join(cols)] + ["\t".join("" if x[c] is None else str(x[c]) for c in cols) for x in rows]
    atomic_write(a.out / "cohort.tsv", ("\n".join(lines) + "\n").encode())
    summary = {"window_blocks": [b0, b1], "window_time": list(times), "wallets": len(rows),
               "reconciled": sum(x["status"] == "reconciled" for x in rows),
               "rpc": pool.stats(), "requests": pool.requests}
    atomic_write(a.out / "cohort.json", orjson.dumps({"summary": summary, "rows": rows,
                 "shared_verified_signers": signer_links(wallet_identities)}, option=orjson.OPT_INDENT_2))
    print(orjson.dumps(summary).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
