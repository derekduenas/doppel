"""doppel.brti: the absorbed PERELMAN recorder writes what the frozen importer reads, the cutover keeps
the tape gap-free, the parity tool passes and fails where it should, and the sink survives rotation
and restarts. The frozen importer (doppel.lab.brti_import) is imported, never edited."""

import gzip
import importlib.util
import json
import time
from pathlib import Path

import orjson
import pytest

from doppel.brti import fastbrti
from doppel.brti.parity import main as parity_main
from doppel.brti.parity import parity
from doppel.brti.recorder import GROUPS, Recorder, Sink, parse_without
from doppel.lab.brti_import import _iter_rows, load_raw, load_raw_rcv
from doppel.lab.brti_import import run as import_run

H0 = 1_764_590_400  # 2025-12-01T12:00:00Z, an hour boundary


def hour_name(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H", time.gmtime(t))


class Clock:
    def __init__(self, t: float):
        self.t = t

    def __call__(self) -> float:
        return self.t


def body(times_ms, value=lambda ms: f"{60000 + ms // 1000 % 1000}.25"):
    return {"data": {"serverTime": "2025-12-01T12:00:00Z",
                     "payload": [{"time": ms, "value": value(ms)} for ms in times_ms]}}


def record(rec: Recorder, clock: Clock, secs, *, rcv_lag=0.4, value=None):
    """One poll at clock.t that returns seconds ``secs`` (unix seconds)."""
    kw = {} if value is None else {"value": value}
    rec.on_brti(clock.t - 0.05, clock.t + rcv_lag, body([s * 1000 for s in secs], **kw))


def stream(tmp: Path, name: str, start: float, seconds: int, *, lag=0.4, skip=(), value=None):
    """A recorder stream that sees each second once, about ``lag`` s after publication."""
    clock = Clock(start)
    sink = Sink(tmp / name, clock=clock)
    rec = Recorder(tmp / name, sink=sink)
    for i in range(seconds):
        clock.t = start + i + 1
        sec = int(start) + i
        if sec not in skip:
            record(rec, clock, [sec], rcv_lag=lag, value=value)
    sink.close()
    return tmp / name


# --- format compatibility -------------------------------------------------------------------------

def test_brti_record_bytes_match_perelman(tmp_path):
    """Same inputs -> the exact line PERELMAN's recorder.py writes (json.dumps, its key order)."""
    clock = Clock(H0 + 10.0)
    sink = Sink(tmp_path, clock=clock)
    rec = Recorder(tmp_path, sink=sink)
    rec.on_brti(H0 + 9.95, H0 + 10.4, body([(H0 + 8) * 1000, (H0 + 9) * 1000]))
    rec.on_brti(H0 + 10.45, H0 + 10.9, body([(H0 + 9) * 1000]))  # nothing new: no record
    sink.close()
    lines = (tmp_path / f"{hour_name(H0)}.jsonl").read_bytes().splitlines()
    want = json.dumps({"kind": "brti", "sent": H0 + 9.95, "received": H0 + 10.4,
                       "server_time": "2025-12-01T12:00:00Z",
                       "new": [[(H0 + 8) * 1000, "60408.25"], [(H0 + 9) * 1000, "60409.25"]]},
                      separators=(",", ":")).encode()
    assert lines == [want]
    assert b'"kind":"brti"' in lines[0]  # ops/recover_brti_receipts.py matches these bytes
    assert rec.stats["brti_polls"] == 2


def test_frozen_reader_and_importer_read_the_new_stream(tmp_path):
    src = stream(tmp_path, "b", H0 - 30, 90)  # spans an hour roll: one .gz and one open .jsonl
    files = sorted(p.name for p in src.iterdir() if p.name.endswith((".jsonl", ".jsonl.gz")))
    assert files == [f"{hour_name(H0 - 1)}.jsonl.gz", f"{hour_name(H0)}.jsonl"]
    rows = [r for f in sorted(src.glob("*.jsonl*")) for r in _iter_rows(f)]
    assert [t for t, _, _ in rows] == [(H0 - 30 + i) * 1000 for i in range(90)]
    assert all(isinstance(v, str) for _, v, _ in rows)
    res = import_run(src, tmp_path / "tape", tmp_path / "state.json")
    assert res["added"] == 90 and res["amendments"] == 0
    raw = load_raw_rcv(tmp_path / "tape", include_live=False)
    assert sorted(raw) == list(range(H0 - 30, H0 + 60))
    assert raw[H0][1] == pytest.approx(H0 + 1.4)


# --- cutover: same importer state, src switched from PERELMAN's stream to DOPPEL's ------------------

def test_cutover_switch_is_gap_free(tmp_path):
    start = H0 + 600
    # A (PERELMAN) ran all along but missed one second; B (DOPPEL) shadows it with its own stamps.
    missed = start + 500
    a = stream(tmp_path, "a", start - 3600, 3600 + 900, lag=0.40, skip={missed})
    b = stream(tmp_path, "b", start - 3600, 3600 + 1500, lag=0.55)
    # Import from A as the scored pipeline does today, but only as far as A had written by then.
    a_cut = tmp_path / "a_cut"
    a_cut.mkdir()
    for f in a.glob("*.jsonl*"):
        (a_cut / f.name).write_bytes(f.read_bytes())
    tape, state = tmp_path / "tape", tmp_path / "state.json"
    first = import_run(a_cut, tape, state)
    w0 = orjson.loads((tape / "ref_brti_raw" / "import-watermark.json").read_bytes())["max_t_ms"]
    assert w0 == (start + 899) * 1000
    # The unit argument changes; nothing else.
    second = import_run(b, tape, state)
    w1 = orjson.loads((tape / "ref_brti_raw" / "import-watermark.json").read_bytes())["max_t_ms"]
    assert w1 == (start + 1499) * 1000 and second["added"] == 600
    secs = load_raw(tape, include_live=False)
    expected = set(range(start - 3600, start + 1500)) - {missed}
    assert set(secs) == expected  # no gap after the switch, no duplicates (keys are unique seconds)
    # The second A missed is at or below the watermark: it goes to the amendments file, not the tape.
    amend = [orjson.loads(x) for x in (tape / "ref_brti_raw" / "import-amendments.jsonl").read_bytes().splitlines()]
    assert [r["t_ms"] for r in amend] == [missed * 1000] and second["amendments"] == 1
    assert first["amendments"] == 0
    # Stamps before the switch are A's, after it B's.
    rcv = load_raw_rcv(tape, include_live=False)
    assert rcv[start + 899][1] == pytest.approx(start + 900.40)
    assert rcv[start + 900][1] == pytest.approx(start + 901.55)
    # A third run with nothing new is a no-op.
    assert import_run(b, tape, state)["added"] == 0


# --- parity tool ------------------------------------------------------------------------------------

def test_parity_pass(tmp_path):
    a = stream(tmp_path, "a", H0, 1200, lag=0.40)
    b = stream(tmp_path, "b", H0, 1200, lag=0.55)
    res = parity(a, b, H0, H0 + 1200, frozenset({"brti"}))
    assert res["pass"] and res["brti"]["coverage"] == 1.0 and res["brti"]["value_mismatch"] == 0
    assert res["brti"]["received_b_minus_a_ms"]["p50"] == pytest.approx(150, abs=1)
    assert parity_main(["--a", str(a), "--b", str(b), "--from", str(H0), "--to", "2025-12-01T12:20:00Z",
                        "--gate", "brti"]) == 0


def test_parity_fails_on_one_value_mismatch(tmp_path):
    a = stream(tmp_path, "a", H0, 600)
    b = stream(tmp_path, "b", H0, 600, value=lambda ms: "1.0" if ms == (H0 + 77) * 1000 else
               f"{60000 + ms // 1000 % 1000}.25")
    res = parity(a, b, H0, H0 + 600)
    assert not res["pass"] and res["brti"]["value_mismatch"] == 1
    assert res["brti"]["mismatch_examples"][0]["t"] == H0 + 77
    assert parity_main(["--a", str(a), "--b", str(b), "--from", str(H0), "--to", str(H0 + 600)]) == 1


def test_parity_coverage_threshold(tmp_path):
    a = stream(tmp_path, "a", H0, 2000)
    one = stream(tmp_path, "b1", H0, 2000, skip={H0 + 5})  # 99.95% coverage: pass
    two = stream(tmp_path, "b2", H0, 2000, skip={H0 + 5, H0 + 6, H0 + 7})  # 99.85%: fail
    extra = stream(tmp_path, "b3", H0 - 10, 2010)
    assert parity(a, one, H0, H0 + 2000)["pass"]
    res = parity(a, two, H0, H0 + 2000)
    assert not res["pass"] and res["brti"]["missing_in_b"] == 3 and res["brti"]["missing_examples"][0] == H0 + 5
    # Seconds outside the window are ignored; extra seconds inside it are reported, not failed.
    assert parity(a, extra, H0, H0 + 2000)["brti"]["extra_in_b"] == 0
    a_short = stream(tmp_path, "a2", H0, 1000)
    res = parity(a_short, a, H0, H0 + 2000)
    assert res["pass"] and res["brti"]["extra_in_b"] == 1000


def test_parity_rti_and_replica(tmp_path):
    def write(name, value, replica):
        d = tmp_path / name
        d.mkdir()
        lines = [{"kind": "rti", "id": "ETHUSD_RTI", "sent": H0 + i, "received": H0 + i + 0.3,
                  "new": [[(H0 + i) * 1000, value(i)]]} for i in range(100)]
        lines += [{"kind": "replica", "t": H0 + i, "value": replica(i)} for i in range(100)]
        (d / f"{hour_name(H0)}.jsonl").write_text("".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines))
        return d
    a = write("a", lambda i: "3000.5", lambda i: 60000.0)
    b = write("b", lambda i: "3000.5", lambda i: 60000.0 + (0.004 if i % 2 else 0.0))
    res = parity(a, b, H0, H0 + 100, frozenset({"rti"}))
    assert res["pass"] and res["rti"]["ETHUSD_RTI"]["coverage"] == 1.0
    assert res["replica"]["gating"] is False and res["replica"]["abs_diff_usd"]["max"] == pytest.approx(0.004)
    c = write("c", lambda i: "3000.6" if i == 3 else "3000.5", lambda i: 1.0)
    assert not parity(a, c, H0, H0 + 100, frozenset({"rti"}))["pass"]
    # No published BRTI in the window: a brti gate cannot pass on nothing.
    assert not parity(a, b, H0, H0 + 100, frozenset({"brti"}))["pass"]


# --- rotation and restart ---------------------------------------------------------------------------

def test_rotation_gzips_whole_hours(tmp_path):
    clock = Clock(H0 - 2)
    sink = Sink(tmp_path, clock=clock)
    for i in range(5):
        clock.t = H0 - 2 + i
        sink.write({"kind": "x", "i": i})
    sink.join()
    prev = tmp_path / f"{hour_name(H0 - 1)}.jsonl.gz"
    assert prev.exists() and not (tmp_path / f"{hour_name(H0 - 1)}.jsonl").exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert [json.loads(x)["i"] for x in gzip.decompress(prev.read_bytes()).splitlines()] == [0, 1]
    sink.close()
    cur = (tmp_path / f"{hour_name(H0)}.jsonl").read_bytes().splitlines()
    assert [json.loads(x)["i"] for x in cur] == [2, 3, 4]


def test_restart_repairs_a_torn_tail_and_leftovers(tmp_path):
    good = json.dumps({"kind": "brti", "sent": 1.0, "received": 2.0, "server_time": None,
                       "new": [[(H0 - 7200) * 1000, "1.5"]]}, separators=(",", ":"))
    old = tmp_path / f"{hour_name(H0 - 7200)}.jsonl"  # an hour a crash never gzipped, torn mid-line
    old.write_text(good + "\n" + '{"kind":"brti","sent":1.0,"rec')
    done = tmp_path / f"{hour_name(H0 - 3600)}.jsonl"  # crash after the rename, before the unlink
    done.write_text(good + "\n")
    (tmp_path / f"{hour_name(H0 - 3600)}.jsonl.gz").write_bytes(gzip.compress((good + "\n").encode()))
    (tmp_path / f"{hour_name(H0 - 10800)}.jsonl.gz.tmp").write_bytes(b"half")
    cur = tmp_path / f"{hour_name(H0)}.jsonl"  # the current hour, torn: appending must not glue lines
    cur.write_text(good + "\n" + '{"kind":"brti","se')
    clock = Clock(H0 + 5)
    sink = Sink(tmp_path, clock=clock)
    rec = Recorder(tmp_path, sink=sink)
    record(rec, clock, [H0 + 4])
    sink.close()
    assert not old.exists() and not done.exists() and not list(tmp_path.glob("*.tmp"))
    assert gzip.decompress((tmp_path / f"{old.name}.gz").read_bytes()) == (good + "\n").encode()
    assert cur.read_bytes().count(b"\n") == 2 and cur.read_bytes().endswith(b"\n")
    for f in tmp_path.glob("*.jsonl*"):
        list(_iter_rows(f))  # every file parses with the frozen reader


def test_restart_resends_recent_values_without_duplicating_the_tape(tmp_path):
    """After a restart the first poll returns ~120 s of values already recorded; the importer skips them."""
    clock = Clock(H0 + 100)
    sink = Sink(tmp_path / "s", clock=clock)
    rec = Recorder(tmp_path / "s", sink=sink)
    record(rec, clock, list(range(H0 + 50, H0 + 100)))
    sink.close()
    import_run(tmp_path / "s", tmp_path / "tape", tmp_path / "st.json")
    clock.t = H0 + 130
    sink = Sink(tmp_path / "s", clock=clock)
    rec = Recorder(tmp_path / "s", sink=sink)  # fresh process: empty first-seen memory
    record(rec, clock, list(range(H0 + 50, H0 + 130)))
    sink.close()
    res = import_run(tmp_path / "s", tmp_path / "tape", tmp_path / "st.json")
    assert res["added"] == 30 and res["amendments"] == 0
    assert sorted(load_raw(tmp_path / "tape", include_live=False)) == list(range(H0 + 50, H0 + 130))


# --- CLI, groups, numerics ---------------------------------------------------------------------------

def test_without_groups(tmp_path):
    assert parse_without("hl, bn") == {"hl", "bn"}
    with pytest.raises(Exception):
        parse_without("brti")
    full = Recorder(tmp_path, sink=Sink(tmp_path))
    jobs = full.tasks()
    n_full = len(jobs)
    for j in jobs:
        j.close()
    lean = Recorder(tmp_path, frozenset(GROUPS), sink=Sink(tmp_path))
    jobs = lean.tasks()
    assert [j.__name__ for j in jobs] == ["brti_loop", "status_loop"]
    for j in jobs:
        j.close()
    # 7 venue feeds + replica + audit + rti + 5 fifteen-minute + 4 hourly + hl + bn + brti + status
    assert n_full == 23


def test_status_file(tmp_path):
    rec = Recorder(tmp_path, frozenset({"hl"}), sink=Sink(tmp_path))
    rec.write_status()
    st = json.loads((tmp_path / "status.json").read_text())
    assert set(st["stats"]) >= {"replica", "brti_polls"} and st["without"] == ["hl"]
    assert st["source_commit"].startswith("9b55eae")


def test_cli_refuses_without_credential(monkeypatch, tmp_path, capsys):
    from doppel.brti.recorder import main
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    assert main(["--out", str(tmp_path)]) == 2
    assert "FileNotFoundError" in capsys.readouterr().err


def test_vendored_fastbrti_matches_reference_replica():
    spec = importlib.util.spec_from_file_location("perelman_brti", Path(__file__).parent / "fixtures_fair" / "perelman_brti.py")
    assert spec and spec.loader
    ref = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ref)

    def ladder(mid, n=200, size=0.4):
        return ([(mid - 1 - i * .5, size) for i in range(n)], [(mid + 1 + i * .5, size) for i in range(n)])
    for books in ([ladder(90000.0)], [ladder(90000.0, 100, .2), ladder(90003.0, 150, .7)]):
        want, _ = ref.brti_from_books([ref.Book(b, a, 0) for b, a in books])
        got, _ = fastbrti.brti(books)
        assert got == pytest.approx(want, abs=1e-4)
    snap = json.loads((Path(__file__).parent / "fixtures_fair" / "snapshot_books.json").read_text())
    books = [([(float(p), float(s)) for p, s in v["bids"]], [(float(p), float(s)) for p, s in v["asks"]])
             for v in snap["venues"]]
    want, _ = ref.brti_from_books([ref.Book(b, a, 0) for b, a in books])
    got, _ = fastbrti.brti(books)
    assert abs(got - want) < 0.01
