"""Astra #3a: same-settlement structural scanner (PM US + Kalshi crypto).

Contracts that settle on the SAME observation (same index, same statistic, same instant) must be
priced consistently. If YES(B) implies YES(A), then YES(A) + NO(B) pays at least $1 whatever the
observation, so buying both for less than $1 after fees is riskless. The same idea covers two
disjoint sets (NO + NO pays at least $1), two sets that cover the line (YES + YES), and a tiling of
mutually exclusive buckets (all NO pays at least n - 1; all YES pays exactly 1 if the tiling is
gap-free).

1. Normaliser. Every PM US / Kalshi contract becomes a :class:`Contract`: index symbol, statistic,
   rounding, settlement instant (unix s) and the YES set as a half-open integer interval
   ``[lo, hi)`` in units of ``10**-decimals`` (``None`` = unbounded). The statistic is read from the
   RULES TEXT, never inferred from the family alone: the hand-listed year-end PM US range ladder
   carries typed ``assetPriceTerms`` but settles on a trimmed mean. Anything unknown or ambiguous is
   an :class:`Excluded` with a reason code, never a guess. One-Touch is path dependent (rolling
   trimmed mean, any time in the window) and is always excluded.
   Rounding: PM US automated families and Kalshi 15m say "rounded to the nearest 2 decimal places".
   KXBTCD / KXBTC rules do not state a rounding. Those contracts are mapped onto the same cent grid
   (``rounding="unstated"``) and every relation that depends on a boundary with zero margin to an
   unstated-rounding contract is flagged ``tie_unverified`` and kept out of headline dollars.
2. Discovery. Public listing endpoints (bounded pages, cached JSON, injectable ``fetch``).
3. Scanner over recorded books. PM US books come from the WS streams ONLY (REST is CDN-stale, E22).
   Kalshi books come from the ``kalshi_books`` REST poll, whose freshness is unverified, so every
   combination with a Kalshi leg carries ``kalshi_rest``; ``kalshi_ws`` is the slot for a future WS
   recorder. Each side of a book is parsed independently (ladder wings are one-sided) and the latest
   snapshot wins, so a pulled side is not resurrected by an older quote. Rules follow ``xvenue``:
   every leg's book at most ``max_age`` (1 s) old at the evaluation second, leg skew (max - min book
   time) at most ``max_skew`` (0.5 s), persistence at least ``persist`` (2) consecutive seconds,
   and each episode valued ONCE at its first second's net edge x displayed size.
4. One-leg failure. For each ordering of the legs, the cost change of the remaining legs 1 s and 5 s
   after the first leg's book time, read from the tape. A missing quote is null and counted, never
   zero. Kalshi REST is polled at about 1 s, which limits the +1 s figure.
5. Ledger. Append-only JSONL (schema ``SCHEMA``): one ``run`` header (params, fees, code SHA), one row
   per excluded contract, per class, per static skip, per opportunity episode, and per (combination,
   reason) skip aggregate, plus a per-combination coverage row.

This is a measurement on displayed quotes, not a P&L. The per-combination dollar sum is an UPPER
bound (one bad quote appears in many pairs); ``leg_once_dollars`` uses each leg's depth once.
"""

from __future__ import annotations

import argparse
import hashlib
import bisect
import math
import re
import sys
import time
import urllib.parse
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import orjson

from ..tape.segment import read_segment
from .pmus_tape import WS_STREAMS as PMUS_STREAMS
from .xvenue import KALSHI, PMUS, _get

SCHEMA = 1
KALSHI_SERIES = ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXBTCD", "KXBTC", "KXETHD", "KXETH")
KALSHI_STRIKE_TYPES = ("greater", "greater_or_equal", "less", "less_or_equal", "between")
# stream -> fresh (verified to reflect the live book at receive time)
KALSHI_STREAMS = {"kalshi_books": False, "kalshi_ws": True}
# Kalshi rules-text index name -> PM US indexSymbol. EMPTY on purpose: Kalshi writes ETHUSDRTI / SOLUSDRTI /
# XRPUSDRTI (15m) and ERTI (KXETHD/KXETH) where PM US says ETHUSD_RTI etc. Add an entry only after settlement
# values match to the cent on both venues (as E23 did for BRTI); until then those classes stay separate.
INDEX_ALIASES: dict[str, str] = {}
LOOSE_AGE_S = 30.0   # quotes older than this are not evaluated at all (no_quote)
UPDOWN_SKIP_S = 5    # first seconds of an up/down window are not scanned (strike stamping, xvenue)

Fetch = Callable[[str], Any]


# --------------------------------------------------------------------------------------------
# contracts


@dataclass(frozen=True)
class Contract:
    venue: str            # "pmus" | "kalshi"
    key: str              # PM US market slug / Kalshi ticker (the book key on the tape)
    index: str            # e.g. "BRTI"
    stat: str             # "mean60" (simple average of [T-60 s, T)) | "trim20_60s"
    decimals: int         # grid of the interval: units of 10**-decimals
    rounding: str         # "stated" | "unstated" (rules text gives no rounding; cent grid assumed)
    instant: int          # settlement instant, unix s (PM US windowEnd / Kalshi close_time)
    lo: int | None        # YES set = [lo, hi) in units; None = unbounded
    hi: int | None
    family: str           # e.g. "pmus:updown-15m", "pmus:strike-weekly", "kalshi:KXBTCD"
    group: str            # ladder / event id (tiling candidates share it)
    start: int | None = None          # up/down window start (scan from start + UPDOWN_SKIP_S)
    maker_coef: float | None = 0.0    # None = maker fee unknown for this contract
    label: str = ""

    @property
    def cls(self) -> str:
        return f"{self.index}|{self.stat}|{self.instant}"


@dataclass(frozen=True)
class Excluded:
    venue: str
    key: str
    reason: str
    detail: str = ""


def _iso(s: str) -> int:
    return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())


def _units(x: Any, d: int) -> Decimal:
    return Decimal(str(x)) * (Decimal(10) ** d)


def _ceil(q: Decimal) -> int:
    return int(q.to_integral_value(rounding=ROUND_CEILING))


def _floor(q: Decimal) -> int:
    return int(q.to_integral_value(rounding=ROUND_FLOOR))


def interval_units(lo: Any, lo_closed: bool, hi: Any, hi_closed: bool, d: int) -> tuple[int | None, int | None]:
    """Half-open [lo_u, hi_u) on the 10**-d grid of {x : lo (<|<=) x (<|<=) hi}, x on the grid."""
    lo_u = None if lo is None else (_ceil(_units(lo, d)) if lo_closed else _floor(_units(lo, d)) + 1)
    hi_u = None if hi is None else (_floor(_units(hi, d)) + 1 if hi_closed else _ceil(_units(hi, d)))
    return lo_u, hi_u


_ROUND_RE = re.compile(r"rounded to the nearest (\d+) decimal")


def _norm_text(s: str) -> str:
    return " ".join((s or "").split()).lower()


def parse_statistic(text: str) -> tuple[str | None, int | None, str]:
    """(stat, stated_decimals, reason). stat None means excluded with ``reason``."""
    t = _norm_text(text)
    m = _ROUND_RE.search(t)
    dec = int(m.group(1)) if m else None
    if "trimmed mean" in t:
        if "20%" in t and ("sixty second" in t or "60 second" in t or "sixty-second" in t):
            return "trim20_60s", dec, ""
        return None, None, "statistic_ambiguous"
    simple = "simple average of these prices" in t or "simple average of the sixty seconds" in t
    collected = ("60 " in t and "prices are collected" in t and "average of these prices" in t)
    if simple or collected:
        if "last minute before" not in t and "sixty seconds" not in t:
            return None, None, "statistic_window_unparsed"
        return "mean60", dec, ""
    return None, None, "statistic_unparsed"


_PM_TYPES = {"ASSET_PRICE_MARKET_TYPE_UP_DOWN": "updown", "ASSET_PRICE_MARKET_TYPE_ABOVE_BELOW": "strike",
             "ASSET_PRICE_MARKET_TYPE_RANGE": "range", "ASSET_PRICE_MARKET_TYPE_ONE_TOUCH": "hit"}


def _amt(a: Any) -> str | None:
    if isinstance(a, dict):
        v = a.get("value")
        return None if v in (None, "") else str(v)
    return None


def normalise_pmus(m: dict) -> Contract | Excluded:
    key = m.get("slug") or str(m.get("id", "?"))
    a = m.get("assetPriceTerms")
    if not a:
        return Excluded("pmus", key, "no_asset_terms")
    if (a.get("asset") or {}).get("assetClass") != "ASSET_CLASS_CRYPTO":
        return Excluded("pmus", key, "not_crypto")
    fam = _PM_TYPES.get(a.get("marketType", ""))
    if fam is None:
        return Excluded("pmus", key, "unknown_market_type", str(a.get("marketType")))
    if fam == "hit":
        return Excluded("pmus", key, "one_touch_path_dependent")
    index = a.get("indexSymbol") or ""
    if not index:
        return Excluded("pmus", key, "no_index")
    desc = m.get("description") or ""
    if index.lower() not in desc.lower():
        return Excluded("pmus", key, "index_text_mismatch", index)
    stat, dec, why = parse_statistic(desc)
    if stat is None:
        return Excluded("pmus", key, why)
    pdec = a.get("priceDecimals")
    if not isinstance(pdec, int):
        return Excluded("pmus", key, "no_price_decimals")
    if dec is not None and dec != pdec:
        return Excluded("pmus", key, "decimals_conflict", f"text {dec} vs priceDecimals {pdec}")
    if not a.get("windowEnd"):
        return Excluded("pmus", key, "no_instant")
    instant = _iso(a["windowEnd"])
    horizon = a.get("horizon") or "?"
    start = None
    if fam == "updown":
        ptb = _amt(a.get("priceToBeat"))
        if ptb is None:
            return Excluded("pmus", key, "strike_pending")
        if not a.get("windowStart"):
            return Excluded("pmus", key, "no_window_start")
        start = _iso(a["windowStart"])
        lo, hi = interval_units(ptb, True, None, False, pdec)   # close >= open settles Up
        label = f">= {ptb}"
    elif fam == "strike":
        k = _amt(a.get("strike"))
        if k is None:
            return Excluded("pmus", key, "no_strike")
        lo, hi = interval_units(k, True, None, False, pdec)     # at or above the strike
        label = f">= {k}"
    else:
        kind = a.get("rangeKind", "")
        rl, ru = _amt(a.get("rangeLower")), _amt(a.get("rangeUpper"))
        want = {"ASSET_PRICE_RANGE_KIND_INTERIOR": (True, True), "ASSET_PRICE_RANGE_KIND_BELOW": (False, True),
                "ASSET_PRICE_RANGE_KIND_ABOVE": (True, False)}.get(kind)
        if want is None or (rl is not None) != want[0] or (ru is not None) != want[1]:
            return Excluded("pmus", key, "range_bounds_inconsistent", f"{kind} {rl} {ru}")
        lo, hi = interval_units(rl, True, ru, True, pdec)       # inclusive on both bounds
        label = f"[{rl}, {ru}]"
    group = m.get("eventSlug") or m.get("eventId") or f"pmus:{fam}-{horizon}:{index}:{instant}"
    return Contract("pmus", key, index, stat, pdec, "stated" if dec is not None else "unstated", instant,
                    lo, hi, f"pmus:{fam}-{horizon}", str(group), start, 0.0, label)


_KIDX_RE = re.compile(r"\b([A-Z][A-Z0-9_]*RTI)\b")


def normalise_kalshi(m: dict, series_fee: dict | None = None) -> Contract | Excluded:
    key = m.get("ticker") or "?"
    st = m.get("strike_type")
    if st not in KALSHI_STRIKE_TYPES:
        return Excluded("kalshi", key, "strike_type_unsupported", str(st))
    prim, sec = m.get("rules_primary") or "", m.get("rules_secondary") or ""
    idx = {x for x in _KIDX_RE.findall(prim) if x != "RTI"}
    if len(idx) != 1:
        return Excluded("kalshi", key, "index_unparsed" if not idx else "index_ambiguous", ",".join(sorted(idx)))
    raw_index = idx.pop()
    index: str = INDEX_ALIASES.get(raw_index) or str(raw_index)
    stat, dec, why = parse_statistic(prim + " " + sec)
    if stat is None:
        return Excluded("kalshi", key, why)
    if not m.get("close_time"):
        return Excluded("kalshi", key, "no_instant")
    instant = _iso(m["close_time"])
    d = dec if dec is not None else 2
    f, c = m.get("floor_strike"), m.get("cap_strike")
    need = {"greater": (True, False), "greater_or_equal": (True, False), "less": (False, True),
            "less_or_equal": (False, True), "between": (True, True)}[st]
    if (f is not None) != need[0] or (c is not None) != need[1]:
        if need[0] and f is None and str(key).split("-")[0].endswith("15M"):
            return Excluded("kalshi", key, "strike_pending")
        return Excluded("kalshi", key, "strike_fields_inconsistent", f"{st} {f} {c}")
    lo, hi = {"greater": lambda: interval_units(f, False, None, False, d),
              "greater_or_equal": lambda: interval_units(f, True, None, False, d),
              "less": lambda: interval_units(None, False, c, False, d),
              "less_or_equal": lambda: interval_units(None, False, c, True, d),
              "between": lambda: interval_units(f, True, c, True, d)}[st]()
    series = (m.get("event_ticker") or key).split("-")[0]
    start = None
    if series.endswith("15M") and m.get("open_time"):
        start = _iso(m["open_time"])
    fee = (series_fee or {}).get(series) or {}
    maker = 0.0 if fee.get("fee_type") == "quadratic" else None
    return Contract("kalshi", key, index, stat, d, "stated" if dec is not None else "unstated", instant,
                    lo, hi, f"kalshi:{series}", m.get("event_ticker") or series, start, maker,
                    f"{st} {f if f is not None else ''}{'-' if f is not None and c is not None else ''}"
                    f"{c if c is not None else ''}")


def normalise_cache(cache: dict) -> tuple[list[Contract], list[Excluded]]:
    out: list[Contract] = []
    exc: list[Excluded] = []
    rs = [normalise_pmus(m) for m in (cache.get("pmus") or {}).values()]
    rs += [normalise_kalshi(m, cache.get("kalshi_series")) for m in (cache.get("kalshi") or {}).values()]
    for r in rs:
        if isinstance(r, Contract):
            out.append(r)
        else:
            exc.append(r)
    return out, exc


def classes(contracts: Iterable[Contract]) -> dict[str, list[Contract]]:
    out: dict[str, list[Contract]] = defaultdict(list)
    for c in contracts:
        out[c.cls].append(c)
    return dict(out)


# --------------------------------------------------------------------------------------------
# relations and combinations

INF = math.inf


def _lo(c: Contract) -> float:
    return -INF if c.lo is None else c.lo


def _hi(c: Contract) -> float:
    return INF if c.hi is None else c.hi


def _gap(x: float, y: float) -> float:
    """y - x where an infinite pair (same unbounded side) has no boundary, hence no tie risk."""
    if math.isinf(x) and math.isinf(y):
        return INF
    return y - x


def relations(a: Contract, b: Contract) -> list[tuple[str, str, str, float]]:
    """Riskless two-leg combinations as (side_a, side_b, relation, margin_units). Payout >= 1."""
    out: list[tuple[str, str, str, float]] = []
    if _lo(a) <= _lo(b) and _hi(b) <= _hi(a):           # B subset of A: YES a + NO b
        out.append(("yes", "no", "contains", min(_gap(_lo(a), _lo(b)), _gap(_hi(b), _hi(a)))))
    if _lo(b) <= _lo(a) and _hi(a) <= _hi(b):           # A subset of B: NO a + YES b
        out.append(("no", "yes", "contained", min(_gap(_lo(b), _lo(a)), _gap(_hi(a), _hi(b)))))
    if _hi(a) <= _lo(b):
        out.append(("no", "no", "disjoint", _lo(b) - _hi(a)))
    elif _hi(b) <= _lo(a):
        out.append(("no", "no", "disjoint", _lo(a) - _hi(b)))
    if a.lo is None and b.hi is None and _lo(b) <= _hi(a):
        out.append(("yes", "yes", "covers", _hi(a) - _lo(b)))
    elif b.lo is None and a.hi is None and _lo(a) <= _hi(b):
        out.append(("yes", "yes", "covers", _hi(b) - _lo(a)))
    return out


@dataclass(frozen=True)
class Combo:
    cls: str
    kind: str                                  # "pair" | "tiling_no" | "tiling_yes"
    relation: str
    legs: tuple[tuple[Contract, str], ...]     # (contract, "yes"|"no")
    payout: int                                # guaranteed minimum payout, $ per set
    tie_unverified: bool = False

    @property
    def id(self) -> str:
        return f"{self.kind}:" + "+".join(f"{c.venue}:{c.key}:{s}" for c, s in self.legs)


def build_combos(members: list[Contract]) -> tuple[list[Combo], list[dict]]:
    """All riskless combinations inside one equivalence class, plus static skip rows."""
    combos: list[Combo] = []
    skips: Counter[str] = Counter()
    ms = sorted(members, key=lambda c: (c.venue, c.key))
    for i, a in enumerate(ms):
        for b in ms[i + 1:]:
            if a.decimals != b.decimals:
                skips["rounding_mismatch"] += 1
                continue
            for sa, sb, rel, margin in relations(a, b):
                tie = margin == 0 and "unstated" in (a.rounding, b.rounding)
                combos.append(Combo(a.cls, "pair", rel, ((a, sa), (b, sb)), 1, tie))
    groups: dict[tuple[str, str], list[Contract]] = defaultdict(list)
    for c in ms:
        groups[(c.venue, c.group)].append(c)
    for g in groups.values():
        if len(g) < 3 or len({c.decimals for c in g}) != 1:
            continue
        g = sorted(g, key=_lo)
        if not all(_hi(x) <= _lo(y) for x, y in zip(g, g[1:])):
            continue                                       # a ladder, not a tiling
        tie = "unstated" in {c.rounding for c in g} and any(_hi(x) == _lo(y) for x, y in zip(g, g[1:]))
        combos.append(Combo(g[0].cls, "tiling_no", "tiling", tuple((c, "no") for c in g), len(g) - 1, tie))
        gapless = g[0].lo is None and g[-1].hi is None and all(_hi(x) == _lo(y) for x, y in zip(g, g[1:]))
        if gapless:
            combos.append(Combo(g[0].cls, "tiling_yes", "tiling", tuple((c, "yes") for c in g), 1, tie))
        else:
            skips["incomplete_tiling"] += 1
    rows = [{"type": "static_skip", "cls": members[0].cls if members else "", "reason": r, "count": n}
            for r, n in sorted(skips.items())]
    return combos, rows


# --------------------------------------------------------------------------------------------
# fees


@dataclass(frozen=True)
class Fees:
    pmus_taker: float = 0.095      # gateway feeCoefficient (xvenue.PMUS_COEF); fees.md says API 0.0695
    kalshi_taker: float = 0.07
    pmus_maker: float = 0.0        # maker rebates ignored (conservative)

    def taker(self, venue: str, p: float, n: float) -> float:
        """Dollars for an n-contract taker order at p (xvenue rounding conventions)."""
        if venue == "pmus":
            return round(self.pmus_taker * n * p * (1 - p), 2)
        return math.ceil(self.kalshi_taker * n * p * (1 - p) * 100 - 1e-9) / 100

    def maker(self, c: Contract, p: float, n: float) -> float | None:
        if c.venue == "pmus":
            return round(self.pmus_maker * n * p * (1 - p), 2)
        if c.maker_coef is None:
            return None
        return math.ceil(c.maker_coef * n * p * (1 - p) * 100 - 1e-9) / 100


# --------------------------------------------------------------------------------------------
# books


@dataclass
class Book:
    """Snapshots of one contract: t (recv s), yes bid/qty, yes ask/qty (None = side empty)."""
    t: list[float] = field(default_factory=list)
    q: list[tuple[float | None, float, float | None, float]] = field(default_factory=list)
    fresh: bool = True

    def add(self, t: float, bid: float | None, bq: float, ask: float | None, aq: float) -> None:
        self.t.append(t)
        self.q.append((bid, bq, ask, aq))

    def sort(self) -> None:
        z = sorted(zip(self.t, self.q), key=lambda x: x[0])
        self.t, self.q = [x[0] for x in z], [x[1] for x in z]

    def leg(self, at: float, side: str) -> tuple[float, float, float] | None:
        """(price to BUY side, qty, book time) from the latest snapshot at or before ``at``."""
        i = bisect.bisect_right(self.t, at) - 1
        if i < 0:
            return None
        bid, bq, ask, aq = self.q[i]
        if side == "yes":
            return None if ask is None or aq <= 0 else (ask, aq, self.t[i])
        return None if bid is None or bq <= 0 else (round(1.0 - bid, 6), bq, self.t[i])


def parse_pmus_ws(raw: str) -> tuple[str, float | None, float, float | None, float] | None:
    if not raw or '"marketData"' not in raw:
        return None
    md = orjson.loads(raw).get("marketData") or {}
    slug = md.get("marketSlug")
    if not slug:
        return None
    b, o = md.get("bids") or [], md.get("offers") or []
    bb = max(b, key=lambda x: float(x["px"]["value"])) if b else None
    oo = min(o, key=lambda x: float(x["px"]["value"])) if o else None
    return (slug, float(bb["px"]["value"]) if bb else None, float(bb["qty"]) if bb else 0.0,
            float(oo["px"]["value"]) if oo else None, float(oo["qty"]) if oo else 0.0)


def parse_kalshi_rest(raw: str) -> tuple[float | None, float, float | None, float] | None:
    d = orjson.loads(raw)
    ob = d.get("orderbook_fp") or d.get("orderbook")
    if not isinstance(ob, dict):
        return None
    yes, no = ob.get("yes_dollars") or [], ob.get("no_dollars") or []
    yb = max(yes, key=lambda x: float(x[0])) if yes else None
    nb = max(no, key=lambda x: float(x[0])) if no else None
    return (float(yb[0]) if yb else None, float(yb[1]) if yb else 0.0,
            round(1.0 - float(nb[0]), 6) if nb else None, float(nb[1]) if nb else 0.0)


def _segments(root: Path, stream: str, t0: float, t1: float) -> list[Path]:
    hours = set()
    h = int(t0 // 3600 * 3600)
    while h <= t1:
        hours.add(time.strftime("%Y%m%d/%H", time.gmtime(h)))
        h += 3600
    return [p for p in sorted((root / stream).glob("*/*.dtape.zst"))
            if f"{p.parent.name}/{p.name[:2]}" in hours]


def load_books(root: Path, keys: dict[str, set[str]], t0: float, t1: float,
               kalshi_stream: str = "kalshi_books") -> dict[tuple[str, str], Book]:
    """(venue, key) -> Book for the wanted keys, from PM US WS streams and the Kalshi stream."""
    books: dict[tuple[str, str], Book] = {}
    lo, hi = t0 - LOOSE_AGE_S - 5, t1 + 10
    want_pm = keys.get("pmus", set())
    for st in PMUS_STREAMS:
        for f in _segments(root, st, lo, hi):
            for rec in read_segment(f, verify=True):
                t = rec["tw"] / 1e9
                if not lo <= t <= hi:
                    continue
                p = parse_pmus_ws(rec["raw"])
                if p is None or p[0] not in want_pm:
                    continue
                books.setdefault(("pmus", p[0]), Book()).add(t, *p[1:])
    want_k = keys.get("kalshi", set())
    if kalshi_stream not in KALSHI_STREAMS:
        raise ValueError(f"unknown kalshi stream {kalshi_stream}")
    if kalshi_stream == "kalshi_ws":
        raise NotImplementedError("kalshi_ws is recorded by the Rust recorder (tape-rust); the Python parser is not written yet")
    for f in _segments(root, kalshi_stream, lo, hi):
        for rec in read_segment(f, verify=True):
            m = rec.get("meta") or {}
            k = m.get("key", "")
            t = rec["tw"] / 1e9
            if m.get("status") != 200 or k not in want_k or not lo <= t <= hi:
                continue
            q = parse_kalshi_rest(rec["raw"])
            if q is not None:
                books.setdefault(("kalshi", k), Book(fresh=KALSHI_STREAMS[kalshi_stream])).add(t, *q)
    for b in books.values():
        b.sort()
    return books


# --------------------------------------------------------------------------------------------
# scanner


@dataclass(frozen=True)
class Params:
    max_age: float = 1.0
    max_skew: float = 0.5
    persist: int = 2


def _price(books: dict[tuple[str, str], Book], c: Contract, side: str, at: float):
    b = books.get((c.venue, c.key))
    return None if b is None else b.leg(at, side)


def leg2_moves(books: dict[tuple[str, str], Book], combo: Combo, quotes: list[tuple[float, float, float]],
               deltas: tuple[int, ...] = (1, 5), max_age: float = 1.0) -> list[dict]:
    """For each choice of first leg: cost change (c/set, + = adverse) of the remaining legs ``d`` s
    after the first leg's book time. None when any remaining leg has no quote then, or only one older
    than ``max_age`` (a stalled stream or failed poll is unknown, not a zero move)."""
    out = []
    for i, (ci, _si) in enumerate(combo.legs):
        t_first = quotes[i][2]
        row: dict[str, Any] = {"first": f"{ci.venue}:{ci.key}"}
        for d in deltas:
            mv = 0.0
            ok = True
            for j, (cj, sj) in enumerate(combo.legs):
                if j == i:
                    continue
                q = _price(books, cj, sj, t_first + d)
                if q is None or t_first + d - q[2] > max_age:
                    ok = False
                    break
                mv += q[0] - quotes[j][0]
            row[f"move_{d}s_c"] = round(100 * mv, 2) if ok else None
        row["kalshi_poll_limited"] = any(c.venue == "kalshi" for j, (c, _) in enumerate(combo.legs) if j != i)
        out.append(row)
    return out


def _runs(points: list[tuple[int, bool]]) -> list[list[int]]:
    """Group indices of consecutive seconds with the same ok flag."""
    runs: list[list[int]] = []
    for idx, (s, ok) in enumerate(points):
        if runs and s == points[runs[-1][-1]][0] + 1 and points[runs[-1][-1]][1] == ok:
            runs[-1].append(idx)
        else:
            runs.append([idx])
    return runs


def scan_class(members: list[Contract], books: dict[tuple[str, str], Book], t0: int, t1: int,
               params: Params = Params(), fees: Fees = Fees(), kalshi_fresh: bool = False) -> Iterator[dict]:
    """Ledger rows for one equivalence class over seconds [t0, t1]."""
    combos, static = build_combos(members)
    yield from static
    memo: dict[tuple[str, str, str, int], tuple[float, float, float] | None] = {}

    def quote(c: Contract, side: str, s: int) -> tuple[float, float, float] | None:
        k = (c.venue, c.key, side, s)
        if k not in memo:
            memo[k] = _price(books, c, side, float(s))
        return memo[k]

    for combo in combos:
        cs = [c for c, _ in combo.legs]
        lo = max([t0] + [c.start + UPDOWN_SKIP_S for c in cs if c.start is not None])
        hi = min(t1, combo.legs[0][0].instant - 1)
        status: Counter[str] = Counter()
        pos: list[tuple[int, bool, str, float, float | None, float, list, float]] = []
        kalshi_rest = not kalshi_fresh and any(c.venue == "kalshi" for c in cs)
        fx: dict[str, Any] = {"seconds": 0, "ok_seconds": 0, "best_gross_c": -INF, "best_net_c": -INF,
                              "best_net_maker_c": None}
        missing = [f"{c.venue}:{c.key}" for c in cs if (c.venue, c.key) not in books]
        if missing:
            yield {"type": "coverage", "cls": combo.cls, "combo": combo.id, "kind": combo.kind,
                   "relation": combo.relation, "seconds": max(0, hi - lo + 1), "no_book": max(0, hi - lo + 1),
                   "missing_books": missing}
            continue
        for s in range(lo, hi + 1):
            qs = [quote(c, side, s) for c, side in combo.legs]
            if any(q is None or s - q[2] > LOOSE_AGE_S for q in qs):
                status["no_quote"] += 1
                continue
            qv: list[tuple[float, float, float]] = [q for q in qs if q is not None]
            gross = combo.payout - sum(q[0] for q in qv)
            if gross <= 0:                     # pre-fee filter: fees only make it worse
                status["evaluated"] += 1
                continue
            size = min(q[1] for q in qv)
            ft = sum(fees.taker(c.venue, q[0], size) for (c, _), q in zip(combo.legs, qv)) / size
            mk = [fees.maker(c, q[0], size) for (c, _), q in zip(combo.legs, qv)]
            net = gross - ft
            net_m = None if any(x is None for x in mk) else gross - sum(x for x in mk if x is not None) / size
            age = max(s - q[2] for q in qv)
            skew = max(q[2] for q in qv) - min(q[2] for q in qv)
            st = "stale" if age > params.max_age else ("skew" if skew > params.max_skew else "ok")
            status["evaluated"] += 1
            if net > 0:
                pos.append((s, st == "ok", st, net, net_m, size, qv, gross))
            else:                              # pre-fee edge that taker fees remove: keep it visible
                fx["seconds"] += 1
                fx["ok_seconds"] += st == "ok"
                fx["best_gross_c"] = max(fx["best_gross_c"], round(100 * gross, 2))
                fx["best_net_c"] = max(fx["best_net_c"], round(100 * net, 2))
                if net_m is not None:
                    fx["best_net_maker_c"] = max(fx["best_net_maker_c"] if fx["best_net_maker_c"] is not None
                                                 else -INF, round(100 * net_m, 2))
        yield {"type": "coverage", "cls": combo.cls, "combo": combo.id, "kind": combo.kind,
               "relation": combo.relation, "seconds": max(0, hi - lo + 1), **status}
        skip_agg: dict[str, dict] = {}
        for run in _runs([(p[0], p[1]) for p in pos]):
            first = pos[run[0]]
            if first[1] and len(run) >= params.persist:
                s, _, _, net, net_m, size, qv, gross = first
                yield {"type": "opportunity", "cls": combo.cls, "combo": combo.id, "kind": combo.kind,
                       "relation": combo.relation, "payout": combo.payout, "start": s, "secs": len(run),
                       "gross_c": round(100 * gross, 2), "net_c": round(100 * net, 2),
                       "net_maker_c": None if net_m is None else round(100 * net_m, 2),
                       "size": size, "dollars": round(net * size, 2),
                       "dollars_maker": None if net_m is None else round(net_m * size, 2),
                       "legs": [{"venue": c.venue, "key": c.key, "side": sd, "family": c.family,
                                 "price": q[0], "qty": q[1], "book_t": round(q[2], 3),
                                 "age_s": round(s - q[2], 3)} for (c, sd), q in zip(combo.legs, qv)],
                       "flags": {"kalshi_rest": kalshi_rest, "tie_unverified": combo.tie_unverified},
                       "leg2": leg2_moves(books, combo, qv, max_age=params.max_age)}
                continue
            reasons = Counter(pos[i][2] for i in run)
            reason = "not_persistent" if first[1] else reasons.most_common(1)[0][0]
            a = skip_agg.setdefault(reason, {"runs": 0, "seconds": 0, "best_net_c": -INF, "best_dollars": 0.0})
            a["runs"] += 1
            a["seconds"] += len(run)
            a["best_net_c"] = max(a["best_net_c"], round(100 * max(pos[i][3] for i in run), 2))
            a["best_dollars"] = max(a["best_dollars"], round(max(pos[i][3] * pos[i][5] for i in run), 2))
        if fx["seconds"]:
            skip_agg["fees_exceed_edge"] = {"runs": None, **fx}
        for reason, a in sorted(skip_agg.items()):
            yield {"type": "skip", "cls": combo.cls, "combo": combo.id, "kind": combo.kind, "reason": reason,
                   "flags": {"kalshi_rest": kalshi_rest, "tie_unverified": combo.tie_unverified}, **a}


def _code_sha() -> str | None:
    """sha256 of this module's source (git is absent in the deployed tree, audit A3-11)."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def scan(cache: dict, root: Path, t0: int, t1: int, params: Params = Params(), fees: Fees = Fees(),
         kalshi_stream: str = "kalshi_books", books: dict[tuple[str, str], Book] | None = None) -> list[dict]:
    """All ledger rows for one run over [t0, t1] (unix s)."""
    run = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]
    contracts, excluded = normalise_cache(cache)
    cl = {k: v for k, v in classes(contracts).items() if v[0].instant >= t0 and len(v) >= 2}
    keys: dict[str, set[str]] = defaultdict(set)
    for v in cl.values():
        for c in v:
            keys[c.venue].add(c.key)
    if books is None:
        books = load_books(root, keys, t0, t1, kalshi_stream)
    rows: list[dict] = [{"type": "run", "params": asdict(params), "fees": asdict(fees), "window": [t0, t1],
                         "code_sha": _code_sha(), "at": int(time.time()),
                         "streams": {"pmus": list(PMUS_STREAMS), "kalshi": kalshi_stream,
                                     "kalshi_fresh": KALSHI_STREAMS.get(kalshi_stream)},
                         "contracts": len(contracts), "excluded": len(excluded), "classes": len(cl)}]
    rows += [{"type": "excluded", **asdict(e)} for e in excluded]
    for k, members in sorted(cl.items()):
        booked = [c for c in members if (c.venue, c.key) in books]
        unbooked = Counter(c.family for c in members if (c.venue, c.key) not in books)
        rows.append({"type": "class", "cls": k, "unbooked": sum(unbooked.values()),
                     "unbooked_by_family": dict(unbooked), "members": [
            {"venue": c.venue, "key": c.key, "family": c.family, "lo": c.lo, "hi": c.hi, "decimals": c.decimals,
             "rounding": c.rounding} for c in booked]})
        if len(booked) >= 2:
            rows += list(scan_class(booked, books, t0, t1, params, fees, bool(KALSHI_STREAMS.get(kalshi_stream))))
    for r in rows:
        r["schema"] = SCHEMA
        r["run"] = run
    return rows


def append_ledger(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        for r in rows:
            fh.write(orjson.dumps(r) + b"\n")


# --------------------------------------------------------------------------------------------
# summary


def _headline(o: dict) -> bool:
    f = o.get("flags") or {}
    return not f.get("kalshi_rest") and not f.get("tie_unverified")


def _leg2(o: dict, key: str, worst: bool = False) -> float | None:
    """Best (min) or worst (max) ordering; worst is None if ANY ordering lacks a quote."""
    vals = [x.get(key) for x in o.get("leg2", [])]
    if worst:
        return None if not vals or any(v is None for v in vals) else max(vals)
    vals = [v for v in vals if v is not None]
    return min(vals) if vals else None


def leg_once_dollars(opps: list[dict]) -> float:
    """Greedy: largest episodes first, each (venue, key, side) leg depth used at most once."""
    used: set[tuple[str, str, str]] = set()
    tot = 0.0
    for o in sorted(opps, key=lambda o: -o["dollars"]):
        legs = {(x["venue"], x["key"], x["side"]) for x in o["legs"]}
        if legs & used:
            continue
        used |= legs
        tot += o["dollars"]
    return round(tot, 2)


def summarize(rows: list[dict]) -> dict:
    opps = [r for r in rows if r["type"] == "opportunity"]
    head = [o for o in opps if _headline(o)]
    secs = sorted(o["secs"] for o in opps)
    chase = {}
    for d in (1, 5):
        k = f"move_{d}s_c"
        for tag, worst in (("best_order", False), ("worst_order", True)):
            mv = [(o, _leg2(o, k, worst)) for o in opps]
            chase[f"{d}s_{tag}"] = {
                "missing": sum(m is None for _, m in mv),
                "loss_if_leg2_chased_dollars": round(sum(max(0.0, m) / 100 * o["size"] for o, m in mv
                                                         if m is not None), 2),
                "edge_wiped": sum(1 for o, m in mv if m is not None and m >= o["net_c"]),
            }
    per: dict[str, dict] = {}
    for o in opps:
        p = per.setdefault(o["cls"], {"opportunities": 0, "dollars": 0.0, "headline_dollars": 0.0, "_o": []})
        p["opportunities"] += 1
        p["dollars"] = round(p["dollars"] + o["dollars"], 2)
        p["headline_dollars"] = round(p["headline_dollars"] + (o["dollars"] if _headline(o) else 0.0), 2)
        p["_o"].append(o)
    for p in per.values():
        p["leg_once_dollars"] = leg_once_dollars(p.pop("_o"))
    skips: dict[str, dict] = {}
    for r in rows:
        if r["type"] == "skip":
            a = skips.setdefault(r["reason"], {"runs": 0, "seconds": 0})
            a["runs"] += r["runs"] or 0
            a["seconds"] += r["seconds"]
        elif r["type"] == "static_skip":
            a = skips.setdefault(r["reason"], {"runs": 0, "seconds": 0})
            a["runs"] += r["count"]
    cov: Counter[str] = Counter()
    for r in rows:
        if r["type"] == "coverage":
            for k in ("seconds", "evaluated", "no_quote", "no_book"):
                cov[k] += r.get(k, 0)
    fams: Counter[str] = Counter()
    for r in rows:
        if r["type"] == "class":
            fams[" + ".join(sorted({m["family"] for m in r["members"]} | set(r.get("unbooked_by_family", {}))))] += 1
    return {
        "run": rows[0]["run"] if rows else None,
        "classes": sum(r["type"] == "class" for r in rows),
        "class_compositions": dict(fams.most_common()),
        "excluded": dict(Counter(r["reason"] for r in rows if r["type"] == "excluded")),
        "combo_seconds": dict(cov),
        "opportunities": len(opps),
        "headline_opportunities": len(head),
        "persistence_s": {"p50": secs[len(secs) // 2] if secs else None, "max": secs[-1] if secs else None},
        "dollars_upper": round(sum(o["dollars"] for o in opps), 2),
        "headline_dollars_upper": round(sum(o["dollars"] for o in head), 2),
        "leg_once_dollars": round(sum(p["leg_once_dollars"] for p in per.values()), 2),
        "maker_dollars_upper": round(sum(o["dollars_maker"] or 0.0 for o in opps), 2),
        "one_leg_failure": chase,
        "skips": skips,
        "per_class": per,
        "note": "displayed-size measurement; dollars valued once per episode but summed across combinations "
                "(upper bound); headline excludes kalshi_rest (freshness unverified) and tie_unverified",
    }


# --------------------------------------------------------------------------------------------
# discovery


def _qs(params: dict) -> str:
    return urllib.parse.urlencode(params, doseq=True)


def discover(cache_path: Path, fetch: Fetch = _get, kalshi_series: Iterable[str] = KALSHI_SERIES,
             pmus_pages: int = 20, kalshi_pages: int = 5, pmus_slugs: Iterable[str] = (),
             kalshi_tickers: Iterable[str] = ()) -> dict:
    """Merge open crypto listings (and explicit keys) into the JSON cache. Bounded page counts."""
    cache = orjson.loads(cache_path.read_bytes()) if cache_path.exists() else {}
    pm: dict = cache.setdefault("pmus", {})
    ka: dict = cache.setdefault("kalshi", {})
    ser: dict = cache.setdefault("kalshi_series", {})
    errors: list[str] = []
    for page in range(pmus_pages):
        try:
            ms = fetch(f"{PMUS}/markets?" + _qs({"active": "true", "closed": "false", "categories": "crypto",
                                                 "limit": 200, "offset": 200 * page})).get("markets") or []
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pmus page {page}: {type(exc).__name__}")
            break
        for m in ms:
            pm[m.get("slug") or str(m.get("id"))] = m
        if len(ms) < 200:
            break
    slugs = sorted(set(pmus_slugs))
    for i in range(0, len(slugs), 20):
        try:
            for m in fetch(f"{PMUS}/markets?" + _qs({"slug": slugs[i:i + 20]})).get("markets") or []:
                pm[m.get("slug")] = m
        except Exception as exc:  # noqa: BLE001
            errors.append(f"pmus slugs {i}: {type(exc).__name__}")
    for s in kalshi_series:
        cursor = ""
        for _ in range(kalshi_pages):
            q = {"series_ticker": s, "status": "open", "limit": 1000}
            if cursor:
                q["cursor"] = cursor
            try:
                d = fetch(f"{KALSHI}/markets?" + _qs(q))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"kalshi {s}: {type(exc).__name__}")
                break
            for m in d.get("markets") or []:
                ka[m["ticker"]] = m
            cursor = d.get("cursor") or ""
            if not cursor:
                break
    tks = sorted(set(kalshi_tickers))
    for i in range(0, len(tks), 50):
        try:
            for m in fetch(f"{KALSHI}/markets?" + _qs({"tickers": ",".join(tks[i:i + 50])})).get("markets") or []:
                ka[m["ticker"]] = m
        except Exception as exc:  # noqa: BLE001
            errors.append(f"kalshi tickers {i}: {type(exc).__name__}")
    for s in sorted({(m.get("event_ticker") or t).split("-")[0] for t, m in ka.items()} - set(ser)):
        try:
            d = fetch(f"{KALSHI}/series/{s}").get("series") or {}
            ser[s] = {"fee_type": d.get("fee_type"), "fee_multiplier": d.get("fee_multiplier")}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"kalshi series {s}: {type(exc).__name__}")
    cache["fetched_at"] = int(time.time())
    cache["errors"] = errors
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(orjson.dumps(cache))
    return cache


def tape_keys(root: Path, t0: float, t1: float, kalshi_stream: str = "kalshi_books") -> tuple[set[str], set[str]]:
    """PM US slugs and Kalshi tickers that have books on the tape in [t0, t1] (for discovery)."""
    pm: set[str] = set()
    ka: set[str] = set()
    for st in PMUS_STREAMS:
        for f in _segments(root, st, t0, t1):
            for rec in read_segment(f, verify=True):
                p = parse_pmus_ws(rec["raw"])
                if p is not None:
                    pm.add(p[0])
    for f in _segments(root, kalshi_stream, t0, t1):
        for rec in read_segment(f, verify=True):
            k = (rec.get("meta") or {}).get("key")
            if k:
                ka.add(k)
    return pm, ka


def census(cache: dict) -> dict:
    contracts, excluded = normalise_cache(cache)
    comp: Counter[str] = Counter()
    for v in classes(contracts).values():
        venues = {c.venue for c in v}
        tag = "cross" if len(venues) > 1 else "single"
        comp[f"{tag}: " + " + ".join(sorted({c.family for c in v}))] += 1
    tilings: Counter[str] = Counter()
    for v in classes(contracts).values():
        cb, sk = build_combos(v)
        tilings["incomplete"] += sum(r["count"] for r in sk if r["reason"] == "incomplete_tiling")
        tilings["gapless"] += sum(1 for c in cb if c.kind == "tiling_yes")
    return {"contracts": len(contracts), "by_family": dict(Counter(c.family for c in contracts).most_common()),
            "tilings": dict(tilings),
            "excluded": dict(Counter(e.reason for e in excluded).most_common()),
            "classes": dict(comp.most_common()), "errors": cache.get("errors", [])}


# --------------------------------------------------------------------------------------------
# CLI


def _when(s: str) -> int:
    return int(s) if s.isdigit() else _iso(s if ("+" in s or s.endswith("Z")) else s + "Z")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-structural")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover")
    d.add_argument("--lab", type=Path, required=True)
    d.add_argument("--cache", type=Path)
    d.add_argument("--pmus-pages", type=int, default=20)
    d.add_argument("--kalshi-pages", type=int, default=5)
    d.add_argument("--series", nargs="*", default=list(KALSHI_SERIES))
    d.add_argument("--data", type=Path, help="also fetch metadata for every key on this tape in --from/--to")
    d.add_argument("--from", dest="t0")
    d.add_argument("--to", dest="t1")
    s = sub.add_parser("scan")
    s.add_argument("--data", type=Path, required=True)
    s.add_argument("--lab", type=Path, required=True)
    s.add_argument("--cache", type=Path)
    s.add_argument("--from", dest="t0", required=True)
    s.add_argument("--to", dest="t1", required=True)
    s.add_argument("--kalshi-stream", default="kalshi_books", choices=sorted(KALSHI_STREAMS))
    s.add_argument("--max-age", type=float, default=1.0)
    s.add_argument("--skew", type=float, default=0.5)
    s.add_argument("--persist", type=int, default=2)
    s.add_argument("--pmus-taker-coef", type=float, default=Fees.pmus_taker)
    s.add_argument("--kalshi-taker-coef", type=float, default=Fees.kalshi_taker)
    m = sub.add_parser("summary")
    m.add_argument("--lab", type=Path, required=True)
    m.add_argument("--run")
    a = ap.parse_args(argv)
    base = a.lab / "structural"
    cache_path = getattr(a, "cache", None) or base / "contracts.json"
    ledger = base / "ledger.jsonl"
    if a.cmd == "discover":
        pm, ka = (set(), set())
        if a.data is not None and a.t0 and a.t1:
            pm, ka = tape_keys(a.data, _when(a.t0), _when(a.t1))
        cache = discover(cache_path, kalshi_series=a.series, pmus_pages=a.pmus_pages,
                         kalshi_pages=a.kalshi_pages, pmus_slugs=pm, kalshi_tickers=ka)
        print(orjson.dumps(census(cache), option=orjson.OPT_INDENT_2).decode())
        print("->", cache_path)
        return 0
    if a.cmd == "scan":
        if not cache_path.exists():
            print(f"no contract cache at {cache_path}; run discover first", file=sys.stderr)
            return 2
        cache = orjson.loads(cache_path.read_bytes())
        rows = scan(cache, a.data, _when(a.t0), _when(a.t1), Params(a.max_age, a.skew, a.persist),
                    Fees(a.pmus_taker_coef, a.kalshi_taker_coef), a.kalshi_stream)
        append_ledger(ledger, rows)
        print(orjson.dumps(summarize(rows), option=orjson.OPT_INDENT_2).decode())
        print("->", ledger)
        return 0
    if not ledger.exists():
        print(f"no ledger at {ledger}", file=sys.stderr)
        return 2
    rows = [orjson.loads(x) for x in ledger.read_bytes().splitlines() if x.strip()]
    run = a.run or next((r["run"] for r in reversed(rows) if r["type"] == "run"), None)
    print(orjson.dumps(summarize([r for r in rows if r.get("run") == run]), option=orjson.OPT_INDENT_2).decode())
    return 0


if __name__ == "__main__":
    sys.exit(main())
