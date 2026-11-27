"""Genome extraction: one wallet dossier -> concrete, executable candidate genomes.

Input is the dossier JSON exactly as ``doppel-dossier`` writes it (``LAB/dossiers/<wallet>.json``),
optionally with its ``<wallet>.orders.parquet`` and ``<wallet>.fills.parquet`` tables for numbers the
JSON only summarises (exact median absolute quote distance, inventory-skew slope). Output is a list
of ``candidate`` genomes plus a list of refusals: what was NOT emitted and why.

Discipline
----------
* Never invent. A tree split on a dossier feature with no genome equivalent refuses the whole leaf
  (dropping only that split would widen the rule beyond what the tree supports). A maker field the
  dossier does not identify gets a DOPPEL default and is tagged ``default`` in
  ``evidence["fields"]``; identified fields are tagged ``derived`` with their source.
* Conservative translation. Every emitted taker rule fires on a SUBSET of its tree leaf's region.
  Panel edges are gross of fees while genome edges are net of the PM US taker fee, so a lower bound
  on an edge carries across unchanged and an upper bound is lowered by the maximum fee.
* Deterministic. The genome id hashes ``name`` and ``evidence`` too, so the same dossier always
  yields the same ids: nothing hashed depends on the wall clock.
* One taker genome (its rules are the qualifying leaves, OR-ed) and at most one maker genome per
  wallet. Each id is one variant charged to its family in the registry.

Dossier panel feature -> genome feature (``FEATURE_MAP``)
---------------------------------------------------------
=========  ===============  ==========================================================================
dossier    genome           transform
=========  ===============  ==========================================================================
edge_up    edge_buy_up_c    x100; panel is gross of fee, genome net: ``>`` kept, ``<=`` minus 3 c
edge_dn    edge_buy_down_c  same as edge_up
fair_up    fair_up          identity (0-1)
tte        tte_s            identity (s); the leaf must intersect the universe's tte range
mv1        brti_move_1s     identity (USD, raw BRTI s-1 minus s-2)
mv5        brti_move_5s     identity (USD)
mv30       brti_move_30s    identity (USD)
spread_up  spread_c         x100 (intl Up book -> PM US YES book)
inv        inventory_up     x (DOPPEL size / wallet shares per order); refused when that is unknown
(other)    none             leaf refused
=========  ===============  ==========================================================================

Genome features with no dossier source (never emitted from a tree): tau_s (universe only),
mid_up_c, depth_top_up, depth_top_down, dist_to_open_usd.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import re
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import orjson

from .genome import (Condition, Costs, Genome, MakerRule, Risk, Source, TakerRule, Universe, describe,
                     fee_c, validate)

# ---------------------------------------------------------------- thresholds (constants on purpose)
MIN_AUC_TREE = 0.65            # held-out AUC of the depth-3 tree whose leaves become rules
MIN_TRAIN_TAKES = 50           # policy.took_any.train_pos
MIN_TEST_TAKES = 20            # policy.took_any.test_pos
MIN_SIDE_TAKES = 20            # each side, train, when a side tree is used
ONE_SIDED_MAX_MINORITY = 0.05  # minority-side share of takes for a one-sided wallet
MIN_BTC_FILLS = 100            # fair-v1 (and so every genome) is BTC-only
MIN_MAKER_SHARE = 0.6          # census: maker labels start at 60 % maker notional
MIN_MAKER_ORDERS = 100
COMPLETE_SET_MIN_PAIRED = 0.3  # inventory.paired_share_of_gross; 0.5 = every share paired
REF_TIGHT_TOL_C = 0.5          # fair may be up to half a cent "looser" (median) than mid
MIN_SKEW_FILLS = 100
MIN_SKEW_T = 2.0

# DOPPEL risk limits (the same envelope as the hand-made H1 family)
TAKER_SIZE_MAX = 5.0
TAKER_MAX_ENTRIES = 3
TAKER_COOLDOWN_S = 5
MAKER_SIZE_MAX = 10.0
QUOTE_AGE_S = (5, 60)
DOPPEL_RISK = Risk(max_inventory=40, max_notional_per_window=40, max_loss_per_day=25, max_open_windows=1)
HZ_LEN = {"5m": 300, "15m": 900}
DECIMALS = 4
CLOCK_FEATURES = frozenset({"tte"})

# Max PM US taker fee per contract (p = 0.5), rounded up to a whole cent for fee rounding.
FEE_BOUND_C = float(math.ceil(fee_c(0.5, Costs().taker_fee_coef)))

FEATURE_MAP: dict[str, dict[str, Any]] = {
    "edge_up": {"genome": "edge_buy_up_c", "scale": 100.0, "kind": "net_edge",
                "note": "gross prob -> net cents: '>' kept (conservative), '<=' lowered by FEE_BOUND_C"},
    "edge_dn": {"genome": "edge_buy_down_c", "scale": 100.0, "kind": "net_edge",
                "note": "gross prob -> net cents: '>' kept (conservative), '<=' lowered by FEE_BOUND_C"},
    "fair_up": {"genome": "fair_up", "scale": 1.0, "kind": "exact", "note": "identity"},
    "tte": {"genome": "tte_s", "scale": 1.0, "kind": "exact", "note": "identity (seconds to close)"},
    "mv1": {"genome": "brti_move_1s", "scale": 1.0, "kind": "exact", "note": "identity (USD)"},
    "mv5": {"genome": "brti_move_5s", "scale": 1.0, "kind": "exact", "note": "identity (USD)"},
    "mv30": {"genome": "brti_move_30s", "scale": 1.0, "kind": "exact", "note": "identity (USD)"},
    "spread_up": {"genome": "spread_c", "scale": 100.0, "kind": "exact",
                  "note": "x100 (intl Up book spread -> PM US YES spread)"},
    "inv": {"genome": "inventory_up", "scale": None, "kind": "inventory",
            "note": "wallet shares -> DOPPEL contracts by size ratio; refused if unknown"},
}


class Refused(ValueError):
    """A leaf, field or genome the dossier does not support."""


# ---------------------------------------------------------------- tree rules text
@dataclass(frozen=True)
class Split:
    feature: str
    op: Literal["<=", ">"]
    threshold: float

    def text(self) -> str:
        return f"{self.feature} {self.op} {self.threshold:g}"


@dataclass(frozen=True)
class Leaf:
    path: tuple[Split, ...]
    cls: str | None                    # class label as printed; None for a truncated branch
    weights: tuple[float, ...] | None = None


_LINE = re.compile(r"^((?:\|   )*)\|--- (.*)$")
_SPLIT = re.compile(r"^(\S+) (<=|>) +(\S+)$")
_CLASS = re.compile(r"^(?:weights: \[([^\]]*)\] )?class: (.+)$")


def parse_tree_rules(text: str) -> list[Leaf]:
    """sklearn ``export_text`` output -> leaves in print order, each with its root-to-leaf path.

    Handles ``show_weights`` and truncated branches (returned with ``cls=None``). Raises
    ``ValueError`` on anything else, so a format change cannot silently drop splits."""
    leaves: list[Leaf] = []
    path: list[Split] = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        m = _LINE.match(ln.rstrip())
        if not m:
            raise ValueError(f"unparseable rules line: {ln!r}")
        depth, body = len(m.group(1)) // 4, m.group(2).strip()
        if depth > len(path):
            raise ValueError(f"rules line deeper than its parent: {ln!r}")
        del path[depth:]
        if body.startswith("truncated branch"):
            leaves.append(Leaf(tuple(path), None))
            continue
        c = _CLASS.match(body)
        if c:
            w = tuple(float(x) for x in c.group(1).split(",")) if c.group(1) else None
            leaves.append(Leaf(tuple(path), c.group(2).strip(), w))
            continue
        s = _SPLIT.match(body)
        if s:
            op: Literal["<=", ">"] = "<=" if s.group(2) == "<=" else ">"
            path.append(Split(s.group(1), op, float(s.group(3))))
            continue
        raise ValueError(f"unparseable rules line: {ln!r}")
    return leaves


def collapse(leaves: list[Leaf]) -> list[Leaf]:
    """Merge sibling leaves (same parent path, complementary last split) with the same class into
    their parent, repeatedly. The covered region is unchanged; the rule gets simpler."""
    out = list(leaves)
    changed = True
    while changed:
        changed = False
        for i, a in enumerate(out):
            for j in range(i + 1, len(out)):
                b = out[j]
                if (a.cls is not None and a.cls == b.cls and a.path and len(a.path) == len(b.path)
                        and a.path[:-1] == b.path[:-1]
                        and (a.path[-1].feature, a.path[-1].threshold) == (b.path[-1].feature, b.path[-1].threshold)
                        and {a.path[-1].op, b.path[-1].op} == {"<=", ">"}):
                    out[i] = Leaf(a.path[:-1], a.cls)
                    del out[j]
                    changed = True
                    break
            if changed:
                break
    return out


def intervals(path: tuple[Split, ...]) -> dict[str, tuple[float, float]] | None:
    """Per-feature half-open interval (lo, hi] implied by a path; None if the path is empty-set."""
    iv: dict[str, tuple[float, float]] = {}
    for s in path:
        lo, hi = iv.get(s.feature, (-math.inf, math.inf))
        if s.op == "<=":
            hi = min(hi, s.threshold)
        else:
            lo = max(lo, s.threshold)
        if lo >= hi:
            return None
        iv[s.feature] = (lo, hi)
    return iv


def _positive(cls: str | None) -> bool:
    return cls in ("1", "1.0", "True")


# ---------------------------------------------------------------- shared helpers
def _r(x: Any, n: int = DECIMALS) -> float | None:
    if x is None:
        return None
    x = float(x)
    return round(x, n) if math.isfinite(x) else None


def _get(d: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def dossier_sha256(doc: dict) -> str:
    """Content hash of a dossier (sorted-key canonical JSON), independent of file whitespace."""
    return hashlib.sha256(orjson.dumps(doc, option=orjson.OPT_SORT_KEYS | orjson.OPT_SERIALIZE_NUMPY)).hexdigest()


def _refuse(refusals: list[dict] | None, wallet: str, kind: str, scope: str, reason: str, **kw) -> None:
    if refusals is not None:
        refusals.append({"wallet": wallet, "kind": kind, "scope": scope, "reason": reason, **kw})


def _btc_horizon(doc: dict) -> tuple[str, dict]:
    """Dominant BTC horizon from ``mechanism.mix`` (the policy panel pools BTC 5m and 15m)."""
    mix = _get(doc, "mechanism", "mix") or {}
    btc = {k.split("-", 1)[1]: int(v) for k, v in mix.items() if k.startswith("btc-") and k.split("-", 1)[1] in HZ_LEN}
    if not btc:
        raise Refused("no BTC 5m/15m fills: fair-v1 (every genome feature set) is BTC-only")
    hz = sorted(btc, key=lambda h: (-btc[h], h))[0]
    tot = sum(int(v) for v in mix.values())
    return hz, {"horizon": hz, "btc_fills_by_horizon": dict(sorted(btc.items())),
                "horizon_share_of_btc": _r(btc[hz] / sum(btc.values())),
                "btc_share_of_fills": _r(sum(btc.values()) / tot) if tot else None}


def _universe(hz: str, q: dict | None, what: str) -> tuple[Universe, dict]:
    """tte_min_s = p10 of the wallet's tte at its fills, tau_min_s = L - p90 (both clamped to the
    window). The dossier's tte quantiles pool every asset and horizon the wallet trades."""
    L = HZ_LEN[hz]
    if not q or q.get("p10") is None or q.get("p90") is None:
        return (Universe(venue="pmus", assets=("btc",), horizons=(hz,)),
                {"tau_min_s": f"default 0: no {what} quantiles", "tte_min_s": f"default 0: no {what} quantiles"})
    tte_min = int(min(max(math.floor(q["p10"]), 0), L - 1))
    tau_min = int(min(max(L - math.ceil(q["p90"]), 0), L - 1))
    return (Universe(venue="pmus", assets=("btc",), horizons=(hz,), tau_min_s=tau_min, tte_min_s=tte_min),
            {"tau_min_s": f"derived: {L} - ceil(p90 {what}={q['p90']})",
             "tte_min_s": f"derived: floor(p10 {what}={q['p10']})",
             "tte_quantiles_pooled": {k: q.get(k) for k in ("p10", "p50", "p90", "n")}})


def _finish(g: Genome) -> Genome:
    errs = validate(g)
    if errs:
        raise Refused("genome fails validate(): " + "; ".join(errs))
    return dataclasses.replace(g, notes=describe(g))


def _coverage(doc: dict) -> dict:
    c = doc.get("coverage") or {}
    return {k: c.get(k) for k in ("fills", "btc_fills", "fair_share_btc", "book_share", "print_ms_share",
                                  "resolved_share")}


# ---------------------------------------------------------------- taker
def _map_interval(feat: str, lo: float, hi: float, inv_scale: float | None) -> list[Condition]:
    m = FEATURE_MAP.get(feat)
    if m is None:
        raise Refused(f"dossier feature {feat!r} has no genome equivalent")
    scale = m["scale"]
    if m["kind"] == "inventory":
        if inv_scale is None:
            raise Refused("inv split but the wallet's shares per order is unknown: no DOPPEL-scale threshold")
        scale = inv_scale
    out = []
    if math.isfinite(lo):
        out.append(Condition(m["genome"], ">", float(round(lo * scale, DECIMALS))))
    if math.isfinite(hi):
        v = hi * scale - (FEE_BOUND_C if m["kind"] == "net_edge" else 0.0)
        out.append(Condition(m["genome"], "<=", float(round(v, DECIMALS))))
    return out


def _rule_conditions(iv: dict[str, tuple[float, float]], inv_scale: float | None, u: Universe,
                     hz: str) -> tuple[Condition, ...]:
    if not iv:
        raise Refused("leaf has no splits (would fire every second)")
    if set(iv) <= CLOCK_FEATURES:
        raise Refused("clock-only leaf (tte): a timing rule with no market-state condition")
    unmapped = sorted(f for f in iv if f not in FEATURE_MAP)
    if unmapped:
        raise Refused(f"no genome equivalent for {unmapped}: whole leaf refused (dropping the split "
                      "would widen the rule)")
    if "tte" in iv:
        lo, hi = iv["tte"]
        u_lo, u_hi = u.tte_min_s, HZ_LEN[hz] - u.tau_min_s
        if not (max(lo, u_lo - 1e-9) < min(hi, u_hi)):
            raise Refused(f"tte interval ({lo:g}, {hi:g}] does not intersect the universe "
                          f"[{u_lo}, {u_hi}] s")
    conds: list[Condition] = []
    for feat in sorted(iv):
        conds += _map_interval(feat, *iv[feat], inv_scale)
    return tuple(sorted(conds, key=lambda c: (c.feature, 0 if c.op == ">" else 1, c.value)))


def _fit_ok(t: Any) -> bool:
    return isinstance(t, dict) and t.get("auc_tree") is not None and bool(t.get("rules"))


def taker_genomes(doc: dict, *, mechanism: str | None = None, refusals: list[dict] | None = None) -> list[Genome]:
    """Taker genome from the dossier's policy inversion (``policy.took_any`` = when the wallet
    takes, ``policy.side_given_took`` = which side). Returns [] (with refusals) when the trees do
    not support a rule."""
    wallet = str(doc.get("wallet", "?"))
    try:
        return [_taker(doc, wallet, mechanism, refusals)]
    except Refused as e:
        _refuse(refusals, wallet, "taker", "genome", str(e))
        return []


def _taker(doc: dict, wallet: str, mechanism: str | None, refusals: list[dict] | None) -> Genome:
    pol = doc.get("policy") or {}
    ta = pol.get("took_any")
    if not _fit_ok(ta):
        raise Refused(f"no fitted took_any tree (policy verdict {pol.get('verdict')!r}, "
                      f"{_get(pol, 'reason') or _get(ta, 'result') or _get(ta, 'status') or _get(ta, 'reason')})")
    assert isinstance(ta, dict)
    try:
        took_leaves = collapse(parse_tree_rules(ta["rules"]))
        side_tree = collapse(parse_tree_rules(_get(pol, "side_given_took", "rules") or ""))
    except ValueError as e:
        raise Refused(f"rules text unparseable: {e}") from None
    if ta["auc_tree"] < MIN_AUC_TREE:
        raise Refused(f"took_any auc_tree {ta['auc_tree']} < {MIN_AUC_TREE}")
    if (ta.get("train_pos") or 0) < MIN_TRAIN_TAKES or (ta.get("test_pos") or 0) < MIN_TEST_TAKES:
        raise Refused(f"support: train_pos {ta.get('train_pos')} / test_pos {ta.get('test_pos')} below "
                      f"{MIN_TRAIN_TAKES}/{MIN_TEST_TAKES}")
    if (_get(doc, "coverage", "btc_fills") or 0) < MIN_BTC_FILLS:
        raise Refused(f"btc fills {_get(doc, 'coverage', 'btc_fills')} < {MIN_BTC_FILLS}")
    hz, hz_ev = _btc_horizon(doc)
    u, u_ev = _universe(hz, _get(doc, "mechanism", "tte_s_taker"), "taker tte_s")

    # which side: a usable side tree, else a one-sided wallet, else no genome
    sg = pol.get("side_given_took") or {}
    n_up = int(sg.get("train_up") or 0) + int(sg.get("test_up") or 0)
    n_dn = int(sg.get("train_down") or 0) + int(sg.get("test_down") or 0)
    side_leaves: list[tuple[Literal["up", "down"], Leaf | None]]
    if (_fit_ok(sg) and sg["auc_tree"] >= MIN_AUC_TREE
            and min(int(sg.get("train_up") or 0), int(sg.get("train_down") or 0)) >= MIN_SIDE_TAKES):
        side_mode = "side_tree"
        side_leaves = []
        for lf in side_tree:
            if lf.cls is None:
                _refuse(refusals, wallet, "taker", "leaf", "truncated side-tree branch",
                        path=[s.text() for s in lf.path])
                continue
            side_leaves.append(("up" if _positive(lf.cls) else "down", lf))
    elif n_up + n_dn > 0 and min(n_up, n_dn) / (n_up + n_dn) <= ONE_SIDED_MAX_MINORITY:
        side_mode = f"one_sided:{'up' if n_up >= n_dn else 'down'}"
        side_leaves = [("up" if n_up >= n_dn else "down", None)]
    else:
        raise Refused(f"side not explained: takes up={n_up} down={n_dn}, side tree "
                      f"auc_tree={sg.get('auc_tree')} (need >= {MIN_AUC_TREE}) or "
                      f"{sg.get('result') or sg.get('status') or 'unfitted'}")

    wallet_size = _get(doc, "orders", "shares_per_order", "p50")
    size = float(min(TAKER_SIZE_MAX, max(1.0, math.floor(wallet_size)))) if wallet_size else TAKER_SIZE_MAX
    inv_scale = _r(size / wallet_size, 6) if wallet_size else None
    windows = _get(doc, "mechanism", "windows") or 0
    t_orders = _get(doc, "orders", "taker_orders") or 0
    per_win = t_orders / windows if windows else None
    max_entries = int(min(TAKER_MAX_ENTRIES, max(1, math.ceil(per_win)))) if per_win else TAKER_MAX_ENTRIES

    rules: list[TakerRule] = []
    rule_ev: list[dict] = []
    refused_ev: list[dict] = []
    seen: set[tuple] = set()

    def refuse_leaf(reason: str, desc: list[str], side: str | None = None) -> None:
        refused_ev.append({"side": side, "path": desc, "reason": reason})
        _refuse(refusals, wallet, "taker", "leaf", reason, side=side, path=desc)

    for ti, tl in enumerate(took_leaves):
        desc = [s.text() for s in tl.path]
        if tl.cls is None:
            refuse_leaf("truncated took-tree branch", desc)
            continue
        if not _positive(tl.cls):
            continue
        iv = intervals(tl.path)
        if iv is None:
            refuse_leaf("empty interval at the printed 2 dp thresholds (rounding collision)", desc)
            continue
        # Side. The side tree was fit on took rows only, so conjoining its leaves with a took leaf
        # creates regions neither tree saw (e.g. both edges negative). A took leaf gets a side only
        # if every side leaf it intersects predicts the same side; the rule is then the took leaf.
        hits = [(s, sl) for s, sl in side_leaves if sl is None or intervals(tl.path + sl.path) is not None]
        sides = sorted({s for s, _ in hits})
        if len(sides) != 1:
            refuse_leaf(f"side ambiguous inside this took leaf: the side tree predicts {sides or 'nothing'} "
                        "over its region (it splits on features the took leaf does not bound); per-leaf "
                        "support would be needed to choose", desc)
            continue
        side: Literal["up", "down"] = "up" if sides[0] == "up" else "down"
        try:
            when = _rule_conditions(iv, inv_scale, u, hz)
        except Refused as e:
            refuse_leaf(str(e), desc, side)
            continue
        key = (side, when)
        if key in seen:
            continue
        seen.add(key)
        rules.append(TakerRule(side=side, when=when, size=size, limit_slippage_c=0.0,
                               cooldown_s=TAKER_COOLDOWN_S, max_entries_per_window=max_entries))
        rule_ev.append({"side": side, "dossier_path": desc, "took_leaf": ti,
                        "dossier_intervals": {k: [_r(a), _r(b)] for k, (a, b) in sorted(iv.items())},
                        "side_leaves": [[x.text() for x in sl.path] for _, sl in hits if sl is not None]})
    if not rules:
        raise Refused(f"no took leaf survived ({len(refused_ev)} refused)")

    m = doc.get("mechanism") or {}
    evidence = {
        "extractor": "doppel.strategy.extract v1",
        "evidence_scope": doc.get("evidence_scope", "Retrospective dossier. Decision-time inputs unverified."),
        "wallet": wallet,
        "policy": {"verdict": pol.get("verdict"), "split": pol.get("split"),
                   "took_any": {k: ta.get(k) for k in ("auc_hgb", "auc_tree", "train_pos", "test_pos",
                                                       "features_used", "features_dropped")},
                   "side_given_took": {k: sg.get(k) for k in ("auc_hgb", "auc_tree", "train_up", "train_down",
                                                              "test_up", "test_down", "result")}},
        "side_mode": side_mode,
        "rules": rule_ev,
        "refused_leaves": refused_ev,
        "leaf_support": ("unknown: export_text carries no per-leaf counts (min_samples_leaf=20 by "
                         "construction; class_weight=balanced, so a class-1 leaf means above base rate, "
                         "not majority)"),
        "thresholds": "export_text prints 2 dp: edges resolve to 1 c, prices to 0.01",
        "mapping": {f: FEATURE_MAP[f]["note"] for f in sorted({k for r in rule_ev for k in r["dossier_intervals"]})},
        "fee_bound_c": FEE_BOUND_C,
        "universe": {**hz_ev, **u_ev},
        "fields": {
            "size": f"derived: min({TAKER_SIZE_MAX:g}, wallet shares/order p50={wallet_size})",
            "max_entries_per_window": f"derived: min({TAKER_MAX_ENTRIES}, ceil(taker orders {t_orders} / "
                                      f"windows {windows}))",
            "cooldown_s": f"default {TAKER_COOLDOWN_S}: dossier has no inter-entry interval statistics",
            "limit_slippage_c": "default 0",
            "inventory_scale": inv_scale,
            "exit": "default hold to settlement: dossier does not identify exits",
            "risk": "DOPPEL envelope (H1)",
        },
        "edge_taker": _get(doc, "edge", "taker"),
        "markouts_taker": _get(doc, "markouts", "taker"),
        "mechanism": {k: m.get(k) for k in ("fills", "notional", "maker_share", "buy_share", "windows",
                                            "tte_s_taker", "entry_price_buys")},
        "coverage": _coverage(doc),
    }
    g = Genome(name=f"clone-{wallet[:10]}-taker", mode="taker", universe=u,
               source=Source(kind="clone", wallets=(wallet,), dossier_sha256=(dossier_sha256(doc),),
                             mechanism=mechanism),
               taker=tuple(rules), risk=DOPPEL_RISK, evidence=evidence)
    return _finish(g)


# ---------------------------------------------------------------- maker
def _ols(x: np.ndarray, y: np.ndarray) -> dict | None:
    n = len(x)
    if n < 3 or float(np.std(x)) == 0.0:
        return None
    X = np.column_stack([np.ones(n), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    s2 = float(resid @ resid) / (n - 2)
    cov = s2 * np.linalg.inv(X.T @ X)
    se = math.sqrt(max(float(cov[1, 1]), 0.0))
    return {"n": n, "intercept_c": _r(beta[0]), "slope_c_per_share": _r(beta[1], 6),
            "t": _r(beta[1] / se) if se > 0 else None}


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def maker_genomes(doc: dict, *, orders: list[dict] | None = None, fills: list[dict] | None = None,
                  mechanism: str | None = None, refusals: list[dict] | None = None) -> list[Genome]:
    """Maker genome from the dossier's maker order lifecycle and inventory policy. ``orders`` and
    ``fills`` are the dossier's per-order / per-fill tables (optional; they make the half-spread
    exact and the skew estimable)."""
    wallet = str(doc.get("wallet", "?"))
    try:
        return [_maker(doc, wallet, orders, fills, mechanism)]
    except Refused as e:
        _refuse(refusals, wallet, "maker", "genome", str(e))
        return []


def _maker(doc: dict, wallet: str, orders: list[dict] | None, fills: list[dict] | None,
           mechanism: str | None) -> Genome:
    m = doc.get("mechanism") or {}
    o = doc.get("orders") or {}
    inv = doc.get("inventory") or {}
    if (m.get("maker_share") or 0) < MIN_MAKER_SHARE:
        raise Refused(f"maker_share {m.get('maker_share')} < {MIN_MAKER_SHARE}")
    if (o.get("maker_orders") or 0) < MIN_MAKER_ORDERS:
        raise Refused(f"maker orders {o.get('maker_orders')} < {MIN_MAKER_ORDERS}")
    if (_get(doc, "coverage", "btc_fills") or 0) < MIN_BTC_FILLS:
        raise Refused(f"btc fills {_get(doc, 'coverage', 'btc_fills')} < {MIN_BTC_FILLS}")
    paired = inv.get("paired_share_of_gross")
    if paired is None or paired < COMPLETE_SET_MIN_PAIRED:
        raise Refused(f"not a complete-set maker (paired_share_of_gross {paired} < {COMPLETE_SET_MIN_PAIRED}; "
                      "0.5 = all paired). A directional maker's quote side is a per-window decision the "
                      "dossier does not invert (no per-side maker split): refusing sides")
    hz, hz_ev = _btc_horizon(doc)
    u, u_ev = _universe(hz, m.get("tte_s_maker"), "maker fill tte_s")
    fields: dict[str, str] = {}

    # quote reference: fair if its distances are tighter (median) and more stable (p10-p90)
    qm: dict = o.get("maker_mid_dist_c") or {}
    qf: dict = o.get("maker_fair_dist_c") or {}

    def usable(q: dict) -> bool:
        return all(q.get(k) is not None for k in ("p10", "p50", "p90")) and (q.get("n") or 0) >= MIN_MAKER_ORDERS

    if usable(qf) and usable(qm):
        tight = abs(qf["p50"]) <= abs(qm["p50"]) + REF_TIGHT_TOL_C
        stable = (qf["p90"] - qf["p10"]) < (qm["p90"] - qm["p10"])
        ref: Literal["fair", "mid"] = "fair" if tight and stable else "mid"
        fields["quote_ref"] = (f"derived: fair |p50| {abs(qf['p50'])} vs mid {abs(qm['p50'])} (tol {REF_TIGHT_TOL_C}), "
                               f"p10-p90 width fair {_r(qf['p90'] - qf['p10'])} vs mid {_r(qm['p90'] - qm['p10'])}")
    elif usable(qm):
        ref = "mid"
        fields["quote_ref"] = "derived: mid (fair distances missing or < MIN_MAKER_ORDERS; fair is BTC-only)"
    elif usable(qf):
        ref = "fair"
        fields["quote_ref"] = "derived: fair (mid distances missing)"
    else:
        raise Refused("no maker distance quantiles with enough orders (mid or fair)")
    q = qf if ref == "fair" else qm
    col = "fair_dist_c" if ref == "fair" else "mid_dist_c"

    # half-spread = median absolute distance at placement (first fill)
    if q["p50"] <= 0:
        raise Refused(f"median maker distance to {ref} is {q['p50']} c (not passive): {ref} is not this "
                      "wallet's quote reference")
    tab = [abs(v) for v in (_num(r.get(col)) for r in (orders or []) if r.get("first_role") == "maker")
           if v is not None]
    if len(tab) >= MIN_MAKER_ORDERS:
        h = float(np.median(np.asarray(tab)))
        fields["half_spread_c"] = f"derived: median |{col}| over {len(tab)} maker orders (orders table)"
    elif q["p10"] >= 0:
        h = float(q["p50"])
        fields["half_spread_c"] = (f"derived: p50 {col} (p10 {q['p10']} >= 0, so >= 90 % of orders are passive "
                                   "and the median distance ~= median |distance|)")
    else:
        raise Refused(f"{col} changes sign (p10 {q['p10']}, p90 {q['p90']}): median |distance| is not "
                      "identified from quantiles; pass the orders table")
    h = round(h, 2)

    life = o.get("lifetime_s_maker")
    if isinstance(life, dict) and life.get("p90") is not None and life["p90"] >= QUOTE_AGE_S[0]:
        age = int(min(math.ceil(life["p90"]), QUOTE_AGE_S[1]))
        fields["max_quote_age_s"] = (f"derived: ceil(p90 maker order lifetime {life['p90']} s), capped at "
                                     f"{QUOTE_AGE_S[1]}; lifetime is first-to-last FILL, a lower bound")
    else:
        age = 30
        fields["max_quote_age_s"] = (f"default 30: maker lifetime p90 {_get(life, 'p90')} s (first-to-last fill; "
                                     f"single-fill orders give 0) is below {QUOTE_AGE_S[0]} s and carries no "
                                     "information about quote age")
    fields["requote_threshold_c"] = ("default 1.0: not identified (placement and cancel times are not on chain; "
                                     "lifetimes measure fill span, not reference moves)")

    # skew: quote equation => dist_c = h + k * udir * inv_before (cents vs ref, wallet shares)
    wallet_size = _get(o, "shares_per_order", "p50")
    size = float(min(MAKER_SIZE_MAX, max(1.0, math.floor(wallet_size)))) if wallet_size else MAKER_SIZE_MAX
    fcol = "edge_c" if ref == "fair" else "vs_mid_c"
    pts = [(float(r["udir"]) * float(r["inv_before"]), float(r[fcol])) for r in (fills or [])
           if r.get("role") == "maker" and _num(r.get(fcol)) is not None and _num(r.get("inv_before")) is not None
           and _num(r.get("udir")) is not None]
    reg = _ols(np.asarray([p[0] for p in pts]), np.asarray([p[1] for p in pts])) if len(pts) >= MIN_SKEW_FILLS else None
    corr = _get(inv, "skew_vs_inventory", "maker")
    if reg and reg["t"] is not None and abs(reg["t"]) >= MIN_SKEW_T and wallet_size:
        skew = round(reg["slope_c_per_share"] * wallet_size / size, DECIMALS)
        fields["skew_c_per_contract"] = (f"derived: OLS {fcol} ~ udir*inv_before over {reg['n']} maker fills, "
                                         f"slope {reg['slope_c_per_share']} c/share (t {reg['t']}) x wallet "
                                         f"shares/order {wallet_size} / DOPPEL size {size:g}")
    else:
        skew = 0.0
        why = ("fills table absent" if fills is None else
               f"{len(pts)} usable maker fills" if reg is None else f"slope t {reg['t']} < {MIN_SKEW_T}")
        fields["skew_c_per_contract"] = (f"default 0: magnitude not identified ({why}); the JSON gives only "
                                         f"corr(udir, -inv) = {corr}")
    fields["sides"] = f"derived: both (complete-set maker, paired_share_of_gross {paired})"
    fields["pause_when"] = ("none: dossier markouts are split by role only; no evidence of adverse selection "
                            "after fast BRTI moves")
    fields["min_edge_vs_fair_c"] = "default 0"
    fields["size"] = f"derived: min({MAKER_SIZE_MAX:g}, wallet shares/order p50={wallet_size})"
    fields["exit"] = "default hold to settlement"
    fields["risk"] = "DOPPEL envelope (H1)"

    rule = MakerRule(quote_ref=ref, half_spread_c=h, size=size, sides="both", skew_c_per_contract=skew,
                     requote_threshold_c=1.0, max_quote_age_s=age, pause_when=(), min_edge_vs_fair_c=0.0)
    evidence = {
        "extractor": "doppel.strategy.extract v1",
        "evidence_scope": doc.get("evidence_scope", "Retrospective dossier. Decision-time inputs unverified."),
        "wallet": wallet,
        "fields": fields,
        "distances": {"mid": qm or None, "fair": qf or None,
                      "caveats": ["placement = first fill (placement times are not on chain)",
                                  "mid includes the wallet's own resting quote",
                                  "fair distances are BTC-only; mid distances pool every asset"]},
        "lifetime_s_maker": life,
        "skew_regression": reg,
        "inventory": {k: inv.get(k) for k in ("windows", "flat_close_share", "paired_share_of_gross",
                                              "abs_net_close_over_gross", "peak_inventory_shares")}
                     | {"skew_vs_inventory_maker": corr},
        "edge_maker": _get(doc, "edge", "maker"),
        "markouts_maker": _get(doc, "markouts", "maker"),
        "counterparties": {k: _get(doc, "counterparties", k) for k in ("maker_notional", "distinct", "null_share")},
        "mechanism": {k: m.get(k) for k in ("fills", "notional", "maker_share", "buy_share", "windows",
                                            "tte_s_maker")},
        "universe": {**hz_ev, **u_ev},
        "coverage": _coverage(doc),
    }
    g = Genome(name=f"clone-{wallet[:10]}-maker", mode="maker", universe=u,
               source=Source(kind="clone", wallets=(wallet,), dossier_sha256=(dossier_sha256(doc),),
                             mechanism=mechanism),
               maker=rule, risk=DOPPEL_RISK, evidence=evidence)
    return _finish(g)


# ---------------------------------------------------------------- both
def extract(doc: dict, *, orders: list[dict] | None = None, fills: list[dict] | None = None,
            mechanism: str | None = None) -> tuple[list[Genome], list[dict]]:
    """(candidate genomes, refusals) for one dossier."""
    refusals: list[dict] = []
    gs = taker_genomes(doc, mechanism=mechanism, refusals=refusals)
    gs += maker_genomes(doc, orders=orders, fills=fills, mechanism=mechanism, refusals=refusals)
    return gs, refusals
