"""Checked handoff from reconstruction to rule fitting and fidelity."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import orjson

from .ordertape import committed_parts


@dataclass(frozen=True)
class Reconstruction:
    orders: Path
    matched_orders: Path
    summary: Path


def _sha(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def _checked_file(root: Path, name: str, outputs: dict) -> Path:
    path = root / name
    metadata = outputs.get(name) or {}
    if not path.is_file() or _sha(path) != metadata.get("sha256"):
        raise ValueError(f"reconstruction file is missing or its checksum changed: {name}")
    return path


def read_reconstruction(path: Path, wallet: str, start: int, end: int, *, exact: bool = False) -> Reconstruction:
    """Return verified artifacts for an interval covered by the wallet's reconstruction."""
    doc = orjson.loads(path.read_bytes())
    if doc.get("status") not in (None, "complete") or doc.get("stopped"):
        raise ValueError("reconstruction manifest is unfinished")
    span = doc.get("range") or {}
    lo, hi = span.get("from"), span.get("to")
    if (type(lo) is not int or type(hi) is not int or not lo <= start < end <= hi
            or exact and (start, end) != (lo, hi)):
        raise ValueError("requested interval is outside the reconstruction range or is not an exact match")
    wallet = wallet.lower()
    row = (doc.get("wallets") or {}).get(wallet)
    if row is None or len(wallet) != 42 or not wallet.startswith("0x") or any(
            c not in "0123456789abcdef" for c in wallet[2:]):
        raise ValueError("wallet is absent from the reconstruction manifest")
    steps = doc.get("steps") or {}
    tape = steps.get("ordertape") or {}
    matched = steps.get("ordermatch") or {}
    trust = row.get("untrusted") or {}
    coverage = row.get("tape") or {}
    expected, total = coverage.get("fills_expected"), coverage.get("fills_in_range")
    if (tape.get("complete") is not True or tape.get("stale") is not False
            or matched.get("stale") is not False or trust.get("ordertape") != []
            or trust.get("ordermatch") != [] or type(expected) is not int
            or type(total) is not int or expected != total or total <= 0):
        raise ValueError("reconstruction order tape or placement evidence is incomplete, stale, or untrusted")

    root = path.resolve().parent
    orders_meta = (tape.get("outputs") or {}).get("orders") or {}
    orders_name = orders_meta.get("path") or (doc.get("args") or {}).get("orders")
    if not orders_name:
        raise ValueError("reconstruction manifest has no order tape path")
    orders = Path(orders_name)
    if not orders.is_dir() and (root / "ordertape").is_dir():
        orders = root / "ordertape"
    parts = {p.relative_to(orders).as_posix(): _sha(p) for p in committed_parts(orders)}
    digest = hashlib.sha256(orjson.dumps(parts, option=orjson.OPT_SORT_KEYS)).hexdigest()
    if not parts or digest != orders_meta.get("sha256"):
        raise ValueError("reconstruction order tape is missing or its checksum changed")
    own = _checked_file(root, f"wallets/{wallet}/orders.jsonl", row.get("outputs") or {})
    summary = _checked_file(root, "ordermatch/ordermatch.json", matched.get("outputs") or {})
    return Reconstruction(orders, own, summary)
