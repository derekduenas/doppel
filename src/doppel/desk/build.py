"""Desk data builder: read the tape status, the registry, twin verdicts and lab outputs into one
deterministic ``desk.json`` (W11).

Read-only over every input. A missing file renders "not yet"; a malformed one is skipped and listed
under ``problems``. Every block carries ``src`` = {"file": "<root label>:<relative path>", "mtime",
"tw"} so a number on the page names the file and time it came from. Root labels: ``tape`` (the
Python recorder's data dir), ``tape-rust`` (``<tape>-rust``, the Rust shadow recorder), ``lab`` and
``root`` (the repo). Output depends only on the inputs, ``now`` and the disk reading, both of which
the caller passes, so a fixed input gives byte-identical output (keys sorted).

Sections, in page order:
  1. recorder: per stream Python vs Rust (msgs, last message age, reconnects, errors, drops), disk.
  2. strategies: every registered family/genome with its eval window, time left and status, the
     latest twin verdict per genome, and the H3 four-arm table once all four arms have results.
  3. evidence: FINDINGS E-rows, latest census, adverse (E24), clock-0 (E27) and xvenue summaries.
  4. calculator: A6 break-even arithmetic (net c/contract x contracts/day - operating $/month / 30).

Producing modules (shapes read here): ``tape.cli``/``doppel-taped`` status.json,
``strategy.registry``, ``twin.cli`` (+ ``twin.verdict``), ``lab.census``, ``lab.adverse``,
``lab.clock0``, ``lab.xvenue``.
"""

from __future__ import annotations

import math
import re
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson

from ..strategy.registry import REL_PATH, Registry, holm
from ..twin.verdict import block_bootstrap_ci

SCHEMA_VERSION = 1
STALE_STATUS_S = 30.0      # status.json is rewritten every 5 s; older than this = recorder not writing
STALE_MSG_S = 60.0         # a stream with no message for this long (as of the build) is flagged
H3_ARMS = ("base", "anon", "ident", "placebo")
DEFAULT_OP_USD_MONTH = 60.0  # twin.cli --op-cost-usd-month default
BOOT = {"block": 12, "n_boot": 2000, "seed": 0}  # twin.verdict defaults, reused for H3 paired deltas

DiskFn = Callable[[Path], dict | None]


# ---------------------------------------------------------------- small helpers
def iso(t: float | int | None) -> str | None:
    if t is None or not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t):
        return None
    return datetime.fromtimestamp(int(t), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(x: Any) -> float | None:
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x):
        return None
    return float(x)


def _r(x: Any, nd: int = 4) -> float | None:
    v = _num(x)
    return None if v is None else round(v, nd)


def disk_free(path: Path) -> dict | None:
    """Live disk reading for the volume holding ``path`` (None if it does not exist)."""
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return {"free_gb": round(u.free / 1e9, 2), "total_gb": round(u.total / 1e9, 2),
            "used_pct": round(100.0 * u.used / u.total, 1) if u.total else None}


class Ctx:
    """Input roots, the build instant and the problems found while reading."""

    def __init__(self, data: Path, lab: Path, root: Path, now: float):
        self.data, self.lab, self.root, self.now = Path(data), Path(lab), Path(root), float(now)
        self.rust = self.data.with_name(self.data.name + "-rust")
        self.problems: list[dict] = []
        self._labels = (("tape-rust", self.rust), ("tape", self.data), ("lab", self.lab), ("root", self.root))

    def label(self, path: Path) -> str:
        p = Path(path)
        for name, base in self._labels:
            try:
                return f"{name}:{p.relative_to(base).as_posix()}"
            except ValueError:
                continue
        return p.name

    def src(self, path: Path, tw_s: float | None = None) -> dict:
        try:
            mt: float | None = Path(path).stat().st_mtime
        except OSError:
            mt = None
        return {"file": self.label(path), "mtime": iso(mt), "tw": iso(tw_s)}

    def problem(self, path: Path, err: str) -> None:
        self.problems.append({"file": self.label(path), "error": err[:300]})

    def load(self, path: Path) -> Any:
        try:
            return orjson.loads(Path(path).read_bytes())
        except (OSError, orjson.JSONDecodeError) as e:
            self.problem(path, f"{type(e).__name__}: {e}")
            return None

    def jsons(self, sub: str, recursive: bool = False) -> list[Path]:
        d = self.lab / sub
        if not d.is_dir():
            return []
        return sorted(p for p in (d.rglob("*.json") if recursive else d.glob("*.json")) if p.is_file())


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def _tw_s(doc: dict) -> float | None:
    v = _num(doc.get("built_tw")) if isinstance(doc, dict) else None
    return v / 1e9 if v is not None else None


# ---------------------------------------------------------------- 1. recorder health
_STREAM_KEYS = ("msgs", "reconnects", "stalls", "errors", "connected", "last_msg_age_s", "last_error")


def _side(ctx: Ctx, base: Path, kind: str, disk: DiskFn) -> tuple[dict | None, dict]:
    path = base / "status.json"
    if not path.is_file():
        return None, {}
    st = ctx.load(path)
    if not isinstance(st, dict) or not isinstance(st.get("sources"), dict):
        if st is not None:
            ctx.problem(path, "status.json has no 'sources' object")
        return None, {}
    tw = _num(st.get("tw"))
    tw_s = tw / 1e9 if tw is not None else None
    status_age = round(ctx.now - tw_s, 1) if tw_s is not None else None
    side = {"kind": kind, "src": ctx.src(path, tw_s), "pid": st.get("pid"), "uptime_s": _num(st.get("uptime_s")),
            "status_age_s": status_age,
            "stale": status_age is None or status_age > STALE_STATUS_S,
            "disk": disk(base), "streams": len(st["sources"])}
    rows: dict = {}
    for name, s in st["sources"].items():
        if not isinstance(s, dict):
            continue
        row = {k: s.get(k) for k in _STREAM_KEYS}
        age = _num(s.get("last_msg_age_s"))
        row["age_at_build_s"] = round(age + (ctx.now - tw_s), 1) if age is not None and tw_s is not None else None
        # drops: the Rust recorder counts writer back-pressure drops; the Python recorder has no
        # such counter (None renders "n/a", never 0).
        row["drops"] = s.get("backpressure_drops") if kind == "rust" else None
        row["write_errors"] = s.get("write_errors")
        extra = {k: v for k, v in s.items() if k not in _STREAM_KEYS and k not in ("backpressure_drops", "write_errors")}
        row["extra"] = extra
        a = row["age_at_build_s"]
        row["flag"] = ("never" if row["msgs"] in (0, None) and a is None else
                       "stale" if a is None or a > STALE_MSG_S or side["stale"] else
                       "errors" if (row.get("errors") or 0) > 0 or row.get("last_error") else "ok")
        rows[str(name)] = row
    return side, rows


def recorder(ctx: Ctx, disk: DiskFn) -> dict:
    py, py_rows = _side(ctx, ctx.data, "python", disk)
    rs, rs_rows = _side(ctx, ctx.rust, "rust", disk)
    names = sorted(set(py_rows) | set(rs_rows))
    streams = [{"name": n, "python": py_rows.get(n), "rust": rs_rows.get(n)} for n in names]
    return {"python": py, "rust": rs, "streams": streams,
            "thresholds": {"stale_status_s": STALE_STATUS_S, "stale_msg_s": STALE_MSG_S}}


# ---------------------------------------------------------------- 2. strategies
def _registry_records(ctx: Ctx) -> tuple[list[dict], dict]:
    path = ctx.root / REL_PATH
    info: dict = {"src": ctx.src(path), "present": path.is_file(), "lines": 0, "bad_lines": 0,
                  "head": None, "verify": None}
    if not path.is_file():
        return [], info
    recs: list[dict] = []
    try:
        lines = Registry(ctx.root).lines()
    except OSError as e:
        ctx.problem(path, f"{type(e).__name__}: {e}")
        return [], info
    for i, ln in enumerate(lines):
        try:
            r = orjson.loads(ln)
            if not isinstance(r, dict):
                raise ValueError("not an object")
            recs.append(r)
        except (orjson.JSONDecodeError, ValueError) as e:
            info["bad_lines"] += 1
            ctx.problem(path, f"line {i}: {e}")
    info["lines"] = len(lines)
    info["head"] = recs[-1].get("hash") if recs else None
    try:
        errs = Registry(ctx.root).verify()
        info["verify"] = {"ok": not errs, "problems": errs[:20], "n_problems": len(errs)}
    except Exception as e:  # noqa: BLE001 - a broken registry must not stop the desk
        info["verify"] = {"ok": False, "problems": [f"verify raised {type(e).__name__}: {e}"], "n_problems": 1}
    last = max((_num(r.get("tw")) or 0.0 for r in recs), default=0.0)
    info["src"]["tw"] = iso(last / 1e9) if last else None
    return recs, info


def _twin_docs(ctx: Ctx) -> list[dict]:
    """Every twin output under <lab>/twin/** reduced to its verdict summary plus a slim per-window
    list for H3 pairing. Ordered by (range end, mtime, path)."""
    out = []
    for p in ctx.jsons("twin", recursive=True):
        d = ctx.load(p)
        if d is None:
            continue
        if not (isinstance(d, dict) and isinstance(d.get("verdict"), dict) and isinstance(d.get("genome"), dict)
                and d["genome"].get("id")):
            continue  # not a twin output (some other JSON in the folder)
        v, tw = d["verdict"], d.get("twin") or {}
        r0 = d.get("range")
        rng: list = r0 if isinstance(r0, list) and len(r0) == 2 else [None, None]
        out.append({"path": p, "mtime": _mtime(p), "windows": _slim(d.get("windows")),
                    "summary": {
                        "src": ctx.src(p), "genome_id": d["genome"]["id"], "name": d["genome"].get("name"),
                        "range": [_num(rng[0]), _num(rng[1])], "range_iso": [iso(_num(rng[0])), iso(_num(rng[1]))],
                        "flow_delay_s": tw.get("flow_delay_s", 0) or 0, "diagnostic": bool(tw.get("diagnostic")),
                        "code_sha256": tw.get("code_sha256"),
                        "decision": v.get("decision"), "reason": v.get("reason"), "metric": v.get("metric"),
                        "windows_total": v.get("windows_total"), "windows_evaluated": v.get("windows_evaluated"),
                        "windows_traded": v.get("windows_traded"), "excluded": v.get("excluded"),
                        "net_c_per_window": v.get("net_c_per_window"), "net_c_per_contract": v.get("net_c_per_contract"),
                        "total_net_usd": v.get("total_net_usd"), "fees_usd": v.get("fees_usd"),
                        "contracts": v.get("contracts"), "max_drawdown_usd": v.get("max_drawdown_usd"),
                        "commercial": v.get("commercial") if isinstance(v.get("commercial"), dict) else None}})
    out.sort(key=lambda x: ((x["summary"]["range"][1] or 0.0), x["mtime"], str(x["path"])))
    return out


def _slim(ws: Any) -> list[dict]:
    """Keep only what H3 pairing needs per window (twin outputs carry fills and inventory paths)."""
    if not isinstance(ws, list):
        return []
    return [{"slug": w.get("slug"), "eligible": bool(w.get("eligible")), "net_c": w.get("net_c"), "end": w.get("end")}
            for w in ws if isinstance(w, dict)]


def _evidence_runs(docs: list[dict], delay: int | None = 0) -> list[dict]:
    """Runs that count as evidence: not DIAGNOSTIC, and (by default) no flow delay."""
    return [d for d in docs if not d["summary"]["diagnostic"] and d["summary"]["decision"] != "DIAGNOSTIC"
            and (delay is None or int(d["summary"]["flow_delay_s"] or 0) == delay)]


FIT_RANK = {"prereg": 3, "partial": 2}


def _rank(d: dict, ew: dict) -> tuple:
    """Best run first by window fit (prereg > partial > off-window), then recency."""
    s = d["summary"]
    return (FIT_RANK.get(_window_fit(s["range"], ew), 1), s["range"][1] or 0.0, d["mtime"], str(d["path"]))


def _primary(docs: list[dict], ew: dict) -> dict | None:
    ok = _evidence_runs(docs)
    return max(ok, key=lambda d: _rank(d, ew)) if ok else None


def _window_fit(rng: list, ew: dict) -> str:
    a, b = rng
    s, e = _num(ew.get("start_s")), _num(ew.get("end_s"))
    if a is None or b is None or s is None or e is None:
        return "unknown"
    if int(a) == int(s) and int(b) == int(e):
        return "prereg"
    if s <= a and b <= e:
        return "partial"
    return "off-window"


def _phase(ew: dict, now: float, tested: bool) -> tuple[str, float | None, float | None]:
    """(phase, seconds to window start, seconds to window end) as of ``now``."""
    s, e = _num(ew.get("start_s")), _num(ew.get("end_s"))
    to_s = round(max(0.0, s - now)) if s is not None else None
    to_e = round(max(0.0, e - now)) if e is not None else None
    if tested:
        return "tested", to_s, to_e
    if s is None or e is None:
        return "unknown", None, None
    if now < s:
        return "scheduled", to_s, to_e
    if now < e:
        return "evaluating", to_s, to_e
    return "awaiting result", to_s, to_e


def strategies(ctx: Ctx) -> dict:
    recs, info = _registry_records(ctx)
    docs = _twin_docs(ctx)
    by_gid: dict[str, list[dict]] = {}
    for d in docs:
        by_gid.setdefault(d["summary"]["genome_id"], []).append(d)
    regs = [r for r in recs if r.get("kind") == "register" and r.get("genome_id")]
    results: dict[str, list[dict]] = {}
    for r in recs:
        if r.get("kind") == "result" and r.get("genome_id"):
            results.setdefault(r["genome_id"], []).append(r)
    fams: dict[str, list[dict]] = {}
    for r in regs:
        fams.setdefault(str(r.get("family")), []).append(r)
    families = []
    for fam in sorted(fams):
        rows = fams[fam]
        ps = [_binding_p(results.get(r["genome_id"], [])) for r in rows]
        adj = holm([1.0 if p is None else p for p in ps])
        genomes = []
        for r, p, a in zip(rows, ps, adj):
            gid = r["genome_id"]
            ew = r.get("eval_window") or {}
            tested = gid in results
            phase, to_start, to_end = _phase(ew, ctx.now, tested)
            ev = ((r.get("genome") or {}).get("evidence") or {}) if isinstance(r.get("genome"), dict) else {}
            prim = _primary(by_gid.get(gid, []), ew)
            twin = None
            if prim is not None:
                twin = {**prim["summary"], "window_fit": _window_fit(prim["summary"]["range"], ew)}
            genomes.append({
                "genome_id": gid, "name": r.get("name"), "arm": ev.get("arm") if isinstance(ev, dict) else None,
                "registered": iso((_num(r.get("tw")) or 0) / 1e9) if r.get("tw") else None,
                "eval_window": {"start": ew.get("start"), "end": ew.get("end"),
                                "start_s": _num(ew.get("start_s")), "end_s": _num(ew.get("end_s"))},
                "code_sha": r.get("code_sha"), "phase": phase,
                "seconds_to_start": to_start, "seconds_remaining": to_end,
                "registry_status": "tested" if tested else "pending",
                "p_value": p, "p_holm": round(a, 6) if tested else None,
                "survives": bool(tested and a <= 0.05), "results": len(results.get(gid, [])),
                "twin_runs": len(by_gid.get(gid, [])), "twin": twin})
        hyp = rows[0].get("hypothesis")
        families.append({"family": fam, "hypothesis": hyp, "m": len(rows),
                         "tested": sum(g["registry_status"] == "tested" for g in genomes),
                         "pending": sum(g["registry_status"] == "pending" for g in genomes),
                         "genomes": genomes})
    known = {r["genome_id"] for r in regs}
    unregistered = [d["summary"] for d in docs if d["summary"]["genome_id"] not in known]
    return {"registry": info, "families": families, "h3": h3_table(ctx, regs, by_gid),
            "unregistered_twin_runs": unregistered[-20:],
            "twin_files": len(docs)}


def _binding_p(res: list[dict]) -> float | None:
    if not res:
        return None
    v = res[0].get("verdict") or {}
    p = _num(v.get("p_value")) if isinstance(v, dict) else None
    return p if p is not None and 0.0 <= p <= 1.0 else 1.0


def _paired(base_w: list[dict], arm_w: list[dict]) -> dict:
    """Arm minus base on identical eligible windows (matched by slug, ordered by end)."""
    b = {w.get("slug"): w for w in base_w if isinstance(w, dict) and w.get("eligible")}
    pairs = []
    for w in arm_w:
        if not (isinstance(w, dict) and w.get("eligible")):
            continue
        bw = b.get(w.get("slug"))
        if bw is None:
            continue
        x, y = _num(w.get("net_c")), _num(bw.get("net_c"))
        if x is None or y is None:
            continue
        pairs.append((w.get("end") or 0, w.get("slug") or "", x - y))
    pairs.sort()
    d = [p[2] for p in pairs]
    if not d:
        return {"n_pairs": 0, "delta_c_per_window": None, "ci95": [None, None], "delta_usd_total": None}
    lo, hi = block_bootstrap_ci(d, None, BOOT["block"], BOOT["n_boot"], BOOT["seed"])
    return {"n_pairs": len(d), "delta_c_per_window": round(sum(d) / len(d), 4),
            "ci95": [_r(lo), _r(hi)], "delta_usd_total": round(sum(d) / 100.0, 4)}


def _h3_family(regs: list[dict]) -> tuple[str | None, dict[str, dict]]:
    """The family whose registered genomes carry evidence.arm for all four H3 arms (grouped by
    family, so another family's 'base' cannot replace H3's). Else the family with the most arms."""
    fams: dict[str, dict[str, dict]] = {}
    for r in regs:
        ev = (r.get("genome") or {}).get("evidence") if isinstance(r.get("genome"), dict) else None
        arm = ev.get("arm") if isinstance(ev, dict) else None
        if arm in H3_ARMS:
            fams.setdefault(str(r.get("family")), {}).setdefault(arm, r)
    if not fams:
        return None, {}
    best = max(sorted(fams), key=lambda f: (len(fams[f]) == len(H3_ARMS), len(fams[f])))
    return best, fams[best]


def h3_table(ctx: Ctx, regs: list[dict], by_gid: dict[str, list[dict]]) -> dict:
    """The H3 four-arm comparison: one row per arm on a replay range all four arms share (the
    registered window if they all have it), latest delay-0 non-diagnostic run per arm on that range,
    deltas vs base on identical windows. Arms are found by genome.evidence.arm within one family."""
    family, arms = _h3_family(regs)
    out: dict = {"family": family, "arms": list(H3_ARMS), "ready": False, "rows": [], "delay_sweep": []}
    if set(arms) != set(H3_ARMS):
        out["why_not"] = f"registry has arms {sorted(arms)}; need all of {list(H3_ARMS)}"
        return out
    ew = arms["base"].get("eval_window") or {}
    out["eval_window"] = [ew.get("start"), ew.get("end")]
    runs = {a: _evidence_runs(by_gid.get(arms[a]["genome_id"], [])) for a in H3_ARMS}
    missing = [a for a in H3_ARMS if not runs[a]]
    if missing:
        out["why_not"] = f"no twin result yet for arm(s) {missing}"
        return out
    shared = set.intersection(*({tuple(d["summary"]["range"]) for d in runs[a]} for a in H3_ARMS))
    if not shared:
        out["why_not"] = "arms were replayed on different ranges: " + ", ".join(
            f"{a}={sorted({tuple(d['summary']['range']) for d in runs[a]}, key=str)}" for a in H3_ARMS)
        return out
    rng = max(sorted(shared, key=str), key=lambda r: (FIT_RANK.get(_window_fit(list(r), ew), 1), r[1] or 0.0))
    got = {a: max((d for d in runs[a] if tuple(d["summary"]["range"]) == rng),
                  key=lambda d: (d["mtime"], str(d["path"]))) for a in H3_ARMS}
    base = got["base"]
    for a in H3_ARMS:
        d = got[a]
        s = d["summary"]
        row = {"arm": a, "genome_id": s["genome_id"], "name": s["name"], "src": s["src"],
               "decision": s["decision"], "windows_evaluated": s["windows_evaluated"],
               "windows_traded": s["windows_traded"], "net_c_per_window": s["net_c_per_window"],
               "net_c_per_contract": s["net_c_per_contract"], "total_net_usd": s["total_net_usd"],
               "contracts": s["contracts"], "max_drawdown_usd": s["max_drawdown_usd"],
               "commercial_lb_usd_day": (s["commercial"] or {}).get("net_usd_per_day_lower_bound_after_ops"),
               "vs_base": None if a == "base" else _paired(base["windows"], d["windows"])}
        out["rows"].append(row)
    out["ready"] = True
    out["range_iso"] = base["summary"]["range_iso"]
    out["window_fit"] = _window_fit(list(rng), ew)
    # Preregistered delay sweep on the ident arm (twin --flow-delay-s), same range as the table.
    sweep: dict[int, dict] = {}
    for d in _evidence_runs(by_gid.get(arms["ident"]["genome_id"], []), delay=None):
        if tuple(d["summary"]["range"]) != rng:
            continue
        k = int(d["summary"]["flow_delay_s"] or 0)
        if k not in sweep or (d["mtime"], str(d["path"])) > (sweep[k]["mtime"], str(sweep[k]["path"])):
            sweep[k] = d
    for k in sorted(sweep):
        s = sweep[k]["summary"]
        out["delay_sweep"].append({"flow_delay_s": k, "src": s["src"], "decision": s["decision"],
                                   "net_c_per_window": s["net_c_per_window"],
                                   "vs_base": _paired(base["windows"], sweep[k]["windows"])})
    return out


# ---------------------------------------------------------------- 3. evidence
_ROW = re.compile(r"^\|\s*(E\d+)\s*\|(.*)\|\s*$")


def _one_line(text: str, n: int = 220) -> str:
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", text).strip()
    m = re.search(r"(?<=[a-z0-9)%\]`])\.\s+(?=[A-Z(])", t)
    if m and m.start() >= 20:
        t = t[: m.start() + 1]
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def findings(ctx: Ctx) -> dict:
    files = sorted(p for p in (ctx.root / "evidence").glob("*/FINDINGS.md") if p.is_file()) \
        if (ctx.root / "evidence").is_dir() else []
    rows: dict[str, dict] = {}
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            ctx.problem(p, f"{type(e).__name__}: {e}")
            continue
        for ln in text.splitlines():
            m = _ROW.match(ln.strip())
            if not m:
                continue
            cells = [c.strip() for c in m.group(2).split("|")]
            if len(cells) < 3:
                continue
            finding = " | ".join(cells[:-2])
            rows[m.group(1)] = {"id": m.group(1), "line": _one_line(finding), "confidence": cells[-1],
                                "src": ctx.src(p)}
    ordered = sorted(rows.values(), key=lambda r: int(r["id"][1:]))
    return {"rows": ordered, "files": [ctx.label(p) for p in files]}


def _latest(ctx: Ctx, sub: str, ok: Callable[[Any], bool], ts: Callable[[Path, dict], float | None]):
    """All well-shaped docs in <lab>/<sub>/*.json as (time, path, doc), oldest first."""
    got = []
    for p in ctx.jsons(sub):
        d = ctx.load(p)
        if d is None:
            continue
        if not ok(d):
            continue
        got.append((ts(p, d) or _mtime(p), str(p), p, d))
    got.sort(key=lambda x: (x[0], x[1]))
    return got


def census(ctx: Ctx) -> dict | None:
    got = _latest(ctx, "census", lambda d: isinstance(d, dict) and isinstance(d.get("by_label"), dict),
                  lambda p, d: _tw_s(d))
    if not got:
        return None
    _, _, p, d = got[-1]
    rows = []
    for k, v in sorted(d["by_label"].items()):
        if not isinstance(v, dict):
            continue
        role, _, lab = str(k).partition(":")
        rows.append({"role": role, "label": lab or None, "wallets": v.get("wallets"),
                     "positive": v.get("positive"), "per_day": v.get("per_day")})
    w = d.get("wallets") if isinstance(d.get("wallets"), dict) else {}
    return {"src": ctx.src(p, _tw_s(d)), "cohort_sha256": d.get("cohort_sha256"), "rows": rows,
            "wallets": len(w), "eligible": sum(1 for x in w.values() if isinstance(x, dict) and x.get("eligible")),
            "selfcheck": d.get("selfcheck"), "files": len(got)}


def adverse(ctx: Ctx) -> list[dict]:
    got = _latest(ctx, "adverse", lambda d: isinstance(d, dict) and "by_model_edge" in d and "total" in d,
                  lambda p, d: _tw_s(d))
    per_hz: dict[str, tuple] = {}
    for item in got:
        per_hz[str(item[3].get("hz"))] = item
    out = []
    for hz in sorted(per_hz):
        _, _, p, d = per_hz[hz]
        out.append({"src": ctx.src(p, _tw_s(d)), "hz": d.get("hz"), "total": d.get("total"),
                    "skipped": d.get("skipped"), "by_tte": d.get("by_tte"), "by_model_edge": d.get("by_model_edge")})
    return out


def clock0(ctx: Ctx) -> dict | None:
    got = _latest(ctx, "clock0", lambda d: isinstance(d, dict) and isinstance(d.get("eval"), dict),
                  lambda p, d: _tw_s(d))
    if not got:
        return None
    _, _, p, d = got[-1]
    e, meta = d["eval"], d.get("meta") if isinstance(d.get("meta"), dict) else {}
    mm = e.get("markout_model") if isinstance(e.get("markout_model"), dict) else None
    keys = ("status", "n", "n_train", "n_test", "label", "excess_threshold_c", "base_rate_label",
            "base_rate_informed_abs", "known_wallet_share", "auc_model", "auc_size_only", "corr_weight_model",
            "by_predicted_quintile")
    return {"src": ctx.src(p, _tw_s(d)), "as_of": iso(_num(meta.get("as_of"))), "until": iso(_num(meta.get("until"))),
            "wallets_scored": meta.get("wallets_scored"), **{k: e.get(k) for k in keys},
            "markout_spearman": mm.get("spearman") if mm else None,
            "top_features": [[k, v] for k, v in list(e["importance_auc_drop"].items())[:5]]
            if isinstance(e.get("importance_auc_drop"), dict) else []}


_XV_TS = re.compile(r"(\d{8}T\d{4}Z)\.json$")


def _xv_ts(p: Path, d: dict) -> float | None:
    m = _XV_TS.search(p.name)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%MZ").replace(tzinfo=UTC).timestamp()


def xvenue(ctx: Ctx) -> list[dict]:
    got = _latest(ctx, "xvenue", lambda d: isinstance(d, dict) and isinstance(d.get("summary"), dict)
                  and isinstance(d.get("equivalence"), dict), _xv_ts)
    per_fee: dict[tuple, tuple] = {}
    for item in got:
        f = item[3].get("fees") if isinstance(item[3].get("fees"), dict) else {}
        per_fee[(_num(f.get("pmus_coef")) or -1.0, _num(f.get("pmus_rebate")) or 0.0)] = item
    out = []
    for k in sorted(per_fee):
        t, _, p, d = per_fee[k]
        eq = d["equivalence"]
        out.append({"src": ctx.src(p, t), "fees": d.get("fees"), "params": d.get("params"),
                    "equivalence": {x: eq.get(x) for x in ("checked", "strike_match", "result_match")},
                    "mismatches": len(eq.get("mismatches") or []), "summary": d["summary"]})
    return out


# ---------------------------------------------------------------- 4. calculator (A6)
def breakeven(net_c_per_contract: float, contracts_per_day: float, op_usd_month: float) -> dict:
    """A6 arithmetic, identical to twin.verdict.commercial: operating $/day = $/month / 30;
    net $/day = net c/contract / 100 x contracts/day - operating $/day; break-even contracts/day =
    operating $/day / (net c/contract / 100), undefined (None) when net c/contract <= 0."""
    op_day = op_usd_month / 30.0
    gross = net_c_per_contract / 100.0 * contracts_per_day
    return {"op_usd_per_day": round(op_day, 2), "edge_usd_per_day": round(gross, 2),
            "net_usd_per_day": round(gross - op_day, 2),
            "breakeven_contracts_per_day": round(op_day / (net_c_per_contract / 100.0), 1)
            if net_c_per_contract > 0 else None}


def calculator(strat: dict, adv: list[dict]) -> dict:
    presets = []
    for fam in strat["families"]:
        for g in fam["genomes"]:
            t = g.get("twin")
            if not t:
                continue
            pc = _num((t.get("net_c_per_contract") or {}).get("value"))
            a, b = t.get("range") or [None, None]
            ctr = _num(t.get("contracts"))
            if pc is None or not ctr or a is None or b is None or b <= a:
                continue
            # per day over the replay range (commercial.days counts calendar days by window end, which
            # counts a midnight-ending window as an extra day)
            presets.append({"label": f"twin {g['name']} ({t.get('window_fit')})", "net_c_per_contract": round(pc, 4),
                            "contracts_per_day": round(ctr / ((b - a) / 86400.0), 1),
                            "op_usd_month": DEFAULT_OP_USD_MONTH, "src": t["src"]})
    for a in adv:
        cell = (a.get("by_model_edge") or {}).get("2..5c")
        if isinstance(cell, dict) and _num(cell.get("maker_pnl_c")) is not None:
            presets.append({"label": f"E24 maker fills, fair edge 2..5c ({a.get('hz')})",
                            "net_c_per_contract": cell["maker_pnl_c"], "contracts_per_day": None,
                            "op_usd_month": None, "src": a["src"],
                            "note": "other makers' public fills, not ours; sets net c/contract only"})
    d = {"net_c_per_contract": 0.5, "contracts_per_day": 10000.0, "op_usd_month": DEFAULT_OP_USD_MONTH}
    return {"defaults": d, "example": breakeven(d["net_c_per_contract"], d["contracts_per_day"], d["op_usd_month"]),
            "presets": presets,
            "formula": "net $/day = net c/contract / 100 x contracts/day - operating $/month / 30; "
                       "break-even contracts/day = (operating $/month / 30) / (net c/contract / 100)"}


# ---------------------------------------------------------------- top level
def build(data: Path, lab: Path, root: Path, now: float, disk: DiskFn = disk_free) -> dict:
    ctx = Ctx(data, lab, root, now)
    rec = recorder(ctx, disk)
    strat = strategies(ctx)
    adv = adverse(ctx)
    ev = {"findings": findings(ctx), "census": census(ctx), "adverse": adv, "clock0": clock0(ctx),
          "xvenue": xvenue(ctx)}
    calc = calculator(strat, adv)
    probs = sorted({(p["file"], p["error"]) for p in ctx.problems})
    return {"schema_version": SCHEMA_VERSION, "built": iso(now), "built_s": int(now),
            "inputs": {"tape": ctx.data.name, "tape_rust": ctx.rust.name, "lab": ctx.lab.name, "root": ctx.root.name},
            "recorder": rec, "strategies": strat, "evidence": ev, "calculator": calc,
            "problems": [{"file": f, "error": e} for f, e in probs]}


def dumps(desk: dict) -> bytes:
    return orjson.dumps(desk, option=orjson.OPT_SORT_KEYS | orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS)
