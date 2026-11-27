"""Strategy simulator: simulation-based inference of a wallet's rule from its PUBLIC footprint
(docs/SIMULATOR.md; plan docs/STATE-AND-PLAN-20261127.md section 4, Phase C).

    doppel-simulate table    --data TAPE --tokens T --stream clob_btc_5m --from F --to T --n 150 --out table.jsonl
    doppel-simulate infer    --table table.jsonl --tape TAPE --orders orders.jsonl --tokens T --wallet W --out DIR
    doppel-simulate validate --data TAPE --tokens T --stream clob_btc_5m --from F --to T --n 150 --out DIR

THE IDEA. Candidate rules (a family plus parameters drawn from an explicit prior) are simulated on
the real recorded book with the answer-key lab's fill path (``answerkey.Sim``: the twin's
``QuoteSim``, own quotes injected, pessimistic queue). Each candidate leaves the same kinds of
public evidence a wallet leaves (``answerkey.ordertape_rows``: signed-order rows of FILLED orders
only, plus the public book at each fill). ``footprint`` turns that evidence into summary
statistics, and the SAME function turns a real wallet's ``orders.jsonl`` plus its tape into the
observed statistics. Rejection ABC on standardized distances keeps the candidates whose footprint
is nearest the observed one; their parameters are the posterior.

FAMILIES (``FAMILIES``; answerkey's policies plus two gates of our own in ``Gated``):
  midpoint_maker  h_c, sizes, timer_s, move_c, band_c
  fair_maker      h_c, k_c_per_contract, size, timer_s, move_c (inert: FairMaker never reads it), band_c
  complete_set    m_c, size, max_age_s, band_c
  touch_ladder    levels_c, sizes, max_age_s, band_c
  random_taker    rate_per_s, sizes
  null            ``RandomPolicy``: Poisson actions at a random token, price and size, a share of
                  them takes; the "random policy at matched activity" every rule must beat.
``band_c``: quote only while |mid - 50| <= band_c (participation band), else cancel everything.
``max_age_s``: cancel a live quote older than this (cancel age). random_taker has no rule beyond
its activity (answerkey's decoy), so it belongs to the NULL CLASS with ``null``.

FOOTPRINT (``FEATURES``, ``footprint``): fills and filled orders per eligible window, markets
entered vs eligible, fill time within the window, fill price relative to the public touch and mid
at the fill, signed full size (median, distinct sizes, spread), filled vs full size, age at the
first fill (block time - signed created_ms), inventory path quantiles, side mix, one-sidedness per
window, taker share, the share of filled shares that resolved winners and P&L per share. Outcome
statistics use resolved payouts AFTER the fact: they describe the footprint and are never a
decision input. Ordermatch placement rows are NOT used: they need the rewritten tape on disk for
every candidate (see Limits in docs/SIMULATOR.md).

INFERENCE (``infer``): scale = robust spread of each statistic over the whole reference table;
distance = RMS of standardized differences, each capped at 3 sd (a statistic missing on one side
counts 3 sd). Per family accept the k nearest (k = accept x n). Posterior median and 90 % interval per parameter
(90 % credible set for a choice). Identifiability: posterior width / the width of a random k-subset
of the prior draws: <= 0.5 IDENTIFIED, <= 0.8 PARTIAL, else UNIDENTIFIED. Model evidence = shares of
the pooled k nearest by family (equal n), with Dirichlet 90 % intervals. Adequacy: the observed
mean k-NN distance against the family's own leave-one-out distribution (p >= 0.05). EXPLAINED only
when the best rule family is adequate, more probable than the null class and nearer than every
null-class family; otherwise "not explained by observed inputs" and no posterior is reported.
Decision-weighted recovery: each parameter belongs to one of six decisions (market, timing, side,
price, size, exit); concentration = 1 - width ratio; weight = first-order share of simulated P&L
variance the decision's parameters explain in the family's reference table.

NO LOOK-AHEAD: policies decide from ``answerkey.Ctx`` (book and own state received by the decision).
The footprint reads the future (fills, payouts) by design; it is analysis, never a decision input.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import resource
import sys
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import orjson

from .answerkey import (MS_NS, NS, POLICIES, Ctx, Order, Policy, Sim, _cancel_all, _h, _stream_records, _tops, addr,
                        ordertape_rows, place, write_public)
from .flow import _ts
from .intlbook import IntlBook, Print, px
from .intltwin import IntlWindow, windows_from_tokens
from .ledger import TokenMap
from .ordermatch import filled_shares

RESERVED_SEEDS = frozenset({271031, 271067, 271091})   # docs/ANSWERKEY.md exam seeds: never used here
DECISIONS = ("market", "timing", "side", "price", "size", "exit")
NULL_CLASS = ("null", "random_taker")
FLAG_ID, FLAG_PARTIAL = 0.5, 0.8
MISSING_Z = 3.0
PPC_PAD = 0.5


def _check_seed(seed: int) -> int:
    if seed in RESERVED_SEEDS:
        raise ValueError(f"seed {seed} is reserved for the answer-key exam")
    return seed


def derive_seed(*parts) -> int:
    """A seed from (run seed, family, index): independent of execution order; never a reserved seed."""
    s = int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:8], 16) % (2**31 - 2) + 1
    return s + 1 if s in RESERVED_SEEDS else s


# ---------------------------------------------------------------- priors and families
@dataclass(frozen=True)
class Prior:
    kind: str                 # uniform | loguniform | choice
    lo: float = 0.0
    hi: float = 1.0
    values: tuple = ()

    def draw(self, rng: np.random.Generator):
        if self.kind == "uniform":
            return float(rng.uniform(self.lo, self.hi))
        if self.kind == "loguniform":
            return float(math.exp(rng.uniform(math.log(self.lo), math.log(self.hi))))
        v = self.values[int(rng.integers(len(self.values)))]
        return list(v) if isinstance(v, tuple) else v

    def t(self, x: float) -> float:
        """Width space: log for a log-uniform prior."""
        return math.log(x) if self.kind == "loguniform" else float(x)

    def index(self, v) -> int:
        key = tuple(v) if isinstance(v, list) else v
        return list(self.values).index(key)

    def spec(self) -> dict:
        return {"kind": self.kind, "lo": self.lo, "hi": self.hi, "values": [list(v) if isinstance(v, tuple) else v
                                                                          for v in self.values]}


def U(lo, hi):
    return Prior("uniform", lo, hi)


def LU(lo, hi):
    return Prior("loguniform", lo, hi)


def C(*vals):
    return Prior("choice", values=tuple(tuple(v) if isinstance(v, list) else v for v in vals))


@dataclass(frozen=True)
class Family:
    name: str
    key: str                      # answerkey.POLICIES key, or "null"
    prior: dict                   # parameter -> Prior
    decision: dict                # parameter -> one of DECISIONS
    fixed: dict                   # parameters held constant

    def draw(self, rng: np.random.Generator) -> dict:
        return {k: p.draw(rng) for k, p in self.prior.items()}


SIZE_SETS = C([10.0, 25.0, 50.0], [20.0], [5.0, 10.0, 15.0], [50.0, 100.0])
FAMILIES = {f.name: f for f in (
    Family("midpoint_maker", "a",
           {"h_c": U(0.5, 5.0), "sizes": SIZE_SETS, "timer_s": U(1.0, 20.0), "move_c": C(1.0, 2.0, 3.0),
            "band_c": U(10.0, 50.0)},
           {"h_c": "price", "sizes": "size", "timer_s": "timing", "move_c": "exit", "band_c": "market"}, {}),
    Family("fair_maker", "b",
           {"h_c": U(1.0, 6.0), "k_c_per_contract": U(0.0, 0.15), "size": C(10.0, 20.0, 40.0),
            "timer_s": U(2.0, 30.0), "move_c": C(1.0, 2.0, 3.0), "band_c": U(10.0, 50.0)},
           {"h_c": "price", "k_c_per_contract": "side", "size": "size", "timer_s": "timing", "move_c": "exit",
            "band_c": "market"}, {}),
    Family("complete_set", "c",
           {"m_c": U(0.0, 6.0), "size": C(10.0, 25.0, 50.0), "max_age_s": LU(1.0, 60.0), "band_c": U(10.0, 50.0)},
           {"m_c": "market", "size": "size", "max_age_s": "exit", "band_c": "market"}, {}),
    Family("touch_ladder", "d",
           {"levels_c": C([0, 1], [0, 1, 2], [0, 2, 4], [0, 1, 2, 3]),
            "sizes": C([97.0, 98.0, 99.0, 100.0], [20.0, 30.0], [50.0]),
            "max_age_s": LU(1.0, 60.0), "band_c": U(10.0, 50.0)},
           {"levels_c": "price", "sizes": "size", "max_age_s": "exit", "band_c": "market"}, {}),
    Family("random_taker", "e", {"rate_per_s": LU(0.005, 0.1), "sizes": SIZE_SETS},
           {"rate_per_s": "timing", "sizes": "size"}, {}),
    Family("null", "null", {"rate_per_s": LU(0.01, 1.0), "taker_share": U(0.0, 1.0), "life_s": LU(1.0, 60.0)},
           {"rate_per_s": "timing", "taker_share": "side", "life_s": "exit"}, {}),
)}
GATE_KEYS = ("band_c", "max_age_s")


class Gated(Policy):
    """An answerkey policy behind two gates: participation band and cancel age."""

    def __init__(self, inner: Policy, band_c: float | None, max_age_s: float | None):
        self.inner, self.band_c, self.max_age_s = inner, band_c, max_age_s
        self.rng, self.state, self.taker, self.name = inner.rng, inner.state, inner.taker, inner.name
        self.params = inner.params

    def refs(self, c: Ctx) -> dict:
        return self.inner.refs(c)

    def decide(self, c: Ctx) -> list[tuple]:
        mid = c.mid_c()
        if self.band_c is not None and mid is not None and abs(mid - 50.0) > self.band_c:
            return _cancel_all(c)
        acts = self.inner.decide(c)
        if self.max_age_s is not None:
            gone = {a[1] for a in acts if a[0] == "cancel"}
            acts += [("cancel", o.oid) for o in c.live if o.kind == "maker" and o.oid not in gone
                     and c.ms - o.created_ms > self.max_age_s * 1000]
        return acts


class RandomPolicy(Policy):
    """NULL: at Poisson times, a random token; with probability taker_share a FAK buy at the ask,
    else a bid 0..3 c behind the best bid; random size; each quote cancelled after an exponential life."""
    name = "null"
    params = {"rate_per_s": 0.1, "taker_share": 0.5, "life_s": 10.0, "sizes": [5.0, 10.0, 20.0, 50.0, 100.0]}

    def decide(self, c):
        p, s = self.params, self.state
        life = s.setdefault("life", {})
        out = [("cancel", o.oid) for o in c.live if o.kind == "maker" and c.ms >= life.get(o.oid, math.inf)]
        for o in c.live:
            life.setdefault(o.oid, o.created_ms + self.rng.exponential(p["life_s"]) * 1000)
        if "next" not in s:
            s["next"] = c.ms + self.rng.exponential(1 / p["rate_per_s"]) * 1000
        if c.ms < s["next"]:
            return out
        s["next"] = c.ms + self.rng.exponential(1 / p["rate_per_s"]) * 1000
        tok = c.win.up if self.rng.random() < 0.5 else c.win.down
        bb, ba = c.top[tok]
        size = float(self.rng.choice(p["sizes"]))
        if self.rng.random() < p["taker_share"]:
            return out if ba is None else out + [("take", tok, px(ba), size, "")]
        if bb is None:
            return out
        pc = round(100 * bb) - int(self.rng.integers(0, 4))
        return out + [place(tok, pc, size)] if 1 <= pc <= 99 else out


def make_policy(fam: Family, params: dict, seed: int) -> Policy:
    full = dict(fam.fixed) | dict(params)
    if fam.key == "null":
        pol: Policy = RandomPolicy(seed)
        pol.params = dict(RandomPolicy.params) | full
        return pol
    cls = POLICIES[fam.key]
    pol = cls(seed)
    pol.params = dict(cls.params) | {k: v for k, v in full.items() if k not in GATE_KEYS}
    if any(k in full for k in GATE_KEYS):
        return Gated(pol, full.get("band_c"), full.get("max_age_s"))
    return pol


# ---------------------------------------------------------------- the public book at a fill
def public_tops(levels: dict | None) -> tuple[float | None, float | None]:
    return _tops({k: v for k, v in (levels or {}).items() if v > 1e-9})


class FootSim(Sim):
    """``answerkey.Sim`` that also records the PUBLIC book (real levels + its own resting size) of both
    tokens at each of its fills, keyed by the fill's tx: what a replay of the public tape shows."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.touch: dict[str, dict] = {}

    def public_levels(self, token: str) -> dict | None:
        real = self.book.levels.get(token)
        if real is None:
            return None
        lv = dict(real)
        for src in (self.rest, self.consumed):
            for (a, p, s), v in src.items():
                if a == token:
                    lv[(p, s)] = lv.get((p, s), 0.0) + v
        return lv

    def both(self, token: str) -> dict:
        return {t: public_tops(self.public_levels(t)) for t in (token, self.mate[token])}

    def fill(self, o, ms, tw, q, via):
        self.touch["0x" + _h("tx", self.wallet, o.oid, len(o.fills))] = self.both(o.token)
        return super().fill(o, ms, tw, q, via)

    def land(self, tw):
        due = [p[3] for p in self.pending if p[0] <= tw and p[3] is not None and p[3].kind == "taker"]
        pre = {o.oid: self.both(o.token) for o in due if self.book.known(o.token)}
        out = super().land(tw)
        for o in due:
            if o.fills and o.oid in pre:
                self.touch[o.fills[0][3]] = pre[o.oid]
        return out


_DONE = Order("", "maker", "", False, "", 0.0, 0.0, "", 0, 0, 0, {}, 0.0, status="folded")


def slim(row: dict, mate: dict[str, str]) -> tuple | None:
    """One public order-tape row of the wallet as (order, token, limit, size, shares, block_ts, created_ms,
    role, tx) in BUY-equivalent terms (a SELL of a token at p is a BUY of its mate at 1 - p)."""
    tok, lim, size = row["token_id"], float(row["limit"]), float(row["size"])
    q = filled_shares(row)
    if row["side"] == "SELL":
        if tok not in mate:
            return None
        tok, lim = mate[tok], round(1 - lim, 6)
    return (row["order_id"], tok, lim, size, q, int(row["block_ts"]), int(row["created_ms"]), row.get("role"), row["tx"])


def simulate(fam: Family, params: dict, seed: int, root: Path, stream: str, wins: list[IntlWindow], t_from: int,
             t_to: int, fair=None) -> dict:
    """One candidate, one forward pass, nothing written. Finished windows are folded into slim public
    rows and their orders dropped, so memory is bounded by one window of orders."""
    _check_seed(seed)
    pol = make_policy(fam, params, seed)
    wallet = addr("simulate", fam.name, seed)
    sim = FootSim(pol, wallet, wins, fair if fam.key == "b" else None, seed)
    sim.t_stop = t_to
    rows: list[tuple] = []
    touch: dict[str, dict] = {}
    todo = sorted(wins, key=lambda w: w.end)
    first = 0

    def fold(upto_s: int) -> None:
        nonlocal first, todo
        done = {sim.slug_of[w.up] for w in todo if w.end < upto_s}
        if not done:
            return
        todo = [w for w in todo if sim.slug_of[w.up] not in done]
        idx = [i for i in range(first, len(sim.orders)) if sim.orders[i].slug in done]
        view = SimpleNamespace(orders=[sim.orders[i] for i in idx], wallet=wallet)
        for r in ordertape_rows(view, {}):  # type: ignore[arg-type]
            if r["maker"] == wallet:
                s = slim(r, sim.mate)
                if s is not None:
                    rows.append(s)
                    if s[8] in sim.touch:
                        touch[s[8]] = sim.touch.pop(s[8])
        for i in idx:
            sim.orders[i] = _DONE
        while first < len(sim.orders) and sim.orders[first] is _DONE:
            first += 1

    for rec in _stream_records(root, stream, t_from, t_to):
        tw = int(rec["tw"])
        while sim.pending and tw > t_from * NS and min(p[0] for p in sim.pending) < tw:
            sim.land(min(p[0] for p in sim.pending))
        if "seq" not in rec:
            sim.book.reset()
            continue
        if not t_from * NS <= tw < t_to * NS:
            continue
        raw = rec.get("raw") or ""
        obj = None
        if (rec.get("meta") or {}).get("ctl") is None and raw and raw[0] in "[{":
            try:
                obj = orjson.loads(raw)
            except orjson.JSONDecodeError:
                obj = None
        if obj is None:
            for _ in sim.book.feed_messages(rec):
                pass
        else:
            for m in obj if isinstance(obj, list) else [obj]:
                if isinstance(m, dict):
                    sim.message(m, tw)
        sim.decide(tw)
        if todo and todo[0].end < tw // NS:
            fold(tw // NS)
    fold(2**62)
    return {"rows": rows, "touch": touch, "wallet": wallet}


# ---------------------------------------------------------------- footprint
FEATURES = ("fills_pw", "orders_pw", "entered", "t_q10", "t_q50", "t_q90", "touch_q10", "touch_q50", "touch_q90",
            "mid_q10", "mid_q50", "mid_q90", "size_med", "size_n", "size_spread", "fillfrac", "full",
            "age_q10", "age_q50", "age_q90", "inv_q50", "inv_q90", "inv_end", "up_share", "onesided", "taker",
            "win", "pnl_c")
NAN = float("nan")


def _q(xs, q):
    return float(np.quantile(xs, q)) if len(xs) else NAN


def footprint(rows: list[tuple], touch: dict[str, dict], wins: list[IntlWindow], t_from: int, t_to: int) -> dict:
    """Summary statistics of a wallet's public evidence (slim rows + the public book at its fills).
    The same function serves simulated candidates and real wallets."""
    win_of = {t: w for w in wins for t in (w.up, w.down)}
    elig = [w for w in wins if w.start < t_to and w.end > t_from]
    rows = sorted((r for r in rows if r[1] in win_of), key=lambda r: (r[5], r[6], r[1], r[2], r[4]))
    n_elig = len(elig)
    out: dict[str, float] = dict.fromkeys(FEATURES, NAN)
    pnl = shares = 0.0
    if n_elig == 0:
        return {"x": out, "pnl_usd": 0.0, "shares": 0.0, "n_fills": 0}
    orders: dict[str, list] = {}
    by_win: dict[int, list[tuple]] = defaultdict(list)
    tq, tch, mid = [], [], []
    for r in rows:
        oid, tok, lim, size, q, bts, cms, role, tx = r
        w = win_of[tok]
        by_win[w.start].append(r)
        tq.append((bts - w.start) / (w.end - w.start))
        tops = (touch.get(tx) or {}).get(tok)
        if tops is not None:
            bb, ba = tops
            if bb is not None:
                tch.append(100 * (lim - bb))
            if bb is not None and ba is not None:
                mid.append(100 * lim - 50 * (bb + ba))
        o = orders.setdefault(oid, [size, 0.0, bts, cms])
        o[1] += q
        o[2] = min(o[2], bts)
        if w.payout_up is not None:
            pay = w.payout_up if tok == w.up else 1 - w.payout_up
            pnl += q * (pay - lim)
            shares += q
    n_fill = len(rows)
    out["fills_pw"] = math.log1p(n_fill / n_elig)
    out["orders_pw"] = math.log1p(len(orders) / n_elig)
    out["entered"] = len(by_win) / n_elig
    for k, v in zip(("t_q10", "t_q50", "t_q90"), (0.1, 0.5, 0.9)):
        out[k] = _q(tq, v)
    for name, xs in (("touch", tch), ("mid", mid)):
        for v in (10, 50, 90):
            out[f"{name}_q{v}"] = _q(xs, v / 100)
    if orders:
        sizes = np.array([o[0] for o in orders.values()])
        med = float(np.median(sizes))
        out["size_med"] = math.log(max(med, 1e-6))
        out["size_n"] = math.log(len({round(s, 2) for s in sizes}))
        out["size_spread"] = math.log(max(_q(sizes, 0.9), 1e-6) / max(_q(sizes, 0.1), 1e-6))
        frac = np.array([min(o[1] / o[0], 1.0) if o[0] > 0 else 1.0 for o in orders.values()])
        out["fillfrac"] = float(frac.mean())
        out["full"] = float((frac >= 1 - 1e-3).mean())
        ages = [math.log1p(max(o[2] - o[3] / 1000, 0.0)) for o in orders.values()]
        for k, v in zip(("age_q10", "age_q50", "age_q90"), (0.1, 0.5, 0.9)):
            out[k] = _q(ages, v)
        inv_path, inv_end, up_sh, one = [], [], 0.0, 0
        for start, rs in by_win.items():
            w = win_of[rs[0][1]]
            inv, by_tok = 0.0, defaultdict(float)
            for r in rs:
                inv += r[4] if r[1] == w.up else -r[4]
                by_tok[r[1]] += r[4]
                inv_path.append(abs(inv) / med)
            inv_end.append(abs(inv) / med)
            tot = sum(by_tok.values())
            up_sh += by_tok.get(w.up, 0.0)
            one += tot > 0 and max(by_tok.values()) / tot >= 0.9
        tot_sh = sum(r[4] for r in rows)
        out["inv_q50"], out["inv_q90"] = _q(inv_path, 0.5), _q(inv_path, 0.9)
        out["inv_end"] = _q(inv_end, 0.5)
        out["up_share"] = up_sh / tot_sh if tot_sh > 0 else NAN
        out["onesided"] = one / len(by_win)
        out["taker"] = sum(r[7] == "taker" for r in rows) / n_fill
    if shares > 0:
        win_sh = sum(r[4] for r in rows if (w := win_of[r[1]]).payout_up is not None
                     and (w.payout_up if r[1] == w.up else 1 - w.payout_up) > 0.5)
        out["win"] = win_sh / shares
        out["pnl_c"] = 100 * pnl / shares
    return {"x": out, "pnl_usd": round(pnl, 6), "shares": round(shares, 6), "n_fills": n_fill}


def observed_footprint(tape: Path, stream: str, orders: Path, tokens: Path, wallet: str, t_from: int,
                       t_to: int) -> dict:
    """A wallet's footprint from public files: its ``orders.jsonl`` rows (verified, maker == wallet) and
    the public book at each of its fill txs, replayed from ``tape``."""
    wins = universe(tokens, stream, t_from, t_to)
    mate = {w.up: w.down for w in wins} | {w.down: w.up for w in wins}
    rows = []
    with open(orders, "rb") as fh:
        for line in fh:
            if line.strip():
                r = orjson.loads(line)
                if r.get("maker") == wallet and r.get("verified") is not False:
                    s = slim(r, mate)
                    if s is not None:
                        rows.append(s)
    txs = {r[8] for r in rows}
    touch: dict[str, dict] = {}
    book = IntlBook()
    for rec in _stream_records(tape, stream, t_from, t_to):
        if "seq" not in rec:
            book.reset()
            continue
        if not t_from * NS <= int(rec["tw"]) < t_to * NS:
            continue
        for _, items in book.feed_messages(rec):
            for it in items:
                if isinstance(it, Print) and it.tx in txs and it.tx not in touch and it.asset in mate:
                    touch[it.tx] = {t: public_tops(book.levels.get(t)) if book.known(t) else None
                                    for t in (it.asset, mate[it.asset])}
    return footprint(rows, {k: {t: v for t, v in d.items() if v is not None} for k, d in touch.items()},
                     wins, t_from, t_to)


def universe(tokens: Path, stream: str, t_from: int, t_to: int) -> list[IntlWindow]:
    asset, hz = stream.split("_")[1], stream.split("_")[2]
    wins, _ = windows_from_tokens(TokenMap(tokens), (asset,), (hz,), t_from - 3600, t_to + 3600)
    return wins


# ---------------------------------------------------------------- reference table
_W: dict = {}


def _init(root: str, stream: str, tokens: str, t_from: int, t_to: int, need_fair: bool) -> None:
    _W.update(root=Path(root), stream=stream, t_from=t_from, t_to=t_to,
              wins=universe(Path(tokens), stream, t_from, t_to), fair=None)
    if need_fair:
        from .rulefit import Fair
        _W["fair"] = Fair(Path(root), t_from, t_to)


def _one(task: tuple) -> dict:
    fname, i, params, seed, fams = task
    fam = fams[fname]
    t0 = time.time()
    r = simulate(fam, params, seed, _W["root"], _W["stream"], _W["wins"], _W["t_from"], _W["t_to"], _W["fair"])
    fp = footprint(r["rows"], r["touch"], _W["wins"], _W["t_from"], _W["t_to"])
    return {"family": fname, "i": i, "seed": seed, "params": params, "x": [fp["x"][k] for k in FEATURES],
            "pnl_usd": fp["pnl_usd"], "shares": fp["shares"], "n_fills": fp["n_fills"],
            "sim_s": round(time.time() - t0, 3)}


def build_table(root: Path, tokens: Path, stream: str, t_from: int, t_to: int, families: dict, n: int, seed: int,
                workers: int = 1) -> list[dict]:
    """``n`` prior draws per family, each simulated once. Deterministic for a seed, whatever ``workers``."""
    _check_seed(seed)
    tasks = []
    for fname, fam in families.items():
        rng = np.random.default_rng(derive_seed(seed, "prior", fname))
        for i in range(n):
            tasks.append((fname, i, fam.draw(rng), derive_seed(seed, "candidate", fname, i), families))
    need_fair = any(f.key == "b" for f in families.values())
    args = (str(root), stream, str(tokens), t_from, t_to, need_fair)
    if workers <= 1:
        _init(*args)
        return [_one(t) for t in tasks]
    with ProcessPoolExecutor(workers, initializer=_init, initargs=args) as ex:
        return list(ex.map(_one, tasks, chunksize=4))


def write_table(path: Path, rows: list[dict], meta: dict) -> None:
    with open(path, "wb") as fh:
        fh.write(orjson.dumps({"meta": meta}) + b"\n")
        for r in rows:
            fh.write(orjson.dumps(r) + b"\n")


def read_table(path: Path) -> tuple[dict, list[dict]]:
    lines = [orjson.loads(x) for x in Path(path).read_bytes().splitlines() if x.strip()]
    return lines[0]["meta"], lines[1:]


# ---------------------------------------------------------------- inference
def _matrix(rows: list[dict]) -> np.ndarray:
    return np.array([[NAN if v is None else v for v in r["x"]] for r in rows], dtype=float).reshape(len(rows), len(FEATURES))


def scales(X: np.ndarray) -> np.ndarray:
    out = np.ones(X.shape[1])
    for j in range(X.shape[1]):
        col = X[:, j][np.isfinite(X[:, j])]
        if len(col) < 2:
            continue
        mad = 1.4826 * float(np.median(np.abs(col - np.median(col))))
        out[j] = max(mad, 0.25 * float(np.std(col)), 1e-3)
    return out


def distances(X: np.ndarray, x: np.ndarray, sc: np.ndarray) -> np.ndarray:
    # a finite difference is capped at the missing-statistic penalty, so a simulation that is missing a
    # statistic (e.g. no fills) is never nearer than one that has it
    z = np.clip((X - x) / sc, -MISSING_Z, MISSING_Z)
    fx, fX = np.isfinite(x), np.isfinite(X)
    z = np.where(fX & fx, z, np.where(fX | fx, MISSING_Z, 0.0))
    return np.sqrt(np.mean(z * z, axis=1))


def _width(prior: Prior, vals: list) -> float:
    """90 % width in the prior's width space; for a choice, the size of the 90 % credible set."""
    if prior.kind == "choice":
        return float(len(credible_set(prior, vals)))
    t = [prior.t(v) for v in vals]
    return float(np.quantile(t, 0.95) - np.quantile(t, 0.05))


def credible_set(prior: Prior, vals: list) -> list:
    counts = np.bincount([prior.index(v) for v in vals], minlength=len(prior.values)) / max(len(vals), 1)
    out, mass = [], 0.0
    for i in np.argsort(-counts, kind="stable"):
        if mass >= 0.9 - 1e-9:
            break
        out.append(prior.values[i])
        mass += counts[i]
    return [list(v) if isinstance(v, tuple) else v for v in out]


def param_summary(prior: Prior, post: list, ref: list[float]) -> dict:
    w = _width(prior, post)
    ref_w = float(np.median(ref)) if ref else NAN
    ratio = w / ref_w if ref_w > 0 else 1.0
    flag = "IDENTIFIED" if ratio <= FLAG_ID else "PARTIAL" if ratio <= FLAG_PARTIAL else "UNIDENTIFIED"
    if prior.kind == "choice":
        cs = credible_set(prior, post)
        return {"map": cs[0], "credible_set_90": cs, "width_ratio": round(ratio, 3), "flag": flag}
    return {"median": float(np.median(post)), "ci90": [float(np.quantile(post, 0.05)), float(np.quantile(post, 0.95))],
            "width_ratio": round(ratio, 3), "flag": flag}


def covers(prior: Prior, summary: dict, truth) -> bool:
    if prior.kind == "choice":
        t = list(truth) if isinstance(truth, (list, tuple)) else truth
        return any((list(v) if isinstance(v, (list, tuple)) else v) == t for v in summary["credible_set_90"])
    lo, hi = summary["ci90"]
    return lo - 1e-9 <= float(truth) <= hi + 1e-9


def pnl_weights(fam: Family, rows: list[dict]) -> dict:
    """First-order share of simulated P&L variance explained by each decision's parameters."""
    y = np.array([r["pnl_usd"] for r in rows], dtype=float)
    var = float(np.var(y))
    idx: dict[str, float] = defaultdict(float)
    for name, prior in fam.prior.items():
        if var <= 0:
            break
        if prior.kind == "choice":
            g = np.array([prior.index(r["params"][name]) for r in rows])
        else:
            t = np.array([prior.t(r["params"][name]) for r in rows])
            g = np.searchsorted(np.quantile(t, [0.2, 0.4, 0.6, 0.8]), t)
        means = np.array([y[g == b].mean() for b in np.unique(g)])
        wts = np.array([(g == b).mean() for b in np.unique(g)])
        idx[fam.decision[name]] += float(np.sum(wts * (means - y.mean()) ** 2)) / var
    tot = sum(idx.values())
    return {d: round(v / tot, 4) for d, v in idx.items()} if tot > 0 else {}


def infer(meta: dict, table: list[dict], x_obs: dict, families: dict | None = None, accept: float = 0.1,
          seed: int = 1) -> dict:
    """Rejection ABC of one observed footprint against the reference table (see module docstring)."""
    families = families or FAMILIES
    rng = np.random.default_rng(_check_seed(seed))
    X = _matrix(table)
    sc = scales(X)
    x = np.array([NAN if x_obs.get(k) is None else x_obs[k] for k in FEATURES], dtype=float)
    d = distances(X, x, sc)
    fams = [f for f in dict.fromkeys(r["family"] for r in table) if f in families]
    by = {f: np.array([i for i, r in enumerate(table) if r["family"] == f]) for f in fams}
    n_min = min(len(v) for v in by.values())
    if n_min < 2:
        raise ValueError("every family needs at least two reference simulations")
    k = min(max(5, int(round(accept * n_min))), n_min - 1)
    out: dict = {"k": k, "accept": accept, "features": dict(zip(FEATURES, x.tolist())), "families": {}}
    # model evidence: the pooled k * n_families nearest, counted per family (equal n drawn per family)
    pool_k = k * len(fams)
    near = np.argsort(d, kind="stable")[:pool_k]
    counts = np.array([sum(table[i]["family"] == f for i in near) / len(by[f]) * n_min for f in fams])
    probs = counts / counts.sum()
    dir_draws = rng.dirichlet(1 + counts, 4000)
    for j, f in enumerate(fams):
        fam, ix = families[f], by[f]
        dd = d[ix]
        order = np.argsort(dd, kind="stable")[:k]
        acc = [table[ix[i]] for i in order]
        D_obs = float(dd[order].mean())
        # adequacy: the observed k-NN distance against the family's own leave-one-out distribution
        Xf = X[ix]
        sub = ix if len(ix) <= 200 else rng.choice(ix, 200, replace=False)
        loo = []
        for i in sub:
            di = distances(Xf, X[i], sc)
            di = np.sort(di)[1:k + 1]
            loo.append(float(di.mean()))
        p_adeq = (1 + sum(v >= D_obs for v in loo)) / (1 + len(loo))
        # posterior predictive check: every statistic inside the accepted particles' range (+- PPC_PAD sd)
        A = X[ix[order]]
        misfit = []
        for jf, name in enumerate(FEATURES):
            col = A[:, jf][np.isfinite(A[:, jf])]
            if not np.isfinite(x[jf]):      # missing in the footprint: a misfit only if every particle has it
                if len(col) == len(A):
                    misfit.append(name)
                continue
            if len(col) == 0:               # present in the footprint, missing in every particle
                misfit.append(name)
                continue
            if not col.min() - PPC_PAD * sc[jf] <= x[jf] <= col.max() + PPC_PAD * sc[jf]:
                misfit.append(name)
        ref_rows = [table[i] for i in ix]
        params = {}
        for name, prior in fam.prior.items():
            post = [r["params"][name] for r in acc]
            ref = [_width(prior, [ref_rows[i]["params"][name] for i in rng.choice(len(ref_rows), k, replace=False)])
                   for _ in range(200)]
            params[name] = param_summary(prior, post, ref)
        out["families"][f] = {"prob": round(float(probs[j]), 4),
                              "prob_ci90": [round(float(np.quantile(dir_draws[:, j], q)), 4) for q in (0.05, 0.95)],
                              "d_knn": round(D_obs, 4), "adequacy_p": round(p_adeq, 4), "misfit": misfit,
                              "adequate": p_adeq >= 0.05 and not misfit,
                              "params": params, "posterior_samples": [r["params"] for r in acc],
                              "pnl_weights": pnl_weights(fam, ref_rows)}
    rule = [f for f in fams if f not in NULL_CLASS]
    nulls = [f for f in fams if f in NULL_CLASS]
    F = out["families"]
    best = max(rule, key=lambda f: (F[f]["prob"], -F[f]["d_knn"])) if rule else None
    p_null = sum(F[f]["prob"] for f in nulls)
    reasons = []
    if best is None:
        reasons.append("no rule family in the table")
    elif not np.isfinite(x[FEATURES.index("t_q50")]):
        reasons.append("no fills: there is no public footprint to explain")
    else:
        if not F[best]["adequate"]:
            reasons.append(f"best family {best} cannot reproduce the footprint (adequacy p {F[best]['adequacy_p']}, "
                           f"outside its simulated range: {F[best]['misfit']})")
        if nulls and F[best]["prob"] <= p_null:
            reasons.append(f"null class as probable ({p_null:.2f}) as {best} ({F[best]['prob']:.2f})")
        if nulls and F[best]["d_knn"] >= min(F[f]["d_knn"] for f in nulls):
            reasons.append("a random policy at matched activity is as near")
    explained = best is not None and not reasons
    out["best_family"] = best
    out["explained"] = explained
    out["reasons"] = reasons
    if not explained:
        out["verdict"] = "not explained by observed inputs"
        for f in F.values():     # no posterior for an unexplained footprint
            f.pop("posterior_samples")
            f.pop("params")
        return out
    out["verdict"] = f"explained by {best}"
    out["recovery"] = recovery(families[best], F[best])
    return out


def recovery(fam: Family, res: dict) -> dict:
    """Decision-weighted recovery: concentration per decision weighted by its share of simulated P&L."""
    conc: dict[str, list[float]] = defaultdict(list)
    for name, s in res["params"].items():
        conc[fam.decision[name]].append(1.0 - min(1.0, s["width_ratio"]))
    per = {d: round(float(np.mean(v)), 4) for d, v in conc.items()}
    w = res["pnl_weights"]
    score = sum(w.get(d, 0.0) * c for d, c in per.items()) if w else NAN
    return {"concentration": per, "fixed_by_family": [d for d in DECISIONS if d not in per], "pnl_weights": w,
            "score_within_family": round(score, 4), "score": round(score * res["prob"], 4)}


# ---------------------------------------------------------------- validation harness
HIDDEN_PRIOR = {"accuracy": U(0.65, 0.9), "size": C(10.0, 25.0, 50.0)}


def exam_params() -> list[dict]:
    p = Path(__file__).resolve().parents[3] / "evidence" / "2026-11-27-answerkey-inputs" / "exam-params.json"
    return list(orjson.loads(p.read_bytes()).values()) if p.exists() else []


def make_subject(fam: Family | None, params: dict, seed: int, root: Path, tokens: Path, stream: str, t_from: int,
                 t_to: int, out: Path, fair=None) -> str:
    """A scripted subject through answerkey's own simulation and public-evidence writer; returns its wallet."""
    _check_seed(seed)
    if fam is None:          # hidden signal: answerkey policy (f)
        pol: Policy = POLICIES["f"](seed)
        pol.params = dict(POLICIES["f"].params) | params
        name = "hidden_signal"
    else:
        pol, name = make_policy(fam, params, seed), fam.name
    wallet = addr("subject", name, seed)
    wins = universe(tokens, stream, t_from, t_to)
    sim = Sim(pol, wallet, wins, fair if (fam is not None and fam.key == "b") else None, seed)
    pub = out / "public"
    sim.run(root, stream, pub / "tape", t_from, t_to)
    write_public(sim, root, tokens, pub, (t_from + (t_to - t_from) // 2, t_to), t_from)
    return wallet


def wilson(k: int, n: int) -> list[float] | None:
    if n == 0:
        return None
    p, z = k / n, 1.96
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(c - h, 4), round(c + h, 4)]


def score_subject(fam_name: str | None, truth: dict, res: dict, families: dict) -> dict:
    out = {"true_family": fam_name or "hidden_signal", "explained": res["explained"], "best": res["best_family"],
           "verdict": res["verdict"], "reasons": res["reasons"], "cells": {}}
    if fam_name is None or fam_name in NULL_CLASS or not res["explained"]:
        return out
    fam = families[fam_name]
    for name, prior in fam.prior.items():
        s = res["families"][fam_name]["params"][name]
        out["cells"][name] = {"truth": truth[name], "covered": covers(prior, s, truth[name]), "flag": s["flag"],
                              "summary": s}
    out["recovery"] = res.get("recovery")
    return out


def harness_totals(subjects: list[dict]) -> dict:
    cells = [c for s in subjects for c in s["cells"].values()]
    cov = sum(c["covered"] for c in cells)
    ident = [c for c in cells if c["flag"] == "IDENTIFIED"]
    wrong = [c for c in ident if not c["covered"]]
    rule = [s for s in subjects if s["true_family"] not in NULL_CLASS + ("hidden_signal",)]
    sel = sum(s["explained"] and s["best"] == s["true_family"] for s in rule)
    hidden = [s for s in subjects if s["true_family"] == "hidden_signal"]
    decoy = [s for s in subjects if s["true_family"] in NULL_CLASS]
    return {"cells": len(cells), "coverage": round(cov / len(cells), 4) if cells else None,
            "coverage_ci95": wilson(cov, len(cells)),
            "identified": len(ident), "false_confidence": len(wrong),
            "false_confidence_rate": round(len(wrong) / len(ident), 4) if ident else None,
            "rule_subjects": len(rule), "rule_explained": sum(s["explained"] for s in rule),
            "family_selection_accuracy": round(sel / len(rule), 4) if rule else None,
            "family_selection_ci95": wilson(sel, len(rule)),
            "hidden_signal": [{"explained": s["explained"], "verdict": s["verdict"], "reasons": s["reasons"]}
                              for s in hidden],
            "hidden_signal_all_unexplained": all(not s["explained"] for s in hidden) if hidden else None,
            "decoys_unexplained": sum(not s["explained"] for s in decoy), "decoys": len(decoy)}


def loo_coverage(meta: dict, table: list[dict], families: dict, per_family: int, accept: float, seed: int) -> dict:
    """Simulation-based calibration on the table itself: each held-out row is a pseudo-subject."""
    rng = np.random.default_rng(_check_seed(seed))
    cells: list[dict] = []
    for f, fam in families.items():
        if f in NULL_CLASS:
            continue
        ix = [i for i, r in enumerate(table) if r["family"] == f]
        for i in rng.choice(ix, min(per_family, len(ix)), replace=False):
            rest = table[:i] + table[i + 1:]
            res = infer(meta, rest, dict(zip(FEATURES, table[i]["x"])), families, accept, seed)
            if not res["explained"]:
                cells.append({"family": f, "explained": False})
                continue
            for name, prior in fam.prior.items():
                s = res["families"][f]["params"][name]
                cells.append({"family": f, "explained": True, "param": name, "flag": s["flag"],
                              "covered": covers(prior, s, table[i]["params"][name])})
    sc = [c for c in cells if c["explained"]]
    cov = sum(c["covered"] for c in sc)
    ident = [c for c in sc if c["flag"] == "IDENTIFIED"]
    return {"cells": len(sc), "coverage": round(cov / len(sc), 4) if sc else None, "coverage_ci95": wilson(cov, len(sc)),
            "identified": len(ident), "false_confidence": sum(not c["covered"] for c in ident),
            "unexplained_pseudo_subjects": sum(not c["explained"] for c in cells),
            "by_param": _by_param(sc)}


def _by_param(cells: list[dict]) -> dict:
    g: dict[str, list] = defaultdict(list)
    for c in cells:
        g[f"{c['family']}.{c['param']}"].append(c)
    return {k: {"n": len(v), "coverage": round(sum(c["covered"] for c in v) / len(v), 3),
                "identified": sum(c["flag"] == "IDENTIFIED" for c in v)} for k, v in sorted(g.items())}


def validate(root: Path, tokens: Path, stream: str, t_from: int, t_to: int, out: Path, n: int, per_family: int,
             hidden: int, seed: int, workers: int, accept: float, table_path: Path | None = None,
             families: dict | None = None, loo: int = 0) -> dict:
    _check_seed(seed)
    families = families or FAMILIES
    out.mkdir(parents=True, exist_ok=False)
    t0 = time.time()
    meta = {"stream": stream, "from": t_from, "to": t_to, "seed": seed, "n": n, "features": FEATURES,
            "families": {f: {k: p.spec() for k, p in fam.prior.items()} for f, fam in families.items()}}
    if table_path is not None:
        meta, table = read_table(table_path)
    else:
        table = build_table(root, tokens, stream, t_from, t_to, families, n, seed, workers)
        write_table(out / "table.jsonl", table, meta)
    table_s = round(time.time() - t0, 1)
    fair = None
    if any(f.key == "b" for f in families.values()):
        from .rulefit import Fair
        fair = Fair(root, t_from, t_to)
    reserved = exam_params()
    subjects = []
    plan: list[tuple[str | None, dict, int]] = []
    for f, fam in families.items():
        rng = np.random.default_rng(derive_seed(seed, "subject-prior", f))
        for j in range(per_family):
            p = fam.draw(rng)
            if any(all(p.get(k) == v for k, v in r.items()) for r in reserved):
                raise ValueError("a subject drew a reserved exam parameter set")
            plan.append((f, p, derive_seed(seed, "subject", f, j)))
    rng = np.random.default_rng(derive_seed(seed, "subject-prior", "hidden"))
    for j in range(hidden):
        plan.append((None, {k: p.draw(rng) for k, p in HIDDEN_PRIOR.items()}, derive_seed(seed, "subject", "hidden", j)))
    t1 = time.time()
    for j, (f, p, s) in enumerate(plan):
        d = out / "subjects" / f"{j:03d}-{f or 'hidden_signal'}"
        wallet = make_subject(families[f] if f else None, p, s, root, tokens, stream, t_from, t_to, d, fair)
        pub = d / "public"
        fp = observed_footprint(pub / "tape", stream, pub / "orders.jsonl", pub / "lab" / "tokens.jsonl", wallet,
                                t_from, t_to)
        res = infer(meta, table, fp["x"], families, accept, seed)
        (d / "inference.json").write_bytes(orjson.dumps(res, option=orjson.OPT_SERIALIZE_NUMPY))
        row = score_subject(f, p, res, families) | {"params": p, "seed": s, "n_fills": fp["n_fills"]}
        subjects.append(row)
        print(f"{j:03d} {f or 'hidden_signal'}: fills {fp['n_fills']} -> {res['verdict']}", file=sys.stderr)
    card = {"meta": meta, "accept": accept, "table_rows": len(table), "table_s": table_s,
            "subjects_s": round(time.time() - t1, 1),
            "sim_s_median": float(np.median([r["sim_s"] for r in table])) if table else None,
            "totals": harness_totals(subjects), "subjects": subjects,
            "peak_rss_mb": {"self": _mb(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
                            "workers": _mb(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)}}
    if loo:
        card["table_loo"] = loo_coverage(meta, table, families, loo, accept, seed)
    (out / "validation.json").write_bytes(orjson.dumps(card, option=orjson.OPT_INDENT_2 | orjson.OPT_SERIALIZE_NUMPY))
    return card


def _mb(maxrss: int) -> float:
    return round(maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


# ---------------------------------------------------------------- CLI
def _families(arg: str | None) -> dict:
    if not arg:
        return FAMILIES
    names = arg.split(",")
    if set(names) - FAMILIES.keys():
        raise SystemExit(f"unknown family; choose from {','.join(FAMILIES)}")
    return {k: FAMILIES[k] for k in names}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-simulate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("table", "validate"):
        s = sub.add_parser(name)
        s.add_argument("--data", type=Path, required=True)
        s.add_argument("--tokens", type=Path, required=True)
        s.add_argument("--stream", default="clob_btc_5m")
        s.add_argument("--from", dest="t_from", type=_ts, required=True)
        s.add_argument("--to", dest="t_to", type=_ts, required=True)
        s.add_argument("--families", help=f"comma list (default all: {','.join(FAMILIES)})")
        s.add_argument("--n", type=int, default=150, help="prior draws per family")
        s.add_argument("--seed", type=int, default=1)
        s.add_argument("--workers", type=int, default=1)
        s.add_argument("--out", type=Path, required=True)
    v = sub.choices["validate"]
    v.add_argument("--per-family", type=int, default=4, help="scripted subjects drawn from each family's prior")
    v.add_argument("--hidden", type=int, default=3, help="hidden-signal subjects")
    v.add_argument("--accept", type=float, default=0.1)
    v.add_argument("--table", type=Path, help="reuse a reference table built on the same tape and range")
    v.add_argument("--loo", type=int, default=0, help="table rows per family for leave-one-out calibration")
    i = sub.add_parser("infer")
    i.add_argument("--table", type=Path, required=True)
    i.add_argument("--tape", type=Path, required=True, help="tape root holding the wallet's public book")
    i.add_argument("--orders", type=Path, required=True, help="ordertape JSONL rows")
    i.add_argument("--tokens", type=Path, required=True)
    i.add_argument("--wallet", required=True)
    i.add_argument("--accept", type=float, default=0.1)
    i.add_argument("--seed", type=int, default=1)
    i.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    try:
        _check_seed(a.seed)
    except ValueError as exc:
        ap.error(str(exc))
    if a.cmd == "table":
        fams = _families(a.families)
        t0 = time.time()
        rows = build_table(a.data, a.tokens, a.stream, a.t_from, a.t_to, fams, a.n, a.seed, a.workers)
        meta = {"stream": a.stream, "from": a.t_from, "to": a.t_to, "seed": a.seed, "n": a.n, "features": FEATURES,
                "families": {f: {k: p.spec() for k, p in fam.prior.items()} for f, fam in fams.items()},
                "wall_s": round(time.time() - t0, 1),
                "peak_rss_mb": {"self": _mb(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
                                "workers": _mb(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)}}
        write_table(a.out, rows, meta)
        print(orjson.dumps({k: meta[k] for k in ("wall_s", "peak_rss_mb")}).decode())
        return 0
    if a.cmd == "validate":
        if a.out.exists():
            ap.error("the run requires a fresh output directory")
        card = validate(a.data, a.tokens, a.stream, a.t_from, a.t_to, a.out, a.n, a.per_family, a.hidden, a.seed,
                        a.workers, a.accept, a.table, _families(a.families), a.loo)
        print(orjson.dumps(card["totals"], option=orjson.OPT_INDENT_2).decode())
        return 0
    meta, table = read_table(a.table)
    fp = observed_footprint(a.tape, meta["stream"], a.orders, a.tokens, a.wallet, meta["from"], meta["to"])
    fams = {f: FAMILIES[f] for f in meta["families"] if f in FAMILIES}
    res = infer(meta, table, fp["x"], fams, a.accept, a.seed) | {"n_fills": fp["n_fills"]}
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "inference.json").write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2 | orjson.OPT_SERIALIZE_NUMPY))
    print(res["verdict"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
