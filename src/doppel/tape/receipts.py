"""Hourly completeness receipts.

A receipt states, per stream and hour: records, control events, the longest silence between data
records, and CRC validity. For the redundant RTDS pair it also states identity-tape agreement
(unique tx hashes seen by a, by b, and by both). Chain-level completeness (RTDS vs on-chain fills)
is added in M1, when the Goldsky/log ingest exists. Until then a receipt never claims it.
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path

import orjson

from .segment import read_segment


def stream_hour_summary(path: Path) -> dict:
    n = data = 0
    ctl: Counter = Counter()
    last_tw = None
    max_gap = 0.0
    first = last = None
    crc_ok = True
    try:
        for rec in read_segment(path, verify=True, strict=True):
            n += 1
            m = rec.get("meta") or {}
            if "ctl" in m:
                ctl[m["ctl"]] += 1
                continue
            data += 1
            tw = rec["tw"]
            first = tw if first is None else first
            last = tw
            if last_tw is not None:
                max_gap = max(max_gap, (tw - last_tw) / 1e9)
            last_tw = tw
    except ValueError:
        crc_ok = False
    return {"file": str(path), "records": n, "data_records": data, "ctl": dict(ctl),
            "max_silence_s": round(max_gap, 3), "first_tw": first, "last_tw": last,
            "crc_ok": crc_ok, "bytes": path.stat().st_size}


def rtds_tx_set(path: Path) -> set[str]:
    out: set[str] = set()
    for rec in read_segment(path, verify=False):
        raw = rec["raw"]
        if not raw or raw[0] != "{":
            continue
        try:
            p = orjson.loads(raw).get("payload") or {}
        except orjson.JSONDecodeError:
            continue
        tx = p.get("transactionHash")
        if tx:
            out.add(tx.lower())
    return out


def build_receipt(root: Path, day: str, hour: int) -> dict:
    root = Path(root)
    hh = f"{hour:02d}"
    streams = {}
    for d in sorted(p for p in root.iterdir() if p.is_dir() and p.name not in ("receipts",)):
        f = d / day / f"{hh}.dtape.zst"
        if f.exists():
            streams[d.name] = stream_hour_summary(f)
    rec = {"day": day, "hour": hour, "built_tw": time.time_ns(), "streams": streams,
           "chain_completeness": "not_measured_until_M1"}
    a, b = root / "rtds_a" / day / f"{hh}.dtape.zst", root / "rtds_b" / day / f"{hh}.dtape.zst"
    if a.exists() and b.exists():
        sa, sb = rtds_tx_set(a), rtds_tx_set(b)
        union = sa | sb
        rec["rtds_pair"] = {"tx_a": len(sa), "tx_b": len(sb), "tx_both": len(sa & sb),
                            "tx_union": len(union),
                            "a_coverage": round(len(sa) / len(union), 5) if union else None,
                            "b_coverage": round(len(sb) / len(union), 5) if union else None}
    out = root / "receipts" / day / f"{hh}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(orjson.dumps(rec, option=orjson.OPT_INDENT_2))
    return rec
