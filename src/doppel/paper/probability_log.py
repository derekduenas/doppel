"""Append-only JSONL projection of committed paper forecast and watchdog records."""

from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Iterable

import orjson

from .journal import JournalError

KINDS = frozenset(("shadow_probability", "shadow_watchdog"))


def _lines(records: Iterable[dict]) -> Iterable[bytes]:
    for record in records:
        if record["k"] in KINDS:
            yield orjson.dumps(record["row"], option=orjson.OPT_SORT_KEYS) + b"\n"


class ProbabilityLog:
    def __init__(self, directory: Path):
        self.path = directory / "shadow_probs.jsonl"

    def _open(self):
        fd = os.open(self.path, os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CREAT | os.O_APPEND | os.O_RDWR, 0o640)
        file = os.fdopen(fd, "r+b")
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            file.close()
            raise JournalError("shadow probability stream must be a regular file")
        return file

    def append(self, batch: Iterable[dict]) -> None:
        blob = b"".join(_lines(batch))
        if blob:
            with self._open() as file:
                file.write(blob)
                file.flush()
                os.fsync(file.fileno())

    def recover(self, records: Iterable[dict]) -> None:
        """Verify the published prefix, append missing committed bytes and reject divergence."""
        with self._open() as file:
            for expected in _lines(records):
                line = file.read(len(expected))
                if line == expected:
                    continue
                if not expected.startswith(line):
                    raise JournalError("shadow probability stream diverges from the committed journal")
                # A crash between the two files may leave a matching unfinished line.
                file.write(expected[len(line):])
                file.flush()
            if file.read(1):
                raise JournalError("shadow probability stream has records outside the committed journal")
            os.fsync(file.fileno())
