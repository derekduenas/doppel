#!/usr/bin/env python3
"""Poll recorder and disk health without opening tape segments."""

import argparse
import http.client
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qsl, urlencode, urlsplit

STALL_SECONDS = 60
REPEAT_SECONDS = 900
DISK_PERCENT = 80
LOW_MEMORY_KIB = 600 * 1024
PRESSURE_PERCENT = 40
UNIT_NAME = re.compile(r"[A-Za-z0-9_.@][A-Za-z0-9_.@-]*\.service")


def unit_status(unit):
    result = subprocess.run(
        ["systemctl", "show", "--property=ActiveState,InvocationID,NRestarts", unit],
        capture_output=True, text=True, timeout=3, check=True,
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def recorder_issues(unit, status_path, current, previous, now_ns):
    if current.get("ActiveState") != "active":
        return {f"{unit}/inactive": f"{unit} is {current.get('ActiveState', 'unknown')}"}
    issues = {}
    if previous.get("InvocationID") and current.get("InvocationID") != previous["InvocationID"]:
        issues[f"{unit}/restart"] = f"{unit} restarted"
    elif int(current.get("NRestarts") or 0) > int(previous.get("NRestarts") or 0):
        issues[f"{unit}/restart"] = f"{unit} restarted"

    try:
        report = json.loads(status_path.read_bytes())
        stamp, sources = report["tw"], report["sources"]
        if type(stamp) is not int or not 0 < stamp <= now_ns or not isinstance(sources, dict) or not sources:
            raise ValueError("invalid recorder status")
    except (OSError, KeyError, ValueError, TypeError):
        issues[f"{unit}/status"] = f"{unit} status is missing or invalid"
        return issues

    status_age = (now_ns - stamp) / 1e9
    if status_age > STALL_SECONDS:
        issues[f"{unit}/status"] = f"{unit} status stopped updating for {status_age:.0f}s"
        # A frozen snapshot cannot tell which individual sources are still receiving.
        return issues
    for stream, source in sources.items():
        if not re.fullmatch(r"[a-z0-9_]+", stream):
            issues[f"{unit}/status"] = f"{unit} status contains an invalid stream name"
            continue
        age = source.get("last_msg_age_s") if isinstance(source, dict) else None
        if age is None:
            age = report.get("uptime_s")
        if type(age) not in (int, float) or not math.isfinite(age) or age < 0:
            issues[f"{unit}/{stream}/age"] = f"{unit} {stream} has no usable receipt clock"
        elif age + status_age > STALL_SECONDS:
            issues[f"{unit}/{stream}/stall"] = f"{unit} {stream} stalled for {age + status_age:.0f}s"
    return issues


def disk_issue(path):
    usage = shutil.disk_usage(path)
    # Match df's denominator, which excludes filesystem-reserved free blocks.
    percent = 100 * usage.used / (usage.used + usage.free)
    return f"{path} is {percent:.1f}% full" if percent > DISK_PERCENT else None


def runtime_issues(units, meminfo=Path("/proc/meminfo"),
                   cgroup=Path("/sys/fs/cgroup/system.slice")):
    """Use the same Linux memory signals as the lead's research watchdog."""
    issues = {}
    try:
        available = next(int(line.split()[1]) for line in meminfo.read_text().splitlines()
                         if line.startswith("MemAvailable:"))
        if available < LOW_MEMORY_KIB:
            issues["host/memory"] = f"available host memory is {available / 1024:.0f} MiB"
    except (OSError, StopIteration, ValueError, IndexError):
        issues["host/memory"] = "available host memory is unknown"
    for unit in units:
        path = cgroup / unit / "memory.pressure"
        try:
            samples = path.read_text().splitlines()
        except FileNotFoundError:
            continue  # An inactive or collected systemd unit has no cgroup.
        except OSError:
            issues[f"{unit}/pressure"] = f"{unit} memory pressure is unreadable"
            continue
        try:
            full = next(line.split()[1:] for line in samples if line.startswith("full "))
            avg300 = float(dict(field.split("=", 1) for field in full)["avg300"])
        except (StopIteration, KeyError, ValueError):
            issues[f"{unit}/pressure"] = f"{unit} memory pressure is invalid"
            continue
        if avg300 > PRESSURE_PERCENT:
            issues[f"{unit}/pressure"] = f"{unit} memory stall is {avg300:.1f}% over 300s"
    return issues


def send_discord(credential, messages):
    url = urlsplit(credential.read_text().strip())
    if (url.scheme, url.hostname, url.port, url.username, url.password) not in {
        ("https", "discord.com", None, None, None),
        ("https", "discord.com", 443, None, None),
    } or not re.fullmatch(r"/api/webhooks/[0-9]+/[A-Za-z0-9_-]+", url.path):
        raise ValueError("expected a Discord HTTPS webhook credential")
    query = dict(parse_qsl(url.query))
    query["wait"] = "true"
    text = "DOPPEL recorder alert\n" + "\n".join(messages)
    payload = json.dumps({"content": text[:1900], "allowed_mentions": {"parse": []}}).encode()
    conn = http.client.HTTPSConnection("discord.com", timeout=8)
    try:
        conn.request("POST", f"{url.path}?{urlencode(query)}", payload, {"Content-Type": "application/json"})
        with conn.getresponse() as reply:
            if not 200 <= reply.status < 300:
                raise OSError(f"Discord webhook returned HTTP {reply.status}")
    finally:
        conn.close()


def write_state(path, state):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        name = f.name
        try:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)


def poll(recorders, disks, state_path, credential, *, research_units=(), dry_run=False):
    try:
        state = json.loads(state_path.read_bytes())
    except FileNotFoundError:
        state = {}
    now_ns = time.time_ns()
    now = now_ns // 10**9
    issues, units = runtime_issues(research_units), {}
    for unit, root in recorders:
        try:
            units[unit] = unit_status(unit)
        except (OSError, subprocess.SubprocessError):
            issues[f"{unit}/query"] = f"{unit} service status unavailable"
            units[unit] = state.get("units", {}).get(unit, {})
            continue
        issues.update(recorder_issues(unit, root / "status.json", units[unit],
                                     state.get("units", {}).get(unit, {}), now_ns))
    for disk in disks:
        try:
            message = disk_issue(disk)
        except OSError:
            message = f"disk usage unavailable for {disk}"
        if message:
            issues[f"disk/{disk}"] = message

    previous = state.get("sent", {})
    messages = [message for k, message in issues.items()
                if k.endswith("/restart") or k not in previous or now - previous[k] >= REPEAT_SECONDS]
    if dry_run:
        print(json.dumps(issues, sort_keys=True))
        return
    if messages:
        print("\n".join(messages), flush=True)
        if credential is not None:
            send_discord(credential, messages)
    sent = {k: now if (k not in previous or now - previous[k] >= REPEAT_SECONDS) else previous[k]
            for k in issues}
    write_state(state_path, {"units": units, "sent": sent})


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--recorder", action="append", required=True, metavar="UNIT=TAPE_ROOT")
    p.add_argument("--disk", type=Path, action="append", required=True)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--webhook-file", type=Path)
    p.add_argument("--research-unit", action="append", default=[], metavar="UNIT")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args(argv)
    recorders = []
    for spec in a.recorder:
        unit, sep, root = spec.partition("=")
        if not sep or not UNIT_NAME.fullmatch(unit) or not root:
            p.error("--recorder must be a systemd service name and tape root")
        recorders.append((unit, Path(root)))
    if not all(UNIT_NAME.fullmatch(unit) for unit in a.research_unit):
        p.error("--research-unit must name a systemd service")
    credential = a.webhook_file
    if credential is None and (directory := os.environ.get("CREDENTIALS_DIRECTORY")):
        candidate = Path(directory) / "discord-webhook"
        if candidate.is_file():
            credential = candidate
    try:
        poll(recorders, a.disk, a.state, credential, research_units=a.research_unit, dry_run=a.dry_run)
    except (OSError, ValueError, TypeError, http.client.HTTPException) as exc:
        # Lower-level HTTP and OS exceptions can contain credential paths or URLs.
        print(f"tape alert delivery or state update failed ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
