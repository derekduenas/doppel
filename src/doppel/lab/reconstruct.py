"""Reconstruction pipeline: one resumable run from a frozen cohort and a time range to, per wallet,
the verified order tape, the book match, the twin replay and the ledger card, with a manifest
that traces every number. Plain-language description: ``docs/RECONSTRUCTION.md``.

STEPS (run in process, in this order; each can be left out with ``--steps``)
  ordertape    ``ordertape.run``: the cohort's txs in [--from, --to) from the fill store, decoded
               from chain calldata into ``<orders>`` (default ``<out>/ordertape``). It resumes per
               tx by itself. Called again while its last call left txs failed or not attempted.
  ordermatch   ``ordermatch.run`` once per chunk on the parquet directory, then a merge.
  orderreplay  ``orderreplay.run`` once per chunk on that chunk's ordermatch output, then a merge.
  profile      ``profile.run`` on the parquet directory and the merged ``ordermatch.json``
               (wallets in batches of ``PROFILE_BATCH``; the cohort table is rebuilt from all
               ledgers). It has no time range: it reads every verified row of the wallet in
               ``<orders>``, which is the run's range unless ``--orders`` points at a shared tape.

RESUME. A unit of work (the ordertape call, one chunk of ordermatch or orderreplay, a merge,
profile) is skipped when its recorded input key is unchanged and its outputs are still there with
the recorded size. The key is a sha256 over: the sha256 of the code files the unit runs, its
arguments, and its inputs' sha256 (fill store state file; order tape parts; tape segments, taken
from the stream's ``manifest.jsonl`` when the line matches the file size, else computed;
upstream outputs). ``--force STEP`` reruns that step's units. Forcing ``ordertape`` calls it
again; it still keeps the txs it already has (delete the directory to refetch).

CHUNKS. ordermatch and orderreplay hold an hour of level events in memory, so the range is cut
at multiples of ``--chunk-hours`` and each chunk is run alone on [chunk from, chunk to): orders
created in the chunk, tape records received in the chunk. Chunks do not overlap, so nothing is
counted twice. The cost, visible per chunk in the manifest: the book is unknown at the start of
a chunk until each asset's next snapshot; an order still resting at the end of its chunk is
``unknown:censored``; an order created in the last ~62.5 s of a chunk is not ``observable`` (its
placebo window crosses the edge). Chunks without a verified maker order created in them are not
run (``empty``). A known span is [first book, last frame] inside one replay, so a quiet stretch
across a chunk edge is not counted in token-hours.

FINGERPRINT FLOOR. Every wallet is passed to every chunk, and ordermatch derives a size
fingerprint from whatever orders the wallet has there, even two. A wallet with fewer than
``MIN_FP_ORDERS`` maker orders in a chunk therefore gets no fingerprint for that chunk: its
``fingerprint`` is set to null (``fingerprint_dropped`` says why) and its candidate rows are not
written. ordermatch has still done the counting; avoiding that cost needs a change inside
``ordermatch.run``. A wallet that never reaches the floor in one chunk has no footprint; a
longer ``--chunk-hours`` gives it one.

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
  are computed exactly as the tool computes them, on the union. Never-filled quote counts are
  summed. The cohort document sums chunk counts; its CI resamples the pooled markets.

GATES. ``GATES`` below is the one table of thresholds. All are PROVISIONAL. A gate is ``pass``,
``fail`` or ``not_evaluable`` (fewer than ``min_n`` cases, or the step did not run). A wallet's
output of a step is trusted only when every gate of that step and of the steps it reads from is
``pass``; otherwise the manifest and the report name the gates under ``untrusted``. The run
never stops on a gate.

Analysis only: nothing here is a decision input. No orders, no credentials; the only network
use is ordertape's public JSON-RPC reads.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
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
NS = 1_000_000_000
MODES = orderreplay.MODES

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
        "basis": "E37 0.93, E39 0.98 (one wallet-hour); profile.T match_min / min_observable"},
    "ordermatch.placebo_rate": {
        "step": "ordermatch", "max": profile.T["placebo_max"], "min_n": profile.T["min_observable"],
        "basis": "E37 / E39 0.046 (one wallet-hour); profile.T placebo_max"},
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
CODE = {
    "ordertape": ("lab/ordertape.py", "lab/fillstore.py"),
    "ordermatch": ("lab/ordermatch.py", "lab/intlbook.py", "tape/segment.py"),
    "orderreplay": ("lab/orderreplay.py", "lab/ordermatch.py", "lab/intlbook.py", "tape/segment.py",
                    "twin/engine.py", "twin/tape.py"),
    "profile": ("lab/profile.py", "lab/ordermatch.py", "lab/ordertape.py"),
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
    steps: tuple[str, ...] = STEPS
    force: frozenset[str] = frozenset()
    rpcs: tuple[str, ...] = ordertape.DEFAULT_RPCS
    rate: float = 5.0
    max_requests: int | None = None


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


def _write_jsonl(p: Path, rows: list[dict]) -> dict:
    return _write(p, b"".join(orjson.dumps(r) + b"\n" for r in rows)) | {"rows": len(rows)}


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


def code_sha() -> dict[str, str]:
    files = sorted({f for fs in CODE.values() for f in fs} | {"lab/reconstruct.py"})
    return {f: sha_file(PKG / f) for f in files}


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
    """sha256 over the order tape's parts (path + sha256 each), with row and part counts. Parts
    are written once, so ``cache`` (path -> [bytes, sha256, rows]) saves rehashing them."""
    parts = {}
    for p in sorted(orders.glob("day=*/part-*.parquet")):
        rel = p.relative_to(orders).as_posix()
        c = cache.get(rel)
        if not c or c[0] != p.stat().st_size:
            c = cache[rel] = [p.stat().st_size, sha_file(p), pq.read_metadata(p).num_rows]
        parts[rel] = c
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


def busy_chunks(orders: Path, chunks: list[tuple[str, int, int]]) -> Counter:
    """chunk id -> verified maker rows created inside it (a chunk with none is not run)."""
    n: Counter = Counter()
    if not chunks:
        return n
    starts = [a * 1000 for _, a, _ in chunks]
    end = chunks[-1][2] * 1000
    for p in sorted(orders.glob("day=*/part-*.parquet")):
        t = pq.read_table(p, columns=["role", "created_ms", "verified"]).to_pydict()
        for role, c, v in zip(t["role"], t["created_ms"], t["verified"]):
            if role == "maker" and v is True and c is not None and starts[0] <= c < end:
                n[chunks[bisect_right(starts, c) - 1][0]] += 1
    return n


# ---------------------------------------------------------------- step: ordertape
def verified_shares(c: Cfg) -> dict[str, dict]:
    """Per wallet, of the fill store's (tx, order_id) fills in [from, to) whose tx is in the
    order tape, the share with a decoded order of that id. Computed from the two stores, so it
    does not depend on how many ordertape calls built the tape."""
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
             "d AS (SELECT DISTINCT tx, order_id FROM ot) "
             "SELECT f.address, count(*), count(d.order_id) FROM f "
             "JOIN (SELECT DISTINCT tx FROM d) t ON t.tx = f.tx "
             "LEFT JOIN d ON d.tx = f.tx AND d.order_id = f.oid GROUP BY 1 ORDER BY 1")
        return {a: {"fills_expected": e, "fills_matched": m, "share": round(m / e, 6)}
                for a, e, m in con.execute(q, [sorted(c.wallets), c.t_from, c.t_to]).fetchall()}
    finally:
        con.close()


def step_ordertape(c: Cfg, code: dict[str, str], transport: Callable) -> dict:
    sp = c.out / "_state" / "ordertape.json"
    prev = _load(sp) or {}
    store_state = c.store / STATE
    key = sha_obj({"wallets": c.wallets, "from": c.t_from, "to": c.t_to, "max_txs": c.max_txs,
                   "store": sha_file(store_state) if store_state.is_file() else None,
                   "code": {f: code[f] for f in CODE["ordertape"]}})
    status = "skipped" if prev else "absent"
    if "ordertape" in c.steps and ("ordertape" in c.force or prev.get("key") != key
                                   or not prev.get("complete") or not c.orders.is_dir()):
        t0 = time.monotonic()
        con = _connect(c.store, "256MB", 2)[0]
        try:
            todo = ordertape.select_txs(con, list(c.wallets), c.t_from, c.t_to, c.max_txs) if con else []
            pool = ordertape.Pool(list(c.rpcs), transport, rate=c.rate, max_requests=c.max_requests)
            res = ordertape.run(todo, pool, c.orders,
                                expected=lambda txs: ordertape.goldsky_ids(con, list(c.wallets), txs))
        finally:
            if con is not None:
                con.close()
        prev = {"key": key, "summary": res, "wall_s": round(time.monotonic() - t0, 1),
                "complete": res["stopped"] is None and not res["txs_failed"] and not res["txs_not_attempted"],
                "parts": prev.get("parts", {})}
        status = "ran"
    elif "ordertape" not in c.steps:
        status = "kept" if c.orders.is_dir() else "absent"
    parts = prev.setdefault("parts", {})
    n_cached = len(parts)
    out = orders_digest(c.orders, parts) if c.orders.is_dir() else None
    if status == "ran" or len(parts) != n_cached:  # keep the part hashes: parts are written once
        _write_json(sp, prev)
    # an order tape this run directory never fetched (--orders) has no key here: kept, not judged
    return {"status": status, "stale": bool(prev.get("key")) and prev["key"] != key,
            "wall_s": prev.get("wall_s"), "complete": prev.get("complete"),
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


def _chunk_ordermatch(c: Cfg, d: Path, a: int, b: int, streams: list[str]) -> dict:
    """Run ordermatch on one chunk and write the tool's own three files plus the candidates'
    life histogram per wallet (integer ms -> count), which the merge needs for exact quantiles."""
    doc, rows, quotes = ordermatch.run(c.data, c.orders, a, b, list(c.wallets), streams=streams)
    thin = {w for w, d in doc["wallets"].items() if d["fingerprint"] and d["maker_orders"] < MIN_FP_ORDERS}
    for w in thin:  # a fingerprint from a handful of orders is noise (see FINGERPRINT FLOOR)
        doc["wallets"][w]["fingerprint"] = None
        doc["wallets"][w]["fingerprint_dropped"] = f"fewer than {MIN_FP_ORDERS} maker orders in the chunk"
    quotes = [r for r in quotes if r["wallet"] not in thin]
    hist: dict[str, Counter] = defaultdict(Counter)
    for r in quotes:
        if r["rest_known"]:
            hist[r["wallet"]][r["rest_max_ms"]] += 1
    ws = doc["wallets"].values()
    return {"outputs": {
        "ordermatch.json": _write_json(d / "ordermatch.json", doc),
        "orders.jsonl": _write_jsonl(d / "orders.jsonl", rows),
        "quotes.jsonl": _write_jsonl(d / "quotes.jsonl", quotes),
        "quotes_rest_ms.json": _write_json(d / "quotes_rest_ms.json", {
            w: {str(k): v for k, v in sorted(h.items())} for w, h in sorted(hist.items())})},
        "counts": {"orders": doc["orders"], "tape": doc["tape"],
                   "maker_orders": sum(w["maker_orders"] for w in ws),
                   "observable": sum(w["observable"] for w in ws),
                   "matched_all": sum(w["matched_all"] for w in ws),
                   "fingerprints_dropped": len(thin),
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
    ident = n["own"] + n["other_known"]
    return {k: n[k] for k in FP_COUNTS} | {
        "control_per_token_hour": _r(rate), "expected_by_chance": _r(exp),
        "net_estimate": None if exp is None else _r(max(0.0, n["candidates"] - n["own_unmatched"] - exp)),
        "identity_precision_upper": _r(n["own"] / ident) if ident else None}


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
            "fingerprint": merge_fingerprint([(cid, d["fingerprint"]) for cid, d in docs if d.get("fingerprint")], rest)}


def _partition(files: list[Path], field_: str, wallets: set[str], dst: Path, name: str) -> dict[str, int]:
    """Split chunk JSONL rows by ``row[field_]`` into ``dst/<wallet>/<name>`` (rewritten from
    scratch); rows of other makers stay in the chunk files only. Returns rows per wallet."""
    n: dict[str, int] = dict.fromkeys(sorted(wallets), 0)
    fhs = {}
    try:
        for w in n:
            (dst / w).mkdir(parents=True, exist_ok=True)
            fhs[w] = open(dst / w / name, "wb")
        for f in files:
            for r in _jsonl(f):
                w = r.get(field_)
                if isinstance(w, str) and w in fhs:
                    fhs[w].write(orjson.dumps(r) + b"\n")
                    n[w] += 1
    finally:
        for fh in fhs.values():
            fh.close()
    return n


def _wallet_files(out: Path, wallets: tuple[str, ...], name: str, rows: dict[str, int]) -> dict[str, dict]:
    return {f"wallets/{w}/{name}": {"sha256": sha_file(out / "wallets" / w / name),
                                    "bytes": (out / "wallets" / w / name).stat().st_size, "rows": rows[w]}
            for w in wallets}


def _chunked(c: Cfg, step: str, code: dict[str, str], chunks: list[tuple[str, int, int]], busy: Counter,
             key_of: Callable[[str, int, int], dict | None], work: Callable[[Path, int, int], dict]) -> list[dict]:
    """Run ``work`` on every busy chunk whose key changed; returns one record per chunk."""
    recs = []
    for cid, a, b in chunks:
        rec: dict = {"chunk": cid, "from": a, "to": b}
        if not busy[cid]:
            recs.append(rec | {"status": "empty"})
            continue
        d = c.out / "chunks" / cid / step
        prev = _load(d / "_done.json")
        inputs = key_of(cid, a, b)
        if inputs is None:
            recs.append(rec | {"status": "missing_input"})
            continue
        key = sha_obj(inputs | {"from": a, "to": b, "code": {f: code[f] for f in CODE[step]}})
        if step in c.steps and (step in c.force or not _fresh(prev, key, d)):
            t0 = time.monotonic()
            prev = work(d, a, b) | {"key": key, "inputs": inputs}
            prev["wall_s"] = round(time.monotonic() - t0, 1)
            _write_json(d / "_done.json", prev)
            status = "ran"
        elif prev is None:
            status = "absent"
        else:
            status = "skipped" if step in c.steps else "kept"
        recs.append(rec | {"status": status} | (
            {"stale": prev["key"] != key, "key": prev["key"], "wall_s": prev["wall_s"],
             "inputs": prev["inputs"], "outputs": prev["outputs"], "counts": prev["counts"]} if prev else {}))
    return recs


def _have(recs: list[dict]) -> list[dict]:
    return [r for r in recs if "outputs" in r]


def step_ordermatch(c: Cfg, code: dict[str, str], chunks: list[tuple[str, int, int]], busy: Counter,
                    orders: dict | None, streams: list[str], tcache: dict) -> dict:
    recs = _chunked(c, "ordermatch", code, chunks, busy,
                    lambda cid, a, b: None if orders is None else {
                        "orders": orders["sha256"], "wallets": c.wallets,
                        "params": ordermatch.Params().__dict__, "max_age_s": ordermatch.MAX_AGE_S,
                        "tape": tape_hashes(c.data, streams, a, b, tcache)},
                    lambda d, a, b: _chunk_ordermatch(c, d, a, b, streams))
    done = _have(recs)
    sp, out = c.out / "_state" / "ordermatch.json", c.out / "ordermatch"
    key = sha_obj([r["key"] for r in done] + [c.wallets])
    prev = _load(sp)
    merged = "skipped"
    if not done and prev is None:
        merged = "absent"
    elif "ordermatch" in c.steps and not (_fresh(prev, key, c.out) and "ordermatch" not in c.force):
        t0 = time.monotonic()
        dirs = [c.out / "chunks" / r["chunk"] / "ordermatch" for r in done]
        n_rows = _partition([d / "orders.jsonl" for d in dirs], "maker", set(c.wallets), c.out / "wallets", "orders.jsonl")
        docs = [(r["chunk"], orjson.loads((d / "ordermatch.json").read_bytes())) for r, d in zip(done, dirs)]
        hists = [orjson.loads((d / "quotes_rest_ms.json").read_bytes()) for d in dirs]
        wallets = {}
        for w in c.wallets:
            rest: Counter = Counter()
            for h in hists:
                rest.update({int(k): v for k, v in h.get(w, {}).items()})
            wallets[w] = merge_match(w, [(cid, d["wallets"][w]) for cid, d in docs if w in d["wallets"]],
                                     list(_jsonl(c.out / "wallets" / w / "orders.jsonl")), rest)
        doc = {"params": {"from": c.t_from, "to": c.t_to, "chunk_s": c.chunk_s,
                          "chunks": [r["chunk"] for r in done], "per_chunk": docs[0][1]["params"] if docs else None},
               "merge": "counts summed over chunks; rates recomputed from the sums; quantiles from the pooled rows",
               "orders": dict(sum((Counter(d["orders"]) for _, d in docs), Counter())),
               "wallet_rows": n_rows, "wallets": wallets}
        prev = {"key": key, "wall_s": round(time.monotonic() - t0, 1),
                "outputs": {"ordermatch/ordermatch.json": _write_json(out / "ordermatch.json", doc)}
                | _wallet_files(c.out, c.wallets, "orders.jsonl", n_rows)}
        _write_json(sp, prev)
        merged = "ran"
    elif "ordermatch" not in c.steps:
        merged = "kept"
    ran = merged == "ran" or any(r["status"] == "ran" for r in recs)
    return {"status": "ran" if ran else merged,
            "stale": bool(prev and prev["key"] != key) or any(r.get("stale") for r in recs),
            "wall_s": round(sum(r.get("wall_s", 0) for r in recs) + (prev or {}).get("wall_s", 0), 1),
            "chunks": recs, "outputs": (prev or {}).get("outputs", {})}


# ---------------------------------------------------------------- step: orderreplay
def _chunk_orderreplay(c: Cfg, d: Path, a: int, b: int, streams: list[str]) -> dict:
    """Replay one chunk. Order rows are written with their maker; fingerprint-candidate rows are
    reduced to counts per wallet (they are many, and only their counts are used)."""
    om = c.out / "chunks" / d.parent.name / "ordermatch"
    doc, rows = orderreplay.run(c.data, om, a, b, streams)
    maker = {r["order_id"]: r["maker"] for r in _jsonl(om / "orders.jsonl")}
    owner: dict[str, list[str]] = defaultdict(list)  # a candidate can be on two wallets' lists
    for r in _jsonl(om / "quotes.jsonl"):
        owner[f"{r['token_id']}:{r['price']}:{r['venue_ms']}:{r['tw_ns']}"].append(r["wallet"])
    orders = []
    nq: dict[str, dict[str, Counter]] = defaultdict(lambda: {m: Counter() for m in MODES})
    for r in rows:
        if r["kind"] == "order":
            orders.append(r | {"maker": maker.get(r["id"])})
            continue
        for w in owner.get(r["id"], ()):
            for m in MODES:
                n = nq[w][m]
                n["n"] += 1
                n["twin_filled"] += r[m]["filled"] > 0
                n["quoted_shares"] += r["size"]
                n["phantom_shares"] += r[m]["filled"]
                if r[m]["by_source"]:
                    n["src:" + "+".join(r[m]["by_source"])] += 1
    doc["quotes_by_wallet"] = {w: {m: dict(n) for m, n in v.items()} for w, v in sorted(nq.items())}
    g = doc["adapter_gate"]
    return {"outputs": {"orderreplay.json": _write_json(d / "orderreplay.json", doc),
                        "orderreplay.jsonl": _write_jsonl(d / "orderreplay.jsonl", orders)},
            "counts": {"counts": doc["counts"], "routed_share": g["routed_share"],
                       "delivered_share": g["delivered_share"],
                       "bracket_violations": {k: v for k, v in doc["bracket_violations"].items() if k != "note"}}}


def _never_filled(ns: list[dict[str, dict]]) -> dict | None:
    """Sum per-mode candidate counts into the shape of orderreplay's never_filled estimate."""
    out = {}
    for m in MODES:
        n: Counter = Counter()
        for x in ns:
            n.update(x.get(m, {}))
        if not n["n"]:
            return None
        out[m] = {"n": n["n"], "twin_filled": n["twin_filled"],
                  "phantom_fill_rate": _r(n["twin_filled"] / n["n"], 5),
                  "quoted_shares": _r(n["quoted_shares"]), "phantom_shares": _r(n["phantom_shares"]),
                  "by_twin_source": {k[4:]: v for k, v in n.items() if k.startswith("src:")}}
    return out | {"note": "ESTIMATE: fingerprint candidates ordermatch classified "
                          "cancelled:exact_decrease; not identified orders"}


def _sum_ints(docs: list[dict]) -> dict:
    """Sum the numeric leaves of dicts with the same shape (one level of nesting)."""
    out: dict = {}
    for d in docs:
        for k, v in d.items():
            if isinstance(v, dict):
                out[k] = dict(Counter(out.get(k, {})) + Counter(v))
            elif isinstance(v, int | float) and not isinstance(v, bool):
                out[k] = out.get(k, 0) + v
    return out


def merge_replay_cohort(docs: list[dict], by_market: dict[str, dict[str, list[float]]]) -> dict:
    """Cohort document from the chunk documents (sums of counts) and the pooled per-market sums."""
    g = _sum_ints([{k: v for k, v in d["adapter_gate"].items() if not k.endswith("_share")} for d in docs])
    seen, routed = g.get("print_seen_in_replay", 0), g.get("routed_to_order_price_and_side", 0)
    cond = {}
    for m in MODES:
        n = _sum_ints([{k: d["conditional_on_real_fill"][m][k] for k in (
            "n", "real_shares", "twin_shares", "exact", "underfilled", "missed", "phantom", "phantom_shares")}
            for d in docs])
        pseudo = [{"market": k, m: {"filled": v[0]}, "real_filled": v[1]} for k, v in by_market[m].items()]
        cond[m] = n | {"markets": len(pseudo),
                       "ratio": _r(n["twin_shares"] / n["real_shares"], 4) if n.get("real_shares") else None,
                       "ratio_ci95": orderreplay.boot_ci(pseudo, m) if n.get("real_shares") else None}
    return {"limit": orderreplay.LIMIT, "counts": _sum_ints([d["counts"] for d in docs]),
            "adapter_gate": g | {"routed_share": _r(routed / seen, 4) if seen else None,
                                 "delivered_share": _r(g.get("delivered_to_live_quote", 0) / routed, 4) if routed else None},
            "conditional_on_real_fill": cond,
            "bracket_violations": {"pessimistic_overfilled": cond["pessimistic"].get("phantom", 0),
                                   "optimistic_underfilled": cond["optimistic"].get("underfilled", 0)},
            "never_filled_quotes_estimate": _never_filled(
                [d.get("never_filled_quotes_estimate") or {} for d in docs])}


def step_orderreplay(c: Cfg, code: dict[str, str], chunks: list[tuple[str, int, int]], busy: Counter,
                     om: dict, streams: list[str], tcache: dict) -> dict:
    om_out = {r["chunk"]: r["outputs"] for r in _have(om["chunks"])}
    recs = _chunked(c, "orderreplay", code, chunks, busy,
                    lambda cid, a, b: None if cid not in om_out else {
                        "orders.jsonl": om_out[cid]["orders.jsonl"]["sha256"],
                        "quotes.jsonl": om_out[cid]["quotes.jsonl"]["sha256"],
                        "tape": tape_hashes(c.data, streams, a, b, tcache)},
                    lambda d, a, b: _chunk_orderreplay(c, d, a, b, streams))
    done = _have(recs)
    sp, out = c.out / "_state" / "orderreplay.json", c.out / "orderreplay"
    key = sha_obj([r["key"] for r in done] + [c.wallets])
    prev = _load(sp)
    merged = "skipped"
    if not done and prev is None:
        merged = "absent"
    elif "orderreplay" in c.steps and not (_fresh(prev, key, c.out) and "orderreplay" not in c.force):
        t0 = time.monotonic()
        dirs = [c.out / "chunks" / r["chunk"] / "orderreplay" for r in done]
        n_rows = _partition([d / "orderreplay.jsonl" for d in dirs], "maker", set(c.wallets), c.out / "wallets",
                            "orderreplay.jsonl")
        docs = [orjson.loads((d / "orderreplay.json").read_bytes()) for d in dirs]
        by_market: dict[str, dict[str, list[float]]] = {m: defaultdict(lambda: [0.0, 0.0]) for m in MODES}
        wallets = {}
        for w in c.wallets:
            rows = list(_jsonl(c.out / "wallets" / w / "orderreplay.jsonl"))
            mp = c.out / "wallets" / w / "orders.jsonl"
            matched = list(_jsonl(mp)) if mp.is_file() else []
            wrong = sum(r["fill_before_placement"] for r in matched)
            d = orderreplay.summarize(rows, Counter({
                "orders": len(matched), "orders_wrong_match_skipped": wrong,
                "placement_not_found": len(matched) - wrong - len(rows)}))
            nf = _never_filled([x["quotes_by_wallet"].get(w, {}) for x in docs])
            if nf:
                d["never_filled_quotes_estimate"] = nf
            wallets[w] = d
            for r in rows:
                if r["end_known"] and not r["cut"]:
                    for m in MODES:
                        by_market[m][r["market"]][0] += r[m]["filled"]
                        by_market[m][r["market"]][1] += r["real_filled"]
        doc = {"params": {"from": c.t_from, "to": c.t_to, "chunk_s": c.chunk_s, "chunks": [r["chunk"] for r in done]},
               "merge": "per wallet: orderreplay.summarize on the wallet's order rows from all chunks; "
                        "cohort: chunk counts summed, CI over the cohort wallets' pooled markets",
               "cohort": merge_replay_cohort(docs, by_market), "wallets": wallets}
        prev = {"key": key, "wall_s": round(time.monotonic() - t0, 1),
                "outputs": {"orderreplay/orderreplay.json": _write_json(out / "orderreplay.json", doc)}
                | _wallet_files(c.out, c.wallets, "orderreplay.jsonl", n_rows)}
        _write_json(sp, prev)
        merged = "ran"
    elif "orderreplay" not in c.steps:
        merged = "kept"
    ran = merged == "ran" or any(r["status"] == "ran" for r in recs)
    return {"status": "ran" if ran else merged,
            "stale": bool(prev and prev["key"] != key) or any(r.get("stale") for r in recs),
            "wall_s": round(sum(r.get("wall_s", 0) for r in recs) + (prev or {}).get("wall_s", 0), 1),
            "chunks": recs, "outputs": (prev or {}).get("outputs", {})}


# ---------------------------------------------------------------- step: profile
def step_profile(c: Cfg, code: dict[str, str], orders: dict | None) -> dict:
    sp, out = c.out / "_state" / "profile.json", c.out / "profile"
    omp = c.out / "ordermatch" / "ordermatch.json"
    dos = {w: sha_file(c.dossiers / f"{w}.json") for w in c.wallets
           if c.dossiers and (c.dossiers / f"{w}.json").is_file()}
    inputs = {"orders": orders and orders["sha256"], "ordermatch": sha_file(omp) if omp.is_file() else None,
              "dossiers": dos, "wallets": c.wallets}
    key = sha_obj(inputs | {"code": {f: code[f] for f in CODE["profile"]}})
    prev = _load(sp)
    status = "skipped" if prev else "absent"
    if "profile" in c.steps and orders is not None and ("profile" in c.force or not _fresh(prev, key, c.out)):
        t0 = time.monotonic()
        for i in range(0, len(c.wallets), PROFILE_BATCH):
            profile.run(c.orders, out, omp if omp.is_file() else None, c.dossiers,
                        list(c.wallets[i:i + PROFILE_BATCH]))
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
    elif "profile" not in c.steps and prev:
        status = "kept"
    return {"status": status, "stale": bool(prev and prev["key"] != key),
            "wall_s": (prev or {}).get("wall_s"), "inputs": inputs, "outputs": (prev or {}).get("outputs", {})}


# ---------------------------------------------------------------- gates
def gate(table: dict[str, dict], name: str, value: float | None, n: int | None) -> dict:
    g = table[name]
    lim = {k: g[k] for k in ("min", "max") if k in g}
    if value is None or not n or n < g["min_n"]:
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


def untrusted(gates: dict[str, dict], stale: frozenset[str] = frozenset()) -> dict[str, list[str]]:
    """step -> the gates (its own and its upstream steps') that are not ``pass``, plus
    ``<step>:stale`` for a step (own or upstream) whose kept output was made from other inputs or
    code than the current ones; [] = trusted."""
    return {s: [f"{u}:stale" for u in (*UPSTREAM[s], s) if u in stale]
            + [f"{k}:{g['status']}" for k, g in gates.items()
               if g["step"] in (s, *UPSTREAM[s]) and g["status"] != "pass"] for s in STEPS}


# ---------------------------------------------------------------- report
def _mark(x, bad: list[str]) -> str:
    """Cell text. UNTRUSTED = a gate failed or an input is stale; NO GATE = a gate could not be
    evaluated (too few cases, step not run), so nothing vouches for the number."""
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
            "match": o and {k: o[k] for k in ("maker_orders", "observable", "matched", "match_rate",
                                              "placebo_matched", "placebo_rate")},
            "rest_ms_p50": o and {k: v and v["p50"] for k, v in o["rest_ms_known"].items()},
            "unfilled_quote_life_ms_p50": ((o or {}).get("fingerprint") or {}).get("candidate_rest_ms_cancelled", None)
            and o["fingerprint"]["candidate_rest_ms_cancelled"]["p50"],
            "footprint": ft and {"fill_ratio": ft["fill_ratio"], "chance_share": ft["chance_share"],
                                 "filled_quotes": ft["filled_quotes"],
                                 "unfilled_net_estimate": ft["unfilled_net_estimate"],
                                 # not measured: non-cohort makers are unverified and left out (see ordermatch)
                                 "identity_precision_upper": None,
                                 "other_cohort_makers_proven": o["fingerprint"]["total"]["other_known"],
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
    cohort = {
        "wallets": len(c.wallets), "wallets_with_verified_orders": sum(bool(r["verified_orders"]) for r in rows),
        "trusted_wallets": {s: sum(not r["untrusted"][s] for r in rows) for s in STEPS},
        "match": dict(tot) | {"match_rate": _r(tot["matched"] / tot["observable"]) if tot["observable"] else None,
                              "placebo_rate": _r(tot["placebo_matched"] / tot["observable"]) if tot["observable"] else None},
        "twin": {m: {k: cr["conditional_on_real_fill"][m].get(k) for k in ("n", "markets", "ratio", "ratio_ci95")}
                 for m in MODES} if cr else None,
        "adapter_gate": cr.get("adapter_gate"), "bracket_violations": cr.get("bracket_violations"),
        "copyability": dict(Counter(r["copyability"] for r in rows if r["copyability"])),
        "software_clusters": len(coh.get("software_clusters", {})),
        "operator_clusters": len(coh.get("operator_clusters", {}))}
    doc = {"run_id": man["run_id"], "git_commit": man["git_commit"], "range": man["range"],
           "cohort_sha256": man["cohort"]["sha256"], "gates_provisional": man["gate_table"],
           "wallets": rows, "cohort": cohort,
           "ledger_status": {k: dict(v) for k, v in status.items()}, "unknown_resolve_by": resolve}

    L = [f"# Reconstruction {man['run_id']}", "",
         f"commit `{man['git_commit']}` | cohort `{man['cohort']['sha256'][:12]}` | "
         f"{man['range']['from_utc']} to {man['range']['to_utc']} | manifest: `manifest.json`", "",
         "Read first: UNTRUSTED = a gate behind the cell failed or its input is stale; NO GATE = a gate "
         "could not be evaluated (too few cases), so nothing vouches for the number. Thresholds are "
         "PROVISIONAL (table in the manifest). Twin ratios are conditional on a real fill, not fill probabilities. "
         "Footprints are estimates with the stated chance share. Only filled orders are on chain.", "",
         "| wallet | role | mechanism | verified orders (maker) | verified share | matched / observable (rate) | "
         "placebo rate | rest ms p50 filled / cancelled | fill ratio (chance share) | twin pess [CI] | "
         "twin opt [CI] | coverage | copyability | software / operator cluster | not passed |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        u = r["untrusted"]
        m, tw, ft = r["match"] or {}, r["twin"] or {}, r["footprint"] or {}
        rest = r["rest_ms_p50"] or {}

        def twin(mode: str, tw=tw, u=u) -> str:
            t = tw.get(mode) or {}
            return _mark(f"{t.get('ratio')} {t.get('ratio_ci95')} n={t.get('n')}" if t else None, u["orderreplay"])

        sc, oc = r["software_cluster"] or {}, r["operator_cluster"] or {}
        L.append(
            f"| {r['wallet']} | {r['role'] or ''} | {_mark(r['mechanism'], [])} | "
            f"{_mark(r['verified_orders'], u['ordertape'])} ({r['verified_maker_orders'] or 0}) | "
            f"{_mark(r['verified_share'], [])} | "
            f"{_mark(f'''{m.get('matched')} / {m.get('observable')} ({m.get('match_rate')})''' if m else None, u['ordermatch'])} | "
            f"{_mark(m.get('placebo_rate'), u['ordermatch'])} | "
            f"{_mark(f'''{rest.get('filled')} / {rest.get('cancelled')}''' if rest else None, u['ordermatch'])} | "
            f"{_mark(f'''{ft.get('fill_ratio')} ({ft.get('chance_share')})''' if ft else None, u['ordermatch'])} | "
            f"{twin('pessimistic')} | {twin('optimistic')} | "
            f"{_mark(r['coverage'], u['profile'])} | {_mark(r['copyability'], u['profile'])} | "
            f"{sc.get('key')} ({sc.get('size')}) / {oc.get('key')} ({oc.get('size')}) | "
            f"{'; '.join(sorted({g for v in u.values() for g in v}))} |")
    L += ["", "## Cohort totals", ""] + [f"- {k}: {_mark(v, [])}" for k, v in cohort.items()]
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
    ``c.out``, and return the manifest. ``transport`` is ordertape's JSON-RPC transport."""
    table = gates or GATES
    t0 = time.monotonic()
    c.out.mkdir(parents=True, exist_ok=True)
    code = code_sha()
    commit, src = git_commit()
    streams = sorted(d.name for d in c.data.iterdir() if d.name.startswith("clob_")) if c.data.is_dir() else []
    chunks = chunk_list(c.t_from, c.t_to, c.chunk_s)
    tcache: dict = {}

    steps: dict[str, dict] = {"ordertape": step_ordertape(c, code, transport)}
    orders = steps["ordertape"]["outputs"]["orders"]
    busy = busy_chunks(c.orders, chunks) if orders else Counter()
    steps["ordermatch"] = step_ordermatch(c, code, chunks, busy, orders, streams, tcache)
    steps["orderreplay"] = step_orderreplay(c, code, chunks, busy, steps["ordermatch"], streams, tcache)
    steps["profile"] = step_profile(c, code, orders)

    shares = verified_shares(c) if orders else {}
    om = (_load(c.out / "ordermatch" / "ordermatch.json") or {}).get("wallets", {})
    rp = (_load(c.out / "orderreplay" / "orderreplay.json") or {}).get("wallets", {})
    wallets = {}
    stale = frozenset(s for s in STEPS if steps[s]["stale"])
    made = {k: v for s in STEPS for k, v in steps[s]["outputs"].items() if isinstance(v, dict) and "sha256" in v}
    for w in c.wallets:
        g = wallet_gates(table, shares.get(w), om.get(w), rp.get(w))
        files = (f"wallets/{w}/orders.jsonl", f"wallets/{w}/orderreplay.jsonl",
                 f"profile/{w}.ledger.json", f"profile/{w}.ledger.md")
        wallets[w] = {"role": c.roles.get(w), "gates": g, "untrusted": untrusted(g, stale),
                      "outputs": {"orders": f"{c.orders} (rows with maker == wallet and verified == True)"}
                      | {f: made[f] for f in files if f in made}}
    cohort_doc = orjson.loads(c.cohort.read_bytes())
    fmt = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))  # noqa: E731
    man = {
        "run_id": c.out.name, "written_utc": fmt(int(time.time())),
        "git_commit": commit, "git_commit_source": src,
        "code_sha256": {"files": code, "all": sha_obj(code)},
        "cohort": {"path": str(c.cohort), "sha256": sha_file(c.cohort),
                   "declared_sha256": cohort_doc.get("sha256"),
                   "candidates": len(cohort_doc.get("candidates", [])),
                   "controls": len(cohort_doc.get("controls", []))},
        "range": {"from": c.t_from, "to": c.t_to, "from_utc": fmt(c.t_from), "to_utc": fmt(c.t_to)},
        "args": {"data": str(c.data), "store": str(c.store), "orders": str(c.orders),
                 "dossiers": str(c.dossiers) if c.dossiers else None, "max_txs_per_wallet": c.max_txs,
                 "chunk_hours": c.chunk_s / 3600, "steps": c.steps, "force": sorted(c.force),
                 "streams": streams},
        "gate_table": {"provisional": True, "gates": table,
                       "rule": "a wallet's output of a step is trusted only when every gate of that step "
                               "and of its upstream steps is pass; not_evaluable is not pass",
                       "upstream": UPSTREAM},
        "chunk_merge": "per wallet: counts summed over chunks, rates recomputed from the sums, quantiles "
                       "from the pooled rows (never averages of chunk rates); see reconstruct.py",
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-reconstruct", description=(
        "cohort + time range -> per wallet: verified order tape, book match, twin replay, ledger; "
        "resumable, with a manifest (see docs/RECONSTRUCTION.md)"))
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--lab", type=Path, required=True, help="lab root (default output, store, dossiers)")
    ap.add_argument("--store", type=Path, help="fill store (default: <lab>/fills); must be compacted already")
    ap.add_argument("--cohort", type=Path, required=True, help="frozen cohort JSON (candidates, controls)")
    ap.add_argument("--from", dest="t_from", type=_ts, required=True, help="unix s or ISO UTC, inclusive")
    ap.add_argument("--to", dest="t_to", type=_ts, required=True, help="exclusive")
    ap.add_argument("--wallets", help="file with one address per line, or a comma-separated list "
                    "(default: every candidate and control of the cohort)")
    ap.add_argument("--max-txs-per-wallet", type=int, help="ordertape: keep each wallet's most recent N txs")
    ap.add_argument("--steps", default=",".join(STEPS), help=f"comma-separated subset of {','.join(STEPS)}")
    ap.add_argument("--force", action="append", default=[], choices=STEPS, help="rerun this step; repeatable")
    ap.add_argument("--chunk-hours", type=int, default=1, help="ordermatch / orderreplay chunk length")
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
    if a.t_to <= a.t_from or a.chunk_hours < 1:
        ap.error("--to must be after --from and --chunk-hours >= 1")
    cohort = orjson.loads(a.cohort.read_bytes())
    roles = {w.lower(): "ctrl" for w in cohort.get("controls", [])} | {
        w.lower(): "cand" for w in cohort.get("candidates", [])}
    wallets = tuple(sorted(set(ordertape._list(a.wallets)))) if a.wallets else tuple(sorted(roles))
    if not wallets:
        ap.error("no wallets: the cohort has no candidates or controls and --wallets is empty")
    opts = {"wallets": a.wallets and wallets, "max_txs": a.max_txs_per_wallet, "chunk_hours": a.chunk_hours}
    fmt = lambda t: time.strftime("%Y%m%dT%H%MZ", time.gmtime(t))  # noqa: E731
    run_id = (f"{(cohort.get('sha256') or sha_file(a.cohort))[:12]}-{fmt(a.t_from)}-{fmt(a.t_to)}"
              + (f"-{sha_obj(opts)[:8]}" if opts != {"wallets": None, "max_txs": None, "chunk_hours": 1} else ""))
    out = a.out or a.lab / "reconstruct" / run_id
    dossiers = a.dossiers or (a.lab / "dossiers" if (a.lab / "dossiers").is_dir() else None)
    c = Cfg(data=a.data, store=a.store or a.lab / "fills", cohort=a.cohort, t_from=a.t_from, t_to=a.t_to,
            out=out, orders=a.orders or out / "ordertape", wallets=wallets, roles=roles, dossiers=dossiers,
            max_txs=a.max_txs_per_wallet, chunk_s=a.chunk_hours * 3600, steps=steps, force=frozenset(a.force),
            rpcs=tuple(a.rpc or ordertape.DEFAULT_RPCS), rate=a.rate, max_requests=a.max_requests)
    man = run(c)
    for s in STEPS:
        st = man["steps"][s]
        ch = Counter(r["status"] for r in st.get("chunks", []))
        print(f"{s:12} {st['status']:8} wall {st['wall_s']} s" + (f" chunks {dict(ch)}" if ch else "")
              + (f" untrusted wallets {len(man['untrusted_wallets'][s])}/{len(wallets)}"))
    ot = man["steps"]["ordertape"]
    if ot["complete"] is False:
        print("ordertape is INCOMPLETE (txs failed or not attempted): run the same command again", file=sys.stderr)
    print(f"-> {out / 'report.md'}\n-> {out / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
