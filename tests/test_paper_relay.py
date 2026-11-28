"""doppel-brti-relay: exactly-once copy of PERELMAN's raw BRTI lines, and paper reading the copy."""

from __future__ import annotations

import orjson
import pytest

import test_paper as T
import test_paper_review as R
from doppel.paper import journal
from doppel.paper.relay import Relay, RelayError
from doppel.paper.runtime import Settings

S, NS, MS = T.S, T.NS, T.MS


def row(i):
    return orjson.dumps({"kind": "brti", "new": [[i * 1000, f"{90000 + i}.5"]], "received": i + 0.4}) + b"\n"


def body(path):
    out = []
    for line in path.read_bytes().splitlines():
        r = orjson.loads(line)
        out.append((r.pop("relay_tw"), r))
    return out


class Clock:
    def __init__(self, t=1_000):
        self.t = t

    def __call__(self):
        return self.t


def test_restart_never_duplicates_or_skips(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    f = src / "brti-2026112701.jsonl"
    f.write_bytes(row(1) + row(2))
    r = Relay(src, out, clock=Clock())
    assert r.step() == 2 and r.step() == 0
    with open(f, "ab") as fh:
        fh.write(row(3))
    r2 = Relay(src, out, clock=Clock())                    # restart: offsets from the output itself
    assert r2.offsets[f.name] == len(row(1) + row(2))
    assert r2.step() == 1
    got = [x for _, x in body(out / f.name)]
    assert got == [orjson.loads(row(i)) for i in (1, 2, 3)]
    # a crash that tore the last output line: cut and copied again, once
    with open(out / f.name, "ab") as fh:
        fh.write(b'{"kind":"br')
    with open(f, "ab") as fh:
        fh.write(row(4))
    r3 = Relay(src, out, clock=Clock())
    assert r3.step() == 1
    assert [x for _, x in body(out / f.name)] == [orjson.loads(row(i)) for i in (1, 2, 3, 4)]
    assert orjson.loads((out / "relay-state.json").read_bytes())["offsets"][f.name] == f.stat().st_size


def test_partial_last_line_waits_for_its_newline(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    f = src / "h.jsonl"
    full = row(1)
    f.write_bytes(full[:10])
    r = Relay(src, out)
    assert r.step() == 0 and not (out / "h.jsonl").exists()
    with open(f, "ab") as fh:
        fh.write(full[10:])
    assert r.step() == 1
    assert [x for _, x in body(out / "h.jsonl")] == [orjson.loads(full)]


def test_hour_rollover_and_rotated_source(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    (src / "brti-2026112701.jsonl").write_bytes(row(1))
    r = Relay(src, out)
    r.step()
    (src / "brti-2026112702.jsonl").write_bytes(row(2) + row(3))   # the next hour appears
    assert r.step() == 2
    (src / "brti-2026112701.jsonl").unlink()                       # PERELMAN rotates the old hour away
    (src / "brti-2026112701.jsonl.gz").write_bytes(b"\x1f\x8b")    # the .gz is never read
    r2 = Relay(src, out)
    assert r2.offsets["brti-2026112701.jsonl"] == -1
    with open(src / "brti-2026112702.jsonl", "ab") as fh:
        fh.write(row(4))
    assert r2.step() == 1
    assert len(body(out / "brti-2026112701.jsonl")) == 1 and len(body(out / "brti-2026112702.jsonl")) == 3
    assert sorted(p.name for p in out.iterdir()) == ["brti-2026112701.jsonl", "brti-2026112702.jsonl",
                                                     "relay-state.json"]


def test_relay_tw_is_strictly_increasing_even_if_the_clock_steps_back(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    c = Clock(5_000)
    r = Relay(src, out, clock=c)
    (src / "h.jsonl").write_bytes(row(1) + row(2))
    r.step()
    c.t = 100                                                       # wall clock stepped backwards
    with open(src / "h.jsonl", "ab") as fh:
        fh.write(row(3))
    r.step()
    r2 = Relay(src, out, clock=c)                                   # and across a restart
    with open(src / "h.jsonl", "ab") as fh:
        fh.write(row(4))
    r2.step()
    tws = [t for t, _ in body(out / "h.jsonl")]
    assert tws == sorted(set(tws)) and tws[0] == 5_000


def test_malformed_upstream_line_is_an_error(tmp_path):
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    (src / "h.jsonl").write_bytes(b"not json\n")
    with pytest.raises(RelayError):
        Relay(src, out).step()


class RelayTape(R.PerelmanTape):
    """PERELMAN writes its source file; the relay copies it at the tape clock (relay_tw <= read time)."""

    def __init__(self, root, recs):
        super().__init__(root, recs)
        self.out = root.parent / "relay"
        self.clock = Clock(0)
        self.relay = Relay(self.src, self.out, clock=self.clock)

    def until(self, t_ns):
        super().until(t_ns)
        self.clock.t = t_ns
        self.relay.step()


def _run(tmp_path, tape_cls, src_name):
    st = Settings(grace_s=0.0, checkpoint_s=60, heartbeat_s=30, perelman_src=str(tmp_path / src_name))
    rt, tape, clock = R.fresh(tmp_path, R.perelman_market(), settings=st, tape_cls=tape_cls)
    t = R.drive(rt, tape, clock, (S - 700) * NS, (S - 30) * NS, 7 * NS)
    R.drive(rt, tape, clock, t, (S + 330) * NS, 250 * MS)
    tape.close()
    return rt.journal.dir


def test_paper_on_the_relay_reproduces_perelman_direct(tmp_path):
    direct = _run(tmp_path / "a", R.PerelmanTape, "perelman")
    relayed = _run(tmp_path / "b", RelayTape, "relay")
    assert T.kinds(journal.load(relayed), "settle")
    assert any(c.get("offsets", {}).get("perelman") for c in T.kinds(journal.load(relayed), "cycle"))
    assert T.by_window(direct) == T.by_window(relayed)


def test_relay_unit_is_sandboxed():
    unit = (T.REPO / "ops" / "systemd" / "doppel-brti-relay.service").read_text()
    for line in ("User=doppel", "AmbientCapabilities=CAP_DAC_READ_SEARCH", "CapabilityBoundingSet=CAP_DAC_READ_SEARCH",
                 "NoNewPrivileges=yes", "TemporaryFileSystem=/var/lib/private:ro",
                 "BindReadOnlyPaths=/var/lib/private/perelman/stream", "ProtectSystem=strict",
                 "ReadWritePaths=/mnt/volume_nyc1_1793485122853/doppel/perelman-relay", "PrivateNetwork=yes",
                 "RestrictAddressFamilies=none", "SystemCallFilter=@system-service", "MemoryMax=128M",
                 "Restart=always"):
        assert line in unit, line
    for p in ("/root", "/etc/doppel", "/etc/edge-engine", "/etc/credstore.encrypted", "/etc/atlas"):
        assert p in unit.split("InaccessiblePaths=")[1].splitlines()[0]
    paper = (T.REPO / "ops" / "systemd" / "doppel-paper@.service").read_text()
    assert "--perelman /mnt/volume_nyc1_1793485122853/doppel/perelman-relay" in paper
    assert "ReadOnlyPaths=/mnt/volume_nyc1_1793485122853/doppel/perelman-relay" in paper
    assert "/var/lib/private" not in paper
    assert 'doppel-brti-relay = "doppel.paper.relay:main"' in (T.REPO / "pyproject.toml").read_text()
