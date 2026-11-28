from __future__ import annotations

from dataclasses import replace

import pytest

import doppel.paper.runtime as runtime
from doppel.paper.epoch import ShadowEpochs, _book_complete
from doppel.paper.journal import iter_records
from doppel.paper.shadow import ShadowSession
from doppel.strategy.genome import Risk
from doppel.strategy.sequential import BLOCK, e_processes
from doppel.twin.engine import SimConfig
from doppel.twin.tape import NS, Book, WindowTape

from test_paper import S, SET, Clock, Tape, maker, market
from test_paper_review import drive
from test_twin import _taker


def test_shadow_monitor_matches_every_prefix_of_frozen_e_process():
    stat = ShadowEpochs(0, 300, 30 * NS, 100)
    values = [25.0] * BLOCK + [220.0] * BLOCK + [-80.0] * BLOCK + [12.0]
    for i, value in enumerate(values):
        stat._observe(i * 300, (i + 1) * 300, value, None)
        ref = e_processes(values[:i + 1], 100)
        summary = stat.snapshot()
        assert summary["n_windows"] == ref["n_windows"]
        assert summary["n_blocks"] == ref["n_blocks"]
        assert summary["e_n"] == (ref["pass"]["e"] if ref["n_blocks"] else None)


def test_open_epoch_blocks_later_settlement_and_incomplete_epoch_never_shifts_blocks():
    stat = ShadowEpochs(0, 300, 30 * NS, 100)
    stat._observe(300, 600, 90, None)
    assert stat.snapshot()["n_windows"] == 0
    stat._observe(0, 300, 15, None)
    assert stat.snapshot()["through_s"] == 600
    stat._observe(600, 900, None, "unsettled_market")
    stat._observe(900, 1200, 90, None)
    assert stat.snapshot()["status"] == "UNMEASURED"
    assert stat.snapshot()["reason"] == "unsettled_market"
    assert stat.snapshot()["n_windows"] == 2
    assert stat.snapshot()["e_n"] is None


def test_shadow_monitor_invalidates_a_loss_bound_violation():
    stat = ShadowEpochs(0, 300, 30 * NS, 100)
    for i in range(BLOCK):
        stat._observe(i * 300, (i + 1) * 300, -101, None)
    assert stat.snapshot()["reason"] == "loss_bound_violated"
    assert stat.snapshot()["e_n"] is None


def test_book_coverage_requires_fresh_valid_opening_and_no_gap():
    def book(s):
        return Book(s * NS, ((0.40, 10.0),), ((0.60, 10.0),))

    tape = WindowTape("x", "btc", "5m", 300, 600, "ws", books=[book(s) for s in range(299, 600, 10)])
    assert _book_complete(tape, 30 * NS)
    assert not _book_complete(replace(tape, gaps=[300 * NS + 1]), 30 * NS)
    assert not _book_complete(replace(tape, gaps=[299 * NS + 1]), 30 * NS)
    assert not _book_complete(replace(tape, books=tape.books[1:]), 30 * NS)
    assert not _book_complete(tape, 9 * NS)


@pytest.mark.parametrize("mode", ["observer", "maker", "taker", "taker_default_marks"])
def test_admitted_monitor_commits_canonical_paper_marks_and_resumes(tmp_path, monkeypatch, mode):
    receipt = ShadowSession("a" * 64, (S + 600) * NS, "b" * 64)
    monkeypatch.setattr(runtime, "authorize", lambda *args: receipt)
    monkeypatch.setattr(ShadowSession, "stop_reason", lambda *args: None)
    g = maker() if mode == "maker" else _taker(risk=Risk(max_loss_per_day=100))
    tape = Tape(tmp_path / "data", market(2))
    clock = Clock(S * NS)
    tape.until(clock.t)
    kwargs = {"shadow_bundle": None if mode == "observer" else tmp_path / "bundle"}
    cfg = SimConfig() if mode == "taker_default_marks" else SimConfig(markouts_s=(2, 10))
    settings = replace(SET, checkpoint_s=90)
    target = tmp_path / "journal"
    rt = runtime.PaperRuntime(g, tape.root, target, cfg, settings, clock=clock, **kwargs)
    rt.start(S)
    drive(rt, tape, clock, clock.t, (S + 315) * NS, 5 * NS)
    resumed = runtime.PaperRuntime(g, tape.root, target, cfg, settings, clock=clock, **kwargs)
    resumed.start(None)
    assert rt.st is not None and resumed.st is not None
    assert resumed.st.shadow_epochs == rt.st.shadow_epochs
    tape.close()
    rows = [r for r in iter_records(target) if r["k"] == "settle"]
    assert len(rows) == 1
    row = rows[0]
    if mode == "observer":
        assert "shadow_monitor" not in row
        return
    monitor = row["shadow_monitor"]
    assert monitor["decay_watchdog"]["status"] == "UNMEASURED"
    assert monitor["e_value"]["reason"] == ("maker_trade_coverage_unmeasured" if mode == "maker"
                                            else "awaiting_complete_block")
    if mode == "maker":
        return
    assert monitor["e_value"]["n_windows"] == 1
    assert monitor["epoch_net_c"] == round(row["net_usd"] * 100, 4)
    if mode == "taker":
        assert monitor["paper_markouts"] == {"status": "MEASURED", "n_paper_fills": 1,
                                             "by_horizon_c": {"2": -10.0, "10": -10.0}}
    else:
        assert monitor["paper_markouts"] == {"status": "UNMEASURED", "n_paper_fills": 1,
                                             "by_horizon_c": {"2": None, "10": None}}
