"""Incremental twin inputs: the ``WindowTape`` and ``WindowRef`` a window would have in replay,
built record by record as the tape is received.

Same parsers and rules as the twin (``twin.tape.ws_event``, gap control records, trade dedup and
state filter, the [start - PRE_S, end + POST_S) span, ``infer_unprinted``'s accounting; the
``ref_prices`` boundary rules of ``RefStore.load_boundaries``; BRTI merged with
``brti_import._first_received``). Where the twin's batch replay uses information a live process
cannot have, paper uses what was received and ``docs/PAPER.md`` lists the difference:

1. Book stream: the twin picks, per window, the WS stream with the most snapshots over the whole
   window. Paper uses the per-horizon stream (``pmus_ws_5m`` / ``pmus_ws_15m``).
2. A trade reaches the engine at min(receive, venue time + L) in the twin (our private fill channel).
   Paper learns it at receipt; an event whose time is already past is processed at the next instant
   and counted ``late``.
3. Unprinted volume is inferred from the prints received so far (the twin also counts prints that
   arrive later).
4. The window's outcome is the boundary value received first after the close (the twin uses the
   last revision).
5. Raw BRTI: the twin also reads ``ref_brti_raw`` (PERELMAN's rows, imported to the tape every
   15 min) at PERELMAN's documented receive time. Paper reads PERELMAN's files directly
   (``tail.PerelmanTail``) and a row is known at max(PERELMAN received, the cycle clock that first
   read it); it never reads ``ref_brti_raw``, whose rows reach the tape too late to matter.

Anything that arrives late (``tw`` already behind the processed watermark) is known from the next
instant only: its receive stamp is raised to cursor + 1 ns, so it can never change what an
already-taken decision could have known, nor which opening source a window had chosen.
"""

from __future__ import annotations

import math

import numpy as np
import orjson

from ..lab.brti_import import _first_received, _imported_receive_s
from ..twin.engine import P_BOOK, P_GAP, P_TRADE, SimConfig
from ..twin.features import BOUNDARY, RAW_PAD_S, FeatureConfig, Features, WindowRef
from ..twin.tape import (GROUP_GAP_CTL, INFERRED, NS, POST_S, PRE_S, STREAM_GAP_CTL, UNIDENTIFIED, Book, Trade,
                         WindowTape, rid_group, ws_event)
from ..tape.universe import HORIZONS, pmus_slug
from .sim import PaperSim


class RefLive:
    """Raw BRTI and boundary revisions, as received."""

    def __init__(self) -> None:
        self.brti: dict[int, tuple[float, float]] = {}
        self.bounds: dict[str, dict[int, dict[int, float | None]]] = {}

    def on_perelman(self, rows, now_ns: int) -> list[int]:
        """PERELMAN rows, known at max(their documented receive time, the relay's copy time
        ``relay_tw`` when relayed, ``now_ns`` when first read)."""
        out = []
        for t_ms, v, received, relay_tw in rows:
            floor = max(now_ns, relay_tw if isinstance(relay_tw, int) else 0) / 1e9
            rcv = _imported_receive_s(received)
            _first_received(self.brti, t_ms, v, max(rcv, floor) if math.isfinite(rcv) else floor)
            out.append(t_ms // 1000)
        return out

    def on_brti_live(self, rec: dict, floor_ns: int = 0) -> list[int]:
        out = []
        tw = max(rec["tw"], floor_ns)
        seconds, remainder = divmod(tw, NS)
        received = tw / 1e9
        if remainder and received == seconds:
            received = math.nextafter(received, math.inf)
        for t_ms, value in (rec.get("meta") or {}).get("new") or []:
            _first_received(self.brti, int(t_ms), value, received)
            out.append(int(t_ms) // 1000)
        return out

    def on_prices(self, rec: dict, floor_ns: int = 0) -> list[tuple[str, int]]:
        m = rec.get("meta") or {}
        asset = m.get("asset")
        if not isinstance(asset, str) or not asset.startswith("crypto:") or m.get("status") != 200:
            return []
        a = asset.removeprefix("crypto:")
        tw = max(rec["tw"], floor_ns)
        out = []
        for ser in orjson.loads(rec["raw"]).get("series", []):
            for c in ser.get("candles") or []:
                t = int(c["timestamp"])
                value = float(c["close"])
                if (t + 1) % BOUNDARY != 0 or type(tw) is not int or tw < (t + 1) * NS \
                        or not math.isfinite(value) or value <= 0:
                    continue
                h = self.bounds.setdefault(a, {}).setdefault(t, {})
                if tw not in h:
                    h[tw] = value
                elif h[tw] != value:
                    h[tw] = None
                out.append((a, t))
        return out

    def history(self, asset: str, t: int) -> tuple[tuple[int, float | None], ...]:
        return tuple(sorted(self.bounds.get(asset, {}).get(t, {}).items()))

    def prune(self, before_s: int) -> None:
        for s in [s for s in self.brti if s < before_s]:
            del self.brti[s]
        for b in self.bounds.values():
            for t in [t for t in b if t < before_s]:
                del b[t]


class PaperFeatures(Features):
    """The twin's Features; the opening SOURCE is fixed by the first decision that knew one, as the
    twin's rule fixes it for the whole window. With paper's causal receive stamps the rule cannot
    switch anyway; if it ever would, the first source is kept and the window counts it."""

    def __init__(self, genome, ref: WindowRef, cfg: FeatureConfig):
        super().__init__(genome, ref, cfg)
        self.source: str | None = None
        self.switches = 0

    def opening(self, d: int) -> float | None:
        src, value = self.ref.opening_known(d, self.cfg.opening, self.cfg.fair_lag_s)
        if src is None:
            return None
        if self.source is None:
            self.source = src
        elif src != self.source:
            self.switches += 1
            if self.source == "published":
                return self.ref.opening_at(d * NS)[1]
            return self.ref.computed_opening(self.cfg.fair_lag_s)[0]
        return value


class LiveWindow:
    """One window's tape and reference, growing as records arrive, and its ``PaperSim``."""

    def __init__(self, asset: str, horizon: str, start: int, g, cfg: SimConfig, ref_live: RefLive, **sim_kw):
        self.asset, self.horizon, self.start = asset, horizon, start
        self.end = start + HORIZONS[horizon]
        self.key = (start, HORIZONS[horizon], asset)
        self.slug = pmus_slug(asset, horizon, start)
        self.book_stream = f"pmus_ws_{horizon}"
        self.lo, self.hi = (start - PRE_S) * NS, (self.end + POST_S) * NS
        self.cfg = cfg
        self.wt = WindowTape(slug=self.slug, asset=asset, horizon=horizon, start=start, end=self.end,
                             source="none")
        self.ref = WindowRef(asset=asset, start=start, end=self.end, ptb=None, settle=None, ptb_history=())
        if asset == "btc":
            self.ref.raw_t0 = start - g.fair.vol_window_s - RAW_PAD_S
            n = self.end - self.ref.raw_t0 + 1
            self.ref.raw, self.ref.rcv = np.full(n, np.nan), np.full(n, np.nan)
            for s in range(self.ref.raw_t0, self.end + 1):
                if s in ref_live.brti:
                    self.set_brti(s, ref_live.brti[s])
        self.refresh_bounds(ref_live)
        self.feats = PaperFeatures(g, self.ref, cfg.feature_config())
        self.sim = PaperSim(g, self.wt, self.ref, cfg, features=self.feats, **sim_kw)
        self.sim.schedule_decisions()
        self.seen: set = set()
        self.vbooks: list[Book] = []
        self.inferred_done = 0.0
        self.late = 0
        self.result: dict | None = None

    # ---------------------------------------------------------------- reference
    def set_brti(self, s: int, obs: tuple[float, float]) -> None:
        r = self.ref
        if r.raw is None or r.rcv is None or not r.raw_t0 <= s <= self.end:
            return
        r.raw[s - r.raw_t0], r.rcv[s - r.raw_t0] = obs

    def refresh_bounds(self, ref_live: RefLive) -> None:
        r = self.ref
        hist = ref_live.history(self.asset, self.start - 1)
        r.ptb_history = hist
        r.ptb = hist[-1][1] if hist else None
        fin = ref_live.history(self.asset, self.end - 1)
        r.settle = fin[-1][1] if fin else None

    def settle_time(self, ref_live: RefLive) -> int | None:
        """Receive time at which both boundary values (opening and close) were known, not before
        the window's data span ends; None while either is missing."""
        a, b = ref_live.history(self.asset, self.start - 1), ref_live.history(self.asset, self.end - 1)
        if not a or not b or a[-1][1] is None or b[-1][1] is None:
            return None
        return max(self.hi, a[-1][0], b[-1][0])

    # ---------------------------------------------------------------- market data
    def _push(self, t: int, prio: int, kind: str, payload, cursor: int) -> None:
        if t <= cursor:
            self.late += 1
            t = cursor + 1
        self.sim._push(t, prio, kind, payload)

    def on_book(self, stream: str, b: Book, cursor: int) -> None:
        if stream != self.book_stream or not self.lo <= b.tw < self.hi:
            return
        self.wt.source, self.wt.stream = "ws", stream
        self.wt.books.append(b)
        self._push(b.tw, P_BOOK, "book", b, cursor)
        if b.shares_traded is not None:
            self.vbooks.append(b)
            self._infer(cursor)

    def on_gap(self, t: int, cursor: int) -> None:
        if not self.lo <= t < self.hi:
            return
        self.wt.gaps.append(t)
        self._push(t, P_GAP, "gap", None, cursor)

    def on_trade(self, tr: Trade | None, state, cursor: int) -> None:
        if tr is None or state not in ("TRADE_STATE_NEW", None) or not self.lo <= tr.tw < self.hi:
            return
        key = tr.id or (tr.t_exch, tr.price, tr.qty, tr.aggressor)
        if key in self.seen:
            return
        self.seen.add(key)
        self._add_trade(tr, cursor)

    def _add_trade(self, tr: Trade, cursor: int) -> None:
        self.wt.trades.append(tr)
        if self.cfg.skip_unidentified and (tr.id or "").startswith(UNIDENTIFIED):
            return
        te = tr.tw if tr.t_exch is None else min(tr.tw, tr.t_exch + self.cfg.latency_ns)   # as WindowSim.run
        self._push(te, P_TRADE, "trade", tr, cursor)

    def _infer(self, cursor: int) -> None:
        """``twin.tape.infer_unprinted`` for the newest volume book, from the prints received so far."""
        vb = self.vbooks
        if len(vb) < 2:
            return
        prev, b = vb[-2], vb[-1]
        prints = [t for t in self.wt.trades if not (t.id or "").startswith(INFERRED)]
        printed = sum(t.qty for t in prints if vb[0].tw < t.t_venue <= b.tw)
        interval = [t for t in prints if prev.tw < t.t_venue <= b.tw]
        last = max(interval, key=lambda t: (t.t_venue, t.tw, t.id or "")) if interval else None
        resid = (b.shares_traded or 0.0) - (vb[0].shares_traded or 0.0) - printed - self.inferred_done
        px = b.last_trade_px
        if resid <= 1e-6 or px is None or not 0.0 < px < 1.0:
            return
        bb, ba = prev.best_bid, prev.best_ask
        if bb is not None and px <= bb + 1e-9:
            agg = "sell"
        elif ba is not None and px >= ba - 1e-9:
            agg = "buy"
        elif bb is not None and ba is not None:
            agg = "sell" if px - bb <= ba - px else "buy"
        else:
            return
        q = round(resid, 6)
        tag = UNIDENTIFIED if last is not None and abs(last.price - px) < 1e-9 else INFERRED
        self.inferred_done += q
        self._add_trade(Trade(tw=b.tw, t_exch=prev.tw + 1, price=px, qty=q, aggressor=agg, id=f"{tag}{b.tw}"),
                        cursor)

    @property
    def data_complete_ns(self) -> int:
        return self.hi


def route_ws(stream: str, rec: dict, windows: dict[str, LiveWindow], cursor: int, depth: int) -> None:
    """One WS tape record to the windows it concerns (the twin's ``_parse_stream`` rules)."""
    ev = ws_event(rec, depth)
    if ev is None:
        return
    kind, slug, obj, extra = ev
    if kind == "ctl":
        if extra in (*STREAM_GAP_CTL, "backpressure_drop", "receive_time_reversal"):
            for w in windows.values():
                if w.book_stream == stream:
                    w.on_gap(rec["tw"], cursor)
        elif extra in GROUP_GAP_CTL and isinstance(obj, dict):
            gr = rid_group(obj.get("rid"))
            if gr is not None:
                for w in windows.values():
                    if w.book_stream == stream and w.horizon == gr[1] and w.start == gr[2]:
                        if gr[0] == "md":
                            w.on_gap(rec["tw"], cursor)
                        elif gr[0] == "tr" and w.lo <= rec["tw"] < w.hi:
                            w.wt.stats["trade_gap_events"] = w.wt.stats.get("trade_gap_events", 0) + 1
        return
    w = windows.get(slug or "")
    if w is None:
        return
    if kind == "book":
        assert isinstance(obj, Book)
        w.on_book(stream, obj, cursor)
    elif kind == "trade":
        w.on_trade(obj if isinstance(obj, Trade) else None, extra, cursor)
