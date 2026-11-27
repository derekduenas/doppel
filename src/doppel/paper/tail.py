"""Read-only tail of the recorder's tape segments.

The recorder writes ``<root>/<stream>/<YYYYMMDD>/<HH>.dtape.zst`` as independent zstd frames and
flushes a frame at most every second (Python recorder: also every 5 s). ``tape.segment._frames``
re-reads from the start and ``read_segment(strict=True)`` raises on the frame being written, so the
tail keeps its own byte offset per file and advances it only past COMPLETE frames. An incomplete
frame at the end of a file is left for the next poll. Every record's CRC is checked.

Offsets are the tail's whole state: a cycle marker in the journal stores them, so a restart can
re-read exactly the bytes each committed cycle had seen.
"""

from __future__ import annotations

import zlib
from pathlib import Path

import orjson
import zstandard

from ..tape.segment import segment_path

NS = 1_000_000_000


def read_new_frames(path: Path, offset: int, limit: int | None = None) -> tuple[list[dict], int]:
    """Records of the complete frames in ``path`` from byte ``offset`` (up to byte ``limit``)."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        buf = fh.read() if limit is None else fh.read(max(0, limit - offset))
    out: list[dict] = []
    pos = 0
    while pos < len(buf):
        obj = zstandard.ZstdDecompressor().decompressobj()
        try:
            chunk = obj.decompress(buf[pos:])
        except zstandard.ZstdError:
            break                        # a frame being written can look corrupt: wait for more
        if not obj.eof:
            break
        used = len(buf) - pos - len(obj.unused_data)
        for line in chunk.splitlines():
            if not line:
                continue
            rec = orjson.loads(line)
            if zlib.crc32(rec["raw"].encode("utf-8")) != rec["crc"]:
                raise ValueError(f"CRC mismatch seq={rec.get('seq')} in {path}")
            out.append(rec)
        pos += used
    return out, offset + pos


class StreamTail:
    """Tail of one stream from receive time ``from_ns`` on (records with smaller ``tw`` dropped)."""

    def __init__(self, root: Path, stream: str, from_ns: int, offsets: dict[str, int] | None = None):
        self.root, self.stream, self.from_ns = Path(root), stream, from_ns
        self.hour = from_ns // NS // 3600 * 3600
        self.offsets: dict[str, int] = dict(offsets or {})   # path relative to the stream dir -> bytes
        self.dirty: dict[str, int] = {}     # offsets advanced since the caller last took them
        self.last_tw: int | None = None

    def _path(self, hour: int) -> Path:
        return segment_path(self.root, self.stream, hour * NS)

    def poll(self, now_ns: int, limits: dict[str, int] | None = None) -> list[dict]:
        """New records of every hour file from the tail's first hour to the hour of ``now_ns``.
        With ``limits`` (a replay), read each file only up to the recorded offset."""
        out: list[dict] = []
        last_hour = now_ns // NS // 3600 * 3600
        for hour in range(self.hour, last_hour + 1, 3600):
            p = self._path(hour)
            rel = p.relative_to(self.root / self.stream).as_posix()
            lim = None if limits is None else limits.get(rel)
            if limits is not None and lim is None:
                continue
            if not p.exists():
                continue
            recs, off = read_new_frames(p, self.offsets.get(rel, 0), lim)
            if off != self.offsets.get(rel, 0):
                self.dirty[rel] = off
            self.offsets[rel] = off
            out.extend(r for r in recs if r["tw"] >= self.from_ns)
        # hours more than one before now are complete (the writer only appends to the hour of tw)
        while self.hour < last_hour - 3600:
            rel = self._path(self.hour).relative_to(self.root / self.stream).as_posix()
            self.offsets.pop(rel, None)
            self.hour += 3600
        for r in out:
            self.last_tw = r["tw"] if self.last_tw is None else max(self.last_tw, r["tw"])
        return out

    def take_dirty(self) -> dict[str, int]:
        d, self.dirty = self.dirty, {}
        return d
