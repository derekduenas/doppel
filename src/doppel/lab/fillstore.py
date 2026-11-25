"""Columnar, incremental store of Goldsky TRADE rows, read one wallet at a time.

``ledger.load_fills`` decodes the whole Goldsky tape into Python dicts for every wallet at once
(~1.7M rows took 2.7 GB on a shared 8 GB host). This module splits that into two steps:

``compact(root, out_dir)``
    Streams ``<root>/goldsky_activity/*/*.dtape.zst`` and writes the TRADE rows of status-200 pages
    whose job matches ``job_prefix`` to one Parquet file per tape segment, in row batches of
    ``BATCH`` rows. The tape is read one zstd frame at a time, so this pass holds one frame plus
    one batch, whatever the segment size. DuckDB then rewrites each file sorted by address in small
    row groups (out-of-core sort, spilling to ``<out_dir>/.duckdb_tmp`` past ``memory_limit``), so
    a wallet's read touches only its own row groups. Peak memory is roughly ``memory_limit`` plus
    ~150 MB. A store written by an older ``STATE_VERSION`` (different schema) is wiped and rebuilt
    from the tape; readers refuse it until then. A JSON state file records each
    compacted segment's byte size: a re-run only reads segments that are new or have grown (the
    current hour's segment is still being appended to), rewrites that segment's Parquet file
    atomically, and drops output for segments that left the tape. There is no lock: do not run
    two compactions of the same ``out_dir`` at once.

``iter_wallet_fills(store_dir, wallets)``
    Yields ``(wallet, rows)`` in sorted wallet order, skipping wallets with no rows. ``rows`` has
    the same dicts, deduplication and order as ``load_fills``. Only files listed in the state file
    are read, so leftovers from a crashed compaction are ignored.

Dedupe choice: no global id set is kept while compacting (it would be the largest object at
~1.7M ids). Every matching row is stored with its position in tape order: segment (sorted path,
as ``load_fills`` reads them) and ``ord`` (row counter within the segment). The reader loads one
wallet's rows, keeps the first occurrence of each id in that order, then sorts by
(block_number, id), so first-occurrence-wins matches ``load_fills`` exactly. A row id belongs to
one address, so deduplicating within a wallet is the same as deduplicating globally.

Types: the schema is fixed (strings, float64 amounts, int64 block fields). A value that does not
fit its column (e.g. a string amount) raises ``ValueError`` instead of being coerced, so the store
can never silently disagree with ``load_fills``. ints in float columns compare equal (10 == 10.0).

Writes use pyarrow (streaming ParquetWriter). Reads use one DuckDB query per wallet over the
listed files, with an explicit ``memory_limit`` and ``threads`` (the DuckDB defaults take most of a
shared box). DuckDB, rather than pyarrow dataset filters, because a per-wallet pyarrow scan held
~340 MB RSS and took ~135 ms per wallet on a 400k-row store.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from pathlib import Path

import duckdb
import orjson
import pyarrow as pa
import pyarrow.parquet as pq

from ..tape.segment import read_segment
from .ledger import KEEP

STREAM = "goldsky_activity"
STATE = "state.json"
STATE_VERSION = 2  # 2: + order_id, counterparty_address, transaction_hash
BATCH = 50_000  # rows per pyarrow write while streaming a segment
ROW_GROUP = 16_384  # rows per row group in the final, address-sorted file
MEMORY_LIMIT = "256MB"  # DuckDB cap for the sort and the reads (shared 8 GB host)

_STR, _F64, _I64 = pa.string(), pa.float64(), pa.int64()
COLUMNS: dict[str, pa.DataType] = {
    "id": _STR, "address": _STR, "token_id": _STR, "side": _STR,
    "amount_shares": _F64, "amount_usdc": _F64, "fee": _F64,
    "block_timestamp": _I64, "block_number": _I64, "order_type": _STR, "price": _F64,
    "order_id": _STR, "counterparty_address": _STR, "transaction_hash": _STR,
    "job": _STR,
}
SCHEMA = pa.schema([*COLUMNS.items(), ("seg", _STR), ("ord", _I64)])
ROW_KEYS = (*KEEP, "id")  # dict shape and key order of load_fills rows


# ---------------------------------------------------------------- value checks
def _check(col: str, v):
    """Return ``v`` if it fits column ``col`` losslessly, else raise ValueError."""
    if v is None:
        return None
    t = COLUMNS[col]
    if t == _STR:
        if isinstance(v, str):
            return v
    elif isinstance(v, bool):
        pass
    elif t == _F64:
        if isinstance(v, (int, float)):
            return v
    elif isinstance(v, int):
        return v
    elif isinstance(v, float) and v.is_integer():
        return int(v)
    raise ValueError(f"fillstore: column {col!r} got {type(v).__name__} {v!r}; schema expects "
                     f"{t}. Not coercing, so the store cannot diverge from load_fills.")


# ---------------------------------------------------------------- state
def _load_state(out_dir: Path) -> dict | None:
    p = out_dir / STATE
    return orjson.loads(p.read_bytes()) if p.exists() else None


def _save_state(out_dir: Path, state: dict) -> None:
    p = out_dir / STATE
    tmp = p.with_suffix(".json.tmp")
    tmp.write_bytes(orjson.dumps(state, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS))
    os.replace(tmp, p)


def _wipe(out_dir: Path, state: dict) -> None:
    """Remove an old-version store's Parquet files and state."""
    for s in state.get("segments", {}).values():
        if s.get("file"):
            (out_dir / s["file"]).unlink(missing_ok=True)
    (out_dir / STATE).unlink(missing_ok=True)


def _out_name(rel: str) -> str:
    return rel.replace("/", "-").removesuffix(".dtape.zst") + ".parquet"


# ---------------------------------------------------------------- compact
class _BatchWriter:
    def __init__(self, path: Path):
        self.path = path
        self.w: pq.ParquetWriter | None = None
        self.cols: dict[str, list] = {k: [] for k in SCHEMA.names}
        self.n = 0

    def add(self, row: dict) -> None:
        for k, col in self.cols.items():
            col.append(row[k])
        self.n += 1
        if len(self.cols["id"]) >= BATCH:
            self.flush()

    def flush(self) -> None:
        if not self.cols["id"]:
            return
        if self.w is None:
            self.w = pq.ParquetWriter(self.path, SCHEMA, compression="zstd")
        self.w.write_table(pa.table({k: pa.array(v, type=SCHEMA.field(k).type)
                                     for k, v in self.cols.items()}, schema=SCHEMA))
        self.cols = {k: [] for k in SCHEMA.names}

    def close(self) -> int:
        self.flush()
        if self.w is not None:
            self.w.close()
        return self.n


def _compact_segment(path: Path, rel: str, tmp: Path, job_prefix: str) -> int:
    bw = _BatchWriter(tmp)
    try:
        for rec in read_segment(path, verify=True):
            m = rec.get("meta") or {}
            job = str(m.get("job", ""))
            if m.get("status") != 200 or not job.startswith(job_prefix):
                continue
            for r in orjson.loads(rec["raw"]).get("data") or []:
                if r["event_type"] != "TRADE":
                    continue
                row = {k: _check(k, r.get(k)) for k in KEEP}
                row["id"] = _check("id", r["id"])
                row["address"] = r["address"].lower()  # str required, as in load_fills
                row["job"] = job
                row["seg"] = rel
                row["ord"] = bw.n
                bw.add(row)
        return bw.close()
    except BaseException:
        bw.close()
        tmp.unlink(missing_ok=True)
        raise


def _sort_by_address(src: Path, dst: Path, memory_limit: str, threads: int) -> None:
    """Rewrite ``src`` clustered by address into small row groups, so a per-wallet read prunes to
    the row groups holding that address (min/max stats) instead of decoding every column of the
    file. DuckDB sorts out of core within ``memory_limit``. Atomic: ``dst`` appears complete or
    not at all; ``src`` is removed."""
    out = dst.with_name(dst.name + ".sorting")
    con = duckdb.connect()
    try:
        con.execute(f"SET memory_limit = '{memory_limit}'")
        con.execute(f"SET threads = {int(threads)}")
        con.execute("SET preserve_insertion_order = false")
        con.execute("SET temp_directory = ?", [str(dst.parent / ".duckdb_tmp")])
        con.read_parquet(str(src)).create_view("seg")
        con.execute("COPY (SELECT * FROM seg ORDER BY address, ord) TO ? "
                    f"(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {ROW_GROUP})", [str(out)])
    except BaseException:
        out.unlink(missing_ok=True)
        src.unlink(missing_ok=True)
        raise
    finally:
        con.close()
    os.replace(out, dst)
    src.unlink(missing_ok=True)


def compact(root: Path, out_dir: Path, job_prefix: str = "cohort-", *,
            memory_limit: str = MEMORY_LIMIT, threads: int = 2) -> dict:
    """Bring the Parquet store at ``out_dir`` up to date with the Goldsky tape under ``root``.
    Returns counts for this run (``compacted`` segments, ``rows`` written, ``dropped`` segments)
    and the store total (``store_rows``)."""
    root, out_dir = Path(root), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    state = _load_state(out_dir) or {"version": STATE_VERSION, "job_prefix": job_prefix,
                                     "segments": {}}
    if state.get("job_prefix") != job_prefix:
        raise ValueError(f"fillstore at {out_dir} was built with job_prefix "
                         f"{state.get('job_prefix')!r}, not {job_prefix!r}; use another out_dir")
    if state.get("version") != STATE_VERSION:  # schema changed: rebuild every segment
        _wipe(out_dir, state)
        state = {"version": STATE_VERSION, "job_prefix": job_prefix, "segments": {}}
        _save_state(out_dir, state)
    segs: dict = state["segments"]
    stream = root / STREAM
    on_tape = {f.relative_to(stream).as_posix(): f for f in sorted(stream.glob("*/*.dtape.zst"))}
    dropped = 0
    for rel in [r for r in segs if r not in on_tape]:  # segment left the tape: so does its output
        if segs[rel].get("file"):
            (out_dir / segs[rel]["file"]).unlink(missing_ok=True)
        del segs[rel]
        dropped += 1
    if dropped:
        _save_state(out_dir, state)
    compacted = rows = 0
    for rel, f in on_tape.items():
        size = f.stat().st_size
        if rel in segs and segs[rel]["bytes"] == size:
            continue
        name = _out_name(rel)
        tmp = out_dir / (name + ".tmp")
        n = _compact_segment(f, rel, tmp, job_prefix)
        if n:
            _sort_by_address(tmp, out_dir / name, memory_limit, threads)
        else:
            tmp.unlink(missing_ok=True)
            (out_dir / name).unlink(missing_ok=True)
        segs[rel] = {"bytes": size, "rows": n, "file": name if n else None}
        _save_state(out_dir, state)
        compacted += 1
        rows += n
    return {"segments": len(segs), "compacted": compacted, "rows": rows, "dropped": dropped,
            "store_rows": sum(s["rows"] for s in segs.values())}


# ---------------------------------------------------------------- read
def _connect(store_dir: Path, memory_limit: str, threads: int):
    """DuckDB connection with a ``fills`` view over the files listed in the state, plus the
    segment order map. Returns (None, order) when the store holds no rows."""
    store_dir = Path(store_dir)
    state = _load_state(store_dir)
    if state is None:
        raise FileNotFoundError(f"no fillstore at {store_dir} (run compact first)")
    if state.get("version") != STATE_VERSION:
        raise ValueError(f"fillstore at {store_dir} has version {state.get('version')}, code expects "
                         f"{STATE_VERSION}; run compact to rebuild it")
    segs = sorted(state["segments"].items())
    order = {rel: i for i, (rel, _) in enumerate(segs)}
    files = [str(store_dir / s["file"]) for _, s in segs if s["file"]]
    if not files:
        return None, order
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{memory_limit}'")
    con.execute(f"SET threads = {int(threads)}")
    con.read_parquet(files).create_view("fills")
    return con, order


_SELECT = f"SELECT {', '.join([*ROW_KEYS, 'seg', 'ord'])} FROM fills WHERE address = ?"


def _wallet_rows(con: duckdb.DuckDBPyConnection, order: dict[str, int], wallet: str) -> list[dict]:
    raw = con.execute(_SELECT, [wallet]).fetchall()
    raw.sort(key=lambda t: (order[t[-2]], t[-1]))  # tape order -> first occurrence wins
    seen: set[str] = set()
    rows = []
    for t in raw:
        r = dict(zip(ROW_KEYS, t[:-2]))
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        rows.append(r)
    rows.sort(key=lambda r: (r["block_number"], r["id"]))  # same key as load_fills
    return rows


def iter_wallet_fills(store_dir: Path, wallets: Iterable[str] | None = None, *,
                      memory_limit: str = MEMORY_LIMIT, threads: int = 2
                      ) -> Iterator[tuple[str, list[dict]]]:
    """One wallet at a time, sorted by address; wallets without rows are skipped. Rows match
    ``load_fills`` (same keys, dedupe by id, sorted by (block_number, id)). Wallets are matched as
    given against lowercased row addresses, like ``load_fills``. ``None`` means every wallet."""
    con, order = _connect(Path(store_dir), memory_limit, threads)
    if con is None:
        return
    try:
        if wallets is None:
            wallets = [a for (a,) in con.execute("SELECT DISTINCT address FROM fills").fetchall()]
        for w in sorted(set(wallets)):
            rows = _wallet_rows(con, order, w)
            if rows:
                yield w, rows
    finally:
        con.close()


def wallet_tokens(store_dir: Path, wallets: Iterable[str], *, memory_limit: str = MEMORY_LIMIT,
                  threads: int = 2) -> set[str]:
    """Distinct token ids traded by ``wallets`` (for TokenMap.ensure before iterating)."""
    con, _ = _connect(Path(store_dir), memory_limit, threads)
    if con is None:
        return set()
    try:
        q = ("SELECT DISTINCT token_id FROM fills WHERE token_id IS NOT NULL "
             "AND address IN (SELECT unnest(?::VARCHAR[]))")
        return {t for (t,) in con.execute(q, [sorted(set(wallets))]).fetchall()}
    finally:
        con.close()


def wallet_token_notional(store_dir: Path, wallets: Iterable[str], *,
                          memory_limit: str = MEMORY_LIMIT, threads: int = 2
                          ) -> list[tuple[str, str, float]]:
    """(address, token_id, sum of amount_usdc) per wallet and token, deduplicated by row id."""
    con, _ = _connect(Path(store_dir), memory_limit, threads)
    if con is None:
        return []
    try:
        q = ("SELECT address, token_id, sum(usd) FROM (SELECT DISTINCT ON (id) id, address, "
             "token_id, amount_usdc AS usd FROM fills WHERE token_id IS NOT NULL "
             "AND address IN (SELECT unnest(?::VARCHAR[]))) GROUP BY address, token_id")
        return [(a, t, float(u or 0.0)) for a, t, u in
                con.execute(q, [sorted(set(wallets))]).fetchall()]
    finally:
        con.close()
