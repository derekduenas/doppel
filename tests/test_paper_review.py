"""Regression tests for the paper-runtime review (M1, M2, S1-S7 and the plausible items)."""

from __future__ import annotations

import pickle
import tracemalloc
import zlib
from pathlib import Path

import orjson
import pytest
import zstandard

import test_paper as T
from doppel.paper import journal, runtime
from doppel.paper.agree import agree
from doppel.paper.cli import main as paper_main
from doppel.paper.inputs import PaperFeatures
from doppel.paper.journal import Journal, JournalError
from doppel.paper.runtime import PaperRuntime, RecoveryError, Settings, check_binding, open_orders
from doppel.paper.tail import StreamTail, TapeError, read_new_frames
from doppel.strategy.genome import from_dict, to_dict
from doppel.tape.segment import SegmentWriter
from doppel.tape.universe import pmus_slug
from doppel.twin.engine import SimConfig
from doppel.twin.features import WindowRef

S, NS, MS = T.S, T.NS, T.MS
SET = T.SET


def drive(rt, tape, clock, t0, t1, step_ns):
    t = t0
    while t < t1:
        t += step_ns
        clock.t = t
        tape.until(t)
        rt.cycle()
    return t


def fresh(tmp_path, recs, settings=SET, g=None, jname="j", tape_cls=None):
    g = g or T.maker()
    data = tmp_path / "data"
    tape = (tape_cls or T.Tape)(data, recs)
    clock = T.Clock((S - 700) * NS)
    tape.until(clock.t)
    rt = PaperRuntime(g, data, tmp_path / jname, SimConfig(), settings, clock=clock)
    rt.start(S)
    return rt, tape, clock


# ---------------------------------------------------------------- M1: order ids survive a checkpoint
def test_m1_order_id_lives_on_the_quote_and_survives_pickle(tmp_path):
    rt, _ = T.run_paper(tmp_path, T.maker(), T.market(2), end=S + 150)
    st2 = pickle.loads(pickle.dumps(rt.st))
    n = 0
    for slug, w in st2.windows.items():
        for side, q in w.sim.live.items():
            if q is not None:
                assert getattr(q, "paper_oid") == getattr(rt.st.windows[slug].sim.live[side], "paper_oid")
                n += 1
    assert n > 0


def test_m1_restart_between_a_post_and_its_fill(tmp_path):
    g = T.maker()
    st = Settings(grace_s=0.0, checkpoint_s=20, heartbeat_s=30)
    _, ref = T.run_paper(tmp_path / "one", g, T.market(2), settings=st)
    recs = journal.load(ref)
    posted = {r["oid"]: r["t"] for r in T.kinds(recs, "intent") if r.get("oid")}
    stop = next(t for f in T.kinds(recs, "fill") if f["role"] == "maker"
                for t in range(S - 700, S + 700) if posted[f["oid"]] < t * NS < f["t"])
    rt, tape, clock = fresh(tmp_path, T.market(2), settings=st)
    t = drive(rt, tape, clock, (S - 700) * NS, (stop - 60) * NS, 7 * NS)
    t = drive(rt, tape, clock, t, stop * NS, 1 * NS)
    assert any(q is not None for w in rt.st.windows.values() for q in w.sim.live.values())
    assert any(r.get("ck") for r in journal.load(rt.journal.dir) if r["k"] == "cycle")
    del rt
    rt2 = PaperRuntime(g, tmp_path / "data", tmp_path / "j", SimConfig(), st, clock=clock)
    rt2.start(None)
    drive(rt2, tape, clock, t, (S + 640) * NS, 7 * NS)
    tape.close()
    got = journal.load(tmp_path / "j")
    assert not [r for r in T.kinds(got, "fill") if r.get("oid") is None]
    assert T.by_window(ref) == T.by_window(tmp_path / "j")


@pytest.mark.parametrize("stop", range(S + 20, S + 600, 69))
def test_m1_restart_anywhere(tmp_path, stop):
    """The reviewer's loop (2 of 25 failed): a restart at any point recovers."""
    rt, j = T.run_paper(tmp_path, T.maker(), T.market(2), end=stop)
    rt2 = PaperRuntime(T.maker(), tmp_path / "data", j, SimConfig(), SET, clock=T.Clock(stop * NS))
    rt2.start(None)
    assert not [r for r in journal.load(j) if r["k"] == "fill" and r.get("oid") is None]


# ---------------------------------------------------------------- M2: late records after merged cycles
def late_market():
    """ref_brti_live rows for every 7th second reach the tape 30 s late (a delayed source)."""
    out = []
    for s, tw, raw, meta in T.market(1):
        vis = tw + 30 * NS if s == "ref_brti_live" and (tw // NS) % 7 == 0 else tw
        out.append((s, tw, raw, meta, vis))
    out.sort(key=lambda r: (r[4], r[1]))
    return out


class LateTape(T.Tape):
    def until(self, t_ns):
        while self.i < len(self.recs) and self.recs[self.i][4] <= t_ns:
            s, tw, raw, meta, _ = self.recs[self.i]
            self.w[s].append(raw, tw=tw, meta=meta)
            self.i += 1
        for w in self.w.values():
            w.flush()


def test_m2_restart_reproduces_late_records_after_uncommitted_cycles(tmp_path):
    st = Settings(grace_s=0.0, checkpoint_s=40, heartbeat_s=30)
    recs = late_market()
    one, tape1, c1 = fresh(tmp_path / "one", recs, settings=st, tape_cls=LateTape)
    drive(one, tape1, c1, (S - 700) * NS, (S - 120) * NS, 7 * NS)
    drive(one, tape1, c1, (S - 120) * NS, (S + 330) * NS, 250 * MS)
    tape1.close()
    ref = journal.load(one.journal.dir)
    assert len(T.kinds(ref, "late")) > 10
    cycles = T.kinds(ref, "cycle")
    assert any(c["c0"] != p["W"] for p, c in zip(cycles, cycles[1:]))   # uncommitted cycles were merged
    rt, tape, clock = fresh(tmp_path, recs, settings=st, tape_cls=LateTape)
    drive(rt, tape, clock, (S - 700) * NS, (S - 120) * NS, 7 * NS)
    t = drive(rt, tape, clock, (S - 120) * NS, (S + 150) * NS, 250 * MS)
    del rt
    rt2 = PaperRuntime(T.maker(), tmp_path / "data", tmp_path / "j", SimConfig(), st, clock=clock)
    rt2.start(None)                    # failed before: replay processed the late rows differently
    drive(rt2, tape, clock, t, (S + 330) * NS, 250 * MS)
    tape.close()
    assert T.by_window(one.journal.dir) == T.by_window(tmp_path / "j")


def test_m2_late_rows_never_change_past_knowledge(tmp_path):
    rt, tape, clock = fresh(tmp_path, late_market(), tape_cls=LateTape)
    drive(rt, tape, clock, (S - 700) * NS, (S + 100) * NS, 7 * NS)
    w = rt.st.windows[pmus_slug("btc", "5m", S)]
    late = [r for r in T.kinds(journal.load(rt.journal.dir), "late") if r["stream"] == "ref_brti_live"]
    assert late
    for r in late:                     # each late BRTI second is known only from its late arrival on
        sec = r["tw"] // NS
        if w.ref.raw_t0 <= sec <= w.end:
            assert w.ref.rcv[sec - w.ref.raw_t0] * NS >= r["t"] - 1000


# ---------------------------------------------------------------- S1: PERELMAN raw BRTI, causally
def perelman_market():
    """No ref_brti_live on the tape: BRTI comes from PERELMAN files (written as they are received);
    the tape also holds ref_brti_raw import rows, which paper must ignore."""
    out = []
    for s, tw, raw, meta in T.market(1):
        if s == "ref_brti_live":
            t_ms, v = meta["new"][0]
            out.append(("perelman", tw, orjson.dumps({"kind": "brti", "new": [[t_ms, v]],
                                                      "received": tw / 1e9}).decode(), None))
            out.append(("ref_brti_raw", t_ms * MS, orjson.dumps({"t_ms": t_ms, "v": v, "received": tw / 1e9}).decode(),
                        None))
        else:
            out.append((s, tw, raw, meta))
    return sorted(out, key=lambda r: r[1])


class PerelmanTape(T.Tape):
    def __init__(self, root, recs):
        super().__init__(root, recs)
        self.w["ref_brti_raw"] = SegmentWriter(root, "ref_brti_raw")
        self.src = root.parent / "perelman"
        self.src.mkdir(parents=True, exist_ok=True)

    def until(self, t_ns):
        while self.i < len(self.recs) and self.recs[self.i][1] <= t_ns:
            s, tw, raw, meta = self.recs[self.i]
            if s == "perelman":
                with open(self.src / "brti-20261119.jsonl", "ab") as fh:
                    fh.write(raw.encode() + b"\n")
            else:
                self.w[s].append(raw, tw=tw, meta=meta)
            self.i += 1
        for w in self.w.values():
            w.flush()


def test_s1_perelman_rows_known_at_max_received_and_read_time(tmp_path):
    st = Settings(grace_s=0.0, checkpoint_s=60, heartbeat_s=30, perelman_src=str(tmp_path / "perelman"))
    recs = perelman_market()
    rt, tape, clock = fresh(tmp_path, recs, settings=st, tape_cls=PerelmanTape)
    t = drive(rt, tape, clock, (S - 700) * NS, (S - 30) * NS, 7 * NS)
    t = drive(rt, tape, clock, t, (S + 150) * NS, 250 * MS)
    w = rt.st.windows[pmus_slug("btc", "5m", S)]
    reads = {}
    for c in T.kinds(journal.load(rt.journal.dir), "cycle"):
        if "perelman" in c.get("offsets", {}):
            reads[c["now"]] = c
    assert reads
    rcv = [x for x in w.ref.rcv if x == x]
    raw_rows = {r[1] // MS // 1000: r[1] / 1e9 for r in recs if r[0] == "perelman"}
    for i, x in enumerate(w.ref.rcv):
        sec = w.ref.raw_t0 + i
        if x == x and sec in raw_rows:
            assert x >= raw_rows[sec]                          # never before PERELMAN received it
            assert any(abs(x - now / 1e9) < 1e-6 for now in reads) or x == raw_rows[sec]
    assert rcv and any(r.get("x", {}).get("fair_up") is not None for r in T.kinds(journal.load(rt.journal.dir), "decide"))
    # a restart replays the PERELMAN reads exactly
    del rt
    rt2 = PaperRuntime(T.maker(), tmp_path / "data", tmp_path / "j", SimConfig(), st, clock=clock)
    rt2.start(None)
    drive(rt2, tape, clock, t, (S + 200) * NS, 250 * MS)
    tape.close()


def test_s1_imported_ref_brti_raw_on_the_tape_is_never_used(tmp_path):
    """Reviewer raw_lookahead: with BRTI only in ref_brti_raw (import rows), paper has no fair."""
    recs = [r for r in perelman_market() if r[0] != "perelman"]
    rt, tape, clock = fresh(tmp_path, recs, tape_cls=PerelmanTape)
    drive(rt, tape, clock, (S - 700) * NS, (S + 200) * NS, 7 * NS)
    tape.close()
    d = T.kinds(journal.load(rt.journal.dir), "decide")
    assert d and not [r for r in d if (r.get("x") or {}).get("fair_up") is not None]
    assert not T.kinds(journal.load(rt.journal.dir), "intent")


def test_s1_opening_source_is_fixed_for_the_window():
    g = T.maker()
    raw_t0 = S - 420
    import numpy as np
    raw = np.full(S + 300 - raw_t0 + 1, 100.0)
    rcv = np.array([float(raw_t0 + i) + 0.5 for i in range(len(raw))])
    ref = WindowRef(asset="btc", start=S, end=S + 300, ptb=100.0, settle=101.0, raw=raw, raw_t0=raw_t0, rcv=rcv,
                    ptb_history=((S * NS + 10 * NS, 100.5),))
    f = PaperFeatures(g, ref, SimConfig(opening="received_raw").feature_config())
    assert f.opening(S) == pytest.approx(100.0) and f.source == "computed"
    ref.ptb_history = ((S * NS - 5 * NS, 100.5),)      # a published row that claims to be earlier
    assert f.opening(S + 20) == pytest.approx(100.0) and f.source == "computed" and f.switches == 1


# ---------------------------------------------------------------- S2: opening rule and protocol binding
def _registry_h_genome():
    for line in (T.REPO / "registry" / "genomes.jsonl").read_bytes().splitlines():
        r = orjson.loads(line)
        if r["kind"] == "protocol":
            gid = r["genome_ids"][0]
            for line2 in (T.REPO / "registry" / "genomes.jsonl").read_bytes().splitlines():
                q = orjson.loads(line2)
                if q["kind"] == "register" and q["genome_id"] == gid:
                    return from_dict(q["genome"]), r["protocol"]
    raise AssertionError("no bound genome in the registry")


def test_s2_bound_genome_requires_its_protocol_settings():
    g, proto = _registry_h_genome()
    assert proto["params"]["simulation"]["opening"] == "received_raw"
    errs = check_binding(g, SimConfig(opening="published"), T.REPO)
    assert errs and "opening='received_raw'" in errs[0]
    assert check_binding(g, SimConfig(opening="received_raw"), T.REPO) == []
    assert check_binding(T.maker(), SimConfig(opening="published"), T.REPO) == []      # unbound


def test_s2_cli_refuses_without_opening_or_against_the_binding(tmp_path):
    g, _ = _registry_h_genome()
    gp = tmp_path / "g.json"
    gp.write_bytes(orjson.dumps(to_dict(g)))
    base = ["run", "--genome", str(gp), "--data", str(tmp_path), "--journal", str(tmp_path / "j"), "--from-now",
            "--registry", str(T.REPO)]
    with pytest.raises(SystemExit):
        paper_main(base)                                   # --opening is required
    assert paper_main(base + ["--opening", "published"]) == 2
    assert paper_main(base[:-2] + ["--registry", str(tmp_path), "--opening", "received_raw"]) == 2   # no registry


# ---------------------------------------------------------------- S3: bounded memory, rotation
def test_s3_journal_rotates_daily_and_chains_across_files(tmp_path):
    j = Journal(tmp_path)
    j.open()
    day = 86400 * NS
    j.append([{"k": "run", "t": 0}], t_ns=T.T0 * NS - 10)
    j.append([{"k": "fill", "t": 1}, {"k": "cycle", "t": 1}], t_ns=T.T0 * NS - 5)
    j.append([{"k": "fill", "t": 2}, {"k": "cycle", "t": 2}], t_ns=T.T0 * NS + 5)
    j.append([{"k": "cycle", "t": 3}], t_ns=T.T0 * NS + day + 5)
    fs = journal.files(tmp_path)
    assert len(fs) == 3 and len(journal.load(tmp_path)) == 6
    j2 = Journal(tmp_path)
    j2.open()
    assert j2.n == 6 and j2.head == journal.load(tmp_path)[-1]["h"]
    fs[1].unlink()                                         # a missing day file breaks the chain
    with pytest.raises(JournalError):
        journal.load(tmp_path)


def test_s3_memory_stays_flat_over_a_long_run(tmp_path):
    recs = T.market(12)
    rt, tape, clock = fresh(tmp_path, recs)
    t = drive(rt, tape, clock, (S - 700) * NS, (S + 900) * NS, 7 * NS)
    tracemalloc.start()
    t = drive(rt, tape, clock, t, (S + 1500) * NS, 7 * NS)
    a, _ = tracemalloc.get_traced_memory()
    t = drive(rt, tape, clock, t, (S + 3300) * NS, 7 * NS)
    b, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    tape.close()
    n_settled = len(T.kinds(journal.load(rt.journal.dir), "settle"))
    assert n_settled >= 9
    assert b - a < 1_500_000, (a, b)        # 6 more windows settled; nothing accumulates per window
    assert len(rt.st.settled) <= n_settled and len(rt.st.windows) <= 2
    assert not hasattr(rt.journal, "records")
    assert len(rt.st.ref.brti) < 1500


# ---------------------------------------------------------------- S4: bounded polls
def _seg(tmp_path, n_frames, per_frame, t0=S):
    w = SegmentWriter(tmp_path, "pmus_ws_5m")
    k = 0
    for f in range(n_frames):
        for _ in range(per_frame):
            w.append("x" * 100, tw=(t0 + k) * NS)
            k += 1
        w.flush()
    w.close()


def test_s4_poll_stops_after_the_bound_and_the_byte_cap(tmp_path):
    _seg(tmp_path, 10, 10)
    t = StreamTail(tmp_path, "pmus_ws_5m", S * NS)
    recs = t.poll((S + 200) * NS, max_tw=(S + 25) * NS)
    assert len(recs) == 30 and t.capped                     # frames of 10 s each: the third crosses
    recs = t.poll((S + 200) * NS, max_tw=(S + 1000) * NS, max_bytes=1)
    assert len(recs) == 10 and t.capped                     # at least one frame, then the cap
    recs = t.poll((S + 200) * NS, max_tw=(S + 1000) * NS)
    assert len(recs) == 60 and not t.capped


# ---------------------------------------------------------------- S5: kill commits once; clean exit
def test_s5_kill_commits_once_and_finished_after_settling(tmp_path):
    rt, tape, clock = fresh(tmp_path, T.market(1))
    t = drive(rt, tape, clock, (S - 700) * NS, (S + 60) * NS, 7 * NS)
    (tmp_path / "j" / "KILL").touch()
    drive(rt, tape, clock, t, (S + 400) * NS, 1 * NS)
    tape.close()
    recs = journal.load(rt.journal.dir)
    kills = [c for c in T.kinds(recs, "cycle") if c["kill"]]
    assert len(kills) == 1
    quiet = [c for c in T.kinds(recs, "cycle") if c["t"] > (S + 320) * NS]
    assert len(quiet) <= 3                                  # 30 s heartbeats only, not one per cycle
    assert rt.finished


# ---------------------------------------------------------------- S6: refuse what cannot be trusted
def test_s6_restart_refuses_other_code_or_checkpoint_format(tmp_path, monkeypatch):
    _, j = T.run_paper(tmp_path, T.maker(), T.market(1), end=S + 100)
    monkeypatch.setattr(runtime, "paper_code_sha256", lambda: "0" * 64)
    with pytest.raises(RecoveryError, match="code differs"):
        PaperRuntime(T.maker(), tmp_path / "data", j, SimConfig(), SET).start(None)
    monkeypatch.undo()
    monkeypatch.setattr(runtime, "CKPT_VERSION", 99)
    with pytest.raises(RecoveryError, match="format"):
        PaperRuntime(T.maker(), tmp_path / "data", j, SimConfig(), SET).start(None)


def _bad_frame(path, tw, crc_ok=True, json_ok=True):
    raw = "{}"
    rec = b"{not json" if not json_ok else orjson.dumps({"seq": 10**6, "stream": "pmus_ws_5m", "tw": tw, "tm": 0,
                                                         "crc": zlib.crc32(raw.encode()) + (0 if crc_ok else 1),
                                                         "raw": raw})
    with open(path, "ab") as fh:
        fh.write(zstandard.ZstdCompressor().compress(rec + b"\n"))


@pytest.mark.parametrize("kind", ["crc", "json", "stall"])
def test_s6_tape_corruption_or_stall_is_journaled_and_stops(tmp_path, kind):
    rt, tape, clock = fresh(tmp_path, T.market(1))
    t = drive(rt, tape, clock, (S - 700) * NS, (S + 30) * NS, 7 * NS)
    path = sorted((tmp_path / "data" / "pmus_ws_5m").glob("*/*.dtape.zst"))[-1]
    if kind == "stall":
        with open(path, "ab") as fh:
            fh.write(b"\x28\xb5\x2f\xfd partial frame")          # bytes that never become a frame
    else:
        _bad_frame(path, (S + 31) * NS, crc_ok=kind != "crc", json_ok=kind != "json")
    with pytest.raises(TapeError):
        for _ in range(60):
            t += NS
            clock.t = t
            rt.cycle()
    tape.close()
    risk = [r for r in journal.load(rt.journal.dir) if r["k"] == "risk"]
    assert risk[-1]["event"] == "tape_error"


def test_s6_stale_ws_book_stops_quoting(tmp_path):
    recs = [r for r in T.market(1) if not (r[0] == "pmus_ws_5m" and r[1] > (S + 100) * NS and "marketData" in r[2])]
    _, j = T.run_paper(tmp_path, T.maker(), recs, end=S + 330)
    recs_j = journal.load(j)
    st = T.kinds(recs_j, "settle")[0]
    assert st["stale_book_s"] > 100
    stale = (S + 131) * NS
    slug = pmus_slug("btc", "5m", S)
    assert not [r for r in T.kinds(recs_j, "intent", slug) if r["kind"] in ("post", "amend") and r["t"] > stale]
    assert [r for r in T.kinds(recs_j, "decide", slug) if r.get("skips") == ["stale_book"]]
    assert open_orders(recs_j, slug) == {}
    assert not [r for r in T.kinds(recs_j, "fill") if r["t"] > stale + NS]


# ---------------------------------------------------------------- S7: agreement report
def test_s7_agree_state_key_modes_and_empty_is_failure(tmp_path):
    _, j = T.run_paper(tmp_path, T.maker(), T.market(2))
    doc = agree(j, tmp_path / "data")
    assert doc["by_mode"]["live"]["windows_compared"] == 2 and doc["by_mode"]["catchup"]["windows_compared"] == 0
    assert all("sh" in r for r in T.kinds(journal.load(j), "decide"))
    assert doc["paper_only_net_usd"] == 0.0
    # catch-up: the clock is a day ahead of the tape
    data = tmp_path / "data"
    clock = T.Clock((S + 86400) * NS)
    rt = PaperRuntime(T.maker(), data, tmp_path / "cu", SimConfig(), SET, clock=clock)
    rt.start(S)
    while rt.cycle() < (S + 640) * NS:
        pass
    doc = agree(tmp_path / "cu", data)
    assert doc["by_mode"]["catchup"]["windows_compared"] == 2 and doc["by_mode"]["catchup"]["intents_identical_share"] == 1.0
    # zero windows compared is a failure
    empty, _ = T.run_paper(tmp_path / "e", T.maker(), T.market(1), end=S - 100)
    assert paper_main(["agree", "--journal", str(empty.journal.dir), "--data", str(tmp_path / "e" / "data")]) == 1


# ---------------------------------------------------------------- unit hardening, entry point
def test_unit_is_sealed_and_entry_point_exists():
    unit = (T.REPO / "ops" / "systemd" / "doppel-paper@.service").read_text()
    for line in ("PrivateNetwork=true", "RestrictAddressFamilies=none", "CapabilityBoundingSet=",
                 "PrivateDevices=yes", "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectKernelLogs=yes",
                 "SystemCallFilter=@system-service", "--opening", "--perelman"):
        assert line in unit
    assert "LoadCredential" not in unit and "SupplementaryGroups" not in unit
    for p in ("/etc/edge-engine", "/etc/atlas", "/etc/doppel", "/etc/credstore.encrypted"):
        assert p in unit
    assert 'doppel-paper = "doppel.paper.cli:main"' in (T.REPO / "pyproject.toml").read_text()


def test_read_new_frames_reports_unreadable_records(tmp_path):
    p = tmp_path / "x.dtape.zst"
    _bad_frame(p, S * NS, json_ok=False)
    with pytest.raises(TapeError):
        read_new_frames(Path(p), 0)
