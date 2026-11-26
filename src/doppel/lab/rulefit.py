"""Rule fit: a wallet's chain-verified signed orders -> an explicit quoting rule with uncertainty,
emitted as a DRAFT candidate genome plus the list of what the genome schema cannot express.

LABEL: every number here is a "rule conditional on fill". Only orders that filled at least once
are on chain (E34), and E39 estimates that fills are ~0.3 % of what such a maker posts. The fit
describes the quotes that TRADED. The only view of the rest is ordermatch's fingerprint candidates
(``quotes.jsonl``), an attribution estimate: when present the price rule is refitted with them,
weighted by the stated identity precision, and the move is reported (selection-bias check).

INPUTS
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
   Columns: ``bb_c``/``ba_c``/``mid_c``/``spread_c``, ``depth_bid``/``depth_ask`` (size at the
   best), ``d_touch_c`` (limit - same-side best), ``d_mid_c``, ``d_fair_c``, ``tau_s``/``tte_s``,
   ``fair_c`` (twin ``Features.fair`` at second floor(cutoff): BRTI received by then, 1 s delay;
   BTC only, null before the window start), ``inv`` (own net Up contracts in the window: tape
   fills located by a tx-hash print received before cutoff, else block time < cutoff; maker and
   taker), ``gap_s`` (venue seconds since its previous verified order on that token).
   The best bid/ask can contain the wallet's own OTHER resting quotes: they are anonymous.
2. FITS. Each fitted number is ``{value, n, clusters, ci95, oos}``: ci95 = percentile bootstrap
   resampling market windows (seeded); oos = time-ordered split, first 70 % fit, last 30 % test
   (``mae_c`` for models, the holdout's own statistic for descriptive numbers). A window can
   straddle the split.
   a. price, per Up side: limit = ref + a - skew * inv for ref in touch / mid / fair (refs known
      for >= half the rows compete, on the rows where all of them are known). Per ref the
      constant-offset (median) and the inventory (least squares) model are fitted on the first
      70 %; the ref with the lowest test MAE wins; ``tie`` when the bootstrap CI of its test-error
      difference to the runner-up spans 0 (touch and mid cannot be told apart under a constant
      spread). Reported: offset median and IQR, the IQR of the chosen model's residual, share within 1 c, skew per contract (genome
      sign: positive = quotes shift down when long), slopes on time-to-close and spread.
   b. size: ``profile._size`` (fixed sizes / fixed notional / variable), median, slope on price.
   c. timing: re-post gap; what preceded each order since its previous one on the token:
      ``own_fill`` (an own fill in the window in between), else ``book_move`` (same-token best
      bid or ask differs by >= 1 c from the state just after the previous order landed), else
      ``timer``; cancels (ordermatch ``cancelled``): life, time from last fill, replaced within
      1 s, and at the cancel whether the opposite best had moved >= 1 c or the order was behind
      the best.
   d. participation: 10 s cells of every window of the wallet's asset-horizons with a known Up
      book at the cell's start; a cell is ``present`` when a verified order rested in it (placement to
      ``rest_max_ms``, else ``rest_min_ms``). Rates by time-to-close and mid band, both sides
      vs one. Absence of a FILLED order is not absence of a quote.
3. GENOME (``genome.json``, ``status: "draft"``, never registered): the best rule the v1 schema can
   hold (one reference mid|fair, one symmetric half-spread, one skew) with its test MAE next to
   the per-side fit's. ``fit_report.json`` -> ``cannot_express`` lists every measured behaviour
   the schema drops, each with its number.

Analysis only. No look-ahead in the state rows (tested); outcome columns come from ordermatch,
which reads the future by design, and never enter a state row.
"""

from __future__ import annotations

import argparse
import bisect
import math
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import orjson

from ..strategy.genome import Condition, Genome, MakerRule, Source, Universe, to_dict, validate
from ..tape.segment import read_segment, segment_path
from ..twin.features import Features, RefStore
from .census import LEN, window_of
from .flow import RTDS_STREAMS, _ts, hours_between, iter_rtds
from .intlbook import NS, IntlBook, LevelEvent, Print, px
from .ledger import TokenMap
from .ordermatch import filled_shares
from .profile import _size, load_rows, prepare

TRAIN_FRAC = 0.7
N_BOOT = 400
SEED = 20261126
MIN_N = 20
GRID_S = 10
MOVE_C = 1.0
REPLACE_MS = 1000
TOL = 0.02  # shares
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
         cancels: dict[tuple, list], txs: set[str], up_by_start: dict[int, list[tuple[str, str]]],
         length: int) -> dict:
    """One pass over a stream in tape order.

    ``queries`` (tw_ns, token, key) sorted by tw: each is answered with the token's top of book
    BEFORE the first record with ``tw >= tw_ns`` is applied, i.e. from records received strictly
    earlier. ``cancels``: (token, price, side, venue_ms) -> [(key, remainder)]: the top just before
    the matching decrease. ``txs``: earliest receive ns of a print per tx. Grid: the Up token's top
    at every GRID_S boundary inside its window."""
    book = IntlBook()
    state: dict = {}
    at_cancel: dict = {}
    fill_tw: dict[str, int] = {}
    grid: dict[tuple[str, int], tuple | None] = {}
    qi = 0
    next_bin = t_from - t_from % GRID_S + GRID_S

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
            for tok, slug in up_by_start.get(next_bin - next_bin % length, ()):
                grid[(slug, next_bin)] = _top(book.levels.get(tok))
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
                    want = cancels.get((it.asset, it.price, it.side, it.venue_ms))
                    hit = next((c for c in want or () if abs(-it.delta - c[1]) <= TOL), None)
                    if hit is not None and want is not None:
                        want.remove(hit)
                        lv = dict(book.levels.get(it.asset) or {})
                        for e in reversed([x for x in items if isinstance(x, LevelEvent) and x.asset == it.asset]):
                            lv[(e.price, e.side)] = e.old_size  # undo this record: the state before it
                        at_cancel[hit[0]] = _top(lv)
    answer(1 << 62)  # only states asked for after the last record
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
def load_specs(om_dir: Path, wallet: str) -> tuple[list[dict], list[dict], dict, Counter]:
    """(the wallet's located maker orders, fingerprint candidates, its ordermatch summary, counts)."""
    stats: Counter = Counter()
    orders = []
    for line in (om_dir / "orders.jsonl").read_bytes().splitlines():
        if not line.strip():
            continue
        r = orjson.loads(line)
        if str(r.get("maker", "")).lower() != wallet:
            stats["orders_other_makers"] += 1
        elif r.get("fill_before_placement"):
            stats["orders_wrong_match_dropped"] += 1
        else:
            orders.append(r)
    quotes = []
    qp = om_dir / "quotes.jsonl"
    if qp.exists():
        for line in qp.read_bytes().splitlines():
            if line.strip():
                r = orjson.loads(line)
                if str(r.get("wallet", wallet)).lower() == wallet:
                    quotes.append(r)
    doc = {}
    dp = om_dir / "ordermatch.json"
    if dp.exists():
        doc = (orjson.loads(dp.read_bytes()).get("wallets") or {}).get(wallet) or {}
    stats["orders"], stats["candidates"] = len(orders), len(quotes)
    return orders, quotes, doc, stats


def wallet_fills(orders_path: Path, wallet: str) -> tuple[list[dict], dict, set[str] | None]:
    """The wallet's verified tape fills [{tx, token, side, shares, block_ts}], input counts, and
    its verified order ids (None for a legacy tape without a ``verified`` column: nothing to check)."""
    rows, flagged = load_rows(orders_path, {wallet})
    w = prepare(rows, flagged)
    ids = {r["order_id"] for r in w["rows"]} if flagged else None
    out = [{"tx": r["tx"], "token": r["token_id"], "side": r["side"], "shares": filled_shares(r),
            "block_ts": r["block_ts"]} for r in w["rows"]
           if r["side"] == "SELL" or r.get("maker_amount") or r.get("limit")]
    return out, w["input"], ids


class Inventory:
    """Own net Up contracts per window from tape fills, as of a receive instant."""

    def __init__(self, fills: list[dict], tok: dict[str, dict], fill_tw: dict[str, int]):
        by: dict[str, list[tuple[int, float]]] = defaultdict(list)
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
            sign = (1 if f["side"] == "BUY" else -1) * (1 if info["idx"] == 0 else -1)
            by[info["slug"]].append((t, sign * f["shares"]))
        self.t: dict[str, list[int]] = {}
        self.cum: dict[str, list[float]] = {}
        for slug, xs in by.items():
            xs.sort()
            self.t[slug] = [x[0] for x in xs]
            self.cum[slug] = np.cumsum([x[1] for x in xs]).tolist()
        self.n = len(fills)

    def at(self, slug: str, tw: int) -> float:
        """Net Up contracts from fills located strictly before ``tw``."""
        i = bisect.bisect_left(self.t.get(slug, []), tw)
        return self.cum[slug][i - 1] if i else 0.0

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
        self.feat: dict[int, Features | None] = {}

    def at(self, info: dict, tw: int) -> float | None:
        d = tw // NS
        if info["asset"] != "btc" or d < info["start"]:
            return None
        if info["start"] not in self.feat:
            try:
                ref = self.store.window_ref("btc", info["start"], info["end"], self.genome.fair.vol_window_s)
                self.feat[info["start"]] = Features(self.genome, ref)
            except KeyError:
                self.feat[info["start"]] = None
        f = self.feat[info["start"]]
        v = f.fair(d) if f is not None else math.nan
        return 100 * v if v == v else None


# ---------------------------------------------------------------- decision table
def decision_table(root: Path, om_dir: Path, orders_path: Path, wallet: str, t_from: int, t_to: int,
                   token_files: list[Path], brti_root: Path | None = None, asof: str = "placement"
                   ) -> dict:
    wallet = wallet.lower()
    orders, quotes, om_doc, stats = load_specs(om_dir, wallet)
    fills, fill_input, verified = wallet_fills(orders_path, wallet)
    if verified is not None:  # ordermatch does not look at ``verified``; the order tape does
        stats["orders_not_verified_dropped"] = sum(o["order_id"] not in verified for o in orders)
        orders = [o for o in orders if o["order_id"] in verified]
    tok = token_map(root, t_from, t_to, token_files)
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
                      "cutoff": int(q["tw_ns"]) - (lag_q if asof == "created" else 0) * 1_000_000, "om": q})

    mapped = {s["token"] for s in specs} & tok.keys()
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
        up_by_start: dict[int, list[tuple[str, str]]] = defaultdict(list)
        for t, info in tok.items():
            if info["idx"] == 0 and _stream(info) == s and (info["asset"], info["hz"]) in pairs:
                up_by_start[info["start"]].append((t, info["slug"]))
        got = scan(root, s, t_from, t_to, qs, cancels, txs, up_by_start, LEN.get(hz, 300))
        for k in ("state", "at_cancel", "grid", "mates"):
            res[k].update({a: b for a, b in got[k].items() if k != "state" or b is not None or a not in res[k]})
        for tx, t in got["fill_tw"].items():
            res["fill_tw"][tx] = min(t, res["fill_tw"].get(tx, t))
    for a, b in res["mates"].items():  # a token never seen by RTDS, but its mate was
        if a not in tok and b in tok:
            tok[a] = tok[b] | {"idx": 1 - tok[b]["idx"]}
            stats["tokens_mapped_by_mate"] += 1

    inv = Inventory(fills, tok, res["fill_tw"])
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
        row = {"id": x["id"], "kind": x["kind"], "w": x["w"], "window": info["slug"], "asset": info["asset"],
               "hz": info["hz"], "token": x["token"], "idx": info["idx"], "cutoff_tw_ns": x["cutoff"],
               "placed_venue_ms": x["placed_ms"], "up_side": side, "px_c": _r(pc), "size": x["size"],
               **v, "fair_c": _r(f),
               "d_touch_c": _r(None if touch is None else pc - touch),
               "d_mid_c": _r(None if v["mid_c"] is None else pc - v["mid_c"]),
               "d_fair_c": _r(None if f is None else pc - f),
               "tau_s": _r(x["cutoff"] / NS - info["start"], 3), "tte_s": _r(info["end"] - x["cutoff"] / NS, 3),
               "inv": _r(inv.at(info["slug"], x["cutoff"])), "gap_s": None}
        if x["kind"] == "order":
            p = prev.get(x["token"])
            if p is not None:
                row["gap_s"] = _r((x["placed_ms"] - p["placed_ms"]) / 1000, 3)
                x["prev"] = p
            prev[x["token"]] = x
        x["row"], x["info"] = row, info
        rows.append(row)
    return {"rows": rows, "specs": [x for x in specs if "row" in x], "res": res, "inv": inv, "tok": tok,
            "om_doc": om_doc, "stats": stats, "pairs": sorted(pairs),
            "inputs": {"fills": fill_input | {"n": inv.n, "unmapped": inv.unmapped,
                                              "timed_by_block": inv.by_block_time},
                       "streams": streams, "asof": asof, "fair": fair is not None,
                       "sign_to_book_ms_p50": lag_q}}


# ---------------------------------------------------------------- 2a price rule
def _arr(rows: list[dict], key: str) -> np.ndarray:
    return np.array([math.nan if r.get(key) is None else r[key] for r in rows], dtype=float)


def _side_data(rows: list[dict], side: str) -> dict:
    rs = sorted((r for r in rows if r["up_side"] == side), key=lambda r: r["cutoff_tw_ns"])
    y = _arr(rs, "px_c")
    return {"y": y, "w": _arr(rs, "w"), "inv": _arr(rs, "inv"), "tte": _arr(rs, "tte_s"),
            "spread": _arr(rs, "spread_c"), "cl": np.array([r["window"] for r in rs], dtype=object).astype(str),
            "ref": {"touch": y - _arr(rs, "d_touch_c"), "mid": y - _arr(rs, "d_mid_c"),
                    "fair": y - _arr(rs, "d_fair_c")}}


def fit_price_side(D: dict, force: str | None = None) -> dict:
    """Price rule of one Up side (module docstring 2a)."""
    n_all = len(D["y"])
    known = {r: np.isfinite(D["ref"][r]) for r in REFS}
    cands = [r for r in REFS if n_all and known[r].sum() >= max(MIN_N, 0.5 * n_all)]
    if force is not None:
        cands = [force] if force in cands else []
    out: dict = {"n_rows": n_all, "ref_coverage": {r: _r(known[r].mean()) if n_all else None for r in REFS},
                 "chosen": None}
    if not cands:
        return out | {"reason": f"no reference known for >= max({MIN_N}, half) of the rows"}
    m = np.isfinite(D["inv"]) & np.all([known[r] for r in cands], axis=0)
    y, w, inv, cl = D["y"][m], D["w"][m], D["inv"][m], D["cl"][m]
    n = len(y)
    tr, te = _split(n)
    if len(tr) < MIN_N // 2 or not len(te):
        return out | {"reason": "too few rows with every candidate reference"}
    X = np.column_stack([np.ones(n), inv])
    by: dict = {}
    errs: dict[str, np.ndarray] = {}
    for r in cands:
        d = y - D["ref"][r][m]
        a = wmedian(d[tr], w[tr])
        beta = wls(X[tr], d[tr], w[tr])
        e_c, e_s = np.abs(d - a), np.abs(d - X @ beta)
        mae_c, mae_s = wmean(e_c[te], w[te]), wmean(e_s[te], w[te])
        errs[r] = e_s if mae_s < mae_c else e_c
        by[r] = {"model": "skew" if mae_s < mae_c else "const", "oos_mae_const_c": _r(mae_c),
                 "oos_mae_skew_c": _r(mae_s), "offset_train_c": _r(a),
                 "oos_within_1c": _r(wmean((errs[r][te] <= 1.0 + 1e-9).astype(float), w[te]))}
    rank = sorted(cands, key=lambda r: (wmean(errs[r][te], w[te]), REFS.index(r)))
    best = rank[0]
    out |= {"n": n, "n_train": int(len(tr)), "n_test": int(len(te)), "candidates": by, "chosen": best,
            "model": by[best]["model"], "tie_with": None}
    if len(rank) > 1:
        diff = errs[rank[1]][te] - errs[best][te]
        ci = boot_ci(lambda i: wmean(diff[i], w[te][i]), cl[te])
        out["runner_up"] = {"ref": rank[1], "oos_mae_gap_c": _r(wmean(diff, w[te])), "ci95": ci}
        if ci is None or ci[0] <= 0:
            out["tie_with"] = rank[1]
    d = y - D["ref"][best][m]
    off = wmedian(d, w)
    oos = {"mae_c": _r(wmean(errs[best][te], w[te])), "within_1c": by[best]["oos_within_1c"], "n_test": int(len(te))}
    q = lambda p: float(np.percentile(d, p))  # noqa: E731
    out["offset_c"] = est(off, n, cl, boot_ci(lambda i: wmedian(d[i], w[i]), cl), oos) | {
        "p25": _r(q(25)), "p75": _r(q(75)), "iqr": _r(q(75) - q(25))}
    in1 = (np.abs(d - off) <= 1.0 + 1e-9).astype(float)
    out["within_1c_of_const_rule"] = est(wmean(in1, w), n, cl, boot_ci(lambda i: wmean(in1[i], w[i]), cl),
                                         {"holdout_value": by[best]["oos_within_1c"], "n_test": int(len(te))})
    slope = lambda i: -wls(X[i], d[i], w[i])[1]  # noqa: E731
    out["skew_c_per_contract"] = est(slope(np.arange(n)), n, cl, boot_ci(slope, cl),
                                     {"mae_c_const": by[best]["oos_mae_const_c"],
                                      "mae_c_with_skew": by[best]["oos_mae_skew_c"], "n_test": int(len(te))})
    out["intercept_c_with_skew"] = _r(wls(X, d, w)[0])
    res = d - (X @ wls(X, d, w) if by[best]["model"] == "skew" else off)
    out["residual_iqr_c"] = _r(np.percentile(res, 75) - np.percentile(res, 25))  # spread the rule leaves
    out["inventory_range"] = [_r(inv.min()), _r(inv.max())]
    # dependence on time-to-close and spread, next to inventory
    tte, sp = D["tte"][m], D["spread"][m]
    ok = np.isfinite(tte) & np.isfinite(sp)
    if ok.sum() >= MIN_N:
        Z = np.column_stack([np.ones(n), inv, tte, sp])[ok]
        dz, wz, cz = d[ok], w[ok], cl[ok]
        tz, ez = _split(len(dz))
        full = wls(Z, dz, wz)
        ci = boot_ci(lambda i: wls(Z[i], dz[i], wz[i])[2:], cz)
        mae = wmean(np.abs(dz[ez] - Z[ez] @ wls(Z[tz], dz[tz], wz[tz])), wz[ez]) if len(ez) and len(tz) > 4 else None
        o = {"mae_c": _r(mae), "n_test": int(len(ez))}
        out["slope_c_per_s_to_close"] = est(full[2], len(dz), cz, ci[0] if ci else None, o)
        out["slope_c_per_c_spread"] = est(full[3], len(dz), cz, ci[1] if ci else None, o)
    return out


def _excludes_zero(e: dict | None) -> bool:
    ci = (e or {}).get("ci95")
    return bool(ci) and (ci[0] > 0 or ci[1] < 0)


def fit_price(rows: list[dict]) -> dict:
    return {s: fit_price_side(_side_data(rows, s)) for s in ("bid", "ask")}


def fit_expressible(rows: list[dict], price: dict) -> dict:
    """The best rule MakerRule v1 can hold: one ref (mid|fair), symmetric half-spread, one skew."""
    rs = sorted(rows, key=lambda r: r["cutoff_tw_ns"])
    y, inv, w = _arr(rs, "px_c"), _arr(rs, "inv"), _arr(rs, "w")
    sign = np.array([-1.0 if r["up_side"] == "bid" else 1.0 for r in rs])
    cl = np.array([r["window"] for r in rs], dtype=object).astype(str)
    best: dict | None = None
    for ref in ("mid", "fair"):
        d = _arr(rs, f"d_{ref}_c")
        m = np.isfinite(d) & np.isfinite(inv)
        if m.sum() < max(MIN_N, 0.5 * len(rs)):
            continue
        dm, im, sm, wm, cm = d[m], inv[m], sign[m], w[m], cl[m]
        tr, te = _split(len(dm))

        Xd = np.column_stack([sm < 0, sm > 0, im]).astype(float)  # side intercepts, one inventory slope
        slope = lambda i: -float(wls(Xd[i], dm[i], wm[i])[2])  # noqa: E731
        k = slope(np.arange(len(dm)))
        ci = boot_ci(slope, cm)
        use = ci is not None and (ci[0] > 0 or ci[1] < 0)

        def offsets(i, kk: float) -> tuple[float, float]:
            """(half-spread, centre shift) at zero inventory: per-side medians of d + kk * inv."""
            z = dm[i] + kk * im[i]
            lo, hi = (wmedian(z[sm[i] == s], wm[i][sm[i] == s]) if (sm[i] == s).any() else None
                      for s in (-1.0, 1.0))
            if lo is not None and hi is not None:
                return max((hi - lo) / 2, 0.0), (hi + lo) / 2
            return abs(hi if hi is not None else lo or 0.0), 0.0

        use_k = k if use else 0.0
        h, c = offsets(np.arange(len(dm)), use_k)
        kt = slope(tr) if use and len(tr) > 3 else 0.0
        ht, _ = offsets(tr, kt)
        mae = wmean(np.abs(dm[te] - sm[te] * ht + kt * im[te]), wm[te]) if len(te) else math.nan
        cand = {"quote_ref": ref, "half_spread_c": _r(h, 2), "centre_shift_c": _r(c, 2),
                "skew_c_per_contract": _r(use_k, 4), "skew_fit": est(k, len(dm), cm, ci),
                "n": int(len(dm)), "oos_mae_c": _r(mae), "n_test": int(len(te)),
                "sides_seen": sorted({"bid" if s < 0 else "ask" for s in sm})}
        if best is None or (cand["oos_mae_c"] or 1e9) < (best["oos_mae_c"] or 1e9):
            best = cand
    if best is None:
        return {"quote_ref": None, "reason": "neither mid nor fair known for half the rows"}
    tot = [(price[s]["offset_c"]["oos"]["mae_c"], price[s]["n_test"]) for s in ("bid", "ask")
           if price[s].get("chosen") and price[s]["offset_c"]["oos"]["mae_c"] is not None]
    per_side = sum(a * b for a, b in tot) / sum(b for _, b in tot) if tot else None
    best["per_side_fit_oos_mae_c"] = _r(per_side)
    best["oos_cost_of_schema_c"] = _r(None if per_side is None or best["oos_mae_c"] is None
                                     else best["oos_mae_c"] - per_side)
    return best


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
    out: dict = {"n_orders": len(xs), "gap_s": describe(g, cl) | {"quantiles": _quant(g)},
                 "gap_note": "gaps between FILLED orders on one token: an upper bound on the re-post cadence",
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
            "life_ms": describe(life, cc) | {"quantiles": _quant(life)},
            "ms_from_last_fill": _quant(np.array(after_fill, dtype=float)),
            "replaced_within_1s": _share(replaced, cc),
            "opposite_best_moved_1c": _share(opp, ck) if opp else None,
            "behind_best_at_cancel": _share(behind, ck) if behind else None,
            "neither_moved_nor_behind": _share([not (a or b) for a, b in zip(opp, behind)], ck) if opp else None,
            "note": "every visible order filled at least once: these are cancels of a REMAINDER after a fill"}
    else:
        out["cancel"] = None
    return out


# ---------------------------------------------------------------- 2d participation
def fit_participation(T: dict) -> dict:
    xs = [x for x in T["specs"] if x["kind"] == "order"]
    cells: dict[tuple[str, int], set[str]] = defaultdict(set)
    for x in xs:
        o = x["om"]
        rest = o.get("rest_max_ms")
        rest = o.get("rest_min_ms") or 0 if rest is None else rest
        lo, hi = x["placed_ms"] // 1000, (x["placed_ms"] + rest) // 1000
        for b in range(lo - lo % GRID_S, hi + 1, GRID_S):  # every cell [b, b + GRID_S) it rested in
            cells[(x["row"]["window"], b)].add(x["row"]["up_side"])
    win = {i["slug"]: i for i in T["tok"].values()}
    recs = []
    for (slug, b), top in sorted(T["res"]["grid"].items()):
        v = up_view(top, 0)
        if v["mid_c"] is None:
            continue
        i = win[slug]
        sides = cells.get((slug, b), set())
        recs.append((slug, i["start"], (i["end"] - b) / (i["end"] - i["start"]), v["mid_c"], sides))
    tau = np.array([x["row"]["tau_s"] for x in xs])
    tte = np.array([x["row"]["tte_s"] for x in xs])
    inwin = (tau >= 0) & (tte > 0)
    out: dict = {"cells": len(recs), "cell_s": GRID_S, "orders": len(xs),
                 "pre_open_order_share": _r(np.mean(tau < 0)) if len(xs) else None,
                 "tte_s_in_window": _quant(tte[inwin]), "tau_s_in_window": _quant(tau[inwin]),
                 "note": "present = a FILLED order was resting in the cell; unfilled quotes are invisible"}
    if not recs:
        return out | {"reason": "no grid cell with a known Up book"}
    recs.sort(key=lambda r: r[1])
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


# ---------------------------------------------------------------- selection-bias check
def selection_check(rows: list[dict], price: dict, precision: float | None) -> dict | None:
    cand = [r for r in rows if r["kind"] == "candidate"]
    if not cand:
        return None
    if precision is None:
        return {"n_candidates": len(cand), "reason": "no identity precision stated (ordermatch.json "
                "fingerprint.total.identity_precision_upper or --identity-precision): not refitted"}
    pooled = [r if r["kind"] == "order" else r | {"w": precision} for r in rows]
    out: dict = {"n_candidates": len(cand), "identity_precision": precision,
                 "weights": "verified orders 1, fingerprint candidates the identity precision (an upper bound)",
                 "note": "candidates are not identified as the wallet's; a move here bounds the selection "
                         "bias only as far as the attribution holds", "sides": {}}
    for s in ("bid", "ask"):
        v = price[s]
        D = _side_data(pooled, s)
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
def build(T: dict, wallet: str, precision: float | None = None) -> tuple[dict, dict]:
    """(draft genome dict, fit report)."""
    rows = T["rows"]
    ver = [r for r in rows if r["kind"] == "order"]
    price = fit_price(ver)
    expr = fit_expressible(ver, price) if ver else {"quote_ref": None, "reason": "no orders"}
    size = fit_size(T["specs"])
    timing = fit_timing(T)
    part = fit_participation(T)
    sel = selection_check(rows, price, precision)

    # ---- genome fields, each with where it came from
    prov: dict = {}
    hz_len = {r["hz"]: LEN[r["hz"]] for r in ver}
    tte_q, tau_q = part.get("tte_s_in_window"), part.get("tau_s_in_window")
    L = min(hz_len.values()) if hz_len else 300
    tte_min = int(min(max(math.floor(tte_q["p10"]), 0), L - 1)) if tte_q else 0
    tau_min = int(min(max(math.floor(tau_q["p10"]), 0), L - 1)) if tau_q else 0
    prov["universe.tte_min_s"] = "floor(p10 of time-to-close of in-window filled orders)" if tte_q else "default"
    prov["universe.tau_min_s"] = "floor(p10 of seconds since start of in-window filled orders)" if tau_q else "default"
    med = size.get("median_size", {}).get("value") if size.get("n") else None
    life = (timing.get("cancel") or {}).get("life_ms", {}).get("quantiles")
    age = max(1, math.ceil(life["p90"] / 1000)) if life else 30
    prov["maker.max_quote_age_s"] = "ceil(p90 life of cancelled remainders), floor 1 s" if life else "default"
    lo, hi = _dead_bands(part.get("by_mid_c") or [], (part.get("presence_rate") or {}).get("value"))
    pause = tuple(c for c in (Condition("mid_up_c", "<", lo) if lo else None,
                              Condition("mid_up_c", ">", hi) if hi else None) if c is not None)
    prov["maker.pause_when"] = ("mid bands with >= 20 cells and presence under a tenth of the overall rate"
                                if pause else "none found")
    edge = None
    df = np.array([(-1 if r["up_side"] == "bid" else 1) * r["d_fair_c"] for r in ver if r["d_fair_c"] is not None])
    if len(df) >= MIN_N:
        edge = max(0.0, round(float(np.percentile(df, 5)), 2))
        prov["maker.min_edge_vs_fair_c"] = f"max(0, p5 of the quote's distance outside fair), n={len(df)}"
    else:
        prov["maker.min_edge_vs_fair_c"] = "default 0: fair not supplied or known for < 20 orders"
    seen = set(expr.get("sides_seen") or [])
    sides = "both" if seen == {"bid", "ask"} or not seen else ("bid_only" if seen == {"bid"} else "ask_only")
    prov["maker.quote_ref/half_spread_c/skew_c_per_contract"] = (
        "fit_expressible: one inventory slope with side intercepts (least squares), kept only when its "
        "bootstrap CI excludes 0; half-spread = half the gap between the per-side median offsets at zero inventory" if expr.get("quote_ref") else "defaults: " + str(expr.get("reason")))
    prov["maker.requote_threshold_c"] = "default 1.0 (the trigger threshold used to classify book moves)"
    prov["maker.size"] = "median size of verified orders" if med else "default"
    prov["risk/costs/kill/exit"] = "schema defaults, not fitted"
    maker = MakerRule(quote_ref=expr.get("quote_ref") or "mid",
                      half_spread_c=float(expr["half_spread_c"]) if expr.get("quote_ref") else 2.0,
                      size=float(med) if med else 10.0, sides=sides,  # type: ignore[arg-type]
                      skew_c_per_contract=expr.get("skew_c_per_contract") or 0.0,
                      requote_threshold_c=MOVE_C, max_quote_age_s=age, pause_when=pause,
                      min_edge_vs_fair_c=edge or 0.0)
    g = Genome(name=f"rulefit-{wallet[:10]}-maker", mode="maker",
               universe=Universe(venue="intl_sim", assets=tuple(sorted({r["asset"] for r in ver})) or ("btc",),
                                 horizons=tuple(sorted(hz_len)) or ("5m",), tau_min_s=tau_min, tte_min_s=tte_min),
               source=Source(kind="clone", wallets=(wallet,), mechanism="rule conditional on fill",
                             parents=(), operator="extract"),
               maker=maker, created_tw=0,
               evidence={"extractor": "doppel.lab.rulefit v1", "wallet": wallet, "label": "rule conditional on fill",
                         "n_orders": len(ver), "fields": prov,
                         "price_rule": {s: {k: price[s].get(k) for k in ("chosen", "model", "tie_with", "n")}
                                        | {"offset_c": (price[s].get("offset_c") or {}).get("value"),
                                           "oos_mae_c": ((price[s].get("offset_c") or {}).get("oos") or {}).get("mae_c")}
                                        for s in ("bid", "ask")},
                         "expressible": {k: expr.get(k) for k in ("quote_ref", "oos_mae_c", "per_side_fit_oos_mae_c",
                                                                  "oos_cost_of_schema_c", "centre_shift_c")}})
    errs = validate(g)
    gd = to_dict(g) | {"status": "draft"}

    # ---- what the v1 schema cannot hold, each with its number
    cx: list[dict] = []

    def cannot(item: str, measured, why: str) -> None:
        cx.append({"item": item, "measured": measured, "why": why})

    for s in ("bid", "ask"):
        v = price[s]
        if v.get("chosen") == "touch":
            cannot(f"{s}: quote pegged to the same-side best", {
                "oos_mae_c_touch": v["offset_c"]["oos"]["mae_c"], "offset_c": v["offset_c"]["value"],
                "tie_with": v["tie_with"], "oos_mae_c_by_ref": {r: min(c["oos_mae_const_c"], c["oos_mae_skew_c"])
                                                                for r, c in v["candidates"].items()}},
                   "MakerRule.quote_ref is fair|mid only")
        if v.get("chosen") and (v["residual_iqr_c"] or 0) > 2:
            cannot(f"{s}: several price levels (IQR of the rule's residual > 2 c)", {
                "residual_iqr_c": v["residual_iqr_c"]} | {k: v["offset_c"][k] for k in ("p25", "value", "p75")},
                   "one quote per side at one half-spread; no ladder")
        for key, what in (("slope_c_per_s_to_close", "time to close"), ("slope_c_per_c_spread", "spread")):
            if _excludes_zero(v.get(key)):
                cannot(f"{s}: offset depends on {what}", v[key], "half_spread_c is a constant (or logit in price)")
    b, a = price["bid"], price["ask"]
    if b.get("chosen") and a.get("chosen"):
        if b["chosen"] != a["chosen"]:
            cannot("a different reference per side", {"bid": b["chosen"], "ask": a["chosen"]}, "one quote_ref")
        kb, ka = b["skew_c_per_contract"], a["skew_c_per_contract"]
        if _excludes_zero(kb) != _excludes_zero(ka) or (
                _excludes_zero(kb) and (kb["ci95"][1] < ka["ci95"][0] or ka["ci95"][1] < kb["ci95"][0])):
            cannot("inventory skew differs by side", {"bid": kb, "ask": ka}, "one skew_c_per_contract for both quotes")
    if expr.get("quote_ref"):
        if abs(expr.get("centre_shift_c") or 0) >= 0.5:
            cannot("asymmetric offsets around the reference", {"centre_shift_c": expr["centre_shift_c"]},
                   "bid/ask = ref -/+ one half_spread_c")
        cannot("cost of the schema on the price rule", {k: expr.get(k) for k in (
            "oos_mae_c", "per_side_fit_oos_mae_c", "oos_cost_of_schema_c")},
               "test MAE of the emitted MakerRule vs the per-side fit")
        if expr["quote_ref"] == "mid":
            cannot("mid-referenced quote without fair", {"fair_coverage": b.get("ref_coverage", {}).get("fair")},
                   "Genome.maker_quotes returns no quote when fair_up is missing, whatever quote_ref is")
    gq = (timing.get("gap_s") or {}).get("quantiles")
    if gq and gq["p50"] < 1:
        cannot("sub-second re-post", {"gap_s": gq}, "decisions are per second; max_quote_age_s is an int >= 1")
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
    if len(hz_len) > 1 or len({r["asset"] for r in ver}) > 1:
        cannot("different parameters per asset / horizon", {"pairs": T["pairs"]}, "one MakerRule per genome")
    cannot("status draft", "draft", "Status has no 'draft'; the JSON carries it, Genome objects stay 'candidate'")
    cannot("unfilled quotes", CAVEAT, "not a schema gap: the data cannot show them")

    report = {"wallet": wallet, "label": "rule conditional on fill", "selection_caveat": CAVEAT,
              "definitions": "see doppel.lab.rulefit module docstring",
              "inputs": T["inputs"] | {"counts": dict(T["stats"]), "rows_orders": len(ver),
                                       "rows_candidates": len(rows) - len(ver), "pairs": T["pairs"]},
              "price_rule": price, "expressible_price_rule": expr, "size_rule": size, "timing_rule": timing,
              "participation_rule": part, "selection_check": sel,
              "genome": {"id": g.id, "status": "draft", "registered": False, "validate": errs, "fields": prov},
              "cannot_express": cx}
    return gd, report


def card(rep: dict) -> str:
    L = [f"# rule fit {rep['wallet']}  [RULE CONDITIONAL ON FILL]", "", rep["selection_caveat"], "",
         f"orders {rep['inputs']['rows_orders']}, candidates {rep['inputs']['rows_candidates']}, "
         f"counts {rep['inputs']['counts']}", "", "## price (Up cents)"]
    for s in ("bid", "ask"):
        v = rep["price_rule"][s]
        if not v.get("chosen"):
            L.append(f"- {s}: no rule ({v.get('reason')}; rows {v['n_rows']})")
            continue
        o, k = v["offset_c"], v["skew_c_per_contract"]
        L.append(f"- {s} = {v['chosen']} {o['value']:+g} c (IQR {o['p25']:+g}..{o['p75']:+g}, CI {o['ci95']}), "
                 f"skew {k['value']:g} c/contract (CI {k['ci95']}); n={v['n']}, test MAE {o['oos']['mae_c']} c, "
                 f"within 1 c {o['oos']['within_1c']}; model {v['model']}"
                 + (f"; TIE with {v['tie_with']}" if v["tie_with"] else ""))
    e = rep["expressible_price_rule"]
    L.append(f"- schema v1 can hold: ref {e.get('quote_ref')} +/- {e.get('half_spread_c')} c, skew "
             f"{e.get('skew_c_per_contract')}; test MAE {e.get('oos_mae_c')} c vs per-side {e.get('per_side_fit_oos_mae_c')} c")
    sz, tm, pt = rep["size_rule"], rep["timing_rule"], rep["participation_rule"]
    d = sz.get("descriptor") or {}
    L += ["", "## size", f"- {d.get('rule')}: sizes {d.get('sizes_90')}, median {sz.get('median_size', {}).get('value')}, "
          f"train set covers test {sz.get('train_size_set_covers_test', {}).get('value')}",
          "", "## timing", f"- gap between filled orders on a token (s): {tm['gap_s'].get('quantiles')}",
          f"- preceded by: { {k: v['value'] for k, v in (tm.get('trigger_shares') or {}).items()} }",
          f"- cancel: {None if not tm.get('cancel') else {k: (v.get('value') if isinstance(v, dict) and 'value' in v else v) for k, v in tm['cancel'].items() if k != 'note'}}",
          "", "## participation", f"- windows with orders {pt.get('windows_with_orders')}/{pt.get('windows_seen')}, "
          f"cell presence {(pt.get('presence_rate') or {}).get('value')}, both sides when present "
          f"{(pt.get('both_sides_share_when_present') or {}).get('value')}, pre-open order share {pt.get('pre_open_order_share')}"]
    sel = rep["selection_check"]
    L += ["", "## selection-bias check"]
    if not sel or "sides" not in sel:
        L.append(f"- not run: {'no quotes.jsonl candidates' if not sel else sel['reason']}")
    else:
        for s, v in sel["sides"].items():
            m = v.get("at_verified_ref")
            L.append(f"- {s}: {v['n_candidates']} candidates at weight {sel['identity_precision']}; "
                     + (f"offset {m['offset_c']['verified']} -> {m['offset_c']['pooled']} c (move {m['offset_c']['move']}), "
                        f"skew move {m['skew_c_per_contract']['move']}; " if m else "no refit; ")
                     + f"ref {v['verified_ref']} -> {v['pooled_ref']}")
    L += ["", f"## genome {rep['genome']['id'][:12]} (draft, not registered; validate: {rep['genome']['validate'] or 'ok'})",
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


def run(root: Path, om_dir: Path, orders_path: Path, wallet: str, t_from: int, t_to: int, out: Path | None = None,
        token_files: list[Path] | None = None, brti_root: Path | None = None, asof: str = "placement",
        precision: float | None = None) -> tuple[dict, dict, list[dict]]:
    """(draft genome dict, fit report, decision-table rows); written to ``out`` when given."""
    wallet = wallet.lower()
    T = decision_table(root, om_dir, orders_path, wallet, t_from, t_to, token_files or [], brti_root, asof)
    if precision is None:
        precision = ((T["om_doc"].get("fingerprint") or {}).get("total") or {}).get("identity_precision_upper")
    genome, report = build(T, wallet, precision)
    genome, report = _clean(genome), _clean(report)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "genome.json").write_bytes(orjson.dumps(genome, option=orjson.OPT_INDENT_2))
        (out / "fit_report.json").write_bytes(orjson.dumps(report, option=orjson.OPT_INDENT_2))
        (out / "rule.md").write_text(card(report))
        with open(out / "decision_table.jsonl", "wb") as f:
            for r in T["rows"]:
                f.write(orjson.dumps(_clean(r)) + b"\n")
    return genome, report, T["rows"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-rulefit")
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--ordermatch-dir", type=Path, required=True, help="doppel-ordermatch output directory")
    ap.add_argument("--orders", type=Path, required=True, help="doppel-ordertape output (parquet dir or JSONL)")
    ap.add_argument("--wallet", required=True)
    ap.add_argument("--from", dest="t_from", type=_ts, required=True, help="unix s or ISO UTC")
    ap.add_argument("--to", dest="t_to", type=_ts, required=True)
    ap.add_argument("--brti-lab", type=Path, default=None,
                    help="enables fair-v1; BRTI is read from here if it holds ref_brti_raw, else from --data; "
                         "its tokens.jsonl (if any) joins the token map")
    ap.add_argument("--tokens", type=Path, action="append", default=[], help="extra tokens.jsonl (no network)")
    ap.add_argument("--asof", choices=("placement", "created"), default="placement",
                    help="state cutoff: the placement's receive time, or that minus the sign-to-book delay")
    ap.add_argument("--identity-precision", type=float, default=None,
                    help="weight of fingerprint candidates (default: ordermatch.json identity_precision_upper)")
    ap.add_argument("--out", type=Path, required=True, help="directory")
    a = ap.parse_args(argv)
    toks = list(a.tokens)
    brti = None
    if a.brti_lab is not None:
        if (a.brti_lab / "tokens.jsonl").exists():
            toks.append(a.brti_lab / "tokens.jsonl")
        brti = a.brti_lab if (a.brti_lab / "ref_brti_raw").exists() else a.data
    _, report, _ = run(a.data, a.ordermatch_dir, a.orders, a.wallet, a.t_from, a.t_to, a.out, toks, brti,
                       a.asof, a.identity_precision)
    print(card(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
