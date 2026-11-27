"""Project selected wallets from one complete, unfiltered, sealed Goldsky activity query."""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import math
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import parse_qs, urlsplit

import orjson

from ..tape.segment import read_segment
from . import fillstore
from .goldsky import BASE, EMPTY_BINDING, PAGE, PAGES, trade_binding
from .goldsky_proof import digest

ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
HASH = re.compile(r"0x[0-9a-fA-F]{64}")
LOG_ID = re.compile(r"log_0x[0-9a-fA-F]{64}_(?:0|[1-9][0-9]*)")
ORDER_ID = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")
TOKEN = re.compile(r"(?:0|[1-9][0-9]{0,77})")
SEGMENT = re.compile(r"[0-9]{8}/(?:[01][0-9]|2[0-3])\.dtape\.zst")
# Stop at unfamiliar types so a renamed or malformed TRADE cannot silently disappear.
EVENTS = {"TRADE", "SPLIT", "MERGE", "REDEEM", "CONVERSION", "REWARD", "MAKER_REBATE",
          "TRANSFER", "DEPOSIT", "WITHDRAWAL"}


def _match(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _row(row, start, end):
    if not isinstance(row, dict) or not _match(ADDRESS, row.get("address")):
        raise ValueError("invalid Goldsky activity address")
    stamp, kind = row.get("block_timestamp"), row.get("event_type")
    if (type(stamp) is not int or not start <= stamp <= end or not _match(LOG_ID, row.get("id"))
            or not isinstance(kind, str) or kind not in EVENTS
            or not _match(HASH, row.get("transaction_hash"))):
        raise ValueError("invalid activity timestamp, type or identity")
    if kind == "TRADE":
        if (not _match(ORDER_ID, row.get("order_id")) or not _match(TOKEN, row.get("token_id"))
                or int(row["token_id"]) >= 2**256 or row.get("side") not in ("BUY", "SELL")
                or row.get("order_type") not in ("maker", "taker")
                or type(row.get("block_number")) is not int or row["block_number"] <= 0
                or ("counterparty_address" not in row or row["counterparty_address"] is not None
                    and not _match(ADDRESS, row["counterparty_address"]))):
            raise ValueError("invalid trade identity, block, side or liquidity role")
        for k in ("amount_shares", "amount_usdc", "fee", "price"):
            v = row.get(k)
            if type(v) not in (float, int) or not math.isfinite(v) or v < 0:
                raise ValueError(f"invalid trade {k}")
        if row["price"] > 1 or row["amount_shares"] <= 0:
            raise ValueError("invalid trade price or size")
    return row["address"].lower(), stamp, kind


def _page(rec, start, end, cursor, limit):
    meta = rec["meta"]
    url = urlsplit(meta["url"])
    q = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
    required = {"start_time", "end_time", "limit"} | ({"cursor"} if cursor is not None else set())
    if (meta.get("status") != 200 or url.fragment or url._replace(query="").geturl() != BASE
            or set(q) != required or any(len(v) != 1 for v in q.values())
            or q["start_time"] != [str(start)] or q["end_time"] != [str(end)]
            or q.get("cursor", [None]) != [cursor] or not re.fullmatch(r"[1-9][0-9]{0,3}", q["limit"][0])):
        raise ValueError("discontinuous or filtered Goldsky request")
    requested = int(q["limit"][0])
    body = orjson.loads(rec["raw"])
    if (requested > PAGE or limit is not None and limit != requested or not isinstance(body, dict)
            or "next_cursor" not in body or type(body.get("limit")) is not int or body["limit"] != requested
            or not isinstance(body.get("data"), list) or len(body["data"]) > requested
            or "rows" in meta and meta["rows"] != len(body["data"])):
        raise ValueError("invalid Goldsky activity page")
    nxt, rows = body["next_cursor"], body["data"]
    if nxt is not None and (not isinstance(nxt, str) or not nxt.strip() or not rows):
        raise ValueError("invalid Goldsky continuation")
    return requested, rows, nxt


def _identity(stat):
    return stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def project(tape: Path, start: int, end: int, job: str, segments: list[str],
            wallets: list[str], out: Path) -> dict:
    """Publish a half-open parent-interval fill store and a bound terminal proof per wallet."""
    if (not job.strip() or start >= end or not segments or len(set(segments)) != len(segments)
            or any(not _match(SEGMENT, n) for n in segments) or not wallets
            or any(not _match(ADDRESS, w) for w in wallets)):
        raise ValueError("invalid job, interval, segment names or wallets")
    selected = sorted({w.lower() for w in wallets})
    if len(selected) != len(wallets):
        raise ValueError("duplicate wallet")
    if out.exists():
        raise FileExistsError(out)
    source = tape / fillstore.STREAM
    manifest = (source / "manifest.jsonl").read_bytes()
    seals = {r["file"]: r for line in manifest.splitlines() if line and (r := orjson.loads(line))}
    pages = total = 0
    limit = cursor = last_tw = None
    seen = set()
    bindings = {w: EMPTY_BINDING for w in selected}
    counts = Counter()
    store_segments, archived = {}, {}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=out.parent, prefix=".global-pages-") as tmp:
        stage = Path(tmp) / "proof"
        fills = stage / "fills"
        fills.mkdir(parents=True)
        with closing(sqlite3.connect(Path(tmp) / "ids.sqlite")) as ids:
            ids.execute("CREATE TABLE ids (id TEXT PRIMARY KEY COLLATE NOCASE) WITHOUT ROWID")
            for name in sorted(segments):
                path = source / name
                before = path.stat()
                seal = seals.get(name)
                if not seal or before.st_size != seal["bytes"] or digest(path) != seal["sha256"]:
                    raise ValueError(f"unsealed or changed segment {name}")
                archived[name] = {"bytes": seal["bytes"], "sha256": seal["sha256"]}
                filename = fillstore._out_name(name)
                writer = fillstore._BatchWriter(fills / filename)
                try:
                    for rec in read_segment(path, strict=True, verify=True):
                        if rec.get("stream") != fillstore.STREAM:
                            raise ValueError("unexpected capture stream")
                        if (rec.get("meta") or {}).get("job") != job:
                            continue
                        if pages and cursor is None:
                            raise ValueError("unexpected request after terminal page")
                        limit, rows, cursor = _page(rec, start, end, cursor, limit)
                        if cursor is not None:
                            if cursor in seen:
                                raise ValueError("Goldsky cursor cycle")
                            seen.add(cursor)
                        trades = defaultdict(list)
                        for index, row in enumerate(rows):
                            wallet, stamp, kind = _row(row, start, end)
                            if wallet not in bindings or stamp == end:
                                continue
                            counts[wallet] += 1
                            if kind == "TRADE":
                                writer.add({**row, "address": wallet, "seg": name, "ord": total + index, "job": job})
                                trades[wallet].append(row)
                        try:
                            ids.executemany("INSERT INTO ids VALUES (?)", ((r["id"],) for r in rows))
                        except sqlite3.IntegrityError as exc:
                            raise ValueError("duplicate Goldsky activity ID") from exc
                        for wallet, batch in trades.items():
                            bindings[wallet] = trade_binding(batch, bindings[wallet])
                        pages += 1
                        total += len(rows)
                        last_tw = rec["tw"]
                finally:
                    writer.close()
                store_segments[name] = {"bytes": seal["bytes"], "rows": writer.n,
                                        "file": filename if writer.n else None}
                if _identity(before) != _identity(path.stat()):
                    raise ValueError(f"segment changed during replay: {name}")
        if not pages or cursor is not None:
            raise ValueError("no complete terminal query")
        fillstore._save_state(fills, {"version": fillstore.STATE_VERSION, "job_prefix": job,
                                      "segments": store_segments})
        # Re-read the native representation before publishing the matching pagination receipts.
        reread = dict(fillstore.iter_wallet_fills(fills, selected))
        for w in selected:
            if trade_binding(reread.get(w, [])) != bindings[w]:
                raise ValueError(f"written fill store differs from archive for {w}")
        states = [{"address": w, "start_time": start, "end_time": end, "pages": pages, "job": job,
                   "rows": counts[w], "cursor": None, "complete": True, "stop": "done",
                   "tw": last_tw, "source": "sealed_global_activity_projection",
                   "trade_binding": bindings[w]} for w in selected]
        pagination = stage / PAGES
        pagination.write_bytes(b"".join(orjson.dumps(s) + b"\n" for s in states))
        summary = {"source": "sealed_global_activity_projection", "job": job, "interval": [start, end],
                   "source_pages": pages, "source_rows": total, "projected_wallet_rows": dict(counts),
                   "trade_bindings": bindings, "segments": archived,
                   "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                   "pagination_sha256": digest(pagination)}
        (stage / "summary.json").write_bytes(orjson.dumps(summary, option=orjson.OPT_INDENT_2) + b"\n")
        stage.rename(out)
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("data", "out"):
        p.add_argument("--" + name, required=True, type=Path)
    for name in ("start", "end"):
        p.add_argument("--" + name, required=True, type=int)
    p.add_argument("--job", required=True)
    p.add_argument("--segments", required=True, nargs="+", help="YYYYMMDD/HH.dtape.zst")
    p.add_argument("--wallets", required=True, nargs="+")
    a = p.parse_args()
    print(orjson.dumps(project(a.data, a.start, a.end, a.job, a.segments, a.wallets, a.out)).decode())


if __name__ == "__main__":
    main()
