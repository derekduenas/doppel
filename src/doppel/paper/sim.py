"""The paper venue: the twin's own ``WindowSim``, observed, and driven either live or in batch.

``PaperSim`` subclasses ``twin.engine.WindowSim`` and overrides only hook points. Every override
calls the engine's method and records what it did; none changes a decision, a landing or a fill.
So paper and the twin's replay run one fill engine (pessimistic queue, post-only, IOC at the landed
book, latency L) and one decision path (``Features.at`` -> ``Genome.maker_quotes`` /
``taker_actions``). A disagreement between them can only come from different INPUTS.

Record kinds (``k``), all stamped with the logical time ``t`` (ns, our receive clock) and ``slug``:
  decide  every decision second: ``d``, ``xh`` (hash of the feature vector), and the vector ``x``
          itself when the decision produced an intent or a skip
  intent  post | amend | cancel (maker, per side, with ``ver``) or take (IOC), with ``why``
  ack     the simulated venue response when the order lands: live (with queue ahead),
          rejected:<reason>, superseded, cancelled, filled or an IOC miss reason
  fill    role, side (YES), px, qty, fee, via, order id, inventory and cash after
  skip    reasons a decision could not act (stood_down, no_book, no_fair, paused, ...)
  risk    halt (kill switch or max loss) applied to this window

Batch driving (``record_windows``) is ``twin.engine.run_windows`` with ``PaperSim`` in place of
``WindowSim``; ``tests/test_paper.py`` pins it to ``run_windows`` output.
"""

from __future__ import annotations

import hashlib
import heapq
import math
from typing import Callable, Iterable

import orjson

from ..strategy.genome import Genome
from ..twin.engine import P_DECIDE, SimConfig, TakerOrder, WindowSim, exclusion, window_head
from ..twin.features import Features, WindowRef
from ..twin.tape import NS, WindowTape

Emit = Callable[[dict], None]
SKIP_KEYS = (("taker", "no_book"), ("taker", "empty_side"), ("taker", "risk_blocked"),
             ("maker", "no_fair"), ("maker", "self_cross"), ("maker", "paused_s"))
IOC_OUTCOMES = ("slippage_miss", "no_book", "empty_side", "depth_capped", "risk_blocked", "closed")


def xhash(x: dict) -> str:
    return hashlib.sha256(orjson.dumps(x, option=orjson.OPT_SORT_KEYS)).hexdigest()[:16]


def _clean(x: dict) -> dict:
    return {k: (None if isinstance(v, float) and v != v else v) for k, v in x.items()}


class _Spy:
    """Wraps the window's Features and keeps the last vector the engine asked for."""

    def __init__(self, inner: Features):
        self.inner = inner
        self.last: dict | None = None

    def at(self, d, book, inventory_up):
        self.last = self.inner.at(d, book, inventory_up)
        return self.last


class PaperSim(WindowSim):
    def __init__(self, g: Genome, wt: WindowTape, ref: WindowRef, cfg: SimConfig = SimConfig(),
                 features: Features | None = None, can_open: Callable[[int], bool] | None = None,
                 stood_down: bool = False, emit: Emit | None = None,
                 stand_down_fn: Callable[[], bool] | None = None):
        super().__init__(g, wt, ref, cfg, features=features, can_open=can_open, stood_down=stood_down)
        self.feats = _Spy(self.feats)   # type: ignore[assignment]
        self.emit: Emit = emit or (lambda r: None)
        self.slug = wt.slug
        self.stand_down_fn = stand_down_fn
        self.started = False
        self.halted: str | None = None
        self._why: str | None = None
        self._intents = 0
        self._oid: dict[int, str] = {}
        self._cur_oid: str | None = None

    def rec(self, k: str, t: int, **kw) -> None:
        self.emit({"k": k, "slug": self.slug, "t": t, **kw})
        if k == "intent":
            self._intents += 1

    # ------------------------------------------------------------------ decisions
    def _on_decide(self, d: int) -> None:
        t = d * NS
        if not self.started:
            self.started = True
            if self.stand_down_fn is not None and not self.stood_down:
                self.stood_down = self.stand_down_fn()
            if self.stood_down:
                self.rec("skip", t, d=d, why=[self.halted or "stood_down"])
        if self.stood_down:
            return
        before = {(a, b): self.st.taker[b] if a == "taker" else self.st.maker[b] for a, b in SKIP_KEYS}
        n0 = self._intents
        self.feats.last = None   # type: ignore[attr-defined]
        super()._on_decide(d)
        x = self.feats.last      # type: ignore[attr-defined]
        if x is None:
            return
        skips = [b.removesuffix("_s") for (a, b), v in before.items()
                 if (self.st.taker[b] if a == "taker" else self.st.maker[b]) > v]
        cx = _clean(x)
        r = {"d": d, "xh": xhash(cx), "inv": self.inv}
        if skips or self._intents > n0:
            r["x"] = cx
            if skips:
                r["skips"] = skips
        self.rec("decide", t, **r)

    def _maker(self, t: int, x: dict) -> None:
        g = self.g
        fair = x.get("fair_up", math.nan)
        if fair != fair:
            self._why = "no_fair"
        elif self.inv == 0 and not self.can_open(t):
            self._why = "max_open_windows"
        elif g.maker_paused(x):
            self._why = "paused"
        else:
            b, a = g.maker_quotes(x)
            self._why = "self_cross" if b is not None and a is not None and a <= b else "not_quoted_or_no_room"
        super()._maker(t, x)
        self._why = None

    def _schedule_quote(self, side: str, q, t: int) -> None:
        working = self._working(side)
        kind = "cancel" if q is None else ("amend" if working is not None else "post")
        super()._schedule_quote(side, q, t)
        ver = self.ver[side]
        if q is None:
            self.rec("intent", t, kind=kind, side=side, ver=ver,
                     why=self.halted or self._why or ("flatten" if self.flattening else "cancel"))
            return
        oid = f"{self.slug}:{side}:{ver}"
        self._oid[id(q)] = oid
        self.rec("intent", t, kind=kind, side=side, ver=ver, oid=oid, px=q.price, size=q.size,
                 ref_c=None if q.ref_c != q.ref_c else q.ref_c, why="quote")

    def _push(self, t: int, prio: int, kind: str, payload) -> None:
        super()._push(t, prio, kind, payload)
        if kind == "ioc":
            o: TakerOrder = payload
            self.rec("intent", o.decided_ns, kind="take", rule=o.rule, side=o.side, qty=o.qty,
                     limit=o.limit, land=t, oid=f"{self.slug}:take:{o.decided_ns}:{o.rule}",
                     why="flatten" if o.rule < 0 else "rule")

    # ------------------------------------------------------------------ venue responses
    def _on_quote_land(self, t: int, payload: tuple[str, int]) -> None:
        side, ver = payload
        pend = self.pending.get(side)
        if pend is None or pend[0] != ver:
            super()._on_quote_land(t, payload)
            self.rec("ack", t, side=side, ver=ver, status="superseded")
            return
        had_old = self.live[side] is not None
        before = dict(self.st.maker)
        super()._on_quote_land(t, payload)
        q = pend[1]
        ms = self.st.maker
        if q is None:
            status = "cancelled" if had_old else "noop"
        elif ms["placed"] > before["placed"]:
            status = "live"
        elif ms["post_only_rejects"] > before["post_only_rejects"]:
            status = "rejected:post_only"
        elif ms["no_book_rejects"] > before["no_book_rejects"]:
            status = "rejected:no_book"
        else:
            status = "rejected:closed"
        r: dict = {"side": side, "ver": ver, "status": status, "replaced": had_old}
        if q is not None and status == "live":
            r.update(queue=None if q.queue == math.inf else q.queue, queue_known=q.queue_known)
        self.rec("ack", t, **r)

    def _on_ioc(self, t: int, o: TakerOrder) -> None:
        before = dict(self.st.taker)
        n0 = len(self.fills)
        self._cur_oid = f"{self.slug}:take:{o.decided_ns}:{o.rule}"
        super()._on_ioc(t, o)
        self._cur_oid = None
        ts = self.st.taker
        if len(self.fills) > n0:
            status = "filled"
        else:
            status = next((k for k in IOC_OUTCOMES if ts[k] > before[k]), "unfilled")
        self.rec("ack", t, oid=f"{self.slug}:take:{o.decided_ns}:{o.rule}", status=status)

    def _maker_fill(self, t: int, q, f: float, via: str, ref_qty: float) -> None:
        self._cur_oid = self._oid.get(id(q))
        super()._maker_fill(t, q, f, via, ref_qty)
        self._cur_oid = None

    def _fill(self, t: int, role: str, side: str, px: float, qty: float, via: str, ref_qty: float) -> None:
        super()._fill(t, role, side, px, qty, via, ref_qty)
        f = self.fills[-1]
        self.rec("fill", t, role=role, side=side, px=px, qty=qty, fee=f["fee"], via=via, oid=self._cur_oid,
                 inv=round(self.inv, 6), cash=round(self.cash, 6), fees=round(self.fees, 6))

    # ------------------------------------------------------------------ risk
    def halt(self, t: int, why: str) -> None:
        """Stop deciding and pull every working quote (cancels land one latency later). An IOC already
        in flight cannot be recalled and still lands."""
        if self.halted is not None:
            return
        self.halted = why
        self.rec("risk", t, event="halt", why=why)
        self.stood_down = True
        for side in ("bid", "ask"):
            if self._working(side) is not None:
                self._schedule_quote(side, None, t)

    # ------------------------------------------------------------------ incremental driving (paper)
    def schedule_decisions(self) -> None:
        for d in range(self.S, self.E):
            self._push(d * NS, P_DECIDE, "decide", d)

    def step(self) -> None:
        """Process the next queued event in the engine's order (time, priority, arrival)."""
        t, _, _, kind, payload = heapq.heappop(self._heap)
        self.dispatch(t, kind, payload)

    def advance(self, until_ns: int) -> None:
        while self._heap and self._heap[0][0] <= until_ns:
            self.step()

    def dispatch(self, t: int, kind: str, payload) -> None:
        # the same dispatch as WindowSim.run
        if kind == "book":
            self._on_book(payload)
        elif kind == "gap":
            self.book_ok = False
        elif kind == "trade":
            self._on_trade(t, payload)
        elif kind == "decide":
            self._on_decide(payload)
        elif kind == "ioc":
            self._on_ioc(t, payload)
        elif kind == "quote":
            self._on_quote_land(t, payload)

    def close(self) -> dict:
        """End of the window (all its data received and the outcome known): WindowSim.run's tail."""
        self.advance(2**62)
        for side in ("bid", "ask"):
            q = self.live[side]
            if q is not None and q.dead_ns is None:
                q.dead_ns = self.E_ns
        return self._result()


def record_windows(g: Genome, items: Iterable[tuple[WindowTape, WindowRef]], cfg: SimConfig,
                   emit: Emit) -> list[dict]:
    """``twin.engine.run_windows`` with ``PaperSim`` (same cross-window risk rules, same order)."""
    out: list[dict] = []
    settled: list[tuple[int, float]] = []
    spans: list[tuple[int, int]] = []
    for wt, ref in items:
        why = exclusion(g, wt, ref, cfg.min_trade_coverage, cfg.max_inferred_share, cfg.max_unidentified_share)
        if why == "stale_rest_diagnostic" and ref.outcome is not None:
            res = PaperSim(g, wt, ref, cfg).run()
            out.append({**res, "eligible": False, "excluded": why})
            continue
        if why is not None:
            out.append({**window_head(wt, ref, cfg.fair_lag_s, cfg.opening), "eligible": False, "excluded": why})
            continue
        day = wt.start // 86400
        settled = [(e, n) for e, n in settled if (e - 1) // 86400 >= day]
        spans = [(a, b) for a, b in spans if b > wt.start * NS]
        realized = sum(n for e, n in settled if e <= wt.start and (e - 1) // 86400 == day)
        stood_down = realized <= -g.risk.max_loss_per_day + 1e-12

        def can_open(t: int, sp=tuple(spans)) -> bool:
            return sum(1 for a, b in sp if a <= t < b) < g.risk.max_open_windows

        res = PaperSim(g, wt, ref, cfg, can_open=can_open, stood_down=stood_down, emit=emit).run()
        out.append(res)
        settled.append((wt.end, res["net_usd"]))
        if res["first_fill_ns"] is not None:
            spans.append((res["first_fill_ns"], wt.end * NS))
    return out
