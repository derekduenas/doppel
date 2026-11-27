"""Append-only, hash-chained JSONL journal of one paper run.

Every line is one JSON object. The journal adds four fields to what the runtime emits:
``n`` (line index), ``prev`` (``h`` of the previous line, 64 zeros for the first), ``h`` =
sha256(prev || canonical body) where the body is the record without ``h``, and nothing else is
trusted. Editing, inserting, deleting or reordering a line breaks the chain at that line.

Lines are written in batches (one runtime cycle) with one ``write`` and an ``fsync``. A batch is
COMMITTED by its last line, a ``cycle`` record. On open, lines after the last committed cycle are an
interrupted batch: they are cut off and a ``recovery`` record names how many lines and the sha256
of the bytes removed, so nothing disappears without a trace. A line that is not valid JSON anywhere
except the very end of the file is tampering, not a crash, and refuses to open.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import orjson

GENESIS = "0" * 64
FILE = "journal.jsonl"
CHAIN_FIELDS = ("n", "prev", "h")


class JournalError(ValueError):
    pass


def _digest(prev: str, body: dict) -> str:
    return hashlib.sha256(prev.encode() + orjson.dumps(body, option=orjson.OPT_SORT_KEYS)).hexdigest()


def verify_lines(lines: list[bytes]) -> list[dict]:
    """Parse and check the chain; raise ``JournalError`` naming the first bad line."""
    out: list[dict] = []
    prev = GENESIS
    for i, line in enumerate(lines):
        try:
            rec = orjson.loads(line)
        except orjson.JSONDecodeError as exc:
            raise JournalError(f"line {i}: not JSON") from exc
        h = rec.pop("h", None)
        if rec.get("n") != i or rec.get("prev") != prev or h != _digest(prev, rec):
            raise JournalError(f"line {i}: hash chain broken")
        rec["h"] = h
        out.append(rec)
        prev = h
    return out


def strip(rec: dict) -> dict:
    """A record without the chain fields and the wall clock (what replay must reproduce)."""
    return {k: v for k, v in rec.items() if k not in CHAIN_FIELDS and k != "wall"}


class Journal:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.path = self.dir / FILE
        self.records: list[dict] = []
        self._prev = GENESIS

    def open(self) -> list[dict]:
        """Load and verify; cut an uncommitted tail (see module docstring). Returns the records."""
        self.dir.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.records, self._prev = [], GENESIS
            return self.records
        data = self.path.read_bytes()
        lines = data.split(b"\n")
        torn = b""
        if lines[-1] != b"":                 # no final newline: the last write was torn
            torn = lines.pop()
        else:
            lines.pop()
        recs = verify_lines(lines)
        last_commit = max((i for i, r in enumerate(recs) if r.get("k") in ("cycle", "run")), default=-1)
        cut = recs[last_commit + 1:]
        keep_bytes = sum(len(x) + 1 for x in lines[:last_commit + 1])
        self.records = recs[:last_commit + 1]
        self._prev = self.records[-1]["h"] if self.records else GENESIS
        if cut or torn:
            removed = data[keep_bytes:]
            with open(self.path, "r+b") as fh:
                fh.truncate(keep_bytes)
                fh.flush()
                os.fsync(fh.fileno())
            self.append([{"k": "recovery", "dropped_lines": len(cut), "torn_bytes": len(torn),
                          "dropped_sha256": hashlib.sha256(removed).hexdigest()}])
        return self.records

    def append(self, batch: list[dict]) -> None:
        if not batch:
            return
        buf = bytearray()
        for rec in batch:
            body = {k: v for k, v in rec.items() if k not in CHAIN_FIELDS}
            body["n"] = len(self.records)
            body["prev"] = self._prev
            h = _digest(self._prev, body)
            body["h"] = h
            buf += orjson.dumps(body, option=orjson.OPT_SORT_KEYS) + b"\n"
            self.records.append(body)
            self._prev = h
        with open(self.path, "ab") as fh:
            fh.write(bytes(buf))
            fh.flush()
            os.fsync(fh.fileno())


def load(directory: Path) -> list[dict]:
    """Read-only load with full verification (a torn tail is reported, not repaired)."""
    p = Path(directory) / FILE
    lines = p.read_bytes().split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    return verify_lines(lines)
