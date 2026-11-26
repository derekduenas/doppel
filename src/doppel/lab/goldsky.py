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
import fcntl
import os
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import orjson

from ..tape.segment import SegmentWriter
from ..tape.sources.base import SSL_CTX, UA

BASE = "https://edge.goldsky.com/data/polymarket/activity"
USD_PER_ROW = 10.0 / 1_000_000
DEFAULT_CAP_ROWS = 7_000_000
STARTER_CREDIT_ROWS = 10_000_000  # $100 at the recorded $10 per million rows
PAGE = 1000
RPS = 4.0  # Starter limit is 5 req/s per project


class BudgetExceeded(RuntimeError):
    pass


class Ledger:
    """Reserve a page durably before sending. A lost reply keeps its maximum charge held."""

    def __init__(self, path: Path, cap_rows: int = DEFAULT_CAP_ROWS):
        if type(cap_rows) is not int or not 0 <= cap_rows <= STARTER_CREDIT_ROWS:
            raise ValueError(f"--cap-rows must be an integer from 0 to {STARTER_CREDIT_ROWS:,}")
        self.path = Path(path)
        self.cap = cap_rows
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.total = 0
        self.pending: dict[str, int] = {}
        self.offset = 0
        with self._locked():
            pass

    @contextmanager
    def _locked(self):
        with self.path.open("a+b") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            if fh.tell() < self.offset:
                raise ValueError("Goldsky ledger was truncated")
            fh.seek(self.offset)
            for line in fh:
                if not line.endswith(b"\n"):
                    raise ValueError("incomplete Goldsky ledger entry")
                if line.strip():
                    self._apply(orjson.loads(line))
                self.offset = fh.tell()
            yield fh

    def _apply(self, entry):
        if "reserve" in entry:
            self.pending[entry["reserve"]] = entry["limit"]
        if "settle" in entry:
            del self.pending[entry["settle"]]
        self.total += entry["rows"]

    def _append(self, fh, **entry):
        fh.write(orjson.dumps({"tw": time.time_ns(), **entry}) + b"\n")
        fh.flush()
        os.fsync(fh.fileno())
        self._apply(entry)
        self.offset = fh.tell()

    def _check(self, rows: int) -> None:
        if rows < 0 or self.total + sum(self.pending.values()) + rows > self.cap:
            raise BudgetExceeded(f"row cap {self.cap:,} would be exceeded "
                                 f"(spent {self.total:,}, reserved {sum(self.pending.values()):,})")

    def check(self, next_rows: int = PAGE) -> None:
        with self._locked():
            self._check(next_rows)

    def add(self, job: str, rows: int, url: str) -> None:
        with self._locked() as fh:
            self._check(rows)
            self._append(fh, job=job, rows=rows, url=url)

    def reserve(self, job: str, limit: int, url: str) -> str:
        with self._locked() as fh:
            self._check(limit)
            rid = uuid.uuid4().hex
            self._append(fh, reserve=rid, limit=limit, rows=0, job=job, url=url)
            return rid

    def settle(self, rid: str, rows: int, job: str, url: str) -> None:
        with self._locked() as fh:
            if rid not in self.pending or rows < 0:
                raise ValueError("invalid Goldsky reservation")
            self._append(fh, settle=rid, rows=rows, job=job, url=url)

    def summary(self) -> dict:
        with self._locked():
            held = sum(self.pending.values())
            return {"rows": self.total, "usd_est": round(self.total * USD_PER_ROW, 4),
                    "cap_rows": self.cap, "cap_usd": round(self.cap * USD_PER_ROW, 2),
                    "reserved_rows": held, "remaining_rows": self.cap - self.total - held}


@contextmanager
def activity_writer(root: Path):
    stream = root / "goldsky_activity"
    stream.mkdir(parents=True, exist_ok=True)
    with (stream / ".pull.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        writer = SegmentWriter(root, "goldsky_activity")
        try:
            yield writer
        finally:
            writer.close()


async def pull(job: str, params: dict, key: str, ledger: Ledger, writer: SegmentWriter,
               max_rows: int, session: aiohttp.ClientSession | None = None) -> int:
    """Return the number of rows fetched for a bounded query."""
    rows, _ = await _pull_pages(job, params, key, ledger, writer, max_rows, session)
    return rows


async def _pull_pages(job: str, params: dict, key: str, ledger: Ledger, writer: SegmentWriter,
                      max_rows: int, session: aiohttp.ClientSession | None = None) -> tuple[int, bool]:
    """Return fetched rows and whether the last response advertised another page."""
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
            q = {**params, "limit": limit}
            if cursor:
                q["cursor"] = cursor
            url = f"{BASE}?{urlencode(q)}"
            rid = ledger.reserve(job, limit, url)
            sent_w, sent_m = time.time_ns(), time.monotonic_ns()
            async with session.get(url, headers={"X-API-Key": key}, allow_redirects=False) as r:
                body = await r.text()
                status = r.status
                retry = r.headers.get("Retry-After")
            if status == 429:
                ledger.settle(rid, 0, job, url)
                await asyncio.sleep(float(retry or 1.0))
                continue
            if status != 200:
                if 300 <= status < 500:
                    ledger.settle(rid, 0, job, url)
                writer.append("", meta={"job": job, "url": url, "status": status,
                                          "sent_tw": sent_w, "sent_tm": sent_m})
                raise RuntimeError(f"Goldsky returned HTTP {status}")
            doc = orjson.loads(body)
            if not isinstance(doc.get("data"), list):
                raise ValueError("missing Goldsky page data")
            rows = len(doc["data"])
            ledger.settle(rid, rows, job, url)
            if rows > limit:
                raise ValueError(f"Goldsky returned {rows} rows for a limit of {limit}")
            writer.append(body, meta={"job": job, "url": url, "status": status, "rows": rows,
                                      "sent_tw": sent_w, "sent_tm": sent_m})
            got += rows
            cursor = doc.get("next_cursor")
            if not cursor or rows == 0 or got >= max_rows:
                break
            await asyncio.sleep(1.0 / RPS)
    finally:
        writer.flush()
        if own:
            await session.close()
    return got, bool(cursor)


def _nonnegative_int(value: str) -> int:
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be a nonnegative integer")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-goldsky")
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--cap-rows", type=int, default=DEFAULT_CAP_ROWS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("window", help="all activity in a time window (<= 7 days)")
    w.add_argument("--start", type=int, required=True)
    w.add_argument("--end", type=int, required=True)
    w.add_argument("--max-rows", type=_nonnegative_int, required=True)
    w.add_argument("--job", required=True)
    c = sub.add_parser("cohort", help="per-wallet history for a frozen cohort file")
    c.add_argument("--cohort", type=Path, required=True)
    c.add_argument("--n-candidates", type=_nonnegative_int, default=10)
    c.add_argument("--n-controls", type=_nonnegative_int, default=10)
    c.add_argument("--days", type=float, default=7.0)
    c.add_argument("--end", type=int, help="explicit end epoch (default now); start = end - days")
    c.add_argument("--max-rows-per-wallet", type=_nonnegative_int, default=20_000)
    c.add_argument("--skip", type=_nonnegative_int, default=0, help="skip the first N candidates and controls")
    c.add_argument("--run-budget-rows", type=_nonnegative_int, help="stop this run after this many rows")
    c.add_argument("--execute", action="store_true", help="without this, only print the plan")
    sub.add_parser("budget")
    a = ap.parse_args(argv)
    try:
        ledger = Ledger(a.ledger, a.cap_rows)
    except ValueError as exc:
        ap.error(str(exc))
    if a.cmd == "budget":
        print(orjson.dumps(ledger.summary()).decode())
        return 0
    key = os.environ.get("GOLDSKY_FEEDS_API_KEY")
    if a.cmd == "cohort" and not a.execute:
        return _cohort(a, ledger, "")
    if not key:
        print("GOLDSKY_FEEDS_API_KEY not set", file=sys.stderr)
        return 2
    if a.cmd == "cohort":
        return _cohort(a, ledger, key)
    if not 0 < a.end - a.start <= 604_800:
        ap.error("Feeds time windows are limited to 7 days")
    with activity_writer(a.data) as writer:
        n = asyncio.run(pull(a.job, {"start_time": a.start, "end_time": a.end}, key, ledger,
                             writer, a.max_rows))
    print(orjson.dumps({"job": a.job, "rows": n, **ledger.summary()}).decode())
    return 0


def _cohort(a, ledger: Ledger, key: str) -> int:
    doc = orjson.loads(a.cohort.read_bytes())
    wallets = ([("cand", w) for w in doc["candidates"][a.skip:a.skip + a.n_candidates]]
               + [("ctrl", w) for w in doc["controls"][a.skip:a.skip + a.n_controls]])
    worst = len(wallets) * a.max_rows_per_wallet
    if a.run_budget_rows is not None:
        worst = min(worst, a.run_budget_rows)
    plan = {"cohort_sha256": doc["sha256"], "wallets": len(wallets), "days": a.days,
            "run_budget_rows": a.run_budget_rows,
            "max_rows_per_wallet": a.max_rows_per_wallet, "worst_case_rows": worst,
            "worst_case_usd": round(worst * USD_PER_ROW, 2), "ledger_before": ledger.summary()}
    print(orjson.dumps(plan).decode())
    if worst > plan["ledger_before"]["remaining_rows"]:
        print("plan exceeds remaining cap; refusing", file=sys.stderr)
        return 3
    if not a.execute or worst == 0:
        return 0
    end = a.end or int(time.time())
    start = end - int(a.days * 86400)
    tag = doc["sha256"][:12]
    spent = 0
    with activity_writer(a.data) as writer:
        for role, w in wallets:
            room = worst - spent
            if room <= 0:
                print(orjson.dumps({"stopped": "run budget reached", "spent": spent}).decode())
                break
            job = f"cohort-{tag}-{role}-{w}-{start}"
            params = {"address": w, "start_time": start, "end_time": end}
            n, truncated = asyncio.run(_pull_pages(job, params, key, ledger, writer,
                                                  min(a.max_rows_per_wallet, room)))
            spent += n
            print(orjson.dumps({"wallet": w, "role": role, "rows": n,
                                "truncated": truncated}).decode(), flush=True)
    print(orjson.dumps({"done": True, **ledger.summary()}).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
