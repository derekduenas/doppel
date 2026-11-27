"""Paper runtime (E1): a synthetic live tape is appended while the runtime tails it."""

from __future__ import annotations

import ast
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import orjson
import pytest

from doppel.lab.ordermatch import load_orders
from doppel.paper import footprint, journal
from doppel.paper.agree import agree
from doppel.paper.cli import main as paper_main
from doppel.paper.journal import Journal, JournalError, strip
from doppel.paper.runtime import PaperRuntime, RecoveryError, Settings, open_orders
from doppel.paper.sim import PaperSim, record_windows
from doppel.strategy.genome import Genome, KillRule, MakerRule, Risk, Source, Universe, to_dict
from doppel.tape.segment import SegmentWriter
from doppel.tape.universe import pmus_slug
from doppel.twin.engine import SimConfig, run_windows
from doppel.twin.features import RefStore
from doppel.twin.tape import NS, iter_windows

T0 = 1795046400
S = T0 + 3600            # first window starts on an hour boundary; the tail starts in the hour before
MS = 1_000_000
STREAMS = ("pmus_ws_5m", "ref_brti_live", "ref_prices")


def maker(**kw) -> Genome:
    risk = kw.pop("risk", Risk(max_inventory=30, max_notional_per_window=100, max_loss_per_day=25))
    return Genome(name="paper-m", mode="maker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
                  maker=MakerRule(quote_ref="fair", half_spread_c=2.0, size=10.0, sides="both",
                                  requote_threshold_c=1.0, max_quote_age_s=30),
                  risk=risk, kill=KillRule(min_windows=30), created_tw=0, **kw)


def _md(slug, bid, ask, vol):
    return {"requestId": "md-5m-x-1", "marketData": {
        "marketSlug": slug, "bids": [{"px": {"value": f"{bid:.4f}"}, "qty": "100.0000"}],
        "offers": [{"px": {"value": f"{ask:.4f}"}, "qty": "100.0000"}], "state": "MARKET_STATE_OPEN",
        "stats": {"sharesTraded": f"{vol:.4f}"}}}


def _tr(slug, px, qty, side, tid, t_ns):
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_ns // NS)) + f".{t_ns % NS:09d}Z"
    return {"requestId": "tr-5m-x-2", "trade": {
        "marketSlug": slug, "price": {"value": f"{px:.4f}"}, "quantity": {"value": f"{qty:.4f}"},
        "tradeTime": iso, "taker": {"side": side}, "id": tid, "state": "TRADE_STATE_NEW"}}


def market(n_windows=2, seed=7, jump_at=None):
    """Deterministic tape records (stream, tw, raw, meta), receive-time ordered. No late prints, no
    unprinted volume, one boundary value per second: the twin's non-causal inputs are avoided, so
    paper and twin must agree exactly (docs/PAPER.md)."""
    rng = np.random.default_rng(seed)
    lo, hi = S - 900, S + 300 * n_windows + 30
    v = 90000.0
    brti = {}
    for t in range(lo - 60, hi):
        v += float(rng.normal(0, 4))
        brti[t] = round(v, 2)
    recs = []
    for t in range(lo, hi):   # live poller: second t received at t + 0.4 s
        recs.append(("ref_brti_live", t * NS + 400 * MS, "", {"new": [[t * 1000, str(brti[t])]]}))
    for t in range(lo, hi):
        if (t + 1) % 300 == 0:
            avg = sum(brti[s] for s in range(t - 59, t + 1)) / 60
            recs.append(("ref_prices", (t + 1) * NS + 200 * MS,
                         orjson.dumps({"series": [{"candles": [{"timestamp": t, "close": avg}]}]}).decode(),
                         {"asset": "crypto:btc", "status": 200, "from": t, "to": t}))
    recs.append(("pmus_ws_5m", (S - 400) * NS, "", {"ctl": "connect", "url": "wss://x"}))
    vol = {}
    n = 0
    for w in range(n_windows):
        st = S + 300 * w
        slug = pmus_slug("btc", "5m", st)
        vol[slug] = 0.0
        for s in range(st - 50, st + 305):
            bid, ask = 0.40, 0.60
            if jump_at is not None and s >= jump_at:
                bid, ask = 0.30, 0.50
            recs.append(("pmus_ws_5m", s * NS + 5 * MS, orjson.dumps(_md(slug, bid, ask, vol[slug])).decode(), None))
            if st + 5 <= s < st + 295 and rng.random() < 0.25:
                side = "ORDER_SIDE_SELL" if rng.random() < 0.5 else "ORDER_SIDE_BUY"
                px = 0.42 if side == "ORDER_SIDE_SELL" else 0.58
                qty = float(rng.integers(1, 8))
                n += 1
                te = s * NS + 490 * MS
                recs.append(("pmus_ws_5m", te + 10 * MS,
                             orjson.dumps(_tr(slug, px, qty, side, f"T{n}", te)).decode(), None))
                vol[slug] += qty
    recs.sort(key=lambda r: (r[1], STREAMS.index(r[0])))
    return recs


class Tape:
    """Writers kept open across appends, flushed explicitly (as the recorder's frames)."""

    def __init__(self, root: Path, recs):
        self.root, self.recs, self.i = root, recs, 0
        self.w = {s: SegmentWriter(root, s) for s in STREAMS}

    def until(self, t_ns: int) -> None:
        while self.i < len(self.recs) and self.recs[self.i][1] <= t_ns:
            s, tw, raw, meta = self.recs[self.i]
            self.w[s].append(raw, tw=tw, meta=meta)
            self.i += 1
        for w in self.w.values():
            w.flush()

    def close(self):
        for w in self.w.values():
            w.close()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


SET = Settings(grace_s=0.0, checkpoint_s=60, heartbeat_s=30)


def run_paper(tmp_path, g, recs, step_s=7, end=None, settings=SET, jdir=None, data=None, cfg=SimConfig()):
    data = data or tmp_path / "data"
    tape = Tape(data, recs)
    clock = Clock(0)
    jdir = jdir or tmp_path / "journal"
    rt = PaperRuntime(g, data, jdir, cfg, settings, clock=clock)
    end = end or recs[-1][1] // NS + 1
    t = S - 700
    clock.t = t * NS
    tape.until(clock.t)
    rt.start(S)
    while t < end:
        t += step_s
        clock.t = t * NS
        tape.until(clock.t)
        rt.cycle()
    tape.close()
    return rt, jdir


def kinds(recs, k, slug=None):
    return [r for r in recs if r["k"] == k and (slug is None or r.get("slug") == slug)]


def twin_records(data, g, t_from, t_to, cfg=SimConfig()):
    store = RefStore(data)
    store.load_boundaries("btc", t_from - 1, t_to)
    items = [(wt, store.window_ref(wt.asset, wt.start, wt.end, g.fair.vol_window_s))
             for wt in iter_windows(data, ("btc",), ("5m",), t_from, t_to)]
    out: list = []
    res = record_windows(g, items, cfg, emit=out.append)
    return items, res, out


# ---------------------------------------------------------------- journal
def test_journal_chain_detects_tampering(tmp_path):
    j = Journal(tmp_path)
    j.open()
    j.append([{"k": "run", "t": 0}])
    j.append([{"k": "fill", "t": 1, "qty": 10.0}, {"k": "cycle", "t": 1}])
    assert len(journal.load(tmp_path)) == 3
    p = tmp_path / journal.FILE
    good = p.read_bytes()
    p.write_bytes(good.replace(b'"qty":10.0', b'"qty":11.0'))
    with pytest.raises(JournalError, match="line 1"):
        journal.load(tmp_path)
    lines = good.split(b"\n")
    p.write_bytes(b"\n".join([lines[0], lines[2], lines[1], b""]))         # reordered
    with pytest.raises(JournalError):
        journal.load(tmp_path)
    p.write_bytes(b"\n".join([lines[0], lines[2], b""]))                   # deleted line
    with pytest.raises(JournalError):
        journal.load(tmp_path)
    assert paper_main(["verify", "--journal", str(tmp_path)]) == 1


def test_journal_cuts_uncommitted_tail_with_a_trace(tmp_path):
    j = Journal(tmp_path)
    j.open()
    j.append([{"k": "run", "t": 0}, {"k": "cycle", "t": 0}])
    j.append([{"k": "fill", "t": 1}])                       # batch without its commit record
    with open(tmp_path / journal.FILE, "ab") as fh:
        fh.write(b'{"k":"fil')                               # torn write
    recs = Journal(tmp_path).open()
    assert [r["k"] for r in recs] == ["run", "cycle", "recovery"]
    assert recs[-1]["dropped_lines"] == 1 and recs[-1]["torn_bytes"] == 9
    assert len(journal.load(tmp_path)) == 3


# ---------------------------------------------------------------- engine parity
@pytest.mark.parametrize("opening", ["published", "received_raw"])
def test_record_windows_reproduces_run_windows(tmp_path, opening):
    g = maker()
    tape = Tape(tmp_path / "data", market(3))
    tape.until(10**20)
    tape.close()
    cfg = SimConfig(opening=opening)
    items, res, recs = twin_records(tmp_path / "data", g, S, S + 900, cfg)
    base = run_windows(g, items, cfg)
    assert res == base
    assert sum(r["n_fills"] for r in res) > 0 and len(kinds(recs, "fill")) == sum(r["n_fills"] for r in res)


# ---------------------------------------------------------------- the paper run
@pytest.mark.parametrize("opening", ["published", "received_raw"])
def test_paper_vs_twin_agreement_is_total_on_deterministic_tape(tmp_path, opening):
    g = maker()
    rt, jdir = run_paper(tmp_path, g, market(2), cfg=SimConfig(opening=opening))
    recs = journal.load(jdir)
    settled = kinds(recs, "settle")
    assert [r["slug"] for r in settled] == [pmus_slug("btc", "5m", S), pmus_slug("btc", "5m", S + 300)]
    assert sum(r["n_fills"] for r in settled) > 0
    assert not kinds(recs, "late")
    doc = agree(jdir, tmp_path / "data")
    assert doc["windows_compared"] == 2 and doc["windows_identical"] == 2
    assert doc["inputs_identical_share"] == 1.0 and doc["intents_identical_share"] == 1.0
    assert doc["fills_identical_share"] == 1.0 and doc["net_diff_usd"] == 0.0
    assert doc["same_input_diff_intent"] == 0 and doc["paper_only"] == {}
    assert paper_main(["agree", "--journal", str(jdir), "--data", str(tmp_path / "data")]) == 0


def test_chunking_does_not_change_the_journal(tmp_path):
    g = maker()
    _, j1 = run_paper(tmp_path / "a", g, market(2), step_s=2)
    _, j2 = run_paper(tmp_path / "b", g, market(2), step_s=23)
    assert by_window(j1) == by_window(j2)


def by_window(jdir):
    """Settled windows' records in a canonical order (emission order across windows is per cycle)."""
    recs = journal.load(jdir)
    done = {r["slug"] for r in kinds(recs, "settle")}
    out = [strip(r) for r in recs if r.get("slug") in done]
    return sorted(out, key=lambda r: (r["slug"], r["t"], orjson.dumps(r, option=orjson.OPT_SORT_KEYS)))


def test_decisions_use_only_data_received_by_decision_time(tmp_path):
    """A book received at J + 0.005 s moves the market; decision J must not see it even though the
    record is already on disk when J is decided."""
    g = maker()
    J = S + 100
    recs = market(1, jump_at=J)
    data = tmp_path / "data"
    tape = Tape(data, recs)
    clock = Clock((S - 700) * NS)
    tape.until(clock.t)
    rt = PaperRuntime(g, data, tmp_path / "j", SimConfig(), SET, clock=clock)
    rt.start(S)
    tape.until((J + 30) * NS)          # everything to J + 30 s is on disk
    clock.t = J * NS                   # ... but the runtime's watermark is J
    while rt.cycle() < J * NS:
        pass
    tape.close()
    sim = rt.st.windows[pmus_slug("btc", "5m", S)].sim
    ds = [r["d"] for r in kinds(rt.journal.records, "decide")]
    assert max(ds) == J
    assert sim.feats.last["mid_up_c"] == pytest.approx(50.0)     # the vector decision J used
    assert sim.feats.last["tau_s"] == 100
    clock.t = (J + 1) * NS
    rt.cycle()
    assert sim.feats.last["tau_s"] == 101 and sim.feats.last["mid_up_c"] == pytest.approx(40.0)


def test_late_record_is_journaled_never_used_retroactively(tmp_path):
    g = maker()
    data = tmp_path / "data"
    recs = market(1)
    tape = Tape(data, recs)
    clock = Clock((S - 700) * NS)
    tape.until(clock.t)
    rt = PaperRuntime(g, data, tmp_path / "j", SimConfig(), SET, clock=clock)
    rt.start(S)
    clock.t = (S + 60) * NS
    tape.until(clock.t)
    while rt.cycle() < clock.t:
        pass
    n_dec = len(kinds(rt.journal.records, "decide"))
    tape.w["pmus_ws_5m"].append(orjson.dumps(_md(pmus_slug("btc", "5m", S), 0.10, 0.90, 0.0)).decode(),
                                tw=(S + 30) * NS + 1)
    tape.until((S + 61) * NS)
    clock.t = (S + 61) * NS
    rt.cycle()
    tape.close()
    late = kinds(rt.journal.records, "late")
    assert len(late) == 1 and late[0]["tw"] == (S + 30) * NS + 1 and late[0]["t"] > (S + 60) * NS
    old = [r for r in kinds(rt.journal.records, "decide") if r["d"] <= S + 60]
    assert len(old) == n_dec              # nothing re-decided


def test_risk_limit_stops_quoting_the_side(tmp_path):
    g = maker(risk=Risk(max_inventory=10, max_notional_per_window=100, max_loss_per_day=25))
    _, jdir = run_paper(tmp_path, g, market(1), end=S + 320)
    recs = journal.load(jdir)
    fills = kinds(recs, "fill")
    first_full = next(r for r in fills if r["inv"] >= 10 - 1e-9)
    after = [r for r in kinds(recs, "intent") if r["t"] > first_full["t"] and r.get("side") == "bid"]
    at_limit = [r for r in kinds(recs, "decide") if r["inv"] >= 10 - 1e-9]
    assert len(at_limit) >= 3                      # the limit was reached and held for a while
    assert not [r for r in after if r["kind"] in ("post", "amend") and _inv_at(recs, r["t"]) >= 10 - 1e-9]
    assert max(abs(r["inv"]) for r in fills) <= 10 + 1e-9


def _inv_at(recs, t):
    inv = 0.0
    for r in kinds(recs, "fill"):
        if r["t"] <= t:
            inv = r["inv"]
    return inv


def test_kill_switch_halts_within_one_cycle(tmp_path):
    g = maker()
    data = tmp_path / "data"
    tape = Tape(data, market(2))
    clock = Clock((S - 700) * NS)
    tape.until(clock.t)
    jdir = tmp_path / "j"
    rt = PaperRuntime(g, data, jdir, SimConfig(), SET, clock=clock)
    rt.start(S)
    for t in range(S - 690, S + 120, 5):
        clock.t = t * NS
        tape.until(clock.t)
        rt.cycle()
    assert kinds(rt.journal.records, "intent")
    (jdir / "KILL").touch()
    kill_cursor = rt.st.cursor
    for t in range(S + 125, S + 700, 5):
        clock.t = t * NS
        tape.until(clock.t)
        rt.cycle()
    tape.close()
    recs = rt.journal.records
    halt = kinds(recs, "risk")[0]
    assert halt["event"] == "halt_all" and halt["why"] == "kill_switch" and halt["t"] == kill_cursor + 1
    later = [r for r in kinds(recs, "intent") if r["t"] >= halt["t"]]
    assert later and all(r["kind"] == "cancel" and r["why"] == "kill_switch" for r in later)
    w = pmus_slug("btc", "5m", S)
    assert open_orders(recs, w) == {}
    assert all(r.get("why") == ["kill_switch"] for r in kinds(recs, "skip", pmus_slug("btc", "5m", S + 300)))


def test_max_loss_stop_halts_everything(tmp_path):
    g = maker()
    _, jdir = run_paper(tmp_path, g, market(2), settings=Settings(grace_s=0.0, max_loss_usd=0.5))
    recs = journal.load(jdir)
    ev = [r for r in kinds(recs, "risk") if r["event"] == "max_loss"]
    assert ev and ev[0]["worst_case_net_usd"] <= -0.5
    t = ev[0]["t"]
    assert not [r for r in kinds(recs, "intent") if r["t"] > t and r["kind"] != "cancel"]


@pytest.mark.parametrize("ckpt", [60, 10**6])   # from a checkpoint, and full replay with none
def test_restart_recovers_and_continues(tmp_path, ckpt):
    g = maker()
    st = Settings(grace_s=0.0, checkpoint_s=ckpt, heartbeat_s=30)
    _, ref = run_paper(tmp_path / "one", g, market(2), settings=st)
    data = tmp_path / "data"
    recs = market(2)
    # first life: stop mid-window 2 (open orders, inventory), as if killed
    tape = Tape(data, recs)
    clock = Clock((S - 700) * NS)
    tape.until(clock.t)
    jdir = tmp_path / "j"
    rt = PaperRuntime(g, data, jdir, SimConfig(), st, clock=clock)
    rt.start(S)
    t = S - 700
    while t < S + 450:
        t += 7
        clock.t = t * NS
        tape.until(clock.t)
        rt.cycle()
    mid = rt.st.windows[pmus_slug("btc", "5m", S + 300)]
    inv_before = mid.sim.inv
    del rt
    # second life: same journal, recovered from it, then the rest of the tape
    rt2 = PaperRuntime(g, data, jdir, SimConfig(), st, clock=clock)
    rt2.start(None)
    resume = kinds(rt2.journal.records, "resume")
    assert resume and pmus_slug("btc", "5m", S + 300) in resume[0]["open_windows"]
    assert rt2.st.windows[pmus_slug("btc", "5m", S + 300)].sim.inv == inv_before
    end = recs[-1][1] // NS + 1
    while t < end:
        t += 7
        clock.t = t * NS
        tape.until(clock.t)
        rt2.cycle()
    tape.close()
    assert by_window(ref) == by_window(jdir) and kinds(journal.load(jdir), "settle")


def test_restart_refuses_a_tape_that_changed(tmp_path):
    g = maker()
    st = Settings(grace_s=0.0, checkpoint_s=10**6, heartbeat_s=30)
    _, jdir = run_paper(tmp_path, g, market(1), settings=st, end=S + 200)
    # the tape under the journal is replaced by a different market
    import shutil
    shutil.rmtree(tmp_path / "data")
    tape = Tape(tmp_path / "data", market(1, seed=99))
    tape.until(10**20)
    tape.close()
    rt = PaperRuntime(g, tmp_path / "data", jdir, SimConfig(), st, clock=Clock((S + 200) * NS))
    with pytest.raises(RecoveryError):
        rt.start(None)
    assert kinds(journal.load(jdir), "risk")[-1]["event"] == "recovery_mismatch"


def test_restart_refuses_other_settings_or_genome(tmp_path):
    g = maker()
    _, jdir = run_paper(tmp_path, g, market(1), end=S - 200)
    with pytest.raises(RecoveryError):
        PaperRuntime(g, tmp_path / "data", jdir, SimConfig(latency_ms=100), SET).start(None)
    g2 = maker(risk=Risk(max_inventory=5))
    with pytest.raises(RecoveryError):
        PaperRuntime(g2, tmp_path / "data", jdir, SimConfig(), SET).start(None)


# ---------------------------------------------------------------- footprint
def test_journal_to_footprint_round_trips(tmp_path):
    g = maker()
    _, jdir = run_paper(tmp_path, g, market(2))
    recs = journal.load(jdir)
    out = tmp_path / "fp"
    doc = footprint.convert(jdir, out)
    wallet = doc["wallet"]
    orders = footprint.orders_from_journal(recs)
    filled = [o for o in orders if o["fills"]]
    assert doc["orders"] == len(orders) > len(filled) > 0      # unfilled orders exist only in truth/
    got = footprint.fills_from_public(out / "public" / "orders.jsonl", wallet)
    want = []
    for o in filled:
        token, limit = footprint.token_view(o["slug"], o["yes_side"], o["px"])
        for f in o["fills"]:
            want.append((footprint._h("order", wallet, o["oid"]), token, limit, round(f["qty"], 6),
                         -(-f["t_ns"] // NS) + 1))
    assert got == sorted(want)
    # journal fills == footprint fills, contract for contract
    assert sum(x[3] for x in got) == pytest.approx(sum(r["qty"] for r in kinds(recs, "fill")))
    mk, _, stats = load_orders(out / "public" / "orders.jsonl", S - 600, S + 1200)
    n_maker = sum(1 for o in filled if o["kind"] == "maker")
    assert len(mk) == n_maker and stats["maker_orders"] == n_maker
    by_id = {footprint._h("order", wallet, o["oid"]): o for o in filled}
    for o in mk:
        assert sum(s for s, _ in o.fills.values()) == pytest.approx(sum(f["qty"] for f in by_id[o.order_id]["fills"]))
    truth = [orjson.loads(x) for x in (out / "truth" / "decisions.jsonl").read_bytes().splitlines()]
    assert len(truth) == len([r for r in recs if r["k"] != "cycle"])
    assert (out / "public" / "fills").is_dir()


# ---------------------------------------------------------------- no orders, structurally
BANNED = ("aiohttp", "websockets", "requests", "urllib", "http", "socket", "ssl", "cryptography", "Crypto",
          "doppel.tape.sources", "doppel.lab.ordertape", "doppel.lab.goldsky")


def test_paper_package_imports_no_network_or_signing_code():
    src = Path(__file__).resolve().parents[1] / "src" / "doppel" / "paper"
    for f in src.glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = ["doppel." + mod.lstrip(".") if node.level == 2 else mod]
            for n in names:
                assert not any(n == b or n.startswith(b + ".") for b in BANNED), f"{f.name} imports {n}"
    # Transitively, the frozen twin imports lab.refprice (a REST poller) and so aiohttp/websockets are
    # loaded but never called; no signing or trading client may be loaded at all.
    code = ("import sys, doppel.paper.cli, doppel.paper.runtime, doppel.paper.agree, doppel.paper.footprint;"
            "bad=[m for m in ('cryptography','Crypto.Signature','eth_account','web3','py_clob_client','nacl',"
            "'doppel.lab.ordertape','doppel.lab.goldsky') if m in sys.modules]; print(bad); sys.exit(1 if bad else 0)")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_refuses_venues_and_features_paper_cannot_run(tmp_path):
    from doppel.strategy.genome import Condition
    g = Genome(name="k", mode="maker", universe=Universe(venue="kalshi"), source=Source(kind="manual"),
               maker=MakerRule(), kill=KillRule(min_windows=30), created_tw=0)
    with pytest.raises(ValueError, match="pmus"):
        PaperRuntime(g, tmp_path, tmp_path / "j")
    g = maker()
    g = Genome(**{**g.__dict__, "maker": MakerRule(pause_when=(Condition("intl_flow_up_usd_5s", ">", 1.0),))})
    with pytest.raises(ValueError, match="intl_"):
        PaperRuntime(g, tmp_path, tmp_path / "j")


def test_cli_run_until_then_resume(tmp_path):
    g = maker()
    data = tmp_path / "data"
    tape = Tape(data, market(1))
    tape.until(10**20)
    tape.close()
    gp = tmp_path / "g.json"
    gp.write_bytes(orjson.dumps(to_dict(g)))
    jdir = tmp_path / "j"
    base = ["run", "--genome", str(gp), "--data", str(data), "--journal", str(jdir), "--grace-s", "0",
            "--poll-s", "0"]
    assert paper_main(base + ["--from", str(S), "--until", str(S + 400)]) == 0
    recs = journal.load(jdir)
    assert kinds(recs, "settle")
    assert paper_main(base + ["--until", str(S + 400)]) == 0      # resumes the same journal
    assert kinds(journal.load(jdir), "resume")
