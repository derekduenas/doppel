"""Persist one ordered, resumable record of every preregistered Shadow trial."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import math
import os
from pathlib import Path
import stat
from typing import BinaryIO

import orjson

from ..tape.segment import atomic_write, sync_directory

PLAN_SCHEMA = "doppel.shadow-trial-plan/v2"
LEDGER_SCHEMA = "doppel.shadow-trial-ledger/v2"
ABSTENTION_SCHEMA = "doppel.shadow-trial-abstention/v1"
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
MAX_JOURNAL_BYTES = 4 * MAX_ARTIFACT_BYTES
IDENTITY = ("trial_id", "genome_id", "subject_id", "variant_id")
GENESIS = "0" * 64
_ROW_FIELDS = set(IDENTITY) | {"seq", "previous_sha256", "sha256", "disposition", "returns_c", "reason"}


def _canon(doc: object) -> bytes:
    return orjson.dumps(doc, option=orjson.OPT_SORT_KEYS)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hex(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _read_bounded(path: Path, limit: int = MAX_ARTIFACT_BYTES) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError(f"{path.name}: expected a bounded regular evidence file")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"{path.name}: evidence exceeds its limit")
    return raw


def _attempts(plan: dict) -> list[dict]:
    rows = plan.get("attempts")
    if plan.get("schema") != PLAN_SCHEMA or not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("expected a v2 plan with at least two ordered trial identities")
    trial_ids, identities = set(), set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(IDENTITY) or not all(_hex(v) for v in row.values()):
            raise ValueError("invalid trial plan identity")
        identity = tuple(row[k] for k in IDENTITY[1:])
        if row["trial_id"] in trial_ids or identity in identities:
            raise ValueError("duplicate trial plan identity")
        trial_ids.add(row["trial_id"])
        identities.add(identity)
    abstentions = plan.get("abstentions")
    if not isinstance(abstentions, dict) or any(k not in trial_ids or not _hex(v) for k, v in abstentions.items()):
        raise ValueError("abstention policies must be pinned for declared trial IDs")
    return rows


def read_plan(path: Path, expected_sha256: str, family: str, start_s: int) -> dict:
    """Read the exact operator-pinned plan before processing any forward outcomes."""
    raw = _read_bounded(path)
    if _sha(raw) != expected_sha256:
        raise ValueError("trial plan SHA-256 differs from the independently pinned digest")
    plan = orjson.loads(raw)
    if not isinstance(plan, dict) or raw not in (_canon(plan), _canon(plan) + b"\n"):
        raise ValueError("expected canonical trial plan JSON")
    frozen = plan.get("frozen_ns")
    if (not isinstance(family, str) or not family.strip() or plan.get("family") != family
            or type(start_s) is not int or type(frozen) is not int or not 0 < frozen < start_s * 10**9):
        raise ValueError("trial plan family or preregistration cutoff mismatch")
    _attempts(plan)
    return plan


def _window_grid(windows: object) -> list[list[int]]:
    if not isinstance(windows, list) or not windows:
        raise ValueError("trial ledger requires a measured forward window grid")
    result: list[list[int]] = []
    for window in windows:
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            raise ValueError("invalid trial window interval")
        start, end = window
        if (type(start) is not int or type(end) is not int or not 0 <= start < end
                or (result and start < result[-1][1])):
            raise ValueError("trial windows must be ordered, unique, and nonoverlapping")
        result.append([start, end])
    return result


def _validate_terminal(row: object, seq: int, previous: str, count: int, windows_sha256: str) -> None:
    if not isinstance(row, dict):
        raise ValueError("expected terminal trial row")
    body = {k: v for k, v in row.items() if k != "sha256"}
    if (any(not _hex(row.get(key)) for key in IDENTITY)
            or type(row.get("seq")) is not int or row["seq"] != seq
            or row.get("previous_sha256") != previous or row.get("sha256") != _sha(_canon(body))):
        raise ValueError("terminal trial row is invalid, out of order or disconnected")
    disposition, returns, reason = row.get("disposition"), row.get("returns_c"), row.get("reason")
    if disposition not in ("PASS", "FAIL", "ABSTAIN", "ERROR") or not isinstance(reason, str) or not reason.strip():
        raise ValueError("terminal trial row requires a disposition and a reason")
    if disposition == "ERROR":
        if "returns_c" not in row or returns is not None:
            raise ValueError("data and execution errors must retain unavailable returns")
        if "abstention" in row:
            raise ValueError("error rows cannot claim abstention")
        return
    if (not isinstance(returns, list) or len(returns) != count
            or not all(type(value) in (int, float) and math.isfinite(value) for value in returns)):
        raise ValueError("measured returns must cover the exact window grid")
    if disposition == "ABSTAIN":
        proof = row.get("abstention")
        if not isinstance(proof, dict):
            raise ValueError("abstention requires complete observed no-entry evidence")
        expected = {"schema": ABSTENTION_SCHEMA, "policy_sha256": proof.get("policy_sha256"),
                    "input_sha256": proof.get("input_sha256"), "windows_sha256": windows_sha256,
                    "observed_windows": count, "entry_attempts": 0}
        if (proof != expected or not _hex(proof["policy_sha256"]) or not _hex(proof["input_sha256"])
                or type(proof["observed_windows"]) is not int or type(proof["entry_attempts"]) is not int
                or any(value != 0 for value in returns)):
            raise ValueError("abstention requires complete observed no-entry evidence")
    elif "abstention" in row:
        raise ValueError("trial disposition contradicts its abstention evidence")


def validate_ledger_v2(ledger: dict) -> None:
    """Verify the v2 structure and chain before comparing independently pinned evidence."""
    rows = ledger.get("trials")
    if (ledger.get("schema") != LEDGER_SCHEMA or not _hex(ledger.get("plan_sha256"))
            or not isinstance(rows, list) or len(rows) < 2):
        raise ValueError("invalid v2 trial ledger")
    windows = _window_grid(ledger.get("windows"))
    windows_sha256 = _sha(_canon(windows))
    previous, trials, identities = GENESIS, set(), set()
    for seq, row in enumerate(rows):
        _validate_terminal(row, seq, previous, len(windows), windows_sha256)
        identity = tuple(row[key] for key in IDENTITY[1:])
        if row["trial_id"] in trials or identity in identities:
            raise ValueError("duplicate terminal trial identity")
        trials.add(row["trial_id"])
        identities.add(identity)
        previous = row["sha256"]


class TrialLedger:
    """One writer appends starts and sealed terminal rows in pinned plan order."""

    def __init__(self, out: Path, plan: dict, plan_sha256: str, windows: list[tuple[int, int]],
                 context_sha256: str):
        canonical = _canon(plan)
        if plan_sha256 not in {_sha(canonical), _sha(canonical + b"\n")} or not _hex(context_sha256):
            raise ValueError("invalid trial plan or run context digest")
        self._attempts = copy.deepcopy(_attempts(plan))
        self._abstentions = dict(plan["abstentions"])
        self._windows = _window_grid(windows)
        self._windows_sha256 = _sha(_canon(self._windows))
        self._header = {"schema": "doppel.shadow-trial-journal/v1", "plan_sha256": plan_sha256,
                        "windows_sha256": self._windows_sha256, "context_sha256": context_sha256}
        self._out = out
        self._journal = out / "trials.log.jsonl"
        self._published = out / "trials.json"
        self._stream: BinaryIO | None = None
        self._rows: list[dict] = []
        self._started = False
        self._write_failed = False

    @property
    def rows(self) -> list[dict]:
        return copy.deepcopy(self._rows)

    @property
    def _head(self) -> str:
        return self._rows[-1]["sha256"] if self._rows else GENESIS

    def __enter__(self) -> TrialLedger:
        if self._stream is not None:
            raise ValueError("trial ledger context is already open")
        self._out.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._journal, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        stream = os.fdopen(fd, "r+b")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_JOURNAL_BYTES:
                raise ValueError("expected a bounded regular trial journal")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("another writer holds the trial journal") from exc
            self._stream = stream
            self._rows = []
            self._started = self._write_failed = False
            try:
                published = _read_bounded(self._published)
            except FileNotFoundError:
                published = None
            self._recover(published)
        except BaseException:
            stream.close()
            self._stream = None
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def _active(self) -> BinaryIO:
        if self._stream is None or self._write_failed:
            raise ValueError("open a fresh trial ledger context before writing")
        return self._stream

    def _next(self) -> dict:
        if len(self._rows) >= len(self._attempts):
            raise ValueError("all planned trials are already terminal")
        return self._attempts[len(self._rows)]

    def _start_event(self) -> dict:
        return {"start": self._next(), "seq": len(self._rows), "previous_sha256": self._head}

    def _append(self, doc: dict) -> None:
        stream = self._active()
        raw = _canon(doc) + b"\n"
        if os.fstat(stream.fileno()).st_size + len(raw) > MAX_JOURNAL_BYTES:
            raise ValueError("trial journal exceeds its storage bound")
        self._write_failed = True
        if stream.write(raw) != len(raw):
            raise OSError("short trial journal write")
        stream.flush()
        os.fsync(stream.fileno())
        self._write_failed = False

    def _recover(self, published: bytes | None) -> None:
        stream = self._active()
        header = _canon(self._header) + b"\n"
        end = 0
        while raw := stream.readline(MAX_JOURNAL_BYTES + 1):
            if end + len(raw) > MAX_JOURNAL_BYTES:
                raise ValueError("trial journal grew past its storage bound")
            if not raw.endswith(b"\n"):
                if published is not None:
                    raise ValueError("invalid torn trial journal")
                if not self._started:
                    expected = header if not end else _canon(self._start_event()) + b"\n"
                    if not expected.startswith(raw):
                        raise ValueError("invalid torn trial start or journal header")
                self._save_torn_tail(stream, raw, end)
                break
            if not end:
                if raw != header:
                    raise ValueError("trial journal context has changed")
            elif not self._started:
                if raw != _canon(self._start_event()) + b"\n":
                    raise ValueError("trial start is reordered, missing or altered")
                self._started = True
            else:
                row = orjson.loads(raw)
                if not isinstance(row, dict) or raw != _canon(row) + b"\n":
                    raise ValueError("expected canonical terminal trial row")
                self._verify_row(row)
                self._rows.append(row)
                self._started = False
            end += len(raw)
        if published is not None:
            if not end or published != self._export_bytes():
                raise ValueError("published trial ledger differs from its journal")
        elif not end:
            self._append(self._header)
            sync_directory(self._out)
            sync_directory(self._out.parent)

    def _save_torn_tail(self, stream: BinaryIO, raw: bytes, offset: int) -> None:
        path = self._out / f"trials.torn-{_sha(raw)}.partial"
        try:
            atomic_write(path, raw, replace=False)
        except FileExistsError:
            if _read_bounded(path, MAX_JOURNAL_BYTES) != raw:
                raise ValueError("torn trial journal evidence differs")
        stream.truncate(offset)
        os.fsync(stream.fileno())

    def _verify_row(self, row: dict) -> None:
        attempt = self._next()
        _validate_terminal(row, len(self._rows), self._head, len(self._windows), self._windows_sha256)
        if any(row.get(key) != val for key, val in attempt.items()):
            raise ValueError("terminal trial differs from the preregistered identity")
        policy = self._abstentions.get(attempt["trial_id"])
        if row["disposition"] == "ABSTAIN" and row["abstention"]["policy_sha256"] != policy:
            raise ValueError("abstention requires the preregistered policy digest")
        if policy is not None and row["disposition"] in ("PASS", "FAIL"):
            raise ValueError("trial disposition contradicts the planned abstention policy")

    def begin(self, attempt: dict) -> None:
        self._active()
        if attempt != self._next():
            raise ValueError("must start exactly the next planned trial")
        if not self._started:
            self._append(self._start_event())
            self._started = True

    def finish(self, attempt: dict, *, disposition: str, returns_c: list[float] | None, reason: str,
               evidence: dict) -> dict:
        self._active()
        if not self._started or attempt != self._next():
            raise ValueError("must finish the started planned trial")
        if _ROW_FIELDS.intersection(evidence):
            raise ValueError("trial evidence cannot overwrite ledger fields")
        body = {**attempt, **evidence, "seq": len(self._rows), "previous_sha256": self._head,
                "disposition": disposition, "returns_c": returns_c, "reason": reason}
        row = {**body, "sha256": _sha(_canon(body))}
        self._verify_row(row)
        self._append(row)
        self._rows.append(copy.deepcopy(row))
        self._started = False
        return row

    def _export_bytes(self) -> bytes:
        if self._started or len(self._rows) != len(self._attempts):
            raise ValueError("incomplete trial ledger cannot be published")
        raw = _canon({"schema": LEDGER_SCHEMA, "plan_sha256": self._header["plan_sha256"],
                      "windows": self._windows, "trials": self._rows}) + b"\n"
        if len(raw) > MAX_ARTIFACT_BYTES:
            raise ValueError("trial ledger exceeds the admission artifact limit")
        return raw

    def publish(self) -> dict:
        """Materialize all terminal rows, including errors, as immutable evidence."""
        self._active()
        raw = self._export_bytes()
        try:
            atomic_write(self._published, raw, replace=False)
        except FileExistsError:
            if _read_bounded(self._published) != raw:
                raise ValueError("published trial ledger differs from its journal")
        return orjson.loads(raw)
