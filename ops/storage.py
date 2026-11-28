#!/usr/bin/env python3
"""Copy and audit DOPPEL data against ops/storage-policy.toml.

  storage.py copy   [--class NAME]   rclone copy each R2 class (checksummed, capped bandwidth); never deletes
  storage.py audit  [--json]         per class: eligible local files vs R2 (missing, size mismatch), plus
                                     anything on the volume that no class covers; exit 1 on a problem

The janitor may prune a local hour only if `audit` lists it as verified (`verified_paths`).
Credentials stay in the rclone config named by the policy; nothing here prints them.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

POLICY = Path(__file__).with_name("storage-policy.toml")
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
            st = p.stat()
            if st.st_mtime <= cutoff:
                out[rel] = st.st_size
    return out


def rclone(policy: dict, *args: str) -> subprocess.CompletedProcess:
    r2 = policy["r2"]
    env = dict(os.environ, RCLONE_CONFIG=r2["config"])
    return subprocess.run([r2["rclone"], *args], env=env, capture_output=True, text=True)


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
    report: dict = {"t": int(now), "classes": {}, "unclassified": unclassified(policy)}
    bad = bool(report["unclassified"])
    for c in classes(policy, name):
        local = local_files(c, policy["do_only"]["skip_names"], now)
        mtimes = {k: (Path(c["local"]) / k).stat().st_mtime for k in local}
        res = compare(local, remote_files(policy, c), mtimes, now, grace + seconds(c["min_age"]))
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
    ap.add_argument("cmd", choices=["copy", "audit"])
    ap.add_argument("--class", dest="cls")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--policy", type=Path, default=POLICY)
    a = ap.parse_args(argv)
    policy = tomllib.loads(a.policy.read_text())
    return cmd_copy(policy, a.cls) if a.cmd == "copy" else cmd_audit(policy, a.json, a.cls)


if __name__ == "__main__":
    sys.exit(main())
