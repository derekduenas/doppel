"""Order tape: the signed orders behind Polymarket intl fills, decoded from chain calldata (E34).

Every fill is settled by the operator calling CTF Exchange V2
``matchOrders(conditionId, takerOrder, makerOrders[], takerFillAmount, makerFillAmounts[],
takerFeeAmount, makerFeeAmounts[])``. The calldata carries each signed ``Order`` in full: the ms
``timestamp`` it was created, ``makerAmount``/``takerAmount`` (full size and limit, not just the
filled part), ``signatureType`` and ``builder``. So for every FILLED order we learn when it was
placed, how long it rested, its intended size and what was left.

Trust rules:
- An order is used only if its EIP-712 hash (computed here) is the ``order_id`` Goldsky reports
  for the fill; ``order_hash`` reproduces the contract's ``hashOrder``.
- ``created_ms`` is self-reported by the signer. ``age_s`` (block time - created) is reported so
  implausible stamps (negative, or far older than any resting order could be) can be screened;
  cross-checking against the first appearance of the size on the CLOB tape is the next step.
- Orders that never filled are not on chain. This tape cannot see them.

Amounts are 6-decimal integers on chain; here ``size`` is shares and ``limit`` dollars per share:
BUY gives makerAmount USDC for takerAmount shares, SELL gives makerAmount shares for takerAmount USDC.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import orjson
from Crypto.Hash import keccak

EXCHANGES = {  # Polygon, chainId 137
    "0xe111180000d2663c0091e4f400237545b87b996b": "standard",
    "0xe2222d279d744050d28e00520010520000310f59": "negrisk",
}
CHAIN_ID = 137
SELECTOR = "3c2b4399"  # matchOrders(bytes32,Order,Order[],uint256,uint256[],uint256,uint256[])
ORDER_TYPEHASH = bytes.fromhex("bb86318a2138f5fa8ae32fbe8e659f8fcf13cc6ae4014a707893055433818589")
ORDER_WORDS = 11  # static fields before the signature
W = 32


def _k(b: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=b).digest()


def _domain(exchange: str) -> bytes:
    th = _k(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
    return _k(th + _k(b"Polymarket CTF Exchange") + _k(b"2") + CHAIN_ID.to_bytes(W, "big")
              + bytes.fromhex(exchange[2:]).rjust(W, b"\0"))


def order_hash(static: bytes, exchange: str) -> str:
    """The contract's ``hashOrder``: EIP-712 over the 11 static order words."""
    return _k(b"\x19\x01" + _domain(exchange) + _k(ORDER_TYPEHASH + static)).hex()


def _u(d: bytes, off: int) -> int:
    return int.from_bytes(d[off:off + W], "big")


def _order(d: bytes, off: int, exchange: str) -> dict:
    static = d[off:off + ORDER_WORDS * W]
    f = [_u(static, i * W) for i in range(ORDER_WORDS)]
    side = "BUY" if f[6] == 0 else "SELL"
    usdc, shares = (f[4], f[5]) if side == "BUY" else (f[5], f[4])
    return {"order_id": order_hash(static, exchange), "maker": "0x" + static[W + 12:2 * W].hex(),
            "signer": "0x" + static[2 * W + 12:3 * W].hex(), "token_id": str(f[3]), "side": side,
            "size": shares / 1e6, "limit": usdc / shares if shares else None,
            "maker_amount": f[4], "taker_amount": f[5], "sig_type": f[7], "created_ms": f[8],
            "builder": static[10 * W:11 * W].hex(), "salt": str(f[0])}


def _uints(d: bytes, off: int) -> list[int]:
    return [_u(d, off + W + i * W) for i in range(_u(d, off))]


def decode_match(input_hex: str, exchange: str) -> list[dict] | None:
    """Orders in one ``matchOrders`` call, taker first. None when the call is something else."""
    h = input_hex[2:] if input_hex.startswith("0x") else input_hex
    if h[:8] != SELECTOR or exchange.lower() not in EXCHANGES:
        return None
    d = bytes.fromhex(h[8:])
    taker = _order(d, _u(d, W), exchange)
    mo = _u(d, 2 * W)
    n = _u(d, mo)
    makers = [_order(d, mo + W + _u(d, mo + W + i * W), exchange) for i in range(n)]
    mfill, mfee = _uints(d, _u(d, 4 * W)), _uints(d, _u(d, 6 * W))
    if len(mfill) != n or len(mfee) != n:
        return None
    out = [taker | {"role": "taker", "fill_amount": _u(d, 3 * W), "fee_amount": _u(d, 5 * W)}]
    out += [m | {"role": "maker", "fill_amount": mfill[i], "fee_amount": mfee[i]} for i, m in enumerate(makers)]
    cid = d[:W].hex()
    return [o | {"condition_id": cid, "n_makers": n} for o in out]


# ---------------------------------------------------------------- fetching
def rpc_batch(url: str, calls: list[tuple[str, list]], timeout: float = 30.0) -> list:
    """One request per call: free public endpoints reject JSON-RPC batch arrays (403)."""
    out = []
    for m, p in calls:
        req = urllib.request.Request(url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": m, "params": p}).encode(),
                                     {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
        out.append(json.loads(urllib.request.urlopen(req, timeout=timeout).read()).get("result"))
    return out


def fetch_orders(url: str, txs: list[str], batch: int = 20, pause_s: float = 0.25) -> tuple[list[dict], dict]:
    """Decode every order in ``txs``. Rows carry tx, block number and block time."""
    rows: list[dict] = []
    stats = {"txs": len(txs), "decoded": 0, "not_match_orders": 0, "missing": 0}
    for i in range(0, len(txs), batch):
        chunk = txs[i:i + batch]
        got = rpc_batch(url, [("eth_getTransactionByHash", [t]) for t in chunk])
        blocks = sorted({g["blockNumber"] for g in got if g})
        bts = dict(zip(blocks, rpc_batch(url, [("eth_getBlockByNumber", [b, False]) for b in blocks])))
        for t, g in zip(chunk, got):
            if not g:
                stats["missing"] += 1
                continue
            orders = decode_match(g["input"], g["to"] or "")
            if orders is None:
                stats["not_match_orders"] += 1
                continue
            stats["decoded"] += 1
            blk = bts.get(g["blockNumber"]) or {}
            ts = int(blk["timestamp"], 16) if blk.get("timestamp") else None
            for o in orders:
                rows.append(o | {"tx": t, "block": int(g["blockNumber"], 16), "block_ts": ts,
                                 "age_s": round(ts - o["created_ms"] / 1000, 3) if ts else None})
        time.sleep(pause_s)
    return rows, stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-ordertape")
    ap.add_argument("--txs", type=Path, required=True, help="file with one tx hash per line")
    ap.add_argument("--rpc", default="https://1rpc.io/matic", help="public Polygon JSON-RPC (no key)")
    ap.add_argument("--out", type=Path, required=True, help="JSONL, one order per line")
    a = ap.parse_args(argv)
    txs = [x.strip() for x in a.txs.read_text().split() if x.strip()]
    rows, stats = fetch_orders(a.rpc, txs)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    with a.out.open("wb") as f:
        for r in rows:
            f.write(orjson.dumps(r) + b"\n")
    print(orjson.dumps(stats | {"orders": len(rows)}).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
