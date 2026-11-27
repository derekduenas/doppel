"""Reconstruction pipeline: one resumable run from a frozen cohort and a time range to, per wallet,
the verified order tape, the book match, the twin replay and the ledger card, with a manifest
that traces every number. Plain-language description: ``docs/RECONSTRUCTION.md``.

STEPS (run in process, in this order; each can be left out with ``--steps``)
  ordertape    ``ordertape.run``: the cohort's txs in [--from, --to) from the fill store, decoded
               from chain calldata into ``<orders>`` (default ``<out>/ordertape``). It resumes per
               tx by itself. It is complete only when every selected tx has decoded rows on
               the tape (checked on disk, not taken from the call's summary). Until then it is
               called again by every run (which retries the failed, missing and undecodable
               txs) and every wallet carries ``ordertape:incomplete``.
  ordermatch   ``ordermatch.run`` once per unit (a chunk x a wallet batch), then a merge.
  orderreplay  ``orderreplay.run`` once per unit on that unit's ordermatch output, then a merge.
  profile      ``profile.run`` on the parquet directory and the merged ``ordermatch.json``
               (wallets in batches of ``PROFILE_BATCH``; the cohort table is rebuilt from all
               ledgers). It has no time range: it reads every verified row of the wallet in
               ``<orders>``, which is the run's range unless ``--orders`` points at a shared tape.

RESUME. A unit of work (the ordertape call, one unit of ordermatch or orderreplay, a merge,
profile) is skipped when its recorded input key is unchanged and its outputs are still there with
the recorded size. The key is a sha256 over: the sha256 of the code files the unit runs (this
file included), the constants and options that change outputs, its arguments, and its inputs'
sha256 (fill store state file; order tape parts; tape segments, taken from the stream's
``manifest.jsonl`` when the line matches the file size, else computed; upstream outputs).
``--force STEP`` reruns that step's units. Forcing ``ordertape`` refreshes verification for cached
transactions and fetches any missing transactions. Unit outputs and per-wallet files are
written to a temporary name and renamed. A kept output whose key no longer matches, and chunk
outputs without a merge, are ``stale`` and untrusted.

MEMORY. ordermatch holds every level event of the batch's markets for the chunk; that is the
large object (an unchunked 12-wallet hour used 5.8 GB on Apex, 2026-11-26). The controls:
- ``--chunk-minutes`` / ``--chunk-hours``: the range is cut at multiples of the chunk length and
  each chunk is run alone on [chunk from, chunk to): orders created in the chunk, tape records
  received in the chunk. Chunks do not overlap, so nothing is counted twice.
- ``--wallet-batch N``: inside a chunk, ordermatch and orderreplay run for at most N wallets at
  a time over the same tape window (``focus``: level events are kept for those wallets' markets
  only). Every verified order is still loaded in each batch, so a book event serves one order and
  another maker's match is a proven false positive. A cancel (exact decrease) is claimed
  by one candidate within a batch, but two wallets in different batches can both claim it.
- Fingerprint candidates are never held as rows: ordermatch hands each one to a writer
  (``quotes.jsonl``, one line at a time) and only counts and an integer-ms life histogram stay in
  memory; orderreplay reads that file line by line into one small tuple per cancelled candidate
  and reduces each finished candidate to counts at once.
- ``--mem-budget-mb``: after every unit the process's peak RSS (``resource.getrusage``; a
  high-water mark, it never goes down) is written to the unit's record. Above the budget the
  run stops cleanly: finished units stay, no further unit, merge or profile runs, the manifest
  says ``stopped`` and every wallet carries ``<step>:mem_budget_stop``. Resume with a shorter
  chunk or a smaller batch (both change the run id unless ``--out`` is given).

WHAT A CHUNK EDGE COSTS (visible per unit in the manifest)
- The book is unknown at the start of a chunk until each asset's next snapshot; nothing is
  warmed up from the previous chunk. Short chunks pay this more often.
- An order still resting at the end of its chunk is ``unknown:censored``.
- An order created in the last ~62.5 s of a chunk is not ``observable`` (its placebo window
  crosses the edge).
- An order whose chain fills are not all located by a print inside its chunk, or whose end or
  any fill is at or after the chunk end, cannot be replayed on a tape cut at the edge: its
  replay row gets ``cut = "chunk_end"`` and is kept out of the headline set (it would read as
  a twin miss).
- A known span is [first book, last frame] inside one replay, so a quiet stretch across a chunk
  edge is not counted in token-hours.
- Chunks without a verified maker order created in them are not run (``empty``).

FINGERPRINT FLOOR. A wallet with fewer than ``MIN_FP_ORDERS`` maker orders in a chunk gets no
size fingerprint there (ordermatch's ``min_fp_orders``; ``attribute`` is not run for it):
a fingerprint from a handful of orders is noise. ``fingerprint_dropped`` says so. A wallet that
never reaches the floor in one chunk has no footprint; a longer chunk gives it one.

IDENTITY PRECISION is not measured here. The order tape selects transactions from the cohort's
fills and verifies whichever claims the store has for those transactions. ``other_known`` covers
only observed makers in this partial sample. ``identity_precision_upper`` is written as null in
every ordermatch document this pipeline writes (and so in the ledgers and the report).

MERGE (per wallet; sums of counts, never averages of rates)
- ordermatch: counts are summed over chunks (maker_orders, observable, matched, placebo_matched,
  outcomes, fills, the fingerprint's own / other_known / candidates / own_unmatched / control,
  token-hours). Rates are recomputed from the sums: match_rate = sum(matched) / sum(observable),
  control rate = sum(control) / sum(control token-hours), expected_by_chance = that rate x
  sum(active token-hours), net_estimate = max(0, candidates - own_unmatched - expected).
  Quantiles are recomputed from the pooled values: order rows for sign-to-book and resting
  times, an integer-ms histogram for the fingerprint candidates' lives. ``match_rate_by_side``
  is not merged (the chunk documents hold no per-side denominators): null.
  Fingerprint sizes are derived inside each chunk from that chunk's orders; ``sizes_by_chunk``
  and ``sizes_stable`` say whether they agree. When they do not, the pooled counts mix
  different size sets.
- orderreplay: a wallet's order rows from all chunks are pooled and ``orderreplay.summarize``
  is run on them, so ratios, the market-bootstrap CI, the adapter gate and bracket violations
  are computed exactly as the tool computes them, on the union. Never-filled candidate counts
  are summed. The cohort document is the sum of the wallet documents' counts, and its CI
  resamples those same wallets' pooled markets (one population for ratio and CI).

GATES. ``GATES`` below is the one table of thresholds. All are PROVISIONAL. A gate is ``pass``,
``fail`` or ``not_evaluable`` (fewer than ``min_n`` cases, or the step did not run); a negative
value is ``fail`` (it means the inputs disagree). A wallet's output of a step is trusted only
when every gate of that step and of the steps it reads from is ``pass`` and none of those steps
is stale, incomplete or stopped; otherwise the manifest and the report name the reasons under
``untrusted``. The run never stops on a gate.

LOCKS. The run directory and, while the ordertape step fetches, the order tape directory are
held with an exclusive ``flock`` on ``<dir>/.lock``; a second run on either fails at once.
``doppel-ordertape`` itself takes no lock: do not run it on a directory this pipeline is using.

Analysis only: nothing here is a decision input. No orders, no credentials; the only network
use is ordertape's public JSON-RPC reads.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import resource
import subprocess
import sys
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import orjson
import pyarrow.parquet as pq

from ..tape.segment import segment_path
from . import ordermatch, orderreplay, ordertape, profile
from .fillstore import STATE, _connect
from .flow import _ts

STEPS = ("ordertape", "ordermatch", "orderreplay", "profile")
UPSTREAM = {"ordertape": (), "ordermatch": ("ordertape",),
            "orderreplay": ("ordertape", "ordermatch"), "profile": ("ordertape", "ordermatch")}
PROFILE_BATCH = 20  # wallets per profile.run call (it holds its wallets' rows in memory)
MIN_FP_ORDERS = 30  # maker orders a wallet needs in a chunk for a size fingerprint (ordermatch's --min-orders)
MAX_PENDING = 8  # --last-hours: ranges kept for retry
MAX_TRIES = 3  # --last-hours: firings a range gets before it is given up
NS = 1_000_000_000
MODES = orderreplay.MODES
NO_PRECISION = ("not measured: the order tape selects transactions from cohort fills, so other_known "
                "sees only the other makers verified in those transactions")

# PROVISIONAL gate thresholds. One row per gate: the step it belongs to, "min" or "max" for the
# value, "min_n" cases needed to evaluate it, and what the number rests on. The ordermatch rows
# reuse profile.T so "stamps validated" has one definition. No orderreplay number has been
# measured on real tape yet (no FINDINGS row): those four are placeholders to be set from the
# first real run.
GATES: dict[str, dict] = {
    "ordertape.verified_share": {
        "step": "ordertape", "min": 0.99, "min_n": 1,
        "basis": "E36: 1,534 of 1,534 fills had a decoded order with the identical order_id"},
    "ordermatch.match_rate": {
        "step": "ordermatch", "min": profile.T["match_min"], "min_n": profile.T["min_observable"],
        "basis": "E37 0.93, E39 0.98 (the same wallet-hour, two methods); profile.T match_min / min_observable"},
    "ordermatch.placebo_rate": {
        "step": "ordermatch", "max": profile.T["placebo_max"], "min_n": profile.T["min_observable"],
        "basis": "E37 / E39 0.046 (the same wallet-hour); profile.T placebo_max"},
    "orderreplay.routed_share": {
        "step": "orderreplay", "min": 0.95, "min_n": 30,
        "basis": "no FINDINGS row yet; synthetic tests give 1.0"},
    "orderreplay.delivered_share": {
        "step": "orderreplay", "min": 0.95, "min_n": 30,
        "basis": "no FINDINGS row yet; synthetic tests give 1.0"},
    "orderreplay.placement_not_found_share": {
        "step": "orderreplay", "max": 0.01, "min_n": 30,
        "basis": "no FINDINGS row yet; 0 expected (same tape, same chunk as ordermatch)"},
    "orderreplay.bracket_violation_share": {
        "step": "orderreplay", "max": 0.05, "min_n": 30,
        "basis": "no FINDINGS row yet; 0 under FIFO with truthful prints"},
}

# code files each step runs (relative to the package); their sha256 is part of the step's key
_ME = "lab/reconstruct.py"
CODE = {
    "ordertape": (_ME, "lab/ordertape.py", "lab/fillstore.py"),
    "ordermatch": (_ME, "lab/ordermatch.py", "lab/intlbook.py", "tape/segment.py"),
    "orderreplay": (_ME, "lab/orderreplay.py", "lab/ordermatch.py", "lab/intlbook.py", "tape/segment.py",
                    "twin/engine.py", "twin/tape.py", "twin/features.py", "strategy/genome.py"),
    "profile": (_ME, "lab/profile.py", "lab/ordermatch.py", "lab/ordertape.py"),
}
PKG = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Cfg:
    data: Path  # tape root
    store: Path  # fill store
    cohort: Path
    t_from: int
    t_to: int
    out: Path
    orders: Path  # ordertape parquet directory
    wallets: tuple[str, ...]
    roles: dict[str, str] = field(default_factory=dict)  # wallet -> cand | ctrl
    dossiers: Path | None = None
    max_txs: int | None = None
    chunk_s: int = 3600
    wallet_batch: int = 10
    size_tol_rel: float = 0.0  # ordermatch Params.size_tol_rel (E39's numbers used 0.02)
    mem_budget_mb: float | None = None
    steps: tuple[str, ...] = STEPS
    force: frozenset[str] = frozenset()
    rpcs: tuple[str, ...] = ordertape.DEFAULT_RPCS
    rate: float = 5.0
    max_requests: int | None = None


class Locked(RuntimeError):
    pass


@dataclass
class _Ctx:
    c: Cfg
    code: dict[str, str]
    streams: list[str]
    tcache: dict = field(default_factory=dict)
    halt: str | None = None  # set when the memory budget is exceeded: no further work
    halt_step: str | None = None

    def on(self, step: str) -> bool:
        return step in self.c.steps and self.halt is None

    def base(self, step: str) -> dict:
        """The part of every key of ``step``: its code and the constants that change outputs."""
        return {"code": {f: self.code[f] for f in CODE[step]},
                "consts": {"MIN_FP_ORDERS": MIN_FP_ORDERS, "size_tol_rel": self.c.size_tol_rel}}


# ---------------------------------------------------------------- hashing and files
def sha_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def sha_obj(o) -> str:
    return hashlib.sha256(orjson.dumps(o, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _load(p: Path):
    return orjson.loads(p.read_bytes()) if p.is_file() else None


def _write(p: Path, data: bytes) -> dict:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def _write_json(p: Path, doc) -> dict:
    return _write(p, orjson.dumps(doc, option=orjson.OPT_INDENT_2))


class Jsonl:
    """A JSONL file written one row at a time to ``<name>.tmp`` and renamed by ``close``, which
    returns {sha256, bytes, rows}. Nothing but the current row is held."""

    def __init__(self, p: Path):
        p.parent.mkdir(parents=True, exist_ok=True)
        self.p, self.tmp = p, p.with_name(p.name + ".tmp")
        self.fh = open(self.tmp, "wb")
        self.h = hashlib.sha256()
        self.rows = self.bytes = 0

    def write(self, row: dict) -> None:
        b = orjson.dumps(row) + b"\n"
        self.fh.write(b)
        self.h.update(b)
        self.rows += 1
        self.bytes += len(b)

    def close(self) -> dict:
        self.fh.close()
        os.replace(self.tmp, self.p)
        return {"sha256": self.h.hexdigest(), "bytes": self.bytes, "rows": self.rows}


def _jsonl(p: Path) -> Iterator[dict]:
    with open(p, "rb") as fh:
        for line in fh:
            if line.strip():
                yield orjson.loads(line)


def _fresh(state: dict | None, key: str, base: Path) -> bool:
    """The unit ran with this key and its outputs are still there with the recorded size."""
    return bool(state and state["key"] == key and all(
        (base / name).is_file() and (base / name).stat().st_size == o["bytes"]
        for name, o in state["outputs"].items()))


@contextmanager
def _lock(d: Path):
    """Exclusive, non-blocking lock on ``d/.lock`` for the life of the block."""
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Locked(f"{d} is in use by another doppel-reconstruct (lock {d / '.lock'})") from None
        yield


def peak_rss_mb() -> float:
    """Peak resident set of this process so far, MB (ru_maxrss is bytes on macOS, KB on Linux)."""
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / (1 << 20) if sys.platform == "darwin" else r / 1024, 1)


def code_sha() -> dict[str, str]:
    return {f: sha_file(PKG / f) for f in sorted({f for fs in CODE.values() for f in fs})}


def git_commit() -> tuple[str | None, str]:
    """(commit, where it was read). A deployed tree carries DEPLOYED_COMMIT (``ops/deploy.sh``)."""
    for d in Path(__file__).resolve().parents:
        if (d / "DEPLOYED_COMMIT").is_file():
            return (d / "DEPLOYED_COMMIT").read_text().strip(), "DEPLOYED_COMMIT"
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=PKG, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None, "unknown"
    return (r.stdout.strip(), "git rev-parse") if r.returncode == 0 else (None, "unknown")


def tape_hashes(root: Path, streams: list[str], t_from: int, t_to: int, cache: dict) -> dict[str, str | None]:
    """stream/day/hour segment -> sha256 for the hours of [t_from, t_to); None when missing. The
    sha comes from the stream's manifest line when it matches the file size (closed segment)."""
    out: dict[str, str | None] = {}
    for s in streams:
        if s not in cache:
            man = root / s / "manifest.jsonl"
            cache[s] = {m["file"]: (m["sha256"], m["bytes"]) for m in _jsonl(man)} if man.is_file() else {}
        for hour in range(t_from - t_from % 3600, t_to, 3600):
            p = segment_path(root, s, hour * NS)
            rel = f"{p.parent.name}/{p.name}"
            if not p.exists():
                out[f"{s}/{rel}"] = None
                continue
            m = cache[s].get(rel)
            if not m or m[1] != p.stat().st_size:
                m = cache[s][rel] = (sha_file(p), p.stat().st_size)
            out[f"{s}/{rel}"] = m[0]
    return out


def orders_digest(orders: Path, cache: dict) -> dict:
    """Hash the part contents, reusing hashes until a file is changed or replaced."""
    parts = {}
    for p in sorted(orders.glob("day=*/part-*.parquet")):
        rel = p.relative_to(orders).as_posix()
        c = cache.get(rel)
        stat = p.stat()
        if not c or c[0] != stat.st_size or c[3:] != [stat.st_ino, stat.st_mtime_ns]:
            c = [stat.st_size, sha_file(p), pq.read_metadata(p).num_rows, stat.st_ino, stat.st_mtime_ns]
        parts[rel] = c
    cache.clear()
    cache.update(parts)
    return {"path": str(orders), "sha256": sha_obj({k: v[1] for k, v in parts.items()}),
            "parts": len(parts), "rows": sum(v[2] for v in parts.values())}


def chunk_list(t_from: int, t_to: int, step: int) -> list[tuple[str, int, int]]:
    """(id, from, to) cut at multiples of ``step`` seconds; the id is the chunk's UTC start."""
    out, a = [], t_from
    while a < t_to:
        b = min(t_to, a - a % step + step)
        out.append((time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(a)), a, b))
        a = b
    return out


def busy_chunks(orders: Path, chunks: list[tuple[str, int, int]]) -> dict[str, Counter]:
    """chunk id -> maker -> verified maker rows created inside the chunk."""
    n: dict[str, Counter] = defaultdict(Counter)
    if not chunks:
        return n
    starts = [a * 1000 for _, a, _ in chunks]
    end = chunks[-1][2] * 1000
    for p in sorted(orders.glob("day=*/part-*.parquet")):
        t = pq.read_table(p, columns=["role", "created_ms", "verified", "maker"]).to_pydict()
        for role, c, v, mk in zip(t["role"], t["created_ms"], t["verified"], t["maker"]):
            if role == "maker" and v is True and c is not None and starts[0] <= c < end:
                n[chunks[bisect_right(starts, c) - 1][0]][mk.lower()] += 1
    return n


def unit_list(c: Cfg, chunks: list[tuple[str, int, int]], busy: dict[str, Counter]) -> list[dict]:
    """One unit per chunk and wallet batch; ``orders_in`` = the batch's maker rows in the chunk."""
    bs = [c.wallets[i:i + c.wallet_batch] for i in range(0, len(c.wallets), c.wallet_batch)]
    return [{"chunk": cid, "batch": f"b{k:02d}", "from": a, "to": b, "wallets": ws,
             "orders_in": sum(busy[cid][w] for w in ws) if cid in busy else 0}
            for cid, a, b in chunks for k, ws in enumerate(bs)]


# ---------------------------------------------------------------- step: ordertape
def verified_shares(c: Cfg) -> dict[str, dict]:
    """Per wallet, from the fill store's distinct (tx, order_id) fills in [from, to):
    ``fills_in_range``; ``fills_expected`` = those whose tx is in the order tape; ``fills_matched``
    = those with a row of that id marked ``verified`` in the tape; ``share`` = matched / expected
    (the gate); ``coverage`` = expected / in range. Computed from the two stores, so it does not
    depend on how many ordertape calls built the tape. A matching row must carry the wallet's
    address and be marked verified."""
    parts = [str(p) for p in sorted(c.orders.glob("day=*/part-*.parquet"))]
    if not parts or not (c.store / STATE).is_file():
        return {}
    con = _connect(c.store, "512MB", 2)[0]
    if con is None:
        return {}
    try:
        con.read_parquet(parts).create_view("ot")
        q = ("WITH f AS (SELECT DISTINCT address, transaction_hash AS tx, "
             "regexp_replace(lower(order_id), '^0x', '') AS oid FROM fills WHERE order_id IS NOT NULL "
             "AND address IN (SELECT unnest(?::VARCHAR[])) AND block_timestamp >= ? AND block_timestamp < ?), "
             "t AS (SELECT DISTINCT tx FROM ot), "
             "d AS (SELECT DISTINCT tx, maker, order_id FROM ot WHERE verified IS TRUE) "
             "SELECT f.address, count(*), count(t.tx), count(d.order_id) FROM f "
             "LEFT JOIN t ON t.tx = f.tx LEFT JOIN d ON d.tx = f.tx AND d.maker = f.address AND d.order_id = f.oid "
             "GROUP BY 1 ORDER BY 1")
        return {a: {"fills_in_range": n, "fills_expected": e, "fills_matched": m,
                    "share": round(m / e, 6) if e else None, "coverage": round(e / n, 6)}
                for a, n, e, m in con.execute(q, [sorted(c.wallets), c.t_from, c.t_to]).fetchall()}
    finally:
        con.close()


def step_ordertape(x: _Ctx, transport: Callable) -> dict:
    c = x.c
    sp = c.out / "_state" / "ordertape.json"
    prev = _load(sp) or {}
    store_state = c.store / STATE
    key = sha_obj({"wallets": c.wallets, "from": c.t_from, "to": c.t_to, "max_txs": c.max_txs,
                   "orders_path": str(c.orders.resolve()),
                   "store": sha_file(store_state) if store_state.is_file() else None} | x.base("ordertape"))
    status = "skipped" if prev.get("key") else "absent"
    missing_parts = ordertape.has_missing_parts(c.orders)
    if x.on("ordertape") and ("ordertape" in c.force or prev.get("key") != key
                              or not prev.get("complete") or not c.orders.is_dir() or missing_parts):
        t0 = time.monotonic()
        con = _connect(c.store, "256MB", 2)[0]
        try:
            with _lock(c.orders):
                todo = ordertape.select_txs(con, list(c.wallets), c.t_from, c.t_to, c.max_txs) if con else []
                pool = ordertape.Pool(list(c.rpcs), transport, rate=c.rate, max_requests=c.max_requests)
                res = ordertape.run(todo, pool, c.orders,
                                    expected=(lambda txs: ordertape.goldsky_ids(con, None, txs)) if con else None)
        finally:
            if con is not None:
                con.close()
        left = {k: res[k] for k in ("txs_failed", "txs_missing", "txs_undecodable", "txs_not_attempted") if res[k]}
        # Judged from the tape itself, not from the call's own counts: every selected tx must have
        # decoded rows on disk. This also catches txs recorded as done without orders
        # (``not_match_orders``: a call this decoder does not know) and parts that went missing.
        selected = {t[0] for t in todo}
        on_tape = {tx for p in c.orders.glob("day=*/part-*.parquet")
                   for tx in pq.read_table(p, columns=["tx"])["tx"].to_pylist()}
        if selected - on_tape:
            left["txs_selected_not_decoded"] = len(selected - on_tape)
        prev = {"key": key, "summary": res, "wall_s": round(time.monotonic() - t0, 1),
                "complete": res["stopped"] is None and not left, "left": left,
                "txs_selected": len(selected), "txs_decoded_on_tape": len(selected & on_tape),
                "parts": prev.get("parts", {})}
        status = "ran"
        missing_parts = ordertape.has_missing_parts(c.orders)
    elif "ordertape" not in c.steps:
        status = "kept" if c.orders.is_dir() else "absent"
    parts = prev.setdefault("parts", {})
    cached = parts.copy()
    out = orders_digest(c.orders, parts) if c.orders.is_dir() else None
    if status == "ran" or parts != cached:
        _write_json(sp, prev)
    # an order tape this run directory never fetched (--orders) has no key here: kept, not judged
    return {"status": status, "stale": bool(prev.get("key")) and prev["key"] != key,
            "wall_s": prev.get("wall_s"), "complete": False if missing_parts else prev.get("complete"),
            "left": prev.get("left"),
            "txs_selected": prev.get("txs_selected"), "txs_decoded_on_tape": prev.get("txs_decoded_on_tape"),
            "inputs": {"store_state_sha256": sha_file(store_state) if store_state.is_file() else None,
                       "store": str(c.store), "wallets": len(c.wallets), "max_txs_per_wallet": c.max_txs},
            "outputs": {"orders": out}, "summary": prev.get("summary")}


# ---------------------------------------------------------------- step: ordermatch
def _hist_q(h: Counter, qs=(0.1, 0.5, 0.9)) -> dict | None:
    """Quantiles (numpy's linear rule) of a value -> count histogram; the shape of ordermatch._q."""
    n = sum(h.values())
    if not n:
        return None
    vals = sorted(h)
    cum = np.cumsum([h[v] for v in vals])

    def at(i: int) -> float:
        return vals[int(np.searchsorted(cum, i, side="right"))]

    out = {}
    for q in qs:
        pos = q * (n - 1)
        lo = int(pos)
        out[f"p{int(q * 100)}"] = round(at(lo) + (at(min(lo + 1, n - 1)) - at(lo)) * (pos - lo), 1)
    return out | {"n": n}


def _no_precision(fp: dict) -> None:
    """Null the identity precision of a fingerprint document, with the reason (see module docstring)."""
    for line in (fp["total"], *fp["per_size"].values()):
        line["identity_precision_upper"] = None
    fp["identity_precision_note"] = NO_PRECISION


def _unit_ordermatch(c: Cfg, d: Path, u: dict, streams: list[str]) -> dict:
    """Run ordermatch for one wallet batch on one chunk. Writes the tool's three files (candidate
    rows streamed to ``quotes.jsonl`` as they are found) plus the candidates' life histogram per
    wallet (integer ms -> count), which the merge needs for exact quantiles."""
    batch = set(u["wallets"])
    qw = Jsonl(d / "quotes.jsonl")
    hist: dict[str, Counter] = defaultdict(Counter)

    def sink(r: dict) -> None:
        qw.write(r)
        if r["rest_known"]:
            hist[r["wallet"]][r["rest_max_ms"]] += 1

    doc, rows, _ = ordermatch.run(c.data, c.orders, u["from"], u["to"], list(u["wallets"]),
                                  ordermatch.Params(size_tol_rel=c.size_tol_rel), streams,
                                  min_fp_orders=MIN_FP_ORDERS, focus=True, quote_sink=sink)
    doc.pop("others_pooled", None)  # other makers are tracked only where their markets overlap
    dropped = 0
    for w in doc["wallets"].values():
        if w["fingerprint"]:
            _no_precision(w["fingerprint"])
        elif 0 < w["maker_orders"] < MIN_FP_ORDERS:
            w["fingerprint_dropped"] = f"fewer than {MIN_FP_ORDERS} maker orders in the chunk"
            dropped += 1
    ow = Jsonl(d / "orders.jsonl")
    for r in rows:
        if r["maker"] in batch:  # persist order rows only for the wallets in this batch
            ow.write(r)
    ws = doc["wallets"].values()
    return {"outputs": {
        "ordermatch.json": _write_json(d / "ordermatch.json", doc),
        "orders.jsonl": ow.close(), "quotes.jsonl": qw.close(),
        "quotes_rest_ms.json": _write_json(d / "quotes_rest_ms.json", {
            w: {str(k): v for k, v in sorted(h.items())} for w, h in sorted(hist.items())})},
        "counts": {"orders": doc["orders"], "tape": doc["tape"],
                   "maker_orders": sum(w["maker_orders"] for w in ws),
                   "observable": sum(w["observable"] for w in ws),
                   "matched_all": sum(w["matched_all"] for w in ws),
                   "fingerprints_dropped": dropped,
                   "censored": sum(w["outcomes"].get("unknown:censored", 0) for w in ws)}}


MATCH_COUNTS = ("maker_orders", "observable", "matched", "placebo_matched", "matched_all",
                "cancelled_alone", "cancelled_no_prints_near", "cancelled_seen_as_price_change",
                "fill_before_placement", "rest_not_known", "rest_censored")
FP_COUNTS = ("own", "other_known", "candidates", "own_unmatched", "control")
_r, _q = ordermatch._r, ordermatch._q


def _fp_line(n: Counter, h_act: float, h_ctl: float) -> dict:
    """ordermatch.attribute's line, from summed counts and summed token-hours."""
    rate = n["control"] / h_ctl if h_ctl > 0 else None
    exp = None if rate is None else rate * h_act
    return {k: n[k] for k in FP_COUNTS} | {
        "control_per_token_hour": _r(rate), "expected_by_chance": _r(exp),
        "net_estimate": None if exp is None else _r(max(0.0, n["candidates"] - n["own_unmatched"] - exp)),
        "identity_precision_upper": None}


def merge_fingerprint(fps: list[tuple[str, dict]], rest: Counter) -> dict | None:
    """One wallet's chunk fingerprints (chunk id, document) merged by summing counts."""
    if not fps:
        return None
    total: Counter = Counter()
    per: dict[str, Counter] = defaultdict(Counter)
    hours: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])  # size -> [active, control] where it was a size
    outcomes: Counter = Counter()
    for _, f in fps:
        total.update({k: f["total"][k] for k in FP_COUNTS})
        outcomes.update(f["candidate_outcomes"])
        for s, v in f["per_size"].items():
            per[s].update({k: v[k] for k in FP_COUNTS})
            hours[s][0] += f["active_token_hours"]
            hours[s][1] += f["control_token_hours"]
    h_act = sum(f["active_token_hours"] for _, f in fps)
    h_ctl = sum(f["control_token_hours"] for _, f in fps)
    return {"sizes": sorted({s for _, f in fps for s in f["sizes"]}),
            "sizes_by_chunk": {cid: f["sizes"] for cid, f in fps},
            "sizes_stable": len({tuple(f["sizes"]) for _, f in fps}) == 1,
            "active_token_hours": _r(h_act), "control_token_hours": _r(h_ctl),
            "total": _fp_line(total, h_act, h_ctl),
            "per_size": {s: _fp_line(per[s], *hours[s]) for s in sorted(per, key=float)},
            "candidate_outcomes": dict(outcomes), "candidate_rest_ms_cancelled": _hist_q(rest),
            "identity_precision_note": NO_PRECISION,
            "note": "counts summed over chunks, rates recomputed from the sums; candidates are NOT "
                    "identified as the wallet's (see ordermatch); if sizes_stable is false the "
                    "sums mix different size sets"}


def merge_match(wallet: str, docs: list[tuple[str, dict]], rows: list[dict], rest: Counter) -> dict:
    """One wallet's chunk documents merged into the shape of ``ordermatch.summarize``."""
    n = {k: sum(d[k] for _, d in docs) for k in MATCH_COUNTS}
    outcomes: Counter = Counter()
    fills: Counter = Counter()
    for _, d in docs:
        outcomes.update(d["outcomes"])
        fills.update(d["fills"])
    obs = n["observable"]
    return {"wallet": wallet, "chunks": [cid for cid, _ in docs],
            "maker_orders": n["maker_orders"], "observable": obs, "matched": n["matched"],
            "match_rate": _r(n["matched"] / obs) if obs else None,
            "placebo_matched": n["placebo_matched"],
            "placebo_rate": _r(n["placebo_matched"] / obs) if obs else None,
            "match_rate_by_side": None,  # no per-side denominators in the chunk documents
            "matched_all": n["matched_all"],
            "sign_to_book_ms": _q([r["sign_to_book_ms"] for r in rows]),
            "tw_minus_created_ms": _q([r["placed_tw_ns"] / 1e6 - r["created_ms"] for r in rows]),
            "outcomes": dict(outcomes),
            "cancelled_alone": n["cancelled_alone"],
            "cancelled_no_prints_near": n["cancelled_no_prints_near"],
            "cancelled_seen_as_price_change": n["cancelled_seen_as_price_change"],
            "fill_before_placement": n["fill_before_placement"],
            "fills": dict(fills),
            "rest_ms_known": {k: _q([r["rest_max_ms"] for r in rows if r["rest_known"] and r["outcome"] == k])
                              for k in ("filled", "cancelled")},
            "rest_not_known": n["rest_not_known"], "rest_censored": n["rest_censored"],
            "fingerprint_dropped_chunks": [cid for cid, d in docs if d.get("fingerprint_dropped")],
            "fingerprint": merge_fingerprint([(cid, d["fingerprint"]) for cid, d in docs if d.get("fingerprint")], rest)}


def _partition(files: list[Path], field_: str, wallets: tuple[str, ...], out: Path, name: str) -> dict[str, dict]:
    """Split unit JSONL rows by ``row[field_]`` into ``out/wallets/<wallet>/<name>``, rewritten
    from scratch under a temporary name and renamed only when every file is complete. Returns
    {relative path: {sha256, bytes, rows}}."""
    ws = {w: Jsonl(out / "wallets" / w / name) for w in wallets}
    try:
        for f in files:
            for r in _jsonl(f):
                w = r.get(field_)
                if isinstance(w, str) and w in ws:
                    ws[w].write(r)
    except BaseException:
        for j in ws.values():
            j.fh.close()
        raise
    return {f"wallets/{w}/{name}": j.close() for w, j in ws.items()}


def _units(x: _Ctx, step: str, units: list[dict], key_of: Callable[[dict], dict | None],
           work: Callable[[Path, dict], dict]) -> list[dict]:
    """Run ``work`` on every unit with orders whose key changed; returns one record per unit.
    After each unit that ran, the process's peak RSS is recorded and checked against the budget."""
    c, recs = x.c, []
    for u in units:
        rec: dict = {k: u[k] for k in ("chunk", "batch", "from", "to")} | {"wallets": len(u["wallets"])}
        if not u["orders_in"]:
            recs.append(rec | {"status": "empty"})
            continue
        d = c.out / "chunks" / u["chunk"] / u["batch"] / step
        prev = _load(d / "_done.json")
        inputs = key_of(u)
        if inputs is None:
            recs.append(rec | {"status": "missing_input"})
            continue
        key = sha_obj(inputs | {"from": u["from"], "to": u["to"], "wallets": u["wallets"]} | x.base(step))
        if x.on(step) and (step in c.force or not _fresh(prev, key, d)):
            t0 = time.monotonic()
            prev = work(d, u) | {"key": key, "inputs": inputs}
            prev |= {"wall_s": round(time.monotonic() - t0, 1), "peak_rss_mb": peak_rss_mb()}
            _write_json(d / "_done.json", prev)
            status = "ran"
            if c.mem_budget_mb is not None and prev["peak_rss_mb"] > c.mem_budget_mb:
                x.halt_step = step
                x.halt = (f"peak RSS {prev['peak_rss_mb']} MB after {step} {u['chunk']}/{u['batch']} is over "
                          f"--mem-budget-mb {c.mem_budget_mb}: stopped; use a shorter chunk "
                          "(--chunk-minutes) or a smaller --wallet-batch")
        elif prev is None:
            status = "absent"
        else:
            status = "skipped" if step in c.steps and x.halt is None else "kept"
        recs.append(rec | {"status": status, "dir": str(d.relative_to(c.out))} | (
            {"stale": prev["key"] != key, "key": prev["key"], "wall_s": prev["wall_s"],
             "peak_rss_mb": prev.get("peak_rss_mb"), "inputs": prev["inputs"],
             "outputs": prev["outputs"], "counts": prev["counts"]} if prev else {}))
    return recs


def _have(recs: list[dict]) -> list[dict]:
    return [r for r in recs if "outputs" in r]


def _step_record(x: _Ctx, step: str, recs: list[dict], prev: dict | None, key: str, merged: str) -> dict:
    """The manifest record of a chunked step. Stale: a unit or the merge was made from other
    inputs or code, a unit with orders has no output, or units exist without a merge."""
    done = _have(recs)
    ran = merged == "ran" or any(r["status"] == "ran" for r in recs)
    missing = any(r["status"] in ("absent", "missing_input") for r in recs)
    stale = (any(r.get("stale") for r in recs) or bool(done) and (missing or prev is None)
             or prev is not None and prev["key"] != key)
    return {"status": "ran" if ran else merged, "stale": stale, "merge_key": prev and prev["key"],
            "wall_s": round(sum(r.get("wall_s", 0) for r in recs) + (prev or {}).get("wall_s", 0), 1),
            "peak_rss_mb": max((r["peak_rss_mb"] for r in recs if r.get("peak_rss_mb")), default=None),
            "chunks": recs, "outputs": (prev or {}).get("outputs", {})}


def step_ordermatch(x: _Ctx, units: list[dict], orders: dict | None) -> dict:
    c = x.c
    recs = _units(x, "ordermatch", units,
                  lambda u: None if orders is None else {
                      "orders": orders["sha256"], "params": ordermatch.Params(size_tol_rel=c.size_tol_rel).__dict__,
                      "max_age_s": ordermatch.MAX_AGE_S,
                      "tape": tape_hashes(c.data, x.streams, u["from"], u["to"], x.tcache)},
                  lambda d, u: _unit_ordermatch(c, d, u, x.streams))
    done = _have(recs)
    sp, out = c.out / "_state" / "ordermatch.json", c.out / "ordermatch"
    key = sha_obj({"units": [r["key"] for r in done], "wallets": c.wallets} | x.base("ordermatch"))
    prev = _load(sp)
    merged = "skipped"
    if not done and prev is None:
        merged = "absent"
    elif x.on("ordermatch") and not (_fresh(prev, key, c.out) and "ordermatch" not in c.force):
        t0 = time.monotonic()
        dirs = [c.out / r["dir"] for r in done]
        files = _partition([d / "orders.jsonl" for d in dirs], "maker", c.wallets, c.out, "orders.jsonl")
        docs = [(r["chunk"], orjson.loads((d / "ordermatch.json").read_bytes())) for r, d in zip(done, dirs)]
        hists = [orjson.loads((d / "quotes_rest_ms.json").read_bytes()) for d in dirs]
        wallets = {}
        for w in c.wallets:
            rest: Counter = Counter()
            for h in hists:
                rest.update({int(k): v for k, v in h.get(w, {}).items()})
            wallets[w] = merge_match(w, [(cid, d["wallets"][w]) for cid, d in docs if w in d["wallets"]],
                                     list(_jsonl(c.out / "wallets" / w / "orders.jsonl")), rest)
        doc = {"params": {"from": c.t_from, "to": c.t_to, "chunk_s": c.chunk_s, "wallet_batch": c.wallet_batch,
                          "chunks": sorted({r["chunk"] for r in done}),
                          "per_unit": docs[0][1]["params"] if docs else None},
               "merge": "counts summed over chunks; rates recomputed from the sums; quantiles from the pooled rows",
               "orders_by_unit": {f"{r['chunk']}/{r['batch']}": d["orders"] for r, (_, d) in zip(done, docs)},
               "wallets": wallets}
        prev = {"key": key, "wall_s": round(time.monotonic() - t0, 1),
                "outputs": {"ordermatch/ordermatch.json": _write_json(out / "ordermatch.json", doc)} | files}
        _write_json(sp, prev)
        merged = "ran"
    elif "ordermatch" not in c.steps or x.halt:
        merged = "kept"
    return _step_record(x, "ordermatch", recs, prev, key, merged)


# ---------------------------------------------------------------- step: orderreplay
_DERIVED = ("orders_replayed", "headline_orders", "known_end_cut_by_reset", "cut_short", "twin_book_cross_fills")


def _unit_orderreplay(c: Cfg, d: Path, u: dict, streams: list[str]) -> dict:
    """Replay one unit. Candidates are reduced to counts inside ``orderreplay.run``; only order
    rows come back. Orders that cannot be replayed on a tape cut at the chunk end are marked
    ``cut = "chunk_end"`` (see WHAT A CHUNK EDGE COSTS) and the document is summarized again."""
    om = d.parent / "ordermatch"
    doc, rows = orderreplay.run(c.data, om, u["from"], u["to"], streams, reduce_quotes=True)
    matched = {r["order_id"]: r for r in _jsonl(om / "orders.jsonl")}
    edge, n_edge = u["to"] * 1000, 0
    ow = Jsonl(d / "orderreplay.jsonl")
    for r in rows:
        m = matched[r["id"]]
        r["maker"] = m["maker"]
        if not r["cut"] and (m["fills_located"] < m["n_fills"] or (m["end_ms"] is not None and m["end_ms"] >= edge)
                             or any(f[1] is not None and f[1] >= edge for f in m["fills_at"])):
            r["cut"] = "chunk_end"
            n_edge += 1
        ow.write(r)
    raw = Counter({k: v for k, v in doc["counts"].items() if k not in _DERIVED})
    doc = {k: doc[k] for k in ("params", "never_filled_quotes_estimate", "never_filled_by_wallet") if k in doc} \
        | orderreplay.summarize(rows, raw)
    g = doc["adapter_gate"]
    return {"outputs": {"orderreplay.json": _write_json(d / "orderreplay.json", doc),
                        "orderreplay.jsonl": ow.close()},
            "counts": {"counts": doc["counts"], "cut_chunk_end": n_edge, "routed_share": g["routed_share"],
                       "delivered_share": g["delivered_share"],
                       "bracket_violations": {k: v for k, v in doc["bracket_violations"].items() if k != "note"}}}


def _sum_tallies(ts: list[dict]) -> dict[str, Counter]:
    """Sum per-mode flat count dicts (``orderreplay.nf_add``'s shape)."""
    out = {m: Counter() for m in MODES}
    for t in ts:
        for m in MODES:
            out[m].update(t.get(m, {}))
    return out


def _sum_counts(docs: list[dict]) -> dict:
    """Sum numeric leaves of flat dicts; a nested dict of numbers is summed key by key."""
    out: dict = {}
    for d in docs:
        for k, v in d.items():
            if isinstance(v, dict):
                out[k] = dict(Counter(out.get(k, {})) + Counter(v))
            elif isinstance(v, int | float) and not isinstance(v, bool):
                out[k] = out.get(k, 0) + v
    return out


COND_COUNTS = ("n", "real_shares", "twin_shares", "exact", "underfilled", "missed", "phantom", "phantom_shares")


def merge_replay_cohort(wallets: dict[str, dict], tallies: list[dict],
                        by_market: dict[str, dict[str, list[float]]]) -> dict:
    """Cohort document: the wallet documents' counts summed; ratio and CI over those wallets'
    headline rows (``by_market`` holds their twin and real shares per market)."""
    docs = list(wallets.values())
    g = _sum_counts([{k: v for k, v in d["adapter_gate"].items() if not k.endswith("_share")} for d in docs])
    seen, routed = g.get("print_seen_in_replay", 0), g.get("routed_to_order_price_and_side", 0)
    cond = {}
    for m in MODES:
        n = _sum_counts([{k: d["conditional_on_real_fill"][m][k] or 0 for k in COND_COUNTS} for d in docs])
        pseudo = [{"market": k, m: {"filled": v[0]}, "real_filled": v[1]} for k, v in by_market[m].items()]
        real = sum(v[1] for v in by_market[m].values())
        cond[m] = n | {"markets": len(pseudo),
                       "ratio": _r(sum(v[0] for v in by_market[m].values()) / real, 4) if real > 0 else None,
                       "ratio_ci95": orderreplay.boot_ci(pseudo, m) if real > 0 else None}
    return {"limit": orderreplay.LIMIT, "counts": _sum_counts([d["counts"] for d in docs]),
            "adapter_gate": g | {"routed_share": _r(routed / seen, 4) if seen else None,
                                 "delivered_share": _r(g.get("delivered_to_live_quote", 0) / routed, 4) if routed else None},
            "conditional_on_real_fill": cond,
            "bracket_violations": {"pessimistic_overfilled": cond["pessimistic"].get("phantom", 0),
                                   "optimistic_underfilled": cond["optimistic"].get("underfilled", 0)},
            "never_filled_quotes_estimate": orderreplay.nf_doc(_sum_tallies(tallies))}


def step_orderreplay(x: _Ctx, units: list[dict], om: dict) -> dict:
    c = x.c
    om_out = {(r["chunk"], r["batch"]): r["outputs"] for r in _have(om["chunks"])}
    recs = _units(x, "orderreplay", units,
                  lambda u: None if (u["chunk"], u["batch"]) not in om_out else {
                      "orders.jsonl": om_out[u["chunk"], u["batch"]]["orders.jsonl"]["sha256"],
                      "quotes.jsonl": om_out[u["chunk"], u["batch"]]["quotes.jsonl"]["sha256"],
                      "tape": tape_hashes(c.data, x.streams, u["from"], u["to"], x.tcache)},
                  lambda d, u: _unit_orderreplay(c, d, u, x.streams))
    done = _have(recs)
    sp, out = c.out / "_state" / "orderreplay.json", c.out / "orderreplay"
    # the merge reads wallets/<w>/orders.jsonl, which the ordermatch merge wrote: its key is an input
    key = sha_obj({"units": [r["key"] for r in done], "wallets": c.wallets, "ordermatch_merge": om["merge_key"]}
                  | x.base("orderreplay"))
    prev = _load(sp)
    merged = "skipped"
    if not done and prev is None:
        merged = "absent"
    elif x.on("orderreplay") and not (_fresh(prev, key, c.out) and "orderreplay" not in c.force):
        t0 = time.monotonic()
        dirs = [c.out / r["dir"] for r in done]
        files = _partition([d / "orderreplay.jsonl" for d in dirs], "maker", c.wallets, c.out, "orderreplay.jsonl")
        docs = [orjson.loads((d / "orderreplay.json").read_bytes()) for d in dirs]
        by_market: dict[str, dict[str, list[float]]] = {m: defaultdict(lambda: [0.0, 0.0]) for m in MODES}
        wallets, tallies = {}, []
        for w in c.wallets:
            rows = list(_jsonl(c.out / "wallets" / w / "orderreplay.jsonl"))
            mp = c.out / "wallets" / w / "orders.jsonl"
            matched = list(_jsonl(mp)) if mp.is_file() else []
            wrong = sum(r["fill_before_placement"] for r in matched)
            d = orderreplay.summarize(rows, Counter({
                "orders": len(matched), "orders_wrong_match_skipped": wrong,
                "placement_not_found": len(matched) - wrong - len(rows)}))
            tally = _sum_tallies([x_.get("never_filled_by_wallet", {}).get(w, {}) for x_ in docs])
            tallies.append(tally)
            if nf := orderreplay.nf_doc(tally):
                d["never_filled_quotes_estimate"] = nf
            wallets[w] = d
            for r in rows:
                if r["end_known"] and not r["cut"]:
                    for m in MODES:
                        by_market[m][r["market"]][0] += r[m]["filled"]
                        by_market[m][r["market"]][1] += r["real_filled"]
        doc = {"params": {"from": c.t_from, "to": c.t_to, "chunk_s": c.chunk_s, "wallet_batch": c.wallet_batch,
                          "chunks": sorted({r["chunk"] for r in done})},
               "merge": "per wallet: orderreplay.summarize on the wallet's order rows from all chunks; "
                        "cohort: the wallet documents' counts summed, ratio and CI over the same rows",
               "cohort": merge_replay_cohort(wallets, tallies, by_market), "wallets": wallets}
        prev = {"key": key, "wall_s": round(time.monotonic() - t0, 1),
                "outputs": {"orderreplay/orderreplay.json": _write_json(out / "orderreplay.json", doc)} | files}
        _write_json(sp, prev)
        merged = "ran"
    elif "orderreplay" not in c.steps or x.halt:
        merged = "kept"
    return _step_record(x, "orderreplay", recs, prev, key, merged)


# ---------------------------------------------------------------- step: profile
def step_profile(x: _Ctx, orders: dict | None) -> dict:
    c = x.c
    sp, out = c.out / "_state" / "profile.json", c.out / "profile"
    omp = c.out / "ordermatch" / "ordermatch.json"
    dos = {w: sha_file(c.dossiers / f"{w}.json") for w in c.wallets
           if c.dossiers and (c.dossiers / f"{w}.json").is_file()}
    inputs = {"orders": orders and orders["sha256"], "ordermatch": sha_file(omp) if omp.is_file() else None,
              "dossiers": dos, "wallets": c.wallets, "block_range": [c.t_from, c.t_to]}
    key = sha_obj(inputs | x.base("profile"))
    prev = _load(sp)
    status = "skipped" if prev else "absent"
    if x.on("profile") and orders is not None and ("profile" in c.force or not _fresh(prev, key, c.out)):
        t0 = time.monotonic()
        for i in range(0, len(c.wallets), PROFILE_BATCH):
            profile.run(c.orders, out, omp if omp.is_file() else None, c.dossiers,
                        list(c.wallets[i:i + PROFILE_BATCH]), block_range=(c.t_from, c.t_to))
        docs = [orjson.loads((out / f"{w}.ledger.json").read_bytes()) for w in c.wallets]
        cdoc, cmd = profile.cohort(docs)  # the per-batch cohort tables only saw their own batch
        outputs = {"profile/cohort.json": _write_json(out / "cohort.json", cdoc),
                   "profile/cohort.md": _write(out / "cohort.md", cmd.encode())}
        for w in c.wallets:
            for ext in ("json", "md"):
                p = out / f"{w}.ledger.{ext}"
                outputs[f"profile/{p.name}"] = {"sha256": sha_file(p), "bytes": p.stat().st_size}
        prev = {"key": key, "inputs": inputs, "outputs": outputs, "wall_s": round(time.monotonic() - t0, 1)}
        _write_json(sp, prev)
        status = "ran"
    elif prev and ("profile" not in c.steps or x.halt):
        status = "kept"
    return {"status": status, "stale": bool(prev and prev["key"] != key),
            "wall_s": (prev or {}).get("wall_s"), "inputs": inputs, "outputs": (prev or {}).get("outputs", {})}


# ---------------------------------------------------------------- gates
def gate(table: dict[str, dict], name: str, value: float | None, n: int | None) -> dict:
    g = table[name]
    lim = {k: g[k] for k in ("min", "max") if k in g}
    if value is not None and value < 0:
        status = "fail"  # a share cannot be negative: the inputs disagree
    elif value is None or not n or n < g["min_n"]:
        status = "not_evaluable"
    else:
        status = "pass" if value >= lim.get("min", value) and value <= lim.get("max", value) else "fail"
    return {"step": g["step"], "value": value, "n": n or 0, **lim, "min_n": g["min_n"], "status": status}


def wallet_gates(table: dict[str, dict], vs: dict | None, om: dict | None, rp: dict | None) -> dict[str, dict]:
    """Every gate for one wallet from its verified share, merged ordermatch and merged replay."""
    vs, om, rp = vs or {}, om or {}, rp or {}
    ag, cnt = rp.get("adapter_gate", {}), rp.get("counts", {})
    bv = rp.get("bracket_violations", {})
    to_replay = cnt.get("orders", 0) - cnt.get("orders_wrong_match_skipped", 0)
    head = cnt.get("headline_orders", 0)
    vals = {
        "ordertape.verified_share": (vs.get("share"), vs.get("fills_expected")),
        "ordermatch.match_rate": (om.get("match_rate"), om.get("observable")),
        "ordermatch.placebo_rate": (om.get("placebo_rate"), om.get("observable")),
        "orderreplay.routed_share": (ag.get("routed_share"), ag.get("print_seen_in_replay")),
        "orderreplay.delivered_share": (ag.get("delivered_share"), ag.get("routed_to_order_price_and_side")),
        "orderreplay.placement_not_found_share": (
            round(cnt["placement_not_found"] / to_replay, 4) if to_replay else None, to_replay),
        "orderreplay.bracket_violation_share": (
            round((bv["pessimistic_overfilled"] + bv["optimistic_underfilled"]) / head, 4) if head else None, head),
    }
    return {k: gate(table, k, *v) for k, v in vals.items()}


def untrusted(gates: dict[str, dict], flags: dict[str, list[str]] | None = None) -> dict[str, list[str]]:
    """step -> why its output is not trusted; [] = trusted. Reasons: the gates (its own and its
    upstream steps') that are not ``pass``, and ``<step>:<flag>`` for a step (own or upstream)
    flagged ``stale`` (kept output made from other inputs or code), ``incomplete`` (order tape
    with txs failed, missing, undecodable or not attempted) or ``mem_budget_stop``."""
    flags = flags or {}
    return {s: [f"{u}:{f}" for u in (*UPSTREAM[s], s) for f in flags.get(u, [])]
            + [f"{k}:{g['status']}" for k, g in gates.items()
               if g["step"] in (s, *UPSTREAM[s]) and g["status"] != "pass"] for s in STEPS}


# ---------------------------------------------------------------- report
def _mark(x, bad: list[str]) -> str:
    """Cell text. UNTRUSTED = a gate failed, or a step is stale, incomplete or stopped; NO GATE =
    a gate could not be evaluated (too few cases, step not run), so nothing vouches for the number."""
    s = "" if x is None else orjson.dumps(x).decode().replace("|", "/") if isinstance(x, dict | list) else str(x)
    if not bad or not s:
        return s
    return f"{s} " + ("NO GATE" if all(b.endswith(":not_evaluable") for b in bad) else "UNTRUSTED")


def build_report(c: Cfg, man: dict) -> tuple[dict, str]:
    om = (_load(c.out / "ordermatch" / "ordermatch.json") or {}).get("wallets", {})
    rp = _load(c.out / "orderreplay" / "orderreplay.json") or {}
    coh = _load(c.out / "profile" / "cohort.json") or {}
    clus = {r["wallet"]: r for r in coh.get("wallets", [])}
    rows, status = [], {k: Counter() for k in profile.WEIGHTS}
    resolve: dict[str, str] = {}
    for w in c.wallets:
        led = _load(c.out / "profile" / f"{w}.ledger.json")
        o, r, mw = om.get(w), rp.get("wallets", {}).get(w), man["wallets"][w]
        ft = profile.footprint(o)
        for k, ob in (led or {}).get("obligations", {}).items():
            status[k][ob["status"]] += 1
            if ob["status"] == profile.UNKNOWN and ob["resolve"]:
                resolve.setdefault(k, ob["resolve"])
        cl = clus.get(w, {})
        rows.append({
            "wallet": w, "role": c.roles.get(w), "mechanism": led and led["mechanism"],
            "verified_orders": led and led["fingerprint"]["n_orders"],
            "verified_maker_orders": led and led["fingerprint"]["n_maker_orders"],
            "verified_share": mw["gates"]["ordertape.verified_share"]["value"],
            "tape": mw["tape"],
            "match": o and {k: o[k] for k in ("maker_orders", "observable", "matched", "match_rate",
                                              "placebo_matched", "placebo_rate")},
            "rest_ms_p50": o and {k: v and v["p50"] for k, v in o["rest_ms_known"].items()},
            "unfilled_quote_life_ms_p50": ((o or {}).get("fingerprint") or {}).get("candidate_rest_ms_cancelled", None)
            and o["fingerprint"]["candidate_rest_ms_cancelled"]["p50"],
            "footprint": ft and {"fill_ratio": ft["fill_ratio"], "chance_share": ft["chance_share"],
                                 "filled_quotes": ft["filled_quotes"],
                                 "unfilled_net_estimate": ft["unfilled_net_estimate"],
                                 "identity_precision_upper": None, "identity_precision_note": NO_PRECISION,
                                 "other_makers_proven": o["fingerprint"]["total"]["other_known"],
                                 "sizes_stable": o["fingerprint"]["sizes_stable"]},
            "twin": r and {m: {k: r["conditional_on_real_fill"][m].get(k) for k in ("n", "markets", "ratio", "ratio_ci95")}
                           for m in MODES},
            "coverage": led and led["coverage"], "copyability": led and led["copyability"],
            "software_cluster": cl and {"key": cl["software_key"], "size": cl["software_cluster_size"]},
            "operator_cluster": cl and {"key": cl["operator_key"], "size": cl["operator_cluster_size"]},
            "untrusted": mw["untrusted"]})
    tot = Counter()
    for o in om.values():
        tot.update({k: o[k] for k in ("maker_orders", "observable", "matched", "placebo_matched")})
    cr = rp.get("cohort") or {}
    bad = {s: sum(bool(r["untrusted"][s]) for r in rows) for s in STEPS}
    cohort = {
        "wallets": len(c.wallets), "wallets_with_verified_orders": sum(bool(r["verified_orders"]) for r in rows),
        "trusted_wallets": {s: len(rows) - bad[s] for s in STEPS},
        "match": dict(tot) | {"match_rate": _r(tot["matched"] / tot["observable"]) if tot["observable"] else None,
                              "placebo_rate": _r(tot["placebo_matched"] / tot["observable"]) if tot["observable"] else None},
        "twin": {m: {k: cr["conditional_on_real_fill"][m].get(k) for k in ("n", "markets", "ratio", "ratio_ci95")}
                 for m in MODES} if cr else None,
        "adapter_gate": cr.get("adapter_gate"), "bracket_violations": cr.get("bracket_violations"),
        "copyability": dict(Counter(r["copyability"] for r in rows if r["copyability"])),
        "software_clusters": len(coh.get("software_clusters", {})),
        "operator_clusters": len(coh.get("operator_clusters", {}))}
    # which step's trust each total rests on; the totals are over ALL wallets
    rests_on = {"match": "ordermatch", "twin": "orderreplay", "adapter_gate": "orderreplay",
                "bracket_violations": "orderreplay", "copyability": "profile",
                "software_clusters": "profile", "operator_clusters": "profile"}
    ot = man["steps"]["ordertape"]
    doc = {"run_id": man["run_id"], "git_commit": man["git_commit"], "range": man["range"],
           "cohort_sha256": man["cohort"]["sha256"], "gates_provisional": man["gate_table"],
           "stopped": man["stopped"], "ordertape_complete": ot["complete"], "ordertape_left": ot["left"],
           "wallets": rows, "cohort": cohort,
           "cohort_totals_include_untrusted_wallets": {k: bad[s] for k, s in rests_on.items()},
           "ledger_status": {k: dict(v) for k, v in status.items()}, "unknown_resolve_by": resolve}

    L = [f"# Reconstruction {man['run_id']}", "",
         f"commit `{man['git_commit']}` | cohort `{man['cohort']['sha256'][:12]}` | "
         f"{man['range']['from_utc']} to {man['range']['to_utc']} | manifest: `manifest.json`", ""]
    if man["stopped"]:
        L += [f"**RUN STOPPED: {man['stopped']}** Everything below is partial.", ""]
    if ot["complete"] is False:
        L += [f"**ORDER TAPE INCOMPLETE** ({ot['txs_decoded_on_tape']} of {ot['txs_selected']} selected txs decoded; "
              f"{ot['left']}; stopped: {(ot['summary'] or {}).get('stopped')}): chain "
              "fills are missing for some txs, so every wallet is UNTRUSTED until the same command is run again "
              "and the tape completes. The tape coverage column says how much of each wallet was decoded.", ""]
    L += ["Read first: UNTRUSTED = a gate behind the cell failed, or a step is stale, incomplete or stopped; "
          "NO GATE = a gate could not be evaluated (too few cases), so nothing vouches for the number. Thresholds are "
          "PROVISIONAL (table in the manifest). Twin ratios are conditional on a real fill, not fill probabilities. "
          "Footprints are estimates with the stated chance share. Only filled orders are on chain. "
          "Identity precision of the footprint is NOT MEASURED in a cohort run (null everywhere): makers outside "
          "the cohort are not in the book match, so nothing bounds how many candidates are someone else's. "
          "sizes stable = the wallet's fingerprint sizes were the same in every chunk; if false the footprint "
          "mixes size sets. Tape coverage = the wallet's fills in decoded txs / its fills in the range "
          "(below 1 with --max-txs-per-wallet or an incomplete tape: chain fills are then a lower bound).", "",
          "| wallet | role | mechanism | verified orders (maker) | verified share | tape coverage | "
          "matched / observable (rate) | placebo rate | rest ms p50 filled / cancelled | fill ratio (chance share) | "
          "sizes stable | twin pess [CI] | twin opt [CI] | coverage | copyability | software / operator cluster | "
          "not passed |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        u = r["untrusted"]
        m, tw, ft = r["match"] or {}, r["twin"] or {}, r["footprint"] or {}
        rest, tp = r["rest_ms_p50"] or {}, r["tape"] or {}

        def twin(mode: str, tw=tw, u=u) -> str:
            t = tw.get(mode) or {}
            return _mark(f"{t.get('ratio')} {t.get('ratio_ci95')} n={t.get('n')}" if t else None, u["orderreplay"])

        sc, oc = r["software_cluster"] or {}, r["operator_cluster"] or {}
        L.append(
            f"| {r['wallet']} | {r['role'] or ''} | {_mark(r['mechanism'], [])} | "
            f"{_mark(r['verified_orders'], u['ordertape'])} ({r['verified_maker_orders'] or 0}) | "
            f"{_mark(r['verified_share'], [])} | "
            f"{_mark(f'''{tp.get('coverage')} ({tp.get('fills_expected')} / {tp.get('fills_in_range')})''' if tp else None, [])} | "
            f"{_mark(f'''{m.get('matched')} / {m.get('observable')} ({m.get('match_rate')})''' if m else None, u['ordermatch'])} | "
            f"{_mark(m.get('placebo_rate'), u['ordermatch'])} | "
            f"{_mark(f'''{rest.get('filled')} / {rest.get('cancelled')}''' if rest else None, u['ordermatch'])} | "
            f"{_mark(f'''{ft.get('fill_ratio')} ({ft.get('chance_share')})''' if ft else None, u['ordermatch'])} | "
            f"{_mark(ft.get('sizes_stable'), [])} | "
            f"{twin('pessimistic')} | {twin('optimistic')} | "
            f"{_mark(r['coverage'], u['profile'])} | {_mark(r['copyability'], u['profile'])} | "
            f"{sc.get('key')} ({sc.get('size')}) / {oc.get('key')} ({oc.get('size')}) | "
            f"{'; '.join(sorted({g for v in u.values() for g in v}))} |")
    L += ["", "## Cohort totals", "", "Totals are over ALL wallets of the run; a line says how many of them are "
          "not trusted for the step it rests on.", ""]
    for k, v in cohort.items():
        n = bad.get(rests_on.get(k, ""), 0)
        L.append(f"- {k}: {_mark(v, [])}" + (f" (includes {n} wallets not trusted for {rests_on[k]})" if n and v else ""))
    L += ["", "## Verified / estimated / unknown (from the ledgers)", "",
          "Wallets per status of each obligation; the definitions are in `profile.py`.", "",
          "| obligation | VERIFIED | ESTIMATED | UNKNOWN | an UNKNOWN is resolved by |", "|---|---|---|---|---|"]
    L += [f"| {k} | {v[profile.VERIFIED]} | {v[profile.ESTIMATED]} | {v[profile.UNKNOWN]} | "
          f"{resolve.get(k, '').replace('|', '/')} |" for k, v in status.items()]
    L += ["", "Not in any ledger: orders that never filled are not on chain and stay anonymous (E34); "
          "`created_ms` is self-reported and is trusted only where the match-rate gate passes (E37)."]
    return doc, "\n".join(L) + "\n"


# ---------------------------------------------------------------- run
def run(c: Cfg, transport: Callable = ordertape.http_transport, gates: dict[str, dict] | None = None) -> dict:
    """Run the requested steps, write ``manifest.json``, ``report.json`` and ``report.md`` in
    ``c.out``, and return the manifest. ``transport`` is ordertape's JSON-RPC transport. Raises
    ``Locked`` when another run holds the run directory or the order tape."""
    with _lock(c.out):
        return _run(c, transport, gates or GATES)


def _run(c: Cfg, transport: Callable, table: dict[str, dict]) -> dict:
    t0 = time.monotonic()
    commit, src = git_commit()
    streams = sorted(d.name for d in c.data.iterdir() if d.name.startswith("clob_")) if c.data.is_dir() else []
    x = _Ctx(c, code_sha(), streams)
    chunks = chunk_list(c.t_from, c.t_to, c.chunk_s)

    steps: dict[str, dict] = {"ordertape": step_ordertape(x, transport)}
    orders = steps["ordertape"]["outputs"]["orders"]
    units = unit_list(c, chunks, busy_chunks(c.orders, chunks) if orders else {})
    steps["ordermatch"] = step_ordermatch(x, units, orders)
    steps["orderreplay"] = step_orderreplay(x, units, steps["ordermatch"])
    steps["profile"] = step_profile(x, orders)

    shares = verified_shares(c) if orders else {}
    om = (_load(c.out / "ordermatch" / "ordermatch.json") or {}).get("wallets", {})
    rp = (_load(c.out / "orderreplay" / "orderreplay.json") or {}).get("wallets", {})
    flags: dict[str, list[str]] = {s: ["stale"] for s in STEPS if steps[s]["stale"]}
    if steps["ordertape"]["complete"] is False:
        flags.setdefault("ordertape", []).append("incomplete")
    if x.halt_step:
        flags.setdefault(x.halt_step, []).append("mem_budget_stop")
    made = {k: v for s in STEPS for k, v in steps[s]["outputs"].items() if isinstance(v, dict) and "sha256" in v}
    wallets = {}
    for w in c.wallets:
        g = wallet_gates(table, shares.get(w), om.get(w), rp.get(w))
        files = (f"wallets/{w}/orders.jsonl", f"wallets/{w}/orderreplay.jsonl",
                 f"profile/{w}.ledger.json", f"profile/{w}.ledger.md")
        wallets[w] = {"role": c.roles.get(w), "gates": g, "untrusted": untrusted(g, flags),
                      "tape": shares.get(w),
                      "outputs": {"orders": f"{c.orders} (rows with maker == wallet and verified == True)"}
                      | {f: made[f] for f in files if f in made}}
    cohort_doc = orjson.loads(c.cohort.read_bytes())
    fmt = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    man = {
        "run_id": c.out.name, "written_utc": fmt(int(time.time())),
        "git_commit": commit, "git_commit_source": src,
        "code_sha256": {"files": x.code, "all": sha_obj(x.code)},
        "cohort": {"path": str(c.cohort), "sha256": sha_file(c.cohort),
                   "declared_sha256": cohort_doc.get("sha256"),
                   "candidates": len(cohort_doc.get("candidates", [])),
                   "controls": len(cohort_doc.get("controls", []))},
        "range": {"from": c.t_from, "to": c.t_to, "from_utc": fmt(c.t_from), "to_utc": fmt(c.t_to)},
        "args": {"data": str(c.data), "store": str(c.store), "orders": str(c.orders),
                 "dossiers": str(c.dossiers) if c.dossiers else None, "max_txs_per_wallet": c.max_txs,
                 "chunk_minutes": c.chunk_s / 60, "wallet_batch": c.wallet_batch,
                 "size_tol_rel": c.size_tol_rel, "min_fp_orders": MIN_FP_ORDERS,
                 "mem_budget_mb": c.mem_budget_mb, "max_requests": c.max_requests,
                 "steps": c.steps, "force": sorted(c.force), "streams": streams},
        "gate_table": {"provisional": True, "gates": table,
                       "rule": "a wallet's output of a step is trusted only when every gate of that step "
                               "and of its upstream steps is pass (not_evaluable is not pass) and none of "
                               "those steps is stale, incomplete or stopped",
                       "upstream": UPSTREAM},
        "chunk_merge": "per wallet: counts summed over chunks, rates recomputed from the sums, quantiles "
                       "from the pooled rows (never averages of chunk rates); see reconstruct.py",
        "stopped": x.halt,
        "peak_rss_mb": peak_rss_mb(),
        "peak_rss_note": "process high-water mark (getrusage); a unit's peak_rss_mb is the mark after that "
                         "unit, so a unit raised it only where the number goes up",
        "steps": steps,
        "untrusted_wallets": {s: sorted(w for w, v in wallets.items() if v["untrusted"][s]) for s in STEPS},
        "wallets": wallets,
        "wall_s": round(time.monotonic() - t0, 1),
    }
    rdoc, rmd = build_report(c, man)
    man["report"] = {"report.json": _write_json(c.out / "report.json", rdoc),
                     "report.md": _write(c.out / "report.md", rmd.encode())}
    _write_json(c.out / "manifest.json", man)
    return man


def run_pending(state: Path, new: tuple[int, int], go: Callable[[int, int, int | None], dict],
                max_requests: int | None = None) -> list[dict]:
    """The timer's queue (``--last-hours``). ``state`` lists ranges not finished yet. The new
    range is added, then ranges are run oldest first with ``go(from, to, requests left)`` until
    one does not finish (stopped, or order tape incomplete), or the request budget of this firing
    is spent. A finished range leaves the list; one that failed ``MAX_TRIES`` firings is moved to
    ``gave_up``. If ``go`` raises, the range stays for the next firing. Returns the manifests."""
    st = _load(state) or {"pending": [], "gave_up": []}
    if not any((p["from"], p["to"]) == new for p in st["pending"]):
        st["pending"].append({"from": new[0], "to": new[1], "tries": 0})
    st["pending"].sort(key=lambda p: p["from"])
    st["gave_up"] += st["pending"][:-MAX_PENDING]
    st["pending"] = st["pending"][-MAX_PENDING:]
    left, mans = max_requests, []
    for p in list(st["pending"]):
        if left is not None and left <= 0:
            break
        p["tries"] += 1
        if p["tries"] > MAX_TRIES:
            st["pending"].remove(p)
            st["gave_up"].append(p)
            _write_json(state, st)
            continue
        _write_json(state, st)  # before the work: a crash leaves the range (and its try) recorded
        man = go(p["from"], p["to"], left)
        mans.append(man)
        ot = man["steps"]["ordertape"]
        if left is not None and ot["status"] == "ran":
            left -= ot["summary"]["requests"]
        if man["stopped"] or ot["complete"] is False:
            break
        st["pending"].remove(p)
        _write_json(state, st)
    return mans


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-reconstruct", description=(
        "cohort + time range -> per wallet: verified order tape, book match, twin replay, ledger; "
        "resumable, with a manifest (see docs/RECONSTRUCTION.md)"))
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--lab", type=Path, required=True, help="lab root (default output, store, dossiers)")
    ap.add_argument("--store", type=Path, help="fill store (default: <lab>/fills); must be compacted already")
    ap.add_argument("--cohort", type=Path, required=True, help="frozen cohort JSON (candidates, controls)")
    ap.add_argument("--from", dest="t_from", type=_ts, help="unix s or ISO UTC, inclusive")
    ap.add_argument("--to", dest="t_to", type=_ts, help="exclusive")
    ap.add_argument("--last-hours", type=int, help="instead of --from/--to (timer use): the last N closed "
                    "hours, after any earlier range of this kind that did not finish "
                    "(<lab>/reconstruct/_pending.json); --max-requests is then the budget of the firing")
    ap.add_argument("--wallets", help="file with one address per line, or a comma-separated list "
                    "(default: every candidate and control of the cohort)")
    ap.add_argument("--max-txs-per-wallet", type=int, help="ordertape: keep each wallet's most recent N txs")
    ap.add_argument("--steps", default=",".join(STEPS), help=f"comma-separated subset of {','.join(STEPS)}")
    ap.add_argument("--force", action="append", default=[], choices=STEPS, help="rerun this step; repeatable")
    ap.add_argument("--chunk-hours", type=int, help="ordermatch / orderreplay chunk length (default 1 hour)")
    ap.add_argument("--chunk-minutes", type=int, help="the same in minutes, for less memory per chunk")
    ap.add_argument("--wallet-batch", type=int, default=10,
                    help="wallets per ordermatch / orderreplay call inside a chunk")
    ap.add_argument("--mem-budget-mb", type=float, help="stop cleanly when the process's peak RSS, "
                    "checked after every chunk unit, is above this")
    ap.add_argument("--size-tol-rel", type=float, default=0.0, help="ordermatch relative size tolerance "
                    "(E39 used 0.02; at 2%% the sizes 97..100 match each other)")
    ap.add_argument("--out", type=Path, help="run directory (default: <lab>/reconstruct/<run-id>, "
                    "the id derived from cohort, range and options, so the same command resumes)")
    ap.add_argument("--orders", type=Path, help="order tape directory (default: <out>/ordertape); "
                    "give an existing one to reuse txs already fetched")
    ap.add_argument("--dossiers", type=Path, help="directory of <wallet>.json dossiers "
                    "(default: <lab>/dossiers when it exists)")
    ap.add_argument("--rpc", action="append", help="public Polygon JSON-RPC, no key; repeatable")
    ap.add_argument("--rate", type=float, default=5.0, help="requests per second per endpoint")
    ap.add_argument("--max-requests", type=int, help="ordertape: stop cleanly after this many requests")
    a = ap.parse_args(argv)
    steps = tuple(s for s in STEPS if s in {x.strip() for x in a.steps.split(",")})
    if not steps or set(a.steps.split(",")) - set(STEPS) - {""}:
        ap.error(f"--steps: comma-separated subset of {','.join(STEPS)}")
    if (a.last_hours is None) == (a.t_from is None or a.t_to is None):
        ap.error("give --from and --to, or --last-hours")
    if a.last_hours is not None and (a.last_hours < 1 or a.out):
        ap.error("--last-hours >= 1, without --out (each range gets its own run directory)")
    if a.chunk_hours and a.chunk_minutes:
        ap.error("give --chunk-hours or --chunk-minutes, not both")
    chunk_s = (a.chunk_minutes or 60 * (a.chunk_hours or 1)) * 60
    if chunk_s < 60 or a.wallet_batch < 1:
        ap.error("--chunk-hours / --chunk-minutes / --wallet-batch must be >= 1")
    cohort = orjson.loads(a.cohort.read_bytes())
    roles = {w.lower(): "ctrl" for w in cohort.get("controls", [])} | {
        w.lower(): "cand" for w in cohort.get("candidates", [])}
    wallets = tuple(sorted(set(ordertape._list(a.wallets)))) if a.wallets else tuple(sorted(roles))
    if not wallets:
        ap.error("no wallets: the cohort has no candidates or controls and --wallets is empty")
    opts = {"wallets": a.wallets and wallets, "max_txs": a.max_txs_per_wallet, "chunk_s": chunk_s,
            "wallet_batch": a.wallet_batch, "size_tol_rel": a.size_tol_rel}
    plain = {"wallets": None, "max_txs": None, "chunk_s": 3600, "wallet_batch": 10, "size_tol_rel": 0.0}
    fmt = lambda t: time.strftime("%Y%m%dT%H%MZ", time.gmtime(t))  # noqa: E731
    dossiers = a.dossiers or (a.lab / "dossiers" if (a.lab / "dossiers").is_dir() else None)

    def cfg(t_from: int, t_to: int, max_requests: int | None) -> Cfg:
        run_id = (f"{(cohort.get('sha256') or sha_file(a.cohort))[:12]}-{fmt(t_from)}-{fmt(t_to)}"
                  + (f"-{sha_obj(opts)[:8]}" if opts != plain else ""))
        out = a.out or a.lab / "reconstruct" / run_id
        return Cfg(data=a.data, store=a.store or a.lab / "fills", cohort=a.cohort, t_from=t_from, t_to=t_to,
                   out=out, orders=a.orders or out / "ordertape", wallets=wallets, roles=roles, dossiers=dossiers,
                   max_txs=a.max_txs_per_wallet, chunk_s=chunk_s, wallet_batch=a.wallet_batch,
                   size_tol_rel=a.size_tol_rel, mem_budget_mb=a.mem_budget_mb, steps=steps,
                   force=frozenset(a.force), rpcs=tuple(a.rpc or ordertape.DEFAULT_RPCS), rate=a.rate,
                   max_requests=max_requests)

    try:
        if a.last_hours is None:
            t_from, t_to = int(a.t_from or 0), int(a.t_to or 0)  # both given (checked above)
            if t_to <= t_from:
                ap.error("--to must be after --from")
            mans = [run(cfg(t_from, t_to, a.max_requests))]
        else:
            t = int(time.time()) // 3600 * 3600
            mans = run_pending(a.lab / "reconstruct" / "_pending.json", (t - a.last_hours * 3600, t),
                               lambda f, t_, left: run(cfg(f, t_, left)), a.max_requests)
    except Locked as e:
        print(f"doppel-reconstruct: {e}", file=sys.stderr)
        return 3
    rc = 0
    for man in mans:
        print(f"== {man['run_id']}")
        for s in STEPS:
            st = man["steps"][s]
            ch = Counter(r["status"] for r in st.get("chunks", []))
            print(f"{s:12} {st['status']:8} wall {st['wall_s']} s" + (f" units {dict(ch)}" if ch else "")
                  + (f" peak RSS {st['peak_rss_mb']} MB" if st.get("peak_rss_mb") else "")
                  + f" untrusted wallets {len(man['untrusted_wallets'][s])}/{len(wallets)}")
        ot = man["steps"]["ordertape"]
        if ot["complete"] is False:
            print(f"ordertape is INCOMPLETE ({ot['left']}, stopped: {ot['summary']['stopped']}): every wallet is "
                  "untrusted; run the same command again", file=sys.stderr)
            rc = rc or 1
        if man["stopped"]:
            print(f"STOPPED: {man['stopped']}", file=sys.stderr)
            rc = 2
        out = a.lab / "reconstruct" / man["run_id"] if not a.out else a.out
        print(f"-> {out / 'report.md'}\n-> {out / 'manifest.json'}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
