"""doppel-paper: run a sealed genome on paper against the live tape, and audit it.

    doppel-paper run --genome G.json --data TAPE --journal DIR [--from-now | --from TS] [--until TS]
    doppel-paper agree --journal DIR --data TAPE [--out agree.json]
    doppel-paper footprint --journal DIR --out DIR
    doppel-paper verify --journal DIR

``run`` on an existing journal resumes it (restart recovery); a new journal needs ``--from-now`` or
``--from``. Touch ``DIR/KILL`` (or any ``--kill-file``) to halt. Nothing here can place an order.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

import orjson

from ..twin.cli import parse_ts
from ..twin.engine import SimConfig
from .journal import JournalError, iter_records
from .runtime import PaperRuntime, RecoveryError, Settings, check_binding, check_genome, load_genome
from .tail import TapeError


def _run(a) -> int:
    g = load_genome(a.genome)
    errs = check_genome(g)
    if errs:
        print("genome not runnable on paper: " + "; ".join(errs), file=sys.stderr)
        return 2
    cfg = SimConfig(latency_ms=a.latency_ms, opening=a.opening)
    errs = check_binding(g, cfg, a.registry)
    if errs:
        print("refused: " + "; ".join(errs), file=sys.stderr)
        return 2
    settings = Settings(grace_s=a.grace_s, max_loss_usd=a.max_loss_usd,
                        kill_files=tuple(str(p) for p in a.kill_file) or (str(a.journal / "KILL"),),
                        perelman_src=str(a.perelman) if a.perelman else None)
    rt = PaperRuntime(g, a.data, a.journal, cfg, settings)
    from_s = int(time.time()) if a.from_now else a.t_from
    try:
        rt.start(from_s)
    except (RecoveryError, JournalError, ValueError) as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 3
    stop = {"x": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("x", True))
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("x", True))
    while not stop["x"]:
        try:
            W = rt.cycle()
        except TapeError as exc:
            print(f"stopped on a tape failure (journaled): {exc}", file=sys.stderr)
            return 3
        if rt.finished:
            print("halted and every window settled: exiting")
            return 0
        if a.until is not None and W >= a.until * 1_000_000_000:
            break
        time.sleep(a.poll_s)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-paper")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--genome", type=Path, required=True)
    r.add_argument("--data", type=Path, required=True, help="tape root (read only)")
    r.add_argument("--journal", type=Path, required=True)
    src = r.add_mutually_exclusive_group()
    src.add_argument("--from-now", action="store_true")
    src.add_argument("--from", dest="t_from", type=parse_ts, help="start a new journal at TS (catch-up from the tape)")
    r.add_argument("--until", type=parse_ts, help="stop once the watermark reaches TS")
    r.add_argument("--latency-ms", type=float, default=SimConfig().latency_ms)
    r.add_argument("--opening", choices=("published", "received_raw"), required=True,
                   help="opening-price rule (required; must equal the genome's bound evaluation protocol)")
    r.add_argument("--registry", type=Path, default=Path("."),
                   help="repo root holding registry/genomes.jsonl (protocol bindings are enforced)")
    r.add_argument("--perelman", type=Path,
                   help="raw BRTI rows: doppel-brti-relay's output directory (or PERELMAN's stream dir), read only")
    r.add_argument("--grace-s", type=float, default=Settings().grace_s)
    r.add_argument("--max-loss-usd", type=float, help="global stop (default: genome risk.max_loss_per_day)")
    r.add_argument("--kill-file", type=Path, action="append", default=[])
    r.add_argument("--poll-s", type=float, default=0.25)
    ag = sub.add_parser("agree")
    ag.add_argument("--journal", type=Path, required=True)
    ag.add_argument("--data", type=Path, required=True)
    ag.add_argument("--out", type=Path)
    fp = sub.add_parser("footprint")
    fp.add_argument("--journal", type=Path, required=True)
    fp.add_argument("--out", type=Path, required=True)
    fp.add_argument("--no-fillstore", action="store_true")
    vf = sub.add_parser("verify")
    vf.add_argument("--journal", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return _run(a)
    if a.cmd == "verify":
        n, top = 0, "-"
        try:
            for r in iter_records(a.journal):
                n, top = n + 1, r["h"]
        except JournalError as exc:
            print(f"journal INVALID: {exc}", file=sys.stderr)
            return 1
        print(f"journal OK: {n} records, chain head {top}")
        return 0
    if a.cmd == "agree":
        from .agree import agree
        doc = agree(a.journal, a.data)
        out = a.out or a.journal / "agree.json"
        out.write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
        if not doc.get("windows_compared"):
            print("agreement: no window compared", file=sys.stderr)
            return 1
        print(f"windows compared={doc.get('windows_compared', 0)} identical={doc.get('windows_identical', 0)} "
              f"inputs={doc.get('inputs_identical_share')} intents={doc.get('intents_identical_share')} "
              f"fills={doc.get('fills_identical_share')} net diff ${doc.get('net_diff_usd')} "
              f"same-input-diff-intent={doc.get('same_input_diff_intent')} paper_only={len(doc.get('paper_only', {}))}")
        return 0 if not doc.get("same_input_diff_intent") else 1
    from .footprint import convert
    doc = convert(a.journal, a.out, fillstore=not a.no_fillstore)
    print(f"footprint: wallet {doc['wallet']} orders {doc['orders']} filled {doc['orders_filled']} rows {doc['order_rows']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
