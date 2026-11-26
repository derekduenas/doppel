"""``doppel-genome``: extract candidate genomes from dossiers, preregister them, read the ledger.

    doppel-genome extract  --dossier LAB/dossiers/0xabc.json [--out DIR] [--mechanism LABEL]
    doppel-genome register --genome G.json [G2.json ...] --family F --hypothesis "..." \
                           [--start ISO --end ISO]   (default: each genome's evidence.eval_window)
    doppel-genome result   --id GENOME_ID --p 0.012 [--verdict '{"net_c_per_window": 0.8}']
    doppel-genome ledger   --family F [--alpha 0.05] [--json]
    doppel-genome monitor  --genome ID|PREFIX|G.json --results TWIN_OUT.json [--alpha 0.05] [--record] [--json]
    doppel-genome verify

``monitor`` prints anytime-valid e-values and the early KILL/PASS decision (``sequential.py``,
docs/sequential.md); ``--record`` appends it as an ``interim``/``early_*`` event.
``register``/``result``/``ledger``/``monitor``/``verify`` use ``<root>/registry/genomes.jsonl`` (``--root``,
default the current directory).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import orjson

from .extract import extract
from .genome import describe, from_dict, to_dict
from .registry import Registry, RegistryError


def _table(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    import pyarrow.parquet as pq
    return pq.read_table(path).to_pylist()


def cmd_extract(a: argparse.Namespace) -> int:
    doc = orjson.loads(a.dossier.read_bytes())
    stem = str(a.dossier)[:-5] if str(a.dossier).endswith(".json") else str(a.dossier)
    orders = None if a.no_tables else _table(Path(f"{stem}.orders.parquet"))
    fills = None if a.no_tables else _table(Path(f"{stem}.fills.parquet"))
    gs, refusals = extract(doc, orders=orders, fills=fills, mechanism=a.mechanism)
    out = a.out or Path("genomes") / "candidates" / str(doc.get("wallet", "unknown"))
    print(f"dossier {a.dossier} (tables: orders={'yes' if orders is not None else 'no'}, "
          f"fills={'yes' if fills is not None else 'no'})")
    if gs:
        out.mkdir(parents=True, exist_ok=True)
    for g in gs:
        p = out / f"{g.name}-{g.id[:12]}.json"
        p.write_bytes(orjson.dumps(to_dict(g), option=orjson.OPT_INDENT_2))
        print(f"\n{g.id}  -> {p}\n{describe(g)}")
    for r in refusals:
        extra = f" {r['side']}" if r.get("side") else ""
        path = f" [{' AND '.join(r['path'])}]" if r.get("path") else ""
        print(f"\nREFUSED {r['kind']} {r['scope']}{extra}{path}: {r['reason']}")
    print(f"\n{len(gs)} candidate genome(s), {len(refusals)} refusal(s)")
    return 0


def cmd_register(a: argparse.Namespace) -> int:
    reg = Registry(a.root)
    for path in a.genome:
        d = orjson.loads(Path(path).read_bytes())
        g = from_dict(d)
        win = (a.start, a.end) if a.start and a.end else tuple((g.evidence or {}).get("eval_window") or ())
        if len(win) != 2:
            print(f"{path}: no --start/--end and no evidence.eval_window", file=sys.stderr)
            return 2
        try:
            rec = reg.register(g, a.family, a.hypothesis, (win[0], win[1]))
        except RegistryError as e:
            print(f"{path}: REFUSED {e}", file=sys.stderr)
            return 2
        print(f"{rec['genome_id'][:12]} {rec['name']} family={rec['family']} "
              f"window={rec['eval_window']['start']}..{rec['eval_window']['end']} seq={rec['seq']} "
              f"code={str(rec['code_sha'])[:12]}{' (dirty)' if rec.get('code_dirty') else ''}")
    print(f"head {reg.head()}")
    return 0


def cmd_result(a: argparse.Namespace) -> int:
    v = orjson.loads(a.verdict) if a.verdict else {}
    v["p_value"] = a.p
    try:
        rec = Registry(a.root).record_result(a.id, v)
    except RegistryError as e:
        print(f"REFUSED {e}", file=sys.stderr)
        return 2
    print(f"{rec['genome_id'][:12]} result seq={rec['seq']} binding={rec['binding']} p={a.p}")
    return 0


def cmd_ledger(a: argparse.Namespace) -> int:
    led = Registry(a.root).ledger(a.family, a.alpha)
    if a.json:
        print(orjson.dumps(led, option=orjson.OPT_INDENT_2).decode())
        return 0
    print(f"family {led['family']}: m={led['m']} tested={led['tested']} pending={led['pending']} "
          f"(Holm, alpha {led['alpha']}; pending counted as p=1)")
    for r in led["rows"]:
        p = "-" if r["p_value"] is None else f"{r['p_value']:.4g}"
        print(f"  {r['genome_id'][:12]} {r['name']:<32} {r['status']:<7} p={p:<8} p_holm={r['p_holm']:.4g} "
              f"{'SURVIVES' if r['survives'] else ''}")
    print(f"winner: {led['winner'] or 'none'}")
    return 0


def _resolve_genome(reg: Registry, ref: str) -> str:
    """A genome JSON path, a full id, or a unique id prefix of a registered genome -> id."""
    p = Path(ref)
    if p.is_file():
        return from_dict(orjson.loads(p.read_bytes())).id
    ids = [r["genome_id"] for r in reg.records() if r["kind"] == "register" and r["genome_id"].startswith(ref)]
    if len(ids) != 1:
        raise RegistryError(f"{ref!r} matches {len(ids)} registered genomes")
    return ids[0]


def _fmt_e(d: dict) -> str:
    return (f"E={d['e']:.4g} max={d['e_max']:.4g} (1/alpha {d['threshold']:g}) p_anytime={d['p_anytime']:.4g}"
            f"{' CROSSED at block ' + str(d['first_cross_block']) if d['rejects'] else ''}"
            f" | clipped {d['clipped_conservative']} (conservative side), bound violations {d['bound_violations']}")


def cmd_monitor(a: argparse.Namespace) -> int:
    import hashlib
    reg = Registry(a.root)
    raw = a.results.read_bytes()
    doc = orjson.loads(raw)
    src = {"results_path": str(a.results), "results_sha256": hashlib.sha256(raw).hexdigest()}
    try:
        gid = _resolve_genome(reg, a.genome)
        if a.record:
            m = reg.record_monitor(gid, doc, a.alpha, src)
        else:
            m = reg.monitor(gid, doc, a.alpha, src)
    except RegistryError as e:
        print(f"REFUSED {e}", file=sys.stderr)
        return 2
    if a.json:
        print(orjson.dumps(m, option=orjson.OPT_INDENT_2).decode())
        return 0
    s, prm, fh = m["stats"], m["params"], m["family_holm"]
    print(f"{m['genome_id'][:12]} family={m['family']} eval {m['eval_window'][0]}..{m['eval_window'][1]} "
          f"as_of {m['as_of']}{' (window closed)' if m.get('window_closed') else ''}")
    print(f"windows used={s['windows_used']} (seen {s['windows_seen']}, traded {s['windows_traded']}) "
          f"blocks={s['n_blocks']}x{prm['block']} mean net/window={s['mean_c']}c bound=+/-{prm['bound_c']:g}c "
          f"[{prm['method']}]")
    print(f"PASS e-process (H0: mean net/window <= 0): {_fmt_e(s['pass'])}")
    print(f"KILL e-process (H0: mean net/window >= 0): {_fmt_e(s['kill'])}")
    print(f"family e-Holm: m={fh['m']} p_anytime={fh['p_anytime']:.4g} p_holm={fh['p_holm']:.4g} "
          f"(alpha {fh['alpha']:g})")
    print(f"decision: {m['decision']}: {m['reason']}")
    if a.record:
        print(f"recorded {m['kind']} seq={m['seq']} binding={m['binding']} head {reg.head()[:16]}")
    else:
        print("not recorded (pass --record to append)")
    print("statistical only: the commercial hurdle (A6) and owner authorization are separate")
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    reg = Registry(a.root)
    errs = reg.verify()
    for e in errs:
        print(e)
    print(f"{reg.path}: {len(reg.lines())} lines, head {reg.head()[:16]}, {'OK' if not errs else 'BROKEN'}")
    return 0 if not errs else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-genome")
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="dossier JSON -> candidate genome JSONs")
    e.add_argument("--dossier", type=Path, required=True)
    e.add_argument("--out", type=Path, default=None, help="default genomes/candidates/<wallet>")
    e.add_argument("--mechanism", default=None, help="census label for Source.mechanism")
    e.add_argument("--no-tables", action="store_true", help="ignore <wallet>.{orders,fills}.parquet")
    e.set_defaults(fn=cmd_extract)
    r = sub.add_parser("register", help="preregister genome(s) in a family")
    r.add_argument("--genome", nargs="+", required=True)
    r.add_argument("--family", required=True)
    r.add_argument("--hypothesis", required=True)
    r.add_argument("--start", default=None, help="ISO-8601 with Z/offset")
    r.add_argument("--end", default=None)
    r.add_argument("--root", type=Path, default=Path("."))
    r.set_defaults(fn=cmd_register)
    s = sub.add_parser("result", help="append a verdict (after the evaluation window closed)")
    s.add_argument("--id", required=True)
    s.add_argument("--p", type=float, required=True, help="one-sided p-value, H1: net per window > 0")
    s.add_argument("--verdict", default=None, help="extra verdict fields as JSON")
    s.add_argument("--root", type=Path, default=Path("."))
    s.set_defaults(fn=cmd_result)
    lg = sub.add_parser("ledger", help="family false-discovery ledger (Holm)")
    lg.add_argument("--family", required=True)
    lg.add_argument("--alpha", type=float, default=0.05)
    lg.add_argument("--json", action="store_true")
    lg.add_argument("--root", type=Path, default=Path("."))
    lg.set_defaults(fn=cmd_ledger)
    mo = sub.add_parser("monitor", help="anytime-valid e-values + early KILL/PASS (no write without --record)")
    mo.add_argument("--genome", required=True, help="genome JSON path, id, or unique id prefix")
    mo.add_argument("--results", type=Path, required=True, help="doppel-twin --out JSON")
    mo.add_argument("--alpha", type=float, default=0.05)
    mo.add_argument("--record", action="store_true", help="append the interim/early event to the registry")
    mo.add_argument("--json", action="store_true")
    mo.add_argument("--root", type=Path, default=Path("."))
    mo.set_defaults(fn=cmd_monitor)
    v = sub.add_parser("verify", help="re-check the hash chain")
    v.add_argument("--root", type=Path, default=Path("."))
    v.set_defaults(fn=cmd_verify)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
