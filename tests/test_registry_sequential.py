import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import orjson
import pytest

from doppel.strategy.cli import main as cli
from doppel.strategy.genome import Genome, KillRule, MakerRule, Risk, Source, Universe, to_dict
from doppel.strategy.registry import Registry, RegistryError

REAL = Path(__file__).resolve().parents[1] / "registry" / "genomes.jsonl"
T0 = datetime(2026, 1, 1, tzinfo=UTC).timestamp()       # registration instant (past for the CLI's real clock)
START = T0 + 3600
END = START + 3 * 86400
WIN = (START, END)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def _g(h):
    # $1 notional per window -> loss bound 100c; min_windows 30 so a short synthetic run can decide
    return Genome(name=f"seq-maker-h{h:g}", mode="maker", universe=Universe(venue="pmus"),
                  source=Source(kind="manual"), maker=MakerRule(half_spread_c=h, size=1),
                  risk=Risk(max_inventory=2, max_notional_per_window=1.0),
                  kill=KillRule(min_windows=30), created_tw=0)


def _results(gid, mu, n=600, sd=20.0, seed=0, t0=START, diagnostic=False):
    rng = np.random.default_rng(seed)
    x = np.clip(rng.normal(mu, sd, n), -100.0, 100.0)
    w = [{"start": int(t0 + 300 * i), "end": int(t0 + 300 * (i + 1)), "eligible": True,
          "net_c": round(float(v), 4), "contracts": 1.0} for i, v in enumerate(x)]
    w.insert(0, {"start": int(t0 - 300), "end": int(t0), "eligible": True, "net_c": -99.0, "contracts": 1.0})
    return {"twin": {"code_sha256": "ab" * 32, "diagnostic": diagnostic}, "genome": {"id": gid},
            "range": [int(t0 - 300), int(t0 + 300 * n)], "windows": w}


def _setup(tmp_path, k=3):
    clock = Clock()
    reg = Registry(tmp_path, now=clock, code={"sha": "c0ffee", "dirty": False})
    ids = [reg.register(_g(h), "seqfam", "net > 0", WIN)["genome_id"] for h in range(1, k + 1)]
    return reg, clock, ids


def test_monitor_refused_before_window_and_writes_nothing(tmp_path):
    reg, clock, ids = _setup(tmp_path)
    before = reg.path.read_bytes()
    with pytest.raises(RegistryError, match="opens"):
        reg.monitor(ids[0], _results(ids[0], 10.0))
    with pytest.raises(RegistryError, match="opens"):
        reg.record_monitor(ids[0], _results(ids[0], 10.0))
    clock.t = START + 86400
    m = reg.monitor(ids[0], _results(ids[0], 10.0))
    assert reg.path.read_bytes() == before                      # monitor never writes
    # only windows inside the eval window and ending by now count (the pre-window -99c row is out)
    assert m["stats"]["windows_seen"] == 288 and m["stats"]["n_blocks"] == 24
    assert m["as_of_s"] == START + 86400 and m["params"]["bound_c"] == 100.0
    with pytest.raises(RegistryError, match="results are for"):
        reg.monitor(ids[0], _results(ids[1], 10.0))
    with pytest.raises(RegistryError, match="not registered"):
        reg.monitor("cd" * 32, _results("cd" * 32, 10.0))


def test_early_pass_needs_family_holm_and_is_binding(tmp_path):
    reg, clock, ids = _setup(tmp_path)
    clock.t = START + 2 * 86400
    m = reg.monitor(ids[0], _results(ids[0], 25.0))
    assert m["decision"] == "EARLY_PASS", m["reason"]
    assert m["family_holm"]["m"] == 3 and m["family_holm"]["p_holm"] <= 0.05
    assert m["stats"]["pass"]["e_max"] >= 3 / 0.05                # Holm step 1 needs m / alpha
    before = reg.path.read_bytes()
    rec = reg.record_monitor(ids[0], _results(ids[0], 25.0), source={"results_sha256": "x"})
    assert reg.path.read_bytes().startswith(before)              # append only
    assert rec["kind"] == "early_pass" and rec["binding"] is True and "owner authorization" in rec["note"]
    again = reg.record_monitor(ids[0], _results(ids[0], 25.0))
    assert again["kind"] == "early_pass" and again["binding"] is False
    led = reg.sequential_ledger("seqfam")
    assert [r["status"] for r in led["rows"]] == ["early_pass", "pending", "pending"]
    assert led["rows"][0]["survives"] and reg.verify() == []
    # the end-of-window verdict path is unchanged and still refused before the window closes
    with pytest.raises(RegistryError, match="closes"):
        reg.record_result(ids[0], {"p_value": 0.01})
    clock.t = END + 1
    reg.record_result(ids[0], {"p_value": 0.01})
    assert reg.ledger("seqfam")["rows"][0]["p_value"] == 0.01 and reg.verify() == []


def test_early_kill_and_interim(tmp_path):
    reg, clock, ids = _setup(tmp_path)
    clock.t = START + 2 * 86400
    k = reg.record_monitor(ids[1], _results(ids[1], -25.0, seed=1))
    assert k["kind"] == "early_kill" and k["binding"] and k["stats"]["kill"]["e_max"] >= 20
    i = reg.record_monitor(ids[2], _results(ids[2], 0.0, seed=2))
    assert i["kind"] == "interim" and i["decision"] == "CONTINUE" and i["binding"] is False
    led = reg.sequential_ledger("seqfam")
    assert [r["status"] for r in led["rows"]] == ["pending", "early_kill", "monitoring"]
    assert reg.verify() == []


def test_record_guards(tmp_path):
    reg, clock, ids = _setup(tmp_path)
    clock.t = START + 600                                        # 2 windows: no complete block
    with pytest.raises(RegistryError, match="no complete block"):
        reg.record_monitor(ids[2], _results(ids[2], 0.0))
    clock.t = START + 86400
    with pytest.raises(RegistryError, match="DIAGNOSTIC"):
        reg.record_monitor(ids[2], _results(ids[2], 0.0, diagnostic=True))
    reg.record_monitor(ids[2], _results(ids[2], 0.0))
    with pytest.raises(RegistryError, match="frozen"):
        reg.record_monitor(ids[2], _results(ids[2], 0.0), alpha=0.1)
    with pytest.raises(RegistryError, match="frozen"):        # the family's alpha is frozen too
        reg.record_monitor(ids[1], _results(ids[1], 0.0), alpha=0.1)
    short = _results(ids[2], 0.0, n=100)                         # a results file that ends earlier
    with pytest.raises(RegistryError, match="before the last recorded"):
        reg.record_monitor(ids[2], short)
    assert reg.verify() == []


def _rewrite(reg, i, fn, rehash=True):
    lines = reg.lines()
    rec = orjson.loads(lines[i])
    fn(rec)
    if rehash:
        body = {k: v for k, v in rec.items() if k != "hash"}
        rec["hash"] = hashlib.sha256(orjson.dumps(body, option=orjson.OPT_SORT_KEYS)).hexdigest()
    lines[i] = orjson.dumps(rec, option=orjson.OPT_SORT_KEYS)
    reg.path.write_bytes(b"".join(ln + b"\n" for ln in lines))


def test_verify_checks_sequential_lines(tmp_path):
    reg, clock, ids = _setup(tmp_path)
    clock.t = START + 2 * 86400
    reg.record_monitor(ids[0], _results(ids[0], 25.0))           # line 3: early_pass
    pristine = reg.path.read_bytes()
    assert reg.verify() == []

    def weak(r):
        r["stats"]["pass"]["e_max"] = 5.0

    _rewrite(reg, 3, weak)
    assert any("early_pass without" in e for e in reg.verify())
    reg.path.write_bytes(pristine)

    def early(r):
        r["tw"] = int((START - 60) * 1e9)

    _rewrite(reg, 3, early)
    assert any("before the evaluation window opened" in e for e in reg.verify())
    reg.path.write_bytes(pristine)

    def unbind(r):
        r["binding"] = False

    _rewrite(reg, 3, unbind)
    assert any("binding flag" in e for e in reg.verify())
    reg.path.write_bytes(pristine)
    _rewrite(reg, 3, weak, rehash=False)
    assert any("self-hash mismatch" in e for e in reg.verify())


def test_real_registry_copy_still_verifies(tmp_path):
    root = tmp_path / "repo"
    (root / "registry").mkdir(parents=True)
    shutil.copyfile(REAL, root / "registry" / "genomes.jsonl")
    original = REAL.read_bytes()
    reg = Registry(root)
    assert reg.verify() == [] and len(reg.lines()) == len(original.splitlines())
    fams = sorted({r["family"] for r in reg.records() if r["kind"] == "register"})
    for f in fams:
        led = reg.sequential_ledger(f)
        assert led["m"] >= 1 and all(r["status"] == "pending" and r["p_holm"] == 1.0 for r in led["rows"])
    assert cli(["verify", "--root", str(root)]) == 0
    assert (root / "registry" / "genomes.jsonl").read_bytes() == original == REAL.read_bytes()


def test_cli_monitor(tmp_path, capsys):
    root = tmp_path / "repo"
    reg, clock, ids = _setup(root)
    res = tmp_path / "res.json"
    res.write_bytes(orjson.dumps(_results(ids[0], 25.0)))
    gpath = tmp_path / "g.json"
    gpath.write_bytes(orjson.dumps(to_dict(_g(1))))
    before = reg.path.read_bytes()
    # the CLI runs on the real clock: this eval window (Jan 2026) is closed, monitoring is allowed
    assert cli(["monitor", "--root", str(root), "--genome", ids[0][:10], "--results", str(res)]) == 0
    out = capsys.readouterr().out
    assert "EARLY_PASS" in out and "not recorded" in out and reg.path.read_bytes() == before
    assert cli(["monitor", "--root", str(root), "--genome", str(gpath), "--results", str(res), "--record"]) == 0
    out = capsys.readouterr().out
    assert "recorded early_pass" in out and len(reg.lines()) == 4
    assert reg.records()[-1]["source"]["results_sha256"] == hashlib.sha256(res.read_bytes()).hexdigest()
    assert cli(["monitor", "--root", str(root), "--genome", "zz", "--results", str(res)]) == 2
    assert cli(["verify", "--root", str(root)]) == 0
