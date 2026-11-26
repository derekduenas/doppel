"""The DNA layer: genotype -> expression -> phenotype -> fitness -> lineage.

A genome's id hashes its JSON (the GENOTYPE). That alone does not pin what the genome DOES: the
interpreter that turns a feature vector into quotes and takes lives in code, and a code change could
silently change a sealed genome's behaviour under the same id. This module closes that gap.

* Expression trace. ``expression_trace(g)`` runs the genome's interpreter (``maker_quotes``,
  ``maker_paused``, ``taker_actions``) over a FIXED, seeded probe set of feature vectors that spans
  the regimes the genomes condition on (time in window, fair near 0/0.5/1, calm and jumping BRTI,
  intl flow bursts both ways, inventory at the caps, placebo draws, wide and tight books). The
  sha256 of the decisions is the genome's EXPRESSION HASH. ``registry/expression.json`` stores one
  per sealed genome; a test fails if any sealed genome's expression ever changes. The same probe set
  is the golden spec a future Rust interpreter must reproduce bit for bit.
* Semantics digest. ``semantics_digest()`` hashes the feature vocabulary definitions, the fair model
  name and the probe-set version, so the stored file also records what the hashes meant.
* Lineage. ``mutate`` and ``crossover`` build children with ``source.parents`` / ``source.operator``
  set, so every genome has an auditable ancestry and siblings are charged to the same family.
* Phenotype. ``phenotype(verdict)`` reduces a twin verdict to a fixed, comparable behaviour vector
  (how the genome trades, not only what it earned). Live-vs-twin phenotype drift is the decay signal
  that triggers a swap; the vector also defines behavioural niches for quality-diversity search.

What this does NOT pin (stated, not hidden): feature computation from the tape and the twin's fill
model. Those are pinned by the twin's own tests and the registry's code_sha; a golden replay of a
committed tape slice is the next step.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import random
from pathlib import Path
from typing import Any

import orjson

from .genome import FEATURES, FairModel, Genome, from_dict, to_dict, validate

PROBE_VERSION = 3   # v2: placebo_block_u; v3: missing values (NaN) like real feeds (2026-11-26)
PROBE_SEED = 20261126
PROBE_N = 4000
EXPRESSION_FILE = "registry/expression.json"


def semantics_digest() -> str:
    body = {"features": FEATURES, "fair": dataclasses.asdict(FairModel()), "probe_version": PROBE_VERSION,
            "probe_seed": PROBE_SEED, "probe_n": PROBE_N}
    return hashlib.sha256(orjson.dumps(body, option=orjson.OPT_SORT_KEYS)).hexdigest()


def probes(n: int = PROBE_N, seed: int = PROBE_SEED) -> list[dict[str, float]]:
    """Deterministic feature vectors covering every FEATURES key and the regimes rules condition on."""
    rng = random.Random(seed)
    out = []
    for i in range(n):
        hz = 300 if i % 3 else 900
        tau = float(rng.choice([0, 3, 5, 6, 30, 120, 200, 240, 280]) if hz == 300 else rng.randrange(0, 900))
        tte = float(hz - tau)
        fair = rng.choice([0.01, 0.03, 0.1, 0.3, 0.48, 0.5, 0.52, 0.7, 0.9, 0.97, 0.99, rng.random()])
        mid = min(0.99, max(0.01, fair + rng.choice([-0.06, -0.02, 0.0, 0.01, 0.03, 0.08])))
        spread = rng.choice([0.01, 0.02, 0.04, 0.1])
        jump = rng.choice([0.0, 0.0, 5.0, -5.0, 31.0, -31.0, 80.0, -80.0, rng.gauss(0, 20)])
        flow = rng.choice([0.0, 50.0, -50.0, 120.0, -120.0, 350.0, -350.0, 2000.0, -2000.0, rng.gauss(0, 300)])
        inv = float(rng.choice([0, 0, 5, -5, 20, -20, 39, -39, 40, -40, 45, -45]))
        ask_up = min(0.99, mid + spread / 2)
        bid_up = max(0.01, mid - spread / 2)
        x = {
            "tau_s": tau, "tte_s": tte, "fair_up": fair,
            "edge_buy_up_c": 100 * (fair - ask_up) - 0.0695 * 100 * ask_up * (1 - ask_up),
            "edge_buy_down_c": 100 * ((1 - fair) - (1 - bid_up)) - 0.0695 * 100 * bid_up * (1 - bid_up),
            "mid_up_c": 100 * mid, "spread_c": 100 * spread,
            "depth_top_up": float(rng.choice([0, 10, 300, 1000])), "depth_top_down": float(rng.choice([0, 10, 300, 1000])),
            "brti_move_1s": jump / 5, "brti_move_5s": jump, "brti_move_30s": jump * 2.5,
            "dist_to_open_usd": rng.gauss(0, 60), "inventory_up": inv, "placebo_u": rng.random(),
            "placebo_block_u": rng.random(),
        }
        for k in (5, 30, 120):
            f = flow * k / 30
            x[f"intl_flow_up_usd_{k}s"] = f * rng.choice([1.0, 5.0, 15.0])
            x[f"intl_informed_flow_{k}s"] = f
            x[f"intl_informed_share_{k}s"] = rng.random()
        x["intl_mid_up_c"] = 100 * min(0.99, max(0.01, fair + rng.gauss(0, 0.03)))
        # real feature vectors have gaps (no book, no fair off-BTC, no intl rows): pin NaN behaviour too
        for k in list(x):
            if k not in ("tau_s", "tte_s", "inventory_up", "placebo_u", "placebo_block_u") and rng.random() < 0.04:
                x[k] = float("nan")
        missing = set(FEATURES) - set(x)
        if missing:  # a new feature must be added to the probe set (and PROBE_VERSION bumped)
            raise AssertionError(f"probe set lacks features {sorted(missing)}")
        out.append(x)
    return out


def expression_trace(g: Genome, xs: list[dict[str, float]] | None = None) -> dict:
    """Decisions of ``g`` on the probe set and their sha256 (the expression hash)."""
    xs = probes() if xs is None else xs
    rows = []
    stats = {"quote_both": 0, "quote_one": 0, "paused": 0, "takes": 0}
    for x in xs:
        if g.mode == "maker" and g.maker is not None:
            paused = g.maker_paused(x)
            q = g.maker_quotes(x)
            rows.append((1 if paused else 0, q[0], q[1]))
            stats["paused"] += paused
            n = sum(v is not None for v in q)
            stats["quote_both"] += n == 2
            stats["quote_one"] += n == 1
        else:
            acts = g.taker_actions(x)
            sig = [(r.side, round(r.size, 6), round(r.limit_slippage_c, 6)) for r in acts]
            rows.append(tuple(sig))
            stats["takes"] += bool(acts)
    h = hashlib.sha256(orjson.dumps(rows)).hexdigest()
    return {"expression_sha256": h, "n": len(xs), "stats": stats}


# ---------------------------------------------------------------- golden store
def load_store(root: Path) -> dict:
    p = Path(root) / EXPRESSION_FILE
    return orjson.loads(p.read_bytes()) if p.exists() else {"semantics": None, "genomes": {}}


def registered_genomes(root: Path) -> dict[str, Genome]:
    out: dict[str, Genome] = {}
    p = Path(root) / "registry" / "genomes.jsonl"
    for line in p.read_bytes().splitlines():
        if not line.strip():
            continue
        r = orjson.loads(line)
        if r.get("kind") == "register":
            out[r["genome_id"]] = from_dict(r["genome"])
    return out


def check(root: Path) -> list[str]:
    """Problems: a sealed genome whose id or expression changed, or one with no stored expression."""
    store = load_store(root)
    probs = []
    xs = probes()
    for gid, g in registered_genomes(root).items():
        if g.id != gid:
            probs.append(f"{gid[:12]}: genome JSON re-hashes to {g.id[:12]} (canonicalisation changed)")
            continue
        want = store["genomes"].get(gid)
        if want is None:
            probs.append(f"{gid[:12]}: no stored expression hash (run doppel-genome dna --write)")
            continue
        got = expression_trace(g, xs)["expression_sha256"]
        if got != want["expression_sha256"]:
            probs.append(f"{gid[:12]} {g.name}: EXPRESSION CHANGED {want['expression_sha256'][:12]} -> {got[:12]}")
    return probs


def write_missing(root: Path) -> list[str]:
    """Store expression hashes for registered genomes that have none. Never overwrites one."""
    store = load_store(root)
    xs = probes()
    added = []
    for gid, g in registered_genomes(root).items():
        if gid in store["genomes"]:
            continue
        tr = expression_trace(g, xs)
        store["genomes"][gid] = {"name": g.name, **tr}
        added.append(gid)
    if store.get("semantics") is None:
        store["semantics"] = {"digest": semantics_digest(), "probe_version": PROBE_VERSION}
    p = Path(root) / EXPRESSION_FILE
    p.write_bytes(orjson.dumps(store, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n")
    return added


def migrate(root: Path, reason: str) -> list[dict]:
    """Explicit, reviewed re-derivation of every stored expression hash (GENOME.md rule 4). Logs
    old -> new per genome with the reason; never silent. Returns the changes."""
    store = load_store(root)
    xs = probes()
    changes = []
    for gid, g in registered_genomes(root).items():
        new = expression_trace(g, xs)
        old = store["genomes"].get(gid, {}).get("expression_sha256")
        if old != new["expression_sha256"]:
            changes.append({"genome_id": gid, "name": g.name, "old": old, "new": new["expression_sha256"]})
        store["genomes"][gid] = {"name": g.name, **new}
    store.setdefault("migrations", []).append({
        "reason": reason, "probe_version": PROBE_VERSION, "semantics": semantics_digest(),
        "changed": [{k: c[k] for k in ("genome_id", "old", "new")} for c in changes]})
    store["semantics"] = {"digest": semantics_digest(), "probe_version": PROBE_VERSION}
    (Path(root) / EXPRESSION_FILE).write_bytes(
        orjson.dumps(store, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n")
    return changes


# ---------------------------------------------------------------- lineage operators
def _set_path(d: dict, path: str, value: Any) -> None:
    keys = path.split(".")
    cur = d
    for k in keys[:-1]:
        cur = cur[k]
    if keys[-1] not in cur:
        raise KeyError(f"no field {path}")
    cur[keys[-1]] = value


def mutate(g: Genome, path: str, value: Any, name: str | None = None) -> Genome:
    """Child genome with one field changed (e.g. ``maker.half_spread_c``), lineage recorded."""
    d = to_dict(g)
    _set_path(d, path, value)
    d["source"] = {**d["source"], "parents": [g.id], "operator": f"mutate:{path}"}
    d["name"] = name or f"{g.name}~{path.split('.')[-1]}={value}"
    d["status"] = "candidate"
    child = from_dict(d)
    errs = validate(child)
    if errs:
        raise ValueError(f"mutant invalid: {errs}")
    return child


def crossover(a: Genome, b: Genome, take_from_b: tuple[str, ...], name: str | None = None) -> Genome:
    """Child of ``a`` with the top-level sections in ``take_from_b`` (e.g. ("risk",), ("maker",))
    copied from ``b``. Both modes must match."""
    if a.mode != b.mode:
        raise ValueError("crossover needs parents of the same mode")
    da, db = to_dict(a), to_dict(b)
    for sec in take_from_b:
        da[sec] = db[sec]
    da["source"] = {**da["source"], "parents": sorted([a.id, b.id]), "operator": "crossover:" + ",".join(take_from_b)}
    da["name"] = name or f"{a.name}x{b.name}"
    child = from_dict(da)
    errs = validate(child)
    if errs:
        raise ValueError(f"crossover invalid: {errs}")
    return child


# ---------------------------------------------------------------- phenotype
PHENOTYPE_KEYS = ("net_c_per_window", "net_c_per_contract", "contracts_per_window", "traded_window_share",
                  "maker_fill_rate", "adverse_selection_c", "markout_1s_c", "markout_30s_c",
                  "stood_down_share", "share_of_venue_volume", "max_drawdown_usd")


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def phenotype(v: dict) -> dict[str, float | None]:
    """Fixed behaviour vector from a ``twin.verdict`` output (None where the verdict lacks it)."""
    n = v.get("windows_evaluated") or 0
    mk = v.get("markout_c", {}).get("maker", {}) or {}
    tk = v.get("markout_c", {}).get("taker", {}) or {}
    mo = mk if any(_f(x) is not None for x in mk.values()) else tk
    return {
        "net_c_per_window": _f((v.get("net_c_per_window") or {}).get("mean")),
        "net_c_per_contract": _f((v.get("net_c_per_contract") or {}).get("value")),
        "contracts_per_window": _f(v.get("contracts", 0) / n) if n else None,
        "traded_window_share": _f(v.get("windows_traded", 0) / n) if n else None,
        "maker_fill_rate": _f((v.get("maker") or {}).get("fill_rate")),
        # adverse selection as the twin measures it: maker 30 s markout minus the quoted edge is not in
        # the verdict summary, so the short markout stands in (negative = filled before adverse moves)
        "adverse_selection_c": _f(mo.get("5")),
        "markout_1s_c": _f(mo.get("1")),
        "markout_30s_c": _f(mo.get("30")),
        "stood_down_share": _f(v.get("windows_stood_down", 0) / n) if n else None,
        "share_of_venue_volume": _f((v.get("capacity") or {}).get("share_of_venue_volume")),
        "max_drawdown_usd": _f(v.get("max_drawdown_usd")),
    }


def phenotype_drift(twin: dict, live: dict, keys: tuple[str, ...] = PHENOTYPE_KEYS) -> dict:
    """Per-key live minus twin, and the keys where both exist. The decay monitor reads this."""
    out = {}
    for k in keys:
        a, b = twin.get(k), live.get(k)
        out[k] = None if a is None or b is None else round(b - a, 6)
    return out
