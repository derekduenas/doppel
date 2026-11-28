"""Remove expired tape segments after verifying their cold archive proof."""

import argparse
from contextlib import nullcontext
from datetime import timedelta
import json
import os
from pathlib import Path
import shutil
import subprocess

if __package__:
    from . import cold_tape as cold
else:
    import cold_tape as cold

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


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    p.add_argument("floor_gib", type=int, nargs="?", default=12)
    p.add_argument("--remote", help="same named rclone destination as the hourly offload")
    p.add_argument("--state", type=Path, help="same proof directory as the hourly offload")
    p.add_argument("--retain-days", type=int, default=10)
    p.add_argument("--dry-run", action="store_true", help="check local deletion eligibility without remote requests")
    args = p.parse_args()
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
