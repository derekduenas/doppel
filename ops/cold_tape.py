"""Verified, content-addressed object storage for closed tape files."""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
SHA256 = re.compile(r"[0-9a-f]{64}")
SEGMENT = re.compile(r"\d{8}/\d{2}\.dtape\.zst")


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def relative(name):
    parts = name.split("/")
    if not all(SAFE.fullmatch(part) for part in parts):
        raise ValueError(f"unsafe archive path: {name!r}")
    return PurePosixPath(*parts)


def closed_hour():
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def file_stamp(st):
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


@contextlib.contextmanager
def open_file(root, name):
    """Anchor all traversal beneath the configured root and disallow symlinks."""
    parts = relative(name).parts
    directories = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, directories)
    try:
        for part in parts[:-1]:
            child = os.open(part, directories, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = parts[-1]
        item = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(item, "rb") as handle:
            if not stat.S_ISREG(os.fstat(item).st_mode):
                raise ValueError(f"not a regular archive file: {name}")
            yield handle, fd, leaf
    finally:
        os.close(fd)


def unchanged(handle, parent_fd, leaf, stamp):
    return (file_stamp(os.fstat(handle.fileno())) == stamp
            and file_stamp(os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)) == stamp)


@contextlib.contextmanager
def locked_state(state):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(state / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def publish_proof(path, proof):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
        scratch = Path(out.name)
        try:
            out.write(json_bytes(proof))
            out.flush()
            os.fsync(out.fileno())
            scratch.replace(path)
            fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            scratch.unlink(missing_ok=True)


class ObjectStore:
    def __init__(self, remote):
        name, sep, prefix = remote.rstrip("/").partition(":")
        if not sep or not SAFE.fullmatch(name):
            raise ValueError("archive destination must be a named rclone remote with a bucket")
        self.remote = f"{name}:{relative(prefix)}"
        self.verified = set()

    def call(self, *args):
        return ["rclone", *args, "--contimeout=10s", "--timeout=60s", "--retries=1", "--transfers=1",
                "--s3-no-check-bucket", "--s3-upload-concurrency=1", "--b2-chunk-size=16Mi"]

    def object_size(self, key):
        proc = subprocess.run(self.call("lsjson", "--stat", f"{self.remote}/{key}"),
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        if proc.returncode in (3, 4):
            return None
        proc.check_returncode()
        row = json.loads(proc.stdout)
        # Bucket backends return an empty directory for an absent leaf.
        if row is None or row["IsDir"]:
            return None
        return row["Size"]

    def ensure(self, key, payload, size, expected_sha):
        identity = key, size, expected_sha
        if identity in self.verified:
            return
        remote_size = self.object_size(key)
        if remote_size is None:
            data = {"input": payload} if isinstance(payload, bytes) else {"stdin": payload}
            subprocess.run(self.call("rcat", f"{self.remote}/{key}", "--size", str(size)), check=True, **data)
        elif remote_size != size:
            raise ValueError(f"remote object size mismatch: {key}")
        self.verify(key, size, expected_sha)

    def verify(self, key, size, expected_sha):
        identity = key, size, expected_sha
        if identity in self.verified:
            return
        with subprocess.Popen(self.call("cat", f"{self.remote}/{key}"), stdout=subprocess.PIPE) as proc:
            assert proc.stdout is not None
            digest, total = hashlib.sha256(), 0
            while chunk := proc.stdout.read(1024 * 1024):
                total += len(chunk)
                if total > size:
                    proc.kill()
                    raise ValueError(f"remote object exceeds expected length: {key}")
                digest.update(chunk)
            if proc.wait() or total != size or digest.hexdigest() != expected_sha:
                raise ValueError(f"remote SHA-256 verification failed: {key}")
        self.verified.add(identity)


def manifests(root, streams, start, through):
    if streams == ["auto"]:
        streams = sorted(p.name for p in root.iterdir()
                         if not p.is_symlink() and (p / "manifest.jsonl").is_file())
    for stream in dict.fromkeys(streams):
        if not SAFE.fullmatch(stream):
            raise ValueError(f"invalid tape stream: {stream}")
        with open_file(root, f"{stream}/manifest.jsonl") as (handle, _, _):
            groups = {}
            for line in handle:
                if not line.endswith(b"\n"):
                    break
                if not line.strip():
                    continue
                row = json.loads(line)
                name = row["file"]
                if not SEGMENT.fullmatch(name):
                    raise ValueError(f"invalid sealed segment path: {name}")
                hour = datetime.strptime(name, "%Y%m%d/%H.dtape.zst").replace(tzinfo=UTC)
                if (start is None or start <= hour) and hour < through:
                    groups.setdefault(name, []).append(row)
            for name, history in sorted(groups.items()):
                yield f"{stream}/{name}", history


def receipt(root, name):
    stream, day, file = relative(name).parts
    hour = int(file[:2])
    try:
        with open_file(root, f"receipts/{day}/{hour:02d}.json") as (handle, _, _):
            raw = handle.read()
    except FileNotFoundError:
        return None, None
    record = json.loads(raw)
    if record["day"] != day or record["hour"] != hour:
        raise ValueError(f"hourly receipt identity mismatch: {name}")
    version = record.get("schema_version", 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError(f"unsupported hourly receipt: {name}")
    row = record["streams"].get(stream)
    if row and version == 2 and "sha256" not in row:
        row = row | {"sha256": None}
    return raw, row if row else {}


def proof_path(state, root, remote, name):
    return state / (sha(json_bytes([str(root.resolve()), remote, name])) + ".json")


def tape_identity(root, name, history, *, legacy=False):
    raw_receipt, attestation = (None, None) if legacy else receipt(root, name)
    return {"path": name, "kind": "historical" if legacy else "sealed_segment",
            "manifest_history": history, "receipt_sha256": sha(raw_receipt) if raw_receipt else None}, raw_receipt, attestation


def archived_proof(path):
    try:
        with open_file(path.parent, path.name) as (handle, _, _):
            return json.load(handle)
    except FileNotFoundError:
        return None


def describe_file(root, name, history, handle, stamp, *, legacy, dry_run):
    identity, raw_receipt, attestation = tape_identity(root, name, history, legacy=legacy)
    size = stamp[2]
    if legacy:
        checksum = None if dry_run else hashlib.file_digest(handle, "sha256").hexdigest()
    else:
        row = history[-1]
        checksum = row.get("sha256")
        if (type(row.get("bytes")) is not int or row["bytes"] != size or not isinstance(checksum, str)
                or not SHA256.fullmatch(checksum)):
            raise ValueError(f"source does not match its sealed size or digest: {name}")
    identity.update(bytes=size, sha256=checksum)
    # A later raw revision may be archived for forensics while the original receipt is retained.
    # Keep that file locally if the published generation is still unresolved.
    can_prune = not legacy and history[-1].get("evidence_use") != "diagnostic_only" and (
        attestation is None or (attestation.get("crc_ok") is True and attestation.get("bytes") == size
                                and attestation.get("sha256", checksum) == checksum))
    return identity, raw_receipt, can_prune


def manifest_objects(identity, raw_receipt):
    name, checksum = identity["path"], identity["sha256"]
    data_key = f"data/{name}/{checksum}"
    objects = {data_key: identity["bytes"]}
    receipt_key = None
    if raw_receipt is not None:
        _, day, file = name.split("/")
        receipt_key = f"receipts/{day}/{file[:2]}/{identity['receipt_sha256']}.json"
        objects[receipt_key] = len(raw_receipt)
    record = json_bytes({"schema": 1, **identity, "objects": objects})
    record_key = f"checksums/{name}/{sha(record)}.json"
    objects[record_key] = len(record)
    return data_key, receipt_key, record_key, record, objects


def offload(root, target, state, streams, start, through, *, dry_run=False, historical=None):
    result = {"archived": 0, "verified": 0, "pending": 0, "bytes": 0, "issues": [], "dry_run": dry_run}
    if not root.is_dir() or root.is_symlink() or state.is_symlink():
        raise ValueError("archive source and proof state must be real directories")
    legacy = historical is not None
    records = ((str(relative(name)), []) for name in historical) if legacy else manifests(root, streams, start, through)
    with contextlib.nullcontext() if dry_run else locked_state(state):
        for name, history in records:
            path = proof_path(state, root, target.remote, name)
            saved = archived_proof(path)
            try:
                with open_file(root, name) as (handle, parent_fd, leaf):
                    stamp = file_stamp(os.fstat(handle.fileno()))
                    if legacy and stamp[3] >= int(through.timestamp()) * 1_000_000_000:
                        raise ValueError(f"historical file is too recent for the supplied closed cutoff: {name}")
                    identity, raw_receipt, prune = describe_file(root, name, history, handle, stamp,
                                                                legacy=legacy, dry_run=dry_run)
                    if saved and saved["identity"] == identity and saved["stamp"] == stamp:
                        result["verified"] += 1
                        continue
                    result["pending"] += 1
                    result["bytes"] += stamp[2]
                    if dry_run:
                        continue
                    checksum = identity["sha256"]
                    if not legacy and hashlib.file_digest(handle, "sha256").hexdigest() != checksum:
                        raise ValueError(f"source SHA-256 differs from seal: {name}")
                    handle.seek(0)
                    if not unchanged(handle, parent_fd, leaf, stamp):
                        raise ValueError(f"source changed while verifying: {name}")
                    data_key, receipt_key, record_key, record, objects = manifest_objects(identity, raw_receipt)
                    target.ensure(data_key, handle, stamp[2], checksum)
                    if raw_receipt is not None:
                        target.ensure(receipt_key, raw_receipt, len(raw_receipt), identity["receipt_sha256"])
                    target.ensure(record_key, record, len(record), sha(record))
                    if not unchanged(handle, parent_fd, leaf, stamp):
                        raise ValueError(f"source changed during upload: {name}")
                    publish_proof(path, {"identity": identity, "objects": objects, "stamp": stamp, "prune": prune})
                    result["archived"] += 1
            except FileNotFoundError:
                if saved and not legacy and history == saved["identity"]["manifest_history"]:
                    result["verified"] += 1  # previously offloaded and pruned
                else:
                    result["issues"].append(f"missing sealed file: {name}")
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                result["issues"].append(str(exc))
    return result
