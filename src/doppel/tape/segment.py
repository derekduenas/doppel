"""Append-only tape segments.

Layout: ``<root>/<stream>/<YYYYMMDD>/<HH>.dtape.zst``. Each segment is a sequence of independent zstd
frames. Each frame holds newline-delimited JSON records:

    {"seq": int, "stream": str, "tw": int, "tm": int, "crc": int, "raw": str, "meta": {...}?}

``tw`` is wall-clock receive time in ns and ``tm`` is monotonic receive time in ns. ``crc`` is
CRC32 of the UTF-8 ``raw`` payload exactly as received. Because every flush closes a zstd frame, a
crash loses at most the unflushed tail; frames already written stay readable. When a segment
closes, one manifest line goes to ``<root>/<stream>/manifest.jsonl``.
"""

from __future__ import annotations

import hashlib
import os
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import orjson
import zstandard

FLUSH_INTERVAL_S = 1.0


def segment_path(root: Path, stream: str, wall_ns: int) -> Path:
    t = time.gmtime(wall_ns // 1_000_000_000)
    return root / stream / time.strftime("%Y%m%d", t) / f"{t.tm_hour:02d}.dtape.zst"


@dataclass
class SegmentStats:
    records: int = 0
    raw_bytes: int = 0
    first_tw: int | None = None
    last_tw: int | None = None


@dataclass
class SegmentWriter:
    """One writer per stream. Not thread-safe; use from a single asyncio task or loop."""

    root: Path
    stream: str
    level: int = 6
    _path: Path | None = None
    _fh: BinaryIO | None = None
    _buf: list[bytes] = field(default_factory=list)
    _last_flush: float = 0.0
    _seq: int = 0
    _stats: SegmentStats = field(default_factory=SegmentStats)
    _cctx: Any = None
    _hash: Any = None

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self._cctx = zstandard.ZstdCompressor(level=self.level)
        self._seq = self._resume_seq()

    # -- public -------------------------------------------------------------------------------
    def append(self, raw: str | bytes, tw: int | None = None, tm: int | None = None,
               meta: dict | None = None) -> int:
        tw = time.time_ns() if tw is None else tw
        tm = time.monotonic_ns() if tm is None else tm
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        path = segment_path(self.root, self.stream, tw)
        if path != self._path:
            self._rotate(path)
        self._seq += 1
        rec = {"seq": self._seq, "stream": self.stream, "tw": tw, "tm": tm,
               "crc": zlib.crc32(raw.encode("utf-8")), "raw": raw}
        if meta:
            rec["meta"] = meta
        self._buf.append(orjson.dumps(rec) + b"\n")
        st = self._stats
        st.records += 1
        st.raw_bytes += len(raw)
        st.first_tw = tw if st.first_tw is None else st.first_tw
        st.last_tw = tw
        now = time.monotonic()
        if now - self._last_flush >= FLUSH_INTERVAL_S:
            self.flush()
        return self._seq

    def flush(self) -> None:
        self._last_flush = time.monotonic()
        if not self._buf or self._fh is None:
            return
        frame = self._cctx.compress(b"".join(self._buf))
        self._buf.clear()
        self._fh.write(frame)
        self._hash.update(frame)
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self.flush()
        if self._fh is not None:
            self._fh.close()
            self._write_manifest()
            self._fh = None
            self._path = None

    def close_if_stale(self, wall_ns: int | None = None) -> bool:
        """Close the open segment if it belongs to an earlier hour than ``wall_ns``."""
        wall_ns = time.time_ns() if wall_ns is None else wall_ns
        if self._path is not None and self._path != segment_path(self.root, self.stream, wall_ns):
            self.close()
            return True
        return False

    @property
    def seq(self) -> int:
        return self._seq

    # -- internals ----------------------------------------------------------------------------
    def _rotate(self, path: Path) -> None:
        self.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._hash = hashlib.sha256()
        if path.exists():  # restart inside the same hour: hash what is already there (rare)
            self._hash.update(path.read_bytes())
        self._fh = open(path, "ab")
        self._stats = SegmentStats()

    def _write_manifest(self) -> None:
        if self._path is None or self._stats.records == 0:
            return
        sha = self._hash.hexdigest()
        line = {"file": str(self._path.relative_to(self.root / self.stream)), "sha256": sha,
                "bytes": self._path.stat().st_size, "records": self._stats.records,
                "raw_bytes": self._stats.raw_bytes, "first_tw": self._stats.first_tw,
                "last_tw": self._stats.last_tw, "last_seq": self._seq, "closed_tw": time.time_ns()}
        man = self.root / self.stream / "manifest.jsonl"
        with open(man, "ab") as fh:
            fh.write(orjson.dumps(line) + b"\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _resume_seq(self) -> int:
        """Continue sequence numbers across restarts from the manifest and any open segment."""
        man = self.root / self.stream / "manifest.jsonl"
        last = 0
        if man.exists():
            for line in man.read_bytes().splitlines():
                if line.strip():
                    last = max(last, orjson.loads(line).get("last_seq", 0))
        # an un-manifested segment (crash) may hold higher seqs
        d = self.root / self.stream
        if d.exists():
            segs = sorted(d.glob("*/*.dtape.zst"))
            if segs:
                for rec in read_segment(segs[-1], verify=False):
                    last = max(last, rec["seq"])
        return last


def _frames(path: Path, read_size: int = 1 << 20):
    """Decompressed zstd frames of a segment, one at a time, reading the file in ``read_size``
    chunks (memory is one frame, not the whole file). A truncated or corrupt tail ends it."""
    dctx = zstandard.ZstdDecompressor()
    with open(path, "rb") as fh:
        buf = b""
        while True:
            obj = dctx.decompressobj()
            out: list[bytes] = []
            while True:
                if not buf:
                    buf = fh.read(read_size)
                    if not buf:
                        return  # clean end, or a truncated tail frame from a crash
                try:
                    out.append(obj.decompress(buf))
                except zstandard.ZstdError:
                    return
                if obj.eof:
                    buf = obj.unused_data
                    break
                buf = b""
            yield b"".join(out)


def read_segment(path: Path, verify: bool = True):
    """Yield records from a segment. Tolerates a truncated final frame. Raises on CRC mismatch
    when ``verify`` is true."""
    for chunk in _frames(Path(path)):
        for line in chunk.splitlines():
            if not line:
                continue
            rec = orjson.loads(line)
            if verify and zlib.crc32(rec["raw"].encode("utf-8")) != rec["crc"]:
                raise ValueError(f"CRC mismatch seq={rec['seq']} in {path}")
            yield rec
