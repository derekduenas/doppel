"""G5 paper-vs-twin agreement: replay the journal's genome over the journal's period through the twin
(``twin.cli``'s inputs: ``RefStore`` + ``iter_windows``, the canonical cross-window rules) with the
same observed engine (``PaperSim``), and compare decision by decision.

Per window both ran:
- inputs: share of decision seconds whose feature-vector hash is identical;
- intents: share of identical intents (time, kind, side, price, size / qty, rule), as matched
  multiset count over the larger of the two lists;
- ``same_input_diff_intent``: decision seconds with identical inputs whose intents differ. With one
  engine this must be 0; anything else is a defect, not drift;
- fills: share of identical fills (time, role, side, price, qty, via);
- net P&L difference (paper - twin).
Windows the twin excludes (feed gaps, coverage, unsettled, no WS) are listed under ``paper_only``
with the twin's reason: paper traded them, the twin never scores them.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from pathlib import Path

from ..strategy.genome import from_dict
from ..twin.engine import SimConfig
from ..twin.features import RefStore
from ..twin.tape import iter_windows
from .journal import load
from .sim import record_windows

KINDS = ("decide", "intent", "ack", "fill", "skip")


def intent_key(r: dict) -> tuple:
    return (r["t"], r["kind"], r.get("side"), r.get("px"), r.get("size"), r.get("qty"), r.get("rule"),
            r.get("limit"))


def fill_key(r: dict) -> tuple:
    return (r["t"], r["role"], r["side"], r["px"], r["qty"], r["via"])


def _share(a: list, b: list) -> float | None:
    n = max(len(a), len(b))
    if n == 0:
        return None
    return sum((Counter(a) & Counter(b)).values()) / n


def compare_window(paper: list[dict], twin: list[dict]) -> dict:
    px = {r["d"]: r["xh"] for r in paper if r["k"] == "decide"}
    tx = {r["d"]: r["xh"] for r in twin if r["k"] == "decide"}
    ds = set(px) | set(tx)
    same = {d for d in ds if px.get(d) == tx.get(d)}
    pi = [intent_key(r) for r in paper if r["k"] == "intent"]
    ti = [intent_key(r) for r in twin if r["k"] == "intent"]
    by_t_p, by_t_t = defaultdict(list), defaultdict(list)
    for k in pi:
        by_t_p[k[0]].append(k)
    for k in ti:
        by_t_t[k[0]].append(k)
    diff_same_input = sorted(d for d in same if sorted(by_t_p.get(d * 10**9, [])) != sorted(by_t_t.get(d * 10**9, [])))
    pf = [fill_key(r) for r in paper if r["k"] == "fill"]
    tf = [fill_key(r) for r in twin if r["k"] == "fill"]
    return {"decisions": len(ds), "inputs_identical_share": len(same) / len(ds) if ds else None,
            "intents_paper": len(pi), "intents_twin": len(ti), "intents_identical_share": _share(pi, ti),
            "same_input_diff_intent": len(diff_same_input), "same_input_diff_intent_seconds": diff_same_input[:20],
            "fills_paper": len(pf), "fills_twin": len(tf), "fills_identical_share": _share(pf, tf)}


def agree(journal_dir: Path, data: Path) -> dict:
    recs = load(journal_dir)
    head = recs[0]
    g = from_dict(head["genome"])
    raw_cfg: dict = {k: tuple(v) if isinstance(v, list) else v for k, v in head["cfg"].items()}
    cfg = SimConfig(**raw_cfg)
    settled = {r["slug"]: r for r in recs if r["k"] == "settle"}
    if not settled:
        return {"genome_id": g.id, "windows": 0, "note": "no settled paper window yet"}
    t_from = head["first_start"]
    t_to = max(r["end"] for r in settled.values())
    store = RefStore(data)
    for a in g.universe.assets:
        store.load_boundaries(a, t_from - 1, t_to)
    items = ((wt, store.window_ref(wt.asset, wt.start, wt.end, g.fair.vol_window_s))
             for wt in iter_windows(data, g.universe.assets, g.universe.horizons, t_from, t_to, cfg.depth_levels))
    twin_recs: dict[str, list[dict]] = defaultdict(list)
    results = record_windows(g, items, cfg, emit=lambda r: twin_recs[r["slug"]].append(r))
    paper_recs: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        if r["k"] in KINDS and r.get("slug"):
            paper_recs[r["slug"]].append(r)
    per, paper_only = {}, {}
    for res in results:
        slug = res["slug"]
        if slug not in settled:
            continue
        if not res.get("eligible", True) or res.get("excluded"):
            paper_only[slug] = res.get("excluded")
            continue
        c = compare_window(paper_recs[slug], twin_recs[slug])
        c["net_paper_usd"] = settled[slug].get("net_usd")
        c["net_twin_usd"] = res["net_usd"]
        c["net_diff_usd"] = None if c["net_paper_usd"] is None else round(c["net_paper_usd"] - res["net_usd"], 6)
        c["late_events"] = settled[slug].get("late", 0)
        per[slug] = c
    missing = sorted(set(settled) - {r["slug"] for r in results})
    tot = Counter()
    for c in per.values():
        for k in ("decisions", "intents_paper", "intents_twin", "fills_paper", "fills_twin", "same_input_diff_intent"):
            tot[k] += c[k]

    def wmean(key: str, wkey: str) -> float | None:
        xs = [(c[key], max(c[wkey], 1)) for c in per.values() if c[key] is not None]
        den = sum(w for _, w in xs)
        return round(sum(v * w for v, w in xs) / den, 6) if den else None

    return {"genome_id": g.id, "range": [t_from, t_to], "windows_compared": len(per),
            "windows_identical": sum(1 for c in per.values() if c["intents_identical_share"] in (1.0, None)
                                     and c["fills_identical_share"] in (1.0, None) and c["net_diff_usd"] == 0),
            "inputs_identical_share": wmean("inputs_identical_share", "decisions"),
            "intents_identical_share": wmean("intents_identical_share", "intents_paper"),
            "fills_identical_share": wmean("fills_identical_share", "fills_paper"),
            "same_input_diff_intent": tot["same_input_diff_intent"],
            "net_paper_usd": round(sum(c["net_paper_usd"] or 0.0 for c in per.values()), 6),
            "net_twin_usd": round(sum(c["net_twin_usd"] for c in per.values()), 6),
            "net_diff_usd": round(sum(c["net_diff_usd"] or 0.0 for c in per.values()), 6),
            "totals": dict(tot), "paper_only": paper_only, "missing_in_twin": missing, "windows": per}
