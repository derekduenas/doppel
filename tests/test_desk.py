"""W11 desk: build desk.json + index.html from fixtures shaped by the producing modules.

Static fixtures (tests/fixtures_desk/): recorder status.json (Python + Rust), census, adverse, clock0,
xvenue (+ the kalshi_meta.json cache that must be ignored), FINDINGS.md, and malformed files.
Generated per test with the real code: the registry (``Registry.register`` of the sealed genomes,
``record_result``) and twin outputs (``twin.verdict.verdict`` over engine-shaped window results,
wrapped as ``twin.cli`` writes them).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import orjson
import pytest

from doppel.desk import build as B
from doppel.desk import cli as desk_cli
from doppel.desk.render import TEMPLATE, render
from doppel.strategy.genome import Genome, describe, from_dict
from doppel.strategy.registry import Registry
from doppel.tape.sources.base import SourceStats
from doppel.tape.universe import pmus_slug
from doppel.twin.verdict import commercial, verdict

REPO = Path(__file__).resolve().parents[1]
FIX = Path(__file__).parent / "fixtures_desk"
NOW = 1796104800                 # 2026-12-01T06:00:00Z
REG_NOW = 1795608000             # 2026-11-25T12:00:00Z (before every eval window)
RES_NOW = 1795996800 + 3600      # after the H3 window closed
H3_S, H3_E = 1795737600, 1795996800   # 2026-11-27 .. 2026-11-30 (the sealed H3 window)
DISK = {"free_gb": 120.5, "total_gb": 500.0, "used_pct": 75.9}
ARM_OFFSET = {"base": 0.0, "anon": 0.1, "ident": 0.5, "placebo": -0.2}
N_WIN = 24


def disk(_p):
    return dict(DISK)


def genome(rel: str) -> Genome:
    return from_dict(orjson.loads((REPO / "genomes" / rel).read_bytes()))


def win(i: int, net_c: float, start0: int = H3_S, ctr: float = 10.0) -> dict:
    """One window result in the shape twin.engine.WindowSim.run returns (fields verdict() reads)."""
    st = start0 + 300 * i
    return {"slug": pmus_slug("btc", "5m", st), "asset": "btc", "horizon": "5m", "start": st, "end": st + 300,
            "source": "ws", "stream": "pmus_ws_5m", "tape": {"venue_volume": 1000.0}, "ptb": 100.0,
            "settle": 101.0, "outcome": 1.0, "eligible": True, "excluded": None, "stood_down": False,
            "decisions": 280, "n_fills": 1, "contracts": ctr, "fees_usd": 0.0, "gross_usd": net_c / 100.0,
            "net_usd": net_c / 100.0, "net_c": net_c,
            "fills": [{"role": "maker", "qty": ctr, "ref_qty": 2 * ctr, "via": "trade", "markout_c": {"5": 0.1}}],
            "taker": {"attempts": 0, "fills": 0, "slippage_miss": 0, "no_book": 0, "depth_capped": 0, "closed": 0,
                      "risk_blocked": 0, "empty_side": 0},
            "maker": {"placed": 2, "post_only_rejects": 0, "no_book_rejects": 0, "beyond_depth": 0, "no_fair": 0,
                      "cancels": 1, "closed": 0, "self_cross": 0, "quoted_qty": 20.0, "fill_qty": ctr,
                      "quote_live_s": 30.0}}


def base_net(i: int) -> float:
    return float(i % 5) - 1.0


def twin_doc(g: Genome, results: list[dict], rng: tuple[int, int], delay: int = 0, diag: bool = False) -> dict:
    """What twin.cli.main writes."""
    v = verdict(g, results)
    if diag:
        v["decision"] = "DIAGNOSTIC"
        v["reason"] = "relaxed data-quality gates: not evidence for or against the genome"
    return {"twin": {"code_sha256": "c" * 64, "config": {"latency_ms": 250.0}, "allow_stale_rest": diag,
                     "flow_delay_s": delay, "diagnostic": diag},
            "genome": {"id": g.id, "name": g.name, "status": g.status, "describe": describe(g)},
            "range": [rng[0], rng[1]], "verdict": v, "windows": results}


def put(path: Path, doc: dict, mtime: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS))
    os.utime(path, (mtime, mtime))


def arm_results(arm: str, offset_extra: float = 0.0) -> list[dict]:
    rs = [win(i, base_net(i) + ARM_OFFSET[arm] + offset_extra) for i in range(N_WIN)]
    rs.append({**win(N_WIN, 0.0), "eligible": False, "excluded": "no_trades"})
    return rs


@pytest.fixture
def world(tmp_path: Path) -> dict:
    """Fixture tree copied to tmp, plus a real registry and real twin outputs."""
    shutil.copytree(FIX, tmp_path / "w")
    w = tmp_path / "w"
    root, lab = w / "root", w / "lab"
    code = {"sha": "f" * 40, "dirty": False}
    reg = Registry(root, now=lambda: REG_NOW, code=code)
    h3 = {arm: genome(f"h3/h3-{arm}.json") for arm in ("base", "anon", "ident", "placebo")}
    for arm, g in h3.items():
        reg.register(g, "h3-flowguard-4arm-v1", "fixture: identified flow beats anon/placebo",
                     ("2026-11-27T00:00:00Z", "2026-11-30T00:00:00Z"))
    h1 = genome("h1/h1-maker-h1.json")
    reg.register(h1, "h1-maker-v1", "fixture: guarded maker", ("2026-11-26T00:00:00Z", "2026-11-29T00:00:00Z"))
    Registry(root, now=lambda: RES_NOW, code=code).record_result(h3["base"].id, {"p_value": 0.3, "decision": "CONTINUE"})
    t = 1796090000
    for i, (arm, g) in enumerate(h3.items()):
        put(lab / "twin" / "h3" / f"{arm}.json", twin_doc(g, arm_results(arm), (H3_S, H3_E)), t + i)
    put(lab / "twin" / "h3" / "ident-d5.json", twin_doc(h3["ident"], arm_results("ident", -0.3), (H3_S, H3_E), delay=5), t + 10)
    # a newer DIAGNOSTIC run of base must never be picked
    put(lab / "twin" / "h3" / "base-diag.json",
        twin_doc(h3["base"], [win(i, 50.0) for i in range(N_WIN)], (H3_S, H3_E), diag=True), t + 20)
    # h1: an interim replay inside its window over 1 day (contracts -> calculator preset)
    h1s = 1795651200
    put(lab / "twin" / "h1-interim.json",
        twin_doc(h1, [win(i, 1.5, h1s, ctr=4.0) for i in range(N_WIN)], (h1s, h1s + 86400)), t + 30)
    return {"w": w, "data": w / "tape", "lab": lab, "root": root, "h3": h3, "h1": h1}


def run(world: dict, now: float = NOW) -> dict:
    return B.build(world["data"], world["lab"], world["root"], now, disk=disk)


# ---------------------------------------------------------------- sections and values
def test_recorder_python_vs_rust(world):
    d = run(world)
    rec = d["recorder"]
    assert rec["python"]["pid"] == 4242 and rec["rust"]["pid"] == 5151
    assert rec["python"]["status_age_s"] == 4.0 and rec["rust"]["status_age_s"] == 3.0
    assert rec["python"]["stale"] is False and rec["python"]["disk"] == DISK
    assert rec["python"]["src"] == {"file": "tape:status.json", "mtime": rec["python"]["src"]["mtime"],
                                    "tw": "2026-12-01T05:59:56Z"}
    assert rec["rust"]["src"]["file"] == "tape-rust:status.json"
    by = {s["name"]: s for s in rec["streams"]}
    assert list(by) == ["kalshi_books", "kalshi_ws", "pmus_ws_5m", "rtds_a"]   # union, sorted
    assert by["kalshi_ws"]["python"] is None                                    # Rust-only stream
    assert by["kalshi_books"]["rust"] is None
    a = by["rtds_a"]
    assert a["python"]["msgs"] == 120345 and a["rust"]["msgs"] == 120400
    assert a["python"]["age_at_build_s"] == 4.4                                 # 0.4 reported + 4 s file age
    assert a["rust"]["age_at_build_s"] == 3.3
    assert a["python"]["drops"] is None and a["rust"]["drops"] == 0             # Python has no drop counter
    assert by["kalshi_ws"]["rust"]["drops"] == 12 and by["kalshi_ws"]["rust"]["write_errors"] == 1
    assert by["kalshi_books"]["python"]["flag"] == "stale"                      # 95 s + 4 s > 60 s
    assert by["pmus_ws_5m"]["python"]["flag"] == "errors"
    assert by["pmus_ws_5m"]["python"]["extra"] == {"heartbeats": 40}
    assert a["python"]["flag"] == "ok"
    # an hour later the frozen status file is stale on both sides
    later = run(world, NOW + 3600)["recorder"]
    assert later["python"]["stale"] and later["rust"]["stale"]
    assert {s["python"]["flag"] for s in later["streams"] if s["python"]} == {"stale"}


def test_registry_families_phases_and_twin(world):
    d = run(world)
    st = d["strategies"]
    reg = st["registry"]
    assert reg["present"] and reg["lines"] == 6 and reg["verify"]["ok"] and reg["bad_lines"] == 0
    fams = {f["family"]: f for f in st["families"]}
    assert list(fams) == ["h1-maker-v1", "h3-flowguard-4arm-v1"]
    h3 = {g["arm"]: g for g in fams["h3-flowguard-4arm-v1"]["genomes"]}
    assert set(h3) == {"base", "anon", "ident", "placebo"}
    assert fams["h3-flowguard-4arm-v1"]["tested"] == 1 and fams["h3-flowguard-4arm-v1"]["pending"] == 3
    b = h3["base"]
    assert b["phase"] == "tested" and b["p_value"] == 0.3 and b["p_holm"] == 1.0 and not b["survives"]
    assert h3["anon"]["phase"] == "awaiting result" and h3["anon"]["seconds_remaining"] == 0
    assert b["eval_window"]["start"] == "2026-11-27T00:00:00Z" and b["eval_window"]["end_s"] == H3_E
    # the latest NON-diagnostic, zero-delay run is the one shown
    assert b["twin"]["decision"] == "CONTINUE" and b["twin"]["src"]["file"] == "lab:twin/h3/base.json"
    assert b["twin_runs"] == 2
    assert b["twin"]["window_fit"] == "prereg"
    assert b["twin"]["windows_evaluated"] == N_WIN and b["twin"]["windows_traded"] == N_WIN
    exp = round(sum(base_net(i) for i in range(N_WIN)) / N_WIN, 4)
    assert b["twin"]["net_c_per_window"]["mean"] == exp
    assert "commercial" in b["twin"] and b["twin"]["commercial"]["windows_per_day"] == 288
    assert h3["ident"]["twin"]["src"]["file"] == "lab:twin/h3/ident.json"       # not the 5 s delay run
    h1 = fams["h1-maker-v1"]["genomes"][0]
    assert h1["phase"] == "awaiting result" and h1["twin"]["window_fit"] == "partial"


def test_registry_phase_scheduled_and_evaluating(world):
    d = run(world, 1795651200 - 7200)          # 2 h before the H1 window opens
    h1 = next(f for f in d["strategies"]["families"] if f["family"] == "h1-maker-v1")["genomes"][0]
    assert h1["phase"] == "scheduled" and h1["seconds_to_start"] == 7200
    d = run(world, 1795651200 + 3600)
    h1 = next(f for f in d["strategies"]["families"] if f["family"] == "h1-maker-v1")["genomes"][0]
    assert h1["phase"] == "evaluating" and h1["seconds_remaining"] == 3 * 86400 - 3600


def test_h3_four_arm_table(world):
    h = run(world)["strategies"]["h3"]
    assert h["ready"] and h["family"] == "h3-flowguard-4arm-v1" and h["window_fit"] == "prereg"
    rows = {r["arm"]: r for r in h["rows"]}
    assert [r["arm"] for r in h["rows"]] == ["base", "anon", "ident", "placebo"]
    assert rows["base"]["vs_base"] is None
    for arm, off in ARM_OFFSET.items():
        if arm == "base":
            continue
        vb = rows[arm]["vs_base"]
        assert vb["n_pairs"] == N_WIN                      # the ineligible window is not paired
        assert vb["delta_c_per_window"] == pytest.approx(off, abs=1e-4)
        assert vb["ci95"][0] == pytest.approx(off, abs=1e-4) and vb["ci95"][1] == pytest.approx(off, abs=1e-4)
        assert vb["delta_usd_total"] == pytest.approx(off * N_WIN / 100.0, abs=1e-4)
    assert rows["ident"]["src"]["file"] == "lab:twin/h3/ident.json"
    sweep = {s["flow_delay_s"]: s for s in h["delay_sweep"]}
    assert sorted(sweep) == [0, 5]
    assert sweep[5]["vs_base"]["delta_c_per_window"] == pytest.approx(0.2, abs=1e-4)


def test_h3_not_yet_when_an_arm_is_missing_or_ranges_differ(world):
    (world["lab"] / "twin" / "h3" / "placebo.json").unlink()
    h = run(world)["strategies"]["h3"]
    assert not h["ready"] and "placebo" in h["why_not"] and h["rows"] == []
    g = world["h3"]["placebo"]
    put(world["lab"] / "twin" / "h3" / "placebo.json",
        twin_doc(g, arm_results("placebo"), (H3_S, H3_E - 3600)), 1796090100)
    h = run(world)["strategies"]["h3"]
    assert not h["ready"] and "different ranges" in h["why_not"]


def test_newer_offwindow_runs_do_not_displace_prereg(world):
    """A later, wider-range replay (doppel-twin without --from/--to) must not replace the prereg run,
    neither in the registry row nor in the H3 table; another family's 'base' arm is ignored."""
    g = world["h3"]["ident"]
    wide = [win(i, 9.0) for i in range(N_WIN + 12)]
    put(world["lab"] / "twin" / "zz-ident-wide.json", twin_doc(g, wide, (H3_S, H3_E + 86400)), 1796099999)
    root = world["root"]
    od = orjson.loads((REPO / "genomes" / "h3" / "h3-base.json").read_bytes())
    other = from_dict({**od, "name": "other-base", "evidence": {**od["evidence"], "family": "zz-other"}})
    Registry(root, now=lambda: REG_NOW + 1, code={"sha": "f" * 40, "dirty": False}).register(
        other, "zz-other", "fixture: unrelated family with an arm label", ("2026-11-27T00:00:00Z", "2026-11-30T00:00:00Z"))
    d = run(world)
    h = d["strategies"]["h3"]
    assert h["ready"] and h["family"] == "h3-flowguard-4arm-v1" and h["window_fit"] == "prereg"
    rows = {r["arm"]: r for r in h["rows"]}
    assert rows["ident"]["src"]["file"] == "lab:twin/h3/ident.json"
    assert rows["ident"]["vs_base"]["delta_c_per_window"] == pytest.approx(0.5, abs=1e-4)
    fam = next(f for f in d["strategies"]["families"] if f["family"] == "h3-flowguard-4arm-v1")
    ident = next(x for x in fam["genomes"] if x["arm"] == "ident")
    assert ident["twin"]["src"]["file"] == "lab:twin/h3/ident.json" and ident["twin_runs"] == 3
    # with ONLY the off-window run left, it is shown, but labelled as such
    (world["lab"] / "twin" / "h3" / "ident.json").unlink()
    (world["lab"] / "twin" / "h3" / "ident-d5.json").unlink()
    d = run(world)
    ident = next(x for f in d["strategies"]["families"] for x in f["genomes"] if x["name"] == "h3-ident")
    assert ident["twin"]["window_fit"] == "off-window"
    assert not d["strategies"]["h3"]["ready"] and "different ranges" in d["strategies"]["h3"]["why_not"]


def test_evidence_sections(world):
    ev = run(world)["evidence"]
    f = ev["findings"]
    assert [r["id"] for r in f["rows"]] == ["E1", "E2", "E10"]
    e1 = f["rows"][0]
    assert e1["line"] == "Identity tape streams every match with `proxyWallet` (wss://example.invalid/ws)."
    assert e1["confidence"] == "observed" and e1["src"]["file"] == "root:evidence/2026-11-25-probes/FINDINGS.md"
    assert f["rows"][1]["line"].startswith("A pipe | inside the finding")      # pipe inside the cell kept
    c = ev["census"]
    assert c["src"]["file"] == "lab:census/census-abcdef012345-20261126T1200Z.json"  # latest by built_tw
    assert c["src"]["tw"] == "2026-11-26T13:20:00Z"
    assert c["wallets"] == 3 and c["eligible"] == 2 and "0xaaa" not in json.dumps(c)
    rows = {(r["role"], r["label"]): r for r in c["rows"]}
    assert rows[("cand", "COMPLETE_SET_MAKER")]["per_day"] == 1200.0
    assert rows[("ctrl", "LATE_TAKER")]["wallets"] == 3
    (a,) = ev["adverse"]
    assert a["hz"] == "5m" and a["total"]["adverse_c"] == -0.5
    assert a["by_model_edge"]["2..5c"]["maker_pnl_c"] == 4.6 and a["by_tte"]["30-60s"] is None
    k = ev["clock0"]
    assert k["src"]["file"] == "lab:clock0/clock0-20261126.json"
    assert k["auc_model"] == 0.708 and k["auc_size_only"] == 0.478 and k["status"] == "ok"
    assert k["as_of"] == "2026-11-25T21:46:40Z" and k["top_features"][0] == ["log_usd", 0.08]
    xs = ev["xvenue"]
    assert [x["fees"]["pmus_coef"] for x in xs] == [0.0695, 0.095]              # kalshi_meta.json ignored
    assert xs[0]["summary"]["episode_dollars"] == 28.2 and xs[0]["equivalence"]["result_match"] == 13
    assert xs[0]["src"]["tw"] == "2026-11-26T01:00:00Z"


def test_problems_listed_not_fatal(world):
    d = run(world)
    files = {p["file"] for p in d["problems"]}
    assert files == {"lab:clock0/clock0-broken.json", "lab:twin/garbage.json"}


def test_breakeven_matches_twin_commercial():
    assert B.breakeven(0.5, 10000, 60.0) == {"op_usd_per_day": 2.0, "edge_usd_per_day": 50.0,
                                             "net_usd_per_day": 48.0, "breakeven_contracts_per_day": 400.0}
    assert B.breakeven(-0.2, 10000, 60.0)["breakeven_contracts_per_day"] is None
    assert B.breakeven(0.0, 10000, 60.0)["net_usd_per_day"] == -2.0
    g = genome("h3/h3-base.json")
    ev = [win(i, 2.0) for i in range(10)]
    pc = sum(r["net_c"] for r in ev) / sum(r["contracts"] for r in ev)
    for op in (60.0, 450.0, 770.0):
        c = commercial(g, ev, 1.0, pc, op)
        b = B.breakeven(pc, 1000.0, op)
        assert b["breakeven_contracts_per_day"] == c["breakeven_contracts_per_day"]
        assert b["op_usd_per_day"] == c["op_cost_usd_per_day"]


def test_calculator_presets(world):
    calc = run(world)["calculator"]
    by = {p["label"]: p for p in calc["presets"]}
    h1 = by["twin h1-maker-h1 (partial)"]
    assert h1["net_c_per_contract"] == pytest.approx(0.375) and h1["contracts_per_day"] == 96.0  # 96 ct over 1 day
    e24 = next(p for k, p in by.items() if k.startswith("E24"))
    assert e24["net_c_per_contract"] == 4.6 and e24["src"]["file"] == "lab:adverse/adverse-btc5m.json"
    assert e24["contracts_per_day"] is None and e24["op_usd_month"] is None and "not ours" in e24["note"]
    assert calc["example"] == B.breakeven(**{k: calc["defaults"][k] for k in
                                             ("net_c_per_contract", "contracts_per_day")},
                                          op_usd_month=calc["defaults"]["op_usd_month"])


# ---------------------------------------------------------------- page, determinism, read-only
def _tree(p: Path) -> dict:
    return {str(f.relative_to(p)): (hashlib.sha256(f.read_bytes()).hexdigest(), f.stat().st_mtime_ns)
            for f in sorted(p.rglob("*")) if f.is_file()}


def test_cli_build_deterministic_and_read_only(world, tmp_path, monkeypatch):
    monkeypatch.setattr(B, "disk_free", disk)
    monkeypatch.setattr(desk_cli, "build", lambda *a, **k: B.build(*a, **{**k, "disk": disk}))
    before = _tree(world["w"])
    args = ["build", "--data", str(world["data"]), "--lab", str(world["lab"]), "--root", str(world["root"]),
            "--now", "2026-12-01T06:00:00Z"]
    # the fixture root has no desk/ template: the CLI falls back to the repo's
    assert desk_cli.main(args + ["--out", str(tmp_path / "o1")]) == 0
    assert desk_cli.main(args + ["--out", str(tmp_path / "o2")]) == 0
    assert _tree(world["w"]) == before                                         # inputs untouched
    for name in ("desk.json", "index.html"):
        assert (tmp_path / "o1" / name).read_bytes() == (tmp_path / "o2" / name).read_bytes()
    d = orjson.loads((tmp_path / "o1" / "desk.json").read_bytes())
    assert d["built"] == "2026-12-01T06:00:00Z" and d["built_s"] == NOW
    raw = (tmp_path / "o1" / "desk.json").read_text()
    assert str(world["w"]) not in raw                                          # paths are root-relative labels
    assert desk_cli.main(args + ["--out", str(world["lab"])]) == 2             # never write into an input


def test_html_offline_escaped_and_complete(world):
    d = run(world)
    page = render(d, (REPO / TEMPLATE).read_text(encoding="utf-8"))
    # offline: nothing loaded from anywhere
    assert not re.search(r"""(?<![-\w])src\s*=""", page)
    assert not re.search(r"""href\s*=\s*["'](?!#)""", page)
    assert "@import" not in page and "url(" not in page
    # sections in order
    pos = [page.index(f'id="{s}"') for s in ("recorder", "strategies", "evidence", "calculator")]
    assert pos == sorted(pos)
    # embedded data round-trips; hostile text cannot close the script tag
    m = re.search(r'<script type="application/json" id="desk-data">(.*?)</script>', page, re.S)
    assert m and json.loads(m.group(1)) == json.loads(B.dumps(d))
    assert page.count("</script>") == 2 and "<b>x</b>" not in page and "<b>bold</b>" not in page
    assert "&lt;/script&gt;&lt;b&gt;bold&lt;/b&gt;" in page
    # values and their sources are on the page
    for s in ("120,345", "120,400", "n/a", "tape-rust:status.json", "lab:twin/h3/ident.json", "+0.50",
              "h3-flowguard-4arm-v1", "awaiting result", "0.708", "28.20", "lab:census/census-abcdef012345-20261126T1200Z.json",
              "E10", "chain intact", "Inputs skipped", "lab:clock0/clock0-broken.json", "120.5 GB"):
        assert s in page, s


def test_missing_inputs_render_not_yet(tmp_path):
    (tmp_path / "root").mkdir()
    d = B.build(tmp_path / "tape", tmp_path / "lab", tmp_path / "root", NOW, disk=disk)
    assert d["recorder"]["python"] is None and d["recorder"]["rust"] is None and d["recorder"]["streams"] == []
    assert d["strategies"]["registry"]["present"] is False and d["strategies"]["families"] == []
    assert not d["strategies"]["h3"]["ready"]
    ev = d["evidence"]
    assert ev["census"] is None and ev["clock0"] is None and ev["adverse"] == [] and ev["xvenue"] == []
    assert ev["findings"]["rows"] == [] and d["problems"] == []
    page = render(d, (REPO / TEMPLATE).read_text(encoding="utf-8"))
    assert page.count("not yet") >= 6


def test_malformed_status_and_registry_line(world):
    (world["data"] / "status.json").write_text('{"tw": 1, "sources": ')
    p = world["root"] / "registry" / "genomes.jsonl"
    p.write_bytes(p.read_bytes() + b"{broken\n")
    d = run(world)
    assert d["recorder"]["python"] is None and d["recorder"]["rust"] is not None
    reg = d["strategies"]["registry"]
    assert reg["bad_lines"] == 1 and reg["verify"]["ok"] is False
    assert len(d["strategies"]["families"]) == 2                               # good lines still shown
    assert {"tape:status.json", "root:registry/genomes.jsonl"} <= {x["file"] for x in d["problems"]}


def test_status_fixtures_track_recorder_shapes():
    """The fixtures must carry every key the recorders write (drift guard)."""
    py = orjson.loads((FIX / "tape" / "status.json").read_bytes())
    keys = set(SourceStats().as_dict())
    assert {"tw", "pid", "uptime_s", "sources"} <= set(py)
    for s in py["sources"].values():
        assert keys <= set(s)
    rs = orjson.loads((FIX / "tape-rust" / "status.json").read_bytes())
    src = (REPO / "crates" / "doppel-tape" / "src" / "recorder.rs").read_text()
    src = src[src.index("pub fn as_value"):src.index("Value::Object(m)")]
    rkeys = set(re.findall(r'm\.insert\("([a-z_]+)"\.into\(\)', src)) - {"write_errors"}
    assert rkeys >= {"msgs", "backpressure_drops", "last_msg_age_s"}
    for s in rs["sources"].values():
        assert rkeys <= set(s)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_page_js_breakeven_matches_python():
    tpl = (REPO / TEMPLATE).read_text(encoding="utf-8")
    js = re.search(r'<script id="desk-calc">(.*?)</script>', tpl, re.S).group(1)
    cases = [(0.5, 10000, 60), (4.6, 2500, 450), (-0.3, 1000, 60), (1.37, 777, 123)]
    prog = js + "\nconsole.log(JSON.stringify(%s.map(function(c){return breakeven(c[0],c[1],c[2]);})));" % json.dumps(cases)
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30, check=True).stdout
    got = json.loads(out)
    for c, g in zip(cases, got):
        exp = B.breakeven(*c)
        for k, v in exp.items():
            assert (g[k] is None) == (v is None)
            if v is not None:
                assert g[k] == pytest.approx(v, abs=0.051)


def test_real_repo_smoke(tmp_path):
    """The repo's own registry and FINDINGS build cleanly (no tape or lab needed)."""
    d = B.build(tmp_path / "tape", tmp_path / "lab", REPO, NOW, disk=disk)
    st = d["strategies"]
    assert st["registry"]["verify"]["ok"]
    assert sum(f["m"] for f in st["families"]) >= 20
    assert {"h1-maker-v1", "h2-flowguard-v1", "h3-flowguard-4arm-v1", "h4-edge-selective-v1"} <= {f["family"] for f in st["families"]}
    ids = [int(r["id"][1:]) for r in d["evidence"]["findings"]["rows"]]
    assert ids[:27] == list(range(1, 28))
    assert st["h3"]["why_not"].startswith("no twin result yet")
