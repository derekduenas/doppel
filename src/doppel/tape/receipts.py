"""Hourly completeness receipts.

A receipt states, per stream and hour: records, control events, the longest silence between data
records, and CRC validity. For the redundant RTDS pair it also states identity-tape agreement
(unique tx hashes seen by a, by b, and by both). Chain-level completeness (RTDS vs on-chain fills)
is added in M1, when the Goldsky/log ingest exists. Until then a receipt never claims it.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import orjson

from .segment import atomic_write, read_segment


class ReceiptPending(ValueError):
    """At least one input can still change before its producer commits the hour."""


def stream_hour_summary(path: Path, *, txs: set[str] | None = None) -> dict:
    stat = path.stat()
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
            if txs is not None:
                _add_tx(txs, rec["raw"])
            tw = rec["tw"]
            first = tw if first is None else first
            last = tw
            if last_tw is not None:
                max_gap = max(max_gap, (tw - last_tw) / 1e9)
            last_tw = tw
    except ValueError:
        crc_ok = False
    with path.open("rb") as fd:
        sha = hashlib.file_digest(fd, "sha256").hexdigest()
    after = path.stat()
    if (stat.st_ino, stat.st_size, stat.st_ctime_ns) != (after.st_ino, after.st_size, after.st_ctime_ns):
        raise ReceiptPending(f"segment changed while reading: {path}")
    return {"file": str(path), "records": n, "data_records": data, "ctl": dict(ctl),
            "max_silence_s": round(max_gap, 3), "first_tw": first, "last_tw": last,
            "crc_ok": crc_ok, "bytes": stat.st_size, "sha256": sha}


def _add_tx(txs: set[str], raw: str) -> None:
    if raw and raw[0] == "{":
        try:
            payload = orjson.loads(raw).get("payload")
        except orjson.JSONDecodeError:
            return
        tx = payload.get("transactionHash") if isinstance(payload, dict) else None
        if isinstance(tx, str) and tx:
            txs.add(tx.lower())


def rtds_tx_set(path: Path) -> set[str]:
    out: set[str] = set()
    for rec in read_segment(path, verify=False):
        _add_tx(out, rec["raw"])
    return out


def build_receipt(root: Path, day: str, hour: int, *, require_brti_import: bool = False) -> dict:
    root = Path(root)
    hh = f"{hour:02d}"
    end_s = int(datetime.strptime(day, "%Y%m%d").replace(hour=hour, tzinfo=timezone.utc).timestamp()) + 3600
    out = root / "receipts" / day / f"{hh}.json"
    if out.exists():
        return orjson.loads(out.read_bytes())  # original receipts are immutable, including legacy ones
    if end_s > time.time():
        raise ReceiptPending(f"UTC hour is still open: {day}/{hh}")
    raw_root = root / "ref_brti_raw"
    expect_raw = require_brti_import or raw_root.is_dir()
    if expect_raw:
        watermark = raw_root / "import-watermark.json"
        if not watermark.is_file() or orjson.loads(watermark.read_bytes())["max_t_ms"] < end_s * 1000:
            raise ReceiptPending(f"BRTI import has not committed past {day}/{hh}")
    streams = {}
    unsealed_errors = []
    pair: dict[str, set[str]] = {}
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and p.name not in ("receipts",))
    for d in dirs:
        f = d / day / f"{hh}.dtape.zst"
        if f.exists():
            txs = pair.setdefault(d.name, set()) if d.name in ("rtds_a", "rtds_b") else None
            summary = stream_hour_summary(f, txs=txs)
            rel = str(f.relative_to(d))
            manifest = d / "manifest.jsonl"
            seals = manifest.read_bytes().split(b"\n")[:-1] if manifest.is_file() else []
            seal = None
            for line in reversed(seals):
                if line:
                    candidate = orjson.loads(line)
                    if candidate["file"] == rel:
                        seal = candidate
                        break
            if seal is None or (seal["bytes"], seal["sha256"]) != (summary["bytes"], summary["sha256"]):
                if summary["crc_ok"]:
                    raise ReceiptPending(f"segment has no matching published revision: {f}")
                unsealed_errors.append(d.name)
            streams[d.name] = summary
    rec = {"schema_version": 2, "day": day, "hour": hour, "built_tw": time.time_ns(), "streams": streams,
           "missing_expected_streams": ["ref_brti_raw"] if expect_raw and "ref_brti_raw" not in streams else [],
           "chain_completeness": "not_measured_until_M1"}
    if "rtds_a" in pair and "rtds_b" in pair:
        sa, sb = pair["rtds_a"], pair["rtds_b"]
        union = sa | sb
        rec["rtds_pair"] = {"tx_a": len(sa), "tx_b": len(sb), "tx_both": len(sa & sb),
                            "tx_union": len(union),
                            "a_coverage": round(len(sa) / len(union), 5) if union else None,
                            "b_coverage": round(len(sb) / len(union), 5) if union else None}
    if unsealed_errors:
        # Preserve CRC diagnostics without permanently certifying an in-progress frame.
        rec["pending_unsealed_streams"] = unsealed_errors
        return rec
    try:
        atomic_write(out, orjson.dumps(rec, option=orjson.OPT_INDENT_2), replace=False)
    except FileExistsError:
        return orjson.loads(out.read_bytes())
    return rec


_STUCK: dict[tuple[str, int], tuple] = {}   # (root, hour) -> file signature at its last failed attempt


def _hour_signature(root: Path, day: str, hh: str) -> tuple:
    """Sizes and mtimes of everything a receipt for this hour depends on. Cheap: stat calls only."""
    sig = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        for f in (d / day / f"{hh}.dtape.zst", d / "manifest.jsonl", d / "import-watermark.json"):
            if f.exists():
                st = f.stat()
                sig.append((str(f), st.st_size, st.st_mtime_ns))
    return tuple(sig)


def finish_receipts(root: Path, current_hour: int, require_brti_import: bool) -> None:
    """Retry every closed hour since this recorder first enabled durable receipt tracking."""
    cursor = root / "receipts" / ".first-pending-hour"
    if not cursor.is_file():
        atomic_write(cursor, str(current_hour).encode(), replace=False)
        return
    first = int(cursor.read_bytes())
    next_pending = current_hour
    for stamp in range(first, current_hour, 3600):
        dt = datetime.fromtimestamp(stamp, tz=timezone.utc)
        # Release review 2026-11-27: an hour that cannot seal was decompressed and hashed again every
        # minute, forever, inside the recorder. Retry it only when one of its files has changed.
        key, sig = (str(root), stamp), _hour_signature(root, dt.strftime("%Y%m%d"), f"{dt.hour:02d}")
        if _STUCK.get(key) == sig:
            next_pending = min(next_pending, stamp)
            continue
        try:
            doc = build_receipt(root, dt.strftime("%Y%m%d"), dt.hour, require_brti_import=require_brti_import)
            if doc.get("pending_unsealed_streams"):
                raise ReceiptPending(f"failed CRC on unsealed streams {doc['pending_unsealed_streams']}")
        except (OSError, ValueError) as exc:
            next_pending = min(next_pending, stamp)
            _STUCK[key] = sig
            logging.warning("receipt pending for %s: %s", dt.isoformat(), exc)
    if next_pending > first:
        atomic_write(cursor, str(next_pending).encode())
