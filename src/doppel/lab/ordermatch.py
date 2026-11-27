"""Order match: find decoded signed orders in the anonymous intl book, follow them, and estimate
the quotes that never filled (E37 productized).

Inputs: the intl CLOB tape (``intlbook``) and ``doppel-ordertape`` rows (signed orders decoded
from ``matchOrders`` calldata, hash-verified). Only maker rows are matched; rows of one
``order_id`` are merged (fills summed per tx). Screen as in the E37 probe: ``created_ms`` inside
[--from, --to) and 0 <= age_s < 600 at the first fill.

``--orders`` is a JSONL sample (in file order) or the cohort parquet directory.
Managed tapes use committed flush parts and require verified rows. Legacy exports without
verification flags can be read standalone. If the source contains any verification flags,
only rows marked true count.
Directory rows are read oldest fill first, so "first fill" does not depend on the
order the parts were written in. A cohort run verifies the cohort's own fills only, so other
makers in the same txs are ``verified == False`` and are left out: ``other_known`` then counts
only other COHORT wallets, ``identity_precision_upper`` is NOT MEASURED (it reads near 1.0 or
null) and ``others_pooled`` holds cohort wallets only. Maker rows created outside [--from, --to) are dropped while reading and
counted in rows (``rows_out_of_range``), not in orders.

WHAT IS IDENTIFIED (from data) AND WHAT IS ASSUMED

1. Placement. An order (token, side, limit, size, created_ms) is "matched" to a level increase at
   (token, limit, side) whose delta equals its size (|delta - size| <= max(size_tol,
   size_tol_rel * size)) with venue time in created_ms + [lo_ms, hi_ms]. One-to-one: candidate
   (order, event) pairs are taken smallest |dt| first and each event serves one order. This is
   evidence, not proof: someone else may have posted that size then. Each matched row records
   how many size and timestamp candidates exist at its level up to its first located fill.
   The same test is run
   with every created_ms shifted by +60 s (placebo). Rates are over "observable" orders: the
   asset's book state is known (``intlbook`` spans) across BOTH the real and the placebo window,
   so the two rates share a denominator. (E37 used orders with any level event at their price and
   a 2 % relative size tolerance; pass --size-tol-rel 0.02 to compare, but at 2 % the sizes
   97..100 all match each other, so the fingerprint's per-size split then means nothing.)

2. Follow-forward of a matched order. Known from chain: its fills (tx, amount).
   ``fill_amount`` is in maker-amount units, so filled shares = fill_amount * taker_amount /
   maker_amount / 1e6 for BUY and fill_amount / 1e6 for SELL. A fill is located in time by a
   print carrying its ``transaction_hash`` on the order's token or its mate (a maker bid on one
   token is hit by takers of either token: the book is mirrored, BUY A @ p == SELL B @ 1-p);
   without such a print the block time is used, which trails the match. The print gives time
   only; quantity comes from chain. R = size - filled is the remainder. A fill printed BEFORE
   the matched increase proves the placement match wrong: ``fill_before_placement``, counted and
   kept out of the resting times.
   - ``filled``: R <= tol. The order ended at its last fill. Exact if every fill has a print.
   - ``cancelled`` (cancel or replace; they look the same): R > tol and, after the last fill, a
     single level decrease of R (within tol) at its price that prints do not explain: the volume
     printed at that level or its mirror within +-print_win_ms is smaller than the decrease.
     ASSUMED: that exact-size decrease is this order and not another order of the same size
     (``alone`` = the level held nothing else, which makes it certain); and prints report at
     least the shares that left the level through trades. ``prints_near`` counts prints in the
     window so a strict reader can keep only cancels with none. Each decrease serves one order
     (earliest placed first; among same-size orders at one level the pairing is arbitrary).
   - ``unknown``: anything else. ``level_below`` = the level fell below R with no such decrease
     (the order is gone by then: merged with other changes, or filled in txs missing from the
     orders file, which holds only the fetched txs so chain fills are a lower bound);
     ``censored`` = the asset's known span ended first (reset, expiry, tape end).
   Before its last located fill the order is certain to exist, so the level is not examined
   there beyond checking that it can still hold R once the fills are done. ``end_src`` says
   whether the cancel was seen as a ``price_change`` or only as a snapshot diff (weaker).
   Resting time is reported as bounds from placement (venue ms): ``rest_min_ms`` = last
   moment it certainly existed (last print-located fill, else placement), ``rest_max_ms`` = first
   moment it certainly did not (None when censored). ``rest_known`` = the two coincide.

3. Fingerprint attribution of quotes that never filled. Fingerprint = the fewest sizes (2 dp)
   covering >= 90 % of the wallet's decoded maker orders (none if that takes more than 8 sizes).
   Counted on the BUY view only (each quote shows once as a bid on one token and once as an ask
   on its mate; counting both would double it): bid-level increases equal to a fingerprint size.
   In the wallet's active tokens (any decoded order of the wallet, plus mates) each is
   ``own`` (matched to the wallet's decoded order), ``other_known`` (matched to another maker's
   decoded order: a proven false positive) or ``candidate``. In control tokens (no decoded order
   of the wallet) the count gives a base rate per token-hour of known book. Reported side by
   side, total and per size: candidates, expected by chance (base rate x active token-hours),
   own_unmatched (the wallet's decoded fingerprint-size orders that found no placement: filled
   quotes hiding among the candidates), net = candidates - own_unmatched - expected, and
   identity precision own / (own + other_known).
   NOT IDENTIFIED: which candidates are the wallet's. ``net`` is an estimate under the
   assumption that other participants post these sizes at the control rate in the wallet's
   markets. The base rate is contaminated upward (the wallet may quote in control tokens without
   filling, and "no fills" is judged from a partial sample), precision is an upper bound (other
   makers are decoded only when they share a tx with the fetched ones), and round sizes are
   generic. Candidates are followed like remainders (R = the increase); their rows carry
   ``attribution: "fingerprint_candidate"`` and are never facts about the wallet.

Analysis only: placement uses created_ms and follow-forward reads the future of the tape by
design, so none of these outputs is a decision input at the time of the quote.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import orjson
import pyarrow.parquet as pq

from .flow import _ts
from .intlbook import IntlBook, LevelEvent, Print, px, replay
from .ordertape import STATE_DIR, committed_parts

Key = tuple[str, float, str]  # asset, price, side
MAX_AGE_S = 600.0
FP_COVER = 0.9
FP_MAX_SIZES = 8


@dataclass(frozen=True)
class Params:
    lo_ms: int = -300
    hi_ms: int = 2500
    size_tol: float = 0.02  # shares
    size_tol_rel: float = 0.0
    placebo_ms: int = 60_000
    print_win_ms: int = 1000

    def tol(self, size: float) -> float:
        return max(self.size_tol, self.size_tol_rel * size)

    def near(self, x: float, size: float) -> bool:
        return abs(x - size) <= self.tol(size)


@dataclass
class Order:
    order_id: str
    maker: str
    token: str
    side: str
    price: float
    size: float
    created_ms: int
    fills: dict[str, tuple[float, int | None]] = field(default_factory=dict)  # tx -> shares, block_ts

    @property
    def key(self) -> Key:
        return (self.token, self.price, self.side)

    @property
    def filled(self) -> float:
        return sum(f[0] for f in self.fills.values())


def filled_shares(row: dict) -> float:
    """Shares filled in one row. ``fill_amount`` is in maker-amount units (USDC for BUY)."""
    fa = row["fill_amount"]
    if row["side"] == "SELL":
        return fa / 1e6
    if row.get("maker_amount"):
        return fa * row["taker_amount"] / row["maker_amount"] / 1e6
    return fa / 1e6 / row["limit"]


PARQUET_COLS = ("tx", "block_ts", "role", "order_id", "maker", "token_id", "side", "size", "limit",
                "maker_amount", "taker_amount", "fill_amount", "created_ms", "age_s", "verified")


def _jsonl_rows(path: Path):
    with open(path, "rb") as fh:
        for line in fh:
            if line.strip():
                yield orjson.loads(line)


def _parquet_rows(path: Path, t_from: int, t_to: int, active: dict[str, set[str]],
                  stats: Counter) -> list[dict]:
    """Maker rows of an ordertape directory created in [t_from, t_to), oldest fill first. Only
    ``verified == True`` rows count (when the column exists); every counted row, whatever its role
    or time, adds its token to ``active``. One part is in memory at a time."""
    keep: list[dict] = []
    managed = (path / STATE_DIR).is_dir()
    schemas = {p: pq.read_schema(p).names for p in committed_parts(path, allow_legacy=True)}
    flagged = any("verified" in names for names in schemas.values())
    for p, names in schemas.items():
        if managed and "verified" not in names:
            raise ValueError(f"committed order tape part lacks verification flags: {p}")
        for r in pq.read_table(p, columns=[c for c in PARQUET_COLS if c in names]).to_pylist():
            if flagged and r.get("verified") is not True:
                stats["rows_not_verified"] += 1
                continue
            active[r["maker"].lower()].add(r["token_id"])
            if r["role"] != "maker":
                continue
            c = r["created_ms"]
            if c is None or not t_from * 1000 <= c < t_to * 1000:
                stats["rows_out_of_range"] += 1
                continue
            keep.append(r)
    keep.sort(key=lambda r: (r["block_ts"], r["tx"]))
    return keep


def load_orders(path: Path, t_from: int, t_to: int) -> tuple[list[Order], dict[str, set[str]], Counter]:
    """Screened maker orders (one per order_id), every maker's tokens (any role), counts.
    ``path`` is a JSONL file or an ordertape parquet directory (see the module docstring)."""
    orders: dict[str, Order] = {}
    active: dict[str, set[str]] = defaultdict(set)
    stats: Counter = Counter()
    skip: set[str] = set()
    path = Path(path)
    if path.is_dir():
        rows = _parquet_rows(path, t_from, t_to, active, stats)
        flagged = False  # parquet rows already screened, including non-makers in active
    else:
        flagged = any("verified" in r for r in _jsonl_rows(path))
        rows = _jsonl_rows(path)
    for r in rows:
        if flagged and r.get("verified") is not True:
            stats["rows_not_verified"] += 1
            continue
        active[r["maker"].lower()].add(r["token_id"])
        if r.get("role") != "maker":
            continue
        oid = r["order_id"]
        if oid in skip:
            continue
        o = orders.get(oid)
        if o is None:  # screen on the first row seen
            age = r.get("age_s")
            why = ("no_limit" if not r.get("limit") else
                   "out_of_range" if not t_from * 1000 <= r["created_ms"] < t_to * 1000 else
                   "bad_age" if age is None or not 0 <= age < MAX_AGE_S else None)
            if why:
                stats[f"screened:{why}"] += 1
                skip.add(oid)
                continue
            o = orders[oid] = Order(oid, r["maker"].lower(), r["token_id"], r["side"],
                                    px(r["limit"]), float(r["size"]), int(r["created_ms"]))
        sh, _ = o.fills.get(r["tx"], (0.0, None))
        o.fills[r["tx"]] = (sh + filled_shares(r), r.get("block_ts"))
    stats["maker_orders"] = len(orders)
    return sorted(orders.values(), key=lambda o: (o.created_ms, o.order_id)), dict(active), stats


# ---------------------------------------------------------------- tape
class LevelTape:
    """Level events per (asset, price, side) in tape order, prints, known spans and mates."""

    def __init__(self) -> None:
        self.events: dict[Key, list[LevelEvent]] = defaultdict(list)
        self.prints: dict[tuple[str, float], list[Print]] = defaultdict(list)
        self.prints_tx: dict[str, list[Print]] = defaultdict(list)
        self.spans: dict[str, list[list[int]]] = {}
        self.mate: dict[str, str] = {}
        self.counts: Counter = Counter()

    def add(self, e: LevelEvent) -> None:
        self.events[(e.asset, e.price, e.side)].append(e)

    def add_print(self, p: Print) -> None:
        self.prints[(p.asset, p.price)].append(p)
        if p.tx:
            self.prints_tx[p.tx].append(p)

    def span_end(self, asset: str, ms: int) -> int | None:
        """End of the known span containing ``ms``; None when the state is unknown at ``ms``."""
        for a, b in self.spans.get(asset, ()):
            if a <= ms <= b:
                return b
        return None

    def known_over(self, asset: str, lo: int, hi: int) -> bool:
        return any(a <= lo and hi <= b for a, b in self.spans.get(asset, ()))

    def hours(self, assets: set[str]) -> float:
        return sum(b - a for t in assets for a, b in self.spans.get(t, ())) / 3.6e6

    def sides(self, asset: str, price: float) -> list[tuple[str, float]]:
        """The level and its mirror on the mate asset."""
        m = self.mate.get(asset)
        return [(asset, price)] + ([(m, px(1 - price))] if m else [])

    def printed(self, asset: str, price: float, ms: int, win: int) -> tuple[float, int]:
        """Volume and count of prints at the level or its mirror within +-win ms."""
        ps = [p for k in self.sides(asset, price) for p in self.prints.get(k, ())
              if abs(p.venue_ms - ms) <= win]
        return sum(p.size for p in ps), len(ps)


def build_tape(root: Path, streams: list[str], t_from: int, t_to: int, tokens: set[str],
               fp_sizes: list[float], p: Params) -> LevelTape:
    """Replay the streams. Every level event is kept for the markets of ``tokens``; elsewhere
    only bid increases of a fingerprint size (the base-rate counts), to bound memory."""
    tape = LevelTape()
    markets: set[str] = set()
    for s in streams:
        book = IntlBook()
        for items in replay(root, s, t_from, t_to, book):
            markets.update(it.market for it in items
                           if isinstance(it, (LevelEvent, Print)) and it.asset in tokens)
            for it in items:
                if isinstance(it, Print):
                    tape.add_print(it)
                elif isinstance(it, LevelEvent):
                    if it.market in markets or it.asset in tokens or (
                            it.side == "BUY" and any(p.near(it.delta, z) for z in fp_sizes)):
                        tape.add(it)
                else:
                    tape.counts[f"reset:{it.why}"] += 1
        tape.counts["price_change_unknown_state"] += book.dropped
        for a, sp in book.spans.items():
            tape.spans.setdefault(a, []).extend(sp)
        tape.mate.update(book.mates())
    return tape


# ---------------------------------------------------------------- placement
def match_placements(orders: list[Order], tape: LevelTape, p: Params,
                     shift_ms: int = 0) -> dict[str, tuple[Key, int]]:
    """order_id -> (key, index of its placement event). One-to-one, smallest |dt| first."""
    by_key: dict[Key, list[Order]] = defaultdict(list)
    for o in orders:
        by_key[o.key].append(o)
    pairs = []
    for k, os_ in by_key.items():
        evs = tape.events.get(k) or []
        for o in os_:
            t = o.created_ms + shift_ms
            for i, e in enumerate(evs):
                dt = e.venue_ms - t
                if p.lo_ms <= dt <= p.hi_ms and p.near(e.delta, o.size):
                    pairs.append((abs(dt), o.order_id, k, i))
    pairs.sort()
    out: dict[str, tuple[Key, int]] = {}
    used: set[tuple[Key, int]] = set()
    for _, oid, k, i in pairs:
        if oid not in out and (k, i) not in used:
            out[oid] = (k, i)
            used.add((k, i))
    return out


def observable(o: Order, tape: LevelTape, p: Params, shift_ms: int = 0) -> bool:
    t = o.created_ms + shift_ms
    return tape.known_over(o.token, t + p.lo_ms, t + p.hi_ms)


def placement_candidate_count(events: list[LevelEvent], created_ms: int, size: float, p: Params,
                              first_fill_ms: int | None) -> int:
    """Count plausible book increases before applying one-to-one assignment."""
    lo = created_ms + p.lo_ms
    hi = created_ms + p.hi_ms
    if first_fill_ms is not None:
        hi = min(hi, first_fill_ms)
    return sum(lo <= e.venue_ms <= hi and p.near(e.delta, size) for e in events)


# ---------------------------------------------------------------- follow-forward
def follow(tape: LevelTape, key: Key, i0: int, r: float, start_ms: int,
           claimed: set[tuple[Key, int]], p: Params) -> dict:
    """What happened to ``r`` shares resting at ``key`` since event ``i0``, certain to be there
    until ``start_ms``. See the module docstring for the rules."""
    evs = tape.events[key]
    tol = p.tol(r)
    end = tape.span_end(key[0], evs[i0].venue_ms)
    j, level = i0 + 1, evs[i0].new_size
    while j < len(evs) and evs[j].venue_ms < start_ms and end is not None and evs[j].venue_ms <= end:
        level = evs[j].new_size  # the order is certain to exist here; only track the level
        j += 1
    if level < r - tol:  # ... yet by start_ms the level cannot hold what should be left
        return {"outcome": "unknown", "reason": "level_below", "end_ms": start_ms}
    for j in range(j, len(evs)):
        e = evs[j]
        if end is None or e.venue_ms > end:
            break
        dec = -e.delta
        if abs(dec - r) <= tol and (key, j) not in claimed:
            vol, n = tape.printed(key[0], key[1], e.venue_ms, p.print_win_ms)
            if vol < dec - tol:
                claimed.add((key, j))
                return {"outcome": "cancelled", "reason": "exact_decrease", "end_ms": e.venue_ms,
                        "end_src": e.src, "alone": e.old_size <= r + tol, "prints_near": n}
        if e.new_size < r - tol:
            return {"outcome": "unknown", "reason": "level_below", "end_ms": e.venue_ms}
    return {"outcome": "unknown", "reason": "censored", "end_ms": None}


def classify(o: Order, key: Key, i0: int, tape: LevelTape, claimed: set[tuple[Key, int]],
             p: Params) -> dict:
    """Per-order row: placement, located fills, outcome and resting-time bounds."""
    place = tape.events[key][i0]
    level = set(tape.sides(o.token, o.price))
    assets = {a for a, _ in level}
    located: list[int] = []
    fills_at: list[list] = []  # [tx, venue ms of its earliest print or None, shares]
    late = place.venue_ms  # after this every fill has certainly happened
    n_price_ok = n_size_ok = 0
    for tx, (sh, bts) in o.fills.items():
        ps = [q for q in tape.prints_tx.get(tx, ()) if q.asset in assets]
        fills_at.append([tx, min(q.venue_ms for q in ps) if ps else None, round(sh, 6)])
        if ps:
            t = min(q.venue_ms for q in ps)
            located.append(t)
            late = max(late, t)
            at = [q for q in ps if (q.asset, q.price) in level]
            n_price_ok += bool(at)
            n_size_ok += any(p.near(q.size, sh) for q in at)
        elif bts is not None:
            late = max(late, bts * 1000)
    all_located = len(located) == len(o.fills)
    certain = max(located, default=place.venue_ms)
    r = o.size - o.filled
    row = {"order_id": o.order_id, "maker": o.maker, "token_id": o.token, "side": o.side,
           "price": o.price, "size": o.size, "created_ms": o.created_ms,
           "placed_venue_ms": place.venue_ms, "placed_tw_ns": place.tw_ns, "placed_src": place.src,
           "sign_to_book_ms": place.venue_ms - o.created_ms,
           "placement_candidates": placement_candidate_count(
               tape.events[key], o.created_ms, o.size, p, min(located, default=None)),
           "filled": round(o.filled, 6), "remainder": round(r, 6), "n_fills": len(o.fills),
           "fills_located": len(located), "print_price_ok": n_price_ok, "print_size_ok": n_size_ok,
           "fills_at": fills_at}
    if r <= p.tol(o.size):
        res = {"outcome": "filled", "reason": "chain" if all_located else "chain_block_time",
               "end_ms": late}
        lo = late if all_located else certain
    else:
        res = follow(tape, key, i0, r, late, claimed, p)
        lo = res["end_ms"] if res["outcome"] == "cancelled" else certain
    hi = res["end_ms"]
    row |= res | {"rest_min_ms": lo - place.venue_ms,
                  "rest_max_ms": None if hi is None else hi - place.venue_ms}
    # a fill printed before the matched increase is impossible: the placement match is wrong
    row["fill_before_placement"] = bool(located) and min(located) < place.venue_ms
    row["rest_known"] = row["rest_min_ms"] == row["rest_max_ms"] and not row["fill_before_placement"]
    return row


# ---------------------------------------------------------------- fingerprint
def fingerprint(sizes: list[float]) -> list[float]:
    """Fewest sizes (2 dp) covering FP_COVER of the orders; [] when that needs > FP_MAX_SIZES."""
    c = Counter(round(s, 2) for s in sizes)
    out: list[float] = []
    got = 0
    for s, n in c.most_common():
        if got >= FP_COVER * len(sizes):
            break
        out.append(s)
        got += n
    return sorted(out) if sizes and len(out) <= FP_MAX_SIZES else []


def attribute(wallet: str, sizes: list[float], orders: list[Order],
              matches: dict[str, tuple[Key, int]], active: set[str], tape: LevelTape,
              claimed: set[tuple[Key, int]], p: Params,
              sink: Callable[[dict], object] | None = None) -> tuple[dict, list[dict]]:
    """Fingerprint-size bid increases split into own / other_known / candidate / control.
    With ``sink`` each candidate row is handed to it and not kept (the returned list is empty):
    a quote-flickering wallet has ~10^5 candidates per hour."""
    active = active | {tape.mate[t] for t in active if t in tape.mate}
    known: dict[bool, Counter] = {True: Counter(), False: Counter()}  # own? -> BUY-view (asset, price, ms)
    for o in orders:
        if o.order_id in matches:
            k, i = matches[o.order_id]
            a, pr = (k[0], k[1]) if k[2] == "BUY" else tape.sides(k[0], k[1])[-1]
            known[o.maker == wallet][(a, pr, tape.events[k][i].venue_ms)] += 1
    per: dict[float, Counter] = {s: Counter() for s in sizes}
    for o in orders:  # the wallet's own decoded orders that found no placement still made an increase
        s = next((z for z in sizes if p.near(o.size, z)), None)
        if o.maker == wallet and o.order_id not in matches and s is not None:
            per[s]["own_unmatched"] += 1
    rows: list[dict] = []
    emit = sink or rows.append
    outcomes: Counter = Counter()
    lives: list[int] = []  # rest_max_ms of the candidates with a known end
    for key in sorted(k for k in tape.events if k[2] == "BUY"):
        for i, e in enumerate(tape.events[key]):
            s = next((z for z in sizes if p.near(e.delta, z)), None)
            if s is None:
                continue
            if key[0] not in active:
                per[s]["control"] += 1
                continue
            cid = (key[0], key[1], e.venue_ms)
            if known[True][cid] > 0:
                known[True][cid] -= 1
                per[s]["own"] += 1
            elif known[False][cid] > 0:
                known[False][cid] -= 1
                per[s]["other_known"] += 1
            else:
                per[s]["candidates"] += 1
                res = follow(tape, key, i, e.delta, e.venue_ms, claimed, p)
                hi = res["end_ms"]
                outcomes[f"{res['outcome']}:{res['reason']}"] += 1
                if res["outcome"] == "cancelled":
                    lives.append(hi - e.venue_ms)
                emit({"attribution": "fingerprint_candidate", "wallet": wallet,
                      "token_id": key[0], "price": key[1], "side_view": "BUY",
                      "venue_ms": e.venue_ms, "tw_ns": e.tw_ns, "size": round(e.delta, 6),
                      "fp_size": s} | res | {
                          "rest_min_ms": (hi - e.venue_ms) if res["outcome"] == "cancelled" else 0,
                          "rest_max_ms": None if hi is None else hi - e.venue_ms,
                          "rest_known": res["outcome"] == "cancelled"})
    h_act = tape.hours(active)
    h_ctl = tape.hours(set(tape.spans) - active)

    def line(c: Counter) -> dict:
        rate = c["control"] / h_ctl if h_ctl > 0 else None
        exp = None if rate is None else rate * h_act
        ident = c["own"] + c["other_known"]
        return {"own": c["own"], "other_known": c["other_known"], "candidates": c["candidates"],
                "own_unmatched": c["own_unmatched"],
                "control": c["control"], "control_per_token_hour": _r(rate),
                "expected_by_chance": _r(exp),
                "net_estimate": None if exp is None else _r(
                    max(0.0, c["candidates"] - c["own_unmatched"] - exp)),
                "identity_precision_upper": _r(c["own"] / ident) if ident else None}

    total: Counter = Counter()
    for c in per.values():
        total.update(c)
    doc = {"sizes": sizes, "active_tokens": len(active), "active_token_hours": _r(h_act),
           "control_token_hours": _r(h_ctl), "total": line(total),
           "per_size": {str(s): line(c) for s, c in per.items()},
           "candidate_outcomes": dict(outcomes),
           "candidate_rest_ms_cancelled": _q(lives),
           "note": "candidates are NOT identified as the wallet's; read net_estimate against "
                   "expected_by_chance and identity_precision_upper (see module docstring)"}
    return doc, rows


# ---------------------------------------------------------------- report
def _r(x, n: int = 3):
    return None if x is None else round(float(x), n)


def _q(xs: list, qs=(0.1, 0.5, 0.9)) -> dict | None:
    if not xs:
        return None
    a = np.asarray(xs, dtype=float)
    return {f"p{int(q * 100)}": _r(np.quantile(a, q), 1) for q in qs} | {"n": len(xs)}


def summarize(name: str, orders: list[Order], matches: dict, placebo: dict, rows: list[dict],
              tape: LevelTape, p: Params) -> dict:
    """Match rate vs placebo, latency and outcomes for one set of maker orders."""
    obs = [o for o in orders if observable(o, tape, p) and observable(o, tape, p, p.placebo_ms)]
    hit = [o for o in obs if o.order_id in matches]
    pl = sum(o.order_id in placebo for o in obs)
    mine = {o.order_id for o in orders}
    rs = [r for r in rows if r["order_id"] in mine]
    canc = [r for r in rs if r["outcome"] == "cancelled"]
    return {
        "wallet": name, "maker_orders": len(orders), "observable": len(obs), "matched": len(hit),
        "match_rate": _r(len(hit) / len(obs)) if obs else None,
        "placebo_matched": pl, "placebo_rate": _r(pl / len(obs)) if obs else None,
        "match_rate_by_side": {s: _r(sum(o.side == s for o in hit) / n) for s in ("BUY", "SELL")
                               if (n := sum(o.side == s for o in obs))},
        "matched_all": len(rs),
        "sign_to_book_ms": _q([r["sign_to_book_ms"] for r in rs]),
        "tw_minus_created_ms": _q([r["placed_tw_ns"] / 1e6 - r["created_ms"] for r in rs]),
        "outcomes": dict(Counter(f"{r['outcome']}:{r['reason']}" for r in rs)),
        "cancelled_alone": sum(r["alone"] for r in canc),
        "cancelled_no_prints_near": sum(r["prints_near"] == 0 for r in canc),
        "cancelled_seen_as_price_change": sum(r["end_src"] == "price_change" for r in canc),
        "fill_before_placement": sum(r["fill_before_placement"] for r in rs),
        "fills": {"n": sum(r["n_fills"] for r in rs), "located_by_print": sum(r["fills_located"] for r in rs),
                  "print_at_level": sum(r["print_price_ok"] for r in rs),
                  "print_size_equal": sum(r["print_size_ok"] for r in rs)},
        "rest_ms_known": {k: _q([r["rest_max_ms"] for r in rs if r["rest_known"] and r["outcome"] == k])
                          for k in ("filled", "cancelled")},
        "rest_not_known": sum(not r["rest_known"] for r in rs),
        "rest_censored": sum(r["rest_max_ms"] is None for r in rs),
    }


def run(root: Path, orders_path: Path, t_from: int, t_to: int, wallets: list[str] | None = None,
        p: Params = Params(), streams: list[str] | None = None, min_orders: int = 30, *,
        min_fp_orders: int = 0, focus: bool = False,
        quote_sink: Callable[[dict], object] | None = None) -> tuple[dict, list[dict], list[dict]]:
    """Returns (doc, per-order rows, fingerprint-candidate rows).

    Memory controls for a cohort run (defaults keep the tool's behaviour):
    ``min_fp_orders``: a chosen wallet with fewer maker orders in the range gets no fingerprint,
    and ``attribute`` is not run for it (a fingerprint from a handful of orders is noise).
    ``focus``: full level events are kept only for the markets of the CHOSEN wallets' tokens
    (other makers' orders are still loaded, so placements stay one-to-one and their matches in
    those markets still count as ``other_known``); ``others_pooled`` is then partial.
    ``quote_sink``: candidate rows go to it one at a time and the returned list is empty."""
    orders, active, stats = load_orders(orders_path, t_from, t_to)
    by_maker: dict[str, list[Order]] = defaultdict(list)
    for o in orders:
        by_maker[o.maker].append(o)
    chosen = ([w.lower() for w in wallets] if wallets else
              sorted(w for w, os_ in by_maker.items() if len(os_) >= min_orders))
    fps = {w: fingerprint([o.size for o in by_maker.get(w, [])])
           if len(by_maker.get(w, [])) >= min_fp_orders else [] for w in chosen}
    streams = streams or sorted(d.name for d in Path(root).iterdir() if d.name.startswith("clob_"))
    tape = build_tape(root, streams, t_from, t_to,
                      {t for w, ts in active.items() if not focus or w in chosen for t in ts},
                      sorted({s for f in fps.values() for s in f}), p)
    matches = match_placements(orders, tape, p)
    placebo = match_placements(orders, tape, p, p.placebo_ms)
    claimed: set[tuple[Key, int]] = set()
    rows = [classify(o, *matches[o.order_id], tape, claimed, p) for o in orders if o.order_id in matches]
    doc = {"params": {"from": t_from, "to": t_to, **p.__dict__, "max_age_s": MAX_AGE_S,
                      "streams": streams},
           "orders": dict(stats), "tape": dict(tape.counts) | {
               "level_keys": len(tape.events), "assets": len(tape.spans),
               "prints": sum(len(v) for v in tape.prints.values())},
           "wallets": {}}
    quotes: list[dict] = []
    for w in chosen:
        d = summarize(w, by_maker.get(w, []), matches, placebo, rows, tape, p)
        d["fingerprint"] = None
        if fps[w]:
            d["fingerprint"], q = attribute(w, fps[w], orders, matches, active.get(w, set()), tape, claimed, p,
                                            quote_sink)
            quotes += q
        doc["wallets"][w] = d
    rest = [o for o in orders if o.maker not in chosen]
    if rest:
        doc["others_pooled"] = summarize("others", rest, matches, placebo, rows, tape, p)
    return doc, rows, quotes


def card(doc: dict) -> str:
    L = [f"orders: {doc['orders']}", f"tape: {doc['tape']}"]
    for d in list(doc["wallets"].values()) + ([doc["others_pooled"]] if "others_pooled" in doc else []):
        L += [f"\n== {d['wallet']}",
              f"placement: {d['matched']}/{d['observable']} observable orders matched "
              f"(rate {d['match_rate']}) vs placebo {d['placebo_matched']} (rate {d['placebo_rate']}); "
              f"by side {d['match_rate_by_side']}",
              f"sign-to-book ms (venue - created): {d['sign_to_book_ms']}; tw - created: {d['tw_minus_created_ms']}",
              f"outcomes: {d['outcomes']} | cancelled alone {d['cancelled_alone']}, "
              f"no prints near {d['cancelled_no_prints_near']}, as price_change "
              f"{d['cancelled_seen_as_price_change']} | WRONG MATCHES (fill printed before placement): "
              f"{d['fill_before_placement']}",
              f"fills: {d['fills']}",
              f"resting ms where known: {d['rest_ms_known']}; not known {d['rest_not_known']} "
              f"(censored {d['rest_censored']})"]
        fp = d.get("fingerprint")
        if fp:
            t = fp["total"]
            L += [f"fingerprint sizes {fp['sizes']} (ESTIMATE, not identification): "
                  f"own {t['own']}, other makers proven {t['other_known']}, candidates {t['candidates']} "
                  f"(own filled-but-unmatched hiding among them: {t['own_unmatched']})",
                  f"  chance: control {t['control']} in {fp['control_token_hours']} token-h -> expected "
                  f"{t['expected_by_chance']} in {fp['active_token_hours']} active token-h; "
                  f"net {t['net_estimate']}; identity precision <= {t['identity_precision_upper']}",
                  f"  per size: { {s: (v['candidates'], v['expected_by_chance'], v['other_known']) for s, v in fp['per_size'].items()} }"
                  " (candidates, expected, other_known)",
                  f"  candidate outcomes {fp['candidate_outcomes']}; resting ms of cancelled {fp['candidate_rest_ms_cancelled']}"]
        elif "fingerprint" in d:
            L.append("fingerprint: none (sizes too diffuse)")
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-ordermatch")
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--orders", type=Path, required=True,
                    help="doppel-ordertape output: JSONL sample, or the cohort parquet directory "
                         "(verified rows only)")
    ap.add_argument("--wallet", nargs="+", action="extend", help="maker address(es) (default: makers "
                    "with >= --min-orders orders)")
    ap.add_argument("--from", dest="t_from", type=_ts, required=True, help="unix s or ISO UTC")
    ap.add_argument("--to", dest="t_to", type=_ts, required=True)
    ap.add_argument("--out", type=Path, required=True, help="directory")
    ap.add_argument("--min-orders", type=int, default=30)
    ap.add_argument("--lo-ms", type=int, default=Params.lo_ms)
    ap.add_argument("--hi-ms", type=int, default=Params.hi_ms)
    ap.add_argument("--size-tol", type=float, default=Params.size_tol, help="shares")
    ap.add_argument("--size-tol-rel", type=float, default=Params.size_tol_rel)
    a = ap.parse_args(argv)
    p = Params(lo_ms=a.lo_ms, hi_ms=a.hi_ms, size_tol=a.size_tol, size_tol_rel=a.size_tol_rel)
    doc, rows, quotes = run(a.data, a.orders, a.t_from, a.t_to, a.wallet, p, min_orders=a.min_orders)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "ordermatch.json").write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
    for name, recs in (("orders", rows), ("quotes", quotes)):
        with open(a.out / f"{name}.jsonl", "wb") as f:
            for r in recs:
                f.write(orjson.dumps(r) + b"\n")
    print(card(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
