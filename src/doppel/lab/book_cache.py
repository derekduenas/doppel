"""Read Rust's lossless international CLOB projection in capture order."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from threading import Lock

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from .intlbook import _CachedSnapshot

SCHEMA = b"doppel.book-replay.schema"
SOURCE_HASH = b"doppel.book-replay.source-sha256"
SOURCE_BYTES = b"doppel.book-replay.source-bytes"
SOURCE_STREAM = b"doppel.book-replay.source-stream"
SOURCE_HOUR = b"doppel.book-replay.source-hour-ns"
SOURCE_RECORDS = b"doppel.book-replay.source-records"
_HEADER = ("record_no", "message_no", "seq", "ts_ns")
_MESSAGE = ("event_type", "venue_ms", "market_id", "asset_id", "bids", "asks", "changes",
            "trade_px", "trade_sz", "trade_side", "tx", "ctl", "fallback_raw")
_SNAPSHOT = ("snapshot_prev_record_no", "snapshot_prev_message_no", "snapshot_delta")
_PREPARED_LIMIT = 64 * 1024**2
_PREPARED_LOCK = Lock()
_PREPARED: OrderedDict[tuple, tuple[int, list[dict]]] = OrderedDict()


@dataclass
class _PreparedHour:
    records: list[dict] | None = field(default_factory=list)
    bytes: int = 0

    def batch(self, batch: pa.RecordBatch, payload: pa.RecordBatch) -> None:
        if self.records is not None:
            # Snapshots keep Arrow buffers, while the projected Python objects
            # also need space for dicts, strings and references.
            self.bytes += batch.nbytes + 16 * payload.nbytes + 1024 * batch.num_rows
            if self.bytes > _PREPARED_LIMIT:
                self.records = None

    def append(self, record: dict) -> None:
        if self.records is not None:
            self.records.append(record)


def _retain(key: tuple, prepared: _PreparedHour) -> None:
    if prepared.records is None:
        return
    with _PREPARED_LOCK:
        _PREPARED.pop(key, None)
        occupied = sum(size for size, _ in _PREPARED.values())
        while _PREPARED and occupied + prepared.bytes > _PREPARED_LIMIT:
            _, (removed, _) = _PREPARED.popitem(last=False)
            occupied -= removed
        _PREPARED[key] = prepared.bytes, prepared.records


def _stamp(path: Path) -> tuple[int, int, int, int, int]:
    st = path.stat()
    return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns


@lru_cache(maxsize=256)
def _source_digest(path: Path, stamp: tuple[int, int, int, int, int]) -> str:
    with path.open("rb") as source:
        result = hashlib.file_digest(source, "sha256").hexdigest()
    if _stamp(path) != stamp:
        raise ValueError(f"book cache source changed during verification: {path}")
    return result


def _message(columns: dict[str, list], i: int, event: str) -> dict:
    def at(name: str):
        return columns[name][i]

    base = {"event_type": event, "timestamp": at("venue_ms"), "market": at("market_id")}
    if event == "price_change":
        return {**base, "price_changes": at("changes")}
    if event == "last_trade_price":
        return {**base, "asset_id": at("asset_id"), "price": at("trade_px"), "size": at("trade_sz"),
                "side": at("trade_side"), "transaction_hash": at("tx")}
    raise ValueError(f"invalid cached CLOB event type: {event}")


def _snapshot(values: dict[str, list], i: int, batch_row: int, scope: object, record: int, message: int,
              bids: pa.Array, asks: pa.Array) -> _CachedSnapshot:
    header = {"event_type": "book", "timestamp": values["venue_ms"][i],
              "asset_id": values["asset_id"][i], "market": values["market_id"][i]}
    previous = delta = None
    if "snapshot_delta" in values:
        prev_record, prev_message = values["snapshot_prev_record_no"][i], values["snapshot_prev_message_no"][i]
        if prev_record is not None and prev_message is not None:
            previous = (scope, prev_record, prev_message)
            delta = values["snapshot_delta"][i]

    def load_levels() -> tuple[list, list]:
        return bids[batch_row].as_py(), asks[batch_row].as_py()

    return _CachedSnapshot(header, (scope, record, message), previous, delta, load_levels)


class BookCache:
    """Require the lossless sidecar for each source hour whenever the cache is selected."""

    def __init__(self, root: Path) -> None:
        self.root = root
        if not root.is_dir():
            raise ValueError(f"book cache directory is unavailable: {root}")

    def path(self, source: Path, stream: str) -> Path:
        filename = source.name.removesuffix(".dtape.zst") + ".replay.parquet"
        return self.root / stream / source.parent.name / filename

    def records(self, source: Path, stream: str, hour_ns: int, lo_ns: int, hi_ns: int) -> Iterator[dict]:
        """Yield record headers and decode messages on demand for the requested receive range."""
        path = self.path(source, stream)
        source_stamp, cache_stamp = _stamp(source), _stamp(path)
        key = (source, source_stamp, path, cache_stamp, stream, hour_ns, lo_ns, hi_ns)
        with _PREPARED_LOCK:
            cached = _PREPARED.get(key)
            if cached is not None:
                _PREPARED.move_to_end(key)
        prepared = _PreparedHour()
        if cached is not None:
            for record in cached[1]:
                yield record.copy()
        else:
            for record in self._read(source, source_stamp, path, stream, hour_ns, lo_ns, hi_ns, prepared):
                prepared.append(record)
                yield record.copy()
        if _stamp(source) != source_stamp or _stamp(path) != cache_stamp:
            raise ValueError(f"book cache or its sealed source changed during replay: {path}")
        if cached is None:
            _retain(key, prepared)

    def _read(self, source: Path, source_stamp: tuple[int, int, int, int, int], path: Path, stream: str,
              hour_ns: int, lo_ns: int, hi_ns: int, prepared: _PreparedHour) -> Iterator[dict]:
        with pq.ParquetFile(path) as f:
            schema = f.schema_arrow
            metadata = (f.metadata.metadata or {}) | (schema.metadata or {})
            record_count = metadata.get(SOURCE_RECORDS, b"")
            if (metadata.get(SCHEMA) != b"1" or metadata.get(SOURCE_STREAM) != stream.encode()
                    or metadata.get(SOURCE_HOUR) != str(hour_ns).encode()
                    or metadata.get(SOURCE_BYTES) != str(source_stamp[2]).encode()
                    or metadata.get(SOURCE_HASH) != _source_digest(source, source_stamp).encode()
                    or not record_count.isdigit()
                    or not set(_HEADER + _MESSAGE) <= set(schema.names)):
                raise ValueError(f"book cache does not match its sealed source or schema: {path}")

            current: dict | None = None
            ordinal, next_message = -1, 0
            scope = object()
            patch_columns = [name for name in _SNAPSHOT if name in schema.names]
            if patch_columns and len(patch_columns) != len(_SNAPSHOT):
                raise ValueError(f"incomplete book cache snapshot patch schema: {path}")
            payload_columns = [name for name in _MESSAGE if name not in ("bids", "asks")] + patch_columns
            for batch in f.iter_batches(batch_size=16_384, columns=[*_HEADER, *_MESSAGE, *patch_columns],
                                        use_threads=True):
                columns = dict(zip(batch.schema.names, batch.columns, strict=True))
                header = [columns[name].to_numpy(zero_copy_only=False) for name in _HEADER]
                numbers, messages, seqs, times = header
                if any(values.dtype.kind != "i" for values in header):
                    raise ValueError(f"invalid book cache capture header: {path}")
                selected = np.flatnonzero((times >= lo_ns) & (times < hi_ns)).tolist()
                payload = batch.select(payload_columns)
                if len(selected) != batch.num_rows:
                    payload = payload.take(pa.array(selected, type=pa.int64()))
                prepared.batch(batch, payload)
                values = payload.to_pydict()
                use = 0
                events = values["event_type"]
                bids, asks = columns["bids"], columns["asks"]
                all_selected = len(selected) == batch.num_rows
                single_message = len(numbers) > 0 and numbers[0] == ordinal + 1 and bool(
                    np.all(messages == 0) and np.all(np.diff(numbers) == 1))
                captures = zip(*(values.tolist() for values in (numbers, messages, seqs, times)), strict=True)
                for i, (number, message, seq, tw) in enumerate(captures):
                    if single_message or number != ordinal:
                        if not single_message and number != ordinal + 1:
                            raise ValueError(f"book cache record ordinal gap: {path}")
                        if current is not None:
                            yield current
                        ordinal, next_message = number, 0
                        current = {"seq": seq, "stream": stream, "tw": tw, "_book_messages": []}
                    assert current is not None
                    if not single_message and (message != next_message or seq != current["seq"] or tw != current["tw"]):
                        raise ValueError(f"book cache message order or capture clock mismatch: {path}")
                    next_message += 1
                    if not all_selected and (use >= len(selected) or i != selected[use]):
                        continue
                    event = events[use]
                    if message and "_book_messages" not in current:
                        raise ValueError(f"standalone book cache row has sibling messages: {path}")
                    if event in ("skip", "control", "fallback") and message:
                        raise ValueError(f"standalone book cache row is out of place: {path}")
                    if event == "book":
                        current["_book_messages"].append(_snapshot(values, use, i, scope, ordinal, message, bids, asks))
                    elif event == "skip":
                        current.pop("_book_messages", None)
                        current["raw"] = ""
                    elif event == "control":
                        ctl = values["ctl"][use]
                        if not isinstance(ctl, str):
                            raise ValueError(f"invalid book cache control: {path}")
                        current.pop("_book_messages", None)
                        current["meta"] = {"ctl": ctl}
                    elif event == "fallback":
                        raw = values["fallback_raw"][use]
                        if not isinstance(raw, str):
                            raise ValueError(f"invalid book cache fallback payload: {path}")
                        current.pop("_book_messages", None)
                        current["raw"] = raw
                        current["_book_cache_barrier"] = True
                    else:
                        current["_book_messages"].append(_message(values, use, event))
                    use += 1
            if ordinal + 1 != int(record_count):
                raise ValueError(f"book cache source record count differs: {path}")
            if current is not None:
                yield current
