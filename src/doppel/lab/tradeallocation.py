"""Offline reconciliation of public CLOB prints to complete decoded matchOrders calls.

A maker's signed amounts and maker fill specify the price and quantity resting at that
level. The taker's limit can be improved. It therefore supplies direction, never executed
quantity. A fill-store hash match anchors the call; the store need not contain every maker.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import orjson
import pyarrow.parquet as pq

from .intlbook import Print, px
from .ordertape import committed_parts


@dataclass(frozen=True, slots=True)
class Allocation:
    market: str
    token: str
    mate: str | None
    side: str
    taker_limit: float
    levels: tuple[tuple[float, float], ...]  # price, shares in taker token view, best first
    total: float
    tolerance: float

    def match(self, event: Print, mate: str | None) -> str:
        if self.market != event.market.lower().removeprefix("0x"):
            return "market_mismatch"
        if self.mate is not None and {self.token, self.mate} != {event.asset, mate}:
            return "asset_mismatch"
        if self.token != event.asset and self.token != mate:
            return "asset_mismatch"
        if (self.side == event.side) != (self.token == event.asset):
            return "side_mismatch"
        label = px(event.price if self.token == event.asset else 1 - event.price)
        best, worst = self.levels[0][0], self.levels[-1][0]
        if label != self.taker_limit and not min(best, worst) <= label <= max(best, worst):
            return "price_mismatch"
        if not math.isfinite(event.size) or abs(self.total - event.size) > self.tolerance:
            return "quantity_mismatch"
        return "allocated"


def allocation(rows: list[dict]) -> Allocation | str:
    takers = [r for r in rows if r.get("role") == "taker"]
    makers = [r for r in rows if r.get("role") == "maker"]
    if len(takers) != 1 or not makers or len(rows) != 1 + len(makers):
        return "incomplete_call"
    t = takers[0]
    if any(r.get("n_makers") != len(makers) or not r.get("order_id") for r in rows):
        return "incomplete_call"
    if len({r["order_id"] for r in rows}) != len(rows):
        return "duplicate_order"
    if not any(r.get("verified") is True for r in rows):
        return "unverified_call"
    market = t.get("condition_id")
    if not market or any(r.get("condition_id") != market for r in rows):
        return "market_mismatch"
    token, side = t.get("token_id"), t.get("side")
    others = {r.get("token_id") for r in makers} - {token}
    if not token or None in others or len(others) > 1 or side not in ("BUY", "SELL"):
        return "invalid_call"

    give, take = t.get("maker_amount"), t.get("taker_amount")
    if type(give) is not int or type(take) is not int or min(give, take) <= 0:
        return "invalid_amount"
    limit = px(give / take if side == "BUY" else take / give)
    if not 0 < limit <= 1:
        return "invalid_amount"
    levels: dict[float, float] = defaultdict(float)
    for r in makers:
        other = r["token_id"] != token
        if r.get("side") not in ("BUY", "SELL") or (side == r["side"]) != other:
            return "side_mismatch"
        ma, ta, fill = r.get("maker_amount"), r.get("taker_amount"), r.get("fill_amount")
        if (type(ma) is not int or type(ta) is not int or type(fill) is not int
                or min(ma, ta, fill) <= 0 or fill > ma):
            return "invalid_amount"
        cash, shares = (ma, ta) if r["side"] == "BUY" else (ta, ma)
        price = cash / shares
        if not 0 < price < 1:
            return "invalid_amount"
        # Integer division is the exchange's 6-decimal proportional maker fill.
        quantity = (fill * ta // ma if r["side"] == "BUY" else fill) / 1_000_000
        level = px(1 - price if other else price)
        if (side == "BUY" and level > limit) or (side == "SELL" and level < limit):
            return "price_mismatch"
        levels[level] += quantity
    return Allocation(market.lower().removeprefix("0x"), token, next(iter(others), None), side, limit,
                      tuple(sorted(levels.items(), reverse=side == "SELL")), sum(levels.values()),
                      (len(makers) + 1) / 1_000_000)


COLS = ("tx", "role", "condition_id", "n_makers", "order_id", "token_id", "side", "maker_amount",
        "taker_amount", "fill_amount", "verified")


def load_allocations(path: Path, wanted: set[str] | None = None) -> dict[str, Allocation | str]:
    """Keep one compact split per transaction. A committed ordertape part contains whole calls."""
    index: dict[str, Allocation | str] = {}

    def consume(rows):
        by_tx = defaultdict(list)
        for r in rows:
            if r.get("tx") and (wanted is None or r["tx"] in wanted):
                by_tx[r["tx"]].append(r)
        for tx, call in by_tx.items():
            index[tx] = allocation(call) if tx not in index else "duplicate_call"

    if path.is_dir():
        for p in committed_parts(path):
            pf = pq.ParquetFile(p)
            consume(pf.read(columns=[c for c in COLS if c in pf.schema_arrow.names]).to_pylist())
    else:
        with path.open("rb") as fh:
            consume(orjson.loads(line) for line in fh if line.strip())
    return index
