"""Receipt-time book observations for retrospective decision studies."""
from __future__ import annotations

from collections import defaultdict, deque
import math
from pathlib import Path

from ..tape.sources.venues import IntlClobMarket
from .dossier import MAX_AGE_S
from .intlbook import NS, IntlBook, LevelEvent, Print, Reset, _records

GRID_S = 10
DEPTH_AGE_NS = int(MAX_AGE_S * NS)
STREAM_STALL_NS = int(IntlClobMarket.stall_s * NS)


def top_of_book(levels: dict | None) -> tuple | None:
    if levels is None:
        return None
    bid = max((p for (p, side), size in levels.items() if side == "BUY" and size > 0), default=None)
    ask = min((p for (p, side), size in levels.items() if side == "SELL" and size > 0), default=None)
    return (bid, levels.get((bid, "BUY")), ask, levels.get((ask, "SELL")))


class _ReceivedBook:
    def __init__(self, flow_tokens: set[str]):
        self.book = IntlBook(strict_clocks=True)
        self.depth: dict[str, tuple] = {}  # latest receipt, venue ms, packet sequence and span object
        self.since: dict[str, int] = {}
        self.flow_tokens = flow_tokens
        self.flow: dict[str, deque[Print]] = defaultdict(deque)
        self.first_market: dict[str, tuple[str, int]] = {}

    def reset(self):
        self.book.reset()
        self.depth.clear()
        self.since.clear()
        self.flow.clear()

    def record(self, rec: dict):
        tw = rec["tw"]
        for msg, items in self.book.feed_messages(rec):
            if not msg:  # feed_messages resets the decoder on control and malformed payloads.
                self.reset()
                yield msg, items
                continue
            kind = msg.get("event_type")
            if kind == "last_trade_price" and not items:
                # An undecodable print prevents an exact volume total over any interval
                # containing it. Other tokens' totals survive a token-identified bad print.
                asset = msg.get("asset_id")
                affected = (asset,) if isinstance(asset, str) and asset else self.flow_tokens
                for token in affected:
                    if token in self.since:
                        self.since[token] = tw + 1
                        self.flow.pop(token, None)
            if kind in ("book", "price_change"):
                ts = msg.get("timestamp")
                valid_clock = type(ts) is int or (isinstance(ts, str) and ts.isascii() and ts.isdecimal())
                if not valid_clock or int(ts) <= 0:
                    self.reset()
                    yield msg, [Reset(tw, "invalid_depth_timestamp")]
                    continue
                touched = ([msg["asset_id"]] if kind == "book"
                           else {ch["asset_id"] for ch in msg.get("price_changes") or []})
                reversed_depth = False
                for asset in touched:
                    old = self.depth.get(asset)
                    if not self.book.known(asset):
                        reversed_depth |= old is not None
                        self.depth.pop(asset, None)
                        self.since.pop(asset, None)
                        self.flow.pop(asset, None)
                        continue
                    span = self.book.spans[asset][-1]
                    reversed_depth |= old is not None and old[3] is not span
                    if old is None or old[3] is not span or tw - old[0] > DEPTH_AGE_NS:
                        self.since[asset] = tw
                        self.flow.pop(asset, None)
                    self.depth[asset] = (tw, span[1], rec["seq"], span)
                    market = self.book.market_of.get(asset)
                    if market:
                        self.first_market.setdefault(asset, (market, tw))
                if reversed_depth:
                    items = [*items, Reset(tw, "asset_venue_time_reversal")]
            for it in items:
                if isinstance(it, Print):
                    if it.market:
                        self.first_market.setdefault(it.asset, (it.market, tw))
                    if it.asset in self.flow_tokens:
                        prints = self.flow[it.asset]
                        while prints and prints[0].tw_ns < tw - NS:
                            prints.popleft()
                        prints.append(it)
            yield msg, items

    def observe(self, asset, cutoff):
        mark = self.depth.get(asset)
        if mark is None:
            return "uninitialized", None
        if cutoff - mark[0] > DEPTH_AGE_NS:
            return "stale_depth", None
        return "known", top_of_book(self.book.levels.get(asset))

    def details(self, asset, cutoff, state, status, level):
        mark = self.depth.get(asset)
        bid, bid_size, ask, ask_size = state or (None, None, None, None)
        flow_ok = status == "known" and self.since[asset] <= cutoff - NS
        prints = [p for p in self.flow.get(asset, ()) if cutoff - NS <= p.tw_ns < cutoff] if flow_ok else []
        # This is token-native feed-message flow, including every observed print on this token.
        # Combining mirrored outcomes or treating messages as unique economic trades needs a separate join.
        return {"status": status, "known": status == "known", "best_bid": bid, "bid_size": bid_size,
                "best_ask": ask, "ask_size": ask_size,
                "level_size": self.book.size(asset, *level) if state is not None and level is not None else None,
                "last_depth_tw_ns": mark[0] if mark else None, "last_depth_venue_ms": mark[1] if mark else None,
                "last_depth_seq": mark[2] if mark else None,
                "flow_status": "covered" if flow_ok else "incomplete",
                "prior_1s_buy_shares": sum(p.size for p in prints if p.side == "BUY") if flow_ok else None,
                "prior_1s_sell_shares": sum(p.size for p in prints if p.side == "SELL") if flow_ok else None,
                "prior_1s_print_count": len(prints) if flow_ok else None}


def scan(root: Path, stream: str, t_from: int, t_to: int, queries: list[tuple],
         cancels: dict[tuple, list], txs: set[str], by_start: dict[int, list[tuple[str, str, int]]],
         length: int, *, grid_from: int | None = None, query_levels: dict | None = None,
         details: bool = False) -> dict:
    """Answer (receipt ns, token, key) queries strictly before applying any equal-time packet.

    The causal state expires at the dossier's maximum quote age. A socket-wide silence past
    the recorder watchdog needs a new snapshot. The independent integrity masks use the next
    receipt envelope to reject queries between a prior packet and a discontinuity or EOF.
    They are retrospective exclusions, never features. Native flow covers [cutoff-1s, cutoff).
    A receive-clock reversal has no unambiguous processing order and rejects this input.
    """
    qs = sorted(queries, key=lambda q: q[0])
    levels = query_levels or {}
    observed = _ReceivedBook({tok for _, tok, _ in qs} if details else set())
    state, detail, state_integrity, grid, grid_integrity = {}, {}, {}, {}, {}
    at_cancel, cancel_diff, fill_tw = {}, {}, {}
    fill_prints = defaultdict(list)
    qi = 0
    start = max(t_from, grid_from) if grid_from is not None else t_from
    next_bin = -(-start // GRID_S) * GRID_S
    prev_tw = None

    def before(tw, integrity):
        nonlocal qi, next_bin
        emitted = []
        while qi < len(qs) and qs[qi][0] <= tw:
            cut, tok, key = qs[qi]
            status, top = observed.observe(tok, cut)
            state[key] = top
            state_integrity[key] = integrity
            if details:
                detail[key] = observed.details(tok, cut, top, status, levels.get(key))
            emitted.append((state_integrity, key))
            qi += 1
        while next_bin * NS <= tw:
            for tok, slug, idx in by_start.get(next_bin - next_bin % length, ()):
                key = (slug, next_bin, idx)
                _, grid[key] = observed.observe(tok, next_bin * NS)
                grid_integrity[key] = integrity
                emitted.append((grid_integrity, key))
            next_bin += GRID_S
        return emitted

    emitted = []
    for rec in _records(root, stream, t_from, t_to, strict=True):
        tw = rec["tw"]
        if not t_from * NS <= tw < t_to * NS:
            continue
        ctl = (rec.get("meta") or {}).get("ctl")
        if ctl == "receive_time_reversal" or (prev_tw is not None and tw < prev_tw):
            raise ValueError(f"non-monotonic receive clock in {stream}, at {tw}")
        stalled = prev_tw is not None and tw - prev_tw > STREAM_STALL_NS
        gap = stalled or (ctl is not None and ctl != "subscribe_add")
        if tw != prev_tw:
            emitted = before(tw, "unbracketed_gap" if gap or prev_tw is None else "covered")
        elif gap:
            for table, key in emitted:
                table[key] = "unbracketed_gap"
        if stalled:
            observed.reset()
        for _, items in observed.record(rec):
            for it in items:
                if isinstance(it, Reset):
                    for table, key in emitted:
                        table[key] = "unbracketed_gap"
                elif isinstance(it, Print) and it.tx in txs:
                    fill_tw.setdefault(it.tx, tw)
                    fill_prints[it.tx].append(it)
                elif isinstance(it, LevelEvent) and it.delta < 0 and cancels:
                    dec = -it.delta
                    near = [c for c in cancels.get((it.asset, it.price, it.side, it.venue_ms), ())
                            if abs(dec - c[1]) <= max(0.02, 0.02 * c[1])
                            and abs(dec - c[1]) < cancel_diff.get(c[0], math.inf)]
                    if near:
                        lv = dict(observed.book.levels.get(it.asset) or {})
                        for e in reversed(items):
                            if isinstance(e, LevelEvent) and e.asset == it.asset:
                                lv[(e.price, e.side)] = e.old_size
                        for key, remaining in near:
                            at_cancel[key], cancel_diff[key] = top_of_book(lv), abs(dec - remaining)
        prev_tw = tw
    before(t_to * NS - 1, "right_censored")
    mates = observed.book.mates()
    mate_known = {a: max(observed.first_market[a][1], observed.first_market[b][1]) for a, b in mates.items()
                  if a in observed.first_market and b in observed.first_market
                  and observed.first_market[a][0] == observed.first_market[b][0] == observed.book.market_of.get(a)}
    return {"state": state, "detail": detail, "state_integrity": state_integrity, "grid": grid,
            "grid_integrity": grid_integrity, "at_cancel": at_cancel, "fill_tw": fill_tw,
            "fill_prints": dict(fill_prints), "mates": mates, "mate_known_tw": mate_known}
