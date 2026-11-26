import hashlib
from datetime import UTC, datetime

import orjson
import pytest

from doppel.strategy.cli import main as cli
from doppel.strategy.genome import Condition, Genome, MakerRule, Source, Universe, to_dict
from doppel.strategy.registry import GENESIS, Registry, RegistryError, holm

NOW = datetime(2026, 11, 25, 23, 0, tzinfo=UTC).timestamp()
DAY = 86400.0
WIN = ("2026-11-26T00:00:00Z", "2026-11-29T00:00:00Z")


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def _g(h=1.0, guard=False):
    pause = (Condition("brti_move_5s", ">", 30.0),) if guard else ()
    return Genome(name=f"h1-maker-h{h:g}{'-guard' if guard else ''}", mode="maker",
                  universe=Universe(venue="pmus", tte_min_s=20), source=Source(kind="manual"),
                  maker=MakerRule(half_spread_c=h, size=10, pause_when=pause), created_tw=0)


def _reg(tmp_path, clock=None):
    return Registry(tmp_path, now=clock or Clock(), code={"sha": "c0ffee", "dirty": False})


def test_holm_known_values():
    assert holm([0.01, 0.04, 0.03, 0.005]) == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert holm([0.01, 0.04, 0.03, 0.005, 1.0]) == pytest.approx([0.04, 0.09, 0.09, 0.025, 1.0])
    assert holm([0.5, 0.5]) == pytest.approx([1.0, 1.0])          # capped at 1
    assert holm([0.02]) == pytest.approx([0.02]) and holm([]) == []


def test_chain_hash_and_idempotent_registration(tmp_path):
    reg = _reg(tmp_path)
    r0 = reg.register(_g(1.0), "h1", "maker beats 0 net per window", WIN)
    r1 = reg.register(_g(2.0), "h1", "maker beats 0 net per window", WIN)
    lines = reg.lines()
    assert len(lines) == 2 and r0["seq"] == 0 and r1["seq"] == 1
    assert r0["prev"] == GENESIS and r1["prev"] == hashlib.sha256(lines[0]).hexdigest()
    for ln in lines:
        rec = orjson.loads(ln)
        body = {k: v for k, v in rec.items() if k != "hash"}
        assert rec["hash"] == hashlib.sha256(orjson.dumps(body, option=orjson.OPT_SORT_KEYS)).hexdigest()
    assert r0["genome_id"] == _g(1.0).id and r0["code_sha"] == "c0ffee"
    assert r0["eval_window"]["start"] == WIN[0] and r0["eval_window"]["end"] == WIN[1]
    before = reg.path.read_bytes()
    again = reg.register(_g(1.0), "other-family", "different words", WIN)   # same id: no-op
    assert again == r0 and reg.path.read_bytes() == before
    assert reg.verify() == [] and reg.head() == r1["hash"]


def test_refuses_past_or_bad_windows(tmp_path):
    reg = _reg(tmp_path)
    for win, why in [(("2026-11-25T22:00:00Z", WIN[1]), "not after now"),
                     ((NOW, NOW + DAY), "not after now"),                       # starts AT now
                     ((WIN[1], WIN[0]), "end must be after"),
                     (("2026-11-26T00:00:00", WIN[1]), "naive")]:
        with pytest.raises(RegistryError, match=why):
            reg.register(_g(), "h1", "h", win)
    assert not reg.path.exists() or reg.lines() == []
    with pytest.raises(RegistryError, match="family"):
        reg.register(_g(), " ", "h", WIN)
    bad = Genome(name="bad", mode="maker", universe=Universe(venue="pmus"), source=Source(kind="manual"))
    with pytest.raises(RegistryError, match="validate"):
        reg.register(bad, "h1", "h", WIN)
    # datetime and epoch forms are accepted
    r = reg.register(_g(3.0), "h1", "h", (datetime(2026, 11, 27, tzinfo=UTC), NOW + 3 * DAY))
    assert r["eval_window"]["start"] == "2026-11-27T00:00:00Z"


def test_results_append_only_first_binding(tmp_path):
    clock = Clock()
    reg = _reg(tmp_path, clock)
    gid = reg.register(_g(), "h1", "h", WIN)["genome_id"]
    with pytest.raises(RegistryError, match="closes"):
        reg.record_result(gid, {"p_value": 0.01})                       # window still open
    clock.t = datetime(2026, 11, 29, 1, tzinfo=UTC).timestamp()
    with pytest.raises(RegistryError, match="not registered"):
        reg.record_result("ab" * 32, {"p_value": 0.01})
    for bad in ({"p": 0.1}, {"p_value": 1.5}, {"p_value": True}, {"p_value": "0.1"}):
        with pytest.raises(RegistryError):
            reg.record_result(gid, bad)
    before = reg.path.read_bytes()
    r1 = reg.record_result(gid, {"p_value": 0.2, "net_c_per_window": 0.3, "windows": 310})
    mid = reg.path.read_bytes()
    r2 = reg.record_result(gid, {"p_value": 0.001})                      # a re-run cannot replace it
    after = reg.path.read_bytes()
    assert mid.startswith(before) and after.startswith(mid)              # only ever appended
    assert r1["binding"] is True and r2["binding"] is False and r2["seq"] == 2
    led = reg.ledger("h1")
    assert led["rows"][0]["p_value"] == 0.2 and led["rows"][0]["results"] == 2
    assert led["winner"] is None and reg.verify() == []


def test_ledger_holm_over_whole_family(tmp_path):
    clock = Clock()
    reg = _reg(tmp_path, clock)
    gs = [_g(h, guard) for h in (1.0, 2.0) for guard in (False, True)]
    ids = [reg.register(g, "h1", "h", WIN)["genome_id"] for g in gs]
    reg.register(_g(5.0), "other", "h", WIN)                            # other families don't count
    clock.t = datetime(2026, 11, 30, tzinfo=UTC).timestamp()
    for gid, p in zip(ids, [0.01, 0.04, 0.03, 0.005]):
        reg.record_result(gid, {"p_value": p})
    led = reg.ledger("h1")
    assert led["m"] == 4 and led["tested"] == 4 and led["pending"] == 0
    assert [r["p_holm"] for r in led["rows"]] == pytest.approx([0.03, 0.06, 0.06, 0.02])
    assert [r["survives"] for r in led["rows"]] == [True, False, False, True]
    assert led["winner"] == ids[3]
    # a fifth variant registered later with no result still charges the family (p = 1)
    clock.t = NOW
    reg.register(_g(3.0), "h1", "h", ("2026-12-01T00:00:00Z", "2026-12-02T00:00:00Z"))
    led = reg.ledger("h1")
    assert led["m"] == 5 and led["pending"] == 1
    assert [r["p_holm"] for r in led["rows"]] == pytest.approx([0.04, 0.09, 0.09, 0.025, 1.0])
    assert led["winner"] == ids[3] and sum(r["survives"] for r in led["rows"]) == 2
    led = reg.ledger("h1", alpha=0.02)
    assert led["winner"] is None


def _rewrite(reg, i, fn, rehash=False):
    lines = reg.lines()
    rec = orjson.loads(lines[i])
    fn(rec)
    if rehash:
        body = {k: v for k, v in rec.items() if k != "hash"}
        rec["hash"] = hashlib.sha256(orjson.dumps(body, option=orjson.OPT_SORT_KEYS)).hexdigest()
    lines[i] = orjson.dumps(rec, option=orjson.OPT_SORT_KEYS)
    reg.path.write_bytes(b"".join(ln + b"\n" for ln in lines))


def test_verify_detects_tampering(tmp_path):
    clock = Clock()
    reg = _reg(tmp_path, clock)
    for h in (1.0, 2.0, 3.0):
        reg.register(_g(h), "h1", "h", WIN)
    clock.t = datetime(2026, 11, 30, tzinfo=UTC).timestamp()
    reg.record_result(_g(1.0).id, {"p_value": 0.3})
    pristine = reg.path.read_bytes()
    head = reg.head()
    assert reg.verify() == []

    def setp(r):
        r["verdict"]["p_value"] = 0.001

    _rewrite(reg, 3, setp)                                       # edit the LAST line, hash untouched
    assert any("line 3: self-hash mismatch" in e for e in reg.verify())
    reg.path.write_bytes(pristine)
    _rewrite(reg, 3, setp, rehash=True)                          # ...and recompute its own hash:
    assert reg.verify() == [] and reg.head() != head             # only an anchored head catches it
    reg.path.write_bytes(pristine)

    def fam(r):
        r["family"] = "h1-renamed"

    _rewrite(reg, 1, fam, rehash=True)                           # middle line: next prev breaks
    assert any("line 2: prev does not match" in e for e in reg.verify())
    reg.path.write_bytes(pristine)

    def spread(r):
        r["genome"]["maker"]["half_spread_c"] = 0.5

    _rewrite(reg, 0, spread, rehash=True)                        # genome no longer hashes to its id
    assert any("stored genome hashes to" in e for e in reg.verify())
    lines = pristine.split(b"\n")
    reg.path.write_bytes(b"\n".join(lines[:1] + lines[2:]))      # delete a line
    errs = reg.verify()
    assert any("seq" in e for e in errs) and any("prev" in e for e in errs)
    reg.path.write_bytes(pristine[:-10])                         # truncated tail
    assert any("partial line" in e for e in reg.verify())


def test_cli_register_ledger_verify(tmp_path, capsys):
    g = _g(2.0)
    d = to_dict(g)
    d["evidence"] = {"family": "h1-maker-v1", "eval_window": ["2030-01-01T00:00:00Z", "2030-01-04T00:00:00Z"]}
    p = tmp_path / "g.json"
    p.write_bytes(orjson.dumps(d, option=orjson.OPT_INDENT_2))
    root = tmp_path / "repo"
    args = ["--root", str(root)]
    assert cli(["register", "--genome", str(p), "--family", "h1-maker-v1", "--hypothesis", "net > 0", *args]) == 0
    assert cli(["register", "--genome", str(p), "--family", "h1-maker-v1", "--hypothesis", "net > 0", *args]) == 0
    assert len(Registry(root).lines()) == 1                     # idempotent through the CLI too
    assert cli(["result", "--id", "ab" * 32, "--p", "0.01", *args]) == 2
    assert cli(["ledger", "--family", "h1-maker-v1", *args]) == 0
    assert cli(["verify", *args]) == 0
    out = capsys.readouterr().out
    assert "m=1 tested=0 pending=1" in out and "OK" in out and "2030-01-01T00:00:00Z" in out
    rec = Registry(root).records()[0]
    assert len(rec["code_sha"] or "") in (0, 40)                 # git SHA of this checkout when available
    assert cli(["register", "--genome", str(p), "--family", "f2", "--hypothesis", "h", "--start",
                "2020-01-01T00:00:00Z", "--end", "2020-01-02T00:00:00Z", "--root", str(tmp_path / "r2")]) == 2


def test_registry_append_does_not_mark_code_dirty(tmp_path):
    """Audit A4-F3: only code changes count as dirty, not the registry's own append."""
    import subprocess
    from doppel.strategy.registry import git_sha
    run = lambda *a: subprocess.run(["git", *a], cwd=tmp_path, check=True, capture_output=True)
    run("init", "-q"); run("config", "user.email", "t@t"); run("config", "user.name", "t")
    (tmp_path / "registry").mkdir(); (tmp_path / "src").mkdir()
    (tmp_path / "registry" / "genomes.jsonl").write_text("a\n"); (tmp_path / "src" / "m.py").write_text("x=1\n")
    run("add", "."); run("commit", "-qm", "init")
    (tmp_path / "registry" / "genomes.jsonl").write_text("a\nb\n")
    assert git_sha(tmp_path / "src")["dirty"] is False
    (tmp_path / "src" / "m.py").write_text("x=2\n")
    assert git_sha(tmp_path / "src")["dirty"] is True


def test_draft_genome_cannot_be_registered(tmp_path):
    """rulefit emits status "draft"; status is not part of the id, so a draft would otherwise seal
    silently into the append-only registry."""
    import pytest

    from doppel.strategy.genome import Genome, KillRule, MakerRule, Source, Universe, to_dict
    from doppel.strategy.registry import Registry, RegistryError
    g = Genome(name="d", mode="maker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
               maker=MakerRule(), kill=KillRule(min_windows=30), created_tw=0)
    d = to_dict(g) | {"status": "draft"}
    reg = Registry(tmp_path, now=lambda: 0.0, code={"sha": "c0ffee", "dirty": False})
    with pytest.raises(RegistryError, match="draft"):
        reg.register(d, "fam", "hyp", (10, 20))
    assert reg.lines() == []
