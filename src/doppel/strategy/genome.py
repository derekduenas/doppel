"""Strategy genome v1: the executable definition of one DOPPEL strategy.

A genome is the ONLY thing that crosses from research to execution. It must be complete enough
that the twin (replay), the paper runtime and, later, riskd-gated live trading all run the SAME
rule from it with no interpretation. Dossier statistics and decision trees are evidence. A genome
is the strategy.

Design rules:
- Rules are data. Taker entries are conjunctions of ``Condition``s over the fixed ``FEATURES``
  vocabulary, the same per-second features the dossier's policy-inversion panel is built on. A rule
  a tree extracts from a wallet can be written down here verbatim.
- Venue semantics are explicit. On Polymarket US one YES instrument trades per window, so "buy Down"
  means SELL YES at the bid (cost 1 - bid per Down share). On intl, Up and Down are separate tokens.
- Prices are in cents of a $1 contract. Sizes are in contracts (shares).
- The genome id is the sha256 of its canonical JSON without ``status``/``created_tw``/``notes``, so
  any change to the strategy produces a new id (preregistration integrity).
- Every genome carries its evidence, the gates it must pass and a NUMERIC kill rule.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from typing import Literal

import orjson

SCHEMA_VERSION = 1

# Per-second decision features (computed as of the decision second, never after). Units in comments.
FEATURES = {
    "tau_s": "seconds since window start",
    "tte_s": "seconds to window close (settlement instant)",
    "fair_up": "fair-v1 P(Up) from raw BRTI (0-1)",
    "edge_buy_up_c": "100*(fair_up - best_ask_up) - fee_c(best_ask_up): net cents to TAKE Up now",
    "edge_buy_down_c": "100*((1-fair_up) - best_ask_down) - fee_c(best_ask_down): net cents to TAKE Down",
    "mid_up_c": "100*mid of Up (PM US: YES mid)",
    "spread_c": "best ask - best bid, cents",
    "depth_top_up": "contracts at best ask (Up / YES)",
    "depth_top_down": "contracts at best bid of YES (= Down ask depth on PM US)",
    "brti_move_1s": "raw BRTI change over last 1 s, USD",
    "brti_move_5s": "raw BRTI change over last 5 s, USD",
    "brti_move_30s": "raw BRTI change over last 30 s, USD",
    "dist_to_open_usd": "current settlement-average projection minus priceToBeat, USD",
    "inventory_up": "own net Up contracts in this window (Down counts negative)",
}

Venue = Literal["pmus", "kalshi", "intl_sim"]
Mode = Literal["maker", "taker"]
Op = Literal["<", "<=", ">", ">=", "between"]
Status = Literal["candidate", "twin_pass", "twin_fail", "shadow", "live", "retired"]


@dataclass(frozen=True)
class Condition:
    feature: str
    op: Op
    value: float
    value_hi: float | None = None  # for "between"

    def holds(self, x: dict[str, float]) -> bool:
        v = x.get(self.feature)
        if v is None or v != v:  # missing or NaN never satisfies a condition
            return False
        if self.op == "<":
            return v < self.value
        if self.op == "<=":
            return v <= self.value
        if self.op == ">":
            return v > self.value
        if self.op == ">=":
            return v >= self.value
        hi = self.value_hi if self.value_hi is not None else float("inf")
        return self.value <= v <= hi


@dataclass(frozen=True)
class Universe:
    venue: Venue
    family: Literal["updown"] = "updown"
    assets: tuple[str, ...] = ("btc",)
    horizons: tuple[str, ...] = ("5m",)
    tau_min_s: int = 0            # earliest decision second in the window
    tte_min_s: int = 0            # no new risk inside this many seconds of close


@dataclass(frozen=True)
class FairModel:
    name: Literal["fair-v1"] = "fair-v1"
    vol_window_s: int = 300
    note: str = "raw BRTI accrued-average model; settlement = A(E-1) vs A(S-1)"


@dataclass(frozen=True)
class TakerRule:
    side: Literal["up", "down"]
    when: tuple[Condition, ...]   # all must hold (AND); several TakerRules are OR-ed
    size: float                   # contracts per entry
    limit_slippage_c: float = 0.0 # max cents worse than the observed best ask
    cooldown_s: int = 5
    max_entries_per_window: int = 3


@dataclass(frozen=True)
class MakerRule:
    quote_ref: Literal["fair", "mid"] = "fair"
    half_spread_c: float = 2.0            # bid = ref - h - skew, ask = ref + h - skew (Up/YES cents)
    size: float = 10.0                    # contracts per side
    sides: Literal["both", "bid_only", "ask_only"] = "both"
    skew_c_per_contract: float = 0.0      # shift both quotes against inventory
    requote_threshold_c: float = 1.0      # requote when ref moves this much
    max_quote_age_s: int = 30
    pause_when: tuple[Condition, ...] = ()  # any holds -> pull quotes (toxicity guard)
    min_edge_vs_fair_c: float = 0.0       # never quote a price worse than fair by less than this


@dataclass(frozen=True)
class Exit:
    hold_to_settlement: bool = True
    flatten_tte_s: int | None = None      # if set, cross to flatten at this tte (taker fees apply)


@dataclass(frozen=True)
class Risk:
    max_inventory: float = 50.0           # |net Up contracts| per window
    max_notional_per_window: float = 100.0
    max_loss_per_day: float = 25.0
    max_open_windows: int = 2


@dataclass(frozen=True)
class Costs:
    taker_fee_coef: float = 0.095         # PM US BTC 5m: fee = coef * p * (1 - p) per contract
    maker_fee_coef: float = 0.0
    fee_rounding: Literal["pmus_cent_half_even", "none"] = "pmus_cent_half_even"


@dataclass(frozen=True)
class KillRule:
    min_windows: int = 300                # independent windows before any verdict
    metric: Literal["net_c_per_window", "net_c_per_contract"] = "net_c_per_window"
    pass_ci_low_gt: float = 0.0           # PASS if 95% CI lower bound > this
    kill_ci_high_lt: float = 0.0          # KILL if 95% CI upper bound < this
    max_drawdown_usd: float = 25.0


@dataclass(frozen=True)
class Source:
    kind: Literal["clone", "compose", "manual"]
    wallets: tuple[str, ...] = ()
    cohort_sha256: str | None = None
    dossier_sha256: tuple[str, ...] = ()
    mechanism: str | None = None          # census label of the source behaviour


@dataclass(frozen=True)
class Genome:
    name: str
    universe: Universe
    mode: Mode
    source: Source
    fair: FairModel = field(default_factory=FairModel)
    taker: tuple[TakerRule, ...] = ()
    maker: MakerRule | None = None
    exit: Exit = field(default_factory=Exit)
    risk: Risk = field(default_factory=Risk)
    costs: Costs = field(default_factory=Costs)
    kill: KillRule = field(default_factory=KillRule)
    gates: tuple[str, ...] = ("G2", "G3", "G4", "G5")
    evidence: dict = field(default_factory=dict)   # dossier stats backing the rule (not executed)
    schema_version: int = SCHEMA_VERSION
    status: Status = "candidate"
    created_tw: int = field(default_factory=time.time_ns)
    notes: str = ""

    # ------------------------------------------------------------------ identity
    def canonical(self) -> bytes:
        d = to_dict(self)
        for k in ("status", "created_tw", "notes"):
            d.pop(k, None)
        return orjson.dumps(d, option=orjson.OPT_SORT_KEYS)

    @property
    def id(self) -> str:
        return hashlib.sha256(self.canonical()).hexdigest()

    # ------------------------------------------------------------------ evaluation helpers
    def taker_actions(self, x: dict[str, float]) -> list[TakerRule]:
        """Taker rules whose conditions all hold for feature vector ``x`` (universe checked)."""
        if self.mode != "taker" or not self._in_universe(x):
            return []
        return [r for r in self.taker if all(c.holds(x) for c in r.when)]

    def maker_paused(self, x: dict[str, float]) -> bool:
        if self.maker is None:
            return True
        return (not self._in_universe(x)) or any(c.holds(x) for c in self.maker.pause_when)

    def maker_quotes(self, x: dict[str, float]) -> tuple[float | None, float | None]:
        """(bid_c, ask_c) for the YES/Up instrument in cents, or None per side when not quoting."""
        m = self.maker
        if m is None or self.maker_paused(x):
            return None, None
        ref = 100 * x["fair_up"] if m.quote_ref == "fair" else x.get("mid_up_c")
        if ref is None or ref != ref:
            return None, None
        skew = m.skew_c_per_contract * x.get("inventory_up", 0.0)
        bid = ref - m.half_spread_c - skew
        ask = ref + m.half_spread_c - skew
        fair_c = 100 * x["fair_up"]
        bid = min(bid, fair_c - m.min_edge_vs_fair_c)
        ask = max(ask, fair_c + m.min_edge_vs_fair_c)
        inv = x.get("inventory_up", 0.0)
        bid_ok = m.sides in ("both", "bid_only") and inv < self.risk.max_inventory
        ask_ok = m.sides in ("both", "ask_only") and inv > -self.risk.max_inventory
        lo, hi = 1.0, 99.0
        return (max(lo, min(hi, round(bid))) if bid_ok else None,
                max(lo, min(hi, round(ask))) if ask_ok else None)

    def _in_universe(self, x: dict[str, float]) -> bool:
        tau, tte = x.get("tau_s"), x.get("tte_s")
        if tau is None or tte is None:
            return False
        return tau >= self.universe.tau_min_s and tte >= self.universe.tte_min_s


# ---------------------------------------------------------------------- (de)serialisation
def to_dict(g: Genome) -> dict:
    return asdict(g)


def _conds(xs) -> tuple[Condition, ...]:
    return tuple(Condition(**c) for c in xs)


def from_dict(d: dict) -> Genome:
    d = dict(d)
    if d.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise ValueError(f"unsupported genome schema {d.get('schema_version')}")
    taker = tuple(TakerRule(**{**t, "when": _conds(t["when"])}) for t in d.pop("taker", ()))
    mk = d.pop("maker", None)
    maker = MakerRule(**{**mk, "pause_when": _conds(mk.get("pause_when", ()))}) if mk else None
    u = d.pop("universe")
    src = d.pop("source")
    return Genome(universe=Universe(**{**u, "assets": tuple(u["assets"]), "horizons": tuple(u["horizons"])}),
                  source=Source(**{**src, "wallets": tuple(src.get("wallets", ())),
                                   "dossier_sha256": tuple(src.get("dossier_sha256", ()))}),
                  fair=FairModel(**d.pop("fair")), taker=taker, maker=maker,
                  exit=Exit(**d.pop("exit")), risk=Risk(**d.pop("risk")),
                  costs=Costs(**d.pop("costs")), kill=KillRule(**d.pop("kill")),
                  gates=tuple(d.pop("gates")), **d)


def validate(g: Genome) -> list[str]:
    """Problems that make a genome non-executable or non-falsifiable (empty list = valid)."""
    errs = []
    if g.mode == "taker" and not g.taker:
        errs.append("taker genome without taker rules")
    if g.mode == "maker" and g.maker is None:
        errs.append("maker genome without maker rule")
    for r in g.taker:
        if not r.when:
            errs.append(f"taker rule {r.side} has no conditions (would fire every second)")
        for c in r.when:
            if c.feature not in FEATURES:
                errs.append(f"unknown feature {c.feature}")
            if c.op == "between" and c.value_hi is None:
                errs.append(f"between without value_hi on {c.feature}")
        if r.size <= 0:
            errs.append("taker size must be > 0")
    if g.maker is not None:
        if g.maker.half_spread_c < 0 or g.maker.size <= 0:
            errs.append("maker half_spread_c >= 0 and size > 0 required")
        for c in g.maker.pause_when:
            if c.feature not in FEATURES:
                errs.append(f"unknown feature {c.feature}")
    if g.kill.min_windows < 30:
        errs.append("kill.min_windows < 30: verdict would be noise")
    if g.universe.venue == "pmus" and g.costs.taker_fee_coef <= 0:
        errs.append("PM US taker fee coefficient must be > 0")
    return errs


def fee_c(price: float, coef: float) -> float:
    """Fee in cents per contract at price (0-1): coef * p * (1-p) dollars."""
    return 100.0 * coef * price * (1.0 - price)


def describe(g: Genome) -> str:
    """One readable paragraph: what the strategy does, exactly."""
    u = g.universe
    head = (f"{g.name} [{g.id[:12]}] {g.mode.upper()} on {u.venue} {','.join(u.assets)} "
            f"{','.join(u.horizons)} up/down, decisions from tau>={u.tau_min_s}s, no new risk "
            f"inside {u.tte_min_s}s of close; fair={g.fair.name}.")
    parts = [head]
    for r in g.taker:
        cond = " AND ".join(f"{c.feature} {c.op} {c.value}" + (f"..{c.value_hi}" if c.op == "between" else "")
                            for c in r.when)
        parts.append(f"TAKE {r.side.upper()} {r.size:g} when {cond}; max {r.max_entries_per_window}/window, "
                     f"cooldown {r.cooldown_s}s.")
    if g.maker:
        m = g.maker
        pause = " OR ".join(f"{c.feature} {c.op} {c.value}" for c in m.pause_when) or "never"
        parts.append(f"QUOTE {m.sides} around {m.quote_ref} +/- {m.half_spread_c:g}c, {m.size:g} per side, "
                     f"skew {m.skew_c_per_contract:g}c/contract, requote on {m.requote_threshold_c:g}c move "
                     f"or {m.max_quote_age_s}s; pause when {pause}.")
    ex = "hold to settlement" if g.exit.hold_to_settlement else f"flatten at tte {g.exit.flatten_tte_s}s"
    parts.append(f"Exit: {ex}. Risk: |inv|<={g.risk.max_inventory:g}, <=${g.risk.max_notional_per_window:g}/window, "
                 f"stop at -${g.risk.max_loss_per_day:g}/day. Fees: taker {g.costs.taker_fee_coef}*p(1-p). "
                 f"Kill: after {g.kill.min_windows} windows, PASS if CI-low({g.kill.metric}) > "
                 f"{g.kill.pass_ci_low_gt}, KILL if CI-high < {g.kill.kill_ci_high_lt}.")
    return " ".join(parts)
