"""Opening price protocol (decision 2026-11-27): the opening computed from RECEIVED raw BRTI, its
parity tool, and the registry binding of a named evaluation protocol."""

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import orjson
import pytest

from doppel.lab.refprice import settlement_value
from doppel.strategy.cli import main as genome_cli
from doppel.strategy.genome import MakerRule, Genome, Source, Universe, from_dict, to_dict
from doppel.strategy.registry import Registry, RegistryError
from doppel.tape.segment import SegmentWriter
from doppel.twin import evaluate
from doppel.twin.engine import SimConfig, WindowSim
from doppel.twin.features import FeatureConfig, Features, WindowRef, computed_opening
from doppel.twin.tape import NS, Book, WindowTape
from doppel.tape.universe import pmus_slug

REPO = Path(__file__).resolve().parent.parent
S = 1795651200                    # 2026-11-26T00:00Z, a 5m window start
E = S + 300
T0 = S - 420


def _raw(seed=1, rcv_lag=0.7):
    rng = np.random.default_rng(seed)
    raw = 60000.0 + np.cumsum(rng.normal(0, 1.5, E - T0 + 1))
    rcv = np.arange(T0, E + 1, dtype=float) + rcv_lag
    return raw, rcv


def _ref(raw, rcv, published_at=S + 311, value=None, start=S):
    comp, _ = computed_opening(raw, rcv, T0, start)
    ptb = comp if value is None else value
    return WindowRef(asset="btc", start=start, end=start + 300, ptb=ptb, settle=ptb + 5.0, raw=raw, raw_t0=T0,
                     rcv=rcv, ptb_history=((int(published_at * NS), ptb),))


# ---------------------------------------------------------------- the rule
def test_computed_opening_is_the_settlement_average_bit_for_bit():
    raw, rcv = _raw()
    rcv[S - 30 - T0] = S + 4.25
    value, known = computed_opening(raw, rcv, T0, S)
    series = {T0 + i: float(v) for i, v in enumerate(raw)}
    assert value == settlement_value(series, S)                   # exact ==, not approx
    assert value == sum(float(raw[s - T0]) for s in range(S - 60, S)) / 60.0
    assert known == S + 4.25                                      # the last of the 60 to arrive
    # E19: the published series at t is mean(raw[t-59..t]); A(S-1) uses raw[S-60..S-1]
    assert value == pytest.approx(float(np.mean(raw[S - 60 - T0:S - T0])), abs=1e-9)
    gap = raw.copy()
    gap[S - 60 - T0] = np.nan
    assert computed_opening(gap, rcv, T0, S) == (None, None)
    nostamp = rcv.copy()
    nostamp[S - 1 - T0] = np.nan
    assert computed_opening(raw, nostamp, T0, S) == (None, None)


def test_opening_unknown_until_all_60_seconds_are_received():
    raw, rcv = _raw()
    rcv[S - 1 - T0] = S + 6.5                                     # the last second arrives late
    ref = _ref(raw, rcv)
    comp, _ = ref.computed_opening()
    for d in range(S - 5, S + 7):
        assert ref.opening_known(d, "received_raw") == (None, None)
        assert ref.opening_known(d, "published") == (None, None)  # legacy: nothing until S+311
    assert ref.opening_known(S + 7, "received_raw") == ("computed", comp)
    assert ref.opening_known(S + 400, "received_raw") == ("computed", comp)   # sticks after publication
    assert ref.opening_known(S + 311, "published") == ("published", ref.ptb)


def test_published_first_wins_and_ties_go_to_published():
    raw, rcv = _raw(rcv_lag=3.0)                                  # computed known at S - 1 + 3 = S + 2
    early = _ref(raw, rcv, published_at=S + 1, value=60001.0)
    assert early.opening_known(S + 1, "received_raw") == ("published", 60001.0)
    assert early.opening_known(S + 50, "received_raw") == ("published", 60001.0)
    tie = _ref(raw, rcv, published_at=S + 2, value=60001.0)
    assert tie.opening_known(S + 2, "received_raw") == ("published", 60001.0)
    with pytest.raises(ValueError):
        tie.opening_known(S + 2, "whatever")


def test_no_look_ahead_raw_received_after_d_cannot_change_the_opening_at_d():
    raw, rcv = _raw()
    late = S - 10
    rcv[late - T0] = S + 20.0
    g = from_dict(orjson.loads((REPO / "registry/genomes.jsonl").read_bytes().splitlines()[0])["genome"])
    cfg = FeatureConfig(brti_delay_s=2, opening="received_raw")
    for d in (S + 5, S + 25, S + 120):
        base = Features(g, _ref(raw, rcv), cfg)
        before = (base.ref.opening_known(d, "received_raw"), base.at(d, None, 0.0))
        changed = raw.copy()
        future = rcv > d                                          # everything WE had not received by d
        changed[future] += 500.0
        other = Features(g, _ref(changed, rcv, value=base.ref.ptb), cfg)
        after = (other.ref.opening_known(d, "received_raw"), other.at(d, None, 0.0))
        assert repr(before) == repr(after)                        # repr: NaN-equal comparison
    assert Features(g, _ref(raw, rcv), cfg).opening(S + 19) is None
    assert Features(g, _ref(raw, rcv), cfg).opening(S + 20) is not None


def _h1():
    line = (REPO / "registry/genomes.jsonl").read_bytes().splitlines()[0]
    rec = orjson.loads(line)
    g = from_dict(rec["genome"])
    assert g.id == rec["genome_id"] and rec["family"] == "h1-maker-v1"
    return g


def test_sealed_h1_genome_quotes_once_the_60_seconds_are_received():
    """Published row at S+311 (the measured median): under the legacy protocol H1 never quotes in a 5m
    window; under received-raw-opening it quotes from the first decision after S-1 is received."""
    g = _h1()
    rng = np.random.default_rng(5)
    raw = 60000.0 + np.cumsum(rng.normal(0, 0.5, E - T0 + 1))
    rcv = np.arange(T0, E + 1, dtype=float) + 0.7
    books = [Book(tw=(S - 1) * NS + k * NS, bids=((0.45, 200.0),), asks=((0.55, 200.0),)) for k in range(0, 300, 5)]
    wt = WindowTape(slug=pmus_slug("btc", "5m", S), asset="btc", horizon="5m", start=S, end=E, source="ws",
                    books=books, trades=[], gaps=[])
    quoted = {}
    for rule in ("published", "received_raw"):
        ref = _ref(raw, rcv)
        res = WindowSim(g, wt, ref, SimConfig(opening=rule)).run()
        quoted[rule] = res["maker"]["quoted_qty"]
        assert res["opening"]["computed"] == ref.ptb and res["opening"]["published_first_s"] == S + 311
        assert res["opening"]["source"] == {"published": "published", "received_raw": "computed"}[rule]
    assert quoted["published"] == 0
    assert quoted["received_raw"] > 0
    f = Features(g, _ref(raw, rcv), SimConfig(opening="received_raw").feature_config())
    assert f.fair(S) == f.fair(S)                                 # finite at S: S-1 arrived at S-0.3
    assert Features(g, _ref(raw, rcv), SimConfig().feature_config()).fair(S + 310) != \
        Features(g, _ref(raw, rcv), SimConfig().feature_config()).fair(S + 310)   # NaN under legacy


# ---------------------------------------------------------------- parity tool
def _write_tape(data: Path, bump: dict | None = None):
    lo, hi = S - 1800, S + 1800
    rng = np.random.default_rng(7)
    raw = {t: 60000.0 + float(v) for t, v in zip(range(lo, hi), np.cumsum(rng.normal(0, 2, hi - lo)))}
    w = SegmentWriter(data, "ref_brti_raw")
    for t, v in raw.items():
        w.append(orjson.dumps({"t_ms": t * 1000, "v": repr(v), "received": (t + 0.6) * 1000}).decode(), tw=t * NS)
    w.close()
    w = SegmentWriter(data, "ref_prices")
    for t in range(lo + 60, hi):
        if (t + 1) % 300 == 0:
            value = sum(raw[s] for s in range(t - 59, t + 1)) / 60 + (bump or {}).get(t + 1, 0.0)
            w.append(orjson.dumps({"series": [{"candles": [{"timestamp": t, "close": value}]}]}).decode(),
                     tw=(t + 1 + 311) * NS, meta={"asset": "crypto:btc", "status": 200, "from": t, "to": t})
    w.close()


def test_opening_parity_tool_on_synthetic_tape(tmp_path, capsys):
    from doppel.lab.opening_parity import main, parity
    data = tmp_path / "tape"
    _write_tape(data)
    res = parity(data, "btc", S - 900, S + 900)
    five, fifteen = res["horizons"]["5m"], res["horizons"]["15m"]
    assert five["windows"] == 6 and fifteen["windows"] == 2
    assert five["compared"] == 6 and five["max_abs_usd"] == 0.0 and five["share_within_tol"] == 1.0
    assert five["known_after_start"]["computed"]["p50_s"] == pytest.approx(-0.4)
    assert five["known_after_start"]["published"]["p50_s"] == 311.0
    assert five["known_after_start"]["published"]["share_before_end"] == 0.0
    assert five["first_known"] == {"computed": 6, "published": 0, "tie": 0, "neither": 0}
    assert res["pass"] and main(["--data", str(data), "--from", str(S - 900), "--to", str(S + 900)]) == 0

    bad = tmp_path / "bad"
    _write_tape(bad, bump={S: 0.02})                              # one opening 2 cents off
    res = parity(bad, "btc", S - 900, S + 900)
    assert res["horizons"]["5m"]["within_tol"] == 5 and res["horizons"]["5m"]["max_abs_usd"] == pytest.approx(0.02)
    assert not res["pass"]
    assert main(["--data", str(bad), "--from", str(S - 900), "--to", str(S + 900)]) == 1
    capsys.readouterr()


# ---------------------------------------------------------------- binding
NOW = datetime(2026, 11, 27, 12, tzinfo=UTC).timestamp()
CLOSE = datetime(2026, 11, 29, tzinfo=UTC).timestamp()


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def _g(h=1.0):
    return Genome(name=f"m{h:g}", mode="maker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
                  maker=MakerRule(half_spread_c=h), created_tw=0)


def _registry(tmp_path, clock):
    reg = Registry(tmp_path, now=Clock(NOW - 86400), code={"sha": "c0ffee", "dirty": False})
    for h in (1.0, 2.0):
        reg.register(_g(h), "h1", "hyp", ("2026-11-27T00:00:00Z", "2026-11-29T00:00:00Z"))
    reg.register(_g(3.0), "h4", "hyp", ("2026-11-28T00:00:00Z", "2026-12-01T00:00:00Z"))
    reg._now = clock
    return reg


def test_bind_before_close_is_ok_after_close_refused(tmp_path):
    clock = Clock()
    reg = _registry(tmp_path, clock)
    raw = evaluate.protocol_binding(evaluate.RECEIVED_RAW_OPENING)
    rec = reg.bind_protocol(raw, family="h1")
    assert rec["kind"] == "protocol" and rec["genome_ids"] == [_g(1.0).id, _g(2.0).id]
    assert rec["closes_s"] == CLOSE and rec["protocol"]["name"] == "received-raw-opening"
    assert reg.protocol_binding(_g(1.0).id) == rec["protocol"] and reg.protocol_binding(_g(3.0).id) is None
    assert reg.verify() == []
    clock.t = datetime(2026, 12, 1, tzinfo=UTC).timestamp()
    with pytest.raises(RegistryError, match="closed"):
        reg.bind_protocol(raw, family="h4")
    with pytest.raises(RegistryError, match="no registered genome"):
        reg.bind_protocol(raw, family="nope")


def test_double_bind_is_idempotent_or_refused(tmp_path):
    reg = _registry(tmp_path, Clock())
    raw = evaluate.protocol_binding(evaluate.RECEIVED_RAW_OPENING)
    first = reg.bind_protocol(raw, family="h1")
    n = len(reg.lines())
    assert reg.bind_protocol(raw, family="h1") == first and len(reg.lines()) == n    # same: no-op
    with pytest.raises(RegistryError, match="already bound"):
        reg.bind_protocol(evaluate.protocol_binding(evaluate.PUBLISHED_OPENING), family="h1")
    with pytest.raises(RegistryError, match="already bound"):
        reg.bind_protocol(raw, genome_id=_g(1.0).id)
    other_code = {**raw, "code_sha256": "0" * 64}
    with pytest.raises(RegistryError, match="already bound"):
        reg.bind_protocol(other_code, family="h1")
    forged = {**raw, "params": {**raw["params"], "flow_delay_s": 5}}
    with pytest.raises(RegistryError, match="do not hash"):
        reg.bind_protocol(forged, family="h4")
    assert len(reg.lines()) == n and reg.verify() == []


def test_mismatched_protocol_is_refused_by_evaluator_and_registry(tmp_path):
    clock = Clock()
    reg = _registry(tmp_path, clock)
    reg.bind_protocol(evaluate.protocol_binding(evaluate.RECEIVED_RAW_OPENING), family="h1")
    gid = _g(1.0).id
    registration = reg.registration(gid)
    bound = reg.protocol_binding(gid)
    assert bound is not None
    with pytest.raises(ValueError, match="bound to evaluation protocol received-raw-opening"):
        evaluate.replay_snapshot(tmp_path, None, registration, binding=bound,
                                 protocol_name=evaluate.PUBLISHED_OPENING)
    with pytest.raises(ValueError, match="installed evaluator differs"):
        evaluate.replay_snapshot(tmp_path, None, registration, binding={**bound, "code_sha256": "0" * 64})
    clock.t = CLOSE + 1
    legacy = evaluate.protocol(evaluate.PUBLISHED_OPENING)
    rng = (registration["eval_window"]["start_s"], registration["eval_window"]["end_s"])
    verdict = {"p_value": 0.2, "p_source": "twin-replay", "evaluation": legacy}
    with pytest.raises(RegistryError, match="not produced by it"):
        reg.record_result(gid, verdict, rng)
    ok = reg.record_result(gid, {**verdict, "evaluation": evaluate.protocol(evaluate.RECEIVED_RAW_OPENING)}, rng)
    assert ok["binding"] and ok["protocol_unbound"] is False
    assert reg.verify() == []


def test_bound_genome_monitor_event_must_come_from_the_bound_protocol(tmp_path):
    reg = _registry(tmp_path, Clock())
    reg.bind_protocol(evaluate.protocol_binding(evaluate.RECEIVED_RAW_OPENING), family="h1")
    gid = _g(1.0).id
    windows = [{"start": S + 300 * i, "end": S + 300 * (i + 1), "eligible": True, "net_c": 1.0, "contracts": 1}
               for i in range(12)]
    doc = {"genome": {"id": gid}, "windows": windows, "twin": {"diagnostic": False}}
    with pytest.raises(RegistryError, match="not produced by it"):
        reg.record_monitor(gid, doc, source={"evaluation": evaluate.protocol(evaluate.PUBLISHED_OPENING)})


def test_cli_result_runs_the_bound_protocol_and_enforces_parity(tmp_path, monkeypatch):
    from doppel.tape.receipts import build_receipt
    root = tmp_path / "root"
    clock = Clock(0)
    reg = Registry(root, now=clock, code={"sha": "c0ffee", "dirty": False})
    g = _g()
    reg.register(g, "h1", "canonical evaluation", (10, 20))
    reg.bind_protocol(evaluate.protocol_binding(evaluate.RECEIVED_RAW_OPENING), family="h1")
    data = tmp_path / "tape"
    seg = SegmentWriter(data, "pmus_ws_5m")
    seg.append("{}", tw=11_000_000_000)
    seg.close()
    build_receipt(data, "19700101", 0)
    rows = [{"ptb": 100.0, "opening": {"computed": 100.004}}] * 99 + [{"ptb": 100.0, "opening": {"computed": 100.0}}]

    def replay(path, genome, start, end, config, lab):
        assert config == SimConfig(opening="received_raw")
        return rows

    monkeypatch.setattr(evaluate, "run_twin", replay)
    monkeypatch.setattr(evaluate, "verdict", lambda *a, **kw: {"decision": "KILL", "p_value": 0.4})
    opts = ["result", "--root", str(root), "--id", g.id, "--data", str(data)]
    assert genome_cli([*opts, "--protocol", "published-opening"]) == 2          # not the bound one
    assert genome_cli(opts) == 0
    rec = reg.records()[-1]
    ev = rec["verdict"]["evaluation"]
    assert rec["binding"] and ev["protocol"]["name"] == "received-raw-opening" and not ev["protocol_unbound"]
    assert ev["opening_parity"]["share_within_tol"] == 1.0
    rows[:] = [{"ptb": 100.0, "opening": {"computed": 100.02}}] * 2 + [{"ptb": 100.0, "opening": {"computed": 100.0}}] * 98
    n = len(reg.lines())
    assert genome_cli(opts) == 2                                                 # 98% < 99%: refused
    assert len(reg.lines()) == n


def test_unbound_genome_keeps_todays_behaviour_marked_unbound(tmp_path, monkeypatch):
    from doppel.tape.receipts import build_receipt
    root = tmp_path / "root"
    reg = Registry(root, now=Clock(0), code={"sha": "c0ffee", "dirty": False})
    g = _g()
    reg.register(g, "h1", "canonical evaluation", (10, 20))
    data = tmp_path / "tape"
    seg = SegmentWriter(data, "pmus_ws_5m")
    seg.append("{}", tw=11_000_000_000)
    seg.close()
    build_receipt(data, "19700101", 0)
    monkeypatch.setattr(evaluate, "run_twin", lambda path, genome, start, end, config, lab: (
        [] if config == SimConfig() else pytest.fail("legacy expected")))
    monkeypatch.setattr(evaluate, "verdict", lambda *a, **kw: {"decision": "KILL", "p_value": 0.4})
    assert genome_cli(["result", "--root", str(root), "--id", g.id, "--data", str(data)]) == 0
    ev = reg.records()[-1]["verdict"]["evaluation"]
    assert reg.records()[-1]["binding"] and ev["protocol_unbound"] and ev["protocol"]["name"] == "published-opening"


def _rehash(reg, i, fn):
    lines = reg.lines()
    rec = orjson.loads(lines[i])
    fn(rec)
    body = {k: v for k, v in rec.items() if k != "hash"}
    rec["hash"] = hashlib.sha256(orjson.dumps(body, option=orjson.OPT_SORT_KEYS)).hexdigest()
    lines[i] = orjson.dumps(rec, option=orjson.OPT_SORT_KEYS)
    reg.path.write_bytes(b"".join(ln + b"\n" for ln in lines))


def test_verify_detects_protocol_tampering(tmp_path):
    reg = _registry(tmp_path, Clock())
    reg.bind_protocol(evaluate.protocol_binding(evaluate.RECEIVED_RAW_OPENING), family="h1")
    pristine = reg.path.read_bytes()
    assert reg.verify() == []
    lines = pristine.splitlines()
    rec = orjson.loads(lines[-1])
    rec["protocol"]["name"] = "published-opening"                 # edited, hash untouched
    reg.path.write_bytes(b"\n".join([*lines[:-1], orjson.dumps(rec)]) + b"\n")
    assert any("self-hash mismatch" in e for e in reg.verify())
    cases = {
        "do not hash": lambda r: r["protocol"]["params"].update(flow_delay_s=9),
        "after an evaluation window closed": lambda r: r.update(tw=int(CLOSE * 1e9)),
        "differ from its target": lambda r: r.update(genome_ids=r["genome_ids"][:1]),
        "not registered before": lambda r: r.update(genome_ids=["f" * 64]),
    }
    for msg, fn in cases.items():
        reg.path.write_bytes(pristine)
        _rehash(reg, len(lines) - 1, fn)                          # even with a recomputed self-hash
        assert any(msg in e for e in reg.verify()), msg
    reg.path.write_bytes(pristine)
    dup = orjson.loads(lines[-1])
    dup.update(seq=len(lines), prev=hashlib.sha256(lines[-1]).hexdigest())
    dup.pop("hash")
    dup["hash"] = hashlib.sha256(orjson.dumps(dup, option=orjson.OPT_SORT_KEYS)).hexdigest()
    reg.path.write_bytes(pristine + orjson.dumps(dup, option=orjson.OPT_SORT_KEYS) + b"\n")
    assert any("bound to an evaluation protocol twice" in e for e in reg.verify())


def test_bind_on_a_copy_of_the_sealed_registry_lines_leaves_them_untouched(tmp_path):
    """The 21 sealed lines (seq 0-20) as committed; later lines (e.g. the owner's own bind) excluded."""
    from doppel.strategy import dna
    (tmp_path / "registry").mkdir()
    sealed = (REPO / "registry/genomes.jsonl").read_bytes().splitlines(keepends=True)[:21]
    before = b"".join(sealed)
    (tmp_path / "registry/genomes.jsonl").write_bytes(before)
    shutil.copy(REPO / "registry/expression.json", tmp_path / "registry/expression.json")
    assert Registry(tmp_path, now=Clock(NOW)).verify() == []
    raw = evaluate.protocol_binding(evaluate.RECEIVED_RAW_OPENING)
    with pytest.raises(RegistryError, match="closed"):
        Registry(tmp_path, now=Clock(CLOSE)).plan_protocol_binding(raw, family="h1-maker-v1")
    reg = Registry(tmp_path, now=Clock(NOW))
    plan = reg.plan_protocol_binding(raw, family="h1-maker-v1")
    assert (tmp_path / "registry/genomes.jsonl").read_bytes() == before and "hash" not in plan
    rec = reg.bind_protocol(raw, family="h1-maker-v1")
    after = (tmp_path / "registry/genomes.jsonl").read_bytes()
    assert after.startswith(before) and after.count(b"\n") == 22 and rec["seq"] == 21
    assert {k: rec[k] for k in plan} == plan
    assert len(rec["genome_ids"]) == 6 and rec["closes_s"] == CLOSE
    assert reg.verify() == [] and dna.check(tmp_path) == []


def test_cli_bind_protocol_prints_and_needs_execute(tmp_path, capsys):
    root = tmp_path / "root"
    reg = Registry(root, now=Clock(0), code={"sha": "c0ffee", "dirty": False})
    reg.register(_g(), "fam", "hyp", (10, 4_000_000_000))       # closes in 2096: wall clock is fine
    before = reg.path.read_bytes()
    opts = ["bind-protocol", "--root", str(root), "--family", "fam", "--protocol", "received-raw-opening"]
    assert genome_cli(opts) == 0
    out = capsys.readouterr().out
    assert "not written: pass --execute" in out and '"name": "received-raw-opening"' in out
    assert reg.path.read_bytes() == before
    assert genome_cli([*opts[:-1], "nonexistent"]) == 2
    assert genome_cli([*opts, "--execute"]) == 0
    assert "appended seq=1" in capsys.readouterr().out
    assert reg.records()[-1]["kind"] == "protocol" and reg.verify() == []
    assert genome_cli([*opts, "--execute"]) == 0 and "already bound" in capsys.readouterr().out
    assert len(reg.lines()) == 2


def test_bind_refused_once_a_genome_has_evaluation_events(tmp_path):
    clock = Clock()
    reg = _registry(tmp_path, clock)
    gid = _g(1.0).id
    clock.t = CLOSE + 1
    rng = (reg.registration(gid)["eval_window"]["start_s"], reg.registration(gid)["eval_window"]["end_s"])
    rec = reg.record_result(gid, {"p_value": 0.5, "p_source": "manual"}, rng)
    assert rec["protocol_unbound"] is True
    clock.t = NOW
    with pytest.raises(RegistryError, match="already evaluated"):
        reg.plan_protocol_binding(evaluate.protocol_binding(evaluate.RECEIVED_RAW_OPENING), genome_id=gid)


