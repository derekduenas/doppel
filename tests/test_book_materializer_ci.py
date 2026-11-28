"""Cross-language tape/cache parity and five-pass throughput on sealed fixtures."""

from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import time

import orjson
import pyarrow.parquet as pq
import pytest

from doppel.lab import cohort_v2, markout, ordermatch, orderreplay
from doppel.lab.intlbook import BookObservation, IntlBook, LevelEvent, Print, Reset, replay_messages
from doppel.lab.mechanisms import walk_cost
from doppel.tape.segment import SegmentWriter
from test_cohort_v2 import FREEZE, NA, _build, _write_tape
from test_ordermatch import W, order, orow
from test_orderreplay import FRAMES, ORDERS, QUOTES, T0, append_received, book, pc, trade, write_dir


STREAM = "clob_btc_5m"
NS = 1_000_000_000


@pytest.fixture
def materializer():
    binary = os.environ.get("DOPPEL_MATERIALIZER")
    if not binary:
        pytest.skip("cross-language cache tests run on the book-cache CI job")
    assert Path(binary).is_file()

    def run(tape: Path, cache: Path) -> list[Path]:
        paths = []
        for source in sorted((tape / STREAM).glob("*/*.dtape.zst")):
            path = cache / STREAM / source.parent.name / source.name.replace(".dtape.zst", ".parquet")
            path.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run([binary, "materialize", str(source), str(path)], check=True)
            paths.append(path)
        assert paths
        return paths

    return run


def _output_bytes(directory):
    return {p.name: p.read_bytes() for p in directory.iterdir()}


def _depth(book_state, token, side):
    return tuple(sorted(((price, size) for (price, s), size in book_state.levels.get(token, {}).items()
                         if s == side), reverse=side == "BUY"))


def _witness(tape, cache=None, *, start=T0, end=T0 + 3605):
    state = IntlBook()
    rows = []
    for seq, events in replay_messages(tape, STREAM, start, end, state, book_cache=cache):
        levels = [_depth(state, "A", side) for side in ("BUY", "SELL")]
        sweeps = [[walk_cost(depth, size) for size in (25, 100, 500)] for depth in levels]
        rows.append([seq, [[type(ev).__name__, asdict(ev)] for ev in events], levels, sweeps])
    return orjson.dumps(rows), rows


def test_deep_queue_fills_receipt_order_bbo_and_sweep_match(tmp_path, materializer):
    tape, cache = tmp_path / "tape", tmp_path / "cache"
    writer = SegmentWriter(tape, STREAM)
    for at, message in FRAMES:
        append_received(writer, at * 1_000_000, (at, message))
    # A 500-contract sweep needs level 6. Only that level changes in the second embedded
    # message at the same receive time. Its venue timestamp remains independently visible.
    deep = [(0.61 + i / 100, 60) for i in range(5)] + [(0.66, 300), (0.67, 100)]
    append_received(writer, 10 * NS, book(10_000, "A", [(0.48, 500)], deep))
    append_received(writer, 11 * NS, pc(10_998, "A", 0.66, 201, "SELL"),
                    pc(10_999, "A", 0.66, 200, "SELL"),
                    trade(11_000, "A", 0.61, 10, "BUY", "deep"))
    append_received(writer, 12 * NS, book(12_000, "A", [(0.48, 500)], deep))
    writer.append("", meta={"ctl": "disconnect"}, tw=(T0 + 13) * NS)
    append_received(writer, 14 * NS, pc(14_000, "A", 0.46, 19, "BUY"))
    append_received(writer, 15 * NS, book(15_000, "A", [(0.48, 500)], deep))
    changed = [*deep[:5], (0.66, 199), deep[-1]]
    append_received(writer, 16 * NS, book(16_000, "A", [(0.48, 500)], changed))
    append_received(writer, 3601 * NS, pc(3_601_000, "A", 0.66, 199, "SELL"))
    writer.close()
    outputs = materializer(tape, cache)
    raw, observations = _witness(tape)
    cached, _ = _witness(tape, cache)
    assert cached == raw
    # The slice starts on a changed snapshot without its Parquet predecessor.
    # Replaying the cached snapshot alone must still seed the complete book.
    assert _witness(tape, cache, start=T0 + 16)[0] == _witness(tape, start=T0 + 16)[0]
    events = [event for row in observations for event in row[1]]
    assert any(name == Reset.__name__ and item["why"] == "disconnect" for name, item in events)
    assert any(name == BookObservation.__name__ for name, _ in events)
    assert any(name == LevelEvent.__name__ and item["price"] == 0.66 for name, item in events)
    assert any(row[3][1][-1] is not None for row in observations)
    compact = pq.read_table(outputs[0], columns=["ts_ns", "event_type", "asset_id", "ask_px"]).to_pylist()
    assert not any(r["ts_ns"] == (T0 + 11) * NS and r["event_type"] == "book" for r in compact)
    assert any(r["ts_ns"] == (T0 + 11) * NS and r["event_type"] == "trade" for r in compact)
    assert all(r["ask_px"] is None or len(r["ask_px"]) == 5 for r in compact)

    order_rows = tmp_path / "decoded.jsonl"
    decoded = orow(order("q1", "A", "BUY", 0.5, 50, 990, fills={"t1": (40, T0 + 2)}))
    order_rows.write_bytes(b"".join(orjson.dumps({**row, "verified": True}) + b"\n" for row in decoded))
    observed_orders = tmp_path / "matched-fixture"
    write_dir(observed_orders, ORDERS, QUOTES)
    for label, runner, inputs in (
        ("match", ordermatch.main, ["--orders", str(order_rows), "--wallet", W, "--slice-s", "3"]),
        ("replay", orderreplay.main, ["--ordermatch-dir", str(observed_orders)]),
    ):
        results = []
        for idx, opt in enumerate(([], ["--book-cache", str(cache)])):
            out = tmp_path / f"{label}-{idx}"
            assert runner(["--data", str(tape), "--from", str(T0), "--to", str(T0 + 3605),
                           "--out", str(out), *inputs, *opt]) == 0
            results.append(_output_bytes(out))
        assert results[0] == results[1]
        if label == "replay":
            assert any(row["optimistic"]["fills"] for row in map(
                orjson.loads, results[0]["orderreplay.jsonl"].splitlines()))

    baseline, tx = markout.intl_paths(tape, STREAM, T0, T0 + 3605, {"A", "B"})
    accelerated, accelerated_tx = markout.intl_paths(tape, STREAM, T0, T0 + 3605, {"A", "B"}, book_cache=cache)
    assert orjson.dumps(accelerated_tx) == orjson.dumps(tx)
    assert orjson.dumps({k: asdict(p) for k, p in accelerated.items()}) == orjson.dumps(
        {k: asdict(p) for k, p in baseline.items()})


def test_freeze_receipt_bytes_match_with_materialized_books(tmp_path, materializer):
    tape, cache = tmp_path / "tape", tmp_path / "cache"
    tokens = tmp_path / "token-labels.json"
    tokens.write_bytes(orjson.dumps(_write_tape(tape, range(NA))))
    materializer(tape, cache)
    assert orjson.dumps(_build(tape, tokens)) == orjson.dumps(_build(tape, tokens, book_cache=cache))
    assert not (tmp_path / "cohort-v1.json").exists()
    assert FREEZE % 3600 == 0


def test_five_pass_compact_projection_benchmark(tmp_path, materializer):
    tape, cache = tmp_path / "tape", tmp_path / "cache"
    writer = SegmentWriter(tape, STREAM)
    # Extend the existing A/m1 fixture with a snapshot-heavy hour, where most changes
    # are behind the touch. Both paths run the same five top-five scans.
    for at, message in FRAMES:
        append_received(writer, at * 1_000_000, (at, message))
    bids = [(tick / 100, 25) for tick in range(1, 50)]
    for i in range(2_400):
        at = 12_000 + i * 20
        asks = [(tick / 100, 20) for tick in range(51, 95)] + [(0.95, 21 + i % 7)]
        if i % 120 < 10:
            asks[0] = (0.51, 35)
        append_received(writer, at * 1_000_000, book(at, "A", bids, asks))
    writer.close()
    (compact,) = materializer(tape, cache)

    def raw_projection():
        state = IntlBook()
        previous, projected = {}, []
        for _, events in replay_messages(tape, STREAM, T0, T0 + 3600, state):
            touched = set()
            for ev in events:
                if isinstance(ev, Reset):
                    previous.clear()
                elif not isinstance(ev, Print):
                    touched.add(ev.asset)
            for asset in sorted(touched):
                if not state.known(asset):
                    continue
                book = tuple(tuple(depth[:5]) for depth in (_depth(state, asset, "BUY"), _depth(state, asset, "SELL")))
                if previous.get(asset) == book:
                    continue
                previous[asset] = book
                projected.append((state.market_of.get(asset, ""), events[0].tw_ns, asset, book))
        return sorted(projected, key=lambda row: (row[0], row[1], row[2]))

    def cache_projection():
        out = []
        for r in pq.read_table(compact, columns=["market_id", "ts_ns", "asset_id",
                                               "bid_px", "bid_sz", "ask_px", "ask_sz"],
                               filters=[("event_type", "=", "book")]).to_pylist():
            levels = tuple(tuple((p, q) for p, q in zip(r[side + "_px"], r[side + "_sz"], strict=True)
                                 if p is not None) for side in ("bid", "ask"))
            out.append((r["market_id"], r["ts_ns"], r["asset_id"], levels))
        return sorted(out, key=lambda row: (row[0], row[1], row[2]))

    expected = raw_projection()
    assert cache_projection() == expected

    def timed(scan):
        start = time.perf_counter()
        for _ in range(5):
            assert scan() == expected
        return time.perf_counter() - start

    raw_s = timed(raw_projection)
    cache_s = timed(cache_projection)
    print(f"5-pass compact top-5 projection: raw={raw_s:.3f}s cache={cache_s:.3f}s speedup={raw_s / cache_s:.2f}x")
    assert raw_s / cache_s >= 15

    def lossless_replay(directory):
        state = IntlBook()
        return sum(len(events) for _, events in replay_messages(tape, STREAM, T0, T0 + 3600, state,
                                                                book_cache=directory))

    replayed = lossless_replay(None)
    samples = []
    for variant in (None, cache):
        start = time.perf_counter()
        for _ in range(5):
            assert lossless_replay(variant) == replayed
        samples.append(time.perf_counter() - start)
    raw_s, cache_s = samples
    print(f"5-pass full-depth replay: raw={raw_s:.3f}s cache={cache_s:.3f}s speedup={raw_s / cache_s:.2f}x")
