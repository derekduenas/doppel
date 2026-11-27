"""Read-only tails of the recorder's tape segments and of PERELMAN's raw BRTI files.

TAPE. The recorder writes ``<root>/<stream>/<YYYYMMDD>/<HH>.dtape.zst`` as independent zstd frames.
``tape.segment._frames`` re-reads from the start and ``read_segment(strict=True)`` raises on the
frame being written, so the tail keeps its own byte offset per file and advances only past COMPLETE
frames; an incomplete frame is left for the next poll. A poll reads at most ``max_bytes`` and stops
after the first frame holding a record past ``max_tw`` (the runtime never needs more than one step
ahead). Every record's CRC is checked; a CRC or JSON error raises ``TapeError``. So does a file
whose unread bytes have not become a complete frame for ``stall_s`` (a recorder that died mid-frame
or a corrupt tail: the twin's reader would also stop there, so paper stops rather than guess).

PERELMAN. ``lab.brti_import`` reads PERELMAN's stream directory (``*.jsonl`` being appended, older
hours as ``*.jsonl.gz``) with rows ``{"kind": "brti", "new": [[t_ms, v], ...], "received": ...}``.
The tail reads complete lines from a byte offset per ``.jsonl`` file and each ``.gz`` file once.

Offsets are a tail's whole state: cycle markers in the journal store the ones that advanced, so a
restart can re-read exactly the bytes each committed cycle had seen.
"""

from __future__ import annotations

import gzip
import os
import zlib
from pathlib import Path

import orjson
import zstandard

from ..tape.segment import segment_path

NS = 1_000_000_000


class TapeError(RuntimeError):
    pass


def read_new_frames(path: Path, offset: int, limit: int | None = None, max_tw: int | None = None,
                    max_bytes: int | None = None) -> tuple[list[dict], int, bool]:
    """(records of complete frames from ``offset``, new offset, stopped early by a cap)."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        n = -1 if limit is None else max(0, limit - offset)
        buf = fh.read(n)
    out: list[dict] = []
    pos = 0
    capped = False
    while pos < len(buf):
        if max_bytes is not None and pos >= max_bytes:
            capped = True
            break
        obj = zstandard.ZstdDecompressor().decompressobj()
        try:
            chunk = obj.decompress(buf[pos:])
        except zstandard.ZstdError:
            break                        # a frame being written can look corrupt: wait (stall check)
        if not obj.eof:
            break
        used = len(buf) - pos - len(obj.unused_data)
        top = None
        for line in chunk.splitlines():
            if not line:
                continue
            try:
                rec = orjson.loads(line)
                ok = zlib.crc32(rec["raw"].encode("utf-8")) == rec["crc"]
            except (orjson.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
                raise TapeError(f"unreadable record in {path} at byte {offset + pos}") from exc
            if not ok:
                raise TapeError(f"CRC mismatch seq={rec.get('seq')} in {path}")
            out.append(rec)
            top = rec["tw"] if top is None else max(top, rec["tw"])
        pos += used
        if max_tw is not None and top is not None and top > max_tw:
            capped = True
            break
    return out, offset + pos, capped


class StreamTail:
    """Tail of one stream from receive time ``from_ns`` on (records with smaller ``tw`` dropped)."""

    def __init__(self, root: Path, stream: str, from_ns: int):
        self.root, self.stream, self.from_ns = Path(root), stream, from_ns
        self.hour = from_ns // NS // 3600 * 3600
        self.offsets: dict[str, int] = {}   # path relative to the stream dir -> bytes consumed
        self.dirty: dict[str, int] = {}     # offsets advanced since the caller last took them
        self.stuck: dict[str, tuple[int, int]] = {}   # rel -> (offset, first now seen stuck there)
        self.last_tw: int | None = None
        self.capped = False

    def _path(self, hour: int) -> Path:
        return segment_path(self.root, self.stream, hour * NS)

    def poll(self, now_ns: int, limits: dict[str, int] | None = None, max_tw: int | None = None,
             max_bytes: int | None = None, stall_s: float | None = None) -> list[dict]:
        """New records of the hour files from the tail's first hour to the hour of ``now_ns`` (and of
        ``max_tw``). With ``limits`` (a replay), each file is read exactly up to the recorded offset."""
        out: list[dict] = []
        last_hour = now_ns // NS // 3600 * 3600
        if max_tw is not None:
            last_hour = min(last_hour, max_tw // NS // 3600 * 3600 + 3600)
        self.capped = False
        budget = max_bytes
        for hour in range(self.hour, last_hour + 1, 3600):
            p = self._path(hour)
            rel = p.relative_to(self.root / self.stream).as_posix()
            if limits is not None:
                lim = limits.get(rel)
                if lim is None or lim <= self.offsets.get(rel, 0):
                    continue
                recs, off, _ = read_new_frames(p, self.offsets.get(rel, 0), lim)
                if off != lim:
                    raise TapeError(f"replay: {p} no longer holds the bytes a committed cycle read")
            else:
                if not p.exists() or (budget is not None and budget <= 0):
                    continue
                start = self.offsets.get(rel, 0)
                recs, off, capped = read_new_frames(p, start, None, max_tw, budget)
                if budget is not None:
                    budget -= off - start
                self.capped = self.capped or capped
                if stall_s is not None:
                    self._check_stall(p, rel, off, now_ns, stall_s, capped)
            if off != self.offsets.get(rel, 0):
                self.dirty[rel] = off
            self.offsets[rel] = off
            out.extend(r for r in recs if r["tw"] >= self.from_ns)
            if self.capped:
                break
        # hours more than one before the last are complete (the writer only appends to the hour of tw)
        while self.hour < last_hour - 3600:
            rel = self._path(self.hour).relative_to(self.root / self.stream).as_posix()
            self.offsets.pop(rel, None)
            self.stuck.pop(rel, None)
            self.hour += 3600
        for r in out:
            self.last_tw = r["tw"] if self.last_tw is None else max(self.last_tw, r["tw"])
        return out

    def _check_stall(self, p: Path, rel: str, off: int, now_ns: int, stall_s: float, capped: bool) -> None:
        if capped or os.path.getsize(p) <= off:
            self.stuck.pop(rel, None)
            return
        seen = self.stuck.get(rel)
        if seen is None or seen[0] != off:
            self.stuck[rel] = (off, now_ns)
        elif now_ns - seen[1] > stall_s * NS:
            raise TapeError(f"stalled: {p} has {os.path.getsize(p) - off} bytes that never became a "
                            f"complete frame in {stall_s:g} s")

    def take_dirty(self) -> dict[str, int]:
        d, self.dirty = self.dirty, {}
        return d


class PerelmanTail:
    """PERELMAN raw BRTI rows (t_ms, value, received_s) as they are appended; read-only."""

    def __init__(self, src: Path):
        self.src = Path(src)
        self.offsets: dict[str, int] = {}
        self.dirty: dict[str, int] = {}

    def poll(self, limits: dict[str, int] | None = None) -> list[tuple[int, str, object]]:
        out: list[tuple[int, str, object]] = []
        if not self.src.is_dir():
            return out
        names = sorted(p.name for p in [*self.src.glob("*.jsonl"), *self.src.glob("*.jsonl.gz")])
        for name in names:
            start = self.offsets.get(name, 0)
            lim = None if limits is None else limits.get(name)
            if limits is not None and (lim is None or lim <= start):
                continue
            path = self.src / name
            if name.endswith(".gz"):
                if start > 0:
                    continue              # a rotated file is complete: read once
                with gzip.open(path, "rb") as fh:
                    data = fh.read()
                lines, end = data.splitlines(keepends=True), len(data)
            else:
                with open(path, "rb") as fh:
                    fh.seek(start)
                    data = fh.read(-1 if lim is None else lim - start)
                cut = data.rfind(b"\n") + 1
                lines, end = data[:cut].splitlines(keepends=True), start + cut
            for line in lines:
                try:
                    rec = orjson.loads(line)
                except orjson.JSONDecodeError as exc:
                    raise TapeError(f"malformed PERELMAN row in {path}") from exc
                if rec.get("kind") != "brti":
                    continue
                for t_ms, v in sorted(rec.get("new") or [], key=lambda pair: int(pair[0])):
                    out.append((int(t_ms), str(v), rec.get("received")))
            if limits is not None and end != lim:
                raise TapeError(f"replay: {path} no longer holds the bytes a committed cycle read")
            if end != start:
                self.offsets[name] = end
                self.dirty[name] = end
        return out

    def take_dirty(self) -> dict[str, int]:
        d, self.dirty = self.dirty, {}
        return d
