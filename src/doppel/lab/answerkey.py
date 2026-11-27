"""Answer-key lab: scripted wallets with KNOWN rules trade on real recorded intl books; DOPPEL's
reconstruction tools see only the evidence a public wallet would leave; their answers are scored
against the rules. This is the accuracy instrument for reconstruction (docs/ANSWERKEY.md).

    doppel-answerkey --data TAPE --tokens tokens.jsonl --stream clob_btc_5m \
        --from 2026-11-26T12:00 --to 2026-11-26T13:00 --out DIR [--strategies a,b,c,d,e,f]

THE SIX SUBJECTS (``POLICIES``; each is a few explicit lines with its parameters in ``params``)
  a midpoint_maker   BUY Up at floor(mid - h), BUY Down at floor(100 - mid - h) (= ask Up at
                     ceil(mid + h)); size drawn from a fixed set; re-posts every T s or on a >= 1 c
                     mid move (cancel + replace, so cancels are on the timer).
  b fair_maker       fair-v1 (``rulefit.Fair``: BRTI received by the decision) -/+ h - k * inv, BTC
                     only, one fixed size; re-posts on a >= 1 c target move or every T s.
  c complete_set     rests BUY Up at the best Up bid and BUY Down at the best Down bid when the two
                     sum to <= 100 - m c, never buys more of one side than one size beyond the
                     other, holds to settlement. (Read as a MAKER: the venue mirrors every book, so
                     ask Up + ask Down = 100 + spread >= 100 c always and the taker reading of the
                     brief never trades.)
  d touch_ladder     BUY at the best bid and 1 and 2 c behind on both tokens, sizes 97..100.
  e random_taker     DECOY: FAK buys at the best ask of a random token at Poisson times, at the rate
                     of (a)'s filled orders when (a) ran, same size set. No rule to find.
  f hidden_signal    touch-joining bid on ONE token per window, chosen by a private signal = the
                     window's resolved outcome, flipped with probability 1 - accuracy. The signal
                     is in no input DOPPEL records. It looks ahead ON PURPOSE: it is the scripted
                     subject's private information, and it lives only in ``truth/``.

THE FILL MODEL (truth). One forward pass over the stream in tape order. A policy decides at a
record's receive time ``tw`` from the book as of that record (records with tw <= now, own orders
excluded: a wallet knows its own quotes). An action lands LAT ms later (seeded, 80..400 ms) in an
injected record. Each resting order is one ``orderreplay.QuoteSim`` (the twin engine's maker fill
code, unchanged) with the PESSIMISTIC queue: everything at the level when it landed is ahead. The
adapter is orderreplay's: books of the order's own token, prints of the token and its mate
(mirrored, mirror duplicates dropped). Two own orders never share a level; prints are offered to
own orders best price first and what one fills is taken from the print before the next. A cancel is
effective at its landing (no dying-quote grace: the book showed it gone). A post-only order that
would cross on landing is rejected (never visible); "cross" includes the wallet's own bid on the
mate token, which is an ask on this one. Takers are FAK at the best ask seen at the
decision, filled at landing up to that level's displayed size. Fees are 0.

THE PUBLIC EVIDENCE (``public/``), in the formats the tools read, and nothing more:
  tape/      the stream rewritten: every original frame that touches a level where the subject
             rests carries the subject's remaining size added (price_change sizes and book
             snapshot levels are absolute totals, so this is an overlay, not an append), on the
             token and its mirror. Placement = one injected price_change (both views) at landing;
             fill = the real print it came from shrunk by the filled shares (and its mirror
             copy), plus an injected last_trade_price with the subject's tx hash and a level
             decrease; cancel = one injected decrease of the remainder; taker = injected print +
             decrease of the level it hit (dropped at that level's next real update). Other clob
             streams and ref_* streams are linked unchanged; rtds_* and goldsky_activity are
             omitted (the subject's RTDS rows are NOT injected).
  orders.jsonl  ordertape rows (``ordertape.SCHEMA`` keys) for orders that filled at least once:
             one row per fill tx, ``verified`` true for the subject, signed ``created_ms`` (the
             decision time on the venue clock estimate), integer ``block_ts`` 1-2 s after the fill.
             Every tx is a complete call: maker fills carry the anonymous taker row, taker fills the
             anonymous maker row (``verified`` false, as a cohort tape has them).
  fills/     the fill store (``fillstore.compact`` of goldsky_activity frames of the subject's fills).
  lab/tokens.jsonl  copy of the public token map; coverage_receipt.json for fidelity's holdout
             (true by construction: the lab wrote every fill).
  NEVER written there: cancels as events, unfilled or rejected orders, parameters, the signal.

KNOWN LIMITS: injected quotes and trades do not change any other participant's behaviour; a
book-cross fill (the opposite side reached a resting quote with no print) is published as the
crossed book itself plus a ZERO-SIZE print that carries its tx and time (so the fill can be
located, and is not counted a second time); frames' own best_bid/best_ask and hash fields are not
recomputed; while an asset's state is unknown nothing is injected for it (the next snapshot
carries the overlay); RTDS activity is absent.

SCORING (``EXPECT``, ``score``): every strategy x obligation cell is fixed in code before a run:
the true value (from the parameters, or measured from the full truth by a stated rule), the tool and
field that answers it, and a tolerance. Categories: correct | wrong (hedged: ESTIMATED, or a CI that
contains the truth) | wrongly_confident (VERIFIED, or a CI that excludes the truth) |
correctly_unknown (no true rule and the tool gave none) | missed (a true rule and the tool said
UNKNOWN) | not_scored. recovery_accuracy = correct / cells with a true value;
false_confidence_rate = wrongly_confident / cells where the tool made a confident claim.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import resource
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import orjson

from ..tape.segment import SegmentWriter, read_segment, segment_path
from .answerkey_inputs import preflight_inputs
from .census import LEN
from .fillstore import compact
from .flow import _ts
from .intlbook import NS, IntlBook, LevelEvent, Print, Reset, px
from .intltwin import IntlWindow, windows_from_tokens
from .ledger import TokenMap
from .orderreplay import AGGR, FLIP, HIT_BY, MS_NS, SIDE, IntlTrade, QuoteSim, to_book
from .profile import COPY

LAT_MS = (80, 400)       # sign-to-book latency of an action, uniform, seeded
DECIDE_MS = 200          # a policy is asked at most this often
STOP_S = 15              # no new orders and everything cancelled this long before a window's close / --to
MIN_QTY = 0.01
TOL = 0.02
JOB = "cohort-answerkey"
MIN_FIDELITY_STARTS = 30   # fidelity's comparable_starts gate (fidelity.GATES)
ZERO32 = "0" * 64


# ---------------------------------------------------------------- truth records
@dataclass
class Order:
    oid: str
    kind: str                 # maker | taker
    token: str
    up: bool                  # token is the window's Up token
    slug: str
    price: float              # dollars, the token's own view
    size: float
    tag: str
    decided_tw: int
    created_ms: int
    land_tw: int
    ref: dict                 # the policy's references at the decision (Up cents)
    inv: float                # net Up shares in the window at the decision
    land_ms: int | None = None
    queue0: float | None = None
    status: str = "pending"   # pending | live | filled | cancelled | rejected:<why> | unfilled
    fills: list = field(default_factory=list)   # [venue ms, tw, shares, tx, via]
    end_ms: int | None = None
    sim: QuoteSim | None = None
    cancel_sent: bool = False

    @property
    def filled(self) -> float:
        return sum(f[2] for f in self.fills)

    @property
    def remaining(self) -> float:
        return self.size - self.filled


def _h(*parts) -> str:
    return hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()


def addr(*parts) -> str:
    return "0x" + _h("addr", *parts)[:40]


def fmt(x: float) -> str:
    s = f"{x:.6f}".rstrip("0").rstrip(".")
    return s or "0"


def fpx(p: float) -> str:
    return f"{p:.2f}" if abs(p * 100 - round(p * 100)) < 1e-9 else f"{p:.3f}"


# ---------------------------------------------------------------- policies
@dataclass
class Ctx:
    """What a policy may read at decision time ``tw``: the real book (own orders excluded) and its
    own state. Prices in Up cents."""
    tw: int
    ms: int
    win: IntlWindow
    top: dict          # token -> (bb, ba) in dollars of that token, None when unknown
    live: list         # own live or pending orders in this window
    inv: float
    fair_c: float | None
    rng: np.random.Generator

    def mid_c(self) -> float | None:
        bb, ba = self.top[self.win.up]
        return None if bb is None or ba is None else 50 * (bb + ba)


def place(token: str, price_c: float, size: float, tag: str = "") -> tuple:
    return ("place", token, round(price_c) / 100, float(size), tag)


class Policy:
    name = ""
    params: dict = {}
    taker = False

    def __init__(self, seed: int):
        self.rng = np.random.default_rng(seed)
        self.state: dict = {}

    def refs(self, c: Ctx) -> dict:
        bb, ba = c.top[c.win.up]
        return {"mid_c": c.mid_c(), "bb_c": None if bb is None else 100 * bb,
                "ba_c": None if ba is None else 100 * ba, "fair_c": c.fair_c}

    def decide(self, c: Ctx) -> list[tuple]:
        raise NotImplementedError


def _cancel_all(c: Ctx) -> list[tuple]:
    return [("cancel", o.oid) for o in c.live if o.kind == "maker"]


class MidMaker(Policy):
    name, params = "midpoint_maker", {"h_c": 2.0, "sizes": [10.0, 25.0, 50.0], "timer_s": 5.0, "move_c": 1.0}

    def decide(self, c):
        p, mid = self.params, c.mid_c()
        if mid is None:
            return []
        s = self.state.setdefault(c.win.up, {"t": -1e18, "mid": None})
        due = not c.live or c.ms - s["t"] >= p["timer_s"] * 1000 or abs(mid - s["mid"]) >= p["move_c"] - 1e-9
        if not due:
            return []
        s.update(t=c.ms, mid=mid)
        bid, ask = math.floor(mid - p["h_c"] + 1e-9), math.ceil(mid + p["h_c"] - 1e-9)
        out = _cancel_all(c)
        if 1 <= bid <= 99:
            out.append(place(c.win.up, bid, self.rng.choice(p["sizes"])))
        if 1 <= 100 - ask <= 99:
            out.append(place(c.win.down, 100 - ask, self.rng.choice(p["sizes"])))
        return out


class FairMaker(Policy):
    name, params = "fair_maker", {"h_c": 3.0, "k_c_per_contract": 0.05, "size": 20.0, "timer_s": 10.0,
                                  "move_c": 1.0}

    def decide(self, c):
        p = self.params
        if c.fair_c is None:
            return []
        bid = round(c.fair_c - p["h_c"] - p["k_c_per_contract"] * c.inv)
        ask = round(c.fair_c + p["h_c"] - p["k_c_per_contract"] * c.inv)
        s = self.state.setdefault(c.win.up, {"t": -1e18, "q": None})
        if c.live and s["q"] == (bid, ask) and c.ms - s["t"] < p["timer_s"] * 1000:
            return []
        s.update(t=c.ms, q=(bid, ask))
        out = _cancel_all(c)
        if 1 <= bid <= 99:
            out.append(place(c.win.up, bid, p["size"]))
        if 1 <= 100 - ask <= 99:
            out.append(place(c.win.down, 100 - ask, p["size"]))
        return out


class _Pegger(Policy):
    """Keep exactly the target (token, price) set live: cancel the rest, place what is missing."""

    def targets(self, c: Ctx) -> dict[tuple[str, float], tuple[float, str]]:
        raise NotImplementedError

    def decide(self, c):
        want = self.targets(c)
        out, have = [], set()
        for o in c.live:
            k = (o.token, round(o.price * 100))
            if k in want and k not in have:
                have.add(k)
            else:
                out.append(("cancel", o.oid))
        for (tok, pc), (size, tag) in want.items():
            if (tok, pc) not in have:
                out.append(place(tok, pc, size, tag))
        return out


class CompleteSet(_Pegger):
    name, params = "complete_set", {"m_c": 2.0, "size": 25.0}

    def targets(self, c):
        p = self.params
        bu, bd = c.top[c.win.up][0], c.top[c.win.down][0]
        if bu is None or bd is None or round(100 * (bu + bd)) > 100 - p["m_c"]:
            return {}
        out = {}
        if c.inv < p["size"]:
            out[(c.win.up, round(100 * bu))] = (p["size"], "up")
        if c.inv > -p["size"]:
            out[(c.win.down, round(100 * bd))] = (p["size"], "down")
        return out


class Ladder(_Pegger):
    name, params = "touch_ladder", {"levels_c": [0, 1, 2], "sizes": [97.0, 98.0, 99.0, 100.0]}

    def targets(self, c):
        out = {}
        for tok in (c.win.up, c.win.down):
            bb = c.top[tok][0]
            if bb is None:
                continue
            for lv in self.params["levels_c"]:
                pc = round(100 * bb) - lv
                if 1 <= pc <= 99:
                    out[(tok, pc)] = (self.rng.choice(self.params["sizes"]), f"L{lv}")
        return out


class RandomTaker(Policy):
    name, taker = "random_taker", True
    params = {"rate_per_s": 0.02, "sizes": [10.0, 25.0, 50.0]}

    def decide(self, c):
        s = self.state
        if "next" not in s:
            s["next"] = c.ms + self.rng.exponential(1 / self.params["rate_per_s"]) * 1000
        if c.ms < s["next"]:
            return []
        s["next"] = c.ms + self.rng.exponential(1 / self.params["rate_per_s"]) * 1000
        tok = c.win.up if self.rng.random() < 0.5 else c.win.down
        ba = c.top[tok][1]
        if ba is None:
            return []
        return [("take", tok, px(ba), float(self.rng.choice(self.params["sizes"])), "")]


class HiddenSignal(_Pegger):
    name, params = "hidden_signal", {"accuracy": 0.75, "size": 25.0}

    def signal(self, win: IntlWindow) -> bool | None:
        """Private: True = Up. Drawn once per window from the RESOLVED outcome (look-ahead on purpose)."""
        sig = self.state.setdefault("signal", {})
        if win.up not in sig:
            if win.payout_up is None:
                sig[win.up] = None
            else:
                right = self.rng.random() < self.params["accuracy"]
                sig[win.up] = (win.payout_up > 0.5) == right
        return sig[win.up]

    def targets(self, c):
        up = self.signal(c.win)
        if up is None:
            return {}
        tok = c.win.up if up else c.win.down
        bb = c.top[tok][0]
        return {} if bb is None else {(tok, round(100 * bb)): (self.params["size"], "up" if up else "down")}


POLICIES = {"a": MidMaker, "b": FairMaker, "c": CompleteSet, "d": Ladder, "e": RandomTaker, "f": HiddenSignal}


# ---------------------------------------------------------------- simulation + injection
def _tops(levels: dict | None) -> tuple[float | None, float | None]:
    if not levels:
        return None, None
    bids = [p for (p, s), z in levels.items() if s == "BUY" and z > 0]
    asks = [p for (p, s), z in levels.items() if s == "SELL" and z > 0]
    return (max(bids) if bids else None), (min(asks) if asks else None)


class Sim:
    """One subject on one stream: truth bookkeeping, fills and the rewritten tape."""

    def __init__(self, policy: Policy, wallet: str, wins: list[IntlWindow], fair, seed: int):
        self.pol, self.wallet, self.fair = policy, wallet, fair
        self.wins = sorted(wins, key=lambda w: w.start)
        self.mate = {w.up: w.down for w in wins} | {w.down: w.up for w in wins}
        self.slug_of = {t: f"{w.asset}-updown-{w.horizon}-{w.start}" for w in wins for t in (w.up, w.down)}
        self.win_of = {t: w for w in wins for t in (w.up, w.down)}
        self.book = IntlBook()
        self.rng = np.random.default_rng(seed + 1)
        self.orders: list[Order] = []
        self.live: dict[str, list[Order]] = defaultdict(list)    # token -> live maker orders
        self.pending: list[tuple[int, int, tuple, Order | None]] = []  # (land_tw, n, action, order)
        self.rest: dict[tuple, float] = defaultdict(float)      # (asset, price, side) -> own resting size
        self.consumed: dict[tuple, float] = {}                  # taker depth taken, until the level's next update
        self.inv: dict[str, float] = defaultdict(float)         # window slug -> net Up shares
        self.clock_off: float | None = None                     # EMA of venue ms - receive ms
        self.open: dict[str, Order] = {}                        # oid -> order still pending or live
        self.dirty = False                                      # the current record was rewritten
        self.t_stop = 0
        self.seen_prints: dict[tuple, float] = {}               # print view key -> shares taken from it
        self.markers: set[str] = set()                          # txs of book-cross fills (zero-size prints)
        self.next_decide = 0
        self.n = 0
        self.stats: Counter = Counter()
        self.pre_tw = 0

    # --- helpers
    def vms(self, tw: int) -> int:
        return int(tw // MS_NS + (self.clock_off or 0.0))

    def keys(self, token: str, price: float) -> list[tuple]:
        """The two book entries of a BUY order on ``token`` at ``price``: its bid and the mirror ask."""
        return [(token, px(price), "BUY"), (self.mate[token], px(1 - price), "SELL")]

    def level_msg(self, keys: list[tuple], ms: int, deltas: dict[tuple, float] | None = None) -> dict | None:
        """price_change with the overlaid totals of ``keys``; None when the real size is unknown."""
        chs = []
        for k in keys:
            real = self.book.size(k[0], k[1], k[2])
            if real is None:
                continue
            new = max(0.0, real + self.rest.get(k, 0.0) + self.consumed.get(k, 0.0) + (deltas or {}).get(k, 0.0))
            chs.append({"asset_id": k[0], "price": fpx(k[1]), "size": fmt(new), "side": k[2],
                        "hash": _h("lvl", self.n, k)[:40]})
        self.n += 1
        if not chs:
            return None
        return {"market": self.book.market_of.get(keys[0][0], ""), "price_changes": chs,
                "timestamp": str(ms), "event_type": "price_change"}

    def print_msg(self, token: str, price: float, size: float, side: str, ms: int, tx: str) -> dict:
        return {"market": self.book.market_of.get(token, ""), "asset_id": token, "price": fpx(price),
                "size": fmt(size), "fee_rate_bps": "0", "side": side, "timestamp": str(ms),
                "event_type": "last_trade_price", "transaction_hash": tx}

    def fill(self, o: Order, ms: int, tw: int, q: float, via: str) -> list[dict]:
        """Book one fill of a maker order: truth, inventory, overlay; returns the frames to inject."""
        tx = "0x" + _h("tx", self.wallet, o.oid, len(o.fills))
        self.dirty = True
        o.fills.append([ms, tw, round(q, 6), tx, via])
        self.inv[o.slug] += q if o.up else -q
        for k in self.keys(o.token, o.price):
            self.rest[k] -= q
        # A book-cross fill is already in the public book (the opposite side is shown at the quote's
        # price), so its print carries the tx and the time but NO size: the execution is published once.
        if via == "book_cross":
            self.markers.add(tx)
        out = [self.print_msg(o.token, o.price, 0.0 if via == "book_cross" else q, "SELL", ms, tx)]
        lv = self.level_msg(self.keys(o.token, o.price), ms)
        if lv:
            out.append(lv)
        if o.remaining <= TOL:
            o.status, o.end_ms = "filled", ms
            self._drop(o)
        return out

    def _drop(self, o: Order) -> None:
        for k in self.keys(o.token, o.price):
            self.rest[k] -= max(0.0, o.remaining)
            if abs(self.rest[k]) < 1e-6:
                del self.rest[k]
        if o in self.live[o.token]:
            self.live[o.token].remove(o)
        self.open.pop(o.oid, None)
        o.sim = None

    # --- landing of actions
    def land(self, tw: int) -> list[dict]:
        """Apply every action due by ``tw`` (cancels first); the injected frames, in order."""
        due = sorted((p for p in self.pending if p[0] <= tw), key=lambda p: (p[0], p[2][0] != "cancel", p[1]))
        self.pending = [p for p in self.pending if p[0] > tw]
        out: list[dict] = []
        for t, _, act, o in due:
            ms = self.vms(t)
            if act[0] == "cancel":
                o = self.open.get(act[1])
                if o is None:
                    continue
                if o.status == "pending":   # cancelled before it landed: never on the book
                    o.status = "rejected:cancelled_in_flight"
                    self.open.pop(o.oid, None)
                    continue
                o.status, o.end_ms = "cancelled", ms
                lv_keys = self.keys(o.token, o.price)
                self._drop(o)
                lv = self.level_msg(lv_keys, ms)
                if lv:
                    out.append(lv)
                continue
            assert o is not None
            if o.status != "pending":   # its cancel landed first (cancelled in flight)
                continue
            o.land_ms = ms
            bb, ba = _tops(self.book.levels.get(o.token))
            if not self.book.known(o.token) or not self.book.known(self.mate[o.token]):
                o.status = "rejected:book_unknown"
            if o.status.startswith("rejected"):
                self.open.pop(o.oid, None)
            elif o.kind == "maker":
                # post-only against the book the venue would show: the real asks AND the wallet's own bids on
                # the mate token, which are asks here (BUY mate @ p == SELL this @ 1 - p). Without the second
                # part two of its own quotes can cross each other in the public book.
                own_ask = min((1 - x.price for x in self.live[self.mate[o.token]]), default=None)
                if (ba is not None and o.price >= ba - 1e-9) or (own_ask is not None and o.price >= own_ask - 1e-9):
                    o.status = "rejected:post_only"
                    self.open.pop(o.oid, None)
                elif any(abs(x.price - o.price) < 1e-9 for x in self.live[o.token]):
                    o.status = "rejected:own_level_taken"
                    self.open.pop(o.oid, None)
                else:
                    o.queue0 = self.book.size(o.token, o.price, "BUY") or 0.0
                    o.status = "live"
                    o.sim = QuoteSim("bid", o.price, o.size, ms * MS_NS, o.queue0)
                    self.live[o.token].append(o)
                    for k in self.keys(o.token, o.price):
                        self.rest[k] += o.size
                    lv = self.level_msg(self.keys(o.token, o.price), ms)
                    if lv:
                        out.append(lv)
            else:  # FAK at the decision's best ask, up to the displayed size at that level
                self.open.pop(o.oid, None)
                key = (o.token, px(o.price), "SELL")
                avail = (self.book.size(*key) or 0.0) + self.consumed.get(key, 0.0)
                q = math.floor(min(o.size, avail) * 100) / 100 if ba is not None and ba <= o.price + 1e-9 else 0.0
                if q < MIN_QTY:
                    o.status = "unfilled"
                    continue
                tx = "0x" + _h("tx", self.wallet, o.oid, 0)
                o.fills.append([ms, t, q, tx, "take"])
                o.status, o.end_ms = "filled", ms
                self.inv[o.slug] += q if o.up else -q
                out.append(self.print_msg(o.token, o.price, q, "BUY", ms, tx))
                ks = [key, (self.mate[o.token], px(1 - o.price), "BUY")]
                for k in ks:
                    self.consumed[k] = self.consumed.get(k, 0.0) - q
                lv = self.level_msg(ks, ms)
                if lv:
                    out.append(lv)
        return out

    # --- one real message
    def overlay(self, m: dict) -> dict:
        et = m.get("event_type")
        if et == "price_change":
            chs = []
            for ch in m.get("price_changes") or []:
                k = (ch.get("asset_id"), px(ch["price"]), str(ch["side"]))
                off = self.rest.get(k, 0.0)
                if off:
                    self.dirty = True
                chs.append(ch | {"size": fmt(float(ch["size"]) + off)} if off else ch)
            return m | {"price_changes": chs}
        if et == "book":
            a = m["asset_id"]
            mine = {(k[1], k[2]): v for k, v in self.rest.items() if k[0] == a and v > TOL}
            if not mine:
                return m
            self.dirty = True
            out = dict(m)
            for side, key, rev in (("BUY", "bids", False), ("SELL", "asks", True)):
                lv = {px(x["price"]): x for x in m.get(key) or []}
                for (p, s), v in mine.items():
                    if s == side:
                        old = float(lv[p]["size"]) if p in lv else 0.0
                        lv[p] = {"price": lv[p]["price"] if p in lv else fpx(p), "size": fmt(old + v)}
                out[key] = [lv[p] for p in sorted(lv, reverse=rev)]
            return out
        return m

    def message(self, m: dict, tw: int) -> list[dict]:
        """Apply one real message; returns it rewritten plus the frames its fills inject."""
        ts = m.get("timestamp")
        if ts not in (None, ""):
            d = int(ts) - tw / MS_NS
            self.clock_off = d if self.clock_off is None else self.clock_off + 0.05 * (d - self.clock_off)
        items = self.book.apply(m, tw)
        for it in items:
            if isinstance(it, LevelEvent):
                self.consumed.pop((it.asset, it.price, it.side), None)
        out = [self.overlay(m)]
        changed = {it.asset for it in items if isinstance(it, LevelEvent)}
        for a in changed:
            orders = [o for o in self.live.get(a, ()) if o.sim is not None]
            if not orders:
                continue
            ms = max(it.venue_ms for it in items if isinstance(it, LevelEvent) and it.asset == a)
            b = to_book(self.book.levels.get(a, {}), ms * MS_NS + 1)
            for o in orders:
                sim = o.sim
                assert sim is not None
                k = len(sim.got)
                sim._on_book(b)
                for _, q, via in sim.got[k:]:
                    out += self.fill(o, ms, tw, q, via)
                    self.stats["book_cross_fills"] += 1
        for it in items:
            if isinstance(it, Print):
                out += self.trade(it, out[0])
        return out

    def trade(self, e: Print, msg: dict) -> list[dict]:
        aggr = AGGR.get(e.side)
        if aggr is None:
            return []
        m = self.mate.get(e.asset)
        own = (e.tx, e.size, e.asset, e.price, e.side)
        if m is not None:
            mirror = (e.tx, e.size, m, px(1 - e.price), "SELL" if e.side == "BUY" else "BUY")
            if e.tx and mirror in self.seen_prints:   # the same execution printed on the mate: dropped
                taken = self.seen_prints.pop(mirror)
                if taken > 0:
                    msg["size"] = fmt(max(0.0, e.size - taken))
                    self.dirty = True
                return []
        views = [(e.asset, e.price, aggr)] + ([(m, px(1 - e.price), FLIP[aggr])] if m else [])
        avail = e.size
        out: list[dict] = []
        for asset, price, ag in views:
            orders = sorted((o for o in self.live.get(asset, ()) if o.sim is not None
                             and ag == HIT_BY[SIDE["BUY"]]), key=lambda o: -o.price)
            for o in orders:
                if avail <= MIN_QTY:
                    break
                sim = o.sim
                assert sim is not None
                k = len(sim.got)
                sim._on_trade(e.tw_ns, IntlTrade(e.tw_ns, e.venue_ms * MS_NS + 1, price, avail, ag, e.tx or None))
                for _, q, via in sim.got[k:]:
                    avail -= q
                    out += self.fill(o, e.venue_ms, e.tw_ns, q, via)
        taken = e.size - avail
        if taken > 0:
            msg["size"] = fmt(max(0.0, avail))
            self.dirty = True
        if e.tx:
            self.seen_prints[own] = taken
        return out

    # --- decisions
    def decide(self, tw: int) -> None:
        if tw < self.next_decide:
            return
        self.next_decide = tw + DECIDE_MS * MS_NS
        d = tw // NS
        for w in self.wins:
            if not (w.start <= d < w.end) or d >= self.t_stop:
                continue
            slug = self.slug_of[w.up]
            mine = [o for o in self.open.values() if o.slug == slug and not o.cancel_sent]
            refs: dict = {}
            if w.end - d <= STOP_S or self.t_stop - d <= STOP_S:
                acts: list[tuple] = [("cancel", o.oid) for o in mine if o.kind == "maker"]
            elif not (self.book.known(w.up) and self.book.known(w.down)):
                continue
            else:
                top = {t: _tops(self.book.levels.get(t)) for t in (w.up, w.down)}
                fair = self.fair.at({"asset": w.asset, "start": w.start, "end": w.end}, tw) if self.fair else None
                c = Ctx(tw, self.vms(tw), w, top, mine, self.inv[slug], fair, self.pol.rng)
                acts = self.pol.decide(c)
                refs = self.pol.refs(c)
            for a in acts:
                lat = int(self.rng.integers(LAT_MS[0], LAT_MS[1] + 1)) * MS_NS
                if a[0] == "cancel":
                    self.open[a[1]].cancel_sent = True
                    self.pending.append((tw + lat, len(self.pending), a, None))
                    continue
                tok, price, size, tag = a[1], a[2], a[3], a[4]
                o = Order(_h("order", self.wallet, len(self.orders)), "taker" if a[0] == "take" else "maker",
                          tok, tok == w.up, slug, price, size, tag, tw, self.vms(tw), tw + lat, refs,
                          self.inv[slug])
                self.orders.append(o)
                self.open[o.oid] = o
                self.pending.append((tw + lat, len(self.pending), a, o))

    # --- the pass
    def run(self, root: Path, stream: str, out_root: Path, t_from: int, t_to: int) -> None:
        self.t_stop = t_to
        w = SegmentWriter(out_root, stream)
        try:
            for rec in _stream_records(root, stream, t_from, t_to):
                tw = int(rec["tw"])
                while self.pending and tw > t_from * NS and min(p[0] for p in self.pending) < tw:
                    t = min(p[0] for p in self.pending)
                    inj = self.land(t)
                    if inj:
                        self.pre_tw = max(self.pre_tw + 1, t)
                        w.append(orjson.dumps(inj).decode(), tw=self.pre_tw)
                        self.stats["injected_records"] += 1
                if "seq" not in rec:            # a missing hour inside the range: not written
                    self.book.reset()
                    self.stats["missing_segment"] += 1
                    continue
                if not t_from * NS <= tw < t_to * NS:
                    w.append(rec["raw"], tw=tw, tm=rec.get("tm"), meta=rec.get("meta"))
                    self.pre_tw = max(self.pre_tw, tw)
                    continue
                raw = rec.get("raw") or ""
                obj = None
                if (rec.get("meta") or {}).get("ctl") is None and raw and raw[0] in "[{":
                    try:
                        obj = orjson.loads(raw)
                    except orjson.JSONDecodeError:
                        obj = None
                if obj is None:
                    for _, items in self.book.feed_messages(rec):
                        self.stats.update(f"reset:{it.why}" for it in items if isinstance(it, Reset))
                    w.append(raw, tw=tw, tm=rec.get("tm"), meta=rec.get("meta"))
                else:
                    self.dirty = False
                    new: list = []
                    for m in obj if isinstance(obj, list) else [obj]:
                        new += self.message(m, tw) if isinstance(m, dict) else [m]
                    if self.dirty:
                        new = [x for x in new if not (isinstance(x, dict) and x.get("event_type") == "last_trade_price"
                                                     and float(x.get("size") or 0) <= 1e-9
                                                     and x.get("transaction_hash") not in self.markers)]
                        raw = orjson.dumps(new if isinstance(obj, list) or len(new) != 1 else new[0]).decode()
                    w.append(raw, tw=tw, tm=rec.get("tm"), meta=rec.get("meta"))
                self.pre_tw = max(self.pre_tw, tw)
                self.decide(tw)
        finally:
            w.close()
        self.stats["resting_size_left_at_end"] = round(sum(v for v in self.rest.values() if v > 0))
        for o in self.orders:
            if o.status == "live":   # still resting at the end of the pass (STOP_S should prevent it)
                o.status = "censored"
            elif o.status == "pending":
                o.status = "rejected:not_landed"


def _stream_records(root: Path, stream: str, t_from: int, t_to: int) -> Iterator[dict]:
    """Every record of the hour segments touching [t_from, t_to), in tape order; a missing hour
    is one record without ``seq`` (a marker, never written)."""
    for hour in range(t_from - t_from % 3600, t_to, 3600):
        p = segment_path(root, stream, hour * NS)
        if not p.exists():
            yield {"tw": max(hour, t_from) * NS}
            continue
        yield from read_segment(p, verify=True)


# ---------------------------------------------------------------- evidence
def ordertape_rows(sim: Sim, cond: dict[str, str]) -> list[dict]:
    """Order-tape rows of every filled order, as complete calls (see module docstring)."""
    rows: list[dict] = []
    for i, o in enumerate(sim.orders):
        if not o.fills:
            continue
        ma, ta = round(o.price * o.size * 1e6), round(o.size * 1e6)  # BUY: USDC for shares
        salt = str(int(np.random.default_rng(i).random() * o.created_ms))
        for ms, _, q, tx, _ in o.fills:
            bts = math.ceil(ms / 1000) + 1
            base = {"tx": tx, "block": 60_000_000 + bts // 2, "block_ts": bts, "token_id": o.token,
                    "builder": ZERO32, "condition_id": cond.get(o.token, ""), "n_makers": 1,
                    "sig_type": 0, "fee_amount": 0}
            me = base | {"order_id": o.oid, "maker": sim.wallet, "signer": sim.wallet, "side": "BUY",
                         "size": o.size, "limit": o.price, "maker_amount": ma, "taker_amount": ta,
                         "fill_amount": round(q * o.price * 1e6), "created_ms": o.created_ms,
                         "age_s": round(bts - o.created_ms / 1000, 3), "ts_flag": None, "verified": True,
                         "salt": salt, "role": o.kind}
            cp = addr("counterparty", tx)
            cid = _h("cporder", tx)
            if o.kind == "maker":   # the anonymous taker who sold into the bid
                other = base | {"order_id": cid, "maker": cp, "signer": cp, "side": "SELL", "size": q,
                                "limit": o.price, "maker_amount": round(q * 1e6), "taker_amount": round(q * o.price * 1e6),
                                "fill_amount": round(q * 1e6), "role": "taker"}
            else:                   # the anonymous maker whose ask was lifted
                other = base | {"order_id": cid, "maker": cp, "signer": cp, "side": "SELL", "size": q,
                                "limit": o.price, "maker_amount": round(q * 1e6), "taker_amount": round(q * o.price * 1e6),
                                "fill_amount": round(q * 1e6), "role": "maker"}
            other |= {"created_ms": o.created_ms - 1000, "age_s": round(bts - o.created_ms / 1000 + 1, 3),
                      "ts_flag": None, "verified": False, "salt": str(int(_h("salt", tx)[:12], 16))}
            rows += [other, me] if o.kind == "maker" else [me, other]
    return rows


def goldsky_rows(sim: Sim) -> list[dict]:
    out = []
    for o in sim.orders:
        for j, (ms, _, q, tx, _) in enumerate(o.fills):
            bts = math.ceil(ms / 1000) + 1
            out.append({"block_number": 60_000_000 + bts // 2, "id": f"log_{tx}_{j}", "transaction_hash": tx,
                        "address": sim.wallet, "token_id": o.token, "amount_usdc": round(q * o.price, 6),
                        "amount_shares": q, "price": o.price, "event_type": "TRADE", "side": "BUY",
                        "order_id": o.oid, "counterparty_address": None, "order_type": o.kind, "fee": 0.0,
                        "builder": ZERO32, "block_timestamp": bts})
    return out


def write_public(sim: Sim, root: Path, stream: str, tokens: Path, pub: Path, hold: tuple[int, int]) -> None:
    """Everything but the rewritten stream (already under ``pub/tape``)."""
    for d in sorted(Path(root).iterdir()):
        if d.is_dir() and d.name != stream and not d.name.startswith(("rtds_", "goldsky")) and not (pub / "tape" / d.name).exists():
            (pub / "tape" / d.name).symlink_to(d.resolve(), target_is_directory=True)
    cond = {t: m.removeprefix("0x") for t, m in sim.book.market_of.items()}
    rows = ordertape_rows(sim, cond)
    with (pub / "orders.jsonl").open("wb") as full, (pub / "train-orders.jsonl").open("wb") as train:
        for row in rows:
            line = orjson.dumps(row) + b"\n"
            full.write(line)
            if row["block_ts"] < hold[0]:
                train.write(line)
    g = SegmentWriter(pub / "goldsky", "goldsky_activity")
    rs = goldsky_rows(sim)
    for i in range(0, max(len(rs), 1), 500):
        g.append(orjson.dumps({"data": rs[i:i + 500]}).decode(), meta={"job": JOB, "status": 200, "url": "answerkey",
                                                                       "rows": len(rs[i:i + 500])})
    g.close()
    compact(pub / "goldsky", pub / "fills")
    (pub / "lab").mkdir(exist_ok=True)
    shutil.copyfile(tokens, pub / "lab" / "tokens.jsonl")
    sha = hashlib.sha256((pub / "orders.jsonl").read_bytes()).hexdigest()
    (pub / "coverage_receipt.json").write_bytes(orjson.dumps(
        {"wallet": sim.wallet, "from": hold[0], "to": hold[1], "complete": True, "order_tape_sha256": sha,
         "basis": "answer-key lab: every fill of the subject was written by the lab"}))


# ---------------------------------------------------------------- truth summary
def _med(xs) -> float | None:
    return float(np.median(xs)) if len(xs) else None


def truth_summary(sim: Sim, pol: Policy) -> dict:
    """Measured from the FULL truth (every order, filled or not), by fixed rules."""
    landed = [o for o in sim.orders if o.land_ms is not None and not o.status.startswith("rejected")
              and o.status != "unfilled"]
    mk = [o for o in landed if o.kind == "maker"]
    filled_mk = [o for o in mk if o.fills]
    by_tok: dict[str, list[int]] = defaultdict(list)
    for o in landed:
        by_tok[o.token].append(int(o.land_ms or 0))
    gaps = [b - a for v in by_tok.values() for a, b in zip(sorted(v), sorted(v)[1:])]
    canc = [o for o in mk if o.status == "cancelled" and o.end_ms is not None and o.land_ms is not None]
    life = [o.end_ms - o.land_ms for o in canc]  # type: ignore[operator]
    life_unfilled = [o.end_ms - o.land_ms for o in canc if not o.fills]  # type: ignore[operator]
    rest_filled = [o.fills[-1][0] - o.land_ms for o in filled_mk if o.status == "filled"]
    rest_canc = [o.end_ms - o.land_ms for o in canc if o.fills]  # type: ignore[operator]
    offs: dict[str, list[float]] = {"bid": [], "ask": []}
    for o in mk:
        lim_up = 100 * o.price if o.up else 100 - 100 * o.price
        side = "bid" if o.up else "ask"
        ref = REF_OF.get(pol.name)
        r = None
        if ref == "mid":
            r = o.ref.get("mid_c")
        elif ref == "fair":
            r = o.ref.get("fair_c")
        elif ref == "touch":
            r = o.ref.get("bb_c") if o.up else o.ref.get("ba_c")
        if r is not None:
            offs[side].append(lim_up - r - (0 if pol.name != "fair_maker" else
                                             -pol.params["k_c_per_contract"] * o.inv))
    sh_mk = sum(o.filled for o in mk)
    sh_tk = sum(o.filled for o in landed if o.kind == "taker")
    pnl, unsettled = 0.0, 0
    for o in sim.orders:
        w = sim.win_of.get(o.token)
        for f in o.fills:
            if w is None or w.payout_up is None:
                unsettled += 1
                continue
            pay = w.payout_up if o.up else 1 - w.payout_up
            pnl += f[2] * (pay - o.price)
    ratio = len(filled_mk) / len(mk) if mk else None
    return {"orders": len(sim.orders), "landed": len(landed), "maker_landed": len(mk),
            "skew_c_per_contract": pol.params.get("k_c_per_contract", None if pol.taker else 0.0),
            "maker_filled": len(filled_mk), "fill_ratio": ratio,
            "status": dict(Counter(o.status.split(":")[0] if not o.status.startswith("rejected") else o.status
                                   for o in sim.orders)),
            "fills": sum(len(o.fills) for o in sim.orders), "fill_via": dict(Counter(f[4] for o in sim.orders for f in o.fills)),
            "repost_gap_s_p50": None if not gaps else _med(gaps) / 1000,  # type: ignore[operator]
            "cancel_life_ms_p50": _med(life), "unfilled_cancel_life_ms_p50": _med(life_unfilled),
            "rest_ms_p50": {"filled": _med(rest_filled), "cancelled_after_fill": _med(rest_canc)},
            "offset_c_p50": {s: _med(v) for s, v in offs.items()},
            "taker_share_of_shares": sh_tk / (sh_mk + sh_tk) if sh_mk + sh_tk else None,
            "pnl_usd": round(pnl, 4), "fills_unsettled": unsettled,
            "inventory_end": {k: round(v, 4) for k, v in sim.inv.items()},
            "sizes": sorted({o.size for o in landed})}


REF_OF = {"midpoint_maker": "mid", "fair_maker": "fair", "complete_set": "touch", "touch_ladder": "touch",
          "hidden_signal": "touch", "random_taker": None}


def truth_copy(t: dict) -> str | None:
    """profile.copyability's rule applied to the TRUE quantities (thresholds ``profile.COPY``)."""
    if (t["taker_share_of_shares"] or 0) >= COPY["taker_share_min"]:
        return "TAKER"
    life, ratio = t["unfilled_cancel_life_ms_p50"], t["fill_ratio"]
    if (life is not None and life < COPY["flicker_life_ms"]) or (ratio is not None and ratio < COPY["flicker_fill_ratio"]):
        return "FLICKER"
    rests = [v for v in t["rest_ms_p50"].values() if v is not None]
    if rests and min(rests) >= COPY["resting_ms"] and ratio is not None and ratio >= COPY["resting_fill_ratio"]:
        return "RESTING"
    return None


# ---------------------------------------------------------------- the expected answers (fixed before any run)
NA = "n/a"   # no defined answer: not scored
# per policy: what is true by construction (the measured parts come from truth_summary)
EXPECT = {
    "midpoint_maker": {"ref": "mid", "ladder": False, "explained": True, "expressible": NA},
    "fair_maker": {"ref": "fair", "ladder": False, "explained": True, "expressible": True},
    "complete_set": {"ref": "touch", "ladder": False, "explained": True, "expressible": NA},
    "touch_ladder": {"ref": "touch", "ladder": True, "explained": True, "expressible": NA},
    "random_taker": {"ref": None, "ladder": None, "explained": False, "expressible": False},
    "hidden_signal": {"ref": "touch", "ladder": False, "explained": False, "expressible": False},
}
OBLIGATIONS = ("size_rule", "price_reference", "price_offset", "skew", "cadence", "cancel_rule", "ladder",
               "fill_footprint", "copyability", "fidelity_verdict", "explained_by_observed_inputs")
# tolerances
TOL_OFFSET_C = 1.0
TOL_SKEW = 0.02
TOL_RATIO = 2.0   # cadence, cancel life and fill ratio: within a factor of 2


def _cell(truth, rec, status: str, ok: bool | None, ci_cover: bool | None = None, **info) -> dict:
    """status: claim | hedged | unknown. ``ok`` compares rec with truth (None when not comparable)."""
    if truth == NA:
        cat = "not_scored"
    elif truth is None:
        cat = "correctly_unknown" if status == "unknown" or rec is None else (
            "wrongly_confident" if status == "claim" else "wrong")
    elif ci_cover is False and rec is not None:
        cat = "wrongly_confident"
    elif status == "unknown" or rec is None:
        cat = "missed"
    elif ok:
        cat = "correct"
    else:
        cat = "wrongly_confident" if status == "claim" or ci_cover is False else "wrong"
    return {"truth": truth, "recovered": rec, "status": status, "ci_covers_truth": ci_cover,
            "category": cat} | info


def _ratio_ok(a, b) -> bool:
    return a is not None and b is not None and a > 0 and b > 0 and max(a / b, b / a) <= TOL_RATIO


def _status(ob: dict | None) -> str:
    s = (ob or {}).get("status")
    return {"VERIFIED": "claim", "ESTIMATED": "hedged"}.get(s or "", "unknown")


def _covers(ci, x, pad=0.0) -> bool | None:
    if not ci or None in ci or x is None:
        return None
    lower, upper = ci[0] - pad, ci[1] + pad
    return ((x >= lower or math.isclose(x, lower, rel_tol=1e-12, abs_tol=1e-12)) and
            (x <= upper or math.isclose(x, upper, rel_tol=1e-12, abs_tol=1e-12)))


def selected_parameter(side: dict, name: str) -> tuple:
    selected = (side.get("selected_parameters") or {}).get(name)
    if selected is not None:
        return selected["value"], selected.get("ci95")
    # Earlier fit reports store descriptive intervals for both candidate models.
    # Only the constant offset and the fitted skew have a matching legacy interval.
    value = (side.get("rule") or {}).get(name)
    model = side.get("model")
    same_parameter = ((name == "offset_c" and model == "const") or
                      (name == "skew_c_per_contract" and model == "skew"))
    ci = (side.get(name) or {}).get("ci95") if same_parameter else None
    return value, ci


def price_parameter_cell(truth, sides, estimates: dict, comparison: list, coverage: list) -> dict:
    """A contradiction on one fitted side survives missing evidence on its mate."""
    stated = {s: value for s, value in estimates.items() if value is not None}
    checked = [v for v in coverage if v is not None]
    covered = all(checked) if checked else None
    complete = bool(sides) and len(stated) == len(sides)
    result = _cell(truth, stated or None, "claim" if stated else "unknown",
                   complete and all(comparison), covered, tool="rulefit", complete_sides=complete)
    if stated and not complete and all(comparison) and covered is not False:
        result["category"] = "missed"
    return result


def _timing_cell(truth, q: dict | None, ob: dict | None) -> dict:
    """A cadence or quote-life cell from rulefit's corrected estimate ``q`` (value, status, ci95 =
    its stated interval, upper_bound) and the profile obligation. The stated interval is scored as
    a claim about the truth; so is a stated upper bound when no interval is given."""
    q = q or {}
    st = q.get("status")
    v = q.get("value") if st in ("ESTIMATED", "UPPER_BOUND") else None
    cover = _covers(q.get("ci95"), truth) if v is not None else None
    ub = q.get("upper_bound")
    holds = None if ub is None or truth is None else truth <= ub
    if v is not None and holds is not None:
        cover = holds if cover is None else cover and holds
    return _cell(truth, v, _status(ob) if v is not None else "unknown", _ratio_ok(v, truth), cover,
                 tool="rulefit+profile", stated=st, method=q.get("method"), interval=q.get("ci95"),
                 upper_bound=ub, bound_holds=holds, bias=q.get("bias"))


def score(name: str, t: dict, tools: dict) -> dict:
    """One strategy's row of the scorecard from the truth summary and the tools' outputs."""
    e = EXPECT[name]
    led = (tools.get("profile") or {}).get("obligations") or {}
    rf = tools.get("rulefit") or {}
    if not rf:
        raise ValueError("cannot score a subject without its rule-fit report")
    for step, rc in tools.get("exit", {}).items():
        if rc and not (step == "rulefit" and rc == 1 and not tools.get("genome") and documented_no_draft(rf)):
            raise ValueError(f"cannot score a subject after {step} exited {rc}")
    fid = tools.get("fidelity")
    maker = e["ref"] is not None
    cells: dict[str, dict] = {}
    # size rule: profile
    ob = led.get("size_rule")
    rec = (ob or {}).get("value") or None
    rs = sorted(rec["sizes"]) if rec and rec.get("sizes") else None
    cells["size_rule"] = _cell(t["sizes"], rs, _status(ob) if rec else "unknown", rs == t["sizes"], tool="profile")
    # price: rulefit per Up side
    pr = rf.get("price_rule") or {}
    sides = [s for s in ("bid", "ask") if t["offset_c_p50"].get(s) is not None] if maker else []
    refs = {s: (pr.get(s) or {}).get("chosen") for s in ("bid", "ask")}
    ties = {s: (pr.get(s) or {}).get("tie_with") for s in ("bid", "ask")}
    have = maker and sides and all(refs[s] for s in sides)
    if not maker:
        cells["price_reference"] = _cell(None, refs if any(refs.values()) else None,
                                         "claim" if any(refs.values()) else "unknown", None, tool="rulefit")
    else:
        ok = bool(have) and all(e["ref"] in (refs[s], ties[s]) for s in sides)
        tie = any(ties[s] for s in sides)
        cells["price_reference"] = _cell(e["ref"], refs if have else None,
                                         "hedged" if tie else "claim", ok, tool="rulefit", tie_with=ties)
    offs, cover, okk = {}, [], []
    for s in sides:
        if refs[s] != e["ref"]:
            continue
        v, ci = selected_parameter(pr.get(s) or {}, "offset_c")
        if v is None:
            continue
        offs[s] = v
        tv = t["offset_c_p50"][s]
        okk.append(v is not None and abs(v - tv) <= TOL_OFFSET_C)
        cover.append(_covers(ci, tv))
    cells["price_offset"] = price_parameter_cell({s: t["offset_c_p50"][s] for s in sides} if maker else None,
                                                 sides, offs, okk, cover)
    sk, skc, sko = {}, [], []
    true_skew = t["skew_c_per_contract"]
    for s in sides:
        if refs[s] != e["ref"]:
            continue
        v, ci = selected_parameter(pr.get(s) or {}, "skew_c_per_contract")
        if v is None:
            continue
        sk[s] = v
        sko.append(v is not None and true_skew is not None and abs(v - true_skew) <= max(TOL_SKEW, 0.5 * abs(true_skew)))
        skc.append(_covers(ci, true_skew))
    cells["skew"] = price_parameter_cell(true_skew, sides, sk, sko, skc)
    # cadence and quote life: rulefit's corrected estimates, status from the profile ledger
    tm = rf.get("timing_rule") or {}
    cells["cadence"] = _timing_cell(t["repost_gap_s_p50"], tm.get("gap_s"), led.get("placement_cadence"))
    cells["cancel_rule"] = _timing_cell(t["cancel_life_ms_p50"] if maker else None, (tm.get("cancel") or {}).get("life_ms"),
                                        led.get("cancel_replace"))
    # ladder: rulefit states ladder (a claim) / no_ladder_seen (hedged: unfilled levels are invisible) / unknown
    lad = rf.get("ladder") or {}
    det = {"ladder": True, "no_ladder_seen": False}.get(lad.get("status") or "")
    cells["ladder"] = _cell(e["ladder"], det, "claim" if det else "hedged" if det is False else "unknown",
                            det == e["ladder"], tool="rulefit", stated=lad.get("status"),
                            ladder_share=(lad.get("ladder_share") or {}).get("value"), floor_ms=lad.get("floor_ms"))
    ob = led.get("fill_footprint")
    fv = ((ob or {}).get("value") or {}).get("fill_ratio")
    cells["fill_footprint"] = _cell(t["fill_ratio"] if maker else None, fv, _status(ob) if fv is not None else "unknown",
                                    _ratio_ok(fv, t["fill_ratio"]), tool="profile")
    ob = led.get("copyability")
    cvd = ((ob or {}).get("value") or {}).get("verdict")
    cvd = None if cvd in (None, "UNKNOWN") else cvd
    tc = truth_copy(t)
    cells["copyability"] = _cell(tc, cvd, _status(ob) if cvd else "unknown", cvd == tc, tool="profile")
    dec = ((fid or {}).get("verdict") or {}).get("decision")
    by_gate = bool(fid) and str(((fid or {}).get("verdict") or {}).get("reason", "")).startswith("data gate")
    # three states that are not a verdict must not read as one: not run (no genome), stopped by a data gate, open
    state = "not_run" if not fid else "gate_inconclusive" if by_gate else {
        "PASS": "pass", "FAIL": "fail"}.get(dec or "", "inconclusive")
    exp = e["expressible"]
    said = {"PASS": True, "FAIL": False}.get(dec or "")
    c = _cell(exp, said, "claim" if said is not None else "unknown", said == exp, tool="fidelity", decision=dec,
              by_gate=by_gate, state=state)
    if exp is not NA and state == "gate_inconclusive":
        c["category"] = "gate_inconclusive"   # the data could not test it: neither right nor wrong
    elif exp is False and state == "not_run" and (rf.get("explained_by_observed_inputs") or {}).get("verdict") is False:
        c["category"] = "correctly_unknown"
    cells["fidelity_verdict"] = c
    emitted = bool(tools.get("genome"))
    ex = rf.get("explained_by_observed_inputs") or {}
    rec = ex.get("verdict")
    cells["explained_by_observed_inputs"] = _cell(
        e["explained"], rec, "claim" if dec == "PASS" else "hedged", rec == e["explained"],
        tool="rulefit+fidelity", genome_emitted=emitted, fidelity=dec, fidelity_state=state,
        reasons=ex.get("reasons"), side=ex.get("side"), timing=ex.get("timing"))
    return cells


def totals(rows: dict[str, dict]) -> dict:
    cats = Counter(c["category"] for r in rows.values() for c in r.values())
    scored = sum(c["truth"] is not None and c["truth"] != NA and c["category"] != "gate_inconclusive"
                 for r in rows.values() for c in r.values())
    fid = Counter(r["fidelity_verdict"].get("state", "?") for r in rows.values() if "fidelity_verdict" in r)
    claims = sum(c["category"] not in ("not_scored", "gate_inconclusive") and c["recovered"] is not None
                 and (c["status"] == "claim" or c["ci_covers_truth"] is not None)
                 for r in rows.values() for c in r.values())
    cov = [c["ci_covers_truth"] for r in rows.values() for c in r.values() if c["ci_covers_truth"] is not None]
    return {"categories": dict(cats), "cells_with_true_value": scored, "confident_claims": claims,
            "fidelity_states": dict(fid),
            "recovery_accuracy": round(cats["correct"] / scored, 4) if scored else None,
            "false_confidence_rate": round(cats["wrongly_confident"] / claims, 4) if claims else None,
            "ci_coverage": round(sum(cov) / len(cov), 4) if cov else None, "ci_cells": len(cov)}


SHORT = {"correct": "OK", "wrong": "WRONG", "wrongly_confident": "WRONG!", "correctly_unknown": "unk-ok",
         "missed": "missed", "not_scored": "-", "gate_inconclusive": "gate"}


def markdown(card: dict) -> str:
    L = ["# Answer-key scorecard", "",
         f"range {card['range']}, stream {card['stream']}, seed {card['seed']}; categories: OK correct, "
         "WRONG hedged wrong, WRONG! wrongly confident, unk-ok correctly unknown, missed (said UNKNOWN on a "
         "true rule), gate fidelity stopped by a data gate (in no rate), - not scored", "",
         "| strategy | " + " | ".join(OBLIGATIONS) + " |", "|---" * (len(OBLIGATIONS) + 1) + "|"]
    for s, row in card["cells"].items():
        L.append(f"| {s} | " + " | ".join(SHORT[row[o]["category"]] for o in OBLIGATIONS) + " |")
    t = card["totals"]
    L += ["", f"recovery accuracy {t['recovery_accuracy']} ({t['categories'].get('correct', 0)} of "
              f"{t['cells_with_true_value']} cells with a true value); false-confidence rate "
              f"{t['false_confidence_rate']} ({t['categories'].get('wrongly_confident', 0)} of "
              f"{t['confident_claims']} confident claims); CI coverage {t['ci_coverage']} over {t['ci_cells']} cells", "",
          f"fidelity: {t.get('fidelity_states')} (not_run = rulefit emitted no genome; gate_inconclusive = a data gate "
          f"stopped it, which is not a verdict); holdout window starts {card.get('holdout_window_starts')}"
          + (" < 30: NO fidelity cell of this run can be read" if card["holdout_window_starts"] < MIN_FIDELITY_STARTS
             else "" if card["fidelity_readable"] else ": no subject reached a fidelity verdict"), ""]
    wc = [f"{s}.{o}" for s, row in card["cells"].items() for o in OBLIGATIONS if row[o]["category"] == "wrongly_confident"]
    L += ["wrongly confident cells: " + (", ".join(wc) or "none"), ""]
    for k, v in card.get("hidden_and_decoy", {}).items():
        L.append(f"- {k}: {v}")
    L += ["", "Per cell values: scorecard.json -> cells.<strategy>.<obligation> (truth, recovered, status, category)."]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- tools
def run_tool(mod: str, args: list[str], log: Path) -> tuple[int, float]:
    """``python -m doppel.lab.<mod>`` in a child process; (exit code, peak RSS MB of the child)."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as fh:
        p = subprocess.Popen([sys.executable, "-m", f"doppel.lab.{mod}", *args], stdout=fh, stderr=subprocess.STDOUT)
        _, status, ru = os.wait4(p.pid, 0)
    p.returncode = os.waitstatus_to_exitcode(status)
    return p.returncode, _mb(ru.ru_maxrss)


def _mb(maxrss: int) -> float:
    return round(maxrss / (1 << 20) if sys.platform == "darwin" else maxrss / 1024, 1)


def _load(p: Path):
    return orjson.loads(p.read_bytes()) if p.exists() else None


def documented_no_draft(report: dict) -> bool:
    genome = report.get("genome") or {}
    return (genome.get("status") == "not_emitted" and bool(genome.get("reasons"))
            and "verdict" in (report.get("explained_by_observed_inputs") or {}))


def run_tools(pub: Path, out: Path, wallet: str, t_from: int, split: int, t_to: int) -> dict:
    """The five tools on ``pub`` only. Returns their parsed outputs, exit codes and peak RSS."""
    tape, orders = str(pub / "tape"), str(pub / "orders.jsonl")
    span = ["--from", str(t_from), "--to", str(t_to)]
    om = out / "ordermatch"
    info: dict = {"exit": {}, "rss_mb": {}}

    def go(mod, args, *, name=None):
        step = name or mod
        rc, mb = run_tool(mod, args, out / f"{step}.log")
        info["exit"][step], info["rss_mb"][step] = rc, mb
        (out / "tool-status.json").write_bytes(orjson.dumps(info))
        if rc and not (mod == "rulefit" and rc == 1):
            raise ValueError(f"{step} exited {rc}, see {out / (step + '.log')}")

    go("ordermatch", ["--data", tape, "--orders", orders, "--wallet", wallet, *span, "--out", str(om)])
    go("orderreplay", ["--data", tape, "--ordermatch-dir", str(om), *span, "--orders", orders,
                       "--out", str(out / "orderreplay")])
    go("profile", ["--orders", orders, "--ordermatch", str(om / "ordermatch.json"), "--wallet", wallet,
                   "--out", str(out / "profile")])
    train_om = out / "ordermatch-train"
    train_orders = str(pub / "train-orders.jsonl")
    go("ordermatch", ["--data", tape, "--orders", train_orders, "--wallet", wallet, "--from", str(t_from),
                      "--to", str(split), "--out", str(train_om)], name="ordermatch-train")
    brti = ["--brti-lab", tape] if (pub / "tape" / "ref_brti_raw").exists() else []
    go("rulefit", ["--data", tape, "--ordermatch-dir", str(train_om), "--orders", train_orders, "--wallet", wallet,
                   "--from", str(t_from), "--to", str(split), "--tokens", str(pub / "lab" / "tokens.jsonl"),
                   *brti, "--out", str(out / "rulefit")])
    g = out / "rulefit" / "genome.json"
    report = _load(out / "rulefit" / "fit_report.json")
    if not report or "verdict" not in (report.get("explained_by_observed_inputs") or {}):
        raise ValueError(f"rulefit did not produce a completed result; see {out / 'rulefit.log'}")
    if info["exit"]["rulefit"] and (g.exists() or not documented_no_draft(report)):
        raise ValueError(f"unexpected rulefit exit for this report; see {out / 'rulefit.log'}")
    if g.exists():
        go("fidelity", ["--data", tape, "--lab", str(pub / "lab"), "--genome", str(g), "--wallet", wallet,
                        "--orders", orders, "--ordermatch-dir", str(om), "--coverage-receipt",
                        str(pub / "coverage_receipt.json"), "--from", str(split), "--to", str(t_to),
                        "--out", str(out / "fidelity")])
    fidelity = _load(out / "fidelity" / "fidelity.json")
    if g.exists() and (not fidelity or (fidelity.get("verdict") or {}).get("decision") not in ("PASS","FAIL","INCONCLUSIVE")):
        raise ValueError(f"missing fidelity result for emitted genome; see {out / 'fidelity.log'}")
    return info | {
        "ordermatch": orjson.loads((om / "ordermatch.json").read_bytes())["wallets"][wallet],
        "orderreplay": orjson.loads((out / "orderreplay" / "orderreplay.json").read_bytes()),
        "profile": orjson.loads((out / "profile" / f"{wallet}.ledger.json").read_bytes()),
        "rulefit": report, "genome": _load(g), "fidelity": fidelity}


def violation_causes(sim: Sim, tdir: Path) -> dict:
    """Each pessimistic over-fill of orderreplay's headline set, by its cause in the truth: the book
    match took another increase (``wrong_placement``), the order's real cancel was not the decrease
    ordermatch took (``wrong_cancel_end``), a book-cross fill (``book_cross``), else ``other``."""
    truth = {o.oid: o for o in sim.orders}
    rp, om = tdir / "orderreplay" / "orderreplay.jsonl", tdir / "ordermatch" / "orders.jsonl"
    if not rp.exists() or not om.exists():
        return {}
    match = {r["order_id"]: r for r in map(orjson.loads, om.read_bytes().splitlines())}
    out: Counter = Counter()
    for r in map(orjson.loads, rp.read_bytes().splitlines()):
        if r.get("kind") != "order" or str(r.get("outcome", "")).startswith("unknown") or r.get("cut") \
                or r["pessimistic"]["error"] <= TOL:
            continue
        o, m = truth.get(r["id"]), match.get(r["id"])
        out["unknown_order" if o is None or m is None else
            "wrong_placement" if m["placed_venue_ms"] != o.land_ms else
            "wrong_cancel_end" if o.status == "cancelled" and m.get("end_ms") != o.end_ms else
            "book_cross" if any(f[4] == "book_cross" for f in o.fills) else "other"] += 1
    return dict(out)


def pipeline_checks(sim: Sim, tools: dict, tdir: Path | None = None) -> dict:
    """Plumbing: does the book match find the subject's filled orders, and does the twin replay
    reproduce the truth's own fills (same fill code, same queue rule)?"""
    om = tools.get("ordermatch") or {}
    rp = ((tools.get("orderreplay") or {}).get("conditional_on_real_fill") or {}).get("pessimistic") or {}
    bv = (tools.get("orderreplay") or {}).get("bracket_violations") or {}
    vis = [o for o in sim.orders if o.fills and o.kind == "maker"]
    truth_out = Counter("filled" if o.status == "filled" else "cancelled" if o.status == "cancelled" else o.status
                        for o in vis)
    return {"visible_maker_orders": len(vis), "truth_outcomes_of_visible": dict(truth_out),
            "ordermatch_observable": om.get("observable"), "ordermatch_matched": om.get("matched"),
            "ordermatch_match_rate": om.get("match_rate"), "ordermatch_placebo_rate": om.get("placebo_rate"),
            "ordermatch_outcomes": om.get("outcomes"),
            "orderreplay_pessimistic_ratio": rp.get("ratio"), "orderreplay_n": rp.get("n"),
            "bracket_violations": {k: bv.get(k) for k in ("pessimistic_overfilled", "optimistic_underfilled")},
            "pessimistic_overfill_causes": violation_causes(sim, tdir) if tdir is not None else None}


# ---------------------------------------------------------------- driver
def run_strategy(key: str, root: Path, tokens: Path, stream: str, t_from: int, split: int, t_to: int,
                 out: Path, seed: int, params: dict | None = None, tools: bool = True) -> dict:
    cls = POLICIES[key]
    pol = cls(seed)
    pol.params = dict(cls.params) | (params or {})
    wallet = addr("answerkey", pol.name, seed)
    asset, hz = stream.split("_")[1], stream.split("_")[2]
    wins, _ = windows_from_tokens(TokenMap(tokens), (asset,), (hz,), t_from - 3600, t_to + 3600)
    fair = None
    if pol.name == "fair_maker":
        from .rulefit import Fair
        fair = Fair(root, t_from, t_to)
    sim = Sim(pol, wallet, wins, fair, seed)
    d = out / pol.name
    d.mkdir(parents=True, exist_ok=False)
    pub = d / "public"
    t0, c0 = time.time(), time.process_time()
    sim.run(root, stream, pub / "tape", t_from, t_to)
    write_public(sim, root, stream, tokens, pub, (split, t_to))
    sim_s = round(time.time() - t0, 1)
    rss_sim = _mb(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    truth = truth_summary(sim, pol)
    (d / "truth").mkdir(parents=True)
    with open(d / "truth" / "orders.jsonl", "wb") as fh:
        for o in sim.orders:
            fh.write(orjson.dumps({k: getattr(o, k) for k in (
                "oid", "kind", "token", "up", "slug", "price", "size", "tag", "decided_tw", "created_ms",
                "land_tw", "land_ms", "queue0", "status", "fills", "end_ms", "ref", "inv")}) + b"\n")
    signal = (pol.state.get("signal") if pol.name == "hidden_signal" else None)
    (d / "truth" / "truth.json").write_bytes(orjson.dumps(
        {"policy": pol.name, "params": pol.params, "wallet": wallet, "summary": truth, "sim_stats": dict(sim.stats),
         "hidden_signal_by_up_token": signal}, option=orjson.OPT_INDENT_2))
    res: dict = {"policy": pol.name, "params": pol.params, "wallet": wallet, "truth": truth, "sim_s": sim_s,
                 "train_visible_maker_orders": sum(o.kind == "maker" and any(math.ceil(f[0] / 1000) + 1 < split
                                                      for f in o.fills) for o in sim.orders),
                 "sim_cpu_s": round(time.process_time() - c0, 1), "rss_mb_after_sim": rss_sim}
    if tools:
        if not truth["fills"]:
            raise ValueError(f"{pol.name} produced zero fills; the trial cannot score this subject")
        tl = run_tools(pub, d / "tools", wallet, t_from, split, t_to)
        res["tools"] = {k: tl[k] for k in ("exit", "rss_mb")}
        res["cells"] = score(pol.name, truth, tl)
        res["pipeline"] = pipeline_checks(sim, tl, d / "tools")
        res["fidelity_reason"] = ((tl.get("fidelity") or {}).get("verdict") or {}).get("reason")
    return res


def policy_overrides(raw: bytes) -> dict:
    """Validate a frozen subject configuration before any simulation starts."""
    doc = orjson.loads(raw)
    if not isinstance(doc, dict) or set(doc) - POLICIES.keys():
        raise ValueError("policy parameters must be keyed by subject a through f")
    for key, fields in doc.items():
        defaults = POLICIES[key].params
        if not isinstance(fields, dict) or set(fields) - defaults.keys():
            raise ValueError(f"unrecognized parameters for subject {key}")
        for name, value in fields.items():
            if isinstance(defaults[name], list):
                if not isinstance(value, list) or not value:
                    raise ValueError(f"{key}.{name} must be a nonempty list")
                numbers = value
            else:
                numbers = [value]
            if any(type(x) not in (int, float) or not math.isfinite(x) or x < 0 for x in numbers):
                raise ValueError(f"{key}.{name} requires finite nonnegative numbers")
            if name == "levels_c":
                if len(set(numbers)) != len(numbers) or len(numbers) < 2 or any(x != int(x) or x >= 100 for x in numbers):
                    raise ValueError("ladder requires distinct integer levels in [0,100)")
            elif name == "accuracy":
                if not 0.5 < numbers[0] <= 1:
                    raise ValueError("hidden-signal accuracy must be above chance and at most one")
            elif name != "k_c_per_contract" and any(x <= 0 for x in numbers):
                raise ValueError(f"{key}.{name} must be positive")
            if key == "b" and name == "move_c" and value != defaults[name]:
                raise ValueError("the fair maker requotes on a one-cent target change")
    return doc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-answerkey")
    ap.add_argument("--data", type=Path, required=True, help="tape root holding the intl clob stream")
    ap.add_argument("--tokens", type=Path, required=True, help="tokens.jsonl (token -> window slug, idx, payout)")
    ap.add_argument("--stream", default="clob_btc_5m")
    ap.add_argument("--from", dest="t_from", type=_ts, required=True)
    ap.add_argument("--to", dest="t_to", type=_ts, required=True)
    ap.add_argument("--split", type=_ts, help="rulefit fits [from, split), fidelity holds out [split, to); "
                                              "default the middle, on a whole minute. Fidelity needs >= 30 "
                                              "window starts in the holdout: 2.5 h of 5m windows, 7.5 h of 15m. "
                                              "On a shorter holdout every fidelity verdict is INCONCLUSIVE by "
                                              "gate and the scorecard says so")
    ap.add_argument("--strategies", default="a,b,c,d,e,f")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--policy-params", type=Path, help="frozen JSON object of parameter overrides by subject")
    ap.add_argument("--no-tools", action="store_true", help="simulate and write evidence only")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    split = a.split if a.split is not None else (a.t_from + (a.t_to - a.t_from) // 2) // 60 * 60
    keys = a.strategies.split(",")
    if not a.t_from < split < a.t_to or len(keys) != len(set(keys)) or set(keys) - POLICIES.keys():
        ap.error("specify a split inside the range and unique subject keys a through f")
    if a.out.exists() and (not a.out.is_dir() or any(a.out.iterdir())):
        ap.error("the run requires a fresh output directory")
    raw_params = a.policy_params.read_bytes() if a.policy_params else b"{}"
    try:
        params = policy_overrides(raw_params)
        preflight = preflight_inputs(a.data, a.tokens, a.stream, a.t_from, split, a.t_to)
    except ValueError as exc:
        ap.error(str(exc))
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "input-preflight.json").write_bytes(orjson.dumps(preflight, option=orjson.OPT_INDENT_2))
    length = LEN[a.stream.rsplit("_", 1)[-1]]
    starts = preflight["holdout"]["book_pairs"]
    if starts < MIN_FIDELITY_STARTS and not a.no_tools:
        print(f"doppel-answerkey: the holdout [{split}, {a.t_to}) holds {starts} whole window(s); fidelity needs "
              f">= {MIN_FIDELITY_STARTS} comparable window starts, so every fidelity verdict will be INCONCLUSIVE by "
              f"gate. Use a range with >= {MIN_FIDELITY_STARTS * length / 3600:g} h after --split.", file=sys.stderr)
    res: dict = {}
    for k in keys:
        subject_params = dict(params.get(k, {}))
        if k == "e" and "a" in res and "rate_per_s" not in subject_params:
            subject_params["rate_per_s"] = max(res["a"]["train_visible_maker_orders"], 1) / (split - a.t_from)
        res[k] = run_strategy(k, a.data, a.tokens, a.stream, a.t_from, split, a.t_to, a.out, a.seed, subject_params,
                              not a.no_tools)
        print(f"{k} {res[k]['policy']}: {res[k]['truth']['status']} sim {res[k]['sim_s']} s", file=sys.stderr)
    card = {"range": [a.t_from, split, a.t_to], "stream": a.stream, "seed": a.seed, "input_preflight": preflight,
            "policy_params_sha256": hashlib.sha256(raw_params).hexdigest(), "scorecard_schema": 2,
            "holdout_window_starts": starts, "fidelity_readable": False,
            "strategies": {k: {x: v for x, v in r.items() if x != "cells"} for k, r in res.items()}}
    if not a.no_tools:
        card["cells"] = {r["policy"]: r["cells"] for r in res.values()}
        card["subjects_with_fidelity_verdict"] = [name for name, cells in card["cells"].items()
                                                  if cells["fidelity_verdict"]["state"] in ("pass", "fail")]
        card["fidelity_readable"] = starts >= MIN_FIDELITY_STARTS and bool(card["subjects_with_fidelity_verdict"])
        card["totals"] = totals(card["cells"])
        hd = {}
        for k, r in res.items():
            if r["policy"] in ("random_taker", "hidden_signal"):
                c = r["cells"]["explained_by_observed_inputs"]
                hd[r["policy"]] = {"rulefit_genome_emitted": c["genome_emitted"], "fidelity": c["fidelity"],
                                   "fidelity_state": c["fidelity_state"], "fidelity_passed": c["fidelity"] == "PASS",
                                   "rulefit_says_explained": c["recovered"], "rulefit_reasons": c["reasons"],
                                   "confident_but_wrong_rule": c["category"] == "wrongly_confident",
                                   "unvalidated_wrong_rule": c["genome_emitted"] and c["category"] == "wrong"}
        card["hidden_and_decoy"] = hd
        card["peak_rss_mb"] = {"self": _mb(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss),
                               "tools_max": max((v for r in res.values() for v in r["tools"]["rss_mb"].values()),
                                                default=None)}
        (a.out / "scorecard.md").write_text(markdown(card))
        print(markdown(card))
    (a.out / "scorecard.json").write_bytes(orjson.dumps(card, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
