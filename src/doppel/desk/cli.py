"""doppel-desk: build the desk page.

    doppel-desk build --data TAPE --lab LAB --root REPO --out DIR [--now TS]

Writes DIR/desk.json and DIR/index.html (self-contained: inline CSS/JS, desk.json embedded, no
network). Reads ``TAPE/status.json`` and ``TAPE-rust/status.json`` (if present), the registry under
REPO, ``LAB/{twin,census,adverse,clock0,xvenue}`` and ``REPO/evidence/*/FINDINGS.md``. Inputs are
never written. ``--now`` (unix seconds or ISO-8601 UTC) fixes the build instant for reproducible
output; the default is the wall clock.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from .build import build, dumps
from .render import TEMPLATE, render


def parse_now(s: str) -> float:
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"bad timestamp {s!r}") from e
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError(f"naive timestamp {s!r}: give a timezone (e.g. Z)")
    return dt.timestamp()


def find_template(root: Path) -> Path | None:
    for p in (root / TEMPLATE, Path(__file__).resolve().parents[3] / TEMPLATE):
        if p.is_file():
            return p
    return None


def write(out: Path, desk: dict, template: str) -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    j, h = out / "desk.json", out / "index.html"
    for path, data in ((j, dumps(desk) + b"\n"), (h, render(desk, template).encode())):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    return j, h


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-desk")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="write desk.json + index.html")
    b.add_argument("--data", type=Path, required=True, help="tape root (Python recorder); <data>-rust is read too")
    b.add_argument("--lab", type=Path, required=True)
    b.add_argument("--root", type=Path, default=Path("."), help="repo root (registry/, evidence/, desk/)")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--now", type=parse_now, default=None, help="build instant (default: now)")
    a = ap.parse_args(argv)
    tpl = find_template(a.root)
    if tpl is None:
        print(f"page template {TEMPLATE} not found under --root {a.root}", file=sys.stderr)
        return 2
    rust = a.data.with_name(a.data.name + "-rust")
    for p in (a.data, rust, a.lab, a.root):
        if a.out.resolve() == p.resolve():
            print(f"--out must not be an input directory ({p})", file=sys.stderr)
            return 2
    now = a.now if a.now is not None else time.time()
    desk = build(a.data, a.lab, a.root, now)
    j, h = write(a.out, desk, tpl.read_text(encoding="utf-8"))
    rec, st = desk["recorder"], desk["strategies"]
    print(f"recorder: python={'yes' if rec['python'] else 'not yet'} rust={'yes' if rec['rust'] else 'not yet'} "
          f"streams={len(rec['streams'])}")
    print(f"registry: {sum(f['m'] for f in st['families'])} genomes in {len(st['families'])} families, "
          f"twin files {st['twin_files']}, H3 {'ready' if st['h3']['ready'] else 'not yet'}")
    print(f"evidence: {len(desk['evidence']['findings']['rows'])} findings; problems {len(desk['problems'])}")
    print(f"-> {j}\n-> {h}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
