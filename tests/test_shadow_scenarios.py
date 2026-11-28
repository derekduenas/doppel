from dataclasses import replace
from pathlib import Path

import pytest

from doppel.lab import shadow_scenarios as scenarios
from doppel.strategy.genome import Condition, Exit, Genome, MakerRule, Risk, Source, TakerRule, Universe
from doppel.twin.engine import SimConfig, WindowSim
from doppel.twin.features import Features, WindowRef
from doppel.twin.tape import NS, Book, Trade, WindowTape


S = 1795046400
E = S + 300
MS = NS // 1000


def _maker(**kwargs):
    return Genome(name="maker", mode="maker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
                  maker=MakerRule(sides="bid_only", pause_when=(Condition("tau_s", ">=", 1),)),
                  risk=Risk(max_inventory=1000, max_notional_per_window=1000), created_tw=0, **kwargs)


def _taker(end=0, slip=5, **kwargs):
    return Genome(name="taker", mode="taker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
                  taker=(TakerRule("up", (Condition("tau_s", "between", 0, end),), 10,
                                  limit_slippage_c=slip, cooldown_s=0, max_entries_per_window=end + 1),),
                  risk=Risk(max_inventory=1000, max_notional_per_window=1000), created_tw=0, **kwargs)


def _book(at_ms, asks, bids=((0.48, 1000),), start=S):
    return Book(start * NS + at_ms * MS, bids, tuple(asks))


def _tape(books, *, trades=(), gaps=(), start=S):
    return WindowTape("btc-5m", "btc", "5m", start, start + 300, source="ws",
                      books=list(books), trades=list(trades), gaps=list(gaps))


def _ref(start=S):
    return WindowRef("btc", start, start + 300, ptb=100, settle=101)


class _Fair(Features):
    def fair(self, d):
        return 0.5


def _simulate(g, tape, *, tier="api_live_rebated", size=25, cfg=SimConfig()):
    ref = _ref(tape.start)
    sim = scenarios.ScenarioSim(g, tape, ref, cfg, tier=tier, size=size, features=_Fair(g, ref))
    return sim, sim.run()


def test_ioc_walks_l2_and_rebates_gross_fill_rounding():
    g = _taker()
    wt = _tape([_book(-100, [(0.50, 7), (0.53, 9), (0.54, 9)])])
    ref = _ref()
    before = WindowSim(g, wt, ref, features=_Fair(g, ref)).run()
    sim, result = _simulate(g, wt)
    after = WindowSim(g, wt, ref, features=_Fair(g, ref)).run()
    assert before == after
    assert [(f["px"], f["qty"]) for f in result["fills"]] == [(0.50, 7), (0.53, 9), (0.54, 9)]
    assert [f["fee"] for f in result["fills"]] == [0.036, 0.048, 0.048]
    assert result["fees_usd"] == 0.132
    assert result["net_c"] == pytest.approx((25 - (7 * 0.50 + 9 * 0.53 + 9 * 0.54) - 0.132) * 100)
    row = sim.attempts[0].receipt()
    assert row["fill_vwap"] == pytest.approx((7 * 0.50 + 9 * 0.53 + 9 * 0.54) / 25)
    assert result["taker"]["fills"] == 1


def test_liquidity_reuse_requires_observed_replenishment_at_price():
    wt = _tape([_book(-100, [(0.50, 10)]), _book(950, [(0.50, 10)]),
                _book(1950, [(0.50, 5)]), _book(2950, [(0.50, 8)]),
                _book(3950, [(0.49, 1), (0.50, 8)])])
    sim, result = _simulate(_taker(end=4), wt)
    assert [f["qty"] for f in result["fills"]] == [10, 3, 1]
    assert [a.filled for a in sim.attempts] == [10, 0, 0, 3, 1]
    assert all("depth_shortfall" in a.reasons for a in sim.attempts)


def test_limits_and_risk_caps_do_not_disappear_from_attempts():
    wt = _tape([_book(-100, [(0.50, 7), (0.52, 3), (0.53, 60)])])
    g = _taker(slip=2)
    sim, result = _simulate(g, wt)
    assert [(f["px"], f["qty"]) for f in result["fills"]] == [(0.50, 7), (0.52, 3)]
    assert sim.attempts[0].available_depth == 10
    g = replace(g, risk=Risk(max_inventory=5))
    sim, result = _simulate(g, wt)
    assert [(f["px"], f["qty"]) for f in result["fills"]] == [(0.50, 5)]
    assert sim.attempts[0].target == 25
    assert "risk_capped" in sim.attempts[0].reasons


def test_landed_ioc_refuses_gap_stale_and_future_books():
    book = _book(-100, [(0.50, 30)])
    g = _taker()
    wt = _tape([book, _book(300, [(0.50, 100)])], gaps=(S * NS + 100 * MS,))
    sim, result = _simulate(g, wt)
    assert result["fills"] == []
    assert sim.attempts[0].landed_ns == S * NS + 250 * MS
    assert sim.attempts[0].reasons == ["no_landing_book"]
    sim, result = _simulate(g, _tape([book]), cfg=SimConfig(latency_ms=3200))
    assert result["fills"] == []
    assert sim.attempts[0].reasons == ["no_landing_book"]


def test_exit_sweeps_bids_without_entering_capacity_denominator():
    wt = _tape([_book(-100, [(0.50, 25)], ((0.49, 10), (0.47, 15)))])
    g = _taker(exit=Exit(hold_to_settlement=False, flatten_tte_s=299))
    sim, result = _simulate(g, wt, tier="api_base_0695")
    assert [(f["via"], f["px"], f["qty"]) for f in result["fills"]] == [
        ("ioc", 0.50, 25), ("flatten", 0.49, 10), ("flatten", 0.47, 15)]
    assert [f["fee"] for f in result["fills"]] == [0.43, 0.17, 0.26]
    assert [a.kind for a in sim.attempts] == ["entry", "exit"]
    assert result["final_inventory"] == 0


def test_maker_quote_keeps_fifo_and_cancellation_latency():
    wt = _tape(
        [_book(-100, [(0.52, 100)], ((0.48, 10),))],
        trades=[
            Trade(S * NS + 700 * MS, None, 0.48, 15, "sell"),
            Trade(S * NS + 1100 * MS, None, 0.48, 3, "sell"),
            Trade(S * NS + 1400 * MS, S * NS + 1200 * MS, 0.48, 4, "sell"),
            Trade(S * NS + 1500 * MS, None, 0.48, 25, "sell"),
        ],
    )
    sim, result = _simulate(_maker(), wt, tier="api_base_0695")
    row, = [a.receipt() for a in sim.attempts]
    assert row["quote_live_ns"] == S * NS + 250 * MS
    assert row["quote_dead_ns"] == S * NS + 1250 * MS
    assert row["quote_queue_known"]
    assert row["passive_sweep_probe_vwap"] == 0.52
    assert row["filled_qty"] == 12
    assert [f["qty"] for f in result["fills"]] == [5, 3, 4]
    assert [f["fee"] for f in result["fills"]] == [0, 0, 0]


def test_nine_cells_stream_same_windows_and_keep_partial_depth(monkeypatch, tmp_path: Path):
    g = _taker()
    tape = [_tape([_book(-100, [(0.50, 7), (0.53, 9), (0.54, 9)], start=start)], start=start)
            for start in (S, E)]
    reads = []

    def window_iter(*args, depth):
        reads.append(depth)
        yield from tape

    monkeypatch.setattr(scenarios, "iter_windows", window_iter)
    monkeypatch.setattr(scenarios.RefStore, "window_ref", lambda _, asset, start, end, vol: _ref(start))
    monkeypatch.setattr(scenarios.RefStore, "load_boundaries", lambda *_: None)
    traces = []
    panel = [(S, E), (E, E + 300)]
    rows, diagnostics = scenarios.replay_scenarios(tmp_path, None, g, S, E + 300, SimConfig(), panel,
                                                    trace=traces.append)
    assert len(reads) == 1 and reads[0] > SimConfig().depth_levels
    assert len(rows) == 9 and len(traces) == 18
    assert {(r["fee_tier"], r["size"]) for r in rows} == {
        (tier, size) for tier in scenarios.FEE_GRID for size in (25, 100, 500)}
    assert all(r["attempted"] == 2 and r["filled"] == (2 if r["size"] == 25 else 0) for r in rows)
    assert all(r["execution"] == "l2_book_walk_vwap" and r["coefficient"] == scenarios.FEE_GRID[r["fee_tier"]]
               for r in rows)
    assert [w["counts"]["partial"] for w in diagnostics["cells"][1]["windows"]] == [1, 1]
    assert all(len(t["attempts"]) == 1 and len(t["fills"]) == 3 for t in traces)
    monkeypatch.setattr(scenarios, "iter_windows", lambda *args, **kwargs: iter(tape[:1]))
    with pytest.raises(ValueError, match="complete bound window panel"):
        scenarios.replay_scenarios(tmp_path, None, g, S, E + 300, SimConfig(), panel)
