"""doppel-fidelity: does this genome behave like this wallet?

    doppel-fidelity --data TAPE --lab LAB --genome G.json --wallet ADDR --orders ORDERTAPE \
                    [--ordermatch-dir DIR] --from TS --to TS --out DIR

The genome is replayed by the twin on the Polymarket INTL book (``lab/intltwin.py``) for every
window of its universe in [from, to] that the order tape covers, whether the wallet traded it or
not, and its simulated fills are scored against the wallet's real fills. A reconstruction is
accepted only if the verdict below is PASS. ``--lab`` holds ``tokens.jsonl`` (token -> window slug,
Up/Down index, payout); it is read, never refreshed.

THE WALLET IS TAKEN OUT OF THE TAPE the clone sees (``intltwin`` docstring): its located resting
orders are subtracted from the book over their lives (``--ordermatch-dir``: ``orders.jsonl`` of a
doppel-ordermatch run), prints of txs it TOOK are dropped, and the third-party orders that hit its
quotes stay as flow for whoever stands there. A window is ``footprint_partial`` when one of the
wallet's filled maker orders there has no located life (not in orders.jsonl, a wrong match, or an
unknown end). The wallet's never-filled quotes cannot be removed at all.

WALLET FILLS (order tape rows with ``maker == wallet``; ``verified`` must be true unless
``--allow-unverified``, which also takes rows the store could not check)
- Maker row: filled at its own limit; shares = ``fill_amount`` in maker-amount units (USDC / limit
  for a BUY, shares for a SELL).
- Taker row: one fill per maker order of the same tx, at that maker's limit (its mirror 1 - limit
  when the maker traded the other token: mint / merge), for that maker's filled shares. The taker's
  own limit is not its price (fixture tx 0x4f5bc2: limit 0.87, executed at 0.77). A taker row whose
  tx is missing maker rows cannot be priced and is UNUSABLE (``taker_unpriced``).
- Everything is put in UP terms, the twin's one instrument: direction +1 = more Up exposure (BUY
  Up, SELL Down), price = p for the Up token and 1 - p for the Down token. P&L is unchanged by this
  (payouts sum to 1).
- Time = venue ms of the tx's print on that window's tokens; without a print, block time - 2 s
  (kept for inventory and P&L, left out of fill matching: ``located_share``).
- Only fills with S <= t < E are scored: the clone can only trade inside the window. Fills before
  the open become the wallet's OPENING POSITION (the path score starts from it); fills after the
  close are only counted. Both are reported under ``coverage`` and gated.

CLONE FILLS: the twin's fills (engine clock: our receive time), same form.

WINDOWS
- eligible: the twin simulated it (tokens known, full window inside the requested holdout, book
  known from the open to the close, outcome known). A verdict also requires a coverage receipt
  attesting that the wallet's order tape is complete for that holdout, including quiet windows.
- comparable: eligible AND traded in-window by the wallet or the clone. Windows neither traded
  carry no information and are only counted. Every score is over the comparable windows.

SCORES (95% CI: resampling distinct window START TIMES with replacement, so windows of several
assets / horizons that share a start move together)
- activity: share of comparable windows traded by both / wallet only / clone only. ``agreement`` =
  the share traded by both (both / either: idle-idle never counts as agreement).
- fills, a maximum number of ONE-TO-ONE pairs on (window, direction, |price| <= 1 c, |time| <= 2 s).
  Augmenting paths prefer nearby time, then price, then input indices for deterministic tie-breaking.
  ``precision_count`` / ``recall_count`` = pairs over clone / wallet fills;
  ``precision_shares`` / ``recall_shares`` = sum of min(qty) over the pairs over each side's shares;
  scale-free ``precision_shares_scaled`` / ``recall_shares_scaled`` = the same with the clone's
  quantities multiplied by k (a pair counts up to the smaller of the two sizes).
  ``match_lag_s`` = median signed time difference (clone - wallet) of the pairs.
- position path: net Up inventory at the end of each second 0..L (wallet: opening position + its
  in-window fills). ``corr`` (pooled over window-seconds), ``mad`` (mean |clone - wallet|, shares),
  and ``end_corr`` / ``end_mad`` at the close. Scale-free ``mad_scaled`` / ``end_mad_scaled``:
  clone times k, divided by the wallet's mean gross volume per comparable window.
- P&L per window from in-window fills, both sides with the SAME outcome and the genome's fee model
  (taker / maker coefficient by role): ``corr``, ``slope`` (clone on wallet), ``level_ratio`` =
  sum clone / sum wallet; scale-free ``slope_scaled``, ``level_ratio_scaled`` (times k = the ratio
  of net per contract).
- k = wallet gross shares / clone gross shares over the eligible windows.
- A score with an empty denominator or zero variance is null, and null never supports PASS.

NULL BASELINE (same window, same market): a pseudo-wallet made of OTHER participants' prints.
In each window every timed wallet fill is given the time and price of a random print of the same
window that is not in a tx of the wallet and whose side fits (a maker fill that bought Up needs a
print whose taker sold Up, and so on), keeping the wallet's direction, role and size. So the null
has the wallet's count and direction mix but someone else's timing. A draw counts only if it could
place at least ``MIN_NULL_COVER`` of the wallet's fills; ``N_NULL`` draws. A clone that matches
anyone who trades (an always-on quoter) does not beat it.

VERDICT: ``THRESHOLDS`` and ``GATES`` below, PROVISIONAL, fixed before any real-data run.

Analysis only: nothing here is a decision input. The twin's decisions use data received by the
decision second; the wallet's fills are read only to remove its footprint and by the scorer.

The coverage receipt is a JSON object with ``wallet``, ``from`` and ``to`` (Unix seconds, exclusive
end), ``complete`` (true only after checking the source fill coverage), and ``order_tape_sha256``.
Use the file SHA-256 for JSONL or the reconstruction order-tape digest for a parquet directory.
Observed fills alone cannot establish completeness.
When a draft records ``evidence.fit_range``, the holdout must begin at or after that range ends.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import sys
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import orjson
import pyarrow.parquet as pq

from ..strategy.genome import Costs, Genome, describe, from_dict, validate
from ..twin.engine import SimConfig, fee_usd
from .census import window_of
from .flow import _ts
from .intlbook import px
from .intltwin import Footprint, IntlWindow, Key, Resting, run_intl, windows_from_tokens
from .ledger import TokenMap
from .ordermatch import filled_shares

TOL_PX = 0.01
TOL_S = 2.0
BLOCK_SLACK_S = 2      # a fill without a print traded up to ~2 s before its block (dossier's rule)
N_BOOT = 2000
N_NULL = 20
MIN_NULL_COVER = 0.5   # a null draw must place this share of the wallet's timed fills
EPS = 1e-9
INF = float("inf")

# ---- PREREGISTERED VERDICT (PROVISIONAL, 2026-11-26: set before any real-data run) -------------
# One row per score: (score, lowest allowed, highest allowed, must beat the null).
#   pass = the WHOLE 95% CI is inside [lowest, highest]
#   fail = the whole CI is outside it
#   open = anything else (the CI straddles a bound, or the score is undefined)
#   must beat the null: the score must also exceed EVERY one of the N_NULL pseudo-wallet values
#   (permutation test, p <= 1/21) and all N_NULL draws must exist; otherwise the row is open.
# PASS: every row passes. FAIL: any row fails. Otherwise INCONCLUSIVE, naming the open rows.
THRESHOLDS = (
    ("activity.agreement", 0.7, INF, False),
    ("fills.recall_count", 0.5, INF, True),
    ("fills.precision_count", 0.5, INF, True),
    ("fills.recall_shares_scaled", 0.5, INF, True),
    ("fills.precision_shares_scaled", 0.5, INF, True),
    ("pnl.corr", 0.3, INF, False),                  # the null keeps the wallet's directions, hence most of
                                                    # its P&L: it cannot test P&L, only timing and price
    ("pnl.level_ratio_scaled", 0.5, 2.0, False),    # net per contract within 2x of the wallet's
    ("path.end_mad_scaled", -INF, 0.5, False),      # closing inventory gap, in wallet volumes per window
)
# Data gates: (measured quantity, lowest allowed, highest allowed). One outside its range, or not
# measured, makes the verdict INCONCLUSIVE whatever the scores say.
GATES = (
    ("verified_holdout", 1, 1),            # a complete wallet order tape for the requested range
    ("comparable_starts", 30, INF),          # distinct window start times with a comparable window
    ("wallet_fills", 1, INF),                # scored wallet fills: none = no wallet data, not a silent wallet
    ("usable_rows_share", 0.9, 1.0),         # wallet rows that are verified and priced
    ("located_share", 0.9, 1.0),             # scored wallet fills timed by a print
    ("out_of_window_share", 0.0, 0.2),       # wallet volume before the open / after the close
    ("volume_share_scored", 0.8, 1.0),       # wallet volume in range that reached the scores
    ("print_lag_s", -0.5, 0.5),              # median receive - venue time of prints: the clocks agree
    ("side_conflict_share", 0.0, 0.01),      # prints whose two tokens disagree on the taker's side
    ("match_lag_s", -0.5, 0.5),              # median clone - wallet time of matched pairs (if any pair)
    ("maker_footprint_partial", 0, 0),       # MAKER genomes only: comparable windows where a filled maker
)                                            # order of the wallet could not be taken out of the book
OPTIONAL_GATES = {"match_lag_s", "maker_footprint_partial"}   # may be unmeasured (no pair / taker genome)


@dataclass(frozen=True, slots=True)
class Fill:
    t: float        # unix seconds
    d: int          # +1 more Up exposure, -1 less
    px: float       # Up-terms price, dollars
    qty: float      # shares
    role: str       # "maker" | "taker"
    timed: bool = True


@dataclass
class Wallet:
    fills: dict[Key, list[Fill]] = field(default_factory=dict)      # S <= t < E, sorted by time
    opening: dict[Key, float] = field(default_factory=dict)         # net Up shares bought before S
    rows: dict[str, int] = field(default_factory=dict)              # row counts by fate
    shares: dict[str, float] = field(default_factory=dict)          # shares by fate
    txs: set[str] = field(default_factory=set)                      # every tx with a row of the wallet


def code_sha256() -> str:
    """sha256 over the code a fidelity verdict depends on."""
    lab = Path(__file__).resolve().parent
    files = [lab / n for n in ("fidelity.py", "intltwin.py", "intlbook.py", "ordermatch.py")]
    if (lab / "research_manifest.py").is_file():
        files.append(lab / "research_manifest.py")
    files += sorted((lab.parent / "twin").glob("*.py"))
    h = hashlib.sha256()
    for f in files:
        h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


def order_tape_sha256(path: Path) -> str:
    """Hash a JSONL order tape or the sorted path -> SHA-256 map of its parquet parts."""
    def sha256(p: Path) -> str:
        with p.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()

    if not path.is_dir():
        return sha256(path)
    parts = {p.relative_to(path).as_posix(): sha256(p)
             for p in sorted(path.glob("day=*/part-*.parquet"))}
    return hashlib.sha256(orjson.dumps(parts, option=orjson.OPT_SORT_KEYS)).hexdigest()


def _order_tape_snapshot(path: Path) -> list[tuple[Path, int, int, int]]:
    parts = sorted(path.glob("day=*/part-*.parquet")) if path.is_dir() else [path]
    snapshot = []
    for part in parts:
        stat = part.stat()
        snapshot.append((part, stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return snapshot


def holdout_coverage(receipt: Path | None, orders: Path, wallet: str, t_from: int, t_to: int) -> dict:
    """Verify the wallet, holdout and artifact named by an explicit completeness receipt."""
    if receipt is None:
        return {"verified": False, "reason": "no coverage receipt"}
    raw = receipt.read_bytes()
    d = orjson.loads(raw)
    out = {"receipt": str(receipt), "receipt_sha256": hashlib.sha256(raw).hexdigest(), "verified": False}
    if not isinstance(d, dict) or not isinstance(d.get("wallet"), str) or d["wallet"].lower() != wallet:
        return out | {"reason": "coverage receipt is for a different wallet"}
    if type(d.get("from")) is not int or type(d.get("to")) is not int \
            or (d["from"], d["to"]) != (t_from, t_to):
        return out | {"reason": "coverage receipt does not cover the exact holdout"}
    if d.get("complete") is not True:
        return out | {"reason": "coverage receipt does not establish completeness"}
    digest = order_tape_sha256(orders)
    if d.get("order_tape_sha256") != digest:
        return out | {"reason": "coverage receipt order tape hash mismatch"}
    return out | {"verified": True, "order_tape_sha256": digest, "reason": None}


# ---------------------------------------------------------------- wallet fills and footprint
def load_rows(path: Path, wallet: str) -> list[dict]:
    """Every order-tape row of the txs in which ``wallet`` is a maker or the taker (the other
    rows of those txs price its taker fills). Parquet directory or JSONL."""
    rows: list[dict] = []
    if path.is_dir():
        for p in sorted(path.glob("day=*/part-*.parquet")):   # a tx never spans two parts
            part = pq.read_table(p).to_pylist()
            txs = {r["tx"] for r in part if r["maker"].lower() == wallet}
            rows += [r for r in part if r["tx"] in txs]
    else:
        with open(path, "rb") as fh:
            rows = [orjson.loads(line) for line in fh if line.strip()]
        txs = {r["tx"] for r in rows if r["maker"].lower() == wallet}
        rows = [r for r in rows if r["tx"] in txs]
    seen: set[tuple] = set()
    out = []
    for r in rows:
        k = (r["tx"], r["order_id"], r["role"])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def wallet_fills(rows: list[dict], wallet: str, wins: list[IntlWindow], tx_ms: dict[tuple[str, Key], int],
                 tmap: TokenMap, t_from: int, t_to: int, allow_unverified: bool = False) -> Wallet:
    """The wallet's fills per window of ``wins``, with everything that was left out counted."""
    tok = {t: (w, up) for w in wins for t, up in ((w.up, True), (w.down, False))}
    by_tx: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_tx[r["tx"]].append(r)
    out = Wallet()
    fills: dict[Key, list[Fill]] = defaultdict(list)
    opening: dict[Key, float] = defaultdict(float)
    rows_n: Counter = Counter()
    shares: dict[str, float] = defaultdict(float)
    for tx, rs in by_tx.items():
        makers = [r for r in rs if r["role"] == "maker" and r.get("limit")]
        for r in rs:
            if r["maker"].lower() != wallet:
                continue
            out.txs.add(tx)
            v = r.get("verified")
            if not r.get("limit"):
                rows_n["no_limit"] += 1
                continue
            if v is False or (v is None and not allow_unverified):
                rows_n["unverified" if v is False else "unchecked"] += 1
                continue
            if r["role"] == "maker":
                legs = [(float(r["limit"]), filled_shares(r))]
            elif makers and len(makers) == (r.get("n_makers") or len(makers)):
                legs = [(float(m["limit"]) if m["token_id"] == r["token_id"] else 1.0 - float(m["limit"]),
                         filled_shares(m)) for m in makers]
            else:
                rows_n["taker_unpriced"] += 1
                continue
            sh = sum(q for _, q in legs)
            hit = tok.get(r["token_id"])
            if hit is None:
                w0 = window_of(tmap.m.get(r["token_id"]) or {})
                if w0 is None:
                    why = "unknown_token_or_other_market" if t_from <= (r.get("block_ts") or 0) < t_to else None
                else:
                    why = "outside_universe" if t_from <= w0["start"] and w0["end"] <= t_to else None
                if why:
                    rows_n[why] += 1
                    shares[why] += sh
                continue
            w, is_up = hit
            ms = tx_ms.get((tx, w.key))
            t = ms / 1000.0 if ms is not None else float(r["block_ts"] - BLOCK_SLACK_S)
            d = (1 if r["side"] == "BUY" else -1) * (1 if is_up else -1)
            rows_n["used"] += 1
            if t < w.start:
                opening[w.key] += d * sh
                shares["pre_open"] += sh
            elif t >= w.end:
                shares["post_close"] += sh
            else:
                shares["in_window"] += sh
                fills[w.key] += [Fill(t, d, p if is_up else 1.0 - p, q, r["role"], ms is not None)
                                 for p, q in legs if q > 0 and 0.0 < p < 1.0]
    for fs in fills.values():
        fs.sort(key=lambda f: f.t)
    out.fills, out.opening = dict(fills), dict(opening)
    out.rows, out.shares = dict(rows_n), {k: round(v, 4) for k, v in shares.items()}
    return out


def footprint(rows: list[dict], wallet: str, wins: list[IntlWindow], om_rows: list[dict] | None
              ) -> tuple[Footprint, dict[Key, str]]:
    """What to take out of the tape, and why a window's footprint is only partly removable.
    ``om_rows``: doppel-ordermatch ``orders.jsonl`` rows (None = no ordermatch run)."""
    tok = {t: (w, up) for w in wins for t, up in ((w.up, True), (w.down, False))}
    fp = Footprint(taker_txs={r["tx"] for r in rows if r["maker"].lower() == wallet and r["role"] == "taker"})
    partial: dict[Key, str] = {}
    located: set[str] = set()
    for o in om_rows or ():
        hit = tok.get(o["token_id"])
        if hit is None or (o.get("maker") or wallet).lower() != wallet:
            continue
        w, is_up = hit
        if o["fill_before_placement"]:
            continue   # a proven wrong match: its order stays unlocated
        fills = tuple(sorted((int(ms), float(sh)) for _, ms, sh in o["fills_at"] if ms is not None))
        whole = o["outcome"] in ("filled", "cancelled") and o["end_ms"] is not None \
            and len(fills) == len(o["fills_at"])
        end = int(o["end_ms"] or 0) if whole else max([t for t, _ in fills], default=int(o["placed_venue_ms"]))
        side = o["side"] if is_up else ("SELL" if o["side"] == "BUY" else "BUY")
        fp.resting.setdefault(w.key, []).append(Resting(
            side, px(o["price"]) if is_up else px(1 - float(o["price"])), float(o["size"]),
            int(o["placed_tw_ns"]), end, fills))
        if whole:
            located.add(o["order_id"])
        else:
            partial[w.key] = "order_end_unknown"
    for r in rows:
        hit = tok.get(r["token_id"])
        if hit and r["maker"].lower() == wallet and r["role"] == "maker" and r["order_id"] not in located:
            partial.setdefault(hit[0].key, "order_not_located")
    return fp, partial


def clone_fills(res: dict) -> list[Fill]:
    return [Fill(res["start"] + f["t"], 1 if f["side"] == "buy" else -1, f["px"], f["qty"], f["role"])
            for f in res.get("fills") or []]


# ---------------------------------------------------------------- one window
def pnl_usd(fills: list[Fill], outcome: int, costs: Costs) -> float:
    cash = inv = fees = 0.0
    for f in fills:
        cash -= f.d * f.px * f.qty
        inv += f.d * f.qty
        fees += fee_usd(costs.taker_fee_coef if f.role == "taker" else costs.maker_fee_coef, f.px, f.qty,
                        costs.fee_rounding)
    return cash + inv * outcome - fees


def match(clone: list[Fill], wallet: list[Fill]) -> list[tuple[int, int]]:
    """Maximum-cardinality (clone index, wallet index) pairs. ``wallet`` is sorted by time."""
    wt = [f.t for f in wallet]
    choices: dict[int, list[tuple[float, float, int]]] = {}
    for i, c in enumerate(clone):
        cand = []
        for j in range(bisect.bisect_left(wt, c.t - TOL_S - EPS), bisect.bisect_right(wt, c.t + TOL_S + EPS)):
            w = wallet[j]
            if w.d == c.d and abs(w.px - c.px) <= TOL_PX + EPS:
                cand.append((abs(w.t - c.t), abs(w.px - c.px), j))
        if cand:
            choices[i] = sorted(cand)

    by_clone: dict[int, int] = {}
    by_wallet: dict[int, int] = {}
    for root in sorted(choices, key=lambda i: (*choices[i][0][:2], i)):
        # Visit alternating edges until reaching a free wallet fill. Reassigning earlier
        # pairs along this path leaves every paired clone matched and adds one more pair.
        queue = deque([root])
        seen_clones = {root}
        parent: dict[int, int] = {}
        free = None
        while queue and free is None:
            i = queue.popleft()
            for _, _, j in choices[i]:
                if j in parent:
                    continue
                parent[j] = i
                owner = by_wallet.get(j)
                if owner is None:
                    free = j
                    break
                if owner not in seen_clones:
                    seen_clones.add(owner)
                    queue.append(owner)
        while free is not None:
            i = parent[free]
            former = by_clone.get(i)
            by_clone[i] = free
            by_wallet[free] = i
            free = former
    return sorted(by_clone.items())


def inv_path(fills: list[Fill], start: int, length: int, opening: float = 0.0) -> np.ndarray:
    """Net Up inventory at the end of seconds 0..length (opening + fills at or before start + s)."""
    if not fills:
        return np.full(length + 1, opening)
    fs = sorted(fills, key=lambda f: f.t)
    cum = np.cumsum([f.d * f.qty for f in fs])
    i = np.searchsorted([f.t for f in fs], start + np.arange(length + 1), side="right")
    return opening + np.where(i > 0, cum[np.maximum(i, 1) - 1], 0.0)


def window_record(clone: list[Fill], wallet: list[Fill], start: int, end: int, outcome: int,
                  costs: Costs, k: float = 1.0, opening: float = 0.0) -> tuple[dict[str, float], list[float]]:
    """(additive per-window statistics: every score is a function of their sums over windows;
    signed time differences clone - wallet of the matched pairs)."""
    timed = [f for f in wallet if f.timed]
    pairs = match(clone, timed)
    x, y = inv_path(clone, start, end - start), inv_path(wallet, start, end - start, opening)
    ex, ey = float(x[-1]), float(y[-1])
    gx, gy = pnl_usd(clone, outcome, costs), pnl_usd(wallet, outcome, costs)
    tc, tw = bool(clone), bool(wallet)
    return {
        "one": 1.0, "both": float(tc and tw), "w_only": float(tw and not tc), "c_only": float(tc and not tw),
        "n_c": float(len(clone)), "n_w": float(len(timed)), "n_w_all": float(len(wallet)), "m": float(len(pairs)),
        "q_c": sum(f.qty for f in clone), "q_w": sum(f.qty for f in timed),
        "alloc": sum(min(clone[i].qty, timed[j].qty) for i, j in pairs),
        "alloc_k": sum(min(k * clone[i].qty, timed[j].qty) for i, j in pairs),
        "v_w": sum(f.qty for f in wallet),
        "p_n": float(len(x)), "p_x": float(x.sum()), "p_y": float(y.sum()), "p_xx": float(x @ x),
        "p_yy": float(y @ y), "p_xy": float(x @ y), "p_ad": float(np.abs(x - y).sum()),
        "p_ad_k": float(np.abs(k * x - y).sum()),
        "e_x": ex, "e_y": ey, "e_xx": ex * ex, "e_yy": ey * ey, "e_xy": ex * ey, "e_ad": abs(ex - ey),
        "e_ad_k": abs(k * ex - ey),
        "g_x": gx, "g_y": gy, "g_xx": gx * gx, "g_yy": gy * gy, "g_xy": gx * gy,
    }, [clone[i].t - timed[j].t for i, j in pairs]


# ---------------------------------------------------------------- scores
def _div(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(np.abs(b) > 1e-12, np.asarray(a, dtype=float) / np.where(np.abs(b) > 1e-12, b, 1.0), np.nan)


def _cov(n, sx, sy, sxx, syy, sxy):
    return sxy - _div(sx * sy, n), sxx - _div(sx * sx, n), syy - _div(sy * sy, n)


def _corr(n, sx, sy, sxx, syy, sxy):
    c, vx, vy = _cov(n, sx, sy, sxx, syy, sxy)
    with np.errstate(invalid="ignore"):
        return _div(c, np.sqrt(np.where((vx > 1e-12) & (vy > 1e-12), vx * vy, 0.0)))


def metrics(S: dict, k: float) -> dict[str, np.ndarray]:
    """Scores from column sums (scalars, or one value per bootstrap resample)."""
    n = S["one"]
    gc, gvx, _ = _cov(n, S["g_y"], S["g_x"], S["g_yy"], S["g_xx"], S["g_xy"])   # x = wallet
    slope = _div(gc, np.where(gvx > 1e-12, gvx, 0.0))
    level = _div(S["g_x"], S["g_y"])
    vpw = _div(S["v_w"], n)   # wallet gross shares per comparable window
    return {
        "activity.agreement": _div(S["both"], n),
        "activity.wallet_only": _div(S["w_only"], n),
        "activity.clone_only": _div(S["c_only"], n),
        "fills.precision_count": _div(S["m"], S["n_c"]),
        "fills.recall_count": _div(S["m"], S["n_w"]),
        "fills.precision_shares": _div(S["alloc"], S["q_c"]),
        "fills.recall_shares": _div(S["alloc"], S["q_w"]),
        "fills.precision_shares_scaled": _div(S["alloc_k"], k * S["q_c"]),
        "fills.recall_shares_scaled": _div(S["alloc_k"], S["q_w"]),
        "path.corr": _corr(S["p_n"], S["p_x"], S["p_y"], S["p_xx"], S["p_yy"], S["p_xy"]),
        "path.mad": _div(S["p_ad"], S["p_n"]),
        "path.mad_scaled": _div(_div(S["p_ad_k"], S["p_n"]), vpw),
        "path.end_corr": _corr(n, S["e_x"], S["e_y"], S["e_xx"], S["e_yy"], S["e_xy"]),
        "path.end_mad": _div(S["e_ad"], n),
        "path.end_mad_scaled": _div(S["e_ad_k"], S["v_w"]),
        "pnl.corr": _corr(n, S["g_x"], S["g_y"], S["g_xx"], S["g_yy"], S["g_xy"]),
        "pnl.slope": slope,
        "pnl.slope_scaled": slope * k,
        "pnl.level_ratio": level,
        "pnl.level_ratio_scaled": level * k,
    }


def _num(x) -> float | None:
    x = float(x)
    return round(x, 6) if np.isfinite(x) else None


def comparable(records: dict[Key, dict]) -> dict[Key, dict]:
    return {key: r for key, r in records.items() if r["both"] + r["w_only"] + r["c_only"] > 0}


def score(records: dict[Key, dict], k: float, n_boot: int = N_BOOT, seed: int = 0) -> dict[str, dict]:
    """{score: {"value", "ci95"}} over ``records`` (the comparable windows). The bootstrap resamples
    distinct START TIMES. ``ci95`` is None without a bootstrap, with fewer than 2 start times or
    when most resamples are undefined."""
    if not records:
        return {}
    cols = list(next(iter(records.values())))
    by_start: dict[int, np.ndarray] = {}
    for key, r in records.items():
        row = np.array([r[c] for c in cols])
        by_start[key[2]] = by_start[key[2]] + row if key[2] in by_start else row
    A = np.array(list(by_start.values()))
    point = metrics(dict(zip(cols, A.sum(0))), k)
    out = {name: {"value": _num(v), "ci95": None} for name, v in point.items()}
    if n_boot and len(A) >= 2:
        rng = np.random.default_rng(seed)
        chunk = max(1, 2_000_000 // A.size)   # resamples per batch: bounded memory on long runs
        parts = [A[rng.integers(0, len(A), (min(chunk, n_boot - i), len(A)))].sum(1)
                 for i in range(0, n_boot, chunk)]
        S = np.concatenate(parts)
        for name, v in metrics({c: S[:, i] for i, c in enumerate(cols)}, k).items():
            v = v[np.isfinite(v)]
            if len(v) >= 0.5 * n_boot:
                lo, hi = np.quantile(v, (0.025, 0.975))
                out[name]["ci95"] = [_num(lo), _num(hi)]
    return out


def pseudo_wallet(wallet: list[Fill], prints: list[tuple], rng: np.random.Generator) -> list[Fill]:
    """The wallet's timed fills re-timed and re-priced onto other participants' prints of the same
    window. ``prints``: (unix s, Up price, aggressor). A maker fill that bought Up needs a print
    whose taker SOLD Up; a taker fill that bought Up needs one whose taker BOUGHT."""
    pools: dict[str, list[tuple]] = {"buy": [], "sell": []}
    for i in rng.permutation(len(prints)):
        pools[prints[int(i)][2]].append(prints[int(i)])
    timed = [f for f in wallet if f.timed]
    out = []
    for i in rng.permutation(len(timed)):
        f = timed[int(i)]
        pool = pools[("sell" if f.d > 0 else "buy") if f.role == "maker" else ("buy" if f.d > 0 else "sell")]
        if pool:
            t, p, _ = pool.pop()
            out.append(Fill(t, f.d, p, f.qty, f.role))
    return sorted(out, key=lambda f: f.t)


def decide(scores: dict, null: dict | None, gates: dict) -> dict:
    """The preregistered verdict from the scores, the null and the measured gate quantities."""
    checks = []
    for name, lo, hi, beat in THRESHOLDS:
        m = scores.get(name) or {"value": None, "ci95": None}
        v, ci = m["value"], m["ci95"]
        ok = failed = False
        if ci is not None and None not in ci:
            ok, failed = lo <= ci[0] and ci[1] <= hi, ci[1] < lo or ci[0] > hi
        nl = (null or {}).get(name) or {}
        note = None
        if ok and beat and not (nl.get("n") == N_NULL and nl.get("max") is not None and v > nl["max"]):
            ok, note = False, "not above the null in every draw"
        checks.append({"score": name, "min": lo if lo > -INF else None, "max": hi if hi < INF else None,
                       "value": v, "ci95": ci, "null_max": nl.get("max"), "null_n": nl.get("n"),
                       "result": "pass" if ok else "fail" if failed else "open", "note": note})
    data = []
    for name, lo, hi in GATES:
        x = gates.get(name)
        if x is None:
            if name not in OPTIONAL_GATES:
                data.append(f"{name} not measured")
        elif not lo <= x <= hi:
            data.append(f"{name} {x} outside [{lo}, {hi}]")
    fails = [c["score"] for c in checks if c["result"] == "fail"]
    opens = [c["score"] for c in checks if c["result"] == "open"]
    if data:
        dec, binding, why = "INCONCLUSIVE", data, "data gate: " + "; ".join(data)
    elif fails:
        dec, binding, why = "FAIL", fails, "the whole CI is outside the threshold: " + ", ".join(fails)
    elif opens:
        dec, binding, why = "INCONCLUSIVE", opens, "binding: " + ", ".join(opens)
    else:
        dec, binding, why = "PASS", [], "every preregistered score passes"
    return {"decision": dec, "reason": why, "binding": binding, "checks": checks}


def evaluate(clone: dict[Key, list[Fill]], wallet: dict[Key, list[Fill]], windows: dict[Key, tuple[int, int, int]],
             costs: Costs, gates: dict | None = None, opening: dict[Key, float] | None = None,
             others: dict[Key, list[tuple]] | None = None, n_boot: int = N_BOOT, n_null: int = N_NULL,
             seed: int = 0) -> dict:
    """Scores, null baseline and verdict from normalised in-window fills. ``windows``: eligible
    window -> (start, end, outcome). ``others``: window -> other participants' prints (unix s, Up
    price, aggressor). ``gates``: the gate quantities measured outside (the rest is measured here).
    Pure: the same inputs always give the same document."""
    opening, others = opening or {}, others or {}
    v_c = sum(f.qty for k in windows for f in clone.get(k, []))
    v_w = sum(f.qty for k in windows for f in wallet.get(k, []))
    k = v_w / v_c if v_c > 0 else float("nan")
    records: dict[Key, dict] = {}
    lags: list[float] = []
    for key, (s, e, o) in windows.items():
        records[key], lg = window_record(clone.get(key, []), wallet.get(key, []), s, e, o, costs, k,
                                         opening.get(key, 0.0))
        lags += lg
    comp = comparable(records)
    scores = score(comp, k, n_boot, seed)
    rng = np.random.default_rng(seed + 1)
    need = sum(r["n_w"] for r in comp.values())
    draws: dict[str, list[float]] = defaultdict(list)
    n_done = 0
    for _ in range(n_null if need else 0):
        ps = {key: pseudo_wallet(wallet.get(key, []), others.get(key, []), rng) for key in windows}
        if sum(len(v) for v in ps.values()) < MIN_NULL_COVER * need:
            break   # too few other prints: no draw can stand in for the wallet
        n_done += 1
        recs = {key: window_record(clone.get(key, []), ps[key], s, e, o, costs, k)[0]
                for key, (s, e, o) in windows.items()}
        for name, m in score(comparable(recs), k, 0).items():
            if m["value"] is not None:
                draws[name].append(m["value"])
    null = ({name: {"mean": _num(np.mean(v)), "max": _num(np.max(v)), "n": len(v)} for name, v in draws.items()}
            if n_done else None)
    n_w = sum(r["n_w_all"] for r in comp.values())
    g = dict(gates or {})
    g["comparable_starts"] = len({key[2] for key in comp})
    g["wallet_fills"] = int(n_w)
    g["located_share"] = _num(need / n_w) if n_w else None
    g["match_lag_s"] = _num(np.median(lags)) if lags else None
    return {"windows": {"eligible": len(windows), "comparable": len(comp),
                        "neither_traded": len(windows) - len(comp)},
            "scale_k": _num(k), "clone_shares": _num(v_c), "wallet_shares": _num(v_w),
            "gates_measured": g, "scores": scores, "null": null, "null_draws": n_done,
            "verdict": decide(scores, null, g), "records": records}


# ---------------------------------------------------------------- run
def run(root: Path, lab: Path, g: Genome, wallet: str, orders: Path, t_from: int, t_to: int,
        cfg: SimConfig = SimConfig(), n_boot: int = N_BOOT, n_null: int = N_NULL, seed: int = 0,
        allow_unverified: bool = False, ordermatch_dir: Path | None = None,
        coverage_receipt: Path | None = None) -> tuple[dict, list[dict]]:
    """Returns (document, per-window rows)."""
    if t_from >= t_to:
        raise ValueError("holdout --to must be after --from")
    fit_range = None
    if isinstance(g.evidence, dict) and "fit_range" in g.evidence:
        fit_range = g.evidence["fit_range"]
        if (not isinstance(fit_range, dict) or type(fit_range.get("from")) is not int
                or type(fit_range.get("to")) is not int or fit_range["from"] >= fit_range["to"]):
            raise ValueError("genome evidence.fit_range must contain increasing integer Unix seconds 'from' and 'to'")
        if t_from < fit_range["to"]:
            raise ValueError(f"holdout starts at {t_from}, before genome evidence.fit_range ends at {fit_range['to']}")
    wallet = wallet.lower()
    orders = Path(orders)
    tmap = TokenMap(Path(lab) / "tokens.jsonl")
    wins, missing = windows_from_tokens(tmap, g.universe.assets, g.universe.horizons, t_from, t_to)
    snapshot = _order_tape_snapshot(orders) if coverage_receipt else None
    coverage = holdout_coverage(coverage_receipt, orders, wallet, t_from, t_to)
    rows = load_rows(orders, wallet)
    if snapshot is not None and snapshot != _order_tape_snapshot(orders):
        raise ValueError("the order tape changed while reading, retry with a complete snapshot")
    mine = [r["block_ts"] for r in rows if r["maker"].lower() == wallet and r.get("block_ts")]
    span = [min(mine), max(mine)] if mine else None
    om = None
    if ordermatch_dir is not None:
        match_path = Path(ordermatch_dir)
        if match_path.is_dir():
            match_path /= "orders.jsonl"
        om = [orjson.loads(line) for line in match_path.read_bytes().splitlines() if line.strip()]
    fp, partial = footprint(rows, wallet, wins, om)
    tx_ms: dict[tuple[str, Key], int] = {}
    prints: dict[Key, list[tuple]] = {}
    results = run_intl(root, g, wins, cfg, lab, {r["tx"] for r in rows}, tx_ms, fp, wallet, prints)
    wal = wallet_fills(rows, wallet, wins, tx_ms, tmap, t_from, t_to, allow_unverified)
    by_key = {(r["asset"], r["horizon"], r["start"]): r for r in results}
    elig = {k: r for k, r in by_key.items() if r["eligible"]}
    clone = {k: clone_fills(r) for k, r in elig.items()}
    others = {k: [(t, p, a) for t, p, _, a, tx in v if tx not in wal.txs] for k, v in prints.items()}
    lags = [r["tape"]["print_lag_s"][1] for r in elig.values() if r["tape"].get("print_lag_s")]
    n_pr = sum(r["tape"]["trades"] + r["tape"]["mirror_dup_prints"] for r in elig.values())
    conf = sum(r["tape"]["side_conflicts"] for r in elig.values())
    bad = sum(wal.rows.get(k, 0) for k in ("unverified", "unchecked", "no_limit", "taker_unpriced"))
    sh = wal.shares
    in_elig = sum(f.qty for k, fs in wal.fills.items() if k in elig for f in fs)
    out_win = sh.get("pre_open", 0.0) + sh.get("post_close", 0.0)
    total = sh.get("in_window", 0.0) + out_win + sh.get("outside_universe", 0.0) \
        + sh.get("unknown_token_or_other_market", 0.0)
    gates = {"verified_holdout": int(coverage["verified"]),
             "print_lag_s": _num(np.median(lags)) if lags else None,
             "usable_rows_share": _num(1 - bad / len(mine)) if mine else None,
             "out_of_window_share": _num(out_win / (sh.get("in_window", 0.0) + out_win))
             if sh.get("in_window", 0.0) + out_win > 0 else None,
             "volume_share_scored": _num(in_elig / total) if total > 0 else None,
             "side_conflict_share": _num(conf / n_pr) if n_pr else None}
    doc = evaluate(clone, wal.fills, {k: (r["start"], r["end"], r["outcome"]) for k, r in elig.items()},
                   g.costs, gates, wal.opening, others, n_boot, n_null, seed)
    records = doc.pop("records")
    comp = comparable(records)
    if g.mode == "maker":   # a maker's verdict needs the wallet's resting orders out of the book
        doc["gates_measured"]["maker_footprint_partial"] = sum(k in partial for k in comp)
        doc["verdict"] = decide(doc["scores"], doc["null"], doc["gates_measured"])
        if om is None:
            doc["verdict"]["reason"] += " (no --ordermatch-dir: none of the wallet's resting orders was removed)"
    if not coverage["verified"]:
        doc["verdict"]["reason"] += f" ({coverage['reason']})"
    base = SimConfig()
    diag = [n for n, on in (("--boot", n_boot != N_BOOT), ("--null-shuffles", n_null != N_NULL),
                            ("--allow-unverified", allow_unverified), ("--latency-ms", cfg.latency_ms != base.latency_ms),
                            ("--brti-delay-s", cfg.brti_delay_s != base.brti_delay_s),
                            ("--depth-levels", cfg.depth_levels != base.depth_levels)) if on]
    if diag:
        v = doc["verdict"]
        doc["verdict"] = v | {"decision": "DIAGNOSTIC", "would_be": v["decision"],
                              "reason": f"non-default {', '.join(diag)}: not evidence for or against the genome "
                                        f"(would be {v['decision']}: {v['reason']})"}
    excl = Counter(r["excluded"] for r in results if not r["eligible"])
    excl_w = Counter(r["excluded"] for k, r in by_key.items() if not r["eligible"] and wal.fills.get(k))
    both = [r for r in elig.values() if r["outcome_src"] == "payout" and r["brti_outcome"] is not None]
    doc = {
        "code_sha256": code_sha256(),
        "params": {"wallet": wallet, "from": t_from, "to": t_to, "tol_px": TOL_PX, "tol_s": TOL_S,
                   "n_boot": n_boot, "n_null": n_null, "seed": seed, "allow_unverified": allow_unverified,
                   "ordermatch_dir": str(ordermatch_dir) if ordermatch_dir else None, "twin": asdict(cfg)},
        "genome": {"id": g.id, "name": g.name, "mode": g.mode, "describe": describe(g), "fit_range": fit_range},
        "thresholds": [{"score": s, "min": lo if lo > -INF else None, "max": hi if hi < INF else None,
                        "must_beat_null": b} for s, lo, hi, b in THRESHOLDS],
        "gates": [{"quantity": n, "min": lo, "max": hi if hi < INF else None} for n, lo, hi in GATES],
        "provisional": True, "diagnostic": diag,
        "coverage": {
            "windows_in_range": len(wins) + len(missing), "windows_without_tokens": len(missing),
            "order_tape_block_ts": span, "verified_holdout": coverage,
            "windows_excluded": dict(excl), "of_which_wallet_traded": dict(excl_w),
            "wallet_rows": wal.rows, "wallet_shares": sh,
            "wallet_shares_in_excluded_windows": _num(sh.get("in_window", 0.0) - in_elig)},
        "footprint": {"ordermatch": om is not None, "taker_txs_dropped": len(fp.taker_txs),
                      "resting_orders_subtracted": sum(len(v) for v in fp.resting.values()),
                      "windows_partial": dict(Counter(partial.values())),
                      "comparable_windows_partial": sum(k in partial for k in comp),
                      "not_removable": "the wallet's never-filled quotes; liquidity its taker orders consumed"},
        "adapter": {"print_lag_s_median": gates["print_lag_s"], "prints": n_pr, "side_conflicts": conf,
                    "mirror_dup_prints": sum(r["tape"]["mirror_dup_prints"] for r in elig.values()),
                    "prints_bad_side": sum(r["tape"]["prints_bad_side"] for r in elig.values()),
                    "outcome_src": dict(Counter(r["outcome_src"] for r in elig.values())),
                    "payout_vs_brti": {"windows": len(both),
                                       "agree": sum(r["outcome"] == r["brti_outcome"] for r in both)}},
    } | doc
    out_rows = []
    for k, r in by_key.items():
        row = {"asset": k[0], "horizon": k[1], "start": k[2], "eligible": r["eligible"],
               "excluded": r["excluded"], "outcome": r["outcome"], "outcome_src": r.get("outcome_src"),
               "wallet_fills": len(wal.fills.get(k, [])), "wallet_opening": _num(wal.opening.get(k, 0.0)),
               "footprint_partial": partial.get(k)}
        if k in records:
            rec = records[k]
            row |= {"clone_fills": int(rec["n_c"]), "matched": int(rec["m"]), "clone_shares": _num(rec["q_c"]),
                    "wallet_shares": _num(rec["v_w"]), "clone_end_inventory": _num(rec["e_x"]),
                    "wallet_end_inventory": _num(rec["e_y"]), "clone_pnl_usd": _num(rec["g_x"]),
                    "wallet_pnl_usd": _num(rec["g_y"])}
        out_rows.append(row)
    out_rows.sort(key=lambda r: (r["start"], r["horizon"], r["asset"]))
    return doc, out_rows


def card(doc: dict) -> str:
    v, w, c = doc["verdict"], doc["windows"], doc.get("coverage", {})
    L = [doc["genome"]["describe"] if "genome" in doc else "",
         f"wallet {doc.get('params', {}).get('wallet')} | code {doc.get('code_sha256', '')[:12]}",
         f"VERDICT (PROVISIONAL thresholds): {v['decision']}: {v['reason']}",
         f"windows: {w['comparable']} comparable of {w['eligible']} eligible ({w['neither_traded']} traded by "
         f"neither); excluded {c.get('windows_excluded')}, without tokens {c.get('windows_without_tokens')}, "
         f"wallet rows span block_ts {c.get('order_tape_block_ts')}",
         f"wallet holdout coverage: {c.get('verified_holdout')}",
         f"shares: clone {doc['clone_shares']} wallet {doc['wallet_shares']} (k = {doc['scale_k']}); wallet "
         f"rows {c.get('wallet_rows')}; wallet shares {c.get('wallet_shares')}",
         f"footprint: {doc.get('footprint')}",
         f"gates measured: {doc['gates_measured']}", f"adapter: {doc.get('adapter')}", "",
         f"{'score':<32}{'value':>10}  {'CI95':<24}{'null mean':>11}{'null max':>11}"]
    null = doc["null"] or {}
    for name, m in doc["scores"].items():
        nm = null.get(name, {})
        L.append(f"{name:<32}{str(m['value']):>10}  {str(m['ci95']):<24}{str(nm.get('mean')):>11}{str(nm.get('max')):>11}")
    L += ["", "PREREGISTERED THRESHOLDS (PROVISIONAL): pass = whole CI inside, fail = whole CI outside",
          f"{'score':<32}{'min':>6}{'max':>6}  {'beat null':<10}{'result'}"]
    for ch, t in zip(v["checks"], THRESHOLDS):
        L.append(f"{t[0]:<32}{str(ch['min']):>6}{str(ch['max']):>6}  {('yes' if t[3] else 'no'):<10}{ch['result']}"
                 + (f" ({ch['note']})" if ch["note"] else ""))
    L.append("data gates (outside its range or unmeasured -> INCONCLUSIVE): "
             + "; ".join(f"{n} in [{lo}, {hi}]" for n, lo, hi in GATES))
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-fidelity")
    ap.add_argument("--data", type=Path, required=True, help="tape root (clob_<asset>_<hz>, ref_*)")
    ap.add_argument("--lab", type=Path, required=True, help="lab dir holding tokens.jsonl (read only)")
    ap.add_argument("--genome", type=Path, required=True)
    ap.add_argument("--wallet", required=True)
    inputs = ap.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--orders", type=Path,
                        help="doppel-ordertape output: parquet directory or JSONL (maker and taker rows)")
    inputs.add_argument("--reconstruct-manifest", type=Path,
                        help="load the order tape and wallet's matched orders from a reconstruction manifest")
    ap.add_argument("--ordermatch-dir", "--ordermatch", type=Path,
                    help="doppel-ordermatch directory or its orders.jsonl file: lives of the wallet's resting orders, "
                         "needed to take them out of the book; required for a MAKER genome's verdict")
    ap.add_argument("--coverage-receipt", type=Path,
                    help="wallet order-tape completeness JSON for the exact holdout and order tape SHA-256")
    ap.add_argument("--from", dest="t_from", type=_ts, required=True, help="unix s or ISO UTC")
    ap.add_argument("--to", dest="t_to", type=_ts, required=True)
    ap.add_argument("--out", type=Path, required=True, help="directory")
    ap.add_argument("--seed", type=int, default=0)
    dg = ap.add_argument_group("diagnostics (any non-default value marks the verdict DIAGNOSTIC)")
    dg.add_argument("--latency-ms", type=float, default=SimConfig().latency_ms)
    dg.add_argument("--brti-delay-s", type=int, default=SimConfig().brti_delay_s)
    dg.add_argument("--depth-levels", type=int, default=SimConfig().depth_levels)
    dg.add_argument("--boot", type=int, default=N_BOOT)
    dg.add_argument("--null-shuffles", type=int, default=N_NULL)
    dg.add_argument("--allow-unverified", action="store_true",
                    help="also use rows whose order hash the fill store could not check (verified null)")
    a = ap.parse_args(argv)
    if a.reconstruct_manifest and a.ordermatch_dir:
        ap.error("--ordermatch-dir cannot be combined with --reconstruct-manifest")
    g = from_dict(orjson.loads(a.genome.read_bytes()))
    errs = validate(g)
    if errs:
        print("genome invalid: " + "; ".join(errs), file=sys.stderr)
        return 2
    cfg = SimConfig(latency_ms=a.latency_ms, brti_delay_s=a.brti_delay_s, depth_levels=a.depth_levels)
    try:
        orders, matched = a.orders, a.ordermatch_dir
        if a.reconstruct_manifest:
            from .research_manifest import read_reconstruction

            files = read_reconstruction(a.reconstruct_manifest, a.wallet, a.t_from, a.t_to, exact=True)
            orders, matched = files.orders, files.matched_orders
        doc, rows = run(a.data, a.lab, g, a.wallet, orders, a.t_from, a.t_to, cfg, a.boot, a.null_shuffles,
                        a.seed, a.allow_unverified, matched, a.coverage_receipt)
    except (ValueError, OSError) as e:
        print(f"fidelity: {e}", file=sys.stderr)
        return 2
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "fidelity.json").write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
    with open(a.out / "windows.jsonl", "wb") as f:
        for r in rows:
            f.write(orjson.dumps(r) + b"\n")
    text = card(doc)
    (a.out / "fidelity.txt").write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
