"""Cohort-wide absorption (Phase B1): ``absorb.absorb_wallet`` for every member of a frozen cohort.

Run state lives in ``--out DIR``:
- ``run.json``: the manifest (cohort hash, UTC window, resolved blocks). A resume with another
  cohort or window is refused.
- ``_chain/window.jsonl``: the shared block-header cache of the window search;
  ``_chain/wallet-<addr>.jsonl``: one RPC cache per wallet, so memory holds one wallet's chain
  data at a time and a retried wallet repeats no request it already made.
- ``wallet-<addr>.json``: the final ledger of a completed wallet; ``wallet-<addr>.attempt.json``:
  the last transport-incomplete attempt.
- ``done.jsonl``: one line per completed wallet with its cohort row and a hash of its inputs: its
  Goldsky pagination records, the fill-store state, the token map and the order-tape parts. A
  completed wallet is never fetched again; when an input changes (a re-pull, a compaction, a
  larger token map) it is recomputed from its RPC cache.
- ``cohort.tsv`` / ``cohort.json``: the cohort table, rewritten after every wallet batch.
- ``.lock``: held with ``flock`` for the whole run; a second concurrent run exits 4.

A wallet is *completed* when its absorption ended without a transport failure (``chain_error``
is None or a validation fault): its status, ``reconciled`` or ``not_reconciled`` with named causes,
is then a property of the data. A transport failure (budget, endpoints down, timeouts, refusals
with no capable endpoint) leaves it *pending* for the next run. Budget exhaustion, all
endpoints down, or no state endpoint serving archive reads stops the run, so later wallets are
not burned on a dead pool or route. Exit 0: every wallet done; 3: wallets remain (resume with the
same command); 2: bad input or window; 4: locked.

``--plan`` makes no network request. It estimates the run's RPC requests and the Goldsky rows the
fixed-end re-pull needs, and prints that ``doppel-goldsky cohort`` command.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import math
import os
import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import orjson

from ..tape.segment import atomic_write
from .absorb import (DEFAULT_CHUNK, Chain, _span_failure, absorb_wallet, cohort_row, load_pairs,
                     rpc_routes, signer_links)
from .fillstore import _connect, iter_wallet_fills
from .goldsky import pagination_state, terminal_page
from .ordertape import ARCHIVE_REFUSED, BudgetExhausted, EndpointsDown, Pool, RpcError, http_transport

STOPS = ("BudgetExhausted", "EndpointsDown", ARCHIVE_REFUSED)  # a dead pool or route stops the run
LOG_QUERIES = 5  # eth_getLogs filters per chunk (absorb.scan)
STATE_CALLS = 6  # balanceOfBatch + two cash balanceOf, at b0 - 1 and at b1
NOMINAL_BLOCK_S = 2.0  # Polygon PoS block time, for planning without a chain read
# Non-fill share transactions per Goldsky fill on the 2026-11-26 reconciled day:
# control 249/2082 = 0.12, candidate (364 + 278)/4556 = 0.14. Each needs one receipt.
RECEIPTS_PER_FILL = (0.12, 0.15)
HEAD_SEARCH_MAX = 2 * (2 + 2 * 27) + 1  # two block_at searches over < 2**27 blocks, plus the head


def transient(err: str | None) -> bool:
    return err is not None and (err in STOPS or err in ("no_attempt", "null_result")
                                or _span_failure(err))


def load_cohort(path: Path) -> tuple[str, list[tuple[str, str]]]:
    doc = orjson.loads(Path(path).read_bytes())
    members = [("cand", w.lower()) for w in doc["candidates"]] + [("ctrl", w.lower()) for w in doc["controls"]]
    wallets = [w for _, w in members]
    if len(set(wallets)) != len(wallets) or any(not re.fullmatch(r"0x[0-9a-f]{40}", w) for w in wallets):
        raise ValueError("cohort wallets must be distinct 20-byte addresses")
    return doc["sha256"], members


def _file_sha(path: Path | None) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path is not None and path.exists() else None


def inputs_digest(store: Path, tokens: Path | None, orders: Path | None) -> str:
    """Digest of the local inputs every wallet's ledger depends on: the fill-store state (changes
    on every compaction), the token map and the committed order-tape parts."""
    parts = []
    if orders is not None and orders.exists():
        from .ordertape import committed_parts
        parts = [[p.name, p.stat().st_size] for p in committed_parts(orders)]
    doc = {"store_state": _file_sha(Path(store) / "state.json"), "tokens": _file_sha(tokens), "orders": parts}
    return hashlib.sha256(orjson.dumps(doc, option=orjson.OPT_SORT_KEYS)).hexdigest()


def input_key(pages: dict, wallet: str, times: tuple[int, int], shared: str) -> str:
    """Hash of the wallet's pagination records overlapping the window and the shared inputs.
    A completed wallet whose key changes is recomputed (from its RPC cache)."""
    recs = [pages[k] for k in sorted(pages) if k[0] == wallet and k[1] < times[1] and k[2] > times[0]]
    return hashlib.sha256(orjson.dumps([recs, shared], option=orjson.OPT_SORT_KEYS)).hexdigest()


def load_done(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if path.exists():
        for line in path.read_bytes().splitlines():
            if line.strip():
                d = orjson.loads(line)
                out[d["wallet"]] = d
    return out


def _append(path: Path, rec: dict) -> None:
    with path.open("ab") as fh:
        fh.write(orjson.dumps(rec) + b"\n")
        fh.flush()
        os.fsync(fh.fileno())


def _headers(chain: Chain) -> dict:
    return {k: v for k, v in chain.mem.items() if k.startswith('["eth_getBlockByNumber",[')}


# ---------------------------------------------------------------- window
def resolve_window(out: Path, sha: str, t_from: int, t_to: int, pool: Pool, routes: dict) -> tuple[dict, Chain]:
    """The run manifest, resolved once; a resume must name the same cohort and window."""
    manifest = out / "run.json"
    chain = Chain(pool, out / "_chain" / "window.jsonl", routes)
    if manifest.exists():
        run = orjson.loads(manifest.read_bytes())
        if (run["cohort_sha256"], run["window_time"]) != (sha, [t_from, t_to]):
            raise ValueError(f"{out} holds a run for another cohort or window; use a new --out")
        return run, chain
    head_hex, _ = pool.call("eth_blockNumber", [], allowed=routes["eth_blockNumber"])
    if not isinstance(head_hex, str):
        raise RpcError("invalid_chain_head")
    head = int(head_hex, 16)
    b0 = chain.block_at(t_from, head)
    b1 = chain.block_at(t_to, head) - 1
    if b0 < 1 or b1 < b0:
        raise ValueError("empty block interval or unavailable opening block")
    for n in (b0 - 1, b0, b1, b1 + 1):  # the boundary headers every wallet checks
        chain.block_ts(n)
    run = {"cohort_sha256": sha, "window_time": [t_from, t_to], "window_blocks": [b0, b1],
           "routes": {m: sorted(u) for m, u in sorted(routes.items())}}
    atomic_write(manifest, orjson.dumps(run, option=orjson.OPT_INDENT_2))
    return run, chain


# ---------------------------------------------------------------- table
COLUMNS = ["wallet", "role", "state", "status", "causes", "chain_error", "fills", "tokens",
           "tokens_with_share_residual", "max_share_residual", "cash_residual", "completing_share_of_buys",
           "paired_share_of_holding_time", "longest_one_sided_s", "splits", "merges", "redeems", "transfers"]


def write_table(out: Path, run: dict, members: list[tuple[str, str]], done: dict[str, dict],
                pending: dict[str, str], pool: Pool | None, stopped: str | None) -> dict:
    rows = []
    for role, w in members:
        if w in done:
            rows.append({**done[w]["row"], "role": role, "state": "done", "chain_error": None})
        else:
            rows.append({"wallet": w, "role": role, "state": "pending" if w in pending else "not_started",
                         "status": "pending", "causes": "", "chain_error": pending.get(w)})
    cause_counts = Counter(c for r in rows if r["state"] == "done" for c in r["causes"].split(",") if c)
    summary = {"cohort_sha256": run["cohort_sha256"], "window_blocks": run["window_blocks"],
               "window_time": run["window_time"], "wallets": len(rows),
               "done": sum(r["state"] == "done" for r in rows),
               "reconciled": sum(r["status"] == "reconciled" for r in rows),
               "not_reconciled": sum(r["status"] == "not_reconciled" for r in rows),
               "pending": sum(r["state"] == "pending" for r in rows),
               "not_started": sum(r["state"] == "not_started" for r in rows),
               "causes": dict(cause_counts.most_common()), "stopped": stopped,
               "rpc": pool.stats() if pool else None, "requests": pool.requests if pool else 0}
    lines = ["\t".join(COLUMNS)] + ["\t".join("" if r.get(c) is None else str(r[c]) for c in COLUMNS) for r in rows]
    atomic_write(out / "cohort.tsv", ("\n".join(lines) + "\n").encode())
    links = signer_links({w: {"signers": d.get("signers", {})} for w, d in done.items()})
    atomic_write(out / "cohort.json", orjson.dumps({"summary": summary, "rows": rows,
                                                    "shared_verified_signers": links},
                                                   option=orjson.OPT_INDENT_2))
    return summary


# ---------------------------------------------------------------- run
def run(a: argparse.Namespace, transport: Callable) -> int:
    a.out.mkdir(parents=True, exist_ok=True)
    lock = (a.out / ".lock").open("a+b")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print(f"another doppel-absorb-cohort run holds {a.out / '.lock'}", file=sys.stderr)
        return 4
    try:
        return _run_locked(a, transport)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def _run_locked(a: argparse.Namespace, transport: Callable) -> int:
    sha, members = load_cohort(a.cohort)
    urls, routes = rpc_routes(a.rpc, a.log_rpc, a.state_rpc)
    pool = Pool(urls, transport, rate=a.rate, max_requests=a.max_requests)
    try:
        run_doc, window_chain = resolve_window(a.out, sha, a.t_from, a.t_to, pool, routes)
    except (BudgetExhausted, EndpointsDown, RpcError, ValueError) as e:
        print(f"cannot resolve the window to blocks: {e}", file=sys.stderr)
        return 2
    b0, b1 = run_doc["window_blocks"]
    times = (a.t_from, a.t_to)
    pages = pagination_state(a.pages) if a.pages else {}
    pairs = load_pairs(a.tokens)
    done_path = a.out / "done.jsonl"
    done = load_done(done_path)
    shared = inputs_digest(a.store, a.tokens, a.orders)
    done = {w: d for w, d in done.items() if d.get("input_key") == input_key(pages, w, times, shared)}
    todo = [(role, w) for role, w in members if w not in done]
    seeds = _headers(window_chain)
    pending: dict[str, str] = {}
    stopped = None
    for i in range(0, len(todo), a.wallet_batch):
        group = todo[i:i + a.wallet_batch]
        fills = dict(iter_wallet_fills(a.store, [w for _, w in group]))  # one batch in memory
        for _, w in group:
            chain = Chain(pool, a.out / "_chain" / f"wallet-{w}.jsonl", routes)
            for k, v in seeds.items():
                chain.mem.setdefault(k, v)
            r = absorb_wallet(chain, w, fills.pop(w, []), (b0, b1), times, a.chunk_blocks, pages, pairs, a.orders)
            err = r["completeness"]["chain_error"]
            budget_hit = a.max_requests is not None and pool.requests >= a.max_requests
            if transient(err) or budget_hit:  # identity reads swallow budget errors; retry those too
                pending[w] = err or "BudgetExhausted"
                atomic_write(a.out / f"wallet-{w}.attempt.json", orjson.dumps(r, option=orjson.OPT_INDENT_2))
                if pending[w] in STOPS or budget_hit:
                    stopped = pending[w]
            else:
                atomic_write(a.out / f"wallet-{w}.json", orjson.dumps(r, option=orjson.OPT_INDENT_2))
                rec = {"wallet": w, "input_key": input_key(pages, w, times, shared), "row": cohort_row(r),
                       "signers": r["identity"]["signers"]}
                _append(done_path, rec)
                done[w] = rec
                (a.out / f"wallet-{w}.attempt.json").unlink(missing_ok=True)
            print(orjson.dumps({"wallet": w, "status": r["status"], "causes": r["causes"],
                                "chain_error": err, "requests": pool.requests}).decode(), flush=True)
            if stopped:
                break
        write_table(a.out, run_doc, members, done, pending, pool, stopped)
        if stopped:
            break
    summary = write_table(a.out, run_doc, members, done, pending, pool, stopped)
    print(orjson.dumps(summary).decode())
    return 0 if summary["done"] == len(members) else 3


# ---------------------------------------------------------------- dry-run planner
def store_fill_counts(store: Path, wallets: list[str], t0: int, t1: int) -> dict[str, int]:
    """Distinct Goldsky fills per wallet in [t0, t1) from the store, without loading rows."""
    try:
        con, _ = _connect(Path(store), "256MB", 2)
    except FileNotFoundError:
        return {}
    if con is None:
        return {}
    try:
        q = ("SELECT address, count(DISTINCT id) FROM fills WHERE address IN (SELECT unnest(?::VARCHAR[])) "
             "AND block_timestamp >= ? AND block_timestamp < ? GROUP BY address")
        return {w: int(n) for w, n in con.execute(q, [sorted(wallets), t0, t1]).fetchall()}
    finally:
        con.close()


def plan(a: argparse.Namespace) -> dict:
    """RPC requests and Goldsky rows the run would need, from local files only (no network)."""
    sha, members = load_cohort(a.cohort)
    t0, t1 = a.t_from, a.t_to
    manifest = a.out / "run.json"
    run_doc = orjson.loads(manifest.read_bytes()) if manifest.exists() else None
    if run_doc and (run_doc["cohort_sha256"], run_doc["window_time"]) != (sha, [t0, t1]):
        raise ValueError(f"{a.out} holds a run for another cohort or window; use a new --out")
    blocks = (run_doc["window_blocks"][1] - run_doc["window_blocks"][0] + 1 if run_doc
              else math.ceil((t1 - t0) / NOMINAL_BLOCK_S))
    pages = pagination_state(a.pages) if a.pages else {}
    done = load_done(a.out / "done.jsonl")
    wallets = [w for _, w in members]
    fills = store_fill_counts(a.store, wallets, t0, t1)
    per_wallet_fixed = LOG_QUERIES * math.ceil(blocks / a.chunk_blocks) + STATE_CALLS + 1  # + eth_getCode
    shared = inputs_digest(a.store, a.tokens, a.orders)
    todo = [w for w in wallets
            if not (w in done and done[w].get("input_key") == input_key(pages, w, (t0, t1), shared))]
    fixed = per_wallet_fixed * len(todo)
    todo_fills = sum(fills.get(w, 0) for w in todo)
    receipts = [math.ceil(todo_fills * k) for k in RECEIPTS_PER_FILL]
    window = 0 if run_doc else HEAD_SEARCH_MAX
    # Goldsky: a terminal, trade-bound record covering the window costs nothing. A covering record
    # without a trade binding (pre-binding captures) costs no rows but needs re-attestation from its
    # sealed pages (goldsky_proof): absorb rejects it, and doppel-goldsky cohort skips an exact key
    # that is already terminal, bound or not. Otherwise estimate from the wallet's row density in
    # its longest terminal record, else its stored fills.
    gs = {"covered": 0, "needs_reattestation": 0, "missing": 0, "unknown_density": 0, "rows_est": 0,
          "command_pulls": 0, "command_rows_est": 0}
    bound = {k: r for k, r in pages.items() if isinstance(r.get("trade_binding"), dict)}
    from .absorb import goldsky_complete
    for w in wallets:
        terminal = [r for (addr, s, e), r in pages.items() if addr == w and terminal_page(r) and e > s]
        best = max(terminal, key=lambda r: r["end_time"] - r["start_time"], default=None)
        est = (math.ceil(best["rows"] * (t1 - t0) / (best["end_time"] - best["start_time"])) if best
               else fills.get(w))
        if est is None:
            gs["unknown_density"] += 1
            est = a.max_rows_per_wallet
        est = min(est, a.max_rows_per_wallet)
        if goldsky_complete(bound, w, t0, t1):
            gs["covered"] += 1
        elif goldsky_complete(pages, w, t0, t1):
            gs["needs_reattestation"] += 1
        else:
            gs["missing"] += 1
            gs["rows_est"] += est
        exact = pages.get((w, t0, t1))
        if not (exact and terminal_page(exact)):  # doppel-goldsky cohort pulls its exact interval
            gs["command_pulls"] += 1
            gs["command_rows_est"] += est
    gs["command_worst_rows"] = gs["command_pulls"] * a.max_rows_per_wallet
    gs["command_worst_usd"] = round(gs["command_worst_rows"] * 10.0 / 1_000_000, 2)
    days = (t1 - t0) / 86400
    n_cand = sum(role == "cand" for role, _ in members)
    return {"cohort_sha256": sha, "window_time": [t0, t1], "blocks": blocks,
            "blocks_source": "run.json" if run_doc else f"nominal {NOMINAL_BLOCK_S}s blocks",
            "wallets": len(wallets), "wallets_to_absorb": len(todo), "chunk_blocks": a.chunk_blocks,
            "rpc": {"window_resolution_max": window, "per_wallet_fixed": per_wallet_fixed,
                    "fixed_total": fixed, "store_fills_in_window": todo_fills,
                    "receipts_floor": 0, "receipts_empirical": receipts,
                    "total_est": [window + fixed, window + fixed + receipts[1]],
                    "note": "first-attempt requests; retries, span halvings and Safe getOwners add to it, "
                            "and all count against --max-requests"},
            "goldsky": gs,
            "goldsky_days_exact": round(float(repr(days)) * 86400) == t1 - t0,
            "goldsky_command": ["doppel-goldsky", "--data", "$TAPE", "--ledger", "$LEDGER", "cohort",
                                "--cohort", str(a.cohort), "--n-candidates", str(n_cand),
                                "--n-controls", str(len(members) - n_cand), "--days", repr(days),
                                "--end", str(t1), "--max-rows-per-wallet", str(a.max_rows_per_wallet)]}


# ---------------------------------------------------------------- census v2 input
def absorbed_census(census: dict, absorb_dir: Path) -> dict:
    """Census wallets whose absorption reconciled, with the absorption's share split added.

    The census aggregates are recomputed over the survivors (never carried over). The split is in
    shares, summed over the wallet's paired markets: ``completing`` buys close a complete set,
    ``directional`` buys open one-sided exposure (``absorb._pairing``)."""
    run_doc = orjson.loads((absorb_dir / "run.json").read_bytes())
    if census.get("cohort_sha256") != run_doc["cohort_sha256"]:
        raise ValueError("the census and the absorption run are for different cohorts")
    table = orjson.loads((absorb_dir / "cohort.json").read_bytes())
    current = {row["wallet"] for row in table["rows"] if row["state"] == "done"}  # not stale files
    kept, excluded = {}, Counter()
    for w, rec in census["wallets"].items():
        if w not in current:
            excluded["no_absorption"] += 1
            continue
        r = orjson.loads((absorb_dir / f"wallet-{w}.json").read_bytes())
        if r.get("not_reconciled") is not False:
            excluded["not_reconciled"] += 1
            continue
        p = r["pairing"]
        tot = Counter()
        for m in p["markets"].values():
            for k in ("completing", "directional", "derisk", "unpair"):
                tot[k] += m.get(k, 0.0)
        buys = tot["completing"] + tot["directional"]
        kept[w] = {**rec, "absorption": {
            "status": r["status"], "window_time": r["window_time"], "pairing_coverage": p["coverage"],
            "complete_set_buy_shares": round(tot["completing"], 6),
            "directional_buy_shares": round(tot["directional"], 6),
            "derisk_sell_shares": round(tot["derisk"], 6), "unpair_sell_shares": round(tot["unpair"], 6),
            "complete_set_share_of_buys": round(tot["completing"] / buys, 4) if buys else None,
            "paired_share_of_holding_time": p["paired_share_of_holding_time"],
            "tokens_without_pair": len(p["tokens_without_pair"])}}
    agg: dict[str, dict] = {}
    for rec in kept.values():
        if rec.get("eligible"):
            g = agg.setdefault(f"{rec['role']}:{rec['label']}", {"wallets": 0, "per_day": 0.0, "positive": 0})
            g["wallets"] += 1
            g["per_day"] += rec.get("window_economic_per_day", 0.0)
            g["positive"] += rec.get("window_economic_per_day", 0.0) > 0
    sc = [r["selfcheck"]["ok"] for r in kept.values() if r.get("eligible") and r.get("selfcheck")]
    out = {k: v for k, v in census.items() if k not in ("wallets", "by_label", "selfcheck")}
    return {**out, "absorption_filter": {"absorb_dir": str(absorb_dir),
                                         "cohort_sha256": run_doc.get("cohort_sha256"),
                                         "window_time": run_doc.get("window_time"),
                                         "kept": len(kept), "excluded": dict(excluded)},
            "wallets": kept,
            "by_label": {k: {**v, "per_day": round(v["per_day"], 2)} for k, v in sorted(agg.items())},
            "selfcheck": {"window_specialists": len(sc), "ok": sum(sc),
                          "rate": round(sum(sc) / len(sc), 4) if sc else None}}


def census_main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-census-absorbed",
                                 description="Census restricted to wallets whose absorption reconciled")
    ap.add_argument("--census", type=Path, required=True, help="doppel-census output JSON")
    ap.add_argument("--absorb", type=Path, required=True, help="doppel-absorb-cohort --out DIR")
    ap.add_argument("--out", type=Path, required=True, help="new census file (must not exist)")
    a = ap.parse_args(argv)
    if a.out.exists() or a.out.resolve() == a.census.resolve():
        ap.error("--out must be a new file")
    try:
        res = absorbed_census(orjson.loads(a.census.read_bytes()), a.absorb)
    except (OSError, KeyError, ValueError) as e:
        print(f"cannot filter the census: {e}", file=sys.stderr)
        return 2
    a.out.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(a.out, orjson.dumps(res, option=orjson.OPT_INDENT_2))
    print(orjson.dumps(res["absorption_filter"]).decode())
    return 0


# ---------------------------------------------------------------- CLI
def main(argv: list[str] | None = None, transport: Callable = http_transport) -> int:
    ap = argparse.ArgumentParser(prog="doppel-absorb-cohort", description="Absorption ledger for a whole cohort")
    ap.add_argument("--store", type=Path, required=True, help="Parquet fill store (fillstore)")
    ap.add_argument("--cohort", type=Path, required=True, help="frozen cohort JSON (sha256, candidates, controls)")
    ap.add_argument("--from", dest="t_from", type=int, required=True, help="window start, unix seconds")
    ap.add_argument("--to", dest="t_to", type=int, required=True, help="window end, unix seconds (exclusive)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-requests", type=int, default=None, help="HTTP requests this run, retries included")
    ap.add_argument("--wallet-batch", type=int, default=10, help="wallets whose fills are loaded at once")
    ap.add_argument("--rpc", nargs="+", help="general RPC endpoints; also overrides the role defaults")
    ap.add_argument("--logs-rpc", "--log-rpc", dest="log_rpc", nargs="+", help="endpoints serving eth_getLogs")
    ap.add_argument("--state-rpc", nargs="+", help="endpoints serving historical eth_call and eth_getCode")
    ap.add_argument("--rate", type=float, default=2.0, help="requests per second per endpoint")
    ap.add_argument("--chunk-blocks", type=int, default=DEFAULT_CHUNK, help="eth_getLogs block span")
    ap.add_argument("--pages", type=Path, help="goldsky_activity/pagination.jsonl")
    ap.add_argument("--tokens", type=Path, help="ledger tokens.jsonl (token -> condition, idx)")
    ap.add_argument("--orders", type=Path, help="ordertape output directory (signers)")
    ap.add_argument("--plan", action="store_true", help="estimate RPC requests and Goldsky rows; no network")
    ap.add_argument("--max-rows-per-wallet", type=int, default=20_000, help="Goldsky cap per wallet (plan)")
    a = ap.parse_args(argv)
    if a.t_to <= a.t_from or a.chunk_blocks < 1 or a.rate <= 0 or a.wallet_batch < 1 or (
            a.max_requests is not None and a.max_requests < 0) or a.max_rows_per_wallet < 0:
        ap.error("use an increasing window, positive chunk-blocks, rate and wallet-batch, nonnegative budgets")
    if a.plan:
        try:
            print(orjson.dumps(plan(a), option=orjson.OPT_INDENT_2).decode())
        except ValueError as e:
            print(str(e), file=sys.stderr)
            return 2
        return 0
    try:
        return run(a, transport)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
