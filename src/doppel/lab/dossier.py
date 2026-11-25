"""Wallet dossier: how one Polymarket wallet trades crypto Up/Down windows, fill by fill.

Joins the wallet's Goldsky fills (fill store) with our intl CLOB tape (``clob_{asset}_{hz}``),
raw 1 Hz BRTI and the PM US settlement series, then summarises mechanism, edge, order lifecycle,
inventory policy, counterparties, and a first policy inversion (can a small model say WHEN the
wallet takes, from data it had at that second?).

Clocks and look-ahead (read this before trusting any number)
-------------------------------------------------------------
* Book state is indexed on OUR receive time ``tw`` (ns). A quote is "as of t" if it was received
  strictly before t and is at most ``MAX_AGE_S`` old; otherwise it is null. The server
  ``timestamp`` (ms) is kept only to report ``tw - timestamp`` per stream (clock + latency check).
* Fill time: the ms ``timestamp`` of the ``last_trade_price`` print with the fill's tx hash
  (``time_src="print_ms"``), else ``block_timestamp`` (``"block_s"``). A block-time fill really
  happened up to ~2 s BEFORE its block (see anticipation.py), so for ``block_s`` fills every
  PRE-TRADE input (book, fair, inventory, panel label second) uses ``t_dec = t - BLOCK_SLACK_S``.
  Print-time fills use ``t_dec = t``.
* fair_v1 (BTC only) is evaluated at second ``floor(t_dec) - 1`` with priceToBeat = A(start-1) from
  the published series; before the window opens priceToBeat is unknown, so fair is null.
* Everything labelled markout / settlement / luck is an OUTCOME, computed with future data on
  purpose. It is never a feature.

Sign conventions: ``dir = +1`` for BUY, ``-1`` for SELL of the traded token. ``edge_c =
100 * dir * (fair_token - price)`` with fair_token = fair_up (Up, idx 0) or 1 - fair_up (Down).
Markout k: ``100 * dir * (mid(t + k) - price)``. Settlement: ``100 * dir * (payout - price)``.
All in cents per share. ``udir`` (Up exposure) = +1 for BUY Up / SELL Down, -1 otherwise.

Outputs (``doppel-dossier``): ``LAB/dossiers/<wallet>.json`` (all statistics), ``<wallet>.md``
(human card), and ``<wallet>.{fills,orders,windows}.parquet`` (one row per enriched fill, per
order_id, per window with its net position at close).

The intl windows do not necessarily settle on BRTI; ``settlement_basis`` reports how often the
PM US BRTI series' outcome agrees with the token payouts for this wallet's BTC windows. A low
agreement means fair_v1 is the wrong basis for these markets.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import statistics as st
import sys
import time
from array import array
from collections import Counter, defaultdict
from collections.abc import Iterable
from pathlib import Path

import numpy as np
import orjson

from ..tape.segment import read_segment
from .census import window_of
from .fair import fair_up_raw, phi, to_array
from .fillstore import compact, iter_wallet_fills, wallet_token_notional, wallet_tokens
from .ledger import TokenMap

ASSETS = ("btc", "eth", "sol", "xrp")
HZS = ("5m", "15m")
STREAMS = tuple(f"clob_{a}_{h}" for a in ASSETS for h in HZS)
NS = 1_000_000_000
MAX_AGE_S = 3.0
BLOCK_SLACK_S = 2  # a block_s fill traded up to ~2 s before its block timestamp
MARKOUTS = (1, 5, 30, 60)
VOL_WINDOW = 300  # fair_up_raw default
CLOCK_SAMPLE_EVERY = 25
# A print must precede its fill's block by PRINT_LEAD_S = (lo, hi) seconds (block_ts - print_s);
# otherwise the print clock is not trusted and the fill falls back to block time (counted).
PRINT_LEAD_S = (-5.0, 120.0)  # keep every Nth (tw - server ts) sample per stream

# policy inversion
FEATURES = ("edge_up", "edge_dn", "fair_up", "tte", "mv1", "mv5", "mv30", "spread_up", "inv")
TRAIN_FRAC = 0.7
MIN_POS_TRAIN, MIN_POS_TEST = 30, 10  # "took" rows
MIN_SIDE_TRAIN, MIN_SIDE_TEST = 15, 5  # per side, given took
SEED = 20261125


# ---------------------------------------------------------------- helpers
def _f(x: object) -> float:
    """Price string/number -> float; empty or missing -> NaN."""
    if x is None or x == "":
        return math.nan
    try:
        return float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return math.nan


def _bid(x: float) -> float:
    return x if x > 0 else math.nan  # 0 means an empty bid side


def _ask(x: float) -> float:
    return x if 0 < x < 1 else math.nan  # >= 1 (or 0) means an empty ask side


def _r(x, n: int = 4):
    if x is None:
        return None
    x = float(x)
    return round(x, n) if math.isfinite(x) else None


def _q(xs: list[float], qs=(0.1, 0.5, 0.9)) -> dict | None:
    v = [x for x in xs if x is not None and math.isfinite(x)]
    if not v:
        return None
    a = np.asarray(v, dtype=float)
    return {f"p{int(q * 100)}": _r(np.quantile(a, q)) for q in qs} | {"n": len(v)}


def _wmean(vals: list[float], wts: list[float]) -> float | None:
    num = den = 0.0
    for v, w in zip(vals, wts):
        if v is not None and math.isfinite(v):
            num += v * w
            den += w
    return num / den if den else None


# ---------------------------------------------------------------- book index
class BookIndex:
    """Per asset_id: receive-time sorted (tw_ns, best_bid, best_ask). Per tx: prints."""

    def __init__(self) -> None:
        self._tw: dict[str, array] = defaultdict(lambda: array("q"))
        self._bid: dict[str, array] = defaultdict(lambda: array("d"))
        self._ask: dict[str, array] = defaultdict(lambda: array("d"))
        self.books: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        self.prints: dict[str, list[tuple[int, float, float, str, str]]] = defaultdict(list)
        self.market_of: dict[str, str] = {}
        self.clock: dict[str, list[float]] = defaultdict(list)
        self.counts: Counter = Counter()

    # -- building
    def add_quote(self, asset: str, tw: int, bid: float, ask: float,
                  market: str | None = None) -> None:
        self._tw[asset].append(int(tw))
        self._bid[asset].append(_bid(bid))
        self._ask[asset].append(_ask(ask))
        if market:
            self.market_of[asset] = market

    def add_print(self, tx: str, ms: int, price: float, size: float, side: str, asset: str) -> None:
        self.prints[tx].append((int(ms), price, size, side, asset))

    def finalize(self) -> BookIndex:
        for a in list(self._tw):
            tw = np.frombuffer(self._tw[a], dtype=np.int64)
            o = np.argsort(tw, kind="stable")
            self.books[a] = (tw[o].copy(), np.frombuffer(self._bid[a], dtype=np.float64)[o].copy(),
                             np.frombuffer(self._ask[a], dtype=np.float64)[o].copy())
        self._tw.clear()
        self._bid.clear()
        self._ask.clear()
        return self

    # -- reading
    def quote(self, asset: str, t_ns: int, max_age_s: float = MAX_AGE_S):
        """(bid, ask) received STRICTLY before t_ns and at most max_age_s old, else None.
        Either side may be NaN (empty side)."""
        b = self.books.get(asset)
        if b is None:
            return None
        tw = b[0]
        i = int(np.searchsorted(tw, t_ns, side="left")) - 1
        if i < 0 or t_ns - int(tw[i]) > max_age_s * NS:
            return None
        return float(b[1][i]), float(b[2][i])

    def print_for(self, tx: str | None, asset: str | None):
        ps = self.prints.get(tx or "")
        if not ps:
            return None
        for p in ps:
            if p[4] == asset:
                return p
        return ps[0]

    def sibling(self, asset: str) -> str | None:
        m = self.market_of.get(asset)
        if m is None:
            return None
        for a, mm in self.market_of.items():
            if mm == m and a != asset:
                return a
        return None

    def clock_report(self) -> dict:
        out = {}
        for s, xs in sorted(self.clock.items()):
            if xs:
                a = np.asarray(xs)
                out[s] = {"n": len(xs), "tw_minus_server_ms_p50": _r(np.median(a), 1),
                          "p10": _r(np.quantile(a, 0.1), 1), "p90": _r(np.quantile(a, 0.9), 1)}
        return out


def ingest_message(idx: BookIndex, m: dict, tw: int, stream: str = "",
                   markets: set[str] | None = None, txs: set[str] | None = None) -> None:
    """One decoded CLOB market-channel message (book / price_change / last_trade_price)."""
    if not isinstance(m, dict):
        return
    if markets is not None and m.get("market") not in markets:
        return
    et = m.get("event_type")
    ts = m.get("timestamp")
    srv = int(ts) if ts not in (None, "") else -1
    if srv > 0 and idx.counts[f"msg:{stream}"] % CLOCK_SAMPLE_EVERY == 0:
        idx.clock[stream].append(tw / 1e6 - srv)
    idx.counts[f"msg:{stream}"] += 1
    mk = m.get("market")
    if et == "book":
        bids = [_f(lv.get("price")) for lv in m.get("bids") or [] if _f(lv.get("size")) > 0]
        asks = [_f(lv.get("price")) for lv in m.get("asks") or [] if _f(lv.get("size")) > 0]
        bids = [x for x in bids if math.isfinite(x)]
        asks = [x for x in asks if math.isfinite(x)]
        idx.add_quote(m["asset_id"], tw, max(bids) if bids else math.nan,
                      min(asks) if asks else math.nan, mk)
        idx.counts["book"] += 1
    elif et == "price_change":
        chs = m.get("price_changes")
        if chs is None:
            idx.counts["price_change_unsupported"] += 1  # older shape without best_bid/ask
            return
        for ch in chs:
            if "best_bid" not in ch and "best_ask" not in ch:
                idx.counts["price_change_no_best"] += 1
                continue
            idx.add_quote(ch["asset_id"], tw, _f(ch.get("best_bid")), _f(ch.get("best_ask")), mk)
            idx.counts["price_change"] += 1
    elif et == "last_trade_price":
        tx = m.get("transaction_hash")
        if mk and m.get("asset_id"):
            idx.market_of.setdefault(m["asset_id"], mk)
        if tx and (txs is None or tx in txs) and srv > 0:
            idx.add_print(tx, srv, _f(m.get("price")), _f(m.get("size")), str(m.get("side")),
                          str(m.get("asset_id")))
            idx.counts["print"] += 1


def build_index(root: Path, markets: set[str] | None = None, txs: set[str] | None = None,
                streams: Iterable[str] = STREAMS) -> BookIndex:
    """Stream the CLOB tape one segment at a time; keep only the wanted markets' best quotes and
    the wanted transactions' prints."""
    idx = BookIndex()
    for s in streams:
        for f in sorted((Path(root) / s).glob("*/*.dtape.zst")):
            for rec in read_segment(f, verify=True):
                raw = rec.get("raw") or ""
                if not raw or raw[0] not in "[{":
                    continue  # ctl records, PONGs
                try:
                    obj = orjson.loads(raw)
                except orjson.JSONDecodeError:
                    idx.counts["bad_json"] += 1
                    continue
                for m in obj if isinstance(obj, list) else [obj]:
                    ingest_message(idx, m, int(rec["tw"]), s, markets, txs)
    return idx.finalize()


# ---------------------------------------------------------------- fair (fast, exact)
_WCACHE: dict[int, float] = {}


def _wsum(r: int) -> float:
    """sum_{k=d+1}^{hi} ((hi - max(k, hi-59) + 1) / 60)^2 for r = hi - d, in fair_up_raw's order."""
    v = _WCACHE.get(r)
    if v is None:
        hi = 0
        lo = hi - 59
        v = 0.0
        for k in range(hi - r + 1, hi + 1):
            w = (hi - max(k, lo) + 1) / 60.0
            v += w * w
        _WCACHE[r] = v
    return v


def fair_up_at(raw: np.ndarray, t0: int, ptb: float | None, d: int, end: int, lag: int = 0,
               vol_window: int = VOL_WINDOW) -> float | None:
    """Same value as ``fair.fair_up_raw`` (tested for equality) with the variance weight sum
    cached; None when priceToBeat is unknown."""
    if ptb is None or not math.isfinite(ptb):
        return None
    lo_s, hi_s = end - 60 - lag, end - 1 - lag
    i_d = d - t0
    if i_d - vol_window < 0 or i_d >= len(raw) or np.isnan(raw[i_d]):
        return None
    hist = raw[i_d - vol_window:i_d + 1]
    if np.isnan(hist).any():
        return None
    sig2 = float(np.mean(np.diff(hist) ** 2))
    known = [raw[s - t0] for s in range(lo_s, min(d, hi_s) + 1)]
    if any(np.isnan(known)):
        return None
    m = max(0, hi_s - max(d, lo_s - 1))
    mean = (sum(known) + m * raw[i_d]) / 60.0
    var = _wsum(max(0, hi_s - d)) * sig2
    diff = mean - ptb
    if var <= 0:
        return 1.0 if diff >= 0 else 0.0
    return phi(diff / math.sqrt(var))


class Brti:
    """Raw BRTI as an array + the published settlement series (BTC only)."""

    def __init__(self, raw: dict[int, float], series: dict[int, float], lag: int = 0):
        self.t0, self.a = to_array(raw) if raw else (0, np.zeros(0))
        self.series = series
        self.lag = lag

    def ptb(self, start: int) -> float | None:
        return self.series.get(start - 1)

    def fair(self, start: int, end: int, d: int) -> float | None:
        if d < start - 1 or not len(self.a):  # priceToBeat A(start-1) not yet published
            return None
        return fair_up_at(self.a, self.t0, self.ptb(start), d, end, self.lag)

    def at(self, s: int) -> float:
        i = s - self.t0
        return float(self.a[i]) if 0 <= i < len(self.a) else math.nan


# ---------------------------------------------------------------- per fill
def enrich(rows: list[dict], tmap: TokenMap, idx: BookIndex, brti: Brti | None) -> tuple[list[dict], dict]:
    """Per-fill record for every fill in a crypto Up/Down window with a known token side.
    Returns (fills, counts): skipped rows by reason, plus ``print_rejected_clock``."""
    out: list[dict] = []
    skip = Counter()
    for r in rows:
        info = tmap.m.get(r["token_id"]) or {}
        w = window_of(info)
        if not info or info.get("status") == "unknown":
            skip["unknown_token"] += 1
            continue
        if not w:
            skip["not_crypto_window"] += 1
            continue
        if info.get("idx") not in (0, 1):
            skip["unknown_side"] += 1
            continue
        tok, i = r["token_id"], int(info["idx"])
        side = r["side"]
        d = 1 if side == "BUY" else -1
        sh, usd = float(r["amount_shares"]), float(r["amount_usdc"])
        price = float(r["price"]) if r.get("price") is not None else usd / sh
        tx = r.get("transaction_hash")
        pr = idx.print_for(tx, tok)
        if pr is not None and not (PRINT_LEAD_S[0] <= int(r["block_timestamp"]) - pr[0] / 1000
                                   <= PRINT_LEAD_S[1]):
            skip["print_rejected_clock"] += 1  # counted, but the fill is kept on block time
            pr = None
        if pr is not None:
            t_ns, src = pr[0] * 1_000_000, "print_ms"
            t_dec_ns = t_ns
        else:
            t_ns, src = int(r["block_timestamp"]) * NS, "block_s"
            t_dec_ns = t_ns - BLOCK_SLACK_S * NS
        f: dict = {"id": r["id"], "tx": tx, "order_id": r.get("order_id"), "token": tok, "idx": i,
                   "slug": w["slug"], "asset": w["asset"], "hz": w["hz"], "start": w["start"],
                   "end": w["end"], "side": side, "dir": d, "udir": d if i == 0 else -d,
                   "role": r.get("order_type"), "shares": sh, "usd": usd, "price": price,
                   "fee": float(r.get("fee") or 0.0),
                   "cp": (r.get("counterparty_address") or "").lower() or None,
                   "t": t_ns / NS, "t_dec": t_dec_ns / NS, "time_src": src,
                   "block_ts": int(r["block_timestamp"]), "tte": w["end"] - t_dec_ns / NS}
        q = idx.quote(tok, t_dec_ns)
        bid, ask = q if q else (math.nan, math.nan)
        mid = (bid + ask) / 2 if q and math.isfinite(bid) and math.isfinite(ask) else math.nan
        f.update(bid=bid, ask=ask, mid=mid, spread=ask - bid, has_book=q is not None,
                 vs_mid_c=100 * d * (mid - price))
        fu = None
        if brti is not None and w["asset"] == "btc":
            fu = brti.fair(w["start"], w["end"], math.floor(t_dec_ns / NS) - 1)
        ft = (fu if i == 0 else 1 - fu) if fu is not None else math.nan
        f.update(fair_up=fu if fu is not None else math.nan, fair_tok=ft,
                 edge_c=100 * d * (ft - price))
        for k in MARKOUTS:  # OUTCOME labels
            qk = idx.quote(tok, t_ns + k * NS)
            mk = (qk[0] + qk[1]) / 2 if qk else math.nan
            f[f"mo{k}_c"] = 100 * d * (mk - price)
        pay = info.get("payout") if info.get("status") == "resolved" else None
        f["payout"] = pay
        f["settle_c"] = 100 * d * (pay - price) if pay is not None else math.nan
        f["settle_usd"] = d * (pay * sh - usd) if pay is not None else math.nan  # OUTCOME
        out.append(f)
    out.sort(key=lambda x: (x["t"], x["id"]))
    return out, dict(skip)


# ---------------------------------------------------------------- per order
def orders(fills: list[dict]) -> tuple[dict, list[dict]]:
    """(summary, one record per order_id)."""
    by: dict[str, list[dict]] = defaultdict(list)
    no_id = 0
    for f in fills:
        if f["order_id"]:
            by[f["order_id"]].append(f)
        else:
            no_id += 1
    recs = []
    for oid, fs in by.items():
        fs.sort(key=lambda x: x["t"])
        roles = Counter(x["role"] for x in fs)
        sh = sum(x["shares"] for x in fs)
        first = fs[0]
        rec = {"order_id": oid, "n": len(fs), "shares": sh,
               "vwap": sum(x["usd"] for x in fs) / sh if sh else math.nan,
               "prices": len({round(x["price"], 6) for x in fs}),
               "first": first["t"], "last": fs[-1]["t"], "lifetime": fs[-1]["t"] - first["t"],
               "roles": dict(roles), "side": first["side"], "token": first["token"],
               "first_role": first["role"]}
        if first["role"] == "maker":
            rec["mid_dist_c"] = first["vs_mid_c"]  # dir * (mid - price): + = passive vs mid
            rec["fair_dist_c"] = first["edge_c"]  # dir * (fair - price)
        recs.append(rec)
    mk = [o for o in recs if o["first_role"] == "maker"]
    tk = [o for o in recs if o["first_role"] == "taker"]
    return {"orders": len(recs), "fills_without_order_id": no_id,
            "maker_orders": len(mk), "taker_orders": len(tk),
            "mixed_role_orders": sum(len(o["roles"]) > 1 for o in recs),
            "fills_per_order": _q([o["n"] for o in recs]),
            "shares_per_order": _q([o["shares"] for o in recs]),
            "multi_price_orders": sum(o["prices"] > 1 for o in recs),
            "lifetime_s_maker": _q([o["lifetime"] for o in mk]),
            "lifetime_s_taker": _q([o["lifetime"] for o in tk]),
            "maker_mid_dist_c": _q([o["mid_dist_c"] for o in mk]),
            "maker_fair_dist_c": _q([o["fair_dist_c"] for o in mk]),
            "note": ("lifetime = first to last FILL (placement/cancel times are not on chain); "
                     "maker mid includes the maker's own resting quote")}, recs


# ---------------------------------------------------------------- per window / inventory
def inventory(fills: list[dict]) -> tuple[dict, list[dict]]:
    """Directional inventory (Up shares - Down shares) per window, in decision-time order. Sets
    ``inv_before`` on each fill. Returns (summary, one record per window)."""
    by: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        by[f["slug"]].append(f)
    wins = []
    xs_d, xs_inv, xs_role = [], [], []
    for slug, fs in by.items():
        fs.sort(key=lambda x: (x["t_dec"], x["id"]))
        up = dn = 0.0
        gross = peak = 0.0
        for f in fs:
            inv = up - dn
            f["inv_before"] = inv
            xs_d.append(f["udir"])
            xs_inv.append(-inv)
            xs_role.append(f["role"])
            q = f["dir"] * f["shares"]
            if f["idx"] == 0:
                up += q
            else:
                dn += q
            gross += f["shares"]
            peak = max(peak, abs(up - dn))
        net = up - dn
        pnl = [f["settle_usd"] for f in fs]
        wins.append({"slug": slug, "asset": fs[0]["asset"], "hz": fs[0]["hz"],
                     "start": fs[0]["start"], "fills": len(fs),
                     "up_close": up, "down_close": dn, "gross": gross, "net_close": net,
                     "pairs": min(max(up, 0.0), max(dn, 0.0)), "peak": peak,
                     "flat": abs(net) <= 0.05 * gross if gross else True,
                     "settle_usd": sum(pnl) if all(math.isfinite(p) for p in pnl) else None})

    def corr(mask) -> dict | None:
        d = np.array([x for x, k in zip(xs_d, mask) if k], dtype=float)
        v = np.array([x for x, k in zip(xs_inv, mask) if k], dtype=float)
        if len(d) < 10 or d.std() == 0 or v.std() == 0:
            return {"n": int(len(d)), "corr": None}
        return {"n": int(len(d)), "corr": _r(np.corrcoef(d, v)[0, 1])}

    nz = [x != 0 for x in xs_inv]
    summary = {
        "windows": len(wins),
        "flat_close_share": _r(sum(w["flat"] for w in wins) / len(wins)) if wins else None,
        "abs_net_close_over_gross": _q([abs(w["net_close"]) / w["gross"] for w in wins if w["gross"]]),
        "paired_share_of_gross": _r(sum(w["pairs"] for w in wins) / max(1e-9, sum(w["gross"] for w in wins))),
        "peak_inventory_shares": _q([w["peak"] for w in wins]),
        "skew_vs_inventory": {
            "all": corr([True] * len(xs_d)),
            "nonzero_inventory": corr(nz),
            "maker": corr([r == "maker" and n for r, n in zip(xs_role, nz)]),
            "taker": corr([r == "taker" and n for r, n in zip(xs_role, nz)]),
            "meaning": "corr(up-exposure direction of fill, -inventory before it); > 0 = trades "
                       "lean against inventory (mean-reverting book)"},
        "top_windows_by_gross": sorted(wins, key=lambda w: -w["gross"])[:5],
    }
    return summary, wins


# ---------------------------------------------------------------- counterparties
def counterparties(fills: list[dict], top: int = 10) -> dict:
    mk = [f for f in fills if f["role"] == "maker"]
    tot = sum(f["usd"] for f in mk)
    by: dict[str, list[float]] = defaultdict(lambda: [0.0, 0])
    for f in mk:
        b = by[f["cp"] or "(none)"]
        b[0] += f["usd"]
        b[1] += 1
    rows = sorted(by.items(), key=lambda kv: -kv[1][0])
    return {"maker_notional": _r(tot, 2), "distinct": len([k for k in by if k != "(none)"]),
            "null_share": _r(by["(none)"][0] / tot) if tot and "(none)" in by else 0.0,
            "top": [{"address": k, "notional": _r(v[0], 2), "fills": int(v[1]),
                     "share": _r(v[0] / tot) if tot else None} for k, v in rows[:top]]}


# ---------------------------------------------------------------- edge decomposition
def edge_decomposition(fills: list[dict]) -> dict:
    """Over BTC fills with BOTH fair and a resolved payout (identical subset for every column):
    model edge $ = sum dir*(shares*fair_tok - usd); realized $ = sum dir*(shares*payout - usd);
    luck = realized - model; fees separate."""
    def block(fs: list[dict]) -> dict:
        model = sum(f["dir"] * (f["shares"] * f["fair_tok"] - f["usd"]) for f in fs)
        real = sum(f["settle_usd"] for f in fs)
        fees = sum(f["fee"] for f in fs)
        sh = sum(f["shares"] for f in fs)
        return {"fills": len(fs), "shares": _r(sh, 2), "model_edge_usd": _r(model, 2),
                "realized_usd": _r(real, 2), "luck_usd": _r(real - model, 2), "fees_usd": _r(fees, 2),
                "net_after_fees_usd": _r(real - fees, 2),
                "model_edge_c_per_share": _r(100 * model / sh, 3) if sh else None,
                "realized_c_per_share": _r(100 * real / sh, 3) if sh else None}
    ok = [f for f in fills if f["asset"] == "btc" and math.isfinite(f["fair_tok"])
          and math.isfinite(f["settle_usd"])]
    out: dict = {"subset": "btc fills with fair_v1 and a resolved payout", "all": block(ok)}
    for role in ("maker", "taker"):
        out[role] = block([f for f in ok if f["role"] == role])
    for src in ("print_ms", "block_s"):
        out[src] = block([f for f in ok if f["time_src"] == src])
    res = [f for f in fills if math.isfinite(f["settle_usd"])]
    out["all_resolved_crypto"] = {"fills": len(res), "realized_usd": _r(sum(f["settle_usd"] for f in res), 2),
                                  "fees_usd": _r(sum(f["fee"] for f in res), 2)}
    return out


def markout_curve(fills: list[dict]) -> dict:
    """Share-weighted mean markouts (cents/share, dir-adjusted) by role. OUTCOMES."""
    cols = ["vs_mid_c", *[f"mo{k}_c" for k in MARKOUTS], "settle_c", "edge_c"]
    out = {}
    for role in ("maker", "taker", "all"):
        fs = [f for f in fills if role == "all" or f["role"] == role]
        out[role] = {c: {"mean": _r(_wmean([f[c] for f in fs], [f["shares"] for f in fs]), 3),
                         "n": sum(1 for f in fs if math.isfinite(f[c]))} for c in cols}
    return out


def settlement_basis(fills: list[dict], brti: Brti | None) -> dict:
    """Does the PM US BRTI series' outcome agree with the token payouts of these BTC windows?"""
    if brti is None:
        return {"windows": 0, "agree": None}
    seen: dict[str, tuple] = {}
    for f in fills:
        if f["asset"] != "btc" or f["payout"] is None or f["slug"] in seen:
            continue
        up_pay = f["payout"] if f["idx"] == 0 else 1 - f["payout"]
        a, b = brti.series.get(f["start"] - 1), brti.series.get(f["end"] - 1)
        if a is None or b is None:
            continue
        seen[f["slug"]] = ((b >= a) == (up_pay >= 0.5),)
    n = len(seen)
    return {"windows": n, "agree": _r(sum(v[0] for v in seen.values()) / n) if n else None}


# ---------------------------------------------------------------- policy inversion v0
def build_panel(fills: list[dict], idx: BookIndex, brti: Brti, tmap_tokens: dict[str, dict[int, str]]
                ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, dict]:
    """Per-second panel over every BTC window the wallet traded.

    Row at second s (start <= s < end) uses only: book received strictly before s, fair_v1 at
    s-1, raw BRTI up to s-1, own inventory from fills with t_dec < s. Label: the wallet's TAKER
    fills with floor(t_dec) == s, by net Up exposure: 1 = took Up, 2 = took Down, 0 = nothing.
    Returns X (rows x FEATURES), y, window start and end per row, info."""
    by: dict[str, list[dict]] = defaultdict(list)
    for f in fills:
        if f["asset"] == "btc":
            by[f["slug"]].append(f)
    X: list[list[float]] = []
    y: list[int] = []
    ws: list[int] = []
    we: list[int] = []
    info = Counter()
    for slug, fs in sorted(by.items(), key=lambda kv: kv[1][0]["start"]):
        start, end = fs[0]["start"], fs[0]["end"]
        toks = dict(tmap_tokens.get(slug, {}))
        for f in fs:
            toks.setdefault(f["idx"], f["token"])
        for i in (0, 1):
            if i not in toks and (1 - i) in toks:
                sib = idx.sibling(toks[1 - i])
                if sib:
                    toks[i] = sib
        up, dn = toks.get(0), toks.get(1)
        take: dict[int, float] = defaultdict(float)
        for f in fs:
            if f["role"] == "taker":
                sec = math.floor(f["t_dec"])
                if not start <= sec < end:
                    info["takes_outside_window"] += 1  # pre-open or clock trouble: no panel row
                    continue
                take[sec] += f["udir"] * f["shares"]
                info["label_block_s" if f["time_src"] == "block_s" else "label_print_ms"] += 1
        evs = sorted((f["t_dec"], f["udir"] * f["shares"]) for f in fs)
        j, inv = 0, 0.0
        info["windows"] += 1
        for s in range(start, end):
            while j < len(evs) and evs[j][0] < s:
                inv += evs[j][1]
                j += 1
            fu = brti.fair(start, end, s - 1)
            lab = 1 if take.get(s, 0.0) > 0 else 2 if take.get(s, 0.0) < 0 else 0
            if fu is None:
                info["rows_no_fair"] += 1
                info["takes_lost_no_fair"] += lab > 0
                continue
            qu = idx.quote(up, s * NS) if up else None
            bid_u, ask_u = qu if qu else (math.nan, math.nan)
            qd = idx.quote(dn, s * NS) if dn else None
            ask_d = qd[1] if qd and math.isfinite(qd[1]) else (1 - bid_u if math.isfinite(bid_u) else math.nan)
            r1 = brti.at(s - 1)
            X.append([fu - ask_u, (1 - fu) - ask_d, fu, float(end - s),
                      r1 - brti.at(s - 2), r1 - brti.at(s - 6), r1 - brti.at(s - 31),
                      ask_u - bid_u, inv])
            y.append(lab)
            ws.append(start)
            we.append(end)
    info["rows"] = len(y)
    info["takes"] = int(sum(v > 0 for v in y))
    return (np.asarray(X, dtype=float).reshape(-1, len(FEATURES)), np.asarray(y, dtype=np.int64),
            np.asarray(ws, dtype=np.int64), np.asarray(we, dtype=np.int64), dict(info))


def _fit(Xtr, ytr, Xte, yte) -> dict:
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.metrics import roc_auc_score
    from sklearn.tree import DecisionTreeClassifier, export_text
    hgb = HistGradientBoostingClassifier(max_iter=200, class_weight="balanced", random_state=SEED)
    hgb.fit(Xtr, ytr)
    tree = DecisionTreeClassifier(max_depth=3, class_weight="balanced", min_samples_leaf=20,
                                  random_state=SEED)
    tree.fit(Xtr, ytr)
    p_hgb = np.asarray(hgb.predict_proba(Xte))[:, 1]
    p_tree = np.asarray(tree.predict_proba(Xte))[:, 1]
    return {"auc_hgb": _r(roc_auc_score(yte, p_hgb)), "auc_tree": _r(roc_auc_score(yte, p_tree)),
            "rules": export_text(tree, feature_names=list(FEATURES), show_weights=False)}


def invert_policy(X: np.ndarray, y: np.ndarray, ws: np.ndarray, we: np.ndarray, info: dict) -> dict:
    """Time-ordered split: cutoff = start of the window at the TRAIN_FRAC quantile of distinct
    window starts. Train = windows ENDING at or before it, test = windows STARTING at or after
    it; windows straddling it (a 15m window overlapping test-period 5m windows) are dropped."""
    out: dict = {"features": list(FEATURES), "panel": info}
    starts = np.unique(ws)
    if len(starts) < 4:
        return out | {"verdict": "insufficient", "reason": f"{len(starts)} windows with fair"}
    cut = int(starts[int(math.floor(TRAIN_FRAC * len(starts)))])
    tr = we <= cut
    te = ws >= cut
    out["split"] = {"cutoff": cut, "train_rows": int(tr.sum()), "test_rows": int(te.sum()),
                    "dropped_straddle_rows": int((~tr & ~te).sum()),
                    "train_windows": int(len(np.unique(ws[tr]))),
                    "test_windows": int(len(np.unique(ws[te])))}
    took = y > 0
    ptr, pte = int((took & tr).sum()), int((took & te).sum())
    ta: dict = {"train_pos": ptr, "test_pos": pte}
    out["took_any"] = ta
    if ptr < MIN_POS_TRAIN or pte < MIN_POS_TEST or (~took & te).sum() == 0 or (~took & tr).sum() == 0:
        ta["result"] = "insufficient"
    else:
        ta |= _fit(X[tr], took[tr].astype(int), X[te], took[te].astype(int))
    is_up = y == 1
    up_tr, dn_tr = int((took & tr & is_up).sum()), int((took & tr & ~is_up).sum())
    up_te, dn_te = int((took & te & is_up).sum()), int((took & te & ~is_up).sum())
    sg: dict = {"train_up": up_tr, "train_down": dn_tr, "test_up": up_te, "test_down": dn_te}
    out["side_given_took"] = sg
    if min(up_tr, dn_tr) < MIN_SIDE_TRAIN or min(up_te, dn_te) < MIN_SIDE_TEST:
        sg["result"] = "insufficient"
    else:
        mtr, mte = took & tr, took & te
        sg |= _fit(X[mtr], is_up[mtr].astype(int), X[mte], is_up[mte].astype(int))
    a = ta.get("auc_hgb")
    if a is None:
        out["verdict"] = "insufficient"
    else:
        out["verdict"] = ("EXPLAINED" if a >= 0.85 else "PARTIAL" if a >= 0.65 else "UNEXPLAINED")
    return out


# ---------------------------------------------------------------- dossier
def mechanism(fills: list[dict]) -> dict:
    n = sum(f["usd"] for f in fills)
    mk = sum(f["usd"] for f in fills if f["role"] == "maker")
    buys = [f for f in fills if f["side"] == "BUY"]
    mix = Counter(f"{f['asset']}-{f['hz']}" for f in fills)
    return {"fills": len(fills), "notional": _r(n, 2), "maker_share": _r(mk / n) if n else None,
            "buy_share": _r(len(buys) / len(fills)) if fills else None,
            "windows": len({f["slug"] for f in fills}),
            "tte_s_taker": _q([f["tte"] for f in fills if f["role"] == "taker"]),
            "tte_s_maker": _q([f["tte"] for f in fills if f["role"] == "maker"]),
            "entry_price_buys": _q([f["price"] for f in buys]),
            "mix": dict(mix.most_common())}


def coverage(fills: list[dict]) -> dict:
    n = len(fills)
    btc = [f for f in fills if f["asset"] == "btc"]
    lag = [f["block_ts"] - f["t"] for f in fills if f["time_src"] == "print_ms"]
    return {"fills": n,
            "print_ms_share": _r(sum(f["time_src"] == "print_ms" for f in fills) / n) if n else None,
            "book_share": _r(sum(f["has_book"] for f in fills) / n) if n else None,
            "btc_fills": len(btc),
            "fair_share_btc": _r(sum(math.isfinite(f["fair_up"]) for f in btc) / len(btc)) if btc else None,
            "resolved_share": _r(sum(f["payout"] is not None for f in fills) / n) if n else None,
            "block_minus_print_s": _q(lag),
            "by_time_src": {s: {"fills": sum(f["time_src"] == s for f in fills),
                                "book_share": _r(_share([f["has_book"] for f in fills if f["time_src"] == s]))}
                            for s in ("print_ms", "block_s")}}


def _share(xs: list[bool]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def tokens_by_slug(tmap: TokenMap) -> dict[str, dict[int, str]]:
    out: dict[str, dict[int, str]] = defaultdict(dict)
    for tok, info in tmap.m.items():
        if info.get("slug") and info.get("idx") in (0, 1):
            out[info["slug"]][int(info["idx"])] = tok
    return out


def dossier(wallet: str, rows: list[dict], tmap: TokenMap, idx: BookIndex, brti: Brti | None,
            by_slug: dict[str, dict[int, str]] | None = None) -> tuple[dict, dict[str, list[dict]]]:
    """(dossier document, tables: per-fill ``fills``, per-order ``orders``, per-window ``windows``)."""
    fills, skip = enrich(rows, tmap, idx, brti)
    inv, wins = inventory(fills)
    osum, orecs = orders(fills)
    doc: dict = {"wallet": wallet, "built_tw": time.time_ns(),
                 "params": {"max_age_s": MAX_AGE_S, "block_slack_s": BLOCK_SLACK_S,
                            "markouts_s": list(MARKOUTS), "fair": "fair_v1 raw BRTI",
                            "fair_lag": brti.lag if brti else None},
                 "skipped_rows": skip, "coverage": coverage(fills),
                 "mechanism": mechanism(fills), "edge": edge_decomposition(fills),
                 "markouts": markout_curve(fills), "orders": osum,
                 "inventory": inv, "counterparties": counterparties(fills),
                 "settlement_basis": settlement_basis(fills, brti)}
    if brti is not None and len(brti.a):
        X, y, ws, we, info = build_panel(fills, idx, brti, by_slug or tokens_by_slug(tmap))
        doc["policy"] = invert_policy(X, y, ws, we, info)
    else:
        doc["policy"] = {"verdict": "insufficient", "reason": "no raw BRTI"}
    return doc, {"fills": fills, "orders": orecs, "windows": wins}


# ---------------------------------------------------------------- card
def card(doc: dict, clock: dict | None = None) -> str:
    m, c, e, o, inv = doc["mechanism"], doc["coverage"], doc["edge"], doc["orders"], doc["inventory"]
    pol = doc.get("policy", {})
    L = [f"# Dossier {doc['wallet']}", ""]
    tte = (m.get("tte_s_taker") or {}).get("p50")
    L += ["## Mechanism",
          f"- {m['fills']} fills, ${m['notional']:,} notional over {m['windows']} windows; "
          f"maker share {m['maker_share']}, buy share {m['buy_share']}; taker time-to-end p50 {tte}s",
          f"- mix: {m['mix']}",
          f"- entry price (buys): {m.get('entry_price_buys')}",
          f"- inventory: flat-close share {inv['flat_close_share']}, paired share of gross "
          f"{inv['paired_share_of_gross']}, skew vs inventory {inv['skew_vs_inventory']['nonzero_inventory']}",
          f"- policy inversion: **{pol.get('verdict')}**", ""]
    L += ["## Edge decomposition (BTC, fair_v1 and resolved; $)", "",
          "| subset | fills | model edge | realized | luck | fees | net | model c/sh | realized c/sh |",
          "|---|---|---|---|---|---|---|---|---|"]
    for k in ("all", "maker", "taker", "print_ms", "block_s"):
        b = e[k]
        L.append(f"| {k} | {b['fills']} | {b['model_edge_usd']} | {b['realized_usd']} | {b['luck_usd']} | "
                 f"{b['fees_usd']} | {b['net_after_fees_usd']} | {b['model_edge_c_per_share']} | "
                 f"{b['realized_c_per_share']} |")
    a = e["all_resolved_crypto"]
    L += ["", f"All resolved crypto fills: {a['fills']} fills, realized ${a['realized_usd']}, fees ${a['fees_usd']}.",
          f"Settlement basis (BRTI series vs payouts, BTC windows): {doc['settlement_basis']}", ""]
    cols = ["vs_mid_c", *[f"mo{k}_c" for k in MARKOUTS], "settle_c", "edge_c"]
    L += ["## Markout curve (cents/share, dir-adjusted, share-weighted; outcomes)", "",
          "| role | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
    for role, v in doc["markouts"].items():
        L.append(f"| {role} | " + " | ".join(f"{v[c]['mean']} ({v[c]['n']})" for c in cols) + " |")
    L += ["", "## Order lifecycle",
          f"- {o['orders']} orders ({o['maker_orders']} maker-first, {o['taker_orders']} taker-first, "
          f"{o['mixed_role_orders']} mixed roles, {o['multi_price_orders']} multi-price); "
          f"{o['fills_without_order_id']} fills without order id",
          f"- fills/order {o['fills_per_order']}; shares/order {o['shares_per_order']}",
          f"- lifetime s (first to last fill): maker {o['lifetime_s_maker']}, taker {o['lifetime_s_taker']}",
          f"- maker price vs as-of mid (c, + = passive): {o['maker_mid_dist_c']}",
          f"- maker price vs fair (c): {o['maker_fair_dist_c']}", "",
          "## Inventory policy",
          f"- windows {inv['windows']}; |net close|/gross {inv['abs_net_close_over_gross']}; "
          f"peak inventory {inv['peak_inventory_shares']}",
          f"- skew vs inventory: {inv['skew_vs_inventory']}", "",
          "## Top counterparties (maker fills)", ""]
    cp = doc["counterparties"]
    L.append(f"maker notional ${cp['maker_notional']}, {cp['distinct']} distinct, null share {cp['null_share']}")
    L += [f"- {x['address']}: ${x['notional']} ({x['share']}), {x['fills']} fills" for x in cp["top"]]
    L += ["", "## Policy inversion v0 (taker, BTC)", "",
          f"- verdict: **{pol.get('verdict')}** {pol.get('reason', '')}",
          f"- panel: {pol.get('panel')}", f"- split: {pol.get('split')}"]
    for task in ("took_any", "side_given_took"):
        t = pol.get(task)
        if not t:
            continue
        L.append(f"- {task}: " + ", ".join(f"{k}={v}" for k, v in t.items() if k != "rules"))
        if t.get("rules"):
            L += ["", "```", t["rules"].rstrip(), "```", ""]
    L += ["", "## Data coverage",
          f"- fills {c['fills']}; print-ms time {c['print_ms_share']}; book state {c['book_share']}; "
          f"BTC fills {c['btc_fills']} with fair {c['fair_share_btc']}; resolved {c['resolved_share']}",
          f"- block_ts - print time (s): {c['block_minus_print_s']}",
          f"- by time source: {c['by_time_src']}", f"- skipped rows: {doc['skipped_rows']}"]
    if clock:
        L.append(f"- CLOB tw - server timestamp (ms) per stream: {clock}")
    return "\n".join(L) + "\n"


def _jsonable(x):
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.floating, np.integer)):
        return _jsonable(x.item())
    return x


def _write_table(path: Path, recs: list[dict]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    if not recs:
        return
    keys = list(dict.fromkeys(k for r in recs for k in r))
    cols = {k: [_jsonable(r.get(k)) for r in recs] for k in keys}
    for k, v in cols.items():  # nested values (role counts) as JSON text
        if any(isinstance(x, (dict, list)) for x in v):
            cols[k] = [orjson.dumps(x).decode() if x is not None else None for x in v]
    pq.write_table(pa.table(cols), path, compression="zstd")


# ---------------------------------------------------------------- run / CLI
def select_wallets(store: Path, tmap: TokenMap, members: set[str], top: int | None) -> list[str]:
    """Wallets ranked by notional in crypto Up/Down windows (descending), first ``top``."""
    notional: dict[str, float] = defaultdict(float)
    for addr, tok, usd in wallet_token_notional(store, members):
        if window_of(tmap.m.get(tok) or {}):
            notional[addr] += usd
    ranked = sorted(notional, key=lambda w: (-notional[w], w))
    return ranked[:top] if top else ranked


def run(root: Path, lab: Path, cohort_path: Path, wallets: list[str] | None = None,
        top: int | None = None, store: Path | None = None, lag: int = 0) -> dict:
    from .brti_import import load_raw
    from .refprice import load_series
    cohort = orjson.loads(Path(cohort_path).read_bytes())
    members = {w.lower() for w in (set(cohort["candidates"]) | set(cohort["controls"]))}
    if wallets:
        members = {w.lower() for w in wallets}
    store = store or lab / "fills"
    compact(root, store)
    tmap = TokenMap(lab / "tokens.jsonl")
    asyncio.run(tmap.ensure(wallet_tokens(store, members)))
    chosen = select_wallets(store, tmap, members, top)
    markets: set[str] = set()
    txs: set[str] = set()
    streams: set[str] = set()
    for _, rows in iter_wallet_fills(store, chosen):
        for r in rows:
            info = tmap.m.get(r["token_id"]) or {}
            win = window_of(info)
            if win:
                streams.add(f"clob_{win['asset']}_{win['hz']}")
                if info.get("condition"):
                    markets.add(info["condition"])
                if r.get("transaction_hash"):
                    txs.add(r["transaction_hash"])
    idx = build_index(root, markets, txs, sorted(streams))
    raw = load_raw(root) if (root / "ref_brti_raw").exists() else {}
    series = load_series(root, "crypto:btc") if (root / "ref_prices").exists() else {}
    brti = Brti(raw, series, lag) if raw and series else None
    del raw
    by_slug = tokens_by_slug(tmap)
    clock = idx.clock_report()
    out_dir = lab / "dossiers"
    out_dir.mkdir(parents=True, exist_ok=True)
    done = {}
    for w, rows in iter_wallet_fills(store, chosen):
        doc, tables = dossier(w, rows, tmap, idx, brti, by_slug)
        doc["clock"] = clock
        doc["index_counts"] = dict(idx.counts)
        (out_dir / f"{w}.json").write_bytes(orjson.dumps(_jsonable(doc), option=orjson.OPT_INDENT_2))
        (out_dir / f"{w}.md").write_text(card(doc, clock))
        for name, recs in tables.items():
            _write_table(out_dir / f"{w}.{name}.parquet", recs)
        done[w] = {"fills": doc["coverage"]["fills"], "verdict": doc["policy"].get("verdict")}
    missing = [w for w in (sorted(members) if wallets else chosen) if w not in done]
    return {"wallets": done, "missing": missing, "clock": clock, "index_counts": dict(idx.counts)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-dossier")
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--lab", type=Path, required=True)
    ap.add_argument("--cohort", type=Path, required=True)
    ap.add_argument("--wallets", default=None, help="comma-separated addresses (default: cohort)")
    ap.add_argument("--top", type=int, default=None,
                    help="only the N wallets with the most crypto-window notional")
    ap.add_argument("--store", type=Path, default=None)
    ap.add_argument("--lag", type=int, default=0, help="fair_v1 settlement alignment lag (s)")
    a = ap.parse_args(argv)
    ws = [w.strip() for w in a.wallets.split(",") if w.strip()] if a.wallets else None
    res = run(a.data, a.lab, a.cohort, ws, a.top, a.store, a.lag)
    for w, v in res["wallets"].items():
        print(f"{w} fills={v['fills']} policy={v['verdict']} -> {a.lab / 'dossiers' / (w + '.md')}")
    for w in res["missing"]:
        print(f"{w} no crypto Up/Down window fills in the store: no dossier written")
    print(f"clock (tw - server ms): {res['clock']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
