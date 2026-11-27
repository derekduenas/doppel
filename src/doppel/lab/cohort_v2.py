"""Cohort v2: two preregistered skill slices beside the profit-blind cohort v1, frozen once.

    doppel-cohort-v2 --data TAPE --tokens TOKENS --out DIR [--freeze-at ISO] [--cohort-v1 PATH]

Writes ``DIR/cohort-v2.json`` (rule, params, seed, input hashes, members, sha256) and
``DIR/cohort-v2-stats.json`` (Period-A statistics of every wallet with >= 50 windows; its sha256 is
in the cohort file). Both are write-once. Cohort v1 and ``lab/scan.py`` are only read.

PREREGISTRATION (fixed before any v2 output exists; the values below are ``RULE``)

Period A is every RTDS trade with receive time ``tw`` AND venue time strictly before the freeze
``frozen_tw``; the freeze is an hour boundary on closed hours (default: the end of the last complete
hour). Period B (``tw >= frozen_tw``) is never read: RTDS and CLOB segments are opened only for
hours before the freeze, every record and mark is re-checked against ``frozen_tw``, and a window's
settlement is used only if ``end + 60 s <= freeze`` (flow's embargo).

Fills: ``flow.iter_rtds`` (both sockets, union by multiplicity, earliest receive time). Volume rank
is v1's metric: crypto-window notional sum(price * size) rounded to cents, among wallets with >= 5
distinct windows, ordered by (-notional, address); rank 1 = largest.

Markout (``lab.markout`` machinery, unchanged): each fill is a ``markout.Fill`` keyed by its token at
our receive time; ``markout.marks`` gives the GROSS signed markout in cents/contract against the mid
of the last intl CLOB book received at or before t + h (``MarketPath.mid_asof``; gaps and the end of
a token's feed make it unknown; at or after the window end it is the settlement), h = +2/+10/+60 s.
Venue fees are never subtracted, so fee/rebate regimes cannot drive selection.
Per wallet and horizon the unit is the window (event slug; Up and Down tokens are one window):
c_w = sum(qty * markout), Q_w = sum(qty) over its marked fills.
- Winsorizing: SNIPER statistics set the 3 largest c_w to the 4th largest; DONOR statistics set the
  3 smallest c_w to the 4th smallest (each slice clips the tail that would flatter it).
- Estimate mu = sum c_w / sum Q_w. t = mu / se, se from window residuals e_w = c_w - mu Q_w ordered
  by (window start, slug), Newey-West (Bartlett, lag L = floor(4 (G/100)^(2/9))):
  var = G/(G-1) * [sum e^2 + 2 sum_l (1 - l/(L+1)) sum_t e_t e_{t-l}] / (sum Q)^2.
  t is undefined with fewer than 20 marked windows or zero variance.
- Log-loss/Brier vs mid: NOT used. A fill reveals an inequality on the wallet's belief (a buy at p
  says p_w > p net of fee), not a probability, so no forecast can be scored honestly. Omitted.

Slices (disjoint, in this order):
- SNIPER (100, stored in rank order): volume ranks 101-2500, >= 50 windows, median per-window notional in [$50, $5000];
  rank by sniper t(+10 s) descending, tie-break sniper t(+60 s) (undefined = last), then address.
  Wallets without a defined t(+10 s) are not rankable.
- DONOR (50): any wallet with >= 50 windows, not a sniper; requires donor t(+2 s) < 0 and
  t(+10 s) < 0, and the donor-winsorized means at +2 s and +10 s negative in BOTH halves of
  Period A (split at the midpoint of [first Period-A RTDS hour, freeze) by window start; each half
  needs >= 10 marked windows). Ranked by max(t2, t10) ascending (most persistently negative), then
  t10, then address.
- CONTROLS (50): ``random.Random(20261127).sample`` of the wallets at volume ranks 101-2500 with
  >= 50 windows, minus both slices, in rank order; stored sorted.
A slice that cannot be filled is published short, with its shortfall recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import math
import random
import statistics as st
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import orjson

from ..tape.segment import atomic_write, segment_path
from .flow import EMBARGO_S, RTDS_STREAMS, iter_rtds, tape_hours
from .intlbook import IntlBook, Print, Reset, replay_messages
from .markout import Fill, MarketPath, marks
from .scan import scan_digest

NS = 1_000_000_000
HOUR = 3600
SEED = 20261127
HORIZONS = (2.0, 10.0, 60.0)
HK = ("2s", "10s", "60s")
V1_MIN_WINDOWS = 5
MIN_WINDOWS = 50
BAND = (101, 2500)
MEDIAN_USD = (50.0, 5000.0)
WINSOR_K = 3
MIN_MARKED = 20
MIN_HALF = 10
FINALIZE_LAG_S = 300          # a token's path is closed this long after its window end / last fill + 60 s
SCHEMA = "doppel-cohort-v2/1"
RULE = {
    "period_a": "RTDS trades with receive tw and venue ts strictly before frozen_tw (hour-aligned, closed hours)",
    "volume_metric": "crypto-window notional (price*size) on RTDS union, profit-blind (v1 metric; "
                     "flow.iter_rtds union, rounded to cents; ranked among wallets with >= 5 windows by (-usd, address))",
    "markout": "gross signed mid markout (cents/contract) at receive tw + h via lab.markout.marks; "
               "settlement only when end + 60 s <= freeze; no fee subtraction",
    "horizons_s": list(HORIZONS),
    "unit": "window (event slug); c_w = sum(qty*markout), Q_w = sum(qty)",
    "winsorize": {"k": WINSOR_K, "sniper": "3 largest c_w -> 4th largest", "donor": "3 smallest c_w -> 4th smallest"},
    "t_stat": "mu = sum c / sum Q; Newey-West (Bartlett, L = floor(4 (G/100)^(2/9))) on residuals c_w - mu Q_w "
              "ordered by window start, G/(G-1) correction",
    "min_marked_windows": MIN_MARKED,
    "probability_skill": "not used: a fill implies an inequality on belief, not a probability",
    "sniper": {"n": 100, "volume_ranks": list(BAND), "min_windows": MIN_WINDOWS, "median_window_usd": list(MEDIAN_USD),
               "rank": "t(+10s) desc, then t(+60s) desc (undefined last), then address"},
    "donor": {"n": 50, "min_windows": MIN_WINDOWS, "excludes": "sniper",
              "require": "t(+2s) < 0 and t(+10s) < 0; donor-winsorized +2s and +10s means < 0 in both halves "
                         f"(>= {MIN_HALF} marked windows per half; split at midpoint of Period A by window start)",
              "rank": "max(t2, t10) asc, then t10 asc, then address"},
    "controls": {"n": 50, "volume_ranks": list(BAND), "min_windows": MIN_WINDOWS, "excludes": "sniper, donor",
                 "draw": "random.Random(seed).sample(pool in rank order, n), stored sorted", "seed": SEED},
}


def _sha(path: Path) -> str:
    with Path(path).open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


# ---------------------------------------------------------------- Period A readers
def period_a_fills(root: Path, freeze_s: int):
    """Deduplicated RTDS crypto-window fills of Period A (hours before the freeze only)."""
    hours = [h for h in tape_hours(root, RTDS_STREAMS) if h < freeze_s]
    for _, fills in iter_rtds(root, hours):
        for f in fills:
            if f.tw < freeze_s * NS and f.ts < freeze_s:
                yield f


def volume_table(root: Path, freeze_s: int) -> dict[str, dict]:
    """Per wallet: notional (v1 metric), per-window notional, fill counts."""
    w: dict[str, dict] = defaultdict(lambda: {"usd": 0.0, "win": defaultdict(float), "fills": 0, "taker": 0,
                                              "fees": 0.0})
    for f in period_a_fills(root, freeze_s):
        a = w[f.wallet]
        usd = f.price * f.size
        a["usd"] += usd
        a["win"][f.slug] += usd
        a["fills"] += 1
        a["taker"] += f.taker
        a["fees"] += f.fee
    return dict(w)


def volume_ranks(table: dict[str, dict]) -> dict[str, int]:
    ws = [(a, round(v["usd"], 2)) for a, v in table.items() if len(v["win"]) >= V1_MIN_WINDOWS]
    ws.sort(key=lambda kv: (-kv[1], kv[0]))
    return {a: i + 1 for i, (a, _) in enumerate(ws)}


def load_outcomes(path: Path) -> dict[str, int | None]:
    """token -> 1/0 payout from a JSON {token: {outcome}} or the ledger's tokens.jsonl (TokenMap merge)."""
    raw = Path(path).read_bytes()
    try:
        doc = orjson.loads(raw)
    except orjson.JSONDecodeError:
        doc = None
    if isinstance(doc, dict) and "token" not in doc:
        return {str(k): (v or {}).get("outcome") for k, v in doc.items()}
    from .ledger import TokenMap
    out: dict[str, int | None] = {}
    for tok, info in TokenMap(Path(path)).m.items():
        pay = info.get("payout") if info.get("status") == "resolved" else None
        out[str(tok)] = int(pay) if pay in (0, 1, 0.0, 1.0) else None
    return out


def stream_marks(root: Path, stream: str, t_from: int, freeze_s: int, fills: dict[str, list],
                 outcomes: dict[str, int | None]):
    """Streaming ``markout.intl_paths`` over [t_from, freeze): a token's ``MarketPath`` is built from the
    same replay and closed FINALIZE_LAG_S after max(window end, last fill + 60 s), then each of its
    fills is marked with ``markout.marks``. Yields (fill row, {horizon key: gross markout | None}).
    ``fills``: token -> list of (Fill, wallet, slug, start, end)."""
    frozen = freeze_s * NS
    due = []
    for tok, rows in fills.items():
        end = rows[0][4]
        last = max(r[0].t_ns for r in rows)
        heapq.heappush(due, (max(end * NS, last + 60 * NS) + FINALIZE_LAG_S * NS, tok))
    book = IntlBook()
    paths: dict[str, MarketPath] = {}
    last_frame: dict[str, int] = {}

    def close(tok: str):
        rows = fills[tok]
        end = rows[0][4]
        out = outcomes.get(tok) if end + EMBARGO_S <= freeze_s else None
        p = paths.pop(tok, None) or MarketPath(key=tok, start=rows[0][3], end=end, outcome=None)
        p.end, p.outcome = end, out
        p.valid_until = min(last_frame.pop(tok, -1), frozen - 1)
        for r in rows:
            f = r[0]
            m = marks(f, p, HORIZONS, fair=False)["mid"]
            res = {}
            for h, k in zip(HORIZONS, HK):
                t = f.t_ns + int(round(h * NS))
                v = m.get(k)
                if t >= frozen and (t < end * NS or out is None):   # Period B is never a mark
                    v = None
                res[k] = v
            yield r, res

    now = t_from * NS
    for _, items in replay_messages(root, stream, t_from, freeze_s, book):
        touched = set()
        for it in items:
            now = max(now, it.tw_ns)
            if it.tw_ns >= frozen:
                raise AssertionError("Period-B CLOB record reached the selection code")
            if isinstance(it, Reset):
                for p in paths.values():
                    p.gaps.append(it.tw_ns)
                continue
            if it.asset not in fills:
                continue
            last_frame[it.asset] = it.tw_ns
            if isinstance(it, Print):
                continue
            touched.add((it.asset, it.tw_ns))
        for a, tw in sorted(touched):
            lv = book.levels.get(a)
            if lv is None:
                continue
            p = paths.get(a)
            if p is None:
                r0 = fills[a][0]
                p = paths[a] = MarketPath(key=a, start=r0[3], end=r0[4], outcome=None)
            bids = [px for (px, s), q in lv.items() if s == "BUY" and q > 0]
            asks = [px for (px, s), q in lv.items() if s == "SELL" and q > 0]
            p.tws.append(tw)
            p.bids.append(max(bids) if bids else None)
            p.asks.append(min(asks) if asks else None)
        while due and due[0][0] < now:
            yield from close(heapq.heappop(due)[1])
    while due:
        yield from close(heapq.heappop(due)[1])


# ---------------------------------------------------------------- statistics
def winsorized_t(cells: list[tuple[tuple, float, float]], tail: str) -> dict:
    """cells: (order key, c_w, Q_w) with Q_w > 0. Returns raw mean, winsorized mean and NW t."""
    G = len(cells)
    out = {"windows": G, "mean_c": None, "mean_w_c": None, "t": None}
    if G == 0:
        return out
    cells = sorted(cells)
    c = [x[1] for x in cells]
    q = [x[2] for x in cells]
    Q = sum(q)
    out["mean_c"] = round(sum(c) / Q, 6)
    if G > WINSOR_K:
        sign = 1.0 if tail == "top" else -1.0
        order = sorted(range(G), key=lambda i: (-sign * c[i], i))
        cap = c[order[WINSOR_K]]
        for i in order[:WINSOR_K]:
            c[i] = cap
    mu = sum(c) / Q
    out["mean_w_c"] = round(mu, 6)
    if G < MIN_MARKED:
        return out
    e = [ci - mu * qi for ci, qi in zip(c, q)]
    L = int(math.floor(4 * (G / 100) ** (2 / 9)))
    s = sum(x * x for x in e)
    for lag in range(1, L + 1):
        s += 2 * (1 - lag / (L + 1)) * sum(e[i] * e[i - lag] for i in range(lag, G))
    var = G / (G - 1) * s / (Q * Q)
    if var > 0 and math.isfinite(var):
        out["t"] = round(mu / math.sqrt(var), 6)
    return out


def wallet_stats(cells: dict[str, list[float]], starts: dict[str, int], split_s: int) -> dict:
    """cells: slug -> [num, den] * len(HORIZONS)."""
    res: dict = {}
    for j, k in enumerate(HK):
        rows = [((starts[s], s), v[2 * j], v[2 * j + 1]) for s, v in cells.items() if v[2 * j + 1] > 0]
        top, bot = winsorized_t(rows, "top"), winsorized_t(rows, "bottom")
        halves = [winsorized_t([r for r in rows if (r[0][0] < split_s) == first], "bottom")
                  for first in (True, False)]
        res[k] = {"windows": top["windows"], "mean_c": top["mean_c"],
                  "sniper": {"mean_w_c": top["mean_w_c"], "t": top["t"]},
                  "donor": {"mean_w_c": bot["mean_w_c"], "t": bot["t"],
                            "halves": [{"windows": h["windows"], "mean_w_c": h["mean_w_c"]} for h in halves]}}
    return res


def _donor_ok(s: dict) -> bool:
    for k in ("2s", "10s"):
        d = s[k]["donor"]
        if d["t"] is None or d["t"] >= 0:
            return False
        if any(h["windows"] < MIN_HALF or h["mean_w_c"] is None or h["mean_w_c"] >= 0 for h in d["halves"]):
            return False
    return True


def select(stats: dict[str, dict], n_sniper: int = 100, n_donor: int = 50, n_ctrl: int = 50,
           seed: int = SEED) -> dict:
    """Slices from per-wallet Period-A stats (every wallet with >= MIN_WINDOWS windows)."""
    lo, hi = BAND
    band = sorted((a for a, s in stats.items() if s["rank"] is not None and lo <= s["rank"] <= hi),
                  key=lambda a: stats[a]["rank"])
    sn_pool = [a for a in band if MEDIAN_USD[0] <= stats[a]["median_window_usd"] <= MEDIAN_USD[1]]
    rankable = [a for a in sn_pool if stats[a]["markout"]["10s"]["sniper"]["t"] is not None]

    def sk(a):
        m = stats[a]["markout"]
        t60 = m["60s"]["sniper"]["t"]
        return (-m["10s"]["sniper"]["t"], math.inf if t60 is None else -t60, a)
    sniper = sorted(rankable, key=sk)[:n_sniper]
    taken = set(sniper)
    qual = [a for a in sorted(stats) if a not in taken and _donor_ok(stats[a]["markout"])]

    def dk(a):
        m = stats[a]["markout"]
        t2, t10 = m["2s"]["donor"]["t"], m["10s"]["donor"]["t"]
        return (max(t2, t10), t10, a)
    donor = sorted(qual, key=dk)[:n_donor]
    taken |= set(donor)
    pool = [a for a in band if a not in taken]
    controls = sorted(random.Random(seed).sample(pool, min(n_ctrl, len(pool))))
    return {"sniper": sniper, "donor": donor, "controls": controls,
            "counts": {"min_windows_wallets": len(stats), "band_pool": len(band), "sniper_pool": len(sn_pool),
                       "sniper_rankable": len(rankable), "donor_qualifying": len(qual),
                       "control_pool": len(pool)},
            "shortfall": {"sniper": n_sniper - len(sniper), "donor": n_donor - len(donor),
                          "controls": n_ctrl - len(controls)}}


# ---------------------------------------------------------------- build
def build(root: Path, tokens: Path, freeze_s: int, *, cohort_v1: Path | None = None,
          n_sniper: int = 100, n_donor: int = 50, n_ctrl: int = 50, seed: int = SEED) -> tuple[dict, dict]:
    root = Path(root)
    if freeze_s % HOUR:
        raise ValueError("the freeze must be an hour boundary")
    if freeze_s * NS > time.time_ns():
        raise ValueError("the freeze must be in the past (closed hours only)")
    rtds_hours = [h for h in tape_hours(root, RTDS_STREAMS) if h < freeze_s]
    if not rtds_hours:
        raise ValueError("no RTDS tape before the freeze")
    first = rtds_hours[0]
    split_s = (first + freeze_s) // 2
    table = volume_table(root, freeze_s)
    ranks = volume_ranks(table)
    keep = {a for a, v in table.items() if len(v["win"]) >= MIN_WINDOWS}

    # Pass 2: fills of the >= MIN_WINDOWS wallets, grouped by CLOB stream and token.
    by_stream: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    starts: dict[str, int] = {}
    for f in period_a_fills(root, freeze_s):
        if f.wallet not in keep:
            continue
        mf = Fill(sys.intern(f.token), f.tw, "buy" if f.side == "BUY" else "sell", f.price, f.size, f.fee,
                  "taker" if f.taker else "maker", "receive")
        slug = sys.intern(f.slug)
        starts[slug] = f.start
        by_stream[f"clob_{f.coin}_{f.hz}"][mf.key].append((mf, sys.intern(f.wallet), slug, f.start, f.end))
    outcomes = load_outcomes(tokens)
    cells: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(lambda: [0.0] * (2 * len(HK))))
    clob_inputs: dict[str, str] = {}
    for stream in sorted(by_stream):
        hours = [h for h in tape_hours(root, [stream]) if h < freeze_s]
        if not hours:
            continue
        t_from = max(hours[0], first - HOUR)
        for h in range(t_from, freeze_s, HOUR):
            p = segment_path(root, stream, h * NS)
            if p.exists():
                clob_inputs[_rel(root, p)] = _sha(p)
        for (f, wallet, slug, _, _), m in stream_marks(root, stream, t_from, freeze_s, by_stream[stream], outcomes):
            c = cells[wallet][slug]
            for j, k in enumerate(HK):
                if m[k] is not None:
                    c[2 * j] += f.qty * m[k]
                    c[2 * j + 1] += f.qty
    rtds_inputs = {}
    for h in rtds_hours:
        for s in RTDS_STREAMS:
            p = segment_path(root, s, h * NS)
            if p.exists():
                rtds_inputs[_rel(root, p)] = _sha(p)
    have = set(rtds_hours)
    missing = [h for h in range(first, freeze_s, HOUR) if h not in have]

    stats: dict[str, dict] = {}
    for a in sorted(keep):
        v = table[a]
        stats[a] = {"rank": ranks.get(a), "usd": round(v["usd"], 2), "n_windows": len(v["win"]),
                    "median_window_usd": round(st.median(v["win"].values()), 4), "fills": v["fills"],
                    "taker_share": round(v["taker"] / v["fills"], 4), "fees_usd": round(v["fees"], 4),
                    "markout": wallet_stats(cells.get(a, {}), starts, split_s), "brier": None}
    sel = select(stats, n_sniper, n_donor, n_ctrl, seed)
    for a, s in stats.items():
        s["slice"] = ("sniper" if a in sel["sniper"] else "donor" if a in sel["donor"]
                      else "control" if a in sel["controls"] else None)
    stats_doc = {"schema": SCHEMA + "/stats", "frozen_tw": freeze_s * NS, "wallets": stats}

    inputs: dict = {"rtds": rtds_inputs, "clob": clob_inputs, "tokens": _sha(tokens)}
    overlap = None
    if cohort_v1 is not None and Path(cohort_v1).is_file():
        v1 = orjson.loads(Path(cohort_v1).read_bytes())
        inputs["cohort_v1"] = _sha(cohort_v1)
        v1c, v1k = set(v1.get("candidates") or []), set(v1.get("controls") or [])
        overlap = {k: {"v1_candidates": sorted(set(sel[k]) & v1c), "v1_controls": sorted(set(sel[k]) & v1k)}
                   for k in ("sniper", "donor", "controls")}
        overlap["v1_sha256"] = v1.get("sha256")
    code = Path(__file__).read_bytes()
    rule = {**RULE, "sniper": {**RULE["sniper"], "n": n_sniper}, "donor": {**RULE["donor"], "n": n_donor},
            "controls": {**RULE["controls"], "n": n_ctrl, "seed": seed}}
    body = {"schema": SCHEMA, "rule": rule, "seeds": {"controls": seed}, "frozen_tw": freeze_s * NS,
            "period_a": {"from_tw": first * NS, "until_tw": freeze_s * NS, "split_tw": split_s * NS,
                         "rtds_hours": len(rtds_hours), "rtds_hours_missing": missing},
            "eligible": sel["counts"], "shortfall": sel["shortfall"],
            "sniper": sel["sniper"], "donor": sel["donor"], "controls": sel["controls"],
            "v1_overlap": overlap, "inputs": inputs,
            "code_sha256": hashlib.sha256(code).hexdigest(),
            "stats_file": "cohort-v2-stats.json", "stats_sha256": scan_digest(stats_doc)}
    return {**body, "sha256": scan_digest(body)}, stats_doc


def verify(doc: dict, stats_doc: dict | None = None) -> None:
    if doc.get("sha256") != scan_digest({k: v for k, v in doc.items() if k != "sha256"}):
        raise ValueError("cohort-v2 checksum mismatch")
    if stats_doc is not None and doc["stats_sha256"] != scan_digest(stats_doc):
        raise ValueError("cohort-v2 stats checksum mismatch")
    groups = [set(doc[k]) for k in ("sniper", "donor", "controls")]
    if sum(map(len, groups)) != len(set().union(*groups)):
        raise ValueError("cohort-v2 slices overlap")


def _freeze_s(iso: str | None) -> int:
    if iso is None:
        return int(time.time()) // HOUR * HOUR
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-cohort-v2",
                                 description="Freeze the preregistered cohort v2 (sniper, donor, controls).")
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--tokens", type=Path, required=True, help="token outcomes: tokens.jsonl or JSON {token: {outcome}}")
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    ap.add_argument("--freeze-at", help="ISO hour boundary (UTC if no offset); default: end of the last complete hour")
    ap.add_argument("--cohort-v1", type=Path, help="v1 cohort for the overlap report (default DIR/cohort-v1.json)")
    a = ap.parse_args(argv)
    cohort_p, stats_p = a.out / "cohort-v2.json", a.out / "cohort-v2-stats.json"
    try:
        for p in (cohort_p, stats_p):
            if p.exists():
                raise FileExistsError(p)
        v1 = a.cohort_v1 if a.cohort_v1 is not None else a.out / "cohort-v1.json"
        doc, stats_doc = build(a.data, a.tokens, _freeze_s(a.freeze_at), cohort_v1=v1)
        verify(doc, stats_doc)
        atomic_write(stats_p, orjson.dumps(stats_doc, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS), replace=False)
        atomic_write(cohort_p, orjson.dumps(doc, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS), replace=False)
    except (ValueError, OSError) as exc:
        print(f"doppel-cohort-v2: {exc}", file=sys.stderr)
        return 1
    print(f"cohort-v2 frozen_tw={doc['frozen_tw']} sha256={doc['sha256'][:16]} sniper={len(doc['sniper'])} "
          f"donor={len(doc['donor'])} controls={len(doc['controls'])} eligible={doc['eligible']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
