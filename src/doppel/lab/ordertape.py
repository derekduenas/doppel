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

Cohort runs (``--store --wallets``) select the wallets' distinct transaction hashes from the fill
store, newest first, and take the block time from the store (one RPC request per tx). Requests go
through ``Pool``: a few threads over several free public endpoints, each endpoint rate-limited,
backed off with jitter after an error and benched after repeated errors. Output is a directory:
``day=YYYY-MM-DD/part-NNNNNN.parquet`` (day of the block, UTC) plus ``_state/flush-NNNNNN.json``,
one manifest per flush naming its parts and its finished txs. A part counts only once its manifest
exists (both written tmp + rename); parts without one are deleted at the next start, so a crash at
any point costs at most one flush of requests and never duplicates rows. Txs that were ``missing``,
``failed`` or ``undecodable`` are not recorded and are tried again by the next run. There is no
lock: do not run two jobs on one output directory.

Each row carries ``verified`` (its maker and hash match a fill the store reports in that tx; null
when the store has no fill with an order_id for the tx) and ``ts_flag`` (null when the stamp is
plausible, else ``zero`` | ``seconds`` | ``after_block`` | ``old`` | ``overflow``). For selected
transactions the current store re-verifies cached rows without RPC. If a manifest part goes
missing, only its decoded transactions become eligible for another fetch.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
from Crypto.Hash import keccak

from .fillstore import _connect

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
    if off < 0 or off > len(d) - W or off % W:
        raise ValueError("matchOrders: invalid word offset")
    return int.from_bytes(d[off:off + W], "big")


def _order(d: bytes, off: int, exchange: str) -> dict:
    static = d[off:off + ORDER_WORDS * W]
    f = [_u(static, i * W) for i in range(ORDER_WORDS)]
    if f[6] not in (0, 1):
        raise ValueError("matchOrders: invalid order side")
    side = "BUY" if f[6] == 0 else "SELL"
    usdc, shares = (f[4], f[5]) if side == "BUY" else (f[5], f[4])
    return {"order_id": order_hash(static, exchange), "maker": "0x" + static[W + 12:2 * W].hex(),
            "signer": "0x" + static[2 * W + 12:3 * W].hex(), "token_id": str(f[3]), "side": side,
            "size": shares / 1e6, "limit": usdc / shares if shares else None,
            "maker_amount": f[4], "taker_amount": f[5], "sig_type": f[7], "created_ms": f[8],
            "builder": static[10 * W:11 * W].hex(), "salt": str(f[0])}


def _uints(d: bytes, off: int) -> list[int]:
    count = _u(d, off)
    if count > (len(d) - off - W) // W:
        raise ValueError("matchOrders: truncated integer array")
    return [_u(d, off + W + i * W) for i in range(count)]


def decode_match(input_hex: str, exchange: str) -> list[dict] | None:
    """Orders in one ``matchOrders`` call, taker first. None when the call is something else."""
    h = input_hex[2:] if input_hex.startswith("0x") else input_hex
    if h[:8] != SELECTOR or exchange.lower() not in EXCHANGES:
        return None
    d = bytes.fromhex(h[8:])
    taker = _order(d, _u(d, W), exchange)
    mo = _u(d, 2 * W)
    n = _u(d, mo)
    if n > len(d) // W:  # malformed calldata: do not loop over a garbage count
        raise ValueError(f"matchOrders: maker count {n} exceeds calldata")
    makers = [_order(d, mo + W + _u(d, mo + W + i * W), exchange) for i in range(n)]
    mfill, mfee = _uints(d, _u(d, 4 * W)), _uints(d, _u(d, 6 * W))
    if len(mfill) != n or len(mfee) != n:
        return None
    out = [taker | {"role": "taker", "fill_amount": _u(d, 3 * W), "fee_amount": _u(d, 5 * W)}]
    out += [m | {"role": "maker", "fill_amount": mfill[i], "fee_amount": mfee[i]} for i, m in enumerate(makers)]
    cid = d[:W].hex()
    return [o | {"condition_id": cid, "n_makers": n} for o in out]


# ---------------------------------------------------------------- RPC pool
DEFAULT_RPCS = ("https://polygon-bor-rpc.publicnode.com", "https://polygon.drpc.org")


class RpcError(Exception):
    """One failed request; ``kind`` is the label counted per endpoint (http_429, timeout, ...)."""

    def __init__(self, kind: str):
        super().__init__(kind)
        self.kind = kind


class BudgetExhausted(Exception):
    pass


class EndpointsDown(Exception):
    pass


# Capability refusals: the endpoint is healthy but will not serve this request. Measured on Apex
# 2026-11-27: publicnode answers historical eth_call with "Archive requests require a personal
# token"; drpc's free plan limits eth_getLogs to 100-block spans. They re-route, never bench.
ARCHIVE_REFUSED, SPAN_REFUSED = "archive_unsupported", "log_range_limit"
REFUSALS = (ARCHIVE_REFUSED, SPAN_REFUSED)
STATE_METHODS = ("eth_call", "eth_getCode", "eth_getBalance", "eth_getStorageAt")  # archive refusals apply
_ARCHIVE_RE = re.compile(r"archive|missing trie node|historical state|state (is )?not available|pruned")
_SPAN_RE = re.compile(r"block range|range (is )?too (large|wide|big)|ranges? over|exceed\w* .*(range|blocks|results)"
                      r"|too many (blocks|results|logs)|more than \d+ (blocks|results|logs)"
                      r"|limited to \d+ blocks?|max(imum)? (block )?range|\d+ blocks? range|range limit")


def refusal_kind(error: object) -> str:
    """Kind of a JSON-RPC ``error`` object: a capability refusal or a generic ``rpc_error``.
    Only the kind is kept; the provider's message is never logged."""
    msg = str(error.get("message", "") if isinstance(error, dict) else error or "").lower()
    if _ARCHIVE_RE.search(msg):
        return ARCHIVE_REFUSED
    if _SPAN_RE.search(msg):
        return SPAN_REFUSED
    return "rpc_error"


def log_span(params: list) -> int | None:
    """Block span of an ``eth_getLogs`` filter with numeric bounds, else None."""
    try:
        f = params[0]
        return int(f["toBlock"], 16) - int(f["fromBlock"], 16) + 1
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def http_transport(url: str, method: str, params: list, timeout: float = 20.0):
    """One JSON-RPC call per HTTP request (free public endpoints reject batch arrays with 403).
    Returns ``result`` (None when the node does not have the object); raises RpcError otherwise.
    A JSON-RPC ``error`` body counts as a failure: public nodes also rate-limit with HTTP 200.
    A capability refusal, in a 200 body or a 4xx body, raises its own kind (``refusal_kind``)."""
    req = urllib.request.Request(
        url, json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
        {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"})
    try:
        body = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        kind = f"http_{e.code}"
        if 400 <= e.code < 500 and e.code != 429:
            try:
                err = json.loads(e.read())
                refused = refusal_kind(err.get("error") if isinstance(err, dict) else None)
                kind = refused if refused in REFUSALS else kind
            except (OSError, ValueError):
                pass
        raise RpcError(kind) from None
    except TimeoutError:
        raise RpcError("timeout") from None
    except urllib.error.URLError as e:
        raise RpcError("timeout" if isinstance(e.reason, TimeoutError) else "network") from None
    except (OSError, ValueError):
        raise RpcError("bad_response") from None
    if not isinstance(body, dict) or "result" not in body:
        raise RpcError(refusal_kind(body.get("error")) if isinstance(body, dict) else "rpc_error")
    return body["result"]


class _Endpoint:
    def __init__(self, url: str):
        self.url = url
        self.next_t = 0.0  # earliest time of its next request (rate limit and backoff)
        self.bench_until = 0.0
        self.fails = 0  # consecutive failures since the last success or bench
        self.benches = 0  # consecutive benches without a success in between
        self.requests = 0
        self.benched = 0
        self.errors: Counter[str] = Counter()
        self.refused: set[str] = set()  # methods refused as a capability (archive state)
        self.max_span: int | None = None  # largest eth_getLogs span it may still be sent

    def serves(self, method: str, span: int | None) -> bool:
        return method not in self.refused and not (
            method == "eth_getLogs" and span is not None and self.max_span is not None and span > self.max_span)


class Pool:
    """Polite access to several endpoints from several threads.

    Each request goes to the endpoint that is free soonest. An endpoint sends at most ``rate``
    requests per second. After an error its next request waits ``backoff_s * 2**(fails-1)``
    (capped at ``backoff_cap_s``) times a jitter in [0.5, 1.5); after ``bench_after`` consecutive
    errors it is benched for ``bench_s``, doubling each time it is benched again without a
    success. A call tries up to ``attempts`` requests, then raises the last RpcError.
    ``max_requests`` counts every HTTP request, retries included; past it calls raise
    BudgetExhausted. When every endpoint has been benched ``give_up`` times in a row, calls raise
    EndpointsDown rather than keep knocking."""

    def __init__(self, urls: list[str], transport: Callable = http_transport, *, rate: float = 5.0,
                 max_requests: int | None = None, attempts: int = 5, backoff_s: float = 0.5,
                 backoff_cap_s: float = 30.0, bench_after: int = 3, bench_s: float = 60.0,
                 give_up: int = 3, now: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 rng: Callable[[], float] = random.random):
        self.eps = [_Endpoint(u) for u in dict.fromkeys(urls)]
        self.transport, self.rate, self.max_requests, self.attempts = transport, rate, max_requests, attempts
        self.backoff_s, self.backoff_cap_s = backoff_s, backoff_cap_s
        self.bench_after, self.bench_s, self.give_up = bench_after, bench_s, give_up
        self.now, self.sleep, self.rng = now, sleep, rng
        self.requests = 0
        self._lock = threading.Lock()

    def _reserve(self, avoid: str | None, allowed: frozenset[str] | None) -> _Endpoint:
        with self._lock:
            if self.max_requests is not None and self.requests >= self.max_requests:
                raise BudgetExhausted
            eligible = self.eps if allowed is None else [e for e in self.eps if e.url in allowed]
            if not eligible:
                raise ValueError("no configured RPC endpoint for this method")
            if all(e.benches >= self.give_up for e in eligible):
                raise EndpointsDown
            e = min([e for e in eligible if e.url != avoid] or eligible,
                    key=lambda e: max(e.next_t, e.bench_until))
            t = max(self.now(), e.next_t, e.bench_until)
            e.next_t = t + 1.0 / self.rate
            e.requests += 1
            self.requests += 1
        wait = t - self.now()
        if wait > 0:
            self.sleep(wait)
        return e

    def call(self, method: str, params: list, avoid: str | None = None, *,
             allowed: frozenset[str] | None = None) -> tuple[object, str]:
        """(result, url of the endpoint that answered). ``avoid`` skips one endpoint if another exists.

        A capability refusal (``REFUSALS``) neither backs off nor benches the endpoint and uses no
        attempt: that endpoint is no longer offered this method (archive) or this log span or wider
        (span), and the call moves to another one. With no capable endpoint left it raises the
        refusal, so a caller can narrow the request (``absorb.scan`` halves its span)."""
        span = log_span(params) if method == "eth_getLogs" else None
        last = RpcError("no_attempt")
        tries = 0
        while tries < self.attempts:
            with self._lock:
                routed = [e for e in self.eps if allowed is None or e.url in allowed]
                capable = frozenset(e.url for e in routed if e.serves(method, span))
            if routed and not capable:  # every routed endpoint refused this capability
                raise RpcError(SPAN_REFUSED if method == "eth_getLogs" and method not in
                               set().union(*(e.refused for e in routed)) else ARCHIVE_REFUSED)
            e = self._reserve(avoid, capable or allowed)
            try:
                res = self.transport(e.url, method, params)
            except Exception as x:  # anything from the transport is that endpoint's failure
                last = x if isinstance(x, RpcError) else RpcError(type(x).__name__)
                if (last.kind == SPAN_REFUSED and method != "eth_getLogs"
                        or last.kind == ARCHIVE_REFUSED and method not in STATE_METHODS):
                    last = RpcError("rpc_error")  # a refusal text on another method is just an error
                with self._lock:
                    e.errors[last.kind] += 1
                    if last.kind == SPAN_REFUSED and span is not None:
                        e.max_span = span - 1 if e.max_span is None else min(e.max_span, span - 1)
                        continue
                    if last.kind in REFUSALS:
                        e.refused.add(method)
                        continue
                tries += 1
                with self._lock:
                    e.fails += 1
                    delay = min(self.backoff_cap_s, self.backoff_s * 2 ** (e.fails - 1)) * (0.5 + self.rng())
                    e.next_t = max(e.next_t, self.now() + delay)
                    if e.fails >= self.bench_after:
                        e.bench_until = self.now() + self.bench_s * 2 ** e.benches
                        e.benches += 1
                        e.benched += 1
                        e.fails = 0
                continue
            with self._lock:
                e.fails = e.benches = 0
            return res, e.url
        raise last

    def stats(self) -> dict:
        return {e.url: {"requests": e.requests, "benched": e.benched, "errors": dict(e.errors)}
                for e in self.eps}


# ---------------------------------------------------------------- one tx
MAX_AGE_S = 30 * 86400.0  # a stamp older than this before its block is flagged "old"
_I64 = ("maker_amount", "taker_amount", "fill_amount", "fee_amount", "sig_type", "created_ms", "n_makers")
_STR, _INT, _F64 = pa.string(), pa.int64(), pa.float64()
SCHEMA = pa.schema([
    ("tx", _STR), ("block", _INT), ("block_ts", _INT), ("role", _STR), ("order_id", _STR),
    ("maker", _STR), ("signer", _STR), ("token_id", _STR), ("side", _STR), ("size", _F64),
    ("limit", _F64), ("maker_amount", _INT), ("taker_amount", _INT), ("fill_amount", _INT),
    ("fee_amount", _INT), ("sig_type", _INT), ("created_ms", _INT), ("age_s", _F64),
    ("ts_flag", _STR), ("verified", pa.bool_()), ("builder", _STR), ("salt", _STR),
    ("condition_id", _STR), ("n_makers", _INT)])


def ts_flag(created_ms: int | None, block_ts: int, max_age_s: float = MAX_AGE_S) -> str | None:
    """Screen a self-reported creation stamp against its block time. None means plausible."""
    if created_ms is None:
        return "overflow"  # did not fit int64
    if created_ms == 0:
        return "zero"
    if created_ms < 10**11:  # as ms this is before 1973: the signer stamped seconds
        return "seconds"
    age = block_ts - created_ms / 1000
    if age < 0:
        return "after_block"
    return "old" if age > max_age_s else None


def _row(o: dict, tx: str, block: int, ts: int, max_age_s: float) -> dict:
    r = o | {k: o[k] if o[k] < 2**63 else None for k in _I64}  # signer-controlled uint256
    c = r["created_ms"]
    return r | {"tx": tx, "block": block, "block_ts": ts, "verified": None,
                "age_s": None if c is None else round(ts - c / 1000, 3),
                "ts_flag": ts_flag(c, ts, max_age_s)}


def fetch_tx(pool: Pool, item: tuple[str, int | None, int | None], blocks: dict[int, int],
             max_age_s: float = MAX_AGE_S) -> dict:
    """Fetch and decode one tx: ``item`` is (tx, block time, block number), the last two from the
    fill store or None. The block is fetched only when the store gave no time or names another
    block than the chain does. Returns {"tx", "status", "rows"}; never raises on RPC trouble."""
    tx, ts, bn = item

    def out(status: str, rows: list[dict] | None = None) -> dict:
        return {"tx": tx, "status": status, "rows": rows or []}

    try:
        g, url = pool.call("eth_getTransactionByHash", [tx])
        if g is None and len(pool.eps) > 1:  # one node may lack it: ask one other
            g, _ = pool.call("eth_getTransactionByHash", [tx], avoid=url)
        if not isinstance(g, dict) or not g.get("blockNumber"):
            return out("missing")
        try:
            orders = decode_match(g["input"], g.get("to") or "")
        except Exception:
            return out("undecodable")
        if orders is None:
            return out("not_match_orders")
        block = int(g["blockNumber"], 16)
        if ts is None or bn != block:
            ts = blocks.get(block)
            if ts is None:
                b, _ = pool.call("eth_getBlockByNumber", [g["blockNumber"], False])
                if not isinstance(b, dict) or not b.get("timestamp"):
                    return out("missing")
                ts = blocks[block] = int(b["timestamp"], 16)
    except BudgetExhausted:
        return out("budget")
    except EndpointsDown:
        return out("down")
    except RpcError:
        return out("failed")
    return out("decoded", [_row(o, tx, block, ts, max_age_s) for o in orders])


# ---------------------------------------------------------------- fill store
def _hex(s: str) -> str:
    return s.lower().removeprefix("0x")


def select_txs(con, wallets: list[str], t_from: int | None = None, t_to: int | None = None,
               max_per_wallet: int | None = None) -> list[tuple[str, int | None, int | None]]:
    """Distinct (tx, block_timestamp, block_number) of the wallets' fills with
    ``t_from <= block_timestamp < t_to``, newest first. ``max_per_wallet`` keeps each wallet's
    most recent txs. ``con`` is a fillstore connection (``fills`` view)."""
    q = ("SELECT address, transaction_hash AS tx, max(block_timestamp) AS ts, max(block_number) AS bn "
         "FROM fills WHERE transaction_hash IS NOT NULL AND address IN (SELECT unnest(?::VARCHAR[]))")
    args: list = [sorted(set(wallets))]
    for op, v in ((">=", t_from), ("<", t_to)):
        if v is not None:
            q += f" AND block_timestamp {op} ?"
            args.append(v)
    q += " GROUP BY address, transaction_hash"
    if max_per_wallet is not None:
        q = (f"SELECT * FROM ({q}) QUALIFY row_number() OVER "
             "(PARTITION BY address ORDER BY ts DESC, tx) <= ?")
        args.append(max_per_wallet)
    q = f"SELECT tx, max(ts), max(bn) FROM ({q}) GROUP BY tx ORDER BY 2 DESC NULLS LAST, 1"
    return con.execute(q, args).fetchall()


def goldsky_ids(con, wallets: list[str] | None, txs: list[str]) -> dict[str, set[tuple[str, str]]]:
    """tx -> {(address, order_id)} the fill store reports (all wallets when ``wallets`` is None)."""
    q = ("SELECT DISTINCT transaction_hash, address, order_id FROM fills WHERE order_id IS NOT NULL "
         "AND transaction_hash IN (SELECT unnest(?::VARCHAR[]))")
    args: list = [txs]
    if wallets is not None:
        q += " AND address IN (SELECT unnest(?::VARCHAR[]))"
        args.append(sorted(set(wallets)))
    out: dict[str, set[tuple[str, str]]] = {}
    for tx, a, oid in con.execute(q, args).fetchall():
        out.setdefault(tx, set()).add((a, _hex(oid)))
    return out


# ---------------------------------------------------------------- output and resume
STATE_DIR = "_state"
DONE = ("decoded", "not_match_orders")  # recorded; other statuses are retried by the next run


def _manifests(out: Path):
    for path in sorted((out / STATE_DIR).glob("flush-*.json")):
        yield int(path.stem.split("-")[1]), orjson.loads(path.read_bytes())


def committed_parts(out: Path, *, allow_legacy: bool = False) -> list[Path]:
    """Read a snapshot of published parts without running writer recovery.

    Standalone readers can opt in to legacy parquet exports without a state directory.
    A managed directory with zero published flushes has zero readable parts.
    """
    if not (out / STATE_DIR).is_dir():
        legacy = sorted(out.glob("day=*/part-*.parquet"))
        if legacy and not allow_legacy:
            raise ValueError(f"order tape has parts but no flush manifests: {out}")
        return legacy
    parts: set[Path] = set()
    for _, rec in _manifests(out):
        for name in rec["parts"]:
            rel = Path(name)
            if len(rel.parts) != 2 or not rel.match("day=*/part-*.parquet"):
                raise ValueError(f"invalid order tape part in flush manifest: {name}")
            part = out / rel
            if part in parts or not part.is_file():
                raise ValueError(f"duplicate or missing committed order tape part: {part}")
            parts.add(part)
    return sorted(parts)


def has_missing_parts(out: Path) -> bool:
    return any(not (out / part).is_file() for _, rec in _manifests(out) for part in rec["parts"])


def _load_flushes(out: Path) -> tuple[list[dict], int]:
    """Keep commits for surviving parts and remove uncommitted parts left by a crash."""
    flushes = []
    parts: set[str] = set()
    seq = 1
    for number, rec in _manifests(out):
        present = [part for part in rec["parts"] if (out / part).is_file()]
        if len(present) != len(rec["parts"]):
            surviving = {tx for part in present
                         for tx in pq.ParquetFile(out / part).read(columns=["tx"])["tx"].to_pylist()}
            rec = {"parts": present, "txs": {tx: status for tx, status in rec["txs"].items()
                                            if status != "decoded" or tx in surviving}}
            _commit(out, number, rec["parts"], rec["txs"])
        flushes.append(rec)
        parts.update(rec["parts"])
        seq = max(seq, number + 1)
    for p in [*out.glob("day=*/part-*"), *(out / STATE_DIR).glob("*.tmp")]:
        if p.relative_to(out).as_posix() not in parts:
            p.unlink()
    return flushes, seq


def load_done(out: Path) -> tuple[set[str], int]:
    """Return the finished transactions and the next flush number."""
    flushes, seq = _load_flushes(out)
    return {tx for rec in flushes for tx in rec["txs"]}, seq


def _write_part(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(pa.Table.from_pylist(rows, schema=SCHEMA), tmp, compression="zstd")
    os.replace(tmp, path)


def _write_parts(out: Path, seq: int, rows: list[dict]) -> list[str]:
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(datetime.fromtimestamp(r["block_ts"], UTC).strftime("%Y-%m-%d"), []).append(r)
    parts = []
    for day, rs in sorted(by_day.items()):
        rel = f"day={day}/part-{seq:06d}.parquet"
        _write_part(out / rel, rs)
        parts.append(rel)
    return parts


def _commit(out: Path, seq: int, parts: list[str], txs: dict[str, str]) -> None:
    p = out / STATE_DIR / f"flush-{seq:06d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(orjson.dumps({"parts": parts, "txs": txs}))
    os.replace(tmp, p)


# ---------------------------------------------------------------- run
def _verify(rows: list[dict], want: set[tuple[str, str]],
            per_wallet: dict[str, list[int]]) -> tuple[bool, bool]:
    """Update verification on one transaction. Return (flags changed, no claims matched)."""
    got = {(row["maker"], row["order_id"]) for row in rows}
    changed = False
    for row in rows:
        verified = (row["maker"], row["order_id"]) in want if want else None
        changed |= row["verified"] != verified
        row["verified"] = verified
    for address, oid in want:
        counts = per_wallet.setdefault(address, [0, 0])
        counts[0] += 1
        counts[1] += (address, oid) in got
    return changed, bool(want and not want & got)


def _reverify(out: Path, rec: dict, selected: set[str], expected: Callable,
              per_wallet: dict[str, list[int]]) -> tuple[int, int]:
    """Refresh selected rows in this flush, leaving other transactions untouched."""
    txs = [tx for tx, status in rec["txs"].items() if tx in selected and status == "decoded"]
    if not txs:
        return 0, 0
    claims = expected(txs)
    wanted = set(txs)
    no_match = 0
    for part in rec["parts"]:
        path = out / part
        rows = pq.ParquetFile(path).read().to_pylist()
        by_tx: dict[str, list[dict]] = {}
        for row in rows:
            if row["tx"] in wanted:
                by_tx.setdefault(row["tx"], []).append(row)
        changed = False
        for tx, tx_rows in by_tx.items():
            updated, unmatched = _verify(tx_rows, claims.get(tx, set()), per_wallet)
            changed |= updated
            no_match += unmatched
        if changed:
            _write_part(path, rows)
    return len(txs), no_match


def run(todo: list[tuple[str, int | None, int | None]], pool: Pool, out: Path, *,
        expected: Callable[[list[str]], dict[str, set[tuple[str, str]]]] | None = None,
        workers: int = 4, flush_txs: int = 500, max_age_s: float = MAX_AGE_S) -> dict:
    """Fetch unfinished transactions and refresh verification for both new and cached rows.

    ``expected(txs)`` supplies all current fill-store claims as {(address, order_id)} per tx.
    Without it, cached flags are left alone. Verification counts include selected cached rows.
    RPC, decoded-order, and timestamp-flag counts cover new fetches only.
    """
    t0 = time.monotonic()
    (out / STATE_DIR).mkdir(parents=True, exist_ok=True)
    flushes, seq = _load_flushes(out)
    done = {tx for rec in flushes for tx in rec["txs"]}
    todo = list({t[0]: t for t in todo}.values())
    work = [t for t in todo if t[0] not in done]
    n: Counter[str] = Counter()
    flags: Counter[str] = Counter()
    per_wallet: dict[str, list[int]] = {}  # address -> [fills with an order_id, of which decoded]
    orders = no_match = reverified = 0
    if expected is not None:
        selected = {tx for tx, _, _ in todo}
        for rec in flushes:
            checked, unmatched = _reverify(out, rec, selected, expected, per_wallet)
            reverified += checked
            no_match += unmatched
    stopped = None
    blocks: dict[int, int] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i in range(0, len(work), flush_txs):
            res = list(ex.map(lambda t: fetch_tx(pool, t, blocks, max_age_s), work[i:i + flush_txs]))
            n.update(r["status"] for r in res)
            dec = [r for r in res if r["status"] == "decoded"]
            exp = expected([r["tx"] for r in dec]) if expected and dec else {}
            rows: list[dict] = []
            for r in dec:
                _, unmatched = _verify(r["rows"], exp.get(r["tx"], set()), per_wallet)
                no_match += unmatched
                for o in r["rows"]:
                    if o["ts_flag"]:
                        flags[o["ts_flag"]] += 1
                rows += r["rows"]
            fin = {r["tx"]: r["status"] for r in res if r["status"] in DONE}
            if fin:
                _commit(out, seq, _write_parts(out, seq, rows), fin)
                seq += 1
            orders += len(rows)
            print(f"ordertape: {min(i + flush_txs, len(work))}/{len(work)} txs, {orders} orders, "
                  f"{pool.requests} requests, {time.monotonic() - t0:.0f}s", file=sys.stderr)
            if n["budget"] or n["down"]:
                stopped = "max_requests" if n["budget"] else "endpoints_down"
                break
    attempted = sum(n[s] for s in (*DONE, "undecodable", "missing", "failed"))
    exp_n, got_n = (sum(c[k] for c in per_wallet.values()) for k in (0, 1))
    wall = time.monotonic() - t0
    return {
        "txs_selected": len(todo), "txs_already_done": len(todo) - len(work),
        "txs_reverified": reverified,
        "txs_requested": attempted, "txs_decoded": n["decoded"],
        "txs_not_match_orders": n["not_match_orders"], "txs_undecodable": n["undecodable"],
        "txs_missing": n["missing"], "txs_failed": n["failed"],
        "txs_not_attempted": len(work) - attempted, "stopped": stopped, "orders": orders,
        "verify": {"fills_expected": exp_n, "fills_matched": got_n,
                   "share": got_n / exp_n if exp_n else None, "txs_no_matching_order": no_match,
                   "per_wallet": {a: {"fills_expected": c[0], "fills_matched": c[1], "share": c[1] / c[0]}
                                  for a, c in sorted(per_wallet.items())}},
        "ts_flags": dict(flags), "requests": pool.requests, "wall_s": round(wall, 1),
        "tx_per_s": round(attempted / wall, 2) if wall > 0 else None, "endpoints": pool.stats()}


def _ts(s: str) -> int:
    """Epoch seconds, or an ISO-8601 time (UTC when it has no offset)."""
    if s.isdigit():
        return int(s)
    d = datetime.fromisoformat(s)
    return int((d if d.tzinfo else d.replace(tzinfo=UTC)).timestamp())


def _list(s: str) -> list[str]:
    """A file with one item per line, or a comma-separated list. Lowercased."""
    try:
        text = Path(s).read_text() if Path(s).is_file() else s
    except OSError:  # a long list is not a valid file name
        text = s
    text = text.replace(",", " ")
    return [x.strip().lower() for x in text.split() if x.strip()]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-ordertape")
    ap.add_argument("--txs", help="file with one tx hash per line, or a comma-separated list")
    ap.add_argument("--store", type=Path, help="fill store dir (doppel fillstore): tx selection, "
                    "block times and order_id verification")
    ap.add_argument("--wallets", help="file with one address per line, or a comma-separated list")
    ap.add_argument("--from", dest="t_from", type=_ts, help="epoch s or ISO time, inclusive")
    ap.add_argument("--to", dest="t_to", type=_ts, help="epoch s or ISO time, exclusive")
    ap.add_argument("--max-txs-per-wallet", type=int, help="keep each wallet's most recent N txs")
    ap.add_argument("--rpc", action="append", help="public Polygon JSON-RPC, no key; repeatable "
                    f"(default: {' '.join(DEFAULT_RPCS)})")
    ap.add_argument("--rate", type=float, default=5.0, help="requests per second per endpoint")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--max-requests", type=int, help="stop cleanly after this many HTTP requests")
    ap.add_argument("--max-age-days", type=float, default=MAX_AGE_S / 86400,
                    help="flag stamps older than this before their block")
    ap.add_argument("--flush-txs", type=int, default=500, help="txs per output flush")
    ap.add_argument("--out", type=Path, required=True, help="output directory (parquet by day + _state)")
    a = ap.parse_args(argv)
    if not a.txs and not (a.store and a.wallets):
        ap.error("give --txs, or --store with --wallets")
    wallets = _list(a.wallets) if a.wallets else None
    todo: list[tuple[str, int | None, int | None]] = [(t, None, None) for t in _list(a.txs)] if a.txs else []
    con = _connect(a.store, "256MB", 2)[0] if a.store else None
    try:
        if con is not None and wallets:
            todo += select_txs(con, wallets, a.t_from, a.t_to, a.max_txs_per_wallet)
        pool = Pool(a.rpc or list(DEFAULT_RPCS), rate=a.rate, max_requests=a.max_requests)
        res = run(todo, pool, a.out, workers=a.workers, flush_txs=a.flush_txs,
                  max_age_s=a.max_age_days * 86400,
                  expected=(lambda txs: goldsky_ids(con, None, txs)) if con is not None else None)
    finally:
        if con is not None:
            con.close()
    print(orjson.dumps(res).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
