"""Append-only, hash-chained JSONL journal of one paper run, rotated daily.

Files: ``journal-YYYYMMDD.jsonl`` (UTC day of the batch's logical time), read in name order as one
chain. Every line is one JSON object; the journal adds ``n`` (global line index), ``prev`` (``h`` of
the previous line, 64 zeros for the first) and ``h`` = sha256(prev || canonical body without ``h``).
Editing, inserting, deleting or reordering a line, or removing a whole file, breaks the chain.

The chain is NOT keyed: it proves the journal is internally consistent and detects any change to
what was written, but anyone with write access can rewrite the whole chain consistently. Anchoring
the head hash elsewhere (git, the registry, an off-host log) is what makes it tamper-evident against
such a writer.

Lines are written in batches (one runtime cycle) with one ``write`` and an ``fsync``. A batch is
COMMITTED by its last line, a ``cycle`` record (or ``run``, ``recovery``, ``resume``). On open, lines after the last
committed one are an interrupted batch: they are cut and a ``recovery`` record names how many lines
and the sha256 of the bytes removed. A line that is not valid JSON anywhere except at the very end
is tampering, not a crash, and the journal refuses to open.

Memory is bounded: the journal object keeps only the chain head, the line count and the current
file; readers stream (``iter_records``).
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Iterator

import orjson

GENESIS = "0" * 64
PATTERN = "journal-*.jsonl"
CHAIN_FIELDS = ("n", "prev", "h")
COMMITS = ("cycle", "run", "recovery", "resume")


class JournalError(ValueError):
    pass


def _digest(prev: str, body: dict) -> str:
    return hashlib.sha256(prev.encode() + orjson.dumps(body, option=orjson.OPT_SORT_KEYS)).hexdigest()


def files(directory: Path) -> list[Path]:
    return sorted(Path(directory).glob(PATTERN))


def day_name(t_ns: int) -> str:
    return "journal-" + time.strftime("%Y%m%d", time.gmtime(t_ns // 1_000_000_000)) + ".jsonl"


def iter_records(directory: Path, *, allow_torn_tail: bool = False) -> Iterator[dict]:
    """Verified records in chain order, streamed. A torn final line raises unless allowed (then it
    is silently not yielded)."""
    prev, n = GENESIS, 0
    fs = files(directory)
    for k, path in enumerate(fs):
        with open(path, "rb") as fh:
            for line in fh:
                if not line.endswith(b"\n"):
                    if allow_torn_tail and k == len(fs) - 1:
                        return
                    raise JournalError(f"{path.name}: torn line {n}")
                try:
                    rec = orjson.loads(line)
                except orjson.JSONDecodeError as exc:
                    raise JournalError(f"line {n}: not JSON") from exc
                h = rec.pop("h", None)
                if rec.get("n") != n or rec.get("prev") != prev or h != _digest(prev, rec):
                    raise JournalError(f"line {n}: hash chain broken")
                rec["h"] = h
                yield rec
                prev, n = h, n + 1


def strip(rec: dict) -> dict:
    """A record without the chain fields and the wall clock (what replay must reproduce)."""
    return {k: v for k, v in rec.items() if k not in CHAIN_FIELDS and k != "wall"}


class Journal:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.n = 0
        self.head = GENESIS
        self.path: Path | None = None

    def open(self) -> dict | None:
        """Verify every file, cut an uncommitted tail (see module docstring). Returns the ``run``
        record, or None for a new journal."""
        self.dir.mkdir(parents=True, exist_ok=True)
        first = None
        commit: int | None = None
        commit_h = GENESIS
        last_n = -1
        for rec in iter_records(self.dir, allow_torn_tail=True):
            first = first or rec
            last_n = rec["n"]
            if rec["k"] in COMMITS:
                commit = rec["n"]
                commit_h = rec["h"]
        fs = files(self.dir)
        if first is None or commit is None:
            for p in fs:            # only an uncommitted / torn first batch: nothing was committed
                p.unlink()
            self.n, self.head, self.path = 0, GENESIS, None
            return None
        # locate the byte offset just after the last committed line
        n = 0
        cut_from: tuple[Path, int] | None = None
        for p in fs:
            data = p.read_bytes()
            off = 0
            for line in data.splitlines(keepends=True):
                if n == commit:
                    cut_from = (p, off + len(line))
                n += 1
                off += len(line)
        assert cut_from is not None
        cut_path, cut_off = cut_from
        later = [p for p in fs if p.name > cut_path.name]
        tail = cut_path.read_bytes()[cut_off:]
        removed = tail + b"".join(p.read_bytes() for p in later)
        dropped = max(0, last_n - commit)
        self.n, self.head, self.path = commit + 1, commit_h, cut_path
        if removed:
            with open(cut_path, "r+b") as fh:
                fh.truncate(cut_off)
                fh.flush()
                os.fsync(fh.fileno())
            for p in later:
                p.unlink()
            torn = len(removed) - sum(len(x) for x in removed.splitlines(keepends=True) if x.endswith(b"\n"))
            self.append([{"k": "recovery", "dropped_lines": dropped, "torn_bytes": torn,
                          "dropped_sha256": hashlib.sha256(removed).hexdigest()}], t_ns=None)
        return first

    def append(self, batch: list[dict], t_ns: int | None) -> None:
        """Write one batch; ``t_ns`` (logical time) picks the day file, None = the current file."""
        if not batch:
            return
        if t_ns is not None:
            p = self.dir / day_name(t_ns)
            if self.path is None or p.name > self.path.name:
                self.path = p
        if self.path is None:
            raise JournalError("first batch needs a logical time")
        buf = bytearray()
        for rec in batch:
            body = {k: v for k, v in rec.items() if k not in CHAIN_FIELDS}
            body["n"] = self.n
            body["prev"] = self.head
            h = _digest(self.head, body)
            body["h"] = h
            buf += orjson.dumps(body, option=orjson.OPT_SORT_KEYS) + b"\n"
            self.n += 1
            self.head = h
        with open(self.path, "ab") as fh:
            fh.write(bytes(buf))
            fh.flush()
            os.fsync(fh.fileno())


def load(directory: Path) -> list[dict]:
    """Every record, verified (batch tools and tests; the runtime streams)."""
    return list(iter_records(directory))
