"""BestQuotes must equal a full rescan of the book after every message (perf change, same output)."""
import random

from doppel.lab.cohort_v2 import BestQuotes, full_best
from doppel.lab.intlbook import IntlBook, LevelEvent


def _drive(msgs):
    book, q = IntlBook(), BestQuotes()
    seen = 0
    for i, m in enumerate(msgs):
        if m == "RESET":
            book.reset()
            continue
        for it in book.apply(m, i):
            if isinstance(it, LevelEvent):
                q.event(it, book.levels.get(it.asset))
        for a, lv in book.levels.items():
            assert q.best(a, lv) == full_best(lv), (i, m)
            seen += 1
    return seen


def _book(a, bids, asks, ts):
    return {"event_type": "book", "asset_id": a, "timestamp": ts,
            "bids": [{"price": p, "size": s} for p, s in bids],
            "asks": [{"price": p, "size": s} for p, s in asks]}


def _chg(a, price, side, size, ts):
    return {"event_type": "price_change", "timestamp": ts,
            "price_changes": [{"asset_id": a, "price": price, "side": side, "size": size}]}


def test_edge_cases():
    msgs = [
        _book("A", [(0.40, 5), (0.41, 3)], [(0.45, 2), (0.47, 1)], 1),  # first snapshot: no events
        _chg("A", 0.42, "BUY", 4, 2),            # better bid arrives
        _chg("A", 0.42, "BUY", 0, 3),            # best bid removed -> rescan
        _chg("A", 0.45, "SELL", 0, 4),           # best ask removed
        _chg("A", 0.44, "SELL", 9, 5),           # better ask
        _chg("A", 0.41, "BUY", 7, 6),            # resize at best
        _book("A", [(0.30, 1)], [], 7),          # snapshot replaces the dict; empty ask side
        "RESET",
        _book("A", [(0.50, 1)], [(0.60, 1)], 8),
        _chg("A", 0.50, "BUY", 0, 9),            # bid side becomes empty
    ]
    assert _drive(msgs) > 0


def test_random_books():
    rng = random.Random(20261128)
    msgs, ts = [], 0
    for _ in range(5000):
        ts += 1
        a = rng.choice("AB")
        r = rng.random()
        if r < 0.03:
            msgs.append("RESET")
        elif r < 0.10:
            ps = rng.sample(range(1, 100), rng.randint(0, 12))
            mid = rng.randint(20, 80)
            msgs.append(_book(a, [(p / 100, rng.randint(1, 5)) for p in ps if p < mid],
                              [(p / 100, rng.randint(1, 5)) for p in ps if p >= mid], ts))
        else:
            msgs.append(_chg(a, rng.randint(1, 99) / 100, rng.choice(("BUY", "SELL")),
                             rng.choice((0, 0, 1, 2, 3)), ts))
    assert _drive(msgs) > 1000
