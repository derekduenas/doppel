"""Rule fit: a wallet's chain-verified signed orders -> an explicit quoting rule with uncertainty,
emitted as a DRAFT candidate genome plus the list of what the genome schema cannot express.

LABEL: every number here is a "rule conditional on fill". Only orders that filled at least once
are on chain (E34), and E39 estimates that fills are ~0.3 % of what such a maker posts. The fit
describes the quotes that TRADED. The only view of the rest is ordermatch's fingerprint candidates
(``quotes.jsonl``), an attribution estimate: when present the price rule is refitted with them,
weighted by the stated identity precision, and the move is reported (selection-bias check).

INPUTS
- ``--reconstruct-manifest`` supplies the verified order tape, per-wallet matched orders and
  aggregate summary. Fingerprint candidates require stand-alone ordermatch inputs.
- ``--ordermatch-dir``: ``orders.jsonl`` (maker orders located in the book; rows of the wallet,
  ``fill_before_placement`` dropped), optional ``quotes.jsonl`` and ``ordermatch.json``.
- ``--orders``: the order tape (``doppel-ordertape``): the wallet's own fills (inventory), and
  when it has a ``verified`` column only orders with a verified row there are kept (counted).
- ``--data``: tape root (``clob_<asset>_<hz>`` books, ``rtds_*`` for the token map).
- Token -> window and Up/Down, no network: RTDS rows on the tape, ``tokens.jsonl`` files
  (``--tokens``, ``<brti-lab>/tokens.jsonl``), and the mate of a mapped token. Unmapped rows are
  counted and left out (they cannot be put in Up terms).
- ``--brti-lab LAB``: enables fair-v1. Raw BRTI and the settlement series are read from LAB when it
  holds ``ref_brti_raw``, else from ``--data``; the same directory may hold ``tokens.jsonl``.

1. DECISION TABLE (``decision_table.jsonl``): one row per order, state as of ``cutoff`` = the
   placement event's receive time ``placed_tw_ns`` (``--asof created``: that minus the order's own
   sign-to-book delay, the state when it was signed). Book state = ``IntlBook`` after every tape
   record with ``tw < cutoff`` and nothing else: the order's own increase is NOT in it.
   Everything is in Up-exposure terms, cents: BUY Up @ p = bid p; SELL Up @ p = ask p;
   BUY Down @ p = ask 100 - p; SELL Down @ p = bid 100 - p; a Down token's book is mirrored.
   STATE columns (``STATE_COLS``; functions of the cutoff and of data received before it, tested
   against a rewritten future): ``bb_c``/``ba_c``/``mid_c``/``spread_c``, ``depth_bid``/
   ``depth_ask`` (size at the best), ``tau_s``/``tte_s``, ``fair_c`` (twin ``Features.fair`` at
   second floor(cutoff): BRTI received by then, 1 s delay; BTC only, null before the window start;
   cached per (start, end)), ``inv`` (own net Up contracts in the window: tape fills located by a
   tx-hash print received before the cutoff, else block time < cutoff; maker and taker),
   ``notional_usd`` (own opening notional in the window from the same fills, accrued as the twin
   does), ``since_prev_s`` (cutoff minus the receive time of its previous verified order's
   placement on that token).
   ORDER columns (``ORDER_COLS``; the order itself, what a rule has to reproduce, never a
   predictor): ``up_side``, ``px_c``, ``size``, ``placed_venue_ms``, ``d_touch_c`` (limit -
   same-side best), ``d_mid_c``, ``d_fair_c``, ``gap_s`` (venue seconds since its previous
   verified order on the token).
   The best bid/ask can contain the wallet's own OTHER resting quotes: they are anonymous.
2. FITS. Each fitted number is ``{value, n, clusters, ci95, oos}``: ci95 = percentile bootstrap
   resampling market windows (seeded). The price fits share a chronological split of market
   windows (``thresholds``), with roughly 56 % for fit, 14 % for validation and 30 % for test.
   Windows straddling a boundary are excluded from the price fits. Every price-model choice (reference,
   constant vs inventory model, keeping the skew) is made on the validation slice with models
   fitted on the fit slice; the chosen model is refitted on fit + validation and scored ONCE on
   the test slice (``oos.mae_c``). Reported parameters use a refit on all eligible rows.
   Descriptive numbers carry the same statistic on the final 30 % of observations.
   a. price, per Up side: limit = ref + a - skew * inv for ref in touch / mid / fair (refs known
      for >= half the rows compete, on the rows where all of them are known). ``tie`` when the
      bootstrap CI of the validation-error difference to the runner-up spans 0 (touch and mid
      cannot be told apart under a constant spread). Reported: ``rule`` (the selected model),
      offset median and IQR, the IQR of the model's residual, share within 1 c, skew per contract
      (genome sign: positive = quotes shift down when long), slopes on time-to-close and spread.
   b. size: ``profile._size`` (fixed sizes / fixed notional / variable), median, slope on price.
   c. timing: re-post gap; what preceded each order since its previous one on the token:
      ``own_fill`` (an own fill in the window in between), else ``book_move`` (same-token best
      bid or ask differs by >= 1 c from the state just after the previous order landed), else
      ``timer``; cancels (ordermatch ``cancelled``): life, time from last fill, replaced within
      1 s, and at the cancel whether the opposite best had moved >= 1 c or the order was behind
      the best. Ladder: share of orders with another own order live on the same token and side at
      a different price, and how many distinct prices (``fit_ladder``).
   d. participation: 10 s cells of every window of the wallet's asset-horizons with a known book
      at the cell's start (the Up token's, else the Down token's mirrored); a cell is ``present``
      when a verified order rested in it (placement to ``rest_max_ms``, else ``rest_min_ms``).
      Rates by time-to-close and mid band, both sides vs one. Absence of a FILLED order is not
      absence of a quote.
3. GENOME (``genome.json``, ``status: "draft"``; ``Registry.register`` refuses that status): the
   best rule the v1 schema can hold (one reference mid|fair, one symmetric half-spread, one skew)
   fitted on orders of assets with fair-v1 only, mid and fair compared on one common set of rows,
   and scored against the per-side rules on the same test rows. ``Risk.max_inventory`` and
   ``max_notional_per_window`` are MEASURED from the wallet (>= fitted size and p99 |inventory|;
   p95 opening notional per window) so the rule can act; they are not limits. The emitted genome
   is then checked through ``Genome.maker_quotes`` and the twin's ``WindowSim._room`` on its
   fitting observations (``emitted_genome_check``). This is a static check at observed state.
   Assess the complete draft with a separately reserved replay window. NO genome is written
   (``genome.status: "not_emitted"`` with
   the reasons, exit 1) when: no order is on an asset with fair-v1; no expressible rule could be
   fitted; half_spread_c < 0.5 (rounds to a self-cross); fewer than 2 windows; or the emitted
   genome would quote on < 70 % of the wallet's fitting observations.
   ``fit_report.json`` -> ``cannot_express`` lists every measured behaviour the schema drops, each
   with its number.

Fingerprint candidates are read in two streaming passes and capped (``--max-candidates``, default
5,000, proportional per window, seeded); the cap and the file total are in ``inputs.candidates``.
The refit with them is reported as "if the attribution held", never as a bound.

Analysis only. No look-ahead in the state columns (tested); outcome columns come from ordermatch,
which reads the future by design, and never enter a state column.
"""

from __future__ import annotations

import argparse
import bisect
from array import array
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from random import Random
from types import SimpleNamespace
from typing import Any

import numpy as np
import orjson

from ..strategy.genome import Condition, Genome, MakerRule, Risk, Source, Universe, to_dict, validate
from ..tape.segment import read_segment, segment_path
from ..twin.engine import MIN_QTY, WindowSim
from ..twin.features import Features, RefStore
from .census import LEN, window_of
from .flow import RTDS_STREAMS, _ts, hours_between, iter_rtds
from .intlbook import NS, IntlBook, LevelEvent, Print, px
from .ledger import TokenMap
from .ordermatch import filled_shares
from .profile import BIAS_LENGTH, BIAS_UPPER, _size, load_rows, prepare

TRAIN_FRAC = 0.7
INNER_FRAC = 0.8          # of the training slice: fit on the first 80 %, select on the last 20 %
MAX_CANDIDATES = 5000     # fingerprint candidates kept per wallet (stratified by window, seeded)
FAIR_ASSETS = ("btc",)    # assets with fair-v1 (raw BRTI); a v1 maker never quotes without fair_up
MIN_HALF_SPREAD_C = 0.5   # below this bid and ask round to the same cent
MIN_QUOTE_SHARE = 0.7     # in-sample static intent check for the full-sample draft
LADDER_SHARE = 0.1
LADDER_MIN_MS = 1000      # two quotes are a ladder only if they rest together at least this long ...
LADDER_GAP_FRAC = 0.5     # ... and at least this share of the wallet's own median re-post gap
SIDE_MIN_ORDERS = 4       # a window says which sides a wallet quotes only with this many visible orders
SKILL_MIN = 0.05          # share of the null's deviance a timing rule must explain (PROVISIONAL, set before any run)
STATE_COLS = ("bb_c", "ba_c", "mid_c", "spread_c", "depth_bid", "depth_ask", "fair_c", "tau_s", "tte_s", "inv",
              "notional_usd", "since_prev_s")
ORDER_COLS = ("placed_venue_ms", "up_side", "px_c", "size", "d_touch_c", "d_mid_c", "d_fair_c", "gap_s")
N_BOOT = 400
SEED = 20261126
MIN_N = 20
GRID_S = 10
MOVE_C = 1.0
REPLACE_MS = 1000
TOL = 0.02  # shares
TOL_REL = 0.02  # the widest relative size tolerance ordermatch is run with (E39)
REFS = ("touch", "mid", "fair")
CAVEAT = ("rule conditional on fill: only orders that filled at least once are visible on chain; "
          "E39 estimates fills at ~0.3% of this kind of maker's quotes, so offsets, sizes, gaps and "
          "absences describe quotes that traded, not the quoting policy")


# ---------------------------------------------------------------- small numerics
def _r(x, n: int = 4):
    if x is None:
        return None
    x = float(x)
    return round(x, n) + 0.0 if math.isfinite(x) else None  # + 0.0: no "-0.0" in reports


def wmedian(x: np.ndarray, w: np.ndarray) -> float:
    o = np.argsort(x, kind="stable")
    cw = np.cumsum(w[o])
    return float(x[o][min(int(np.searchsorted(cw, 0.5 * cw[-1])), len(x) - 1)])


def wmean(x: np.ndarray, w: np.ndarray) -> float:
    return float(np.sum(x * w) / np.sum(w))


def wls(X: np.ndarray, y: np.ndarray, w: np.ndarray) -> np.ndarray:
    s = np.sqrt(w)
    return np.linalg.lstsq(X * s[:, None], y * s, rcond=None)[0]


def boot_ci(stat, cl: np.ndarray, n_boot: int = N_BOOT) -> list | None:
    """95 % percentile CI of ``stat(row indices)`` resampling clusters (market windows)."""
    if not len(cl):
        return None
    codes = np.unique(cl, return_inverse=True)[1]
    order = np.argsort(codes, kind="stable")
    groups = np.split(order, np.flatnonzero(np.diff(codes[order])) + 1)
    if len(groups) < 2:
        return None
    rng = np.random.default_rng(SEED)
    vals = []
    for _ in range(n_boot):
        idx = np.concatenate([groups[i] for i in rng.integers(0, len(groups), len(groups))])
        v = np.atleast_1d(np.asarray(stat(idx), dtype=float))
        if np.isfinite(v).all():
            vals.append(v)
    if len(vals) < n_boot // 2:
        return None
    lo, hi = np.percentile(np.stack(vals), [2.5, 97.5], axis=0)
    out = [[_r(a), _r(b)] for a, b in zip(lo, hi)]
    return out[0] if len(out) == 1 else out


def est(value, n: int, cl: np.ndarray | None = None, ci=None, oos=None) -> dict:
    return {"value": _r(value), "n": int(n), "clusters": None if cl is None else int(len(np.unique(cl))),
            "ci95": ci, "oos": oos}


def _split(n: int) -> tuple[np.ndarray, np.ndarray]:
    k = int(TRAIN_FRAC * n)
    return np.arange(k), np.arange(k, n)


def describe(x: np.ndarray, cl: np.ndarray, stat=np.median) -> dict:
    """A descriptive statistic of time-ordered ``x`` with cluster CI and its holdout value."""
    if not len(x):
        return est(None, 0)
    _, te = _split(len(x))
    return est(stat(x), len(x), cl, boot_ci(lambda i: stat(x[i]), cl),
               {"holdout_value": _r(stat(x[te])) if len(te) else None, "n_test": int(len(te))})


def poisson_range(k: int) -> tuple[float, float]:
    """95 % range of a Poisson mean given one count ``k`` (Wilson-Hilferty)."""
    def q(v: float, z: float) -> float:
        return 0.5 * v * (1 - 2 / (9 * v) + z * math.sqrt(2 / (9 * v))) ** 3
    return (q(2 * k, -1.96) if k else 0.0), q(2 * (k + 1), 1.96)


def binom_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact (Clopper-Pearson) two-sided interval of a share, by bisection on the binomial tail."""
    def tail(p: float, lo: int, hi: int) -> float:
        return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(lo, hi + 1))

    def solve(f, target: float) -> float:   # f decreasing in p
        a, b = 0.0, 1.0
        for _ in range(50):
            m = (a + b) / 2
            a, b = (m, b) if f(m) > target else (a, m)
        return (a + b) / 2
    lo = 0.0 if k == 0 else solve(lambda p: -tail(p, k, n), -alpha / 2)
    hi = 1.0 if k == n else solve(lambda p: tail(p, 0, k), alpha / 2)
    return lo, hi


def footprint_bounds(fp: dict | None) -> dict | None:
    """From ordermatch's fingerprint counts: p = the share of the wallet's quotes that filled, and
    the share of the fingerprint candidates that are other people's (chance), each with a range.
    The chance count is the control-token rate scaled to the active token-hours; its range is the
    Poisson range of the control count. Without control tokens nothing bounds it: the range is
    every candidate or none."""
    t = (fp or {}).get("total")
    if not t:
        return None
    filled = t.get("own", 0) + t.get("own_unmatched", 0)
    cand = max(0, t.get("candidates", 0) - t.get("own_unmatched", 0))
    if not filled:
        return None
    exp, k = t.get("expected_by_chance"), t.get("control") or 0
    act, ctl = fp.get("active_token_hours") or 0.0, fp.get("control_token_hours") or 0.0  # type: ignore[union-attr]
    if exp is None or ctl <= 0:
        lo, hi, known = 0.0, float(cand), False
    else:
        a, b = poisson_range(k)
        lo, hi, known = min(cand, a * act / ctl), min(cand, b * act / ctl), True
    exp = min(float(cand), exp or 0.0)
    return {"filled_quotes": filled, "candidates": cand, "chance_known": known,
            "p": _r(filled / (filled + cand - exp)), "p_range": [_r(filled / (filled + cand - lo)), _r(filled / (filled + cand - hi))],
            "chance_share": _r(exp / cand) if cand and known else None,
            "chance_share_range": [_r(lo / cand), _r(hi / cand)] if cand else [0.0, 0.0]}


def thinned_gap(m_obs: float, p_lo: float, p_hi: float) -> tuple[float, float]:
    """Range of the true median gap between placements when each placement is SEEN independently
    with a probability in [p_lo, p_hi] and the observed median gap is ``m_obs``. A seen gap is the
    sum of a geometric number of true gaps. Lower end: a Poisson stream (the median scales with
    p). Upper end: a strictly periodic stream (the median divides by the geometric median)."""
    geo = 1 if p_hi >= 0.5 else math.ceil(math.log(0.5) / math.log(1 - p_hi))
    return p_lo * m_obs, m_obs / geo


def median_gap(times: dict[str, list[int]]) -> tuple[float | None, int]:
    """(median gap in ms between consecutive events of one token, number of gaps)."""
    g = [b - a for v in times.values() for a, b in zip(sorted(v), sorted(v)[1:])]
    return (float(np.median(g)) if g else None), len(g)


def _skill(loss_rule: np.ndarray, loss_null: np.ndarray, cl: np.ndarray) -> dict:
    """1 - loss(rule) / loss(null) over held-out observations, CI over market windows."""
    tot = float(loss_null.sum())
    if not len(loss_rule) or tot <= 0:
        return est(None, len(loss_rule), cl)
    ci = boot_ci(lambda i: 1 - loss_rule[i].sum() / loss_null[i].sum() if loss_null[i].sum() > 0 else math.nan, cl)
    return est(1 - float(loss_rule.sum()) / tot, len(loss_rule), cl, ci)


def _quant(x: np.ndarray) -> dict | None:
    if not len(x):
        return None
    return {f"p{q}": _r(np.percentile(x, q)) for q in (10, 25, 50, 75, 90)} | {"n": int(len(x))}


# ---------------------------------------------------------------- token map (no network)
def token_map(root: Path, t_from: int, t_to: int, token_files: list[Path]) -> dict[str, dict]:
    """token -> {slug, asset, hz, start, end, idx} from tokens.jsonl files and RTDS rows on the tape."""
    out: dict[str, dict] = {}
    for p in token_files:
        for tok, info in TokenMap(p).m.items():
            w = window_of(info)
            if w and info.get("idx") in (0, 1):
                out[tok] = w | {"idx": int(info["idx"])}
    if any((Path(root) / s).exists() for s in RTDS_STREAMS):
        for _, fills in iter_rtds(Path(root), hours_between(t_from - 3600, t_to)):
            for f in fills:
                out.setdefault(f.token, {"asset": f.coin, "hz": f.hz, "start": f.start, "end": f.end,
                                         "slug": f.slug, "idx": f.idx})
    return out


def _stream(info: dict) -> str:
    return f"clob_{info['asset']}_{info['hz']}"


# ---------------------------------------------------------------- book scan
def _top(levels: dict | None) -> tuple | None:
    """(best bid, its size, best ask, its size) of one token; None while its state is unknown."""
    if levels is None:
        return None
    bb = ba = None
    for (p, side), z in levels.items():
        if z <= 0:
            continue
        if side == "BUY":
            if bb is None or p > bb:
                bb = p
        elif ba is None or p < ba:
            ba = p
    return (bb, levels[(bb, "BUY")] if bb is not None else None,
            ba, levels[(ba, "SELL")] if ba is not None else None)


def scan(root: Path, stream: str, t_from: int, t_to: int, queries: list[tuple],
         cancels: dict[tuple, list], txs: set[str], by_start: dict[int, list[tuple[str, str, int]]],
         length: int, *, grid_from: int | None = None) -> dict:
    """One pass over a stream in tape order.

    ``queries`` (tw_ns, token, key) sorted by tw: each is answered with the token's top of book
    BEFORE the first record with ``tw >= tw_ns`` is applied, i.e. from records received strictly
    earlier. ``cancels``: (token, price, side, venue_ms) -> [(key, remainder)]: for each entry, the
    top just before the decrease at that level and venue ms whose size is CLOSEST to the remainder
    (within max(TOL, TOL_REL * remainder)). ``txs``: earliest receive ns of a print per tx. Grid: each mapped token's top
    at every GRID_S boundary inside its window, keyed (slug, boundary, idx)."""
    book = IntlBook()
    state: dict = {}
    at_cancel: dict = {}
    cancel_diff: dict = {}
    fill_tw: dict[str, int] = {}
    grid: dict[tuple[str, int, int], tuple | None] = {}
    qi = 0
    grid_start = grid_from if grid_from is not None else t_from
    next_bin = grid_start - grid_start % GRID_S + GRID_S

    def answer(tw: int) -> None:
        nonlocal qi
        while qi < len(queries) and queries[qi][0] <= tw:
            _, tok, key = queries[qi]
            state[key] = _top(book.levels.get(tok))
            qi += 1

    def before(tw: int) -> None:
        nonlocal next_bin
        answer(tw)
        while next_bin * NS <= tw:
            for tok, slug, idx in by_start.get(next_bin - next_bin % length, ()):
                grid[(slug, next_bin, idx)] = _top(book.levels.get(tok))
            next_bin += GRID_S

    for hour in range(t_from - t_from % 3600, t_to, 3600):
        path = segment_path(Path(root), stream, hour * NS)
        if not path.exists():
            before(max(hour, t_from) * NS)
            book.reset()
            continue
        for rec in read_segment(path, verify=True):
            tw = rec["tw"]
            if not t_from * NS <= tw < t_to * NS:
                continue
            before(tw)
            items = book.feed(rec)
            for it in items:
                if isinstance(it, Print):
                    if it.tx in txs and tw < fill_tw.get(it.tx, 1 << 62):
                        fill_tw[it.tx] = tw
                elif isinstance(it, LevelEvent) and it.delta < 0:
                    dec = -it.delta
                    near = [c for c in cancels.get((it.asset, it.price, it.side, it.venue_ms), ())
                            if abs(dec - c[1]) <= max(TOL, TOL_REL * c[1])
                            and abs(dec - c[1]) < cancel_diff.get(c[0], math.inf)]
                    if near:
                        lv = dict(book.levels.get(it.asset) or {})
                        for e in reversed([x for x in items if isinstance(x, LevelEvent) and x.asset == it.asset]):
                            lv[(e.price, e.side)] = e.old_size  # undo this record: the state before it
                        top = _top(lv)
                        for c in near:
                            at_cancel[c[0]], cancel_diff[c[0]] = top, abs(dec - c[1])
    answer(t_to * NS - 1)
    return {"state": state, "at_cancel": at_cancel, "fill_tw": fill_tw, "grid": grid, "mates": book.mates()}


# ---------------------------------------------------------------- Up terms
def up_view(top: tuple | None, idx: int) -> dict:
    """Top of a token's book in Up cents. A Down bid at p is an Up ask at 100 - p."""
    if top is None:
        return {"bb_c": None, "ba_c": None, "depth_bid": None, "depth_ask": None, "mid_c": None, "spread_c": None}
    bb, bs, ba, as_ = top
    if idx == 1:
        bb, bs, ba, as_ = (None if ba is None else 1 - ba), as_, (None if bb is None else 1 - bb), bs
    both = bb is not None and ba is not None
    return {"bb_c": _r(None if bb is None else 100 * bb), "ba_c": _r(None if ba is None else 100 * ba),
            "depth_bid": bs, "depth_ask": as_,
            "mid_c": _r(50 * (bb + ba)) if both else None,  # type: ignore[operator]
            "spread_c": _r(100 * (ba - bb)) if both else None}  # type: ignore[operator]


def up_order(side: str, price: float, idx: int) -> tuple[str, float]:
    """(Up side bid|ask, Up limit in cents) of an order on token ``idx``."""
    if idx == 0:
        return ("bid" if side == "BUY" else "ask"), 100 * price
    return ("ask" if side == "BUY" else "bid"), 100 * (1 - price)


# ---------------------------------------------------------------- inputs
ORDER_KEYS = ("order_id", "token_id", "side", "price", "size", "created_ms", "placed_venue_ms", "placed_tw_ns",
              "outcome", "reason", "end_ms", "remainder", "rest_min_ms", "rest_max_ms", "fills_at")
QUOTE_KEYS = ("token_id", "price", "venue_ms", "tw_ns", "size")


def _jsonl(path: Path):
    with open(path, "rb") as fh:  # one line at a time: quotes.jsonl can hold 100k+ rows per wallet-hour
        for line in fh:
            if line.strip():
                yield orjson.loads(line)


def load_orders(path: Path, summary: Path | None, wallet: str) -> tuple[list[dict], dict, Counter]:
    """(the wallet's located maker orders, its ordermatch summary, counts). Needed columns only."""
    stats: Counter = Counter()
    orders = []
    for r in _jsonl(path):
        if str(r.get("maker", "")).lower() != wallet:
            stats["orders_other_makers"] += 1
        elif r.get("fill_before_placement"):
            stats["orders_wrong_match_dropped"] += 1
        else:
            orders.append({k: r.get(k) for k in ORDER_KEYS})
    doc = {}
    if summary is not None and summary.exists():
        doc = (orjson.loads(summary.read_bytes()).get("wallets") or {}).get(wallet) or {}
    stats["orders"] = len(orders)
    return orders, doc, stats


def sample_quotes(path: Path | None, wallet: str, tok: dict[str, dict], cap: int,
                  bounds: tuple[int, int] | None = None) -> tuple[list[dict], dict]:
    """At most ``cap`` of the wallet's fingerprint candidates, stratified by window (by token when
    the token is not mapped), seeded. Two streaming passes; the file is never held in memory."""
    info = {"cap": cap, "total": 0, "outside_range": 0, "kept": 0, "seed": SEED, "strata": 0,
            "sampling": "proportional per window (largest remainder), uniform within a window"}
    if path is None or not path.exists():
        return [], info
    def mine(row: dict) -> bool:
        return str(row.get("wallet", wallet)).lower() == wallet

    def in_range(row: dict) -> bool:
        return bounds is None or bounds[0] <= row["tw_ns"] < bounds[1]
    stratum = lambda r: (tok.get(r["token_id"]) or {}).get("slug") or r["token_id"]  # noqa: E731
    counts: Counter = Counter()
    for row in _jsonl(path):
        if mine(row):
            if in_range(row):
                counts[stratum(row)] += 1
            else:
                info["outside_range"] += 1
    n = sum(counts.values())
    info |= {"total": n, "strata": len(counts)}
    keep: dict[str, set[int] | None] = {s: None for s in counts}  # None = all of the stratum
    if n > cap:
        rng = Random(SEED)
        names = sorted(counts)
        share = np.array([cap * counts[s] / n for s in names])
        quota = np.floor(share).astype(int)
        for i in np.argsort(-(share - quota), kind="stable")[:cap - int(quota.sum())]:
            quota[i] += 1
        keep = {s: set(rng.sample(range(counts[s]), int(q))) for s, q in zip(names, quota)}
    seen: Counter = Counter()
    out = []
    for r in _jsonl(path):
        if not mine(r) or not in_range(r):
            continue
        s = stratum(r)
        k = keep[s]
        if k is None or seen[s] in k:
            out.append({c: r[c] for c in QUOTE_KEYS})
        seen[s] += 1
    info["kept"] = len(out)
    return out, info


def quote_events(path: Path | None, wallet: str, bounds: tuple[int, int]) -> dict:
    """EVERY fingerprint candidate of the wallet received in ``bounds`` (not the capped sample):
    placement ms per token, and (token, life ms) of those ordermatch saw cancelled. One pass, three
    numbers per row, held as 4-byte arrays (ms since the range began): a quote-flickering wallet has
    ~10^5 candidates per hour."""
    t0 = bounds[0] // 1_000_000
    times: dict[str, array] = defaultdict(lambda: array("i"))
    lives: dict[str, array] = defaultdict(lambda: array("i"))
    if path is not None and path.exists():
        for r in _jsonl(path):
            if str(r.get("wallet", wallet)).lower() == wallet and bounds[0] <= r["tw_ns"] < bounds[1]:
                times[r["token_id"]].append(int(r["venue_ms"]) - t0)
                if r.get("outcome") == "cancelled" and r.get("rest_max_ms") is not None:
                    lives[r["token_id"]].append(min(int(r["rest_max_ms"]), 2**31 - 1))
    return {"t0_ms": t0, "times": dict(times), "lives": dict(lives), "n": sum(len(v) for v in times.values())}


def wallet_fills(orders_path: Path, wallet: str) -> tuple[list[dict], dict, set[str] | None]:
    """The wallet's verified tape fills [{tx, token, side, shares, price, block_ts}], input counts,
    and its verified order ids (None for a legacy tape without a ``verified`` column)."""
    rows, flagged = load_rows(orders_path, {wallet})
    w = prepare(rows, flagged)
    ids = {r["order_id"] for r in w["rows"]} if flagged else None
    out = [{"tx": r["tx"], "token": r["token_id"], "side": r["side"], "shares": filled_shares(r),
            "price": float(r.get("limit") or 0.5), "block_ts": r["block_ts"], "role": r.get("role")} for r in w["rows"]
           if r["side"] == "SELL" or r.get("maker_amount") or r.get("limit")]
    return out, w["input"], ids


class Inventory:
    """Own net Up contracts and opening notional per window from tape fills, as of a receive
    instant. Notional accrues as in the twin (``WindowSim._fill``): the part of a fill that opens
    or adds exposure, at its Up-terms cost (price for Up bought, 1 - price for Up sold). The fill
    price is the order's limit."""

    def __init__(self, fills: list[dict], tok: dict[str, dict], fill_tw: dict[str, int],
                 bounds: tuple[int, int] | None = None):
        by: dict[str, list[tuple[int, float, float]]] = defaultdict(list)
        self.unmapped = self.by_block_time = 0
        for f in fills:
            info = tok.get(f["token"])
            if info is None:
                self.unmapped += 1
                continue
            t = fill_tw.get(f["tx"])
            if t is None:  # no print: the block time trails the match, so the fill counts late
                t = int(f["block_ts"]) * NS
                self.by_block_time += 1
            if bounds is not None and not bounds[0] <= t < bounds[1]:
                continue
            sign = (1 if f["side"] == "BUY" else -1) * (1 if info["idx"] == 0 else -1)
            up_px = f.get("price", 0.5) if info["idx"] == 0 else 1 - f.get("price", 0.5)
            by[info["slug"]].append((t, sign * f["shares"], up_px))
        self.t: dict[str, list[int]] = {}
        self.cum: dict[str, list[float]] = {}
        self.notional: dict[str, list[float]] = {}
        for slug, xs in by.items():
            xs.sort()
            inv = spent = 0.0
            self.t[slug], self.cum[slug], self.notional[slug] = [], [], []
            for t, q, p in xs:
                closing = min(abs(q), max(0.0, -inv if q > 0 else inv))
                spent += (abs(q) - closing) * (p if q > 0 else 1 - p)
                inv += q
                self.t[slug].append(t)
                self.cum[slug].append(inv)
                self.notional[slug].append(spent)
        self.n = len(fills)

    def _i(self, slug: str, tw: int) -> int:
        return bisect.bisect_left(self.t.get(slug, []), tw)

    def at(self, slug: str, tw: int) -> float:
        """Net Up contracts from fills located strictly before ``tw``."""
        i = self._i(slug, tw)
        return self.cum[slug][i - 1] if i else 0.0

    def notional_at(self, slug: str, tw: int) -> float:
        """Opening notional (USD) of the window from fills located strictly before ``tw``."""
        i = self._i(slug, tw)
        return self.notional[slug][i - 1] if i else 0.0

    def window_notional(self, windows: set[str]) -> list[float]:
        return [v[-1] for slug, v in self.notional.items() if slug in windows and v]

    def any_between(self, slug: str, lo: int, hi: int) -> bool:
        ts = self.t.get(slug, [])
        return bisect.bisect_left(ts, hi) > bisect.bisect_right(ts, lo)


class Fair:
    """fair-v1 through the twin's own ``Features.fair`` (BRTI masked by receive time)."""

    def __init__(self, root: Path, t_from: int, t_to: int):
        self.store = RefStore(root)
        self.store.load_boundaries("btc", t_from - 3600, t_to + 3600)
        self.genome = Genome(name="rulefit-fair", universe=Universe(venue="intl_sim"), mode="maker",
                             source=Source(kind="manual"))
        self.feat: dict[tuple[int, int], Features | None] = {}

    def at(self, info: dict, tw: int) -> float | None:
        d = tw // NS
        if info["asset"] not in FAIR_ASSETS or d < info["start"]:
            return None
        key = (info["start"], info["end"])  # 5m and 15m windows share starts: the end is part of the key
        if key not in self.feat:
            try:
                ref = self.store.window_ref("btc", info["start"], info["end"], self.genome.fair.vol_window_s)
                self.feat[key] = Features(self.genome, ref)
            except KeyError:
                self.feat[key] = None
        f = self.feat[key]
        v = f.fair(d) if f is not None else math.nan
        return 100 * v if v == v else None


# ---------------------------------------------------------------- decision table
def decision_table(root: Path, om_dir: Path | None, orders_path: Path, wallet: str, t_from: int, t_to: int,
                   token_files: list[Path], brti_root: Path | None = None, asof: str = "placement",
                   max_candidates: int = MAX_CANDIDATES, *, matched_orders: Path | None = None,
                   match_summary: Path | None = None) -> dict:
    wallet = wallet.lower()
    if t_from >= t_to:
        raise ValueError("rulefit interval must end after it begins")
    if om_dir is not None:
        matched_orders = om_dir / "orders.jsonl"
        match_summary = om_dir / "ordermatch.json"
    if matched_orders is None:
        raise ValueError("missing matched orders")
    orders, om_doc, stats = load_orders(matched_orders, match_summary, wallet)
    lo, hi = t_from * NS, t_to * NS
    in_range = [o for o in orders if lo <= o["placed_tw_ns"] < hi]
    stats["orders_outside_range_dropped"] = len(orders) - len(in_range)
    orders = in_range
    fills, fill_input, verified = wallet_fills(orders_path, wallet)
    if verified is not None:  # ordermatch does not look at ``verified``; the order tape does
        stats["orders_not_verified_dropped"] = sum(o["order_id"] not in verified for o in orders)
        orders = [o for o in orders if o["order_id"] in verified]
    tok = token_map(root, t_from, t_to, token_files)
    quotes_path = om_dir / "quotes.jsonl" if om_dir is not None else None
    quotes, cand_info = sample_quotes(quotes_path, wallet, tok, max_candidates, (lo, hi))
    stats["candidates"] = len(quotes)
    stats["candidates_in_file"] = cand_info["total"] + cand_info["outside_range"]
    s2b = [o["placed_venue_ms"] - o["created_ms"] for o in orders]
    lag_q = int(max(0, np.median(s2b))) if s2b else 0  # candidates have no stamp: the wallet's median

    specs = []
    for o in orders:
        lag = max(0, o["placed_venue_ms"] - o["created_ms"]) if asof == "created" else 0
        specs.append({"kind": "order", "id": o["order_id"], "token": o["token_id"], "side": o["side"],
                      "price": px(o["price"]), "size": float(o["size"]), "w": 1.0,
                      "placed_tw": int(o["placed_tw_ns"]), "placed_ms": int(o["placed_venue_ms"]),
                      "cutoff": int(o["placed_tw_ns"]) - lag * 1_000_000, "om": o})
    for q in quotes:
        specs.append({"kind": "candidate", "id": f"{q['token_id']}:{q['price']}:{q['venue_ms']}:{q['tw_ns']}",
                      "token": q["token_id"], "side": "BUY", "price": px(q["price"]), "size": float(q["size"]),
                      "w": None, "placed_tw": int(q["tw_ns"]), "placed_ms": int(q["venue_ms"]),
                      "cutoff": int(q["tw_ns"]) - (lag_q if asof == "created" else 0) * 1_000_000, "om": {}})
    del quotes
    # A created-time cutoff may precede its placement by enough to cross the lower bound.
    # Load one market window of pre-roll to reconstruct state and locate prior fills.
    first_cutoff = min((s["cutoff"] // NS for s in specs), default=t_from)
    scan_from = min(t_from, first_cutoff) - max(LEN.values())

    mapped = {s["token"] for s in specs} & tok.keys()
    if not specs:  # a wallet with no maker order: its taker fills say where it trades
        mapped = {f["token"] for f in fills if f["role"] == "taker"} & tok.keys()
    streams = sorted({_stream(tok[t]) for t in mapped}) or sorted(
        d.name for d in Path(root).iterdir() if d.name.startswith("clob_"))
    pairs = {(tok[t]["asset"], tok[t]["hz"]) for t in mapped}
    txs = {f["tx"] for f in fills}
    res = {"state": {}, "at_cancel": {}, "fill_tw": {}, "grid": {}, "mates": {}}
    for s in streams:
        mine = [x for x in specs if x["token"] not in tok or _stream(tok[x["token"]]) == s]
        qs = sorted([(x["cutoff"], x["token"], ("pre", x["id"])) for x in mine]
                    + [(x["placed_tw"] + 1, x["token"], ("post", x["id"])) for x in mine if x["kind"] == "order"],
                    key=lambda q: q[0])
        cancels: dict[tuple, list] = defaultdict(list)
        for x in mine:
            o = x["om"]
            if x["kind"] == "order" and o.get("outcome") == "cancelled" and o.get("end_ms") is not None:
                cancels[(x["token"], x["price"], x["side"], o["end_ms"])].append((x["id"], o["remainder"]))
        hz = s.rsplit("_", 1)[1]
        # every mapped token of the stream's windows (Up or Down): a window whose Up token is only
        # mapped later, as its mate's mate, still gets its grid from the Down book
        by_start: dict[int, list[tuple[str, str, int]]] = defaultdict(list)
        for t, info in tok.items():
            if _stream(info) == s and (info["asset"], info["hz"]) in pairs:
                by_start[info["start"]].append((t, info["slug"], info["idx"]))
        got = scan(root, s, scan_from, t_to, qs, cancels, txs, by_start, LEN.get(hz, 300), grid_from=t_from)
        stats["cancels_asked"] += sum(len(v) for v in cancels.values())
        stats["cancels_state_found"] += len(got["at_cancel"])
        for k in ("state", "at_cancel", "grid", "mates"):
            res[k].update({a: b for a, b in got[k].items() if k != "state" or b is not None or a not in res[k]})
        for tx, t in got["fill_tw"].items():
            res["fill_tw"][tx] = min(t, res["fill_tw"].get(tx, t))
    for a, b in res["mates"].items():  # a token never seen by RTDS, but its mate was
        if a not in tok and b in tok:
            tok[a] = tok[b] | {"idx": 1 - tok[b]["idx"]}
            stats["tokens_mapped_by_mate"] += 1

    inv = Inventory(fills, tok, res["fill_tw"], (scan_from * NS, hi))
    fair = Fair(brti_root, t_from, t_to) if brti_root is not None else None
    rows = []
    prev: dict[str, dict] = {}  # token -> previous verified order's spec
    for x in sorted(specs, key=lambda s: (s["placed_tw"], s["id"])):
        info = tok.get(x["token"])
        if info is None:
            stats[f"{x['kind']}_unmapped_token"] += 1
            continue
        top = res["state"].get(("pre", x["id"]))
        if top is None:
            stats[f"{x['kind']}_book_unknown"] += 1
        side, pc = up_order(x["side"], x["price"], info["idx"])
        v = up_view(top, info["idx"])
        touch = v["bb_c"] if side == "bid" else v["ba_c"]
        f = fair.at(info, x["cutoff"]) if fair is not None else None
        p = prev.get(x["token"])
        if p is not None and p["placed_tw"] >= x["cutoff"]:
            p = None  # not yet in the book at the cutoff
        row = {"id": x["id"], "kind": x["kind"], "w": x["w"], "window": info["slug"],
               "window_start_ns": info["start"] * NS, "window_end_ns": info["end"] * NS, "asset": info["asset"],
               "hz": info["hz"], "token": x["token"], "idx": info["idx"], "cutoff_tw_ns": x["cutoff"],
               # state (STATE_COLS): functions of the cutoff and of data received before it
               **v, "fair_c": _r(f),
               "tau_s": _r(x["cutoff"] / NS - info["start"], 3), "tte_s": _r(info["end"] - x["cutoff"] / NS, 3),
               "inv": _r(inv.at(info["slug"], x["cutoff"])),
               "notional_usd": _r(inv.notional_at(info["slug"], x["cutoff"])),
               "since_prev_s": None if p is None else _r((x["cutoff"] - p["placed_tw"]) / NS, 3),
               # the order itself (ORDER_COLS): what the rule has to reproduce
               "placed_venue_ms": x["placed_ms"], "up_side": side, "px_c": _r(pc), "size": x["size"],
               "d_touch_c": _r(None if touch is None else pc - touch),
               "d_mid_c": _r(None if v["mid_c"] is None else pc - v["mid_c"]),
               "d_fair_c": _r(None if f is None else pc - f), "gap_s": None}
        if x["kind"] == "order":
            q = prev.get(x["token"])
            if q is not None:
                row["gap_s"] = _r((x["placed_ms"] - q["placed_ms"]) / 1000, 3)
                x["prev"] = q
            prev[x["token"]] = x
        x["row"], x["info"] = row, info
        rows.append(row)
    takes = []
    for f in fills:
        t = res["fill_tw"].get(f["tx"], (int(f["block_ts"]) - 2) * NS)  # no print: ~2 s before its block
        if f["role"] == "taker" and f["token"] in tok and lo <= t < hi:
            takes.append({"tw": t, "token": f["token"], "idx": tok[f["token"]]["idx"], "side": f["side"],
                          "slug": tok[f["token"]]["slug"], "located": f["tx"] in res["fill_tw"]})
    return {"rows": rows, "specs": [x for x in specs if "row" in x], "res": res, "inv": inv, "tok": tok,
            "takes": takes, "fair": fair, "cand": quote_events(quotes_path, wallet, (lo, hi)),
            "om_doc": om_doc, "stats": stats, "pairs": sorted(pairs),
            "inputs": {"fills": fill_input | {"n": inv.n, "unmapped": inv.unmapped,
                                              "timed_by_block": inv.by_block_time},
                       "streams": streams, "asof": asof, "from": t_from, "to": t_to, "fair": fair is not None,
                       "sign_to_book_ms_p50": lag_q, "candidates": cand_info}}


# ---------------------------------------------------------------- 2a price rule
def _arr(rows: list[dict], key: str) -> np.ndarray:
    return np.array([math.nan if r.get(key) is None else r[key] for r in rows], dtype=float)


def thresholds(rows: list[dict]) -> tuple[int, int]:
    """Validation and test boundaries from chronological market-window starts, in ns."""
    starts = sorted({r["window_start_ns"] for r in rows})
    if not starts:
        return 0, 0
    test = min(len(starts) - 1, max(2, int(TRAIN_FRAC * len(starts))))
    val = min(test, max(1, int(INNER_FRAC * test)))
    return starts[val], starts[test]


def _phase(rows: list[dict], th: tuple[int, int]) -> np.ndarray:
    starts = np.array([r["window_start_ns"] for r in rows], dtype=np.int64)
    ends = np.array([r["window_end_ns"] for r in rows], dtype=np.int64)
    # A longer market window may straddle a boundary between two shorter ones.
    return np.select([ends <= th[0], starts >= th[1], (starts >= th[0]) & (ends <= th[1])],
                     [0, 2, 1], default=-1)


def _side_data(rows: list[dict], side: str, th: tuple[int, int]) -> dict:
    rs = sorted((r for r in rows if r["up_side"] == side), key=lambda r: r["cutoff_tw_ns"])
    y = _arr(rs, "px_c")
    return {"y": y, "w": _arr(rs, "w"), "inv": _arr(rs, "inv"), "tte": _arr(rs, "tte_s"), "phase": _phase(rs, th),
            "spread": _arr(rs, "spread_c"), "cl": np.array([r["window"] for r in rs], dtype=object).astype(str),
            "ref": {"touch": y - _arr(rs, "d_touch_c"), "mid": y - _arr(rs, "d_mid_c"),
                    "fair": y - _arr(rs, "d_fair_c")}}


def _side_model(d: np.ndarray, X: np.ndarray, w: np.ndarray, i: np.ndarray, model: str) -> np.ndarray:
    """(intercept, slope on inventory) of d fitted on rows ``i``; slope 0 for the constant model."""
    if model == "skew" and i.sum() > 2:
        return wls(X[i], d[i], w[i])
    return np.array([wmedian(d[i], w[i]), 0.0])


def fit_price_side(D: dict, force: str | None = None) -> dict:
    """Price rule of one Up side (module docstring 2a)."""
    n_all = len(D["y"])
    known = {r: np.isfinite(D["ref"][r]) for r in REFS}
    fit_phase = D["phase"] == 0
    cands = [r for r in REFS if (known[r] & fit_phase).sum() >= max(MIN_N, 0.5 * fit_phase.sum())]
    if force is not None:
        cands = [force] if force in cands else []
    out: dict = {"n_rows": n_all, "ref_coverage": {r: _r(known[r].mean()) if n_all else None for r in REFS},
                 "chosen": None}
    if not cands:
        return out | {"reason": f"no reference known for >= max({MIN_N}, half) of the fit rows"}
    m = (D["phase"] >= 0) & np.isfinite(D["inv"]) & np.all([known[r] for r in cands], axis=0)
    y, w, inv, cl, ph = D["y"][m], D["w"][m], D["inv"][m], D["cl"][m], D["phase"][m]
    n = len(y)
    fit, val, tr, te = ph == 0, ph == 1, ph <= 1, ph == 2
    if fit.sum() < MIN_N // 2 or not val.any():
        return out | {"reason": "too few rows with every candidate reference in the fit or validation windows"}
    X = np.column_stack([np.ones(n), inv])
    # selection (reference, constant vs inventory model) on the validation slice only
    by: dict = {}
    verr: dict[str, np.ndarray] = {}
    for r in cands:
        d = y - D["ref"][r][m]
        e = {k: np.abs(d - X @ _side_model(d, X, w, fit, k))[val] for k in ("const", "skew")}
        mae = {k: wmean(v, w[val]) for k, v in e.items()}
        model = "skew" if mae["skew"] < mae["const"] else "const"
        verr[r] = e[model]
        by[r] = {"model": model, "val_mae_const_c": _r(mae["const"]), "val_mae_skew_c": _r(mae["skew"])}
    rank = sorted(cands, key=lambda r: (wmean(verr[r], w[val]), REFS.index(r)))
    best = rank[0]
    model = by[best]["model"]
    out |= {"n": n, "n_fit": int(fit.sum()), "n_val": int(val.sum()), "n_test": int(te.sum()), "candidates": by,
            "chosen": best, "model": model, "tie_with": None,
            "selection": "reference and model chosen on validation windows, refitted on fit and validation "
                         "windows, and scored once on test windows"}
    if len(rank) > 1:
        diff = verr[rank[1]] - verr[best]
        ci = boot_ci(lambda i: wmean(diff[i], w[val][i]), cl[val])
        out["runner_up"] = {"ref": rank[1], "val_mae_gap_c": _r(wmean(diff, w[val])), "ci95": ci}
        if ci is None or ci[0] <= 0:
            out["tie_with"] = rank[1]
    # Score the chosen reference and model once, after fitting on fit and validation windows.
    d = y - D["ref"][best][m]
    bt = _side_model(d, X, w, tr, model)
    et = np.abs(d - X @ bt)[te]
    in1 = lambda e: (e <= 1.0 + 1e-9).astype(float)  # noqa: E731
    oos = {"mae_c": _r(wmean(et, w[te])) if te.any() else None,
           "within_1c": _r(wmean(in1(et), w[te])) if te.any() else None, "n_test": int(te.sum())}
    out["train_rule"] = {"ref": best, "offset_c": float(bt[0]), "skew_c_per_contract": float(-bt[1])}
    # reported parameters: the same model refitted on ALL rows (the test score above is not theirs)
    off = wmedian(d, w)
    q = lambda p: float(np.percentile(d, p))  # noqa: E731
    out["offset_c"] = est(off, n, cl, boot_ci(lambda i: wmedian(d[i], w[i]), cl), oos) | {
        "p25": _r(q(25)), "p75": _r(q(75)), "iqr": _r(q(75) - q(25))}
    e_all = in1(np.abs(d - off))
    ec = np.abs(d - wmedian(d[tr], w[tr]))[te]
    out["within_1c_of_const_rule"] = est(wmean(e_all, w), n, cl, boot_ci(lambda i: wmean(e_all[i], w[i]), cl),
                                         {"holdout_value": _r(wmean(in1(ec), w[te])) if te.any() else None,
                                          "n_test": int(te.sum())})
    slope = lambda i: -wls(X[i], d[i], w[i])[1]  # noqa: E731
    out["skew_c_per_contract"] = est(slope(np.arange(n)), n, cl, boot_ci(slope, cl),
                                     {"val_mae_c_const": by[best]["val_mae_const_c"],
                                      "val_mae_c_with_skew": by[best]["val_mae_skew_c"], "n_val": int(val.sum())})
    ba = wls(X, d, w)
    out["intercept_c_with_skew"] = _r(ba[0])
    skewed = model == "skew"
    res = d - (X @ ba if skewed else off)
    # the rule as selected: limit = ref + offset_c - skew_c_per_contract * inv
    out["rule"] = {"ref": best, "offset_c": _r(ba[0]) if skewed else _r(off),
                   "skew_c_per_contract": out["skew_c_per_contract"]["value"] if skewed else 0.0,
                   "fitted_on": "all eligible rows; offset_c.oos uses only fit and validation windows"}
    out["residual_iqr_c"] = _r(np.percentile(res, 75) - np.percentile(res, 25))  # spread the rule leaves
    out["inventory_range"] = [_r(inv.min()), _r(inv.max())]
    # dependence on time-to-close and spread, next to inventory (descriptive; selects nothing)
    tte, sp = D["tte"][m], D["spread"][m]
    ok = np.isfinite(tte) & np.isfinite(sp)
    if ok.sum() >= MIN_N:
        Z = np.column_stack([np.ones(n), inv, tte, sp])[ok]
        dz, wz, cz = d[ok], w[ok], cl[ok]
        full = wls(Z, dz, wz)
        ci = boot_ci(lambda i: wls(Z[i], dz[i], wz[i])[2:], cz)
        out["slope_c_per_s_to_close"] = est(full[2], len(dz), cz, ci[0] if ci else None)
        out["slope_c_per_c_spread"] = est(full[3], len(dz), cz, ci[1] if ci else None)
    return out


def _excludes_zero(e: dict | None) -> bool:
    ci = (e or {}).get("ci95")
    return bool(ci) and (ci[0] > 0 or ci[1] < 0)


def fit_price(rows: list[dict], th: tuple[int, int]) -> dict:
    return {s: fit_price_side(_side_data(rows, s, th)) for s in ("bid", "ask")}


def _side_pred(rows: list[dict], price: dict) -> np.ndarray:
    """Per-side rule (fit and validation windows) applied to ``rows``. NaN where it has no rule or
    its reference is unknown."""
    out = np.full(len(rows), math.nan)
    for i, r in enumerate(rows):
        u = price[r["up_side"]].get("train_rule")
        d = r.get(f"d_{u['ref']}_c") if u else None
        if u and d is not None and r["inv"] is not None:
            out[i] = (r["px_c"] - d) + u["offset_c"] - u["skew_c_per_contract"] * r["inv"]
    return out


def fit_expressible(rows: list[dict], price: dict, th: tuple[int, int]) -> dict:
    """The best rule MakerRule v1 can hold: one ref (mid|fair), symmetric half-spread, one skew.
    Only rows of assets with fair-v1 enter (a v1 maker cannot quote without fair). Mid and fair
    are compared on one common set of rows; the choice of reference and of keeping the skew is
    made on the validation slice, the test slice is scored once."""
    n_in = len(rows)
    rows = [r for r in rows if r["asset"] in FAIR_ASSETS]
    out: dict = {"quote_ref": None, "rows_in": n_in, "rows_dropped_no_fair_model": n_in - len(rows),
                 "assets": sorted({r["asset"] for r in rows}), "horizons": sorted({r["hz"] for r in rows})}
    if not rows:
        return out | {"reason": f"no order on an asset with fair-v1 ({', '.join(FAIR_ASSETS)}): a v1 maker "
                                "(quote_ref mid|fair) never quotes without fair_up"}
    rs = sorted(rows, key=lambda r: r["cutoff_tw_ns"])
    inv, w, ph = _arr(rs, "inv"), _arr(rs, "w"), _phase(rs, th)
    sign = np.array([-1.0 if r["up_side"] == "bid" else 1.0 for r in rs])
    cl = np.array([r["window"] for r in rs], dtype=object).astype(str)
    dref = {ref: _arr(rs, f"d_{ref}_c") for ref in ("mid", "fair")}
    base = np.isfinite(inv) & np.isfinite(dref["mid"])
    has_fair = np.isfinite(dref["fair"])
    fit_base = base & (ph == 0)
    refs = ["mid"] + (["fair"] if (fit_base & has_fair).sum() >= max(MIN_N, 0.5 * fit_base.sum()) else [])
    m = (ph >= 0) & (base & has_fair if "fair" in refs else base)
    out |= {"refs_compared": refs, "rows_dropped_by_fair_requirement": int((base & ~m & (ph >= 0)).sum()),
            "rows_straddling_split": int((ph < 0).sum()),
            "rows_without_book_or_inventory": int((~base).sum())}
    im, sm, wm, cm, pm = inv[m], sign[m], w[m], cl[m], ph[m]
    fit, val, tr, te = pm == 0, pm == 1, pm <= 1, pm == 2
    if fit.sum() < MIN_N // 2 or not val.any():
        return out | {"reason": "too few rows with a book in the fit or validation windows"}
    Xd = np.column_stack([sm < 0, sm > 0, im]).astype(float)  # side intercepts, one inventory slope

    def params(d: np.ndarray, i: np.ndarray, skew: bool) -> tuple[float, float, float]:
        """(half-spread, centre shift, skew) on rows ``i``. The half-spread is NOT clamped here
        (negative = the quotes sit inside the reference)."""
        k = -float(wls(Xd[i], d[i], wm[i])[2]) if skew and len(d[i]) > 3 else 0.0
        z = d[i] + k * im[i]
        lo, hi = (wmedian(z[sm[i] == s], wm[i][sm[i] == s]) if (sm[i] == s).any() else None for s in (-1.0, 1.0))
        if lo is not None and hi is not None:
            return (hi - lo) / 2, (hi + lo) / 2, k
        return (hi if hi is not None else -(lo or 0.0)), 0.0, k

    def err(d: np.ndarray, p: tuple[float, float, float]) -> np.ndarray:
        return np.abs(d - sm * max(p[0], 0.0) + p[2] * im)  # the centre shift cannot be held: not used

    grid = [(ref, skew) for ref in refs for skew in (False, True)]
    vmae = {c: wmean(err(dref[c[0]][m], params(dref[c[0]][m], fit, c[1]))[val], wm[val]) for c in grid}
    ref, skew = min(grid, key=lambda c: (vmae[c], grid.index(c)))
    d = dref[ref][m]
    et = err(d, params(d, tr, skew))[te]
    mae = wmean(et, wm[te]) if te.any() else None
    h_raw, c, k = params(d, np.arange(len(d)), skew)
    ci = boot_ci(lambda i: params(d, i, skew), cm)
    o = {"mae_c": _r(mae), "n_test": int(te.sum())}
    # explained share of the PRICE on the test rows: the rule against "the same price whatever the book"
    # (each side's median limit on the fit and validation rows)
    pxv = _arr(rs, "px_c")[m]
    e0 = np.zeros(len(pxv))
    for sd in (-1.0, 1.0):
        if (tr & (sm == sd)).any():
            e0[sm == sd] = np.abs(pxv[sm == sd] - wmedian(pxv[tr & (sm == sd)], wm[tr & (sm == sd)]))
    out["price_skill"] = _skill((et * wm[te]), (e0[te] * wm[te]), cm[te]) | {
        "null": "each side's median limit price on the fit and validation windows, whatever the book",
        "mae_c_rule": _r(mae), "mae_c_null": _r(wmean(e0[te], wm[te])) if te.any() else None}
    out |= {"quote_ref": ref, "half_spread_c": _r(max(h_raw, 0.0), 2), "centre_shift_c": _r(c, 2),
            "skew_c_per_contract": _r(k, 4), "skew_kept": skew,
            "validation_mae_c": {f"{a}{'+skew' if b else ''}": _r(v) for (a, b), v in vmae.items()},
            "selection": "reference and keep-skew chosen on validation windows, refitted on fit and validation "
                         "windows, and scored once on test windows. Draft parameters use all eligible rows",
            "half_spread_fit": est(h_raw, len(d), cm, ci[0] if ci else None, o) | {"clamped_to_zero": h_raw < 0},
            "centre_shift_fit": est(c, len(d), cm, ci[1] if ci else None, o),
            "skew_fit": est(k, len(d), cm, ci[2] if ci else None, o),
            "n": int(len(d)), "oos_mae_c": _r(mae), "n_test": int(te.sum()), "windows": int(len(np.unique(cm))),
            "sides_seen": sorted({"bid" if s < 0 else "ask" for s in sm})}
    # the per-side rules on exactly the same test rows (where each has its reference)
    used = [r for r, keep in zip(rs, m) if keep]
    ps = np.abs(_side_pred(used, price) - _arr(used, "px_c"))[te]
    both = np.isfinite(ps)
    if both.any():
        a, b = wmean(et[both], wm[te][both]), wmean(ps[both], wm[te][both])
        out |= {"per_side_fit_oos_mae_c": _r(b), "oos_mae_c_same_rows": _r(a), "oos_cost_of_schema_c": _r(a - b),
                "n_test_common": int(both.sum())}
    else:
        out |= {"per_side_fit_oos_mae_c": None, "oos_mae_c_same_rows": None, "oos_cost_of_schema_c": None,
                "n_test_common": 0}
    return out


def check_genome(g: Genome, rows: list[dict], fair_supplied: bool) -> dict:
    """Check draft quote intent at observed book, inventory and notional state.

    This is in-sample. The twin's quote timing, landing book and queue need a separate replay.
    When fair-v1 is unavailable the check substitutes the mid and reports the substitution.
    """
    m = g.maker
    assert m is not None
    why: Counter = Counter()
    errs, clamp = [], 0
    for r in rows:
        if r["mid_c"] is None or r["inv"] is None:
            why["no_book"] += 1
            continue
        fair = r["fair_c"] if fair_supplied else r["mid_c"]
        x = {"tau_s": r["tau_s"], "tte_s": r["tte_s"], "inventory_up": r["inv"], "mid_up_c": r["mid_c"],
             "fair_up": math.nan if fair is None else fair / 100}
        if not g._in_universe(x):
            why["universe"] += 1
        elif fair is None:
            why["no_fair"] += 1
        elif g.maker_paused(x):
            why["paused"] += 1
        else:
            bid, ask = g.maker_quotes(x)
            q = bid if r["up_side"] == "bid" else ask
            if bid is not None and ask is not None and ask <= bid:
                why["self_cross"] += 1
            elif q is None:
                why["inventory_cap_or_side"] += 1
            elif (r["up_side"] == "bid" and r["ba_c"] is not None and q >= r["ba_c"]) or (
                    r["up_side"] == "ask" and r["bb_c"] is not None and q <= r["bb_c"]):
                why["would_cross_book"] += 1
            else:
                sim = SimpleNamespace(g=g, notional=r["notional_usd"] or 0.0)
                room = WindowSim._room(sim, "buy" if r["up_side"] == "bid" else "sell", q / 100, r["inv"])  # type: ignore[arg-type]
                if min(m.size, room) < MIN_QTY:
                    why["no_room"] += 1
                else:
                    sgn = -1 if r["up_side"] == "bid" else 1
                    ref = fair if m.quote_ref == "fair" else r["mid_c"]
                    raw = round(ref + sgn * m.half_spread_c - m.skew_c_per_contract * r["inv"])
                    clamp += q != max(1, min(99, raw))
                    errs.append(abs(q - r["px_c"]))
    n = len(rows)
    return {"rows": n, "would_quote": len(errs), "would_quote_share": _r(len(errs) / n) if n else None,
            "mae_c_when_quoting": _r(np.mean(errs)) if errs else None, "silent_by": dict(why),
            "fair_clamp_binds_share": _r(clamp / len(errs)) if errs else None,
            "fair_substituted_by_mid": not fair_supplied,
            "note": "In-sample quote intent at observed state. Out-of-sample execution needs a separate replay."}


# ---------------------------------------------------------------- 2b size rule
def fit_size(specs: list[dict]) -> dict:
    xs = sorted((x for x in specs if x["kind"] == "order"), key=lambda x: x["cutoff"])
    if not xs:
        return {"n": 0}
    size = np.array([x["size"] for x in xs])
    pc = np.array([x["row"]["px_c"] for x in xs])
    cl = np.array([x["row"]["window"] for x in xs], dtype=object).astype(str)
    desc = _size([{"size": x["size"], "limit": x["price"]} for x in xs])
    tr, te = _split(len(xs))
    train = _size([{"size": xs[i]["size"], "limit": xs[i]["price"]} for i in tr]) if len(tr) else {}
    s90 = set(train.get("sizes_90") or [])
    cover = float(np.mean([round(float(size[i]), 2) in s90 for i in te])) if s90 and len(te) else None
    X = np.column_stack([np.ones(len(xs)), pc])
    one = np.ones(len(xs))
    slope = lambda i: wls(X[i], size[i], one[i])[1]  # noqa: E731
    return {"n": len(xs), "descriptor": desc,
            "median_size": describe(size, cl),
            "train_size_set_covers_test": {"value": _r(cover), "n": int(len(te)), "sizes": sorted(s90)},
            "size_per_c_of_price": est(slope(np.arange(len(xs))), len(xs), cl, boot_ci(slope, cl)),
            "by_side": {s: _quant(size[[x["row"]["up_side"] == s for x in xs]]) for s in ("bid", "ask")},
            "notional_usd": _quant(np.array([x["size"] * x["price"] for x in xs]))}


# ---------------------------------------------------------------- 2c timing rule
def _share(flags: list[bool], cl: np.ndarray) -> dict:
    a = np.array(flags, dtype=float)
    return describe(a, cl, np.mean)


def fit_timing(T: dict) -> dict:
    xs = sorted((x for x in T["specs"] if x["kind"] == "order"), key=lambda x: x["cutoff"])
    state, inv = T["res"]["state"], T["inv"]
    gaps = [x for x in xs if x["row"]["gap_s"] is not None]
    cl = np.array([x["row"]["window"] for x in gaps], dtype=object).astype(str)
    trig = []
    for x in gaps:
        p = x["prev"]
        a, b = state.get(("post", p["id"])), state.get(("pre", x["id"]))
        if inv.any_between(x["info"]["slug"], p["placed_tw"], x["cutoff"]):
            trig.append("own_fill")
        elif a is None or b is None or p["placed_tw"] >= x["cutoff"]:
            trig.append("unknown")
        elif any(u is None or v is None or abs(u - v) * 100 >= MOVE_C - 1e-9 for u, v in ((a[0], b[0]), (a[2], b[2]))):
            trig.append("book_move")
        else:
            trig.append("timer")
    g = np.array([x["row"]["gap_s"] for x in gaps])
    fb = footprint_bounds(T["om_doc"].get("fingerprint"))
    own_t: dict[str, list[int]] = defaultdict(list)
    for x in xs:
        own_t[x["token"]].append(x["placed_ms"] - T["cand"]["t0_ms"])
    out: dict = {"n_orders": len(xs), "gap_s": fit_cadence(g, cl, own_t, T["cand"]["times"], fb),
                 "footprint": fb,
                 "gap_note": "gap_s.value is the corrected estimate; gap_s.filled is the gap between FILLED orders on "
                             "one token (a fill-conditional upper bound, no CI)",
                 "trigger_shares": {k: _share([t == k for t in trig], cl)
                                    for k in ("own_fill", "book_move", "timer", "unknown")} if trig else {},
                 "gap_s_by_trigger": {k: _quant(g[[t == k for t in trig]]) for k in ("own_fill", "book_move", "timer")},
                 "trigger_note": "book_move compares the token's best bid/ask just after the previous own order "
                                 "landed with the state before this one; the wallet's own cancels move it too"}
    # cancels (ordermatch outcome), post-placement facts only
    can = [x for x in xs if x["om"].get("outcome") == "cancelled" and x["om"].get("rest_max_ms") is not None]
    out["outcomes"] = dict(Counter(f"{x['om'].get('outcome')}:{x['om'].get('reason')}" for x in xs))
    if can:
        cc = np.array([x["row"]["window"] for x in can], dtype=object).astype(str)
        life = np.array([x["om"]["rest_max_ms"] for x in can], dtype=float)
        by_tok: dict[tuple, list[int]] = defaultdict(list)
        for x in xs:
            by_tok[(x["token"], x["side"])].append(x["placed_ms"])
        after_fill, replaced, opp, behind, known = [], [], [], [], []
        for x in can:
            o = x["om"]
            end = o["end_ms"]
            loc = [f[1] for f in o.get("fills_at") or [] if f[1] is not None]
            if loc:
                after_fill.append(end - max(loc))
            replaced.append(any(abs(t - end) <= REPLACE_MS for t in by_tok[(x["token"], x["side"])]
                                if t != x["placed_ms"]))
            a, b = state.get(("pre", x["id"])), T["res"]["at_cancel"].get(x["id"])
            if a is None or b is None:
                continue
            known.append(x["row"]["window"])
            buy = x["side"] == "BUY"
            u, v = (a[2], b[2]) if buy else (a[0], b[0])  # opposite best at placement / at cancel
            opp.append(u is None or v is None or abs(u - v) * 100 >= MOVE_C - 1e-9)
            own = b[0] if buy else b[2]
            behind.append(own is not None and ((own > x["price"] + 1e-9) if buy else (own < x["price"] - 1e-9)))
        ck = np.array(known, dtype=str)
        out["cancel"] = {
            **fit_life(life, cc, T["cand"]["lives"], T["tok"], fb),
            "ms_from_last_fill": _quant(np.array(after_fill, dtype=float)),
            "replaced_within_1s": _share(replaced, cc),
            "opposite_best_moved_1c": _share(opp, ck) if opp else None,
            "behind_best_at_cancel": _share(behind, ck) if behind else None,
            "neither_moved_nor_behind": _share([not (a or b) for a, b in zip(opp, behind)], ck) if opp else None,
            "note": "every visible order filled at least once: these are cancels of a REMAINDER after a fill"}
    elif T["cand"]["lives"]:
        out["cancel"] = fit_life(np.array([]), np.array([], dtype=str), T["cand"]["lives"], T["tok"], fb) | {
            "note": "no visible order was seen cancelled: lives of unfilled fingerprint candidates only"}
    else:
        out["cancel"] = None
    return out


def fit_cadence(g: np.ndarray, cl: np.ndarray, own: dict[str, list[int]], cand: dict[str, Any],
                fb: dict | None) -> dict:
    """Median re-post gap on one token, corrected for the selection (only filled orders are seen).

    ``filled``: the gap between FILLED orders, a fill-conditional upper bound, never with a CI.
    ``status`` UNKNOWN below MIN_N gaps or with one market window; UPPER_BOUND (``upper_bound`` =
    the upper limit of the filled gap's spread over windows) when nothing corrects it.
    Two corrections, each a RANGE; the reported interval is their union, widened by the sampling
    spread of the filled gap over market windows:
    - candidates: the gap in the merged series of the wallet's located orders and EVERY
      fingerprint candidate (a lower end: other people's quotes are in it), up to that gap divided
      by 1 - the largest plausible share of chance events (dropping a share s of events at random
      stretches gaps by 1 / (1 - s)); without a chance rate the upper end is the filled gap;
    - thinning: ``thinned_gap`` over the footprint's range of p.
    ``value``: the candidate estimate at the stated chance share, else the thinning range's
    geometric middle, else the filled gap (status UPPER_BOUND, no interval). The interval is not a
    sampling CI: fills are not an independent thinning and the candidates are not identified."""
    raw = describe(g, cl) if len(g) else est(None, 0)
    filled = {"value": raw["value"], "n": raw["n"], "clusters": raw["clusters"], "ci95": None, "bias": BIAS_UPPER,
              "sample_spread95": raw["ci95"], "oos": raw["oos"]}
    out = {"value": raw["value"], "n": raw["n"], "clusters": raw["clusters"], "ci95": None, "oos": None,
           "bias": BIAS_UPPER, "status": "UNKNOWN", "upper_bound": None, "method": "gap between filled orders",
           "filled": filled, "quantiles": _quant(g), "routes": {}}
    if len(g) < MIN_N or raw["ci95"] is None or not raw["value"]:
        return out  # too few gaps (or one market window) to state even a bound
    out |= {"status": "UPPER_BOUND", "upper_bound": raw["ci95"][1]}  # the bound is the sample's upper limit
    m = float(np.median(g))
    lo_f, hi_f = raw["ci95"][0] / m, raw["ci95"][1] / m   # sampling spread of the filled gap, as factors
    ranges, value, method = [], None, None
    if fb is not None:
        a, b = thinned_gap(m, fb["p_range"][0], fb["p_range"][1])
        out["routes"]["thinning"] = {"p": fb["p"], "p_range": fb["p_range"], "range_s": [_r(a), _r(b)]}
        ranges.append((a, b))
        value, method = math.sqrt(a * b), "filled gap thinned by the footprint's fill share (middle of the range)"
    n_c = sum(len(v) for v in cand.values())
    if n_c:
        merged = {t: list(own.get(t, ())) + list(cand.get(t, ())) for t in set(own) | set(cand)}
        mg, n_g = median_gap(merged)
        if mg is not None and mg > 0:
            n_all = sum(len(v) for v in merged.values())
            c_lo, c_hi = fb["chance_share_range"] if fb else (0.0, 1.0)
            c = fb["chance_share"] if fb and fb["chance_share"] is not None else None
            s_hi = c_hi * n_c / n_all
            a = mg / 1000
            b = a / (1 - s_hi) if fb and fb["chance_known"] and s_hi < 1 else m
            out["routes"]["candidates"] = {"merged_gap_s": _r(a), "n_candidates": n_c, "n_gaps": n_g,
                                           "chance_share_of_events": [_r(c_lo * n_c / n_all), _r(s_hi)],
                                           "range_s": [_r(a), _r(max(a, b))]}
            ranges.append((a, max(a, b)))
            value = a / (1 - c * n_c / n_all) if c is not None else a
            method = ("located orders merged with every fingerprint candidate"
                      + (", stretched by the stated chance share" if c is not None else " (chance share not measured)"))
    if not ranges:
        return out
    lo, hi = min(r[0] for r in ranges) * min(lo_f, 1.0), max(r[1] for r in ranges) * max(hi_f, 1.0)
    assert value is not None
    return out | {"value": _r(min(max(value, lo), hi)), "ci95": [_r(lo), _r(hi)], "status": "ESTIMATED", "method": method,
                  "interval_note": "union of the correction ranges, not a sampling CI"}


def fit_life(vis: np.ndarray, vcl: np.ndarray, cand: dict[str, Any], tok: dict[str, dict], fb: dict | None) -> dict:
    """Median quote life (placement to cancel), corrected for the selection.

    ``visible``: cancels of orders that had filled (a remainder was cancelled). Long-lived quotes
    are over-sampled (length bias), so it is an upper bound and carries no CI.
    Where ordermatch saw unfilled fingerprint candidates cancelled, the two are pooled: that is
    every cancelled quote if the attribution held. A share s of the pool may be other people's
    quotes; whatever THEIR lives are, the wallet's median lies between the pool's quantiles
    0.5 (1 - s) and 0.5 (1 + s). The interval is that band at the largest plausible s, each end
    taken at its bootstrap limit over market windows. It assumes chance candidates are seen
    cancelled as often as the wallet's own, and it is not reported when s is not measured."""
    visible = (describe(vis, vcl) if len(vis) else est(None, 0)) | {"bias": BIAS_LENGTH, "quantiles": _quant(vis)}
    visible["sample_spread95"], visible["ci95"] = visible["ci95"], None
    p90 = float(np.percentile(vis, 90)) if len(vis) else None
    bound = visible["sample_spread95"][1] if len(vis) >= MIN_N and visible["sample_spread95"] else None
    out = {"value": visible["value"], "n": visible["n"], "clusters": visible["clusters"], "ci95": None, "oos": None,
           "bias": BIAS_LENGTH, "status": "UNKNOWN" if bound is None else "UPPER_BOUND", "upper_bound": bound,
           "method": "cancels of partly filled orders", "visible": visible, "quantiles": _quant(vis)}
    cl_c = np.array([(tok.get(t) or {}).get("slug") or t for t, v in cand.items() for _ in v], dtype=str)
    lc = np.array([x for v in cand.values() for x in v], dtype=float)
    if len(lc) >= MIN_N:
        pool, pcl = np.concatenate([vis, lc]), np.concatenate([vcl.astype(str), cl_c])
        share = len(lc) / len(pool)
        p90 = float(np.percentile(pool, 90))
        out |= {"value": _r(np.median(pool)), "n": int(len(pool)), "clusters": int(len(np.unique(pcl))),
                "status": "ESTIMATED", "quantiles": _quant(pool),
                "method": "cancels of partly filled orders pooled with cancelled fingerprint candidates",
                "candidate_share_of_pool": _r(share)}
        if fb is not None and fb["chance_known"]:
            s = fb["chance_share_range"][1] * share
            out["chance_share_of_pool_max"] = _r(s)
            if s < 1:
                a = boot_ci(lambda i: np.percentile(pool[i], 50 * (1 - s)), pcl)
                b = boot_ci(lambda i: np.percentile(pool[i], 50 * (1 + s)), pcl)
                if a and b:
                    out["ci95"] = [a[0], b[1]]
                    out["interval_note"] = "contamination band of the pooled median, not a sampling CI"
    return {"life_ms": out, "life_ms_p90": {"value": _r(p90), "method": out["method"], "bias": BIAS_LENGTH}}


# ---------------------------------------------------------------- 2d participation
def grid_cells(T: dict) -> list[tuple]:
    """(slug, window start, share of the window left, Up mid, cell start) of every GRID_S cell with a
    known book at its start, in window order."""
    win = {i["slug"]: i for i in T["tok"].values()}
    seen: dict[tuple[str, int], dict] = {}
    for (slug, b, idx), top in sorted(T["res"]["grid"].items()):  # idx 0 first: the Up book when it is known
        v = up_view(top, idx)
        if v["mid_c"] is not None:
            seen.setdefault((slug, b), v)
    out = []
    for (slug, b), v in sorted(seen.items()):
        i = win[slug]
        out.append((slug, i["start"], (i["end"] - b) / (i["end"] - i["start"]), v["mid_c"], b))
    return sorted(out, key=lambda r: r[1])


def presence_cells(T: dict) -> list[tuple]:
    """``grid_cells`` with the Up sides on which a verified order rested in the cell, in place of its start."""
    cells: dict[tuple[str, int], set[str]] = defaultdict(set)
    for x in T["specs"]:
        if x["kind"] != "order":
            continue
        o = x["om"]
        rest = o.get("rest_max_ms")
        rest = o.get("rest_min_ms") or 0 if rest is None else rest
        lo, hi = x["placed_ms"] // 1000, (x["placed_ms"] + rest) // 1000
        for b in range(lo - lo % GRID_S, hi + 1, GRID_S):  # every cell [b, b + GRID_S) it rested in
            cells[(x["row"]["window"], b)].add(x["row"]["up_side"])
    return [(slug, start, frac, mid, cells.get((slug, b), set())) for slug, start, frac, mid, b in grid_cells(T)]


def fit_participation(T: dict) -> dict:
    xs = sorted((x for x in T["specs"] if x["kind"] == "order"), key=lambda x: x["cutoff"])
    recs = presence_cells(T)
    tau = np.array([x["row"]["tau_s"] for x in xs])
    tte = np.array([x["row"]["tte_s"] for x in xs])
    inwin = (tau >= 0) & (tte > 0)
    ocl = np.array([x["row"]["window"] for x in xs], dtype=object).astype(str)
    p1 = lambda v: np.percentile(v, 1)  # noqa: E731
    out: dict = {"cells": len(recs), "cell_s": GRID_S, "orders": len(xs),
                 "pre_open_order_share": _r(np.mean(tau < 0)) if len(xs) else None,
                 "tte_s_in_window": _quant(tte[inwin]), "tau_s_in_window": _quant(tau[inwin]),
                 "tte_s_p1": describe(tte[inwin], ocl[inwin], p1), "tau_s_p1": describe(tau[inwin], ocl[inwin], p1),
                 "note": "present = a FILLED order was resting in the cell; unfilled quotes are invisible"}
    if not recs:
        return out | {"reason": "no grid cell with a known book"}
    cl = np.array([r[0] for r in recs], dtype=str)
    pres = np.array([bool(r[4]) for r in recs], dtype=float)
    both = np.array([len(r[4]) == 2 for r in recs], dtype=float)
    frac = np.array([r[2] for r in recs])
    mid = np.array([r[3] for r in recs])
    out["windows_seen"] = int(len(np.unique(cl)))
    out["windows_with_orders"] = int(len({x["row"]["window"] for x in xs} & set(cl.tolist())))
    out["presence_rate"] = describe(pres, cl, np.mean)
    p = pres > 0
    out["both_sides_share_when_present"] = describe(both[p], cl[p], np.mean)
    for name, a in (("bid_only", "bid"), ("ask_only", "ask")):
        only = np.array([r[4] == {a} for r in recs], dtype=float)
        out[f"{name}_share_when_present"] = describe(only[p], cl[p], np.mean)

    def table(x: np.ndarray, edges: list[float]) -> list[dict]:
        rows = []
        for lo, hi in zip(edges, edges[1:]):
            m = (x >= lo) & (x < hi)
            if m.any():
                rows.append({"lo": lo, "hi": hi, "cells": int(m.sum()),
                             "presence": _r(pres[m].mean()),
                             "ci95": boot_ci(lambda i, m=m: pres[m][i].mean(), cl[m]),
                             "both_share": _r(both[m & p].mean()) if (m & p).any() else None})
        return rows

    out["by_time_to_close_frac"] = table(frac, [round(0.1 * k, 1) for k in range(10)] + [1.0001])
    out["by_mid_c"] = table(mid, [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100.01])
    return out


def _dead_bands(by_mid: list[dict], overall: float | None) -> tuple[float | None, float | None]:
    """(lo, hi): contiguous mid bands from 0 / from 100 with >= MIN_N cells and presence under a
    tenth of the overall rate. No fills seen there; not proof that it never quotes there."""
    if not overall:
        return None, None
    dead = lambda b: b["cells"] >= MIN_N and b["presence"] < 0.1 * overall  # noqa: E731
    lo = hi = None
    for b in by_mid:
        if not dead(b) or b["lo"] != (lo or 0):
            break
        lo = b["hi"]
    for b in reversed(by_mid):
        if not dead(b) or min(b["hi"], 100) != (hi or 100):
            break
        hi = b["lo"]
    return lo, hi


# ---------------------------------------------------------------- ladders
def fit_ladder(specs: list[dict], gap_s: float | None = None) -> dict:
    """Is this a ladder: several own quotes resting TOGETHER at distinct prices on one token and
    side? A cancel and its replacement also overlap, for the difference of two latencies, so an
    overlap counts only when it lasts ``floor_ms`` = max(LADDER_MIN_MS, LADDER_GAP_FRAC x the
    wallet's median re-post gap ``gap_s``). Lives are placement to ``rest_max_ms``, else
    ``rest_min_ms``. ``ladder_share`` = share of orders with such a partner; ``depth`` of an order
    = itself plus the distinct other prices it rested with that long. ``brief_overlap_share``
    counts every overlap, however short (the pre-floor definition).

    ``status``: ``ladder`` when the share's CI over market windows lies at or above LADDER_SHARE
    (two located orders that rested together are a fact, whatever went unseen);
    ``no_ladder_seen`` when the CI lies below it: a hedged answer, because only filled orders are
    visible and an unfilled level never shows; else ``unknown``."""
    xs = sorted((x for x in specs if x["kind"] == "order"), key=lambda x: (x["placed_ms"], x["id"]))
    if not xs:
        return {"n": 0, "status": "unknown"}
    floor = max(LADDER_MIN_MS, LADDER_GAP_FRAC * 1000 * (gap_s or 0.0))
    live: dict[tuple, list[tuple[int, float, int]]] = defaultdict(list)  # (end_ms, price, index)
    partners: list[set[float]] = [set() for _ in xs]
    brief = np.zeros(len(xs))
    for i, x in enumerate(xs):
        o = x["om"]
        rest = o.get("rest_max_ms")
        rest = (o.get("rest_min_ms") or 0) if rest is None else rest
        end = x["placed_ms"] + rest
        key = (x["token"], x["side"])
        live[key] = [a for a in live[key] if a[0] > x["placed_ms"]]
        for a_end, a_px, j in live[key]:
            if abs(a_px - x["price"]) > 1e-9:
                brief[i] = brief[j] = 1
                if min(a_end, end) - x["placed_ms"] >= floor:
                    partners[i].add(a_px)
                    partners[j].add(x["price"])
        live[key].append((end, x["price"], i))
    depth = np.array([1 + len(p) for p in partners], dtype=float)
    laddered = (depth > 1).astype(float)
    cl = np.array([x["row"]["window"] for x in xs], dtype=object).astype(str)
    share = describe(laddered, cl, np.mean)
    ci = share["ci95"]
    status = "unknown"
    if len(xs) >= MIN_N and ci is not None:
        status = "ladder" if ci[0] >= LADDER_SHARE else "no_ladder_seen" if ci[1] < LADDER_SHARE else "unknown"
    on = laddered > 0
    return {"n": len(xs), "status": status, "floor_ms": _r(floor), "ladder_share": share,
            "brief_overlap_share": _r(brief.mean()),
            "depth_when_laddered": describe(depth[on], cl[on], np.mean) if on.any() else None,
            "depth": _quant(depth), "depth_max": int(depth.max()),
            "bias": "fill-conditional lower bound: an unfilled quote at another level is invisible, so the share "
                    "and the depth are lower bounds and no_ladder_seen is not proof of absence"}


# ---------------------------------------------------------------- explained share
def _calls(x: np.ndarray, sign: float) -> np.ndarray:
    """1 / 0 where ``sign * x`` is positive / negative, -1 (no call, always wrong) where x is 0 or unknown."""
    return np.where(np.isfinite(x) & (x != 0), (sign * np.nan_to_num(x) > 0).astype(float), -1.0)


def _ba(pred: np.ndarray, y: np.ndarray) -> float:
    """Balanced accuracy: the mean of the two classes' recalls; NaN unless both classes are present."""
    r = [float((pred[y == c] == c).mean()) for c in (0.0, 1.0) if (y == c).any()]
    return float(np.mean(r)) if len(r) == 2 else math.nan


def _best_call(X: dict[str, np.ndarray], y: np.ndarray, mask: np.ndarray) -> tuple[float, str, float] | None:
    """(balanced accuracy, predictor, sign) of the best one-predictor sign rule on the rows of ``mask``."""
    best = None
    for name, x in X.items():
        for sign in (1.0, -1.0):
            v = _ba(_calls(x[mask], sign), y[mask])
            if v == v and (best is None or v > best[0]):
                best = (v, name, sign)
    return best


def _signed(rows: list[dict]) -> dict[str, np.ndarray]:
    """What a side rule may look at: where the mid and fair-v1 stand (state columns only)."""
    mid, fair = _arr(rows, "mid_c"), _arr(rows, "fair_c")
    X = {"mid_minus_50": mid - 50, "fair_minus_mid": fair - mid, "fair_minus_50": fair - 50}
    return {k: v for k, v in X.items() if np.isfinite(v).any()}


def side_test(rows: list[dict], th: tuple[int, int]) -> dict:
    """Does anything DOPPEL records say which SIDE this wallet quotes?

    The unit is the market window. A window with >= SIDE_MIN_ORDERS visible orders shows the sides
    quoted in it. ``two_sided`` (both sides in most such windows): exempt, the side is not a
    choice. ``one_sided_constant`` (always the same side): nothing to explain, ``sides`` holds it.
    Otherwise the side changes from window to window and a rule has to predict it: the best
    one-predictor sign rule (mid - 50, fair - mid, fair - 50, at the window's FIRST visible order:
    later books move with the outcome) is chosen on the fit windows and scored on the later ones.
    ``explained`` = its balanced accuracy's exact interval (the mean of the two classes'
    Clopper-Pearson limits, each at 97.5 %) lies above 0.5."""
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["window"]].append(r)
    wins = [{"first": min(rs, key=lambda r: r["cutoff_tw_ns"]), "sides": {r["up_side"] for r in rs}}
            for rs in by.values() if len(rs) >= SIDE_MIN_ORDERS]
    out: dict = {"windows": len(by), "windows_judged": len(wins), "min_orders_per_window": SIDE_MIN_ORDERS,
                 "null": "balanced accuracy 0.5"}
    if len(wins) < 3:
        return out | {"kind": "undetermined", "explained": None,
                      "reason": f"fewer than 3 windows with >= {SIDE_MIN_ORDERS} visible orders"}
    one = sorted((w for w in wins if len(w["sides"]) == 1), key=lambda w: w["first"]["window_start_ns"])
    out["one_sided_window_share"] = _r(len(one) / len(wins))
    if 2 * len(one) < len(wins):
        return out | {"kind": "two_sided", "explained": None, "reason": "both sides quoted in most windows: exempt"}
    y = np.array([w["sides"] == {"bid"} for w in one], dtype=float)
    out["bid_window_share"] = _r(y.mean())
    if y.all() or not y.any():
        return out | {"kind": "one_sided_constant", "explained": True, "reason": "always the same side"}
    first = [w["first"] for w in one]
    X = _signed(first)
    fit = np.array([r["window_start_ns"] < th[0] for r in first])
    out |= {"kind": "one_sided_by_window", "explained": False, "n_fit": int(fit.sum()), "n_held_out": int((~fit).sum())}
    best = _best_call(X, y, fit)
    if best is None:
        return out | {"reason": "the fit windows do not hold both sides, or no predictor is known"}
    _, name, sign = best
    pred, yh = _calls(X[name][~fit], sign), y[~fit]
    n1, n0 = int((yh == 1).sum()), int((yh == 0).sum())
    out |= {"predictor": name, "sign": sign, "fit_balanced_accuracy": _r(best[0]), "held_out_per_side": [n1, n0]}
    if not n1 or not n0:
        return out | {"reason": "the held-out windows do not hold both sides"}
    k1, k0 = int((pred[yh == 1] == 1).sum()), int((pred[yh == 0] == 0).sum())
    a, b = binom_ci(k1, n1, 0.025), binom_ci(k0, n0, 0.025)
    ci = [round((a[0] + b[0]) / 2, 4), round((a[1] + b[1]) / 2, 4)]
    ok = ci[0] > 0.5
    return out | {"balanced_accuracy": _r((k1 / n1 + k0 / n0) / 2), "ci95": ci, "explained": bool(ok),
                  "reason": None if ok else "the interval of the held-out balanced accuracy includes chance (0.5)"}


def presence_skill(T: dict, th: tuple[int, int]) -> dict:
    """How much of WHEN the wallet is in the book a rule of the genome's kind explains.

    The rule (a span of the window, from tau_min to tte_min, and dead mid bands) is fitted on the
    fit windows; on the later windows its presence probabilities are scored against the null "the
    same presence rate in every cell" (always present, when that rate is ~1). Skill = 1 -
    log-loss(rule) / log-loss(null). Presence means a FILLED order rested in the cell, so this is
    reported and does not gate a maker: absence of a fill is not absence of a quote."""
    recs = presence_cells(T)
    null = "one presence rate for every cell (the fit windows' rate)"
    if not recs:
        return est(None, 0) | {"null": null, "reason": "no grid cell with a known book"}
    start = np.array([r[1] for r in recs], dtype=np.int64) * NS
    fit = start < th[0]
    y = np.array([bool(r[4]) for r in recs], dtype=float)
    frac, mid = np.array([r[2] for r in recs]), np.array([r[3] for r in recs])
    cl = np.array([r[0] for r in recs], dtype=str)
    if fit.all() or not (fit & (y > 0)).any():
        return est(None, 0) | {"null": null, "reason": "no held-out window, or no presence in the fit windows"}
    lo, hi = np.percentile(frac[fit & (y > 0)], [1, 99])
    band = np.minimum(mid // 10, 9).astype(int)
    base = float(np.clip(y[fit].mean(), 0.01, 0.99))
    dead = [b for b in range(10) if (m := fit & (band == b)).sum() >= MIN_N and y[m].mean() < 0.1 * base]
    on = (frac >= lo) & (frac <= hi) & ~np.isin(band, dead)
    rate = {k: float(np.clip(y[fit & (on == k)].mean(), 0.01, 0.99)) if (fit & (on == k)).any() else base
            for k in (True, False)}
    p_rule = np.where(on, rate[True], rate[False])
    loss = lambda q: -(y * np.log(q) + (1 - y) * np.log(1 - q))  # noqa: E731
    return _skill(loss(p_rule)[~fit], loss(np.full(len(y), base))[~fit], cl[~fit]) | {
        "null": null, "rule": {"window_share_left": [_r(hi), _r(lo)], "dead_mid_bands_c": [[10 * b, 10 * b + 10] for b in dead],
                               "presence_when_on": _r(rate[True]), "presence_when_off": _r(rate[False])},
        "presence_rate_held_out": _r(y[~fit].mean()),
        "bias": "presence is a FILLED order resting in the cell: fill-conditional"}


# ---------------------------------------------------------------- takers
def fit_taker(T: dict) -> dict:
    """A wallet's TAKER fills: is there a rule for WHEN it takes and WHICH side?

    Unit: the GRID_S cells of its windows with a known book at the cell's start (state before the
    cell, no look-ahead). Windows are split in time as for the price fits (fit / validation / test).
    - when: the count of takes per cell against the null "one rate for every cell" (a Poisson
      stream, which is what a random taker is). Three one-feature rate tables compete: time to
      close (fifths of the window), price band (|mid - 50|) and edge vs fair (|fair - mid|, when
      fair-v1 is supplied). Rates are fitted on the fit windows, the table is chosen on the
      validation windows, refitted on both and scored once on the test windows: skill = 1 -
      Poisson deviance(rule) / deviance(null), CI over windows. ``explained`` = the CI lies above
      SKILL_MIN.
    - which side (Up exposure bought or sold): the best one-predictor sign rule (fair - mid,
      mid - 50, fair - 50 at the cell's start) chosen on the fit windows, balanced accuracy on the
      later windows with a CI over windows. ``explained`` = the CI lies above 0.5, or the wallet
      always takes the same side.
    ``found`` = both explained. Nothing here is a genome: rulefit emits maker drafts only."""
    takes, cells = T["takes"], grid_cells(T)
    out: dict = {"found": False, "n_takes": len(takes), "located_by_print": sum(t["located"] for t in takes),
                 "cells": len(cells), "cell_s": GRID_S}
    if len(takes) < MIN_N or not cells:
        return out | {"reason": f"fewer than {MIN_N} taker fills in mapped windows, or no cell with a known book"}
    win = {i["slug"]: i for i in T["tok"].values()}
    start = np.array([c[1] for c in cells], dtype=np.int64) * NS
    th = thresholds([{"window_start_ns": int(v)} for v in start])
    ph = np.select([start < th[0], start < th[1]], [0, 1], default=2)
    if not all((ph == k).any() for k in (0, 1, 2)):
        return out | {"reason": "fewer than 3 market windows: no fit / validation / test split"}
    cl = np.array([c[0] for c in cells], dtype=str)
    frac, mid = np.array([c[2] for c in cells]), np.array([c[3] for c in cells])
    fair = np.array([math.nan if T["fair"] is None or (f := T["fair"].at(win[c[0]], c[4] * NS)) is None else f
                     for c in cells])
    at = {(c[0], c[4]): i for i, c in enumerate(cells)}
    y = np.zeros(len(cells))
    t_cell, t_up = [], []
    for t in takes:
        b = t["tw"] // NS
        i = at.get((t["slug"], b - b % GRID_S))
        if i is not None:
            y[i] += 1
            t_cell.append(i)
            t_up.append(float((t["side"] == "BUY") == (t["idx"] == 0)))
    out["takes_in_known_cells"] = len(t_cell)
    bins = {"time_to_close": np.minimum((frac * 5).astype(int), 4), "price_band": np.digitize(np.abs(mid - 50), [5, 10, 20, 30])}
    if np.isfinite(fair).mean() >= 0.5:
        bins["edge_vs_fair"] = np.where(np.isfinite(fair), np.digitize(np.abs(fair - mid), [1, 2, 4]), 4)

    def rates(b: np.ndarray, m: np.ndarray) -> tuple[np.ndarray, float]:
        lam0 = max(float(y[m].mean()), 1e-6)
        lam = {k: (y[m & (b == k)].sum() + lam0) / ((m & (b == k)).sum() + 1) for k in np.unique(b)}  # shrunk to lam0
        return np.array([lam[k] for k in b]), lam0

    def dev(lam: np.ndarray) -> np.ndarray:
        return 2 * (np.where(y > 0, y * np.log(np.maximum(y, 1e-12) / lam), 0.0) - (y - lam))

    val = {k: float(dev(rates(b, ph == 0)[0])[ph == 1].sum()) for k, b in bins.items()}
    best = min(val, key=lambda k: (val[k], list(bins).index(k)))
    lam, lam0 = rates(bins[best], ph <= 1)
    te = ph == 2
    when = _skill(dev(lam)[te], dev(np.full(len(y), lam0))[te], cl[te]) | {
        "feature": best, "validation_deviance": {k: _r(v) for k, v in val.items()},
        "null": "one rate of takes for every cell (a Poisson stream)", "takes_per_cell": _r(lam0),
        "rate_by_bin": {str(int(k)): _r(lam[bins[best] == k][0]) for k in np.unique(bins[best])}, "skill_min": SKILL_MIN}
    when["explained"] = bool(when["ci95"] and when["ci95"][0] > SKILL_MIN)
    ci_ = np.array(t_cell, dtype=int)
    up = np.array(t_up)
    side: dict = {"n": len(up), "up_exposure_bought_share": _r(up.mean()) if len(up) else None, "null": "balanced accuracy 0.5"}
    if len(up) and (up.all() or not up.any()):
        side |= {"explained": True, "reason": "always the same side"}
    else:
        X = {"fair_minus_mid": fair[ci_] - mid[ci_], "mid_minus_50": mid[ci_] - 50, "fair_minus_50": fair[ci_] - 50}
        X = {k: v for k, v in X.items() if np.isfinite(v).any()}
        fitm = ph[ci_] == 0
        pick = _best_call(X, up, fitm)
        side["explained"] = False
        if pick is None or not (~fitm).any():
            side["reason"] = "the fit windows do not hold both sides, or no later window"
        else:
            pred, yh, ch = _calls(X[pick[1]][~fitm], pick[2]), up[~fitm], cl[ci_][~fitm]
            ci = boot_ci(lambda i: _ba(pred[i], yh[i]), ch)
            side |= {"predictor": pick[1], "sign": pick[2], "fit_balanced_accuracy": _r(pick[0]),
                     "balanced_accuracy": _r(_ba(pred, yh)), "ci95": ci, "explained": bool(ci and ci[0] > 0.5)}
    found = when["explained"] and side["explained"]
    return out | {"found": bool(found), "when": when, "side": side,
                  "reason": None if found else "no rule found: " + "; ".join(
                      f"{k} is not explained" for k, v in (("when it takes", when), ("which side", side))
                      if not v["explained"])}


def taker_report(T: dict, wallet: str) -> dict:
    """The fit report of a wallet with no located maker order (its taker fills, if any, are tested)."""
    tk = fit_taker(T)
    why = ["no maker order of this wallet was located: rulefit emits maker drafts only",
           tk["reason"] or "a taker rule was found; taker genomes are not emitted by rulefit v1"]
    return {"wallet": wallet, "label": "taker fills: no maker order located", "selection_caveat": CAVEAT,
            "inputs": T["inputs"] | {"counts": dict(T["stats"]), "rows_orders": 0, "rows_candidates": 0, "pairs": T["pairs"]},
            "taker_rule": tk,
            "explained_by_observed_inputs": {"verdict": tk["found"], "side": tk.get("side"), "timing": tk.get("when"),
                                             "price": None, "reasons": [] if tk["found"] else [tk["reason"]]},
            "genome": {"id": None, "status": "not_emitted", "registered": False, "reasons": why,
                       "reason": "; ".join(why)}}


def taker_card(rep: dict) -> str:
    tk = rep["taker_rule"]
    L = [f"# rule fit {rep['wallet']}  [TAKER FILLS, NO MAKER ORDER]", "",
         f"taker fills {tk['n_takes']} ({tk['located_by_print']} timed by a print), cells {tk['cells']} x {tk['cell_s']} s", "",
         "## RULE FOUND (not emitted)" if tk["found"] else "## NO RULE FOUND", f"- {tk['reason'] or 'when and side both explained'}"]
    for k in ("when", "side"):
        v = tk.get(k)
        if v:
            L.append(f"- {k}: explained {v['explained']}; " + ", ".join(
                f"{a} {v[a]}" for a in ("feature", "value", "predictor", "balanced_accuracy", "ci95", "null") if v.get(a) is not None))
    return "\n".join(L + ["", "## NO GENOME EMITTED"] + [f"- {x}" for x in rep["genome"]["reasons"]]) + "\n"


# ---------------------------------------------------------------- selection-bias check
def selection_check(rows: list[dict], price: dict, precision: float | None) -> dict | None:
    cand = [r for r in rows if r["kind"] == "candidate"]
    if not cand:
        return None
    if precision is None:
        return {"n_candidates": len(cand), "reason": "no identity precision stated (ordermatch.json "
                "fingerprint.total.identity_precision_upper or --identity-precision): not refitted"}
    pooled = [r if r["kind"] == "order" else r | {"w": precision} for r in rows]
    th = thresholds(pooled)
    out: dict = {"n_candidates": len(cand), "identity_precision": precision,
                 "weights": "verified orders 1, fingerprint candidates the identity precision (an upper bound)",
                 "reading": "IF THE ATTRIBUTION HELD: what the rule would be were the candidates this wallet's "
                            "quotes at the stated precision. Not a bound on the selection bias: the candidates "
                            "are not identified, other makers post the same sizes, and the sample is capped",
                 "sides": {}}
    for s in ("bid", "ask"):
        v = price[s]
        D = _side_data(pooled, s, th)
        free = fit_price_side(D)
        doc: dict = {"n_candidates": sum(r["up_side"] == s for r in cand), "verified_ref": v.get("chosen"),
                     "pooled_ref": free.get("chosen"), "pooled_fit": free}
        if v.get("chosen"):
            same = fit_price_side(D, force=v["chosen"])
            if same.get("chosen"):
                a, b = v["offset_c"]["value"], same["offset_c"]["value"]
                ka, kb = v["skew_c_per_contract"]["value"], same["skew_c_per_contract"]["value"]
                doc["at_verified_ref"] = {
                    "offset_c": {"verified": a, "pooled": b, "move": _r(b - a), "pooled_ci95": same["offset_c"]["ci95"]},
                    "skew_c_per_contract": {"verified": ka, "pooled": kb, "move": _r(kb - ka),
                                            "pooled_ci95": same["skew_c_per_contract"]["ci95"]},
                    "within_1c": {"verified": v["within_1c_of_const_rule"]["value"],
                                  "pooled": same["within_1c_of_const_rule"]["value"]}}
        doc["ref_changed"] = bool(v.get("chosen")) and free.get("chosen") not in (None, v.get("chosen"))
        out["sides"][s] = doc
    return out


# ---------------------------------------------------------------- genome + fit report
def build(T: dict, wallet: str, precision: float | None = None) -> tuple[dict | None, dict]:
    """(draft genome dict, or None with the reasons in ``report["genome"]``; fit report)."""
    rows = T["rows"]
    ver = [r for r in rows if r["kind"] == "order"]
    th = thresholds(ver)
    price = fit_price(ver, th)
    expr = fit_expressible(ver, price, th)
    size = fit_size(T["specs"])
    timing = fit_timing(T)
    ladder = fit_ladder(T["specs"], timing["gap_s"]["value"])
    part = fit_participation(T)
    side = side_test(ver, th)
    sel = selection_check(rows, price, precision)
    fit_rows = [r for r in ver if r["asset"] in expr["assets"]]  # the rows the genome is about

    # ---- genome fields, each with where it came from
    prov: dict = {}
    hzs = expr["horizons"]
    L = min((LEN[h] for h in hzs), default=300)
    tte_q, tau_q = part["tte_s_p1"]["value"], part["tau_s_p1"]["value"]
    tte_min = int(min(max(math.floor(tte_q), 0), L - 1)) if tte_q is not None else 0
    tau_min = int(min(max(math.floor(tau_q), 0), L - 1)) if tau_q is not None else 0
    prov["universe.tte_min_s"] = "floor(participation_rule.tte_s_p1)" if tte_q is not None else "default"
    prov["universe.tau_min_s"] = "floor(participation_rule.tau_s_p1)" if tau_q is not None else "default"
    prov["universe.assets/horizons"] = "assets with fair-v1 that are in the fit (expressible_price_rule.assets)"
    med = size.get("median_size", {}).get("value") if size.get("n") else None
    life = (timing.get("cancel") or {}).get("life_ms", {}).get("quantiles")
    p90 = ((timing.get("cancel") or {}).get("life_ms_p90") or {}).get("value")
    age = max(1, math.ceil(p90 / 1000)) if p90 is not None else 30
    prov["maker.max_quote_age_s"] = ("ceil(timing_rule.cancel.life_ms_p90 / 1000), floor 1 s ("
                                     + timing["cancel"]["life_ms_p90"]["method"] + ")" if p90 is not None
                                     else "default 30: no cancelled order with a known life")
    lo, hi = _dead_bands(part.get("by_mid_c") or [], (part.get("presence_rate") or {}).get("value"))
    pause = tuple(c for c in (Condition("mid_up_c", "<", lo) if lo else None,
                              Condition("mid_up_c", ">", hi) if hi else None) if c is not None)
    prov["maker.pause_when"] = ("mid bands with >= 20 cells and presence under a tenth of the overall rate"
                                if pause else "none found")
    edge = edge_fit = None
    fr = sorted((r for r in fit_rows if r["d_fair_c"] is not None), key=lambda r: r["cutoff_tw_ns"])
    if len(fr) >= MIN_N:
        df = np.array([(-1 if r["up_side"] == "bid" else 1) * r["d_fair_c"] for r in fr])
        edge_fit = describe(df, np.array([r["window"] for r in fr], dtype=str), lambda v: np.percentile(v, 5))
        edge = max(0.0, round(edge_fit["value"], 2))
        prov["maker.min_edge_vs_fair_c"] = "max(0, edge_outside_fair_c_p5): p5 of the quote's distance outside fair"
    else:
        prov["maker.min_edge_vs_fair_c"] = "default 0: fair not supplied or known for < 20 orders"
    seen = set(expr.get("sides_seen") or [])
    sides = "both" if seen == {"bid", "ask"} or not seen else ("bid_only" if seen == {"bid"} else "ask_only")
    prov["maker.quote_ref/half_spread_c/skew_c_per_contract"] = (
        "expressible_price_rule: reference and keep-skew chosen on the validation slice; half-spread = half the "
        "gap between the per-side median offsets at zero inventory (half_spread_fit, clamped at 0); one "
        "inventory slope with side intercepts; refitted on all rows" if expr.get("quote_ref")
        else "defaults: " + str(expr.get("reason")))
    prov["maker.requote_threshold_c"] = "default 1.0 (the trigger threshold used to classify book moves)"
    prov["maker.size"] = "size_rule.median_size" if med else "default"
    # risk envelope from the wallet's own measured state: the emitted rule must be able to quote
    ai = np.abs(np.array([r["inv"] for r in fit_rows if r["inv"] is not None], dtype=float))
    wn = np.array(T["inv"].window_notional({r["window"] for r in fit_rows}), dtype=float)
    p99 = float(np.percentile(ai, 99)) if len(ai) else 0.0
    n95 = float(np.percentile(wn, 95)) if len(wn) else 0.0
    base = Risk()
    risk = Risk(max_inventory=float(max(math.ceil(max(med or 0.0, p99)), 1)),
                max_notional_per_window=float(max(math.ceil(n95), 1)),
                max_loss_per_day=base.max_loss_per_day, max_open_windows=base.max_open_windows)
    risk_fit = {"max_inventory": risk.max_inventory, "max_notional_per_window": risk.max_notional_per_window,
                "fitted_size": med, "abs_inventory_at_placement": (
                    {f"p{q}": _r(np.percentile(ai, q)) for q in (50, 90, 99, 100)} if len(ai) else None),
                "window_opening_notional_usd": ({f"p{q}": _r(np.percentile(wn, q)) for q in (50, 95, 100)}
                                                | {"windows": int(len(wn))} if len(wn) else None),
                "note": "MEASURED from this wallet, not a DOPPEL risk limit: max_inventory = ceil(max(fitted size, "
                        "p99 |inventory at placement|)), max_notional_per_window = ceil(p95 of its opening notional "
                        "per window, accrued as in the twin). Set the real envelope before any run with money"}
    prov["risk.max_inventory/max_notional_per_window"] = "fit_report.risk_fit (measured from the wallet)"
    prov["risk.max_loss_per_day/max_open_windows, costs, kill, exit"] = "schema defaults, not fitted"
    not_fitted = {"costs": "schema default (PM US taker coefficient); intl fees are not fitted",
                  "kill_exit": "schema defaults", "risk": "max_loss_per_day and max_open_windows are schema defaults",
                  "requote_threshold_c": "default 1.0, the threshold used to classify book moves"}
    maker = MakerRule(quote_ref=expr.get("quote_ref") or "mid",
                      half_spread_c=float(expr["half_spread_c"]) if expr.get("quote_ref") else 2.0,
                      size=float(med) if med else 10.0, sides=sides,  # type: ignore[arg-type]
                      skew_c_per_contract=expr.get("skew_c_per_contract") or 0.0,
                      requote_threshold_c=MOVE_C, max_quote_age_s=age, pause_when=pause,
                      min_edge_vs_fair_c=edge or 0.0)
    g = Genome(name=f"rulefit-{wallet[:10]}-maker", mode="maker",
               universe=Universe(venue="intl_sim", assets=tuple(expr["assets"]) or ("btc",),
                                 horizons=tuple(hzs) or ("5m",), tau_min_s=tau_min, tte_min_s=tte_min),
               source=Source(kind="clone", wallets=(wallet,), mechanism="rule conditional on fill",
                             parents=(), operator="extract"),
               maker=maker, risk=risk, created_tw=0,
               evidence={"extractor": "doppel.lab.rulefit v1", "wallet": wallet, "label": "rule conditional on fill",
                         "fit_range": {"from": T["inputs"]["from"], "to": T["inputs"]["to"]},
                         "n_orders": len(ver), "fields": prov,
                         "price_rule": {s: {k: price[s].get(k) for k in ("chosen", "model", "tie_with", "n", "rule")}
                                        | {"oos_mae_c": ((price[s].get("offset_c") or {}).get("oos") or {}).get("mae_c")}
                                        for s in ("bid", "ask")},
                         "expressible": {k: expr.get(k) for k in ("quote_ref", "oos_mae_c", "per_side_fit_oos_mae_c",
                                                                  "oos_cost_of_schema_c", "centre_shift_c")}})
    errs = validate(g)
    gd = to_dict(g) | {"status": "draft"}

    # ---- sanity-check the full-sample draft on the observations it describes
    check = check_genome(g, fit_rows, T["inputs"]["fair"]) if expr.get("quote_ref") else None
    uni = [not (r["tau_s"] >= tau_min and r["tte_s"] >= tte_min) for r in fit_rows]
    universe = {"tau_min_s": tau_min, "tte_min_s": tte_min, "orders": len(uni),
                "own_orders_excluded_share": _r(np.mean(uni)) if uni else None,
                "pre_open_share": _r(np.mean([r["tau_s"] < 0 for r in fit_rows])) if fit_rows else None}

    # ---- reasons not to emit: a genome that cannot act like the fit must not carry the wallet's name
    blockers: list[str] = []
    n_windows = len({r["window"] for r in fit_rows})
    if n_windows == 1:
        blockers.append("1 market window in the fit: no clustered CI, need >= 2")
    if not expr.get("quote_ref"):
        blockers.append(str(expr.get("reason")))
    else:
        if expr["half_spread_c"] < MIN_HALF_SPREAD_C:
            blockers.append(f"half_spread_c {expr['half_spread_c']} < {MIN_HALF_SPREAD_C}: bid and ask round to the "
                            "same cent, the twin drops both as a self-cross")
        if expr["windows"] < 2 and n_windows != 1:
            blockers.append(f"{expr['windows']} market window in the fit: no clustered CI, need >= 2")
        if check is not None and (check["would_quote_share"] or 0) < MIN_QUOTE_SHARE:
            blockers.append(f"the emitted genome would quote {check['would_quote_share']} of the wallet's in-sample "
                            f"orders (< {MIN_QUOTE_SHARE}): silent by {check['silent_by']}")
    # ---- explained share: a rule that does not explain the wallet's side or price must not carry its name
    psk = expr.get("price_skill")
    price_ok = bool(psk and psk["value"] is not None and psk["value"] > 0 and (psk["ci95"] is None or psk["ci95"][0] > 0))
    why_not = []
    if side["explained"] is False:
        why_not.append(f"side: {side['kind']}, {side['reason']} (balanced accuracy {side.get('balanced_accuracy')}, "
                       f"interval {side.get('ci95')})")
    if not price_ok:
        why_not.append("price: the expressible rule does not beat a constant price on the test windows "
                       f"(skill {psk and psk['value']}, CI {psk and psk['ci95']})" if psk else f"price: {expr.get('reason')}")
    explained = {"verdict": not why_not, "reasons": why_not, "side": side,
                 "price": (psk or {}) | {"explained": price_ok}, "timing": presence_skill(T, th),
                 "rule": "verdict = the price rule beats a constant price out of sample AND the side is explained "
                         "(a wallet that quotes both sides, or always the same one, is exempt from the side test); "
                         "timing is reported and does not gate a maker"}
    blockers += [f"not explained by observed inputs: {w}" for w in why_not if not w.startswith("price: None")]
    blockers += [f"validate: {e}" for e in errs]

    # ---- what the v1 schema cannot hold, each with its number
    cx: list[dict] = []

    def cannot(item: str, measured, why: str) -> None:
        cx.append({"item": item, "measured": measured, "why": why})

    lad = (ladder.get("ladder_share") or {}).get("value") or 0
    is_ladder = ladder.get("status") == "ladder"
    for s in ("bid", "ask"):
        v = price[s]
        if v.get("chosen") == "touch":
            cannot(f"{s}: quote pegged to the same-side best", {
                "oos_mae_c_touch": v["offset_c"]["oos"]["mae_c"], "offset_c": v["offset_c"]["value"],
                "tie_with": v["tie_with"], "val_mae_c_by_ref": {r: min(c["val_mae_const_c"], c["val_mae_skew_c"])
                                                                for r, c in v["candidates"].items()}},
                   "MakerRule.quote_ref is fair|mid only")
        if v.get("chosen") and (is_ladder or (v["residual_iqr_c"] or 0) >= 2):
            cannot(f"{s}: several price levels", {
                "ladder_status": ladder.get("status"), "ladder_share": lad, "depth_max": ladder.get("depth_max"),
                "residual_iqr_c": v["residual_iqr_c"]} | {k: v["offset_c"][k] for k in ("p25", "value", "p75")},
                   "one quote per side at one half-spread; flagged when the ladder status is 'ladder' (sustained "
                   f"overlaps at distinct prices, share CI >= {LADDER_SHARE}) or the IQR of the rule's residual >= 2 c")
        for key, what in (("slope_c_per_s_to_close", "time to close"), ("slope_c_per_c_spread", "spread")):
            if _excludes_zero(v.get(key)):
                cannot(f"{s}: offset depends on {what}", v[key], "half_spread_c is a constant (or logit in price)")
    b, a = price["bid"], price["ask"]
    if b.get("chosen") and a.get("chosen"):
        if b["chosen"] != a["chosen"]:
            cannot("a different reference per side", {"bid": b["chosen"], "ask": a["chosen"]}, "one quote_ref")
        if (b["model"] == "skew") != (a["model"] == "skew") or (
                _excludes_zero(b["skew_c_per_contract"]) and _excludes_zero(a["skew_c_per_contract"])
                and (b["skew_c_per_contract"]["ci95"][1] < a["skew_c_per_contract"]["ci95"][0]
                     or a["skew_c_per_contract"]["ci95"][1] < b["skew_c_per_contract"]["ci95"][0])):
            cannot("inventory skew differs by side", {"bid": b["rule"], "ask": a["rule"]},
                   "one skew_c_per_contract for both quotes")
    if expr.get("quote_ref"):
        if expr["half_spread_fit"]["clamped_to_zero"]:
            cannot("quotes inside the reference (negative half-spread)", expr["half_spread_fit"],
                   "half_spread_c >= 0 is required; emitted as 0")
        if abs(expr.get("centre_shift_c") or 0) >= 0.5:
            cannot("asymmetric offsets around the reference", {"centre_shift_c": expr["centre_shift_c"]},
                   "bid/ask = ref -/+ one half_spread_c")
        cannot("cost of the schema on the price rule", {k: expr.get(k) for k in (
            "oos_mae_c_same_rows", "per_side_fit_oos_mae_c", "oos_cost_of_schema_c", "n_test_common")},
               "test MAE of the expressible rule vs the per-side rules, on the same test rows")
        if expr["quote_ref"] == "mid" and check is not None:
            cannot("mid-referenced quote still bound to fair", {
                "fair_clamp_binds_share": check["fair_clamp_binds_share"],
                "fair_substituted_by_mid": check["fair_substituted_by_mid"], "silent_no_fair": check["silent_by"].get("no_fair", 0)},
                   "Genome.maker_quotes returns no quote when fair_up is missing and clamps every quote to "
                   "fair -/+ min_edge_vs_fair_c, whatever quote_ref is")
    if expr.get("rows_dropped_no_fair_model"):
        cannot("assets without fair-v1", {"rows_dropped": expr["rows_dropped_no_fair_model"],
                                          "assets": sorted({r["asset"] for r in ver} - set(expr["assets"]))},
               "a v1 maker never quotes without fair_up; these orders are in the per-side rules only")
    gs = timing.get("gap_s") or {}
    if gs.get("value") is not None and gs["value"] < 1:
        cannot("sub-second re-post", {k: gs.get(k) for k in ("value", "ci95", "status", "method")},
               "decisions are per second; max_quote_age_s is an int >= 1")
    if life and life["p50"] < 1000:
        cannot("sub-second quote life", {"cancel_life_ms": life}, "max_quote_age_s is whole seconds")
    ts = timing.get("trigger_shares") or {}
    if ts and (ts["own_fill"]["value"] or 0) > 0:
        cannot("re-post triggered by an own fill", {k: v["value"] for k, v in ts.items()},
               "requote happens on a ref move (requote_threshold_c) or age only")
    d = size.get("descriptor") or {}
    if d.get("n_sizes_90", 1) > 1:
        cannot("size drawn from a set / randomised", {"rule": d.get("rule"), "sizes_90": d.get("sizes_90"),
                                                       "cv_size": d.get("cv_size")}, "MakerRule.size is one number")
    if _excludes_zero(size.get("size_per_c_of_price")):
        cannot("size depends on price", size["size_per_c_of_price"], "MakerRule.size is one number")
    if d.get("tick_grid") == "subcent":
        cannot("sub-cent prices", {"cent_share": d.get("cent_share")}, "maker_quotes rounds to whole cents")
    bs = part.get("both_sides_share_when_present")
    if bs and bs["value"] is not None and bs["value"] < 0.9:
        cannot("one-sided quoting by condition", {"both_sides_share_when_present": bs["value"],
                                                  "by_mid_c": part.get("by_mid_c")},
               "MakerRule.sides is fixed (GENOME.md: side selection by condition is v2)")
    if (part.get("pre_open_order_share") or 0) > 0:
        cannot("quoting before the window opens", {"share_of_orders": part["pre_open_order_share"]},
               "the universe starts at tau_min_s >= 0")
    if len(hzs) > 1:
        cannot("different parameters per horizon", {"horizons": hzs}, "one MakerRule per genome")
    cannot("status draft", "draft", "Status has no 'draft'; the JSON carries it and Registry.register refuses it")
    cannot("unfilled quotes", CAVEAT, "not a schema gap: the data cannot show them")

    emitted = not blockers
    report = {"wallet": wallet, "label": "rule conditional on fill", "selection_caveat": CAVEAT,
              "definitions": "see doppel.lab.rulefit module docstring",
              "inputs": T["inputs"] | {"counts": dict(T["stats"]), "rows_orders": len(ver),
                                       "rows_candidates": len(rows) - len(ver), "pairs": T["pairs"],
                                       "split": {"t_val_ns": th[0], "t_test_ns": th[1], "unit": "market window"}},
              "price_rule": price, "expressible_price_rule": expr, "size_rule": size, "timing_rule": timing,
              "ladder": ladder, "participation_rule": part, "selection_check": sel,
              "explained_by_observed_inputs": explained,
              "edge_outside_fair_c_p5": edge_fit, "risk_fit": risk_fit, "universe_fit": universe,
              "emitted_genome_check": check,
              "genome": {"id": g.id if emitted else None, "status": "draft" if emitted else "not_emitted",
                         "registered": False, "validate": errs, "fields": prov, "reasons": blockers,
                         "reason": "; ".join(blockers) or None,
                         "few_windows": bool(expr.get("windows") and expr["windows"] < MIN_N),
                         "windows": expr.get("windows")},
              "not_fitted": not_fitted, "cannot_express": cx}
    return (gd if emitted else None), report


def card(rep: dict) -> str:
    L = [f"# rule fit {rep['wallet']}  [RULE CONDITIONAL ON FILL]", "", rep["selection_caveat"], "",
         f"orders {rep['inputs']['rows_orders']}, candidates {rep['inputs']['rows_candidates']}, "
         f"counts {rep['inputs']['counts']}", "", "## price (Up cents)"]
    for s in ("bid", "ask"):
        v = rep["price_rule"][s]
        if not v.get("chosen"):
            L.append(f"- {s}: no rule ({v.get('reason')}; rows {v['n_rows']})")
            continue
        o, k, u = v["offset_c"], v["skew_c_per_contract"], v["rule"]
        L.append(f"- {s} = {u['ref']} {u['offset_c']:+g} c"
                 + (f" - {u['skew_c_per_contract']:g} c x inventory (CI {k['ci95']})" if v["model"] == "skew"
                    else f" (inventory skew not kept: {k['value']:g}, CI {k['ci95']})")
                 + f"; n={v['n']}, test MAE {o['oos']['mae_c']} c, within 1 c {o['oos']['within_1c']}"
                 + (f"; TIE with {v['tie_with']}" if v["tie_with"] else ""))
        L.append(f"  offset from {u['ref']}: median {o['value']:+g} c (CI {o['ci95']}), IQR {o['p25']:+g}..{o['p75']:+g}, "
                 f"residual IQR {v['residual_iqr_c']} c, {o['clusters']} windows")
    e = rep["expressible_price_rule"]
    L.append(f"- schema v1 can hold: ref {e.get('quote_ref')} +/- {e.get('half_spread_c')} c, skew "
             f"{e.get('skew_c_per_contract')}; test MAE {e.get('oos_mae_c_same_rows')} c vs per-side "
             f"{e.get('per_side_fit_oos_mae_c')} c on the same {e.get('n_test_common')} rows"
             + (f"; {e['rows_dropped_by_fair_requirement']} rows dropped for want of fair" if e.get("rows_dropped_by_fair_requirement") else "")
             + (f"; {e.get('reason')}" if not e.get("quote_ref") else ""))
    sz, tm, pt = rep["size_rule"], rep["timing_rule"], rep["participation_rule"]
    d = sz.get("descriptor") or {}
    L += ["", "## size", f"- {d.get('rule')}: sizes {d.get('sizes_90')}, median {sz.get('median_size', {}).get('value')}, "
          f"train set covers test {sz.get('train_size_set_covers_test', {}).get('value')}",
          "", "## timing",
          f"- re-post gap on a token (s): {tm['gap_s']['value']} [{tm['gap_s']['status']}, interval {tm['gap_s']['ci95']}, "
          f"{tm['gap_s']['method']}]; between FILLED orders (upper bound): {tm['gap_s'].get('quantiles')}",
          f"- preceded by: { {k: v['value'] for k, v in (tm.get('trigger_shares') or {}).items()} }",
          f"- cancel: {None if not tm.get('cancel') else {k: (v.get('value') if isinstance(v, dict) and 'value' in v else v) for k, v in tm['cancel'].items() if k != 'note'}}",
          "", "## participation", f"- windows with orders {pt.get('windows_with_orders')}/{pt.get('windows_seen')}, "
          f"cell presence {(pt.get('presence_rate') or {}).get('value')}, both sides when present "
          f"{(pt.get('both_sides_share_when_present') or {}).get('value')}, pre-open order share {pt.get('pre_open_order_share')}"]
    sel = rep["selection_check"]
    L += ["", "## selection refit (if the attribution held; not a bound)"]
    if not sel or "sides" not in sel:
        L.append(f"- not run: {'no quotes.jsonl candidates' if not sel else sel['reason']}")
    else:
        for s, v in sel["sides"].items():
            m = v.get("at_verified_ref")
            L.append(f"- {s}: {v['n_candidates']} candidates (of {rep['inputs']['candidates']['total']} in the file, "
                     f"cap {rep['inputs']['candidates']['cap']}) at weight {sel['identity_precision']}; "
                     + (f"offset {m['offset_c']['verified']} -> {m['offset_c']['pooled']} c (move {m['offset_c']['move']}), "
                        f"skew move {m['skew_c_per_contract']['move']}; " if m else "no refit; ")
                     + f"ref {v['verified_ref']} -> {v['pooled_ref']}")
    gn, ck, ld = rep["genome"], rep["emitted_genome_check"], rep["ladder"]
    ex = rep["explained_by_observed_inputs"]
    L += ["", "## explained by observed inputs: " + ("YES" if ex["verdict"] else "NO"),
          f"- side: {ex['side']['kind']}, explained {ex['side']['explained']}, balanced accuracy "
          f"{ex['side'].get('balanced_accuracy')} (interval {ex['side'].get('ci95')}, null 0.5)",
          f"- price: skill vs a constant price {ex['price'].get('value')} (CI {ex['price'].get('ci95')}), explained {ex['price']['explained']}",
          f"- timing (filled presence, not a gate): skill vs constant presence {ex['timing'].get('value')} (CI {ex['timing'].get('ci95')})"]
    L += ["", "## ladder", f"- {ld.get('status')}: share of orders resting >= {ld.get('floor_ms')} ms with another own level "
          f"on the same token and side {(ld.get('ladder_share') or {}).get('value')} "
          f"(CI {(ld.get('ladder_share') or {}).get('ci95')}; any overlap {ld.get('brief_overlap_share')}), depth {ld.get('depth')}",
          "", f"## genome {gn['id'][:12]} (draft, not registered)" if gn["id"] else "## NO GENOME EMITTED"]
    L += [f"- NOT EMITTED because: {x}" for x in gn["reasons"]]
    if gn["few_windows"]:
        L.append(f"- only {gn['windows']} windows: every CI here is crude")
    if ck:
        L.append(f"- draft's in-sample quote intent: would quote {ck['would_quote_share']} of {ck['rows']} orders "
                 f"(silent by {ck['silent_by']}), MAE {ck['mae_c_when_quoting']} c when quoting, fair clamp binds "
                 f"{ck['fair_clamp_binds_share']}" + ("; fair_up SUBSTITUTED by mid" if ck["fair_substituted_by_mid"] else ""))
    L += [f"- universe: {rep['universe_fit']}", f"- risk (measured, not a limit): {rep['risk_fit']}",
          "", "## the v1 schema cannot express"]
    L += [f"- {c['item']}: {c['why']}" for c in rep["cannot_express"]]
    return "\n".join(L) + "\n"


def _clean(x) -> Any:
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.floating, float)):
        return float(x) if math.isfinite(x) else None
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def run(root: Path, om_dir: Path | None, orders_path: Path | None, wallet: str, t_from: int, t_to: int,
        out: Path | None = None,
        token_files: list[Path] | None = None, brti_root: Path | None = None, asof: str = "placement",
        precision: float | None = None, max_candidates: int = MAX_CANDIDATES, *,
        reconstruct_manifest: Path | None = None,
        ) -> tuple[dict | None, dict, list[dict]]:
    """(draft genome dict or None, fit report, decision-table rows); written to ``out`` when given.
    No genome (and no ``genome.json``) when the emitted rule could not act like the fit; the
    reasons are in ``report["genome"]["reasons"]``."""
    wallet = wallet.lower()
    matched_orders = match_summary = None
    if reconstruct_manifest is not None:
        if om_dir is not None or orders_path is not None:
            raise ValueError("use reconstruction manifest or standalone order inputs")
        from .research_manifest import read_reconstruction

        source = read_reconstruction(reconstruct_manifest, wallet, t_from, t_to)
        orders_path, matched_orders, match_summary = source.orders, source.matched_orders, source.summary
    if orders_path is None:
        raise ValueError("missing order tape")
    T = decision_table(root, om_dir, orders_path, wallet, t_from, t_to, token_files or [], brti_root, asof,
                       max_candidates, matched_orders=matched_orders, match_summary=match_summary)
    if reconstruct_manifest is not None:
        T["inputs"]["reconstruct_manifest"] = str(reconstruct_manifest)
        T["inputs"]["candidates"]["unavailable"] = "reconstruction does not publish per-wallet candidate quotes"
    if not any(r["kind"] == "order" for r in T["rows"]) and T["takes"]:
        report = _clean(taker_report(T, wallet))  # no maker order, but taker fills: a report, never a genome
        if out is not None:
            out.mkdir(parents=True, exist_ok=True)
            (out / "genome.json").unlink(missing_ok=True)
            (out / "fit_report.json").write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
            (out / "rule.md").write_text(taker_card(report))
        return None, report, []
    if not any(r["kind"] == "order" for r in T["rows"]):
        raise SystemExit(f"doppel-rulefit: no order of {wallet} could be put in a window ({dict(T['stats'])}). "
                         "Token -> window needs rtds_* rows on --data or a tokens.jsonl (--tokens, or "
                         "<--brti-lab>/tokens.jsonl); also check --wallet and --from/--to.")
    if precision is None:
        precision = ((T["om_doc"].get("fingerprint") or {}).get("total") or {}).get("identity_precision_upper")
    genome, report = build(T, wallet, precision)
    genome, report = _clean(genome), _clean(report)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        if genome is not None:
            (out / "genome.json").write_bytes(orjson.dumps(genome, option=orjson.OPT_INDENT_2))
        else:
            (out / "genome.json").unlink(missing_ok=True)  # never leave an older run's genome next to this report
        (out / "fit_report.json").write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
        (out / "rule.md").write_text(card(report))
        with open(out / "decision_table.jsonl", "wb") as f:
            for r in T["rows"]:
                f.write(orjson.dumps(_clean(r)) + b"\n")
    return genome, report, T["rows"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-rulefit")
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--ordermatch-dir", type=Path, help="doppel-ordermatch output directory")
    ap.add_argument("--orders", type=Path, help="doppel-ordertape output (parquet dir or JSONL)")
    ap.add_argument("--reconstruct-manifest", type=Path, help="use verified artifacts from a reconstruction manifest")
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--from", dest="t_from", type=_ts, required=True, help="inclusive receive time, unix s or ISO UTC")
    ap.add_argument("--to", dest="t_to", type=_ts, required=True, help="exclusive receive time, unix s or ISO UTC")
    ap.add_argument("--brti-lab", type=Path, default=None,
                    help="enables fair-v1; BRTI is read from here if it holds ref_brti_raw, else from --data; "
                         "its tokens.jsonl (if any) joins the token map")
    ap.add_argument("--tokens", type=Path, action="append", default=[], help="extra tokens.jsonl (no network)")
    ap.add_argument("--asof", choices=("placement", "created"), default="placement",
                    help="state cutoff: the placement's receive time, or that minus the sign-to-book delay")
    ap.add_argument("--identity-precision", type=float, default=None,
                    help="weight of fingerprint candidates (default: ordermatch.json identity_precision_upper)")
    ap.add_argument("--max-candidates", type=int, default=MAX_CANDIDATES,
                    help="fingerprint candidates kept (stratified by window, seeded); the cap is reported")
    ap.add_argument("--out", type=Path, required=True, help="directory")
    a = ap.parse_args(argv)
    if a.reconstruct_manifest is None and (a.ordermatch_dir is None or a.orders is None):
        ap.error("give --reconstruct-manifest or both --ordermatch-dir and --orders")
    toks = list(a.tokens)
    brti = None
    if a.brti_lab is not None:
        if (a.brti_lab / "tokens.jsonl").exists():
            toks.append(a.brti_lab / "tokens.jsonl")
        brti = a.brti_lab if (a.brti_lab / "ref_brti_raw").exists() else a.data
    try:
        genome, report, _ = run(a.data, a.ordermatch_dir, a.orders, a.wallet, a.t_from, a.t_to, a.out, toks, brti,
                                a.asof, a.identity_precision, a.max_candidates,
                                reconstruct_manifest=a.reconstruct_manifest)
    except ValueError as exc:
        ap.error(str(exc))
    if "taker_rule" in report:   # a wallet with no maker order: "no rule found" is a result, not a failure
        print(taker_card(report))
        return 0
    print(card(report))
    return 0 if genome is not None else 1


if __name__ == "__main__":
    sys.exit(main())
