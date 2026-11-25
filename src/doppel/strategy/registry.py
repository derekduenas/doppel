"""Strategy registry: append-only, hash-chained preregistration plus a false-discovery ledger.

DOPPEL tests many genome variants. Without bookkeeping, the best of N noisy variants looks like an
edge. The registry makes the search itself part of the record:

* ``register`` seals a genome (its full dict and content-hash id) to a FAMILY (the multiple-testing
  unit) and a PREREGISTERED evaluation window that must start after the registration instant, with
  the code git SHA. Re-registering an id is a no-op: an id is charged once.
* ``record_result`` appends a verdict carrying a one-sided p-value for "net per window > 0". Only
  after the evaluation window has closed; the FIRST result per id is binding (later ones are kept
  and shown, never used).
* ``ledger(family)`` lists every registered variant and applies Holm-Bonferroni across the whole
  family, counting variants without a result as p = 1. A family's winner must survive that.
* ``verify`` re-checks the chain: each line carries ``prev`` = sha256 of the previous line's bytes
  and ``hash`` = sha256 of its own canonical JSON without ``hash``; every stored genome must still
  hash to its id. ``head`` is the last line's hash, to anchor externally (an attacker who rewrites
  the last line AND recomputes its hash is only caught against an anchored head).

File: ``<root>/registry/genomes.jsonl``. Lines are never edited or deleted.
"""

from __future__ import annotations

import fcntl
import hashlib
import math
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import orjson

from .genome import Genome, from_dict, to_dict, validate

REL_PATH = Path("registry") / "genomes.jsonl"
GENESIS = "0" * 64
ALPHA = 0.05

TimeLike = datetime | str | int | float


class RegistryError(ValueError):
    pass


# ---------------------------------------------------------------- helpers
def _canon(d: dict) -> bytes:
    return orjson.dumps(d, option=orjson.OPT_SORT_KEYS)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def to_epoch(x: TimeLike) -> float:
    """Timezone-aware datetime, ISO-8601 string with an offset or Z, or epoch seconds -> epoch s."""
    if isinstance(x, bool):
        raise RegistryError("a boolean is not a timestamp")
    if isinstance(x, (int, float)):
        if not math.isfinite(x):
            raise RegistryError("non-finite timestamp")
        return float(x)
    if isinstance(x, str):
        try:
            x = datetime.fromisoformat(x.strip())
        except ValueError as e:
            raise RegistryError(f"bad ISO timestamp {x!r}") from e
    if isinstance(x, datetime):
        if x.tzinfo is None or x.utcoffset() is None:
            raise RegistryError(f"naive timestamp {x.isoformat()}: give a timezone (e.g. Z)")
        return x.timestamp()
    raise RegistryError(f"unsupported timestamp {x!r}")


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat().replace("+00:00", "Z")


def git_sha(cwd: Path | None = None) -> dict:
    """{"sha": HEAD or None, "dirty": bool or None} for the repo holding this module."""
    cwd = cwd or Path(__file__).resolve().parent
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True,
                             timeout=10, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=cwd,
                               capture_output=True, text=True, timeout=30, check=True).stdout.strip() != ""
        return {"sha": sha or None, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"sha": None, "dirty": None}


def holm(pvals: list[float]) -> list[float]:
    """Holm-Bonferroni adjusted p-values, in input order: sort ascending, multiply the k-th
    smallest (1-based) by m - k + 1, take the running maximum, cap at 1. Ties keep input order."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: (pvals[i], i))
    adj = [0.0] * m
    run = 0.0
    for k, i in enumerate(order):
        run = max(run, min(1.0, (m - k) * pvals[i]))
        adj[i] = run
    return adj


# ---------------------------------------------------------------- registry
class Registry:
    def __init__(self, root: Path | str = ".", *, now: Callable[[], float] = time.time,
                 code: dict | None = None):
        self.root = Path(root)
        self.path = self.root / REL_PATH
        self._now = now
        self._code = code  # {"sha", "dirty"}; None -> ask git at registration time

    # -- reading
    def lines(self) -> list[bytes]:
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        return [ln for ln in data.split(b"\n") if ln]

    def records(self) -> list[dict]:
        return [orjson.loads(ln) for ln in self.lines()]

    def head(self) -> str:
        ls = self.lines()
        return orjson.loads(ls[-1])["hash"] if ls else GENESIS

    def registration(self, genome_id: str) -> dict | None:
        for r in self.records():
            if r["kind"] == "register" and r["genome_id"] == genome_id:
                return r
        return None

    # -- writing
    def _append(self, build: Callable[[list[dict]], dict | None]) -> dict:
        """Under an exclusive lock: read the chain, let ``build`` make the record (or return an
        existing one, signalled by a ``hash`` already present), append it with prev/hash."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a+b") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                data = fh.read()
                if data and not data.endswith(b"\n"):
                    raise RegistryError(f"{self.path} ends with a partial line: run verify")
                lines = [ln for ln in data.split(b"\n") if ln]
                recs = [orjson.loads(ln) for ln in lines]
                rec = build(recs)
                if rec is None:
                    raise RegistryError("nothing to append")
                if "hash" in rec:  # idempotent no-op: an existing record
                    return rec
                rec = {**rec, "seq": len(lines), "prev": _sha(lines[-1]) if lines else GENESIS}
                rec["hash"] = _sha(_canon(rec))
                fh.seek(0, 2)
                fh.write(_canon(rec) + b"\n")
                fh.flush()
                os.fsync(fh.fileno())
                return rec
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def register(self, genome: Genome | dict, family: str, hypothesis: str,
                 data_window: tuple[TimeLike, TimeLike]) -> dict:
        """Preregister ``genome`` in ``family`` with its evaluation window (start, end). The window
        must start after now. Returns the registration record (the existing one if the id is
        already registered: no-op)."""
        g = genome if isinstance(genome, Genome) else from_dict(genome)
        errs = validate(g)
        if errs:
            raise RegistryError("genome fails validate(): " + "; ".join(errs))
        if not family or not family.strip():
            raise RegistryError("family is required (it is the multiple-testing unit)")
        if not hypothesis or not hypothesis.strip():
            raise RegistryError("hypothesis is required")
        start, end = (to_epoch(x) for x in data_window)
        gid = g.id
        code = self._code if self._code is not None else git_sha()

        def build(recs: list[dict]) -> dict:
            for r in recs:
                if r["kind"] == "register" and r["genome_id"] == gid:
                    return r
            now = self._now()
            if start <= now:
                raise RegistryError(f"evaluation window starts {iso(start)}, not after now {iso(now)}: "
                                    "a preregistered window must be in the future")
            if end <= start:
                raise RegistryError("evaluation window end must be after its start")
            return {"kind": "register", "tw": int(now * 1e9), "genome_id": gid, "name": g.name,
                    "family": family.strip(), "hypothesis": hypothesis.strip(),
                    "eval_window": {"start": iso(start), "end": iso(end), "start_s": start, "end_s": end},
                    "code_sha": code.get("sha"), "code_dirty": code.get("dirty"),
                    "genome": orjson.loads(orjson.dumps(to_dict(g)))}
        return self._append(build)

    def record_result(self, genome_id: str, verdict: dict) -> dict:
        """Append a verdict for a registered genome. ``verdict["p_value"]`` is the one-sided p for
        H1: net per window > 0. Refused before the evaluation window has closed."""
        if not isinstance(verdict, dict) or "p_value" not in verdict:
            raise RegistryError("verdict must be a dict with p_value (one-sided, H1: net per window > 0)")
        p = verdict["p_value"]
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not (0.0 <= float(p) <= 1.0):
            raise RegistryError(f"p_value must be a number in [0, 1], got {p!r}")
        try:
            vd = orjson.loads(orjson.dumps(verdict))
        except (TypeError, orjson.JSONEncodeError) as e:
            raise RegistryError(f"verdict is not JSON-serialisable: {e}") from e

        def build(recs: list[dict]) -> dict:
            reg = next((r for r in recs if r["kind"] == "register" and r["genome_id"] == genome_id), None)
            if reg is None:
                raise RegistryError(f"genome {genome_id} is not registered")
            now = self._now()
            if now < reg["eval_window"]["end_s"]:
                raise RegistryError(f"evaluation window closes {reg['eval_window']['end']}: a result before "
                                    "then is not on the preregistered window")
            prior = sum(1 for r in recs if r["kind"] == "result" and r["genome_id"] == genome_id)
            return {"kind": "result", "tw": int(now * 1e9), "genome_id": genome_id,
                    "family": reg["family"], "binding": prior == 0, "verdict": vd}
        return self._append(build)

    # -- ledger
    def ledger(self, family: str, alpha: float = ALPHA) -> dict:
        """Every variant registered in ``family`` with its binding p-value, Holm-adjusted over all of
        them (pending = p 1), and the winner (smallest adjusted p <= alpha), if any."""
        recs = self.records()
        regs = [r for r in recs if r["kind"] == "register" and r["family"] == family]
        res: dict[str, list[dict]] = {}
        for r in recs:
            if r["kind"] == "result":
                res.setdefault(r["genome_id"], []).append(r)
        ps = [float(res[r["genome_id"]][0]["verdict"]["p_value"]) if r["genome_id"] in res else 1.0
              for r in regs]
        adj = holm(ps)
        rows = []
        for r, p, a in zip(regs, ps, adj):
            got = res.get(r["genome_id"], [])
            rows.append({"genome_id": r["genome_id"], "name": r["name"], "registered": iso(r["tw"] / 1e9),
                         "eval_window": [r["eval_window"]["start"], r["eval_window"]["end"]],
                         "status": "tested" if got else "pending",
                         "p_value": p if got else None, "p_holm": a,
                         "survives": bool(got) and a <= alpha, "results": len(got),
                         "verdict": got[0]["verdict"] if got else None})
        surv = sorted((x for x in rows if x["survives"]), key=lambda x: (x["p_holm"], x["p_value"]))
        return {"family": family, "alpha": alpha, "method": "holm", "m": len(rows),
                "tested": sum(x["status"] == "tested" for x in rows),
                "pending": sum(x["status"] == "pending" for x in rows),
                "rows": rows, "winner": surv[0]["genome_id"] if surv else None}

    # -- integrity
    def verify(self) -> list[str]:
        """Problems with the chain (empty list = intact)."""
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        errs = []
        if data and not data.endswith(b"\n"):
            errs.append("file ends with a partial line")
        prev = GENESIS
        regs: dict[str, dict] = {}
        parts = data.split(b"\n")
        for i, ln in enumerate(parts):
            if not ln:
                if i < len(parts) - 1:
                    errs.append(f"line {i}: empty line inside the chain")
                continue
            try:
                r = orjson.loads(ln)
            except orjson.JSONDecodeError:
                errs.append(f"line {i}: not JSON")
                prev = _sha(ln)
                continue
            body = {k: v for k, v in r.items() if k != "hash"}
            if r.get("hash") != _sha(_canon(body)):
                errs.append(f"line {i}: self-hash mismatch (edited)")
            if r.get("prev") != prev:
                errs.append(f"line {i}: prev does not match sha256 of line {i - 1} (chain broken)")
            if r.get("seq") != i:
                errs.append(f"line {i}: seq {r.get('seq')} != {i} (line removed or inserted)")
            kind, gid = r.get("kind"), str(r.get("genome_id"))
            if kind == "register":
                try:
                    got = from_dict(r["genome"]).id
                except (KeyError, TypeError, ValueError) as e:
                    errs.append(f"line {i}: stored genome does not load ({e})")
                    got = None
                if got is not None and got != gid:
                    errs.append(f"line {i}: stored genome hashes to {got[:12]}, not {gid[:12]}")
                if gid in regs:
                    errs.append(f"line {i}: genome {gid[:12]} registered twice")
                regs[gid] = r
                w = r.get("eval_window") or {}
                if not (isinstance(w.get("start_s"), (int, float)) and w["start_s"] * 1e9 > r.get("tw", 0)):
                    errs.append(f"line {i}: evaluation window does not start after registration")
            elif kind == "result":
                reg = regs.get(gid)
                if reg is None:
                    errs.append(f"line {i}: result for unregistered genome {gid[:12]}")
                elif r.get("tw", 0) < reg["eval_window"]["end_s"] * 1e9:
                    errs.append(f"line {i}: result recorded before the evaluation window closed")
            else:
                errs.append(f"line {i}: unknown kind {kind!r}")
            prev = _sha(ln)
        return errs

