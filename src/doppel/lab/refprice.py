"""Settlement reference series from Polymarket US (crypto: BRTI-based) at 1 s.

``GET /v1/asset-prices/history`` serves, without auth, 3600 candles per call, >= ~30 days back, a 1 s
series that is ALREADY the settlement statistic: the rolling 60 s average of BRTI, labeled at its
last second (tickCount=60; lags raw BRTI). Verified on 10 settled windows (FINDINGS E18):
Up/Down priceToBeat = A(S-1), settlement = A(E-1), ties settle Up. So for this series use
``updown_open_close`` below, NOT a second 60 s average.

Raw responses go to tape stream ``ref_prices``. A state file records the last fully fetched second
per asset, so the timer only tops up. Chunks overlap by a few seconds; readers dedupe by
(asset, timestamp).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import aiohttp
import orjson

from ..tape.segment import SegmentWriter, read_segment
from ..tape.sources.base import SSL_CTX, UA

URL = "https://gateway.polymarket.us/v1/asset-prices/history"
CHUNK = 3600
BUCKET = "ASSET_PRICE_BUCKET_1S"


async def fetch(session, asset: str, start: int, end: int) -> tuple[int, str]:
    q = {"assets": asset, "from": start, "to": end, "bucket": BUCKET}
    async with session.get(URL, params=q) as r:
        return r.status, await r.text()


async def run(root: Path, state_path: Path, assets: list[str], backfill_days: float,
              rps: float = 2.0, lag: int = 5) -> dict:
    state = orjson.loads(state_path.read_bytes()) if state_path.exists() else {}
    writer = SegmentWriter(root, "ref_prices")
    now = int(time.time()) - lag
    done = {}
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=SSL_CTX),
                                     timeout=aiohttp.ClientTimeout(total=30),
                                     headers={"user-agent": UA}) as s:
        for asset in assets:
            start = state.get(asset, now - int(backfill_days * 86400))
            calls = rows = 0
            while start < now:
                end = min(start + CHUNK - 1, now)
                sent = time.time_ns()
                status, body = await fetch(s, asset, start, end)
                calls += 1
                if status != 200:
                    writer.append(body, meta={"asset": asset, "from": start, "to": end,
                                              "status": status, "sent_tw": sent})
                    break
                n = sum(len(x.get("candles") or []) for x in orjson.loads(body).get("series", []))
                writer.append(body, meta={"asset": asset, "from": start, "to": end, "status": 200,
                                          "candles": n, "sent_tw": sent})
                rows += n
                start = end + 1
                state[asset] = start
                state_path.write_bytes(orjson.dumps(state))
                await asyncio.sleep(1.0 / rps)
            done[asset] = {"calls": calls, "candles": rows, "next_from": state.get(asset)}
    writer.close()
    return done


def load_series(root: Path, asset: str) -> dict[int, float]:
    """timestamp -> close, deduplicated across chunks."""
    out: dict[int, float] = {}
    for f in sorted((root / "ref_prices").glob("*/*.dtape.zst")):
        for rec in read_segment(f, verify=True):
            m = rec.get("meta") or {}
            if m.get("asset") != asset or m.get("status") != 200:
                continue
            for ser in orjson.loads(rec["raw"]).get("series", []):
                for c in ser.get("candles") or []:
                    out[int(c["timestamp"])] = float(c["close"])
    return out


def updown_open_close(series: dict[int, float], start: int, end: int):
    """(priceToBeat, settlement) of a PM US Up/Down window from the published averaged series."""
    return series.get(start - 1), series.get(end - 1)


def settlement_value(series: dict[int, float], t: int) -> float | None:
    """Average of 60 RAW index prints in [t-60, t); None if any second is missing.
    Use only with a raw-index series (e.g. a BRTI replica), never the PM US averaged series."""
    xs: list[float] = []
    for s in range(t - 60, t):
        v = series.get(s)
        if v is None:
            return None
        xs.append(v)
    return sum(xs) / 60.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-refprice")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--assets", default="crypto:btc,crypto:eth,crypto:sol,crypto:xrp")
    ap.add_argument("--backfill-days", type=float, default=30.0)
    a = ap.parse_args(argv)
    res = asyncio.run(run(a.data, a.state, a.assets.split(","), a.backfill_days))
    print(orjson.dumps(res).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
