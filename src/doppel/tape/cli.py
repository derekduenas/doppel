"""doppel-tape: run the M0 recorders, build receipts, verify segments.

    doppel-tape run --data DIR [--only rtds_a,clob_intl,...] [--seconds N]
    doppel-tape receipt --data DIR --day YYYYMMDD --hour H
    doppel-tape verify --data DIR
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

import orjson

from .receipts import build_receipt, finish_receipts
from .segment import SegmentWriter, read_segment
from .sources.base import RateLimiter
from .sources.kalshi_brti import KalshiBrti
from .sources.pmus_ws import PmusMarketsWs
from .sources.venues import (BitstampTrades, CoinbaseTrades, IntlClobMarket, KalshiBooks,
                             KrakenTrades, PmusBooks, RtdsTrades)
from .universe import IntlUniverse

ASSETS = ("btc", "eth", "sol", "xrp")
HZ = ("5m", "15m")
CLOB_SHARDS = tuple(f"clob_{a}_{h}" for a in ASSETS for h in HZ)  # one socket per shard
ALL = ("rtds_a", "rtds_b", *CLOB_SHARDS, "pmus_books", "pmus_ws_5m", "pmus_ws_15m", "kalshi_books", "kalshi_brti",
       "spot_coinbase", "spot_kraken", "spot_bitstamp")


def build_sources(data: Path, only: set[str]):
    def w(name: str) -> SegmentWriter:
        return SegmentWriter(data, name, level=3 if name.startswith("clob_") else 6)

    pmus_lim = RateLimiter(10.0, burst=4)   # PM US public limit is 25 rps per IP; stay well under
    kalshi_lim = RateLimiter(8.0, burst=4)
    makers = {
        "rtds_a": lambda: RtdsTrades("rtds_a", w("rtds_a")),
        "rtds_b": lambda: RtdsTrades("rtds_b", w("rtds_b")),
        "pmus_books": lambda: PmusBooks("pmus_books", w("pmus_books"), pmus_lim),
        "kalshi_books": lambda: KalshiBooks("kalshi_books", w("kalshi_books"), kalshi_lim),
        # auth via the host signer socket; DOPPEL never holds the PM US key
        # One connection per horizon: PM US caps subscriptions per connection ("max subscriptions
        # per connection reached" when one socket pre-subscribed the next 15m window).
        "pmus_ws_5m": lambda: PmusMarketsWs("pmus_ws_5m", w("pmus_ws_5m"), horizons=("5m",)),
        "pmus_ws_15m": lambda: PmusMarketsWs("pmus_ws_15m", w("pmus_ws_15m"), horizons=("15m",)),
        # ALL key differs from the stream name on purpose (it is a reference-price stream)
        "kalshi_brti": lambda: KalshiBrti("ref_brti_live", w("ref_brti_live")),
        "spot_coinbase": lambda: CoinbaseTrades("spot_coinbase", w("spot_coinbase")),
        "spot_kraken": lambda: KrakenTrades("spot_kraken", w("spot_kraken")),
        "spot_bitstamp": lambda: BitstampTrades("spot_bitstamp", w("spot_bitstamp")),
    }
    for a in ASSETS:
        for h in HZ:
            n = f"clob_{a}_{h}"
            makers[n] = (lambda n=n, a=a, h=h:
                         IntlClobMarket(n, w(n), IntlUniverse(assets=(a,), horizons=(h,))))
    if "clob_intl" in only:  # shorthand for every shard
        only = (only - {"clob_intl"}) | set(CLOB_SHARDS)
    return [makers[n]() for n in ALL if n in only]


async def run(data: Path, only: set[str], seconds: float | None, *, require_brti_import: bool = False) -> int:
    data.mkdir(parents=True, exist_ok=True)
    sources = build_sources(data, only)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    started = time.time()
    last_hour = int(started // 3600) * 3600
    next_receipt_check = 0.0
    # Initialize inside the guarded loop. Receipt I/O must never keep capture from starting.
    tasks = [asyncio.create_task(s.run(stop), name=s.name) for s in sources]
    status_path = data / "status.json"
    try:
        while not stop.is_set():
            if time.monotonic() >= next_receipt_check:
                try:
                    await asyncio.to_thread(finish_receipts, data, last_hour, require_brti_import)
                except Exception as exc:  # bookkeeping must never stop capture (release review)
                    logging.warning("receipt tracking failed: %s", exc)
                next_receipt_check = time.monotonic() + 60
            try:
                await asyncio.wait_for(stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            for s in sources:
                s.writer.flush()
            st = {"tw": time.time_ns(), "pid": os.getpid(), "uptime_s": round(time.time() - started),
                  "sources": {s.name: s.stats.as_dict() for s in sources}}
            tmp = status_path.with_suffix(".tmp")
            tmp.write_bytes(orjson.dumps(st, option=orjson.OPT_INDENT_2))
            tmp.replace(status_path)
            h = int(time.time() // 3600) * 3600
            if h != last_hour:
                for s in sources:
                    s.writer.close_if_stale()
                next_receipt_check = 0.0
                last_hour = h
            if seconds is not None and time.time() - started >= seconds:
                stop.set()
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        for s in sources:
            s.writer.close()
    return 0


def verify(data: Path) -> int:
    import hashlib
    bad = 0
    checked = 0
    for man in sorted(data.glob("*/manifest.jsonl")):
        latest: dict[str, dict] = {}  # a file reopened after a restart has several lines; last wins
        for line in man.read_bytes().splitlines():
            if line.strip():
                m = orjson.loads(line)
                latest[m["file"]] = m
        for m in latest.values():
            f = man.parent / m["file"]
            if not f.exists() and not f.parent.exists():   # whole day pruned by the janitor (A3-7)
                print(f"PURGED {man.parent.name}/{m['file']} (day deleted by retention)")
                continue
            checked += 1
            ok_hash = False
            if f.is_file():
                with f.open("rb") as handle:
                    ok_hash = hashlib.file_digest(handle, "sha256").hexdigest() == m["sha256"]
            try:
                n = sum(1 for _ in read_segment(f, verify=True, strict=True)) if f.is_file() else -1
            except ValueError:
                n = -2
            ok = ok_hash and n >= m["records"]
            bad += not ok
            print(f"{'OK ' if ok else 'BAD'} {man.parent.name}/{m['file']} records={n}/{m['records']} sha={'ok' if ok_hash else 'MISMATCH'}")
    if not checked:
        print("verify: no retained, manifested segments to check")
        return 1
    print("verify:", "PASS" if bad == 0 else f"FAIL ({bad})")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-tape")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--data", type=Path, required=True)
    r.add_argument("--only", default=",".join(ALL))
    r.add_argument("--seconds", type=float)
    r.add_argument("--require-brti-import", action="store_true",
                   help="defer final receipts until the raw BRTI importer has closed each UTC hour")
    rc = sub.add_parser("receipt")
    rc.add_argument("--data", type=Path, required=True)
    rc.add_argument("--day", required=True)
    rc.add_argument("--hour", type=int, required=True)
    rc.add_argument("--require-brti-import", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--data", type=Path, required=True)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if a.cmd == "run":
        only = {x.strip() for x in a.only.split(",") if x.strip()}
        unknown = only - set(ALL) - {"clob_intl"}
        if unknown:
            ap.error(f"unknown sources: {sorted(unknown)}")
        return asyncio.run(run(a.data, only, a.seconds, require_brti_import=a.require_brti_import))
    if a.cmd == "receipt":
        try:
            result = build_receipt(a.data, a.day, a.hour, require_brti_import=a.require_brti_import)
        except (OSError, ValueError) as exc:
            ap.exit(1, f"receipt: {exc}\n")
        print(orjson.dumps(result, option=orjson.OPT_INDENT_2).decode())
        return int(bool(result.get("pending_unsealed_streams")))
    return verify(a.data)


if __name__ == "__main__":
    sys.exit(main())
