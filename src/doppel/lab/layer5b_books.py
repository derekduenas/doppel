"""Extract causal BTC PM US book witnesses on a preregistered Layer 5b clock."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

from ..tape.segment import read_segment
from ..tape.universe import HORIZONS, pmus_slug
from ..twin.tape import Book, GROUP_GAP_CTL, NS, STREAM_GAP_CTL, hour_path, rid_group, ws_event
from .taker_entries import MAX_AGE_NS


def _packets(root: Path, stream: str, start_s: int, end_s: int) -> Iterator[dict]:
    previous = 0
    for hour in range(start_s // 3600 * 3600, end_s, 3600):
        path = hour_path(root, stream, hour)
        for row in read_segment(path, strict=True):
            tw = row["tw"]
            if (row.get("stream") != stream or type(tw) is not int
                    or tw < previous or not hour * NS <= tw < (hour + 3600) * NS):
                raise ValueError(f"invalid native book witness clock or stream in {path}")
            previous = tw
            yield row


def _quotes(book: Book | None, cutoff: int) -> dict | None:
    if book is None or not book.open or not 0 < cutoff - book.tw <= MAX_AGE_NS:
        return None
    bid, ask = book.best_bid, book.best_ask
    if bid is None or ask is None or not 0 < bid <= ask < 1:
        return None
    # The PM US up/down contract is one binary book. Down prices are the
    # complement of the opposing Up quote, including for an aggressive order.
    return {"tw_ns": book.tw, "up_bid": bid, "up_ask": ask,
            "down_bid": round(1 - ask, 6), "down_ask": round(1 - bid, 6)}


def book_witnesses(root: Path, horizon: str, start_s: int, end_s: int,
                   step_ms: int = 250) -> Iterator[dict]:
    """Yield every scheduled decision, including explicit missing observations.

    The bounds describe complete, adjacent BTC market windows and must be fixed
    before scoring. Tape reads are sequential and keep only the current and
    immediately upcoming market's most recent book.
    """
    span = HORIZONS.get(horizon)
    if span is None or not 0 < start_s < end_s or start_s % span or end_s % span:
        raise ValueError("book witness bounds must align to complete 5m or 15m BTC windows")
    if type(step_ms) is not int or not 0 < step_ms <= 1000 or span * 1000 % step_ms:
        raise ValueError("book witness step must divide the market duration and be at most one second")
    stream = f"pmus_ws_{horizon}"
    records = _packets(root, stream, start_s, end_s)
    record = next(records, None)
    last_books: dict[str, Book] = {}
    for start in range(start_s, end_s, span):
        market = pmus_slug("btc", horizon, start)
        upcoming = pmus_slug("btc", horizon, start + span)
        last_books.pop(pmus_slug("btc", horizon, start - span), None)
        for cutoff in range(start * NS, (start + span) * NS, step_ms * 1_000_000):
            while record is not None and record["tw"] < cutoff:
                event = ws_event(record, depth=1)
                if event is not None:
                    kind, slug, value, control = event
                    if kind == "book" and slug is not None and slug in (market, upcoming) and isinstance(value, Book):
                        last_books[slug] = value
                    elif kind == "ctl":
                        if control in (*STREAM_GAP_CTL, "backpressure_drop", "receive_time_reversal"):
                            last_books.clear()
                        elif control in GROUP_GAP_CTL and isinstance(value, dict):
                            group = rid_group(value.get("rid"))
                            if group is not None and group[0] == horizon:
                                last_books.pop(pmus_slug("btc", horizon, int(group[1])), None)
                record = next(records, None)
            yield {"ts_ns": cutoff, "market_id": market, "book": _quotes(last_books.get(market), cutoff)}
    # Read through the sealed input tail so a late checksum/clock error cannot
    # silently qualify a completed panel.
    for _ in records:
        pass
