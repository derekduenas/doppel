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
* ``monitor`` / ``record_monitor`` (registry v2, ``sequential.py``): anytime-valid e-processes over
  the twin's per-window net P&L inside the evaluation window, checked at any time after the window
  opens. Appends ``interim``, ``early_pass`` or ``early_kill`` events (the first early event per id is
  binding). An early pass needs family e-Holm across every registered variant. These events are
  statistical only: the commercial hurdle and owner authorization stay separate, and the
  end-of-window ``result`` path is unchanged. ``sequential_ledger(family)`` summarises them.
* ``bind_protocol`` (kind ``protocol``) binds a named evaluation protocol (name, version, parameters
  and their digest, evaluator code hash) to a registered genome or to every genome registered in a
  family so far, listed explicitly. Only before the earliest covered evaluation window closes; one
  binding per genome (re-binding the same target to the same protocol is a no-op). A binding result
  or early event for a bound genome must come from that exact protocol.
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
from typing import get_args
from pathlib import Path

import orjson

from . import sequential as seq
from .genome import Genome, Status, from_dict, to_dict, validate

REL_PATH = Path("registry") / "genomes.jsonl"
GENESIS = "0" * 64
ALPHA = 0.05
SEQ_KINDS = ("interim", "early_pass", "early_kill")
STATISTICAL_ONLY = ("statistical evidence only: the commercial hurdle (A6) and owner authorization are "
                    "separate decisions")

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
        # the registry's own append is not a code change (audit A4-F3: it flagged 11/20 genomes dirty)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no", "--", ":(top)",
                                ":(top,exclude)registry"], cwd=cwd,
                               capture_output=True, text=True, timeout=30, check=True).stdout.strip() != ""
        return {"sha": sha or None, "dirty": dirty}
    except (OSError, subprocess.SubprocessError):
        return {"sha": None, "dirty": None}


def protocol_digest(params: dict) -> str:
    """sha256 of an evaluation protocol's parameters (canonical JSON)."""
    return _sha(_canon(params))


PROTOCOL_KEYS = ("name", "version", "params_sha256", "code_sha256")


def protocol_matches(bound: dict, evaluation: dict | None) -> bool:
    """Did an evaluation (``replay_snapshot`` evidence) run exactly the bound protocol?"""
    if not isinstance(evaluation, dict):
        return False
    ran = {**(evaluation.get("protocol") or {}), "code_sha256": evaluation.get("code_sha256")}
    return all(ran.get(k) == bound.get(k) for k in PROTOCOL_KEYS)


def _protocol_problems(protocol) -> list[str]:
    if not isinstance(protocol, dict):
        return ["protocol is not an object"]
    errs = []
    if not isinstance(protocol.get("name"), str) or not protocol["name"]:
        errs.append("protocol name missing")
    if type(protocol.get("version")) is not int:
        errs.append("protocol version must be an integer")
    for k in ("params_sha256", "code_sha256"):
        v = protocol.get(k)
        if not (isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)):
            errs.append(f"protocol {k} must be a sha256 hex digest")
    params = protocol.get("params")
    if not isinstance(params, dict):
        errs.append("protocol params missing")
    elif protocol_digest(params) != protocol.get("params_sha256"):
        errs.append("protocol params do not hash to params_sha256")
    return errs


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
                 data_window: tuple[TimeLike, TimeLike], *, evaluation_protocol: dict | None = None) -> dict:
        """Preregister ``genome`` in ``family`` with its evaluation window (start, end). The window
        must start after now. Returns the registration record (the existing one if the id is
        already registered: no-op)."""
        g = genome if isinstance(genome, Genome) else from_dict(genome)
        st = getattr(g, "status", None)
        if st not in get_args(Status):
            # e.g. rulefit's "draft": status is outside the id, so a draft would seal silently and the
            # registry is append-only. Promote it to a real Status deliberately before registering.
            raise RegistryError(f"genome status {st!r} cannot be registered (allowed: {', '.join(get_args(Status))})")
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
                    "genome": orjson.loads(orjson.dumps(to_dict(g))),
                    **({"evaluation_protocol": evaluation_protocol} if evaluation_protocol is not None else {})}
        return self._append(build)

    def record_result(self, genome_id: str, verdict: dict, data_range: tuple[int, int] | None = None) -> dict:
        """Append a verdict for a registered genome. ``verdict["p_value"]`` is the one-sided p for
        H1: net per window > 0. Refused before the evaluation window has closed.

        Binding (Astra audit 2026-11-26): a twin verdict counts only if ``data_range`` is exactly the
        preregistered [start, end) window; anything else is refused. Manual and imported verdicts
        are recorded as nonbinding."""
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
            manual = vd.get("p_source") == "manual"
            nonbinding = manual or vd.get("p_source") == "imported"
            protocol = reg.get("evaluation_protocol")
            if protocol is not None and not nonbinding:
                actual = vd.get("evaluation")
                if (vd.get("p_source") != "twin-replay" or not isinstance(actual, dict)
                        or any(actual.get(k) != v for k, v in protocol.items())):
                    raise RegistryError("result does not match the preregistered evaluation protocol")
            bound = _binding(recs, genome_id)
            if bound is not None and not nonbinding and not protocol_matches(bound, vd.get("evaluation")):
                raise RegistryError(f"genome is bound to evaluation protocol {bound['name']} v{bound['version']}: "
                                    "this result was not produced by it")
            if not manual:
                w = reg["eval_window"]
                if data_range != (w["start_s"], w["end_s"]):
                    raise RegistryError(f"twin results cover {data_range}, not the preregistered window "
                                        f"[{w['start_s']}, {w['end_s']}) ({w['start']} .. {w['end']})")
            prior = sum(1 for r in recs if r["kind"] == "result" and r["genome_id"] == genome_id
                        and r.get("binding"))
            return {"kind": "result", "tw": int(now * 1e9), "genome_id": genome_id,
                    "family": reg["family"], "binding": (not nonbinding) and prior == 0,
                    "protocol_unbound": bound is None,
                    "data_range": list(data_range) if data_range else None, "verdict": vd}
        return self._append(build)

    # -- evaluation protocol binding
    def protocol_binding(self, genome_id: str) -> dict | None:
        """The protocol bound to ``genome_id`` (None = unbound)."""
        return _binding(self.records(), genome_id)

    def plan_protocol_binding(self, protocol: dict, *, family: str | None = None,
                              genome_id: str | None = None) -> dict:
        """The ``protocol`` record ``bind_protocol`` would append now (without seq/prev/hash), or
        the existing identical record (it carries ``hash``). Writes nothing."""
        return self._protocol_record(self.records(), protocol, family, genome_id)

    def bind_protocol(self, protocol: dict, *, family: str | None = None,
                      genome_id: str | None = None) -> dict:
        """Append a ``protocol`` record binding ``protocol`` to a family or one genome."""
        return self._append(lambda recs: self._protocol_record(recs, protocol, family, genome_id))

    def _protocol_record(self, recs: list[dict], protocol: dict, family: str | None,
                         genome_id: str | None) -> dict:
        if (family is None) == (genome_id is None):
            raise RegistryError("bind exactly one of a family or a genome id")
        errs = _protocol_problems(protocol)
        if errs:
            raise RegistryError("; ".join(errs))
        protocol = orjson.loads(orjson.dumps({k: protocol[k] for k in (*PROTOCOL_KEYS, "params")}))
        regs = [r for r in recs if r["kind"] == "register"
                and (r["family"] == family if family is not None else r["genome_id"] == genome_id)]
        if not regs:
            raise RegistryError(f"no registered genome for {'family ' + str(family) if family else genome_id}")
        target = {"family": family} if family is not None else {"genome_id": genome_id}
        ids = [r["genome_id"] for r in regs]
        for r in recs:
            if (r["kind"] == "protocol" and r["target"] == target and r["protocol"] == protocol
                    and r["genome_ids"] == ids):
                return r    # idempotent: this exact binding already exists
        taken = [g for g in ids if _binding(recs, g) is not None]
        if taken:
            raise RegistryError(f"already bound to an evaluation protocol: {', '.join(g[:12] for g in taken)} "
                                "(a binding is never replaced)")
        evaluated = sorted({r["genome_id"][:12] for r in recs
                            if r["kind"] in ("result", *SEQ_KINDS) and r["genome_id"] in ids})
        if evaluated:
            raise RegistryError(f"already evaluated under an unbound protocol: {', '.join(evaluated)} "
                                "(bind before any result or monitoring event)")
        now = self._now()
        closes = min(r["eval_window"]["end_s"] for r in regs)
        if now >= closes:
            raise RegistryError(f"evaluation window closed {iso(closes)}: a protocol binds only before then")
        return {"kind": "protocol", "tw": int(now * 1e9), "target": target, "family": regs[0]["family"],
                "genome_ids": ids, "closes_s": closes, "protocol": protocol}

    # -- ledger
    def ledger(self, family: str, alpha: float = ALPHA) -> dict:
        """Every variant registered in ``family`` with its binding p-value, Holm-adjusted over all of
        them (pending = p 1), and the winner (smallest adjusted p <= alpha), if any."""
        recs = self.records()
        regs = [r for r in recs if r["kind"] == "register" and r["family"] == family]
        res: dict[str, list[dict]] = {}
        for r in recs:   # binding result first; later re-runs are counted but never replace it
            if r["kind"] == "result":
                res.setdefault(r["genome_id"], []).append(r)
        for v in res.values():
            v.sort(key=lambda r: not r.get("binding"))
        def binding(gid: str) -> dict | None:
            got = res.get(gid) or []
            return got[0] if got and got[0].get("binding") else None
        ps = [float(binding(r["genome_id"])["verdict"]["p_value"]) if binding(r["genome_id"]) else 1.0  # type: ignore[index]
              for r in regs]
        adj = holm(ps)
        rows = []
        for r, p, a in zip(regs, ps, adj):
            got = res.get(r["genome_id"], [])
            rows.append({"genome_id": r["genome_id"], "name": r["name"], "registered": iso(r["tw"] / 1e9),
                         "eval_window": [r["eval_window"]["start"], r["eval_window"]["end"]],
                         "status": "tested" if binding(r["genome_id"]) else "pending",
                         "p_value": p if binding(r["genome_id"]) else None, "p_holm": a,
                         # a KILL (or any non-PASS decision) can never be the winner, whatever its p
                         "survives": bool(binding(r["genome_id"])) and a <= alpha
                         and ((binding(r["genome_id"]) or {}).get("verdict") or {}).get("decision") == "PASS",
                         "results": len(got),
                         "verdict": (binding(r["genome_id"]) or {}).get("verdict")})
        surv = sorted((x for x in rows if x["survives"]), key=lambda x: (x["p_holm"], x["p_value"]))
        return {"family": family, "alpha": alpha, "method": "holm", "m": len(rows),
                "tested": sum(x["status"] == "tested" for x in rows),
                "pending": sum(x["status"] == "pending" for x in rows),
                "rows": rows, "winner": surv[0]["genome_id"] if surv else None}

    # -- sequential monitoring (registry v2)
    def monitor(self, genome_id: str, results: dict, alpha: float = seq.ALPHA,
                source: dict | None = None) -> dict:
        """E-values and the early decision for ``genome_id`` from a ``doppel-twin`` results document,
        as of now. Writes nothing."""
        return self._monitor(self.records(), genome_id, results, alpha, self._now(), source)

    def record_monitor(self, genome_id: str, results: dict, alpha: float = seq.ALPHA,
                       source: dict | None = None) -> dict:
        """Append the monitoring event (``early_pass`` / ``early_kill`` if decided, else ``interim``).
        Refused before the evaluation window opens, without a complete block, on a diagnostic twin
        run, if the parameters differ from this genome's (or this family's) first event, or if
        ``as_of`` moves backwards."""
        def build(recs: list[dict]) -> dict:
            now = self._now()
            mine = [r for r in recs if r["kind"] in SEQ_KINDS and r["genome_id"] == genome_id]
            m = self._monitor(recs, genome_id, results, alpha, now, source, previous=mine[-1] if mine else None)
            if (results.get("twin") or {}).get("diagnostic"):
                raise RegistryError("twin run is DIAGNOSTIC (relaxed data gates): not evidence, not recorded")
            bound = _binding(recs, genome_id)
            if bound is not None and not protocol_matches(bound, (source or {}).get("evaluation")):
                raise RegistryError(f"genome is bound to evaluation protocol {bound['name']} v{bound['version']}: "
                                    "this monitoring run was not produced by it")
            if m["stats"]["n_blocks"] < 1:
                raise RegistryError(f"no complete block of {m['params']['block']} windows inside the "
                                    "evaluation window yet")
            fam = [r for r in recs if r["kind"] in SEQ_KINDS and r["family"] == m["family"]]
            registration = next(r for r in recs if r["kind"] == "register" and r["genome_id"] == genome_id)
            policy = registration.get("evaluation_protocol") or {}
            fixed_alpha = policy.get("sequential_alpha", fam[0]["params"]["alpha"] if fam else seq.ALPHA)
            if alpha != fixed_alpha:
                raise RegistryError(f"recorded sequential alpha is frozen at {fixed_alpha}")
            if mine and mine[0]["params"] != m["params"]:
                raise RegistryError(f"monitoring parameters {m['params']} differ from this genome's first "
                                    f"event {mine[0]['params']}: parameters are frozen")
            frozen = ("method", "block", "alpha", "lambda_cap", "prior_var")
            if fam and any(fam[0]["params"][k] != m["params"][k] for k in frozen):
                first = {k: fam[0]["params"][k] for k in frozen}
                raise RegistryError(f"family {m['family']} is monitored with {first}: parameters are frozen")
            kind = {"EARLY_PASS": "early_pass", "EARLY_KILL": "early_kill"}.get(m["decision"], "interim")
            prior_early = any(r["kind"] != "interim" for r in mine)
            return {"kind": kind, "tw": int(now * 1e9), "genome_id": genome_id, "family": m["family"],
                    "binding": kind != "interim" and not prior_early, "protocol_unbound": bound is None,
                    "eval_window": m["eval_window"], "window_closed": m["window_closed"], "as_of": m["as_of"], "as_of_s": m["as_of_s"], "params": m["params"], "stats": m["stats"],
                    "family_holm": m["family_holm"], "decision": m["decision"], "reason": m["reason"],
                    "source": m["source"], "note": STATISTICAL_ONLY}
        return self._append(build)

    def _monitor(self, recs: list[dict], genome_id: str, results: dict, alpha: float, now: float,
                 source: dict | None, *, previous: dict | None = None) -> dict:
        if not 0.0 < alpha < 1.0:
            raise RegistryError("alpha must be in (0, 1)")
        reg = next((r for r in recs if r["kind"] == "register" and r["genome_id"] == genome_id), None)
        if reg is None:
            raise RegistryError(f"genome {genome_id} is not registered")
        got = (results.get("genome") or {}).get("id")
        if got != genome_id:
            raise RegistryError(f"results are for genome {str(got)[:12]}, not {genome_id[:12]}")
        w = reg["eval_window"]
        if now < w["start_s"]:
            raise RegistryError(f"evaluation window opens {w['start']}: nothing to monitor before then")
        g = from_dict(reg["genome"])
        prm = seq.params(g, alpha)
        rows = seq.eval_windows(results.get("windows") or [], w["start_s"], w["end_s"], now)
        used = rows[: len(rows) // prm["block"] * prm["block"]]
        as_of_s = float(used[-1]["end"]) if used else float(w["start_s"])
        # A sequential test can extend its observed prefix. A feed revision that changes a
        # completed block (including moving a previously excluded window into it) is a new sample.
        observations = [(r["start"], r["end"], r.get("slug"), r["net_c"], r.get("contracts") or 0)
                        for r in used]
        if previous is not None:
            if as_of_s < previous["as_of_s"]:
                raise RegistryError(f"as_of {iso(as_of_s)} is before the last recorded {previous['as_of']}")
            prior = previous["stats"]
            if "observations_sha256" in prior:
                prefix = observations[:prior["windows_used"]]
                if _sha(orjson.dumps(prefix)) != prior["observations_sha256"]:
                    raise RegistryError("previously recorded window observations changed")
        stats = seq.e_processes([float(r["net_c"]) for r in used], prm["bound_c"], prm["block"], alpha)
        n_traded = sum(1 for r in used if (r.get("contracts") or 0) > 0)
        stats.update({"windows_seen": len(rows), "windows_traded": n_traded,
                      "observations_sha256": _sha(orjson.dumps(observations))})
        fam = self._family_e(recs, reg["family"], {genome_id: stats["pass"]["e_max"]})
        p_holm = seq.e_holm([x["e_max"] for x in fam])
        me = [x["genome_id"] for x in fam].index(genome_id)
        decision, reason = seq.decide(stats, p_holm[me], n_traded, g.kill.min_windows, alpha)
        twin = results.get("twin") or {}
        src = {"twin_code_sha256": twin.get("code_sha256"), "twin_diagnostic": bool(twin.get("diagnostic")),
               "twin_config": twin.get("config"), "flow_delay_s": twin.get("flow_delay_s"),
               "range": results.get("range"), **(source or {})}
        return {"genome_id": genome_id, "name": reg["name"], "family": reg["family"],
                "eval_window": [w["start"], w["end"]], "window_closed": now >= w["end_s"],
                "as_of": iso(as_of_s), "as_of_s": as_of_s, "params": prm, "stats": stats,
                "family_holm": {"m": len(fam), "alpha": alpha, "p_anytime": seq.anytime_p(fam[me]["e_max"]),
                                "p_holm": p_holm[me],
                                "members": [{**x, "p_holm": p} for x, p in zip(fam, p_holm)]},
                "decision": decision, "reason": reason, "source": src}

    @staticmethod
    def _family_e(recs: list[dict], family: str, override: dict[str, float] | None = None) -> list[dict]:
        """Every genome registered in ``family`` with its pass e-value running max: the binding early
        pass if any (locked in), else ``override``, else the latest event, else 1 (pending)."""
        out = []
        for r in recs:
            if r["kind"] != "register" or r["family"] != family:
                continue
            gid = r["genome_id"]
            ev = [x for x in recs if x["kind"] in SEQ_KINDS and x["genome_id"] == gid]
            lock = next((x for x in ev if x["kind"] == "early_pass" and x.get("binding")), None)
            if lock is not None:
                e = float(lock["stats"]["pass"]["e_max"])
            elif override and gid in override:
                e = float(override[gid])
            else:
                e = float(ev[-1]["stats"]["pass"]["e_max"]) if ev else 1.0
            out.append({"genome_id": gid, "name": r["name"], "e_max": e})
        return out

    def sequential_ledger(self, family: str, alpha: float = seq.ALPHA) -> dict:
        """Per variant in ``family``: monitoring status (pending / monitoring / early_pass /
        early_kill, from the binding early event), the latest e-values, and family e-Holm."""
        recs = self.records()
        fam = self._family_e(recs, family)
        adj = seq.e_holm([x["e_max"] for x in fam])
        rows = []
        for x, p in zip(fam, adj):
            ev = [r for r in recs if r["kind"] in SEQ_KINDS and r["genome_id"] == x["genome_id"]]
            early = next((r for r in ev if r["kind"] != "interim" and r.get("binding")), None)
            last = ev[-1] if ev else None
            rows.append({"genome_id": x["genome_id"], "name": x["name"],
                         "status": early["kind"] if early else ("monitoring" if ev else "pending"),
                         "events": len(ev), "as_of": last["as_of"] if last else None,
                         "n_blocks": last["stats"]["n_blocks"] if last else 0,
                         "e_pass_max": x["e_max"], "e_kill_max": last["stats"]["kill"]["e_max"] if last else 1.0,
                         "p_anytime": seq.anytime_p(x["e_max"]), "p_holm": p,
                         "survives": p <= alpha, "decided_at": early["as_of"] if early else None})
        return {"family": family, "alpha": alpha, "method": "e-holm (anytime p = 1 / running max e)",
                "m": len(rows), "rows": rows}

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
        seen_early: set[str] = set()
        bound: set[str] = set()
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
            elif kind in SEQ_KINDS:
                errs.extend(_verify_seq(i, r, regs.get(gid), seen_early))
            elif kind == "protocol":
                errs.extend(_verify_protocol(i, r, regs, bound))
            else:
                errs.append(f"line {i}: unknown kind {kind!r}")
            prev = _sha(ln)
        return errs



def _binding(recs: list[dict], genome_id: str) -> dict | None:
    for r in recs:
        if r["kind"] == "protocol" and genome_id in r["genome_ids"]:
            return r["protocol"]
    return None


def _verify_protocol(i: int, r: dict, regs: dict[str, dict], bound: set[str]) -> list[str]:
    """Checks for one protocol binding line (updates ``bound``)."""
    errs = [f"line {i}: {e}" for e in _protocol_problems(r.get("protocol"))]
    ids, target = r.get("genome_ids"), r.get("target") or {}
    if not isinstance(ids, list) or not ids:
        return [*errs, f"line {i}: protocol binding covers no genome"]
    covered = [regs.get(str(g)) for g in ids]
    if any(c is None for c in covered):
        return [*errs, f"line {i}: protocol binding for a genome not registered before it"]
    if "family" in target:
        expected = [g for g, reg in regs.items() if reg["family"] == target["family"]]
    else:
        expected = [target.get("genome_id")]
    if ids != expected:
        errs.append(f"line {i}: protocol binding genomes differ from its target {target}")
    if any(r.get("tw", 0) >= c["eval_window"]["end_s"] * 1e9 for c in covered if c is not None):
        errs.append(f"line {i}: protocol bound after an evaluation window closed")
    if any(str(g) in bound for g in ids):
        errs.append(f"line {i}: genome bound to an evaluation protocol twice")
    bound.update(map(str, ids))
    return errs


def _verify_seq(i: int, r: dict, reg: dict | None, seen_early: set[str]) -> list[str]:
    """Checks for one interim / early_pass / early_kill line (updates ``seen_early``)."""
    gid = str(r.get("genome_id"))
    if reg is None:
        return [f"line {i}: {r.get('kind')} for unregistered genome {gid[:12]}"]
    errs = []
    w = reg["eval_window"]
    tw, as_of = r.get("tw", 0), r.get("as_of_s")
    if tw < w["start_s"] * 1e9:
        errs.append(f"line {i}: {r['kind']} recorded before the evaluation window opened")
    if not isinstance(as_of, (int, float)) or as_of > w["end_s"] or as_of * 1e9 > tw:
        errs.append(f"line {i}: as_of outside the evaluation window or after the record time")
    try:
        alpha = float(r["params"]["alpha"])
        e_pass, e_kill = float(r["stats"]["pass"]["e_max"]), float(r["stats"]["kill"]["e_max"])
        p_holm = float(r["family_holm"]["p_holm"])
    except (KeyError, TypeError, ValueError):
        return [*errs, f"line {i}: {r['kind']} lacks params/stats/family_holm"]
    if r["kind"] == "early_pass" and not (e_pass >= 1.0 / alpha and p_holm <= alpha):
        errs.append(f"line {i}: early_pass without pass e-value >= 1/alpha and family e-Holm p <= alpha")
    if r["kind"] == "early_kill" and not e_kill >= 1.0 / alpha:
        errs.append(f"line {i}: early_kill without kill e-value >= 1/alpha")
    if r["kind"] != "interim":
        if bool(r.get("binding")) == (gid in seen_early):
            errs.append(f"line {i}: binding flag wrong (only the first early event per genome binds)")
        seen_early.add(gid)
    elif r.get("binding"):
        errs.append(f"line {i}: an interim is never binding")
    return errs
