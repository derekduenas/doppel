from __future__ import annotations

import hashlib
import math

import numpy as np
from dataclasses import replace

import orjson
import pytest

import doppel.paper.runtime as runtime
from doppel.paper.epoch import ShadowEpochs
from doppel.paper.inputs import LiveWindow, RefLive
from doppel.paper.journal import Journal, JournalError, iter_records
from doppel.paper.probability_log import ProbabilityLog
from doppel.paper.shadow import ShadowSession
from doppel.paper.watchdog import MARKETS_PER_BLOCK, HEALTHY, KILL, WARN, DecayBlocks, ShadowWatchdog, paper_marks
from doppel.strategy.genome import Condition, Risk
from doppel.strategy.shadow_probability import ShadowProbability, load_probability
from doppel.tape.universe import pmus_slug
from doppel.twin.engine import P_LAND, Quote, SimConfig, TakerOrder
from doppel.twin.tape import Book, NS, Trade

from test_paper import MS, S, SET, Clock, Tape, _md, maker
from test_paper_review import drive
from test_twin import _taker


def model():
    # Logit(0.8) at 20c spread, logit(0.5) at 2c spread on the dev fixture.
    weight = math.log(4.0) / 18
    return ShadowProbability("a" * 64, 30, -2 * weight, (("spread_c", weight),))


def _model_bytes(g, **overrides):
    return orjson.dumps({"schema": "doppel.shadow-probability/v1", "genome_id": g.id,
                         "fit_end_ns": (S - 301) * NS, "fit_sha256": "1" * 64, "offset_s": 30,
                         "intercept": -1, "weights": {"spread_c": 0.05}, **overrides},
                        option=orjson.OPT_SORT_KEYS)


def test_model_requires_signed_digest_genome_and_pre_replay_calibration(tmp_path):
    g = _taker()
    path = tmp_path / "probability.json"
    good = _model_bytes(g)
    path.write_bytes(good)
    digest = hashlib.sha256(good).hexdigest()
    assert load_probability(path, digest, g, before_ns=S * NS).predict({"spread_c": 20.0}) == .5
    assert load_probability(path, digest, g, before_ns=S * NS).predict({}) is None
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        load_probability(path, "0" * 64, g, before_ns=S * NS)
    with pytest.raises(ValueError, match="prereplay"):
        load_probability(path, digest, g, before_ns=(S - 301) * NS)
    bad = _model_bytes(g, weights={"settlement_outcome": 1})
    path.write_bytes(bad)
    with pytest.raises(ValueError, match="public features"):
        load_probability(path, hashlib.sha256(bad).hexdigest(), g, before_ns=S * NS)


def _book(t, bid=.40, ask=.60):
    return Book(t, ((bid, 25.0),), ((ask, 30.0),))


def test_strict_pre_cutoff_probability_and_real_markout_horizons():
    g = _taker()
    w = LiveWindow("btc", "5m", S, g, SimConfig(), RefLive())
    t = (S + 30) * NS
    w.wt.source = "ws"
    w.wt.books = [_book(t - 1), _book(t, .25, .75), _book(t + 1, .01, .99)]
    first = ShadowWatchdog(model(), g.id).forecast(w, t, t + 3 * NS, active=True)
    w.wt.books[-2:] = [_book(t, .10, .20), _book(t + 1, .70, .72)]
    second = ShadowWatchdog(model(), g.id).forecast(w, t, t + 3 * NS, active=True)
    assert first == second
    assert first["p_hat"] == pytest.approx(.8)
    assert first["p_mkt_mid"] == .5 and first["book_tw_ns"] == t - 1
    assert ShadowWatchdog(model(), g.id).forecast(w, t, w.end * NS, active=True)["p_hat"] is None

    w.sim.fills = [{"t_ns": t, "qty": 10., "px": .50, "side": "buy", "role": "taker"}]
    w.wt.books = [_book(t + ns - 1, p - .01, p + .01) for ns, p in (
        (NS // 2, .51), (2 * NS, .53), (10 * NS, .57), (60 * NS, .59))]
    assert paper_marks(w) == {"markout_500ms": 1., "markout_2s": 3., "markout_10s": 7., "markout_60s": 9.}
    w.wt.gaps = [t + NS]
    w.wt.books.pop(1)
    assert paper_marks(w)["markout_2s"] is None
    w.sim.fills[0]["t_ns"] = (w.end - 60) * NS
    assert paper_marks(w)["markout_60s"] is None  # close is not a market quote


def fixture_market():
    for i in range(61):
        start = S + i * 300
        close = orjson.dumps({"series": [{"candles": [{"timestamp": start - 1, "close": 90_000 + i}]}]}).decode()
        yield "ref_prices", start * NS, close, {"asset": "crypto:btc", "status": 200}
        if i == 60:
            break
        market = pmus_slug("btc", "5m", start)
        width = (.10 + .01 * (i % 2)) if i < 30 else .01
        for t in range(start - 1, start + 300):
            bid, ask = .5 - width, .5 + width
            body = orjson.dumps(_md(market, bid, ask, 0.)).decode()
            yield "pmus_ws_5m", t * NS + MS, body, None


def _rows(path):
    return [orjson.loads(line) for line in path.read_bytes().splitlines()]


def test_sixty_market_shadow_stream_seals_resumes_and_stays_in_warmup(tmp_path, monkeypatch):
    g = _taker(when=(Condition("mid_up_c", ">", 100),), risk=Risk(max_loss_per_day=200))
    session = ShadowSession("1" * 64, (S + 61 * 300) * NS, "2" * 64, model())
    monkeypatch.setattr(runtime, "authorize", lambda *_: session)
    monkeypatch.setattr(ShadowSession, "stop_reason", lambda *_: None)
    tape = Tape(tmp_path / "tape", sorted(fixture_market(), key=lambda r: r[1]))
    clock = Clock(S * NS)
    tape.until(clock.t)
    settings = replace(SET, checkpoint_s=900, heartbeat_s=60, max_step_s=600)
    folder = tmp_path / "journal"
    args = g, tape.root, folder, SimConfig(), settings
    kwargs = {"clock": clock, "shadow_bundle": tmp_path / "bundle"}
    rt = runtime.PaperRuntime(*args, **kwargs)
    rt.start(S)
    drive(rt, tape, clock, clock.t, (S + 40) * NS, 10 * NS)
    stream = folder / "shadow_probs.jsonl"
    first = _rows(stream)
    assert len(first) == 1 and first[0]["stage"] == "forecast" and first[0]["realized_y"] is None
    assert first[0]["status"] == "SEALED" and first[0]["p_hat"] == pytest.approx(.8)
    assert first[0]["observation_ns"] < first[0]["window_end_ns"]
    sealed = next(r for r in iter_records(folder) if "forecast_fsynced_ns" in r)
    assert first[0]["observation_ns"] <= sealed["forecast_fsynced_ns"] < first[0]["window_end_ns"]
    # Exclude restarting from a future state. Repair an interrupted append from the same journal.
    exact = stream.read_bytes()
    stream.write_bytes(exact[:-19])
    resumed = runtime.PaperRuntime(*args, **kwargs)
    resumed.start(None)
    assert stream.read_bytes() == exact
    drive(resumed, tape, clock, clock.t, (S + 60 * 300 + 10) * NS, 25 * NS)
    tape.close()
    assert not resumed.finished
    scores = _rows(stream)
    forecasts = {row["window_id"]: row for row in scores if row["stage"] == "forecast"}
    settlements = [row for row in scores if row["stage"] == "settlement"]
    assert settlements[11]["e_value_running"] == 1.
    assert all(row["net_return_02085"] == 0. for row in settlements)
    for row in settlements:
        pred = forecasts[row["window_id"]]
        assert pred["record_sha256"] == row["forecast_sha256"]
        assert pred["ts_ns"] < pred["window_end_ns"] <= row["ts_ns"]
        assert row["realized_y"] == 1 and row["p_hat"] == pred["p_hat"]
    assert len(settlements) == 60
    assert not any(row["stage"] == "watchdog" for row in scores)
    settled_journal = [r for r in iter_records(folder) if r["k"] == "settle"]
    assert all(r["shadow_monitor"]["decay_watchdog"]["status"] == HEALTHY for r in settled_journal)
    assert not settled_journal[-1]["shadow_monitor"]["decay_watchdog"]["log_loss_measured"]


def test_paper_kill_persists_outside_journal_and_stops_runtime(tmp_path, monkeypatch):
    g = _taker(when=(Condition("mid_up_c", ">", 100),), risk=Risk(max_loss_per_day=200))
    demotion = tmp_path / ".shadow-demotions" / f"{g.id}.json"
    demotion.parent.mkdir()
    active = ShadowSession("1" * 64, (S + 600) * NS, "2" * 64, model(), demotion)
    monkeypatch.setattr(runtime, "authorize", lambda *_: active)
    monkeypatch.setattr(ShadowSession, "stop_reason", lambda *_: None)

    def with_measurement_history(*args):
        wd = ShadowWatchdog(*args)
        metric = wd.detectors["delta_log_loss"]
        for score in [.4] * 20 + [0.] * 19:
            assert metric.observe(score)[1] is False
        wd.block_rows = 99
        wd.block_values["delta_log_loss"] = [0.] * 99
        return wd

    monkeypatch.setattr(runtime, "ShadowWatchdog", with_measurement_history)
    tape = Tape(tmp_path / "tape", sorted(fixture_market(), key=lambda r: r[1]))
    clock = Clock(S * NS)
    tape.until(clock.t)
    folder = tmp_path / g.id
    rt = runtime.PaperRuntime(g, tape.root, folder, SimConfig(), SET, clock=clock,
                              shadow_bundle=tmp_path / "bundle")
    rt.start(S)
    drive(rt, tape, clock, clock.t, (S + 615) * NS, 5 * NS)
    tape.close()
    rows = _rows(folder / "shadow_probs.jsonl")
    assert [row["state"] for row in rows if row["stage"] == "watchdog"] == [WARN, KILL]
    assert rt.finished
    assert orjson.loads(demotion.read_bytes())["receipt_sha256"] == active.receipt_sha256


@pytest.mark.parametrize("truth,mid", [(.55, .5), (.6, .5), (.7, .5), (.8, .6)])
def test_noisy_steady_edge_has_zero_false_demotions_in_500_seeds(truth, mid):
    n = MARKETS_PER_BLOCK
    for seed in range(90500, 91000):
        up = np.random.default_rng(seed).binomial(n, truth, 150)
        means = (up * math.log(truth / mid) + (n - up) * math.log((1 - truth) / (1 - mid))) / n
        sensor = DecayBlocks(.01)
        assert not any(sensor.observe(float(score))[1] for score in means), (truth, mid, seed)


def test_weak_baseline_false_kill_regression_stays_unarmed():
    n, p, mid = MARKETS_PER_BLOCK, .55, .5
    up = np.random.default_rng(55059).binomial(n, p, 150)
    means = (up * math.log(p / mid) + (n - up) * math.log((1 - p) / (1 - mid))) / n
    sensor = DecayBlocks(.01)
    assert not any(sensor.observe(float(score))[1] for score in means)
    assert not sensor.armed


def _noisy_probability_blocks(seed, loss_block):
    n = MARKETS_PER_BLOCK
    r = np.random.default_rng(seed)
    logits = r.normal(0, 1.5, 150 * n)
    q = 1 / (1 + np.exp(-logits))
    outcomes = r.random(len(q)) < q
    mid = 1 / (1 + np.exp(-(logits + r.normal(0, .8, len(q)))))
    p = q.copy()
    if loss_block is not None:
        index = loss_block * n
        p[index:] = 1 / (1 + np.exp(-(logits[index:] + r.normal(0, .8, len(q) - index))))
    return np.where(outcomes, np.log(p / mid), np.log((1 - p) / (1 - mid))).reshape(-1, n).mean(axis=1)


@pytest.mark.parametrize("metric", ["delta_log_loss", "markout_2s", "markout_10s"])
def test_noisy_steady_and_lost_edge_on_independent_block_sequences(metric):
    loss = 60
    hit = 0
    for seed in range(92000, 92200):
        if metric == "delta_log_loss":
            steady = _noisy_probability_blocks(seed, None)
            changed = _noisy_probability_blocks(seed + 1000, loss)
            floor = .01
        else:
            salt = 2000 if metric == "markout_2s" else 3000
            rng = np.random.default_rng(seed + salt)
            steady = rng.normal(0, 2., (150, MARKETS_PER_BLOCK)).mean(axis=1) + .4
            changed = rng.normal(0, 2., (150, MARKETS_PER_BLOCK)).mean(axis=1)
            changed[:loss] += .4
            floor = .10
        sensor = DecayBlocks(floor)
        assert not any(sensor.observe(float(score))[1] for score in steady), (metric, seed)
        sensor = DecayBlocks(floor)
        alarm = next((i for i, score in enumerate(changed) if sensor.observe(float(score))[1]), None)
        assert alarm is None or alarm >= loss, (metric, seed, alarm)
        hit += alarm is not None and alarm - loss + 1 <= 38
    print(f"{metric}: 0/200 steady demotions, {hit}/200 demotions within 38 blocks of loss")
    assert hit >= 180


def test_detector_uses_complete_market_blocks_and_floors_constant_variance():
    wd = ShadowWatchdog(model(), "fixture")
    row = {"monitor_eligible": True, "p_hat": .8, "p_mkt_mid": .5, "realized_y": 1,
           "markout_2s": .2, "markout_10s": .2, "window_id": "fixture"}
    for _ in range(MARKETS_PER_BLOCK - 1):
        assert wd.consume(row) == []
    assert wd.detectors["delta_log_loss"].completed == 0
    assert wd.consume(row | {"monitor_eligible": False}) == []
    assert wd.detectors["delta_log_loss"].completed == 0
    for _ in range(20 * MARKETS_PER_BLOCK):
        assert wd.consume(row) == []
    assert wd.detectors["delta_log_loss"].sigma == .01
    assert wd.detectors["markout_2s"].sigma == .10


def test_late_fsync_aborts_forecast_batch_and_projection_refuses_tampering(tmp_path):
    j = Journal(tmp_path)
    j.open()
    j.append([{"k": "run"}], t_ns=0)
    rec = {"k": "shadow_probability", "row": {"p_hat": 0.6}}

    def after_close():
        raise JournalError("too late")

    with pytest.raises(JournalError, match="too late"):
        j.append([rec, {"k": "cycle"}], 1, forecast_wall=after_close)
    j.open()
    assert [r["k"] for r in iter_records(tmp_path)] == ["run", "recovery"]
    log = ProbabilityLog(tmp_path)
    log.append([rec])
    with pytest.raises(JournalError, match="diverges"):
        log.recover([{**rec, "row": {"p_hat": 0.5}}])


def test_throttle_halves_new_quotes_and_taker_entries_preserving_flatten():
    def emitted(g):
        records = []
        w = LiveWindow("btc", "5m", S, g, SimConfig(), RefLive(), emit=records.append)
        w.sim.throttle(S * NS)
        return w.sim, records

    sim, records = emitted(_taker())
    for rule in (0, -1):
        sim._push(S * NS + sim.L, P_LAND, "ioc", TakerOrder(rule, "buy", 10., .5, S * NS))
    assert [r["qty"] for r in records if r["k"] == "intent"] == [5., 10.]
    sim, records = emitted(maker())
    quote = Quote(side="bid", price=.5, size=10., decided_ns=S * NS, ref_c=50., inv_at=0.)
    sim._schedule_quote("bid", quote, S * NS)
    assert records[-1]["size"] == 5.
    assert sim.pending["bid"][1].remaining == 5.


def test_maker_live_e_value_requires_exact_volume_receipts():
    g = maker()
    w = LiveWindow("btc", "5m", S, g, SimConfig(), RefLive())
    start, end = S * NS, (S + 300) * NS
    w.wt.source = "ws"
    w.wt.books = [_book(t) for t in range(start - NS, end, NS)]
    w.vbooks = [replace(_book(start - 1), shares_traded=10), replace(_book(end + 1), shares_traded=15)]
    w.wt.trades = [Trade(start + NS, start + NS, .5, 5., "sell", "actual")]
    measured = ShadowEpochs.start(g, S, 3).settle(w, {"net_c": 25.}, inactive=False)
    assert measured["e_value"]["n_windows"] == 1
    assert measured["epoch_net_c"] == 25.
    for trades in ([], [replace(w.wt.trades[0], id="inferred:fixture")]):
        w.wt.trades = trades
        blocked = ShadowEpochs.start(g, S, 3).settle(w, {"net_c": 25.}, inactive=False)
        assert blocked["e_value"]["n_windows"] == 0
        assert blocked["e_value"]["reason"] == "maker_trade_coverage_unmeasured"
    w.wt.trades = [Trade(start + NS, start + NS, .5, 5., "sell", "actual")]
    w.wt.stats["trade_gap_events"] = 1
    assert ShadowEpochs.start(g, S, 3).settle(w, {"net_c": 25.}, inactive=False)["epoch_net_c"] is None


def test_positive_markouts_decline_to_zero_and_independently_trip_watchdog():
    monitor = ShadowWatchdog(model(), _taker().id)
    events = []
    for i in range(60):
        value = (2.0 + i % 2) if i < 30 else 0.0
        events.extend(monitor.consume({"window_id": str(i + 1), "monitor_eligible": True,
                                       "p_hat": None, "p_mkt_mid": None, "realized_y": None,
                                       "markout_2s": value, "markout_10s": value}))
    assert [(r["state"], r["window_id"]) for r in events] == [(WARN, "39"), (KILL, "47")]
    assert all(r["metrics"] == ["markout_2s", "markout_10s"] for r in events)
