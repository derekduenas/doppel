"""Signed markout curve of fills (STATE-AND-PLAN §9.2 item 2): +0.5 s, +2 s, +10 s, +60 s and
settlement, against the recorded book mid and (PM US BTC) fair-v1.

    doppel-markout --data TAPE --fills FILLS --out OUT.json [--intl-stream clob_btc_5m --outcomes OUT.json]

FILLS (format detected per file):
- a ``doppel-twin`` output JSON (``windows[].fills``: ``t`` seconds after the window start, role,
  side, px, qty, fee);
- a paper journal (``journal-*.jsonl`` directory or file; ``k == "fill"`` rows: slug, t ns, role,
  side, px, qty, fee; chain fields are not verified here);
- wallet fills as Goldsky TRADE rows (JSONL: token_id, side BUY/SELL, price, amount_shares, fee,
  block_timestamp, transaction_hash) on Polymarket intl;
- generic JSONL rows ``{key|slug, t_ns, side buy|sell, px, qty, fee, role?}``.
``side`` is always the side of the keyed instrument (PM US: YES = Up; intl: the token itself).

Measurement (no look-ahead, our receive clock): the mid at t + h is the last book RECEIVED at or
before t + h (never the next one); a gap record after that book makes it unknown; at or after the
window end the mark is the settlement (100 * outcome), exactly the twin's ``_mid_c``. Markouts are
cents per contract, signed for the fill's side (buy: mark - px, sell: px - mark); ``net`` subtracts
the fee the fill paid (100 * fee / qty). Fair marks use ``fair_up_asof`` (BRTI received by t + h,
the twin's 2 s BRTI delay) with the opening known at t + h (published revision received, else the
received-raw computation). Goldsky fills carry chain block time, not our receive time: they are
re-anchored to the receive time of the intl print with the same transaction hash when the tape has
it; otherwise the +0.5 s and +2 s marks are left empty (block time is only second-resolution).

CIs: percentile bootstrap resampling whole market windows (keys), quantity-weighted means.
Shape labels (informational only, never a gate), from the GROSS mid curve, 95% CIs:
  INFORMATION  lo(+2 s) > 0 and lo(+60 s) > 0          (positive by <= 5 s and holds)
  REVERSION    hi(+2 s) < 0 and lo(late - (+2 s)) > 0, late = +60 s or settlement (paired draws)
  ADVERSE      makers only: hi(+2 s) < 0 and not REVERSION
  UNCLEAR      otherwise.
The twin's own 1/5/30 s markouts are untouched; ``curve(..., horizons=(1, 5, 30))`` reproduces them.
"""

from __future__ import annotations

import argparse
import bisect
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
import orjson

NS = 1_000_000_000
HORIZONS = (0.5, 2.0, 10.0, 60.0)
SETTLE = "settle"
BLOCK_UNRESOLVED_S = 2.0      # block-time anchors leave horizons <= this empty
N_BOOT = 2000
EARLY, LATE = 2.0, 60.0


# ---------------------------------------------------------------------------- market paths
@dataclass
class MarketPath:
    """One market window as received: top of book over time plus what settles it."""
    key: str
    start: int                              # unix s
    end: int | None                         # unix s; None = unknown (intl without metadata)
    outcome: int | None                     # 1 = YES paid
    tws: list[int] = field(default_factory=list)            # receive ns, ascending
    bids: list[float | None] = field(default_factory=list)  # dollars; None = side empty / book closed
    asks: list[float | None] = field(default_factory=list)
    gaps: list[int] = field(default_factory=list)           # receive ns at which the book became unknown
    valid_until: int | None = None          # receive ns after which nothing is known (feed ended)
    fair: Callable[[float], float | None] | None = None     # P(YES) known at unix s (PM US BTC)
    index: Callable[[float], float | None] | None = None    # underlying index known at unix s
    cluster: int = 0                        # bootstrap block id

    def top_asof(self, t_ns: int) -> tuple[float | None, float | None] | None:
        """(bid, ask) of the last book received at or before t_ns; None if unknown."""
        if self.valid_until is not None and t_ns > self.valid_until:
            return None
        j = bisect.bisect_right(self.tws, t_ns) - 1
        if j < 0:
            return None
        k = bisect.bisect_right(self.gaps, t_ns) - 1
        if k >= 0 and self.gaps[k] > self.tws[j]:
            return None
        return self.bids[j], self.asks[j]

    def mid_asof(self, t_ns: int) -> float | None:
        """Mid in cents at t_ns; the settlement value at or after the end."""
        if self.end is not None and t_ns >= self.end * NS:
            return None if self.outcome is None else 100.0 * self.outcome
        top = self.top_asof(t_ns)
        if top is None or top[0] is None or top[1] is None:
            return None
        return 50.0 * (top[0] + top[1])

    def fair_asof(self, t_ns: int) -> float | None:
        if self.end is not None and t_ns >= self.end * NS:
            return None if self.outcome is None else 100.0 * self.outcome
        if self.fair is None:
            return None
        p = self.fair(t_ns / NS)
        return None if p is None else 100.0 * p


def path_from_window(wt, ref, raw: tuple[int, np.ndarray, np.ndarray] | None = None,
                     cluster: int | None = None) -> MarketPath:
    """``twin.tape.WindowTape`` + ``twin.features.WindowRef`` -> MarketPath. ``raw`` = (t0, BRTI,
    receive s) covering at least [start - 900, end] for the BTC index; defaults to the ref's own."""
    from .fair import BRTI_DELAY_S, fair_up_asof
    p = MarketPath(key=wt.slug, start=wt.start, end=wt.end, outcome=ref.outcome, gaps=list(wt.gaps),
                   cluster=wt.start // 900 if cluster is None else cluster)
    for b in wt.books:
        p.tws.append(b.tw)
        p.bids.append(b.best_bid if b.open else None)
        p.asks.append(b.best_ask if b.open else None)
    if raw is None and ref.raw is not None and ref.rcv is not None:
        raw = (ref.raw_t0, ref.raw, ref.rcv)
    if raw is not None:
        t0, a, r = raw

        def opening(t_s: float) -> float | None:
            if ref.ptb_history is None:
                return ref.ptb
            _, v = ref.opening_at(int(t_s * NS))
            if v is not None:
                return v
            v, known = ref.computed_opening()
            return v if v is not None and known is not None and known <= t_s else None

        def fair(t_s: float) -> float | None:
            ptb = opening(t_s)
            return None if ptb is None else fair_up_asof(a, r, t0, ptb, t_s, wt.end)

        def index(t_s: float) -> float | None:
            s = int(math.floor(t_s)) - BRTI_DELAY_S
            for i in range(s - t0, max(-1, s - t0 - 30), -1):
                if 0 <= i < len(a) and math.isfinite(a[i]) and math.isfinite(r[i]) and r[i] <= t_s:
                    return float(a[i])
            return None
        p.fair, p.index = fair, index
    return p


def pmus_paths(root: Path, assets, horizons, t_from: int, t_to: int, *, want: set[str] | None = None,
               index_lookback_s: int = 960) -> Iterator[MarketPath]:
    """PM US WS windows (twin reader) with settlement from the published series; BTC windows carry
    fair-v1 and the received BRTI index. Windows without WS books are skipped."""
    from ..twin.features import RefStore
    from ..twin.tape import iter_windows
    store = RefStore(root)
    for a in assets:
        store.load_boundaries(a, t_from - 1, t_to)
    for wt in iter_windows(root, assets, horizons, t_from, t_to):
        if want is not None and wt.slug not in want:
            continue
        if wt.source != "ws":
            continue
        ref = store.window_ref(wt.asset, wt.start, wt.end, 300)
        raw = None
        if wt.asset == "btc":
            lo = wt.start - index_lookback_s
            a, r = store.brti_rcv(lo, wt.end)
            raw = (lo, a, r)
        yield path_from_window(wt, ref, raw)


def intl_paths(root: Path, stream: str, t_from: int, t_to: int, assets: set[str],
               meta: dict[str, dict] | None = None) -> tuple[dict[str, MarketPath], dict[str, int]]:
    """Intl CLOB top of book per token from ``lab.intlbook`` replay, and transaction hash -> receive
    ns of its print. ``meta``: token -> {"outcome": 0|1, "end": unix s} (not on the tape). As in
    ``IntlBook.spans``, nothing is claimed about a token after its last frame (an unsubscribed
    token just goes quiet), so marks past it are empty until the window end."""
    from .intlbook import IntlBook, Print, Reset, replay_messages
    book = IntlBook()
    meta = meta or {}
    paths = {a: MarketPath(key=a, start=t_from, end=(meta.get(a) or {}).get("end"),
                           outcome=(meta.get(a) or {}).get("outcome"), cluster=0) for a in assets}
    tx: dict[str, int] = {}
    last: dict[str, int] = {}
    for _, items in replay_messages(root, stream, t_from, t_to, book):   # state as of each message
        touched = set()
        for it in items:
            if isinstance(it, Reset):
                for p in paths.values():
                    p.gaps.append(it.tw_ns)
                continue
            if it.asset not in paths:
                continue
            last[it.asset] = it.tw_ns
            if isinstance(it, Print):
                if it.tx:
                    tx.setdefault(it.tx, it.tw_ns)
                continue
            touched.add((it.asset, it.tw_ns))
        for a, tw in sorted(touched):
            lv = book.levels.get(a)
            if lv is None:
                continue
            bids = [px for (px, s), q in lv.items() if s == "BUY" and q > 0]
            asks = [px for (px, s), q in lv.items() if s == "SELL" and q > 0]
            p = paths[a]
            p.tws.append(tw)
            p.bids.append(max(bids) if bids else None)
            p.asks.append(min(asks) if asks else None)
    for a, p in paths.items():
        p.valid_until = last.get(a)
        if p.tws:
            p.cluster = p.tws[0] // NS // 900
    return paths, tx


# ---------------------------------------------------------------------------- fills
@dataclass(frozen=True)
class Fill:
    key: str
    t_ns: int
    side: str           # "buy" | "sell" of the keyed instrument
    px: float           # dollars
    qty: float
    fee_usd: float
    role: str | None    # "taker" | "maker" | None
    anchor: str         # "receive" (our clock) | "block" (chain time)


def _side(s) -> str:
    s = str(s).lower()
    if s in ("buy", "order_side_buy"):
        return "buy"
    if s in ("sell", "order_side_sell"):
        return "sell"
    raise ValueError(f"unknown side {s!r}")


def fills_from_twin(doc: dict) -> list[Fill]:
    out = []
    for w in doc.get("windows") or []:
        for f in w.get("fills") or []:
            out.append(Fill(w["slug"], w["start"] * NS + int(round(f["t"] * NS)), _side(f["side"]),
                            float(f["px"]), float(f["qty"]), float(f.get("fee") or 0.0), f.get("role"), "receive"))
    return out


def fill_from_row(r: dict) -> Fill | None:
    if r.get("k") is not None:                                 # paper journal
        if r["k"] != "fill":
            return None
        return Fill(str(r["slug"]), int(r["t"]), _side(r["side"]), float(r["px"]), float(r["qty"]),
                    float(r.get("fee") or 0.0), r.get("role"), "receive")
    if "token_id" in r and "block_timestamp" in r:              # Goldsky TRADE row
        q = float(r["amount_shares"])
        px = float(r["price"]) if r.get("price") is not None else float(r["amount_usdc"]) / q
        role = r.get("role") or ({"TAKER": "taker", "MAKER": "maker"}.get(str(r.get("order_type", "")).upper()))
        return Fill(str(r["token_id"]), int(r["block_timestamp"]) * NS, _side(r["side"]), px, q,
                    float(r.get("fee") or 0.0), role, "block:" + str(r.get("transaction_hash") or ""))
    key = r.get("key", r.get("slug"))
    t = r.get("t_ns", r.get("tw"))
    if key is None or t is None:
        raise ValueError(f"unrecognised fill row: {sorted(r)}")
    return Fill(str(key), int(t), _side(r["side"]), float(r["px"] if "px" in r else r["price"]), float(r["qty"]),
                float(r.get("fee", r.get("fee_usd")) or 0.0), r.get("role"), "receive")


def load_fills(path: Path) -> list[Fill]:
    path = Path(path)
    if path.is_dir():
        files = sorted(path.glob("journal-*.jsonl"))
        return [f for p in files for f in load_fills(p)]
    raw = path.read_bytes()
    if raw.lstrip().startswith(b"{") and b"\n{" not in raw.strip():
        doc = orjson.loads(raw)
        if "windows" in doc:
            return fills_from_twin(doc)
        rows = [doc]
    else:
        rows = [orjson.loads(line) for line in raw.splitlines() if line.strip()]
    return [f for f in map(fill_from_row, rows) if f is not None]


def reanchor(fills: list[Fill], tx_tw: dict[str, int]) -> list[Fill]:
    """Block-time fills whose transaction hash has a recorded print -> that print's receive time."""
    out = []
    for f in fills:
        if f.anchor.startswith("block:") and tx_tw.get(f.anchor[6:]) is not None:
            f = Fill(f.key, tx_tw[f.anchor[6:]], f.side, f.px, f.qty, f.fee_usd, f.role, "receive")
        out.append(f)
    return out


# ---------------------------------------------------------------------------- curve
def _hkey(h) -> str:
    return SETTLE if h == SETTLE else f"{h:g}s"


def marks(f: Fill, path: MarketPath, horizons=HORIZONS, fair: bool = True) -> dict:
    """Per-fill signed markouts (cents/contract): {"mid": {h: gross}, "fair": {...}, "fee_c": ...}."""
    pc = 100.0 * f.px
    sign = 1.0 if f.side == "buy" else -1.0
    out: dict = {"mid": {}, "fair": {}, "fee_c": 100.0 * f.fee_usd / f.qty if f.qty > 0 else 0.0}
    block = not f.anchor == "receive"
    for h in (*horizons, SETTLE):
        k = _hkey(h)
        if h == SETTLE:
            m = None if path.outcome is None else 100.0 * path.outcome
            out["mid"][k] = None if m is None else sign * (m - pc)
            out["fair"][k] = out["mid"][k]
            continue
        if block and h <= BLOCK_UNRESOLVED_S:
            out["mid"][k] = out["fair"][k] = None
            continue
        t = f.t_ns + int(round(h * NS))
        m = path.mid_asof(t)
        out["mid"][k] = None if m is None else sign * (m - pc)
        if fair:
            fv = path.fair_asof(t)
            out["fair"][k] = None if fv is None else sign * (fv - pc)
    return out


def cluster_draws(cl: np.ndarray, num: np.ndarray, den: np.ndarray, n_boot: int = N_BOOT,
                  seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Point estimate(s) and bootstrap draws of sum(num)/sum(den) resampling whole clusters.
    num/den: (n,) or (n, m). Returns (est (m,), draws (n_boot, m)); NaN where den sums to 0."""
    num = np.asarray(num, float).reshape(len(cl), -1)
    den = np.asarray(den, float).reshape(len(cl), -1)
    ids, inv = np.unique(cl, return_inverse=True)
    C = len(ids)
    cn = np.zeros((C, num.shape[1]))
    cd = np.zeros((C, num.shape[1]))
    np.add.at(cn, inv, num)
    np.add.at(cd, inv, den)
    with np.errstate(invalid="ignore", divide="ignore"):
        est = cn.sum(0) / cd.sum(0)
        if C < 2:
            return est, np.full((0, num.shape[1]), np.nan)
        rng = np.random.default_rng(seed)
        draws = np.empty((n_boot, num.shape[1]))
        for i in range(0, n_boot, 250):
            idx = rng.integers(0, C, size=(min(250, n_boot - i), C))
            draws[i:i + len(idx)] = cn[idx].sum(1) / cd[idx].sum(1)
    return est, draws


def ci(draws: np.ndarray, level: float = 0.95) -> tuple[float | None, float | None]:
    d = draws[np.isfinite(draws)]
    if len(d) < 10:
        return None, None
    a = (1 - level) / 2
    return float(np.quantile(d, a)), float(np.quantile(d, 1 - a))


def _r(x):
    return None if x is None or not math.isfinite(x) else round(float(x), 4)


def curve(rows: list[tuple[Fill, dict, int]], kind: str = "mid", net: bool = False,
          horizons=HORIZONS, n_boot: int = N_BOOT, seed: int = 0) -> dict:
    """rows: (fill, marks, cluster). Quantity-weighted mean per horizon with cluster-bootstrap CIs."""
    keys = [_hkey(h) for h in (*horizons, SETTLE)]
    if not rows:
        return {"n_fills": 0, "n_windows": 0, "by_h": {k: None for k in keys}}
    cl = np.array([c for _, _, c in rows])
    q = np.array([f.qty for f, _, _ in rows])
    val = np.full((len(rows), len(keys)), np.nan)
    for i, (_, m, _) in enumerate(rows):
        for j, k in enumerate(keys):
            v = m[kind].get(k)
            if v is not None:
                val[i, j] = v - (m["fee_c"] if net else 0.0)
    ok = np.isfinite(val)
    est, draws = cluster_draws(cl, np.where(ok, val * q[:, None], 0.0), np.where(ok, q[:, None], 0.0), n_boot, seed)
    by = {}
    for j, k in enumerate(keys):
        lo, hi = ci(draws[:, j])
        by[k] = {"mean_c": _r(est[j]), "ci95": [_r(lo), _r(hi)], "n": int(ok[:, j].sum()),
                 "windows": int(len(set(cl[ok[:, j]])))}
    out = {"n_fills": len(rows), "n_windows": int(len(set(cl))), "contracts": round(float(q.sum()), 4), "by_h": by}
    if kind == "mid" and not net and _hkey(EARLY) in keys and _hkey(LATE) in keys:
        e = keys.index(_hkey(EARLY))
        diffs = {}
        for late in (_hkey(LATE), SETTLE):
            j = keys.index(late)
            lo, hi = ci(draws[:, j] - draws[:, e])
            diffs[f"{late}-minus-{_hkey(EARLY)}"] = {"mean_c": _r(est[j] - est[e]), "ci95": [_r(lo), _r(hi)]}
        out["paired"] = diffs
    return out


def classify(c: dict, maker: bool) -> str:
    """Informational shape label from a gross mid curve (see module docstring)."""
    by = c.get("by_h") or {}
    e, la = by.get(_hkey(EARLY)), by.get(_hkey(LATE))
    if not e or e["ci95"][0] is None:
        return "UNCLEAR"
    e_lo, e_hi = e["ci95"]
    if e_lo > 0 and la and la["ci95"][0] is not None and la["ci95"][0] > 0:
        return "INFORMATION"
    if e_hi < 0:
        rises = any(v["ci95"][0] is not None and v["ci95"][0] > 0 for v in (c.get("paired") or {}).values())
        if rises:
            return "REVERSION"
        if maker:
            return "ADVERSE"
    return "UNCLEAR"


def analyse(fills: list[Fill], paths: dict[str, MarketPath], horizons=HORIZONS, n_boot: int = N_BOOT,
            seed: int = 0) -> dict:
    rows, missing = [], 0
    for f in fills:
        p = paths.get(f.key)
        if p is None:
            missing += 1
            continue
        rows.append((f, marks(f, p, horizons), p.key))
    groups = {"all": rows, "taker": [r for r in rows if r[0].role == "taker"],
              "maker": [r for r in rows if r[0].role == "maker"]}
    out: dict = {"horizons": [_hkey(h) for h in (*horizons, SETTLE)], "fills_in": len(fills),
                 "fills_without_tape": missing,
                 "anchors": {a: sum(1 for f, _, _ in rows if f.anchor.split(":")[0] == a) for a in ("receive", "block")},
                 "groups": {}}
    for g, rs in groups.items():
        if not rs:
            continue
        gross = curve(rs, "mid", False, horizons, n_boot, seed)
        out["groups"][g] = {"mid_gross": gross, "mid_net": curve(rs, "mid", True, horizons, n_boot, seed),
                            "fair_gross": curve(rs, "fair", False, horizons, n_boot, seed),
                            "fair_net": curve(rs, "fair", True, horizons, n_boot, seed),
                            "shape": classify(gross, maker=g == "maker")}
    return out


def _pmus_key_meta(key: str) -> tuple[str, str] | None:
    from ..tape.universe import HORIZONS as HZ, PMUS_PREFIX
    for a, pref in PMUS_PREFIX.items():
        for h in HZ:
            if key.startswith(f"{pref}-updown-{h}-"):
                return a, h
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="doppel-markout", description="Signed markout curve of fills.")
    ap.add_argument("--data", type=Path, required=True, help="tape root")
    ap.add_argument("--fills", type=Path, required=True, nargs="+")
    ap.add_argument("--intl-stream", help="clob_<asset>_<hz> stream for intl (token-keyed) fills")
    ap.add_argument("--outcomes", type=Path, help="intl: JSON token -> {outcome, end}")
    ap.add_argument("--boot", type=int, default=N_BOOT)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    fills = [f for p in a.fills for f in load_fills(p)]
    if not fills:
        print("no fills", file=sys.stderr)
        return 2
    pm = [f for f in fills if _pmus_key_meta(f.key)]
    other = [f for f in fills if not _pmus_key_meta(f.key)]
    paths: dict[str, MarketPath] = {}
    if pm:
        meta = {f.key: _pmus_key_meta(f.key) for f in pm}
        assets = sorted({m[0] for m in meta.values() if m})
        hz = sorted({m[1] for m in meta.values() if m})
        lo = min(f.t_ns for f in pm) // NS - 900
        hi = max(f.t_ns for f in pm) // NS + 960
        for p in pmus_paths(a.data, assets, hz, lo // 300 * 300, hi, want=set(meta)):
            paths[p.key] = p
    if other:
        if not a.intl_stream:
            print("fills not keyed by PM US slug need --intl-stream", file=sys.stderr)
            return 2
        meta = orjson.loads(a.outcomes.read_bytes()) if a.outcomes else {}
        lo = min(f.t_ns for f in other) // NS - 300
        hi = max(f.t_ns for f in other) // NS + 600
        ip, tx = intl_paths(a.data, a.intl_stream, lo, hi, {f.key for f in other}, meta)
        paths.update(ip)
        fills = pm + reanchor(other, tx)
    res = analyse(fills, paths, n_boot=a.boot, seed=a.seed)
    res.update({"built_tw": time.time_ns(), "inputs": [str(p) for p in a.fills],
                "evidence_scope": "informational markout curve; shape labels are never a gate"})
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(orjson.dumps(res, option=orjson.OPT_INDENT_2))
    for g, v in res["groups"].items():
        by = v["mid_net"]["by_h"]
        print(f"{g:6} fills={v['mid_net']['n_fills']} windows={v['mid_net']['n_windows']} shape={v['shape']} net c/ct: "
              + " ".join(f"{k}={b['mean_c']}" for k, b in by.items() if b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
