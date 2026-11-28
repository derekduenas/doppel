"""Remove expired tape segments after verifying their cold archive proof."""

import argparse
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tomllib

if __package__:
    from . import cold_tape as cold
    from . import storage
else:
    import cold_tape as cold
    import storage

GIB = 1024**3


def protected_days(root):
    try:
        with cold.open_file(root, "janitor-protect.txt") as (handle, _, _):
            return {line.strip() for line in handle.read().decode().splitlines() if line.strip()}
    except FileNotFoundError:
        return set()


def prunable(stream):
    return (stream.startswith(("clob_", "rtds_", "spot_", "perp_", "lead_", "pmus_ws", "offshore_"))
            or stream in {"pmus_books", "kalshi_books", "kalshi_ws"})


def prune(root, floor_gib=12, *, target=None, state=None, retain_days=10, dry_run=False, now=None):
    if floor_gib < 0 or not 7 <= retain_days <= 14:
        raise ValueError("retention must be 7 to 14 days and the free-space floor must be nonnegative")
    free = shutil.disk_usage(root).free
    result = {"deleted": [], "retained": [], "ready": [], "issues": [], "free_bytes": free}
    if target is None or state is None or not state.is_dir():
        if free < floor_gib * GIB:
            result["issues"].append("disk below reserve and verified cold archive state is unavailable")
        return result
    if root.is_symlink() or state.is_symlink():
        raise ValueError("tape root and proof directory must be real directories")
    through = (now or cold.closed_hour()) - timedelta(days=retain_days)
    # The archive takes the same lock from inventory through proof publication.
    lock = cold.locked_state(state) if not dry_run else nullcontext()
    with lock:
        protected = protected_days(root)
        candidates = sorted(cold.manifests(root, ["auto"], None, through), key=lambda entry: entry[0].split("/")[1:])
        day_being_pruned = None
        for name, history in candidates:
            stream, day, _ = name.split("/")
            if not prunable(stream) or day in protected:
                continue
            if day_being_pruned is not None and day != day_being_pruned:
                break
            try:
                with cold.open_file(root, name) as (handle, parent_fd, leaf):
                    proof = cold.archived_proof(cold.proof_path(state, root, target.remote, name))
                    stamp = cold.file_stamp(os.fstat(handle.fileno()))
                    identity, receipt, allowed = cold.describe_file(root, name, history, handle, stamp,
                                                                    legacy=False, dry_run=dry_run)
                    data_key, receipt_key, record_key, record, objects = cold.manifest_objects(identity, receipt)
                    if (not allowed or not proof or proof.get("prune") is not True or proof.get("stamp") != stamp
                            or proof.get("identity") != identity or proof.get("objects") != objects):
                        result["retained"].append(name)
                        continue
                    day_being_pruned = day
                    if dry_run:
                        result["ready"].append(name)
                        continue
                    target.verify(record_key, len(record), cold.sha(record))
                    if receipt_key is not None:
                        target.verify(receipt_key, len(receipt), identity["receipt_sha256"])
                    target.verify(data_key, identity["bytes"], identity["sha256"])
                    if day in protected_days(root) or not cold.unchanged(handle, parent_fd, leaf, stamp):
                        result["retained"].append(name)
                        continue
                    os.unlink(leaf, dir_fd=parent_fd)
                    result["deleted"].append(name)
            except FileNotFoundError:
                continue
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                result["issues"].append(str(exc))
    if not dry_run:
        result["free_bytes"] = shutil.disk_usage(root).free
    if result["free_bytes"] < floor_gib * GIB:
        result["issues"].append("disk below reserve after applying verified age-based retention")
    return result


def read_audit(path: Path, policy: dict, now: datetime) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("storage audit is not a regular service-owned file")
        audit = json.load(source)
    if not isinstance(audit, dict):
        raise ValueError("invalid storage audit")
    stamp = audit.get("t")
    if (audit.get("schema") != storage.AUDIT_SCHEMA or audit.get("policy_sha256") != storage.policy_sha256(policy)
            or type(stamp) is not int or not 0 <= now.timestamp() - stamp <= storage.seconds(policy["audit"]["grace"])
            or audit.get("unclassified") != [] or not isinstance(audit.get("classes"), dict)):
        raise ValueError("storage audit is stale, unclassified, or outside the active policy")
    return audit["classes"]


def policy_prune(policy: dict, audit_path: Path, floor_gib: int = 12, *, max_files: int = 12,
                 dry_run: bool = False, now: datetime | None = None) -> dict:
    """Remove old, locally sealed segments only after current exact R2 readback."""
    if floor_gib < 0 or max_files <= 0:
        raise ValueError("free-space floor and file limit are invalid")
    now = now or datetime.now(timezone.utc)
    audit = read_audit(audit_path, policy, now)
    classes = [c for c in policy["class"] if c["local_days"] and Path(c["local"]).is_dir()]
    def protected():
        return set().union(*(protected_days(Path(c["local"])) for c in classes))

    keep_days = protected()
    result: dict = {"deleted": [], "ready": [], "issues": [], "kept_protected": sorted(keep_days)}
    candidates = []
    for cls in classes:
        name, root = cls["name"], Path(cls["local"])
        rec = audit.get(name, {})
        paths = rec.get("verified_paths")
        keys = rec.get("remote_keys") if storage.archive_owns(cls) else None
        if (root.is_symlink() or not 7 <= cls["local_days"] <= 14 or not isinstance(paths, list)
                or not all(isinstance(path, str) for path in paths) or len(set(paths)) != len(paths)
                or rec.get("verified") != len(paths) or rec.get("local_days") != cls["local_days"]):
            raise ValueError(f"missing or invalid size-audit scope: {name}")
        if storage.archive_owns(cls) and (not isinstance(keys, dict) or set(keys) != set(paths)):
            raise ValueError(f"missing or invalid immutable archive audit scope: {name}")
        matched = set(paths)
        through = now - timedelta(days=cls["local_days"])
        for path, history in cold.manifests(root, ["auto"], None, through.replace(minute=0, second=0, microsecond=0)):
            stream, day, hour = path.split("/")
            if path in matched and day not in keep_days and prunable(stream):
                key = path if keys is None else keys[path]
                if keys is None or key == cold.data_key(path, history[-1].get("sha256")):
                    candidates.append((day, hour, name, path, key, cls, history))

    processed = 0
    for day, _, name, path, key, cls, history in sorted(candidates):
        if processed >= max_files:
            break
        root = Path(cls["local"])
        try:
            with cold.open_file(root, path) as (source, parent_fd, leaf):
                processed += 1
                stamp = cold.file_stamp(os.fstat(source.fileno()))
                identity, receipt, allowed = cold.describe_file(root, path, history, source, stamp,
                                                               legacy=False, dry_run=dry_run)
                if not allowed or day in protected():
                    continue
                if dry_run:
                    result["ready"].append(f"{name}/{path}")
                    continue
                checksum = hashlib.file_digest(source, "sha256").hexdigest()
                if (checksum != identity["sha256"]
                        or storage.remote_digest(policy, cls, key, stamp[2]) != checksum):
                    raise ValueError(f"SHA-256 mismatch: {name}/{path}")
                if storage.archive_owns(cls):
                    _, receipt_key, record_key, record, _ = cold.manifest_objects(identity, receipt)
                    if storage.remote_digest(policy, cls, record_key, len(record)) != cold.sha(record):
                        raise ValueError(f"remote archive manifest hash mismatch: {name}/{path}")
                else:
                    receipt_key = f"receipts/{day}/{Path(path).name[:2]}.json"
                if receipt is not None:
                    if storage.remote_digest(policy, cls, receipt_key, len(receipt)) != identity["receipt_sha256"]:
                        raise ValueError(f"remote receipt hash mismatch: {name}/{path}")
                if day in protected() or not cold.unchanged(source, parent_fd, leaf, stamp):
                    continue
                os.unlink(leaf, dir_fd=parent_fd)
                result["deleted"].append(f"{name}/{path}")
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            result["issues"].append(str(exc))
    for mount in {Path(c["local"]).parent for c in classes}:
        if shutil.disk_usage(mount).free < floor_gib * GIB:
            result["issues"].append(f"disk below reserve: {mount}")
    return result


def closed_days(root: Path, today: date) -> dict[date, list[Path]]:
    days: dict[date, list[Path]] = {}
    protected = protected_days(root)
    for stream in root.iterdir():
        if not (stream.name.startswith(("clob_", "rtds_", "spot_", "pmus_ws"))
                or stream.name in {"pmus_books", "kalshi_books", "kalshi_ws"}):
            continue
        if stream.is_symlink() or not stream.is_dir():
            continue
        for path in stream.iterdir():
            if (len(path.name) != 8 or not path.name.isascii() or not path.name.isdigit()
                    or path.is_symlink() or not path.is_dir()):
                continue
            try:
                day = date.fromisoformat(path.name)
            except ValueError:
                continue
            if day < today and path.name not in protected:
                days.setdefault(day, []).append(path)
    return days


def prune_local_floor(root: Path, floor_gib: int) -> int:
    if floor_gib < 0:
        raise ValueError("free-space floor must be nonnegative")
    floor = floor_gib * GIB
    if (free := shutil.disk_usage(root).free) < floor:   # one day per run
        now = datetime.now(timezone.utc)
        days = closed_days(root, now.date())
        oldest = min(days) if days else None
        message = f"deleting live-stream day {oldest:%Y%m%d}" if oldest else "nothing deletable"
        with (root / "janitor.log").open("a") as log:
            print(f"{now:%Y-%m-%dT%H:%M:%SZ} free={free}B < {floor}B: {message}", file=log)
        if oldest is None:
            return 1
        for path in days[oldest]:
            # A stream replaced by a symlink after enumeration must never redirect deletion.
            fd = os.open(path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                shutil.rmtree(path.name, dir_fd=fd)
            finally:
                os.close(fd)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path, nargs="?")
    p.add_argument("floor_gib", type=int, nargs="?", default=12)
    p.add_argument("--local-floor", action="store_true", help="existing protected-day local retention policy")
    p.add_argument("--remote", help="same named rclone destination as the hourly offload")
    p.add_argument("--state", type=Path, help="same proof directory as the hourly offload")
    p.add_argument("--retain-days", type=int, default=10)
    p.add_argument("--storage-policy", type=Path, help="sole production storage policy")
    p.add_argument("--storage-audit", type=Path, help="fresh canonical audit from the same policy")
    p.add_argument("--max-files", type=int, default=12, help="per-run canonical readback budget")
    p.add_argument("--dry-run", action="store_true", help="check local deletion eligibility without remote requests")
    args = p.parse_args()
    if args.storage_policy is not None or args.storage_audit is not None:
        if not args.storage_policy or not args.storage_audit or args.root or args.remote or args.state or args.local_floor:
            p.error("use --storage-policy and --storage-audit together, without a local or alternate archive mode")
        try:
            policy = tomllib.loads(args.storage_policy.read_text())
            result = policy_prune(policy, args.storage_audit, dry_run=args.dry_run, max_files=args.max_files)
        except (OSError, ValueError) as exc:
            p.exit(1, f"{exc}\n")
        print(json.dumps(result, sort_keys=True))
        return int(bool(result["issues"]))
    if args.root is None:
        p.error("root is required outside the canonical storage policy")
    if args.local_floor:
        if args.remote or args.state or args.dry_run:
            p.error("--local-floor is separate from object-backed retention")
        return prune_local_floor(args.root, args.floor_gib)
    if (args.remote is None) != (args.state is None):
        p.error("--remote and --state must be supplied together")
    try:
        target = cold.ObjectStore(args.remote) if args.remote else None
        result = prune(args.root, args.floor_gib, target=target, state=args.state,
                       retain_days=args.retain_days, dry_run=args.dry_run)
    except (OSError, ValueError) as exc:
        p.exit(1, f"{exc}\n")
    print(json.dumps(result, sort_keys=True))
    return int(bool(result["issues"]))


if __name__ == "__main__":
    raise SystemExit(main())
