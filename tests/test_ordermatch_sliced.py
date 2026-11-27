"""Sliced ordermatch == whole-span ordermatch on a synthetic tape that crosses many slice
boundaries, and its memory does not grow with the span."""

import random
import tracemalloc
from collections import Counter

import orjson
import pytest

from doppel.lab import ordermatch
from doppel.lab.ordermatch import main, run, run_sliced
from doppel.tape.segment import SegmentWriter

T0 = 1795629600  # hour-aligned
MS = T0 * 1000
STREAM = "clob_btc_5m"
MARKETS = {"m1": ("A1", "A2"), "m2": ("B1", "B2"), "m3": ("C1", "C2"), "m4": ("Z1", "Z2")}
MATE = {a: b for x, y in MARKETS.values() for a, b in ((x, y), (y, x))}
MARKET_OF = {t: m for m, ts in MARKETS.items() for t in ts}
WALLETS = ("0xw", "0xx", "0xy")
SIZES = (25.0, 25.0, 25.0, 40.0, 10.0)


def _book(ms, asset, levels):
    lv = lambda side: [{"price": str(p), "size": str(s)} for (p, sd), s in sorted(levels.items()) if sd == side]  # noqa: E731
    return ms, {"event_type": "book", "asset_id": asset, "market": MARKET_OF[asset], "bids": lv("BUY"),
                "asks": lv("SELL"), "timestamp": str(MS + ms)}


def _pc(ms, changes):
    return ms, {"event_type": "price_change", "market": MARKET_OF[changes[0][0]], "timestamp": str(MS + ms),
                "price_changes": [{"asset_id": a, "price": str(p), "size": str(s), "side": sd}
                                  for a, p, s, sd in changes]}


def _trade(ms, asset, price, size, tx, side):
    return ms, {"event_type": "last_trade_price", "asset_id": asset, "market": MARKET_OF[asset], "price": str(price),
                "size": str(size), "side": side, "timestamp": str(MS + ms), "transaction_hash": tx}


def synth(root, seconds, seed=7, rate=8.0, n_orders=150, boundaries=(60_000, 600_000)):
    """Tape + JSONL orders. Background adds/removes on a few levels of 8 tokens (``rate`` per
    second), mirrored adds, signed orders placed near (and often exactly at) slice boundaries
    with their add 0-2.5 s later, fills printed minutes later, a disconnect and fingerprint-size
    noise on a token no wallet trades. Returns the orders path."""
    rnd = random.Random(seed)
    span = seconds * 1000
    prices = (0.40, 0.41, 0.42, 0.43)
    state = {t: {} for t in MATE}
    msgs = []  # (venue ms, lag ms, message)
    for t in MATE:
        msgs.append((0, 0, _book(0, t, {})[1]))

    def change(ms, asset, price, side, delta):
        k = (price, side)
        new = round(max(0.0, state[asset].get(k, 0.0) + delta), 2)
        state[asset][k] = new
        return asset, price, new, side

    # background book activity, in venue-time order so the replayed state is the generated one
    events = sorted(rnd.randrange(1, span) for _ in range(int(rate * seconds)))
    adds = {}  # order index -> add time
    orders = []
    for n in range(n_orders):
        if n % 3 == 0:
            created = rnd.choice(boundaries) * rnd.randrange(1, max(2, span // rnd.choice(boundaries)))
            created += rnd.choice((-250, -1, 0, 1, 1200, 2499))
        else:
            created = rnd.randrange(1000, span - 5000)
        created = min(max(created, 1000), span - 400_000)
        token = rnd.choice(("A1", "A2", "B1", "B2", "C1", "C2"))
        side = rnd.choice(("BUY", "BUY", "SELL"))
        orders.append(dict(oid=f"o{n}", maker=WALLETS[n % 3 if n % 5 else 0], token=token, side=side,
                           price=rnd.choice(prices), size=rnd.choice(SIZES), created=created))
        if rnd.random() < 0.8:
            adds[n] = created + rnd.choice((-100, 50, 400, 2400, 2600))
    # a second order of the same size and level close by: contested adds
    for n in range(0, n_orders, 7):
        o = dict(orders[n], oid=f"o{n}b", maker=WALLETS[1], created=orders[n]["created"] + rnd.randrange(0, 1500))
        orders.append(o)
    # two orders 1 s apart around one add, the first's window ends just before a 60 s boundary and the
    # second's just after: the nearer (second) takes it only if the first waits for the second
    for j in range(1, (span - 400_000) // 420_000):
        c = j * 420_000 - 63_000
        first = dict(oid=f"s{j}", maker=WALLETS[0], token=("A1", "B1", "C1")[j % 3], side="BUY", price=0.42,
                     size=33.0, created=c)
        orders += [first, dict(first, oid=f"s{j}b", maker=WALLETS[2], created=c + 1000)]
        adds[len(orders) - 2] = c + 1300
    timeline =[(ms, "bg", None) for ms in events] + [(ms, "add", n) for n, ms in adds.items()]
    timeline.append((span // 2 + 777, "reset", None))
    timeline += [(ms, "snap", None) for ms in range(299_990, span, 300_000)]
    fills = []
    for ms, kind, n in sorted(timeline, key=lambda x: (x[0], x[1], -1 if x[2] is None else x[2])):
        lag = 900 if rnd.random() < 0.001 else rnd.choice((40, 41))  # a late frame resets its asset
        if kind in ("reset", "snap"):
            if kind == "reset":
                msgs.append((ms, 40, None))
            for t in MATE:
                msgs.append((ms + 5, 41, _book(ms + 5, t, state[t])[1]))
            continue
        if kind == "add":
            o = orders[n]
            ch = [change(ms, o["token"], o["price"], o["side"], o["size"])]
            if rnd.random() < 0.5:  # shown on the mate as well (one physical add)
                ch.append(change(ms, MATE[o["token"]], round(1 - o["price"], 2),
                                 "SELL" if o["side"] == "BUY" else "BUY", o["size"]))
            msgs.append((ms, lag, _pc(ms, ch)[1]))
            if rnd.random() < 0.85:
                fills.append((ms + rnd.randrange(3_000, 300_000), n, rnd.choice((1.0, 0.5))))
            continue
        asset = rnd.choice(tuple(MATE))
        price = rnd.choice(prices)
        side = rnd.choice(("BUY", "SELL"))
        cur = state[asset].get((price, side), 0.0)
        r = rnd.random()
        delta = (-cur if r < 0.06 else rnd.choice((25.0, 25.0, 40.0, 10.0, 7.0)) if r < 0.6
                 else -min(cur, rnd.choice((5.0, 25.0))))
        if delta:
            msgs.append((ms, lag, _pc(ms, [change(ms, asset, price, side, delta)])[1]))
    rows = []
    for ms, n, frac in fills:
        o = orders[n]
        sh = round(o["size"] * frac, 2)
        tx = f"0xt{n}"
        msgs.append((ms, 50, _trade(ms, o["token"], o["price"], sh, tx, "SELL" if o["side"] == "BUY" else "BUY")[1]))
        rows.append(_row(o, tx, sh, T0 + ms // 1000))
    for o in orders[n_orders:]:  # the contested twins fill too, in their own txs
        ms = o["created"] + 20_000
        msgs.append((ms, 50, _trade(ms, o["token"], o["price"], o["size"], "0xt" + o["oid"],
                                    "SELL" if o["side"] == "BUY" else "BUY")[1]))
        rows.append(_row(o, "0xt" + o["oid"], o["size"], T0 + ms // 1000))
    w = SegmentWriter(root / "tape", STREAM)
    last = 0
    for i, (ms, lag, m) in enumerate(sorted(msgs, key=lambda x: x[0] + x[1])):
        if i % 200 == 0:
            w.flush()  # small zstd frames, like the recorder's 1 s flushes (a reader holds one frame)
        tw = max(last, (MS + ms + lag) * 1_000_000)
        last = tw
        if m is None:
            w.append("", tw=tw, meta={"ctl": "disconnect"})
        else:
            w.append(orjson.dumps([m]).decode(), tw=tw)
    w.close()
    op = root / "orders.jsonl"
    op.write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in rows))
    return op


def _row(o, tx, shares, block_ts):
    ma, ta = (o["price"] * o["size"], o["size"]) if o["side"] == "BUY" else (o["size"], o["price"] * o["size"])
    fa = shares * o["price"] if o["side"] == "BUY" else shares
    return {"order_id": o["oid"], "maker": o["maker"], "token_id": o["token"], "side": o["side"], "size": o["size"],
            "limit": o["price"], "maker_amount": round(ma * 1e6), "taker_amount": round(ta * 1e6),
            "fill_amount": round(fa * 1e6), "created_ms": MS + o["created"], "role": "maker", "tx": tx,
            "block_ts": block_ts, "verified": True}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("sliced")
    seconds = 5400  # 1.5 h: two hour segments, 90 slices of 60 s
    op = synth(root, seconds)
    ref = run(root / "tape", op, T0, T0 + seconds, list(WALLETS), min_orders=1)
    return root, op, seconds, ref


def test_reference_run_is_not_trivial(world):
    _, _, _, (doc, rows, quotes) = world
    w = doc["wallets"]["0xw"]
    ev = sum((Counter(d["placement_evidence_counts"]) for d in doc["wallets"].values()), Counter())
    assert w["observable"] >= 10 and w["matched"] >= 5 and w["fingerprint"]["total"]["candidates"] > 100
    assert ev["unique_candidate"] >= 10 and ev["ambiguous"] >= 3 and ev["level_empty_before_fill"] >= 3
    assert {"unknown:level_zero", "filled:chain"} <= {k for d in doc["wallets"].values() for k in d["outcomes"]}
    assert doc["tape"]["reset:disconnect"] == 1 and len(quotes) > 100 and len(rows) > 50
    assert len(w["fingerprint"]["candidate_outcomes"]) >= 2


@pytest.mark.parametrize("slice_s", [60, 600, 10_000])
def test_sliced_equals_whole_span(world, slice_s):
    root, op, seconds, (doc, rows, quotes) = world
    got = run_sliced(root / "tape", op, T0, T0 + seconds, list(WALLETS), min_orders=1, slice_s=slice_s)
    assert orjson.dumps(got[0], option=orjson.OPT_INDENT_2) == orjson.dumps(doc, option=orjson.OPT_INDENT_2)
    assert got[1] == rows
    assert got[2] == quotes


def test_orders_on_slice_boundaries_are_matched(world):
    _, _, _, (_, rows, _) = world
    edge = [r for r in rows if (r["created_ms"] - MS) % 60_000 in (0, 1, 59_999, 59_750)
            and r["placement_evidence"] == "unique_candidate"]
    crossing = [r for r in rows if (r["created_ms"] - MS) // 60_000 != (r["placed_venue_ms"] - MS) // 60_000]
    assert len(edge) >= 3 and len(crossing) >= 3


def test_cli_sliced_files_match_unsliced(world, tmp_path):
    root, op, seconds, _ = world
    args = ["--data", str(root / "tape"), "--orders", str(op), "--from", str(T0), "--to", str(T0 + seconds),
            "--min-orders", "1"]
    assert main(args + ["--out", str(tmp_path / "a"), "--slice-s", "0"]) == 0
    assert main(args + ["--out", str(tmp_path / "b"), "--slice-s", "120"]) == 0
    for name in ("ordermatch.json", "orders.jsonl"):
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()
    qa, qb = ((tmp_path / d / "quotes.jsonl").read_bytes().splitlines() for d in "ab")
    assert qa != qb and sorted(qa) == sorted(qb)  # completion order when streamed, same rows


def test_several_streams_fall_back_to_whole_span(world, monkeypatch):
    root, op, seconds, (doc, _, _) = world
    calls = []
    monkeypatch.setattr(ordermatch, "run", lambda *a, **k: calls.append(a) or (doc, [], []))
    run_sliced(root / "tape", op, T0, T0 + seconds, list(WALLETS), streams=[STREAM, "clob_other"], slice_s=60)
    assert len(calls) == 1


def _peak(fn) -> int:
    tracemalloc.start()
    try:
        fn()
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_sliced_memory_does_not_grow_with_the_span(tmp_path, capsys):
    peaks = {}
    for hours in (1, 2):
        root = tmp_path / f"h{hours}"
        root.mkdir()
        op = synth(root, hours * 3600, seed=3, rate=15.0, n_orders=60, boundaries=(300_000,))
        args = (root / "tape", op, T0, T0 + hours * 3600, list(WALLETS))
        peaks[hours] = (_peak(lambda: run_sliced(*args, min_orders=1, slice_s=300, quote_sink=lambda r: None)),
                        _peak(lambda: run(*args, min_orders=1, quote_sink=lambda r: None)))
    with capsys.disabled():
        print("\npeak traced MB (sliced 300 s, whole span): "
              + "; ".join(f"{h} h: {s / 2**20:.1f}, {u / 2**20:.1f}" for h, (s, u) in peaks.items()))
    (s1, u1), (s2, u2) = peaks[1], peaks[2]
    assert u2 - u1 > 0.25 * u1  # the whole-span run holds every level event
    # the sliced run grows only by the survey (spans, per-slice venue bounds, 16 bytes per print)
    assert s2 - s1 < 0.15 * (u2 - u1) and s2 < u2
