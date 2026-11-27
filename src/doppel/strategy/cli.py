"""``doppel-genome``: extract candidate genomes from dossiers, preregister them, read the ledger.

    doppel-genome extract  --dossier LAB/dossiers/0xabc.json [--out DIR] [--mechanism LABEL]
    doppel-genome register --genome G.json [G2.json ...] --family F --hypothesis "..." \
                           [--start ISO --end ISO]   (default: each genome's evidence.eval_window)
    doppel-genome result   --id GENOME_ID --data FROZEN_TAPE [--lab FROZEN_LAB]
                           (or --results TWIN_OUT.json / --p 0.012 for a nonbinding result)
    doppel-genome ledger   --family F [--alpha 0.05] [--json]
    doppel-genome monitor  --genome ID|PREFIX|G.json --data FROZEN_TAPE [--lab FROZEN_LAB] [--record]
    doppel-genome bind-protocol --family F|--id GENOME_ID --protocol NAME [--execute]
    doppel-genome verify

``monitor`` prints anytime-valid e-values and the early KILL/PASS decision (``sequential.py``,
docs/sequential.md); ``--record`` appends it as an ``interim``/``early_*`` event.
``register``/``result``/``ledger``/``monitor``/``verify`` use ``<root>/registry/genomes.jsonl`` (``--root``,
default the current directory).
"""

from __future__ import annotations

import argparse
import sys
import time
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
    from ..twin.evaluate import protocol
    from . import dna
    reg = Registry(a.root)
    evaluation_protocol = protocol()
    known = {v["expression_sha256"]: v["name"] for v in dna.load_store(a.root)["genomes"].values()}
    for path in a.genome:
        d = orjson.loads(Path(path).read_bytes())
        g = from_dict(d)
        twin = known.get(dna.expression_trace(g)["expression_sha256"])
        if twin is not None:  # informational: same behaviour as an already-sealed genome
            print(f"{path}: NOTE behaves identically to sealed genome {twin} (expression hash match)",
                  file=sys.stderr)
        win = (a.start, a.end) if a.start and a.end else tuple((g.evidence or {}).get("eval_window") or ())
        if len(win) != 2:
            print(f"{path}: no --start/--end and no evidence.eval_window", file=sys.stderr)
            return 2
        try:
            rec = reg.register(g, a.family, a.hypothesis, (win[0], win[1]),
                               evaluation_protocol=evaluation_protocol)
        except RegistryError as e:
            print(f"{path}: REFUSED {e}", file=sys.stderr)
            return 2
        print(f"{rec['genome_id'][:12]} {rec['name']} family={rec['family']} "
              f"window={rec['eval_window']['start']}..{rec['eval_window']['end']} seq={rec['seq']} "
              f"code={str(rec['code_sha'])[:12]}{' (dirty)' if rec.get('code_dirty') else ''}")
    added = dna.write_missing(a.root)
    print(f"head {reg.head()}; expression hashes stored for {len(added)} new genome(s)")
    return 0


def cmd_result(a: argparse.Namespace) -> int:
    rng: tuple | None = None
    reg = Registry(a.root)
    try:
        if a.data is not None:
            from ..twin.evaluate import evaluate

            registration = reg.registration(a.id)
            if registration is None:
                raise RegistryError(f"genome {a.id} is not registered")
            w = registration["eval_window"]
            if time.time() < w["end_s"]:
                raise RegistryError(f"evaluation window closes {w['end']}")
            v = evaluate(a.data, a.lab, registration, binding=reg.protocol_binding(a.id),
                         protocol_name=a.protocol)
            rng = (w["start_s"], w["end_s"])
        elif a.results:
            doc = orjson.loads(a.results.read_bytes())
            gid = (doc.get("genome") or {}).get("id")
            if gid != a.id:
                raise RegistryError(f"results are for genome {str(gid)[:12]}, not {a.id[:12]}")
            v = {**(doc.get("verdict") or {}), "p_source": "imported"}
            if v.get("decision") == "DIAGNOSTIC" or v.get("p_value") is None:
                raise RegistryError("diagnostic run or no p_value in the twin verdict")
            v["twin_config"] = (doc.get("twin") or {}).get("config")
            v["twin_code_sha256"] = (doc.get("twin") or {}).get("code_sha256")
            rng = tuple(doc["range"]) if doc.get("range") else None
        else:
            v = orjson.loads(a.verdict) if a.verdict else {}
            v["p_value"] = a.p
            v["p_source"] = "manual"
        rec = reg.record_result(a.id, v, rng)
    except (OSError, ValueError) as e:
        print(f"REFUSED {e}", file=sys.stderr)
        return 2
    print(f"{rec['genome_id'][:12]} result seq={rec['seq']} binding={rec['binding']} p={v['p_value']}")
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
    from ..twin.evaluate import replay_snapshot
    reg = Registry(a.root)
    try:
        gid = _resolve_genome(reg, a.genome)
        if a.data is not None:
            registration = reg.registration(gid)
            if registration is None:
                raise RegistryError("genome is not registered")
            # Receipts cover closed wall-clock hours, including the hour of the final window.
            through = min(int(registration["eval_window"]["end_s"]), int(time.time()) // 3600 * 3600)
            windows, evidence = replay_snapshot(a.data, a.lab, registration, through=through,
                                                binding=reg.protocol_binding(gid), protocol_name=a.protocol)
            doc = {"genome": {"id": gid}, "windows": windows, "range": evidence["range"],
                   "twin": {"code_sha256": evidence["code_sha256"], "diagnostic": False}}
            src = {"evaluation": evidence}
        else:
            if a.record:
                raise RegistryError("recording a monitoring decision requires --data with a sealed snapshot")
            raw = a.results.read_bytes()
            doc = orjson.loads(raw)
            src = {"results_path": str(a.results), "results_sha256": hashlib.sha256(raw).hexdigest()}
        if a.record:
            m = reg.record_monitor(gid, doc, a.alpha, src)
        else:
            m = reg.monitor(gid, doc, a.alpha, src)
    except (OSError, ValueError) as e:
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


def cmd_bind_protocol(a: argparse.Namespace) -> int:
    from ..twin.evaluate import protocol_binding
    reg = Registry(a.root)
    try:
        binding = protocol_binding(a.protocol)
        plan = reg.plan_protocol_binding(binding, family=a.family, genome_id=a.id)
    except (OSError, ValueError) as e:
        print(f"REFUSED {e}", file=sys.stderr)
        return 2
    if "hash" in plan:
        print(f"already bound (seq {plan['seq']}): nothing to append")
        return 0
    print(f"will append to {reg.path} after head {reg.head()} (line {len(reg.lines())}); "
          "tw, seq, prev and hash are set at append time:")
    print(orjson.dumps(plan, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS).decode())
    if not a.execute:
        print("not written: pass --execute to append")
        return 0
    try:
        rec = reg.bind_protocol(binding, family=a.family, genome_id=a.id)
    except (OSError, ValueError) as e:
        print(f"REFUSED {e}", file=sys.stderr)
        return 2
    print(f"appended seq={rec['seq']} head {reg.head()}")
    print(orjson.dumps(rec, option=orjson.OPT_SORT_KEYS).decode())
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    reg = Registry(a.root)
    errs = reg.verify()
    for e in errs:
        print(e)
    print(f"{reg.path}: {len(reg.lines())} lines, head {reg.head()[:16]}, {'OK' if not errs else 'BROKEN'}")
    return 0 if not errs else 1


def cmd_dna(a) -> int:
    from . import dna
    if a.migrate:
        ch = dna.migrate(a.root, a.migrate)
        for c in ch:
            print(f"MIGRATED {c['genome_id'][:12]} {c['name']}: {str(c['old'])[:12]} -> {c['new'][:12]}")
        print(f"migration logged: {len(ch)} genome(s) changed")
    if a.write:
        added = dna.write_missing(a.root)
        print(f"stored expression hashes for {len(added)} genome(s)")
    probs = dna.check(a.root)
    for p in probs:
        print("PROBLEM", p)
    print(f"{dna.EXPRESSION_FILE}: {'OK' if not probs else f'{len(probs)} problem(s)'} "
          f"(semantics {dna.semantics_digest()[:12]}, probe v{dna.PROBE_VERSION})")
    return 1 if probs else 0


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
    source = s.add_mutually_exclusive_group(required=True)
    source.add_argument("--data", type=Path, help="sealed tape snapshot for canonical replay")
    source.add_argument("--results", type=Path, help="doppel-twin output JSON (nonbinding import)")
    source.add_argument("--p", type=float, help="manual one-sided p (nonbinding)")
    s.add_argument("--lab", type=Path, help="snapshot of the token map for intl-flow genomes")
    s.add_argument("--verdict", default=None, help="extra verdict fields as JSON")
    s.add_argument("--protocol", default=None,
                   help="evaluation protocol for --data (default: the bound one, else published-opening)")
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
    monitor_input = mo.add_mutually_exclusive_group(required=True)
    monitor_input.add_argument("--results", type=Path, help="inspect imported doppel-twin JSON")
    monitor_input.add_argument("--data", type=Path, help="sealed tape snapshot starting at the registered window")
    mo.add_argument("--lab", type=Path, help="frozen token map directory for intl-flow genomes")
    mo.add_argument("--alpha", type=float, default=0.05)
    mo.add_argument("--record", action="store_true", help="append the interim/early event to the registry")
    mo.add_argument("--protocol", default=None,
                    help="evaluation protocol for --data (default: the bound one, else published-opening)")
    mo.add_argument("--json", action="store_true")
    mo.add_argument("--root", type=Path, default=Path("."))
    mo.set_defaults(fn=cmd_monitor)
    dn = sub.add_parser("dna", help="check (or --write missing) expression hashes of sealed genomes")
    dn.add_argument("--root", type=Path, default=Path("."))
    dn.add_argument("--write", action="store_true", help="store hashes for genomes that have none (never overwrites)")
    dn.add_argument("--migrate", metavar="REASON", default=None,
                    help="re-derive ALL stored hashes after a reviewed semantics change; logs old->new")
    dn.set_defaults(fn=cmd_dna)
    bp = sub.add_parser("bind-protocol", help="bind a named evaluation protocol (dry run without --execute)")
    target = bp.add_mutually_exclusive_group(required=True)
    target.add_argument("--family")
    target.add_argument("--id", help="one registered genome id")
    bp.add_argument("--protocol", required=True, help="e.g. received-raw-opening or published-opening")
    bp.add_argument("--execute", action="store_true", help="append the record (default: print it only)")
    bp.add_argument("--root", type=Path, default=Path("."))
    bp.set_defaults(fn=cmd_bind_protocol)
    v = sub.add_parser("verify", help="re-check the hash chain")
    v.add_argument("--root", type=Path, default=Path("."))
    v.set_defaults(fn=cmd_verify)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
