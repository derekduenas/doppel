#!/usr/bin/env python3
"""Copy and audit DOPPEL data against ops/storage-policy.toml.

  storage.py copy   [--class NAME]   rclone copy each R2 class (checksummed, capped bandwidth); never deletes
  storage.py audit  [--json]         per class: eligible local files vs R2 (missing, size mismatch), plus
                                     anything on the volume that no class covers; exit 1 on a problem
  storage.py verify --class NAME    compare exact local and remote SHA-256 for the closed file inventory
                    [--dry-run]     check local seals and list candidate bytes without contacting R2

The janitor uses audit's `verified_paths` as a size-matched shortlist and rechecks exact bytes.
Credentials stay in the rclone config named by the policy; nothing here prints them.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path

if __package__:
    from . import cold_tape as cold
else:
    import cold_tape as cold

POLICY = Path(__file__).with_name("storage-policy.toml")
AUDIT_SCHEMA = "doppel.storage-audit/v2"
VERIFY_SCHEMA = "doppel.storage-sha256/v1"
PERELMAN_HOLD = "perelman-offshore-hold-20261128"
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds(text: str) -> int:
    m = re.fullmatch(r"(\d+)([smhd])", text)
    if not m:
        raise ValueError(f"bad duration {text!r}")
    return int(m.group(1)) * _UNITS[m.group(2)]


def matches(rel: str, patterns: list[str]) -> bool:
    for p in patterns:
        if p == "**" or fnmatch.fnmatch(rel, p) or (p.startswith("**/") and fnmatch.fnmatch(rel, p[3:])):
            return True
    return False


def skipped(rel: str, skip_names: list[str]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name, s) for s in skip_names)


def local_files(cls: dict, skip_names: list[str], now: float) -> dict[str, int]:
    """Eligible files (relative path -> size): matching an include, not skipped, older than min_age."""
    root, cutoff = Path(cls["local"]), now - seconds(cls["min_age"])
    out: dict[str, int] = {}
    if not root.is_dir():
        return out
    for dirpath, _, names in os.walk(root):
        for n in names:
            p = Path(dirpath) / n
            rel = p.relative_to(root).as_posix()
            if not matches(rel, cls["include"]) or skipped(rel, skip_names):
                continue
            try:
                st = p.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_mtime <= cutoff:
                out[rel] = st.st_size
    return out


def rclone(policy: dict, *args: str) -> subprocess.CompletedProcess:
    r2 = policy["r2"]
    env = dict(os.environ, RCLONE_CONFIG=r2["config"])
    return subprocess.run([r2["rclone"], *args], env=env, capture_output=True, text=True)


def policy_sha256(policy: dict) -> str:
    return hashlib.sha256(cold.json_bytes(policy)).hexdigest()


def archive_owns(cls: dict) -> bool:
    return cls.get("uploader") == "archive_tape"


def local_seals(root: Path) -> dict:
    if not root.is_dir():
        return {}
    return dict(cold.manifests(root, ["auto"], None, cold.closed_hour()))


def archive_key(name: str, checksum: str | None, remote: dict[str, int]) -> str:
    """Prefer the exact immutable generation and keep flat-key backfill readable."""
    if isinstance(checksum, str) and cold.SHA256.fullmatch(checksum):
        immutable = cold.data_key(name, checksum)
        if immutable in remote:
            return immutable
    return name


def remote_key(policy: dict, cls: dict, name: str) -> str:
    return f"{policy['r2']['remote']}/{cold.relative(cls['r2'])}/{cold.relative(name)}"


def remote_digest(policy: dict, cls: dict, name: str, expected_bytes: int) -> str:
    """Read exactly one object, without writes or buffering a segment in memory."""
    r2 = policy["r2"]
    args = [r2["rclone"], "cat", remote_key(policy, cls, name), "--bwlimit", r2["bwlimit"],
            "--contimeout", "10s", "--timeout", "30s", "--retries", "1"]
    env = dict(os.environ, RCLONE_CONFIG=r2["config"])
    with subprocess.Popen(args, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as proc:
        assert proc.stdout is not None
        digest, total = hashlib.sha256(), 0
        while chunk := proc.stdout.read(1024 * 1024):
            total += len(chunk)
            if total > expected_bytes:
                proc.kill()
                raise ValueError(f"remote object exceeds expected length: {name}")
            digest.update(chunk)
        if proc.wait() != 0 or total != expected_bytes:
            raise ValueError(f"remote object unavailable or wrong length: {name}")
    return digest.hexdigest()


def summarize_verify(policy: dict, cls: dict, *, dry_run: bool, inventory: list[str] | None = None) -> dict:
    """Compare the planned immutable generations against their canonical R2 keys."""
    from datetime import datetime, timezone

    if cls["name"] != PERELMAN_HOLD and not cls["local_days"]:
        raise ValueError("verification requires a tape class or the closed PERELMAN hold")
    root = Path(cls["local"])
    eligible = local_files(cls, policy["do_only"]["skip_names"], time.time())
    if cls["name"] == PERELMAN_HOLD:
        candidates = {name for name in eligible if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}\.jsonl\.gz", name)}
        seals: dict = {}
    else:
        candidates = {name for name in eligible if name.endswith(".dtape.zst")}
        seals = local_seals(root)
    paths = sorted(candidates) if inventory is None else inventory
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("verification requires a nonempty inventory of unique paths")
    proof: dict = {"schema": VERIFY_SCHEMA, "policy_sha256": policy_sha256(policy), "class": cls["name"],
                   "dry_run": dry_run, "objects": {}, "issues": []}
    remote = remote_files(policy, cls) if archive_owns(cls) and not dry_run else {}
    for name in paths:
        if name not in candidates:
            proof["issues"].append(f"path missing, open, or outside archive class: {name}")
            continue
        try:
            with cold.open_file(root, name) as (source, parent_fd, leaf):
                stamp = cold.file_stamp(os.fstat(source.fileno()))
                if stamp[2] != eligible[name]:
                    raise ValueError(f"local generation changed during inventory: {name}")
                if cls["name"] == PERELMAN_HOLD:
                    hour = datetime.strptime(name, "%Y-%m-%dT%H.jsonl.gz").replace(tzinfo=timezone.utc)
                    if hour >= cold.closed_hour() or source.read(2) != b"\x1f\x8b":
                        raise ValueError(f"not a closed PERELMAN gzip hour: {name}")
                    source.seek(0)
                    expected = None
                else:
                    if not (history := seals.get(name)):
                        raise ValueError(f"missing native segment seal: {name}")
                    identity, _, _ = cold.describe_file(root, name, history, source, stamp,
                                                         legacy=False, dry_run=dry_run)
                    expected = identity["sha256"]
                checksum = expected
                key = name
                if not dry_run:
                    checksum = hashlib.file_digest(source, "sha256").hexdigest()
                    if expected is not None and checksum != expected:
                        raise ValueError(f"local tape differs from native segment seal: {name}")
                    key = archive_key(name, checksum, remote)
                    if remote_digest(policy, cls, key, stamp[2]) != checksum:
                        raise ValueError(f"remote SHA-256 mismatch: {name}")
                if not cold.unchanged(source, parent_fd, leaf, stamp):
                    raise ValueError(f"local generation changed during verification: {name}")
                proof["objects"][name] = {"bytes": stamp[2], "sha256": checksum, "remote_verified": not dry_run}
                if key != name:
                    proof["objects"][name]["r2_key"] = key
        except (ValueError, OSError) as exc:
            proof["issues"].append(str(exc))
    return proof


def remote_files(policy: dict, cls: dict) -> dict[str, int]:
    r = rclone(policy, "lsjson", "-R", "--files-only", f"{policy['r2']['remote']}/{cls['r2']}")
    if r.returncode != 0:
        if "directory not found" in r.stderr.lower():
            return {}
        raise RuntimeError(f"rclone lsjson failed for {cls['name']}: exit {r.returncode}")
    return {e["Path"]: int(e["Size"]) for e in json.loads(r.stdout or "[]")}


def compare(local: dict[str, int], remote: dict[str, int], mtimes: dict[str, float], now: float, grace: int) -> dict:
    """Missing / size-mismatched files; only those older than min_age + grace are errors."""
    missing = sorted(k for k in local if k not in remote)
    mismatch = sorted(k for k in local if k in remote and remote[k] != local[k])
    overdue = [k for k in missing + mismatch if now - mtimes[k] > grace]
    verified = sorted(k for k in local if remote.get(k) == local[k])
    return {"eligible": len(local), "verified": len(verified), "missing": missing, "size_mismatch": mismatch,
            "overdue": overdue, "verified_paths": verified}


def unclassified(policy: dict) -> list[str]:
    """Top-level entries under the classified roots that no class, do_only or skip rule covers."""
    covered = {c["local"] for c in policy["class"]} | set(policy["do_only"]["paths"]) | set(policy["never"]["paths"])
    out = []
    for root in policy["audit"]["classified_roots"]:
        base = Path(root)
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if str(child) not in covered and not skipped(child.name, policy["do_only"]["skip_names"]):
                out.append(str(child))
    return out


def classes(policy: dict, name: str | None) -> list[dict]:
    cs = [c for c in policy["class"] if name in (None, c["name"])]
    if name and not cs:
        raise SystemExit(f"no class {name!r}")
    return cs


def cmd_copy(policy: dict, name: str | None) -> int:
    rc = 0
    for c in classes(policy, name):
        if archive_owns(c):
            print(f"copy {c['name']}: delegated to archive_tape", flush=True)
            if name is not None:
                return 1
            continue
        if not Path(c["local"]).is_dir():
            continue
        args = ["copy", c["local"], f"{policy['r2']['remote']}/{c['r2']}", "--checksum", "--min-age", c["min_age"],
                "--bwlimit", policy["r2"]["bwlimit"], "--transfers", "4", "--retries", "5"]
        for p in c["include"]:
            args += ["--include", p]
        for s in policy["do_only"]["skip_names"]:
            args += ["--exclude", f"**/{s}", "--exclude", s]
        r = rclone(policy, *args)
        print(f"copy {c['name']}: exit {r.returncode}", flush=True)
        if r.returncode != 0:
            rc = 1
            print(r.stderr[-800:], file=sys.stderr)
    return rc


def cmd_audit(policy: dict, as_json: bool, name: str | None) -> int:
    now, grace = time.time(), seconds(policy["audit"]["grace"])
    report: dict = {"schema": AUDIT_SCHEMA, "t": int(now), "policy_sha256": policy_sha256(policy),
                    "classes": {}, "unclassified": unclassified(policy)}
    bad = bool(report["unclassified"])
    for c in classes(policy, name):
        local = local_files(c, policy["do_only"]["skip_names"], now)
        mtimes = {k: (Path(c["local"]) / k).stat().st_mtime for k in local}
        remote = remote_files(policy, c)
        keys = {}
        if archive_owns(c):
            seals = local_seals(Path(c["local"]))
            keys = {k: archive_key(k, history[-1].get("sha256"), remote)
                    for k, history in seals.items() if k in local}
            remote = {k: remote[key] for k, key in keys.items() if key in remote}
        res = compare(local, remote, mtimes, now, grace + seconds(c["min_age"]))
        if archive_owns(c):
            res["remote_keys"] = {k: keys[k] for k in res["verified_paths"]}
        res["local_days"] = c["local_days"]
        report["classes"][c["name"]] = res
        bad |= bool(res["overdue"])
    if as_json:
        print(json.dumps(report))
    else:
        for n, r in report["classes"].items():
            print(f"{n:34s} eligible {r['eligible']:6d}  in R2 {r['verified']:6d}  missing {len(r['missing']):5d}"
                  f"  size-mismatch {len(r['size_mismatch']):3d}  overdue {len(r['overdue']):5d}")
        for u in report["unclassified"]:
            print(f"UNCLASSIFIED {u}  (add it to ops/storage-policy.toml)")
    return 1 if bad else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["copy", "audit", "verify"])
    ap.add_argument("--class", dest="cls")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="inspect local seals without reading R2")
    ap.add_argument("--inventory", type=Path, help="verify exactly the given relative paths, one per line")
    ap.add_argument("--policy", type=Path, default=POLICY)
    a = ap.parse_args(argv)
    policy = tomllib.loads(a.policy.read_text())
    if a.cmd == "verify":
        if not a.cls:
            ap.error("verify requires --class")
        try:
            inventory = a.inventory.read_text().splitlines() if a.inventory else None
            result = summarize_verify(policy, classes(policy, a.cls)[0], dry_run=a.dry_run, inventory=inventory)
        except (OSError, RuntimeError, ValueError) as exc:
            ap.exit(1, f"{exc}\n")
        print(json.dumps(result, sort_keys=True))
        return int(bool(result["issues"]))
    if a.dry_run or a.inventory:
        ap.error("--dry-run and --inventory apply only to verify")
    return cmd_copy(policy, a.cls) if a.cmd == "copy" else cmd_audit(policy, a.json, a.cls)


if __name__ == "__main__":
    sys.exit(main())
