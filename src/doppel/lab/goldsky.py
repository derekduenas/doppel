"""Goldsky Feeds puller with a hard spend cap.

Owner rule (2026-11-25): use ONLY the one-time free $100 Starter credit. Feeds Activity bills
1 compute unit per row returned, at $10 per million CU, so $100 is about 10M rows. This module enforces
a lower self-imposed cap (default 7M rows) through an append-only ledger that is checked BEFORE every
request. A request is refused when it could push the total past the cap. Starter also pauses at $0
and is never billed. The cap is a second, independent guard.

Raw pages are written to the tape (stream ``goldsky_activity``) exactly as received, with the
request URL (minus the key) and both clocks.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import orjson

from ..tape.segment import SegmentWriter
from ..tape.sources.base import SSL_CTX, UA

BASE = "https://edge.goldsky.com/data/polymarket/activity"
USD_PER_ROW = 10.0 / 1_000_000
DEFAULT_CAP_ROWS = 7_000_000
PAGE = 1000
RPS = 4.0  # Starter limit is 5 req/s per project


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    """Append-only row ledger. One line per page: {"tw","job","rows","url"}. Totals are recomputed
    from the file on open, so restarts cannot forget spend."""

    def __init__(self, path: Path, cap_rows: int = DEFAULT_CAP_ROWS):
        self.path = Path(path)
        self.cap = cap_rows
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.total = 0
        if self.path.exists():
            for line in self.path.read_bytes().splitlines():
                if line.strip():
                    self.total += orjson.loads(line)["rows"]

    def check(self, next_rows: int = PAGE) -> None:
        if self.total + next_rows > self.cap:
            raise BudgetExceeded(f"row cap {self.cap:,} would be exceeded (spent {self.total:,})")

    def add(self, job: str, rows: int, url: str) -> None:
        self.total += rows
        with open(self.path, "ab") as fh:
            fh.write(orjson.dumps({"tw": time.time_ns(), "job": job, "rows": rows, "url": url}) + b"\n")
            fh.flush()
            os.fsync(fh.fileno())

    def summary(self) -> dict:
        return {"rows": self.total, "usd_est": round(self.total * USD_PER_ROW, 4),
                "cap_rows": self.cap, "cap_usd": round(self.cap * USD_PER_ROW, 2),
                "remaining_rows": self.cap - self.total}


async def pull(job: str, params: dict, key: str, ledger: Ledger, writer: SegmentWriter,
               max_rows: int, session: aiohttp.ClientSession | None = None) -> int:
    """Page through one filtered query (newest first) until exhausted or ``max_rows``.
    Returns rows fetched. Every page is budget-checked first and ledgered after."""
    own = session is None
    if own:
        session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX),
                                        timeout=aiohttp.ClientTimeout(total=60),
                                        headers={"user-agent": UA})
    got = 0
    cursor = None
    try:
        while got < max_rows:
            limit = min(PAGE, max_rows - got)
            ledger.check(limit)
            q = {**params, "limit": limit}
            if cursor:
                q["cursor"] = cursor
            url = f"{BASE}?{urlencode(q)}"
            sent_w, sent_m = time.time_ns(), time.monotonic_ns()
            async with session.get(url, headers={"X-API-Key": key}) as r:
                body = await r.text()
                status = r.status
                retry = r.headers.get("Retry-After")
            if status == 429:
                await asyncio.sleep(float(retry or 1.0))
                continue
            if status != 200:
                writer.append(body, meta={"job": job, "url": url, "status": status,
                                          "sent_tw": sent_w, "sent_tm": sent_m})
                raise RuntimeError(f"HTTP {status}: {body[:200]}")
            doc = orjson.loads(body)
            rows = len(doc.get("data") or [])
            ledger.add(job, rows, url)
            writer.append(body, meta={"job": job, "url": url, "status": status, "rows": rows,
                                      "sent_tw": sent_w, "sent_tm": sent_m})
            got += rows
            cursor = doc.get("next_cursor")
            if not cursor or rows == 0:
                break
            await asyncio.sleep(1.0 / RPS)
    finally:
        writer.flush()
        if own:
            await session.close()
    return got


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-goldsky")
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--cap-rows", type=int, default=DEFAULT_CAP_ROWS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("window", help="all activity in a time window (<= 7 days)")
    w.add_argument("--start", type=int, required=True)
    w.add_argument("--end", type=int, required=True)
    w.add_argument("--max-rows", type=int, required=True)
    w.add_argument("--job", required=True)
    sub.add_parser("budget")
    a = ap.parse_args(argv)
    ledger = Ledger(a.ledger, a.cap_rows)
    if a.cmd == "budget":
        print(orjson.dumps(ledger.summary()).decode())
        return 0
    key = os.environ.get("GOLDSKY_FEEDS_API_KEY")
    if not key:
        print("GOLDSKY_FEEDS_API_KEY not set", file=sys.stderr)
        return 2
    if a.end - a.start > 604_800:
        ap.error("Feeds time windows are limited to 7 days")
    writer = SegmentWriter(a.data, "goldsky_activity")
    try:
        n = asyncio.run(pull(a.job, {"start_time": a.start, "end_time": a.end}, key, ledger,
                             writer, a.max_rows))
    finally:
        writer.close()
    print(orjson.dumps({"job": a.job, "rows": n, **ledger.summary()}).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
