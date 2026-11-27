"""Cohort absorption: refusal-aware routing, resumable runner, census filter and offline planner."""

import fcntl
import io
import urllib.error
import urllib.request

import orjson
import pytest

import doppel.lab.absorb as ab
import doppel.lab.absorb_cohort as ac
from doppel.lab.fillstore import compact
from doppel.lab.ordertape import ARCHIVE_REFUSED, SPAN_REFUSED, Pool, RpcError, http_transport, log_span, refusal_kind
from doppel.tape.segment import SegmentWriter
from tests.test_absorb import COND, NO, W, YES, FakeChain, _pages

W2 = "0x" + "cd" * 20  # a quiet cohort member
PUB, DRPC = "https://polygon-bor-rpc.publicnode.com", "https://polygon.drpc.org"
ARCHIVE_MSG = "Archive requests require a personal token"
SPAN_MSG = "eth_getLogs is limited to 100 block range on the free plan"


class Measured:
    """The Apex measurement of 2026-11-27: publicnode serves 10,000-block logs but no archive
    state; drpc serves archive state but only 100-block logs."""

    def __init__(self, chain):
        self.chain, self.calls = chain, []

    def __call__(self, url, method, params):
        self.calls.append((url, method, log_span(params) if method == "eth_getLogs" else None))
        if method == "eth_call" and url == PUB:
            raise RpcError(refusal_kind({"code": -32000, "message": ARCHIVE_MSG}))
        if method == "eth_getLogs":
            span = self.calls[-1][2]
            if url == DRPC and span > 100 or url == PUB and span > 10_000:
                raise RpcError(refusal_kind({"code": 35, "message": SPAN_MSG}))
        return self.chain(url, method, params)




@pytest.fixture
def world(tmp_path):
    c = FakeChain(head=400)
    c.open[(ab.PUSD, W, "cash")] = 100 * U_
    rows = [c.fill("0x01", 20, "BUY", NO, 10, 0.48), c.fill("0x02", 30, "BUY", YES, 10, 0.49)]
    c.single("0x03", 40, "0x" + "bb" * 20, W, YES, 5 * U_)  # a transfer: needs a receipt
    w = SegmentWriter(tmp_path / "tape", "goldsky_activity")
    w.append(orjson.dumps({"data": rows}).decode(), meta={"job": "cohort-x", "status": 200})
    w.close()
    compact(tmp_path / "tape", tmp_path / "store")
    pages = tmp_path / "pagination.jsonl"
    pages.write_bytes(orjson.dumps(_pages(W, 0, 5000, rows)[W, 0, 5000]) + b"\n")
    tokens = tmp_path / "tokens.jsonl"
    tokens.write_bytes(b"".join(orjson.dumps({"token": t, "condition": "0x" + COND, "idx": i}) + b"\n"
                                for i, t in enumerate((YES, NO))))
    cohort = tmp_path / "cohort.json"
    cohort.write_bytes(orjson.dumps({"sha256": "ab" * 32, "candidates": [W], "controls": [W2]}))
    return {"chain": c, "tmp": tmp_path, "pages": pages, "tokens": tokens, "cohort": cohort, "rows": rows}


U_ = ab.U


def _args(world, out, *extra):
    t = world["tmp"]
    return ["--store", str(t / "store"), "--cohort", str(world["cohort"]), "--from", str(1000 + 2 * 10),
            "--to", str(1000 + 2 * 101), "--out", str(out), "--pages", str(world["pages"]),
            "--tokens", str(world["tokens"]), "--rate", "1e9", *extra]


def _wide(world, out, *extra):
    """A 291-block window (blocks 10..300): wider than drpc's 100-block log limit."""
    args = _args(world, out, *extra)
    args[args.index("--to") + 1] = str(1000 + 2 * 301)
    return args


def _wallet(out, w=W):
    return orjson.loads((out / f"wallet-{w}.json").read_bytes())


# ---------------------------------------------------------------- refusals
def test_refusal_messages_are_classified_without_keeping_text():
    assert refusal_kind({"message": ARCHIVE_MSG}) == ARCHIVE_REFUSED
    assert refusal_kind({"message": SPAN_MSG}) == SPAN_REFUSED
    assert refusal_kind({"message": "ranges over 10000 blocks are not supported on freetier"}) == SPAN_REFUSED
    assert refusal_kind({"message": "Too Many Requests"}) == "rpc_error"
    assert refusal_kind(None) == "rpc_error"


@pytest.mark.parametrize("status", [200, 400])
def test_http_transport_surfaces_refusal_kinds(monkeypatch, status):
    body = orjson.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": ARCHIVE_MSG}})

    def urlopen(req, timeout):
        if status == 200:
            return io.BytesIO(body)
        raise urllib.error.HTTPError(req.full_url, status, "Bad Request", {}, io.BytesIO(body))  # type: ignore[arg-type]
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    with pytest.raises(RpcError) as e:
        http_transport("https://x", "eth_call", [])
    assert e.value.kind == ARCHIVE_REFUSED


def test_both_refusals_reroute_without_benching_and_reconcile(world, tmp_path):
    """With every method allowed on both nodes, each refusal moves the request to the other node."""
    net = Measured(world["chain"])
    out = tmp_path / "both"
    assert ac.main(_wide(world, out, "--rpc", PUB, DRPC), transport=net) == 0
    r = _wallet(out)
    assert r["status"] == "reconciled", r["causes"]
    stats = orjson.loads((out / "cohort.json").read_bytes())["summary"]["rpc"]
    assert stats[PUB]["benched"] == stats[DRPC]["benched"] == 0
    assert stats[PUB]["errors"].get(ARCHIVE_REFUSED, 0) == 1  # learned once, then never asked again
    assert sum(1 for url, m, _ in net.calls if m == "eth_call" and url == PUB) == 1
    assert stats[DRPC]["errors"].get(SPAN_REFUSED, 0) == 1  # one 291-block refusal, then publicnode
    assert {s for url, m, s in net.calls if m == "eth_getLogs" and url == PUB} == {291}


def test_default_routes_send_large_log_spans_and_archive_reads_to_their_nodes(world, tmp_path):
    net = Measured(world["chain"])
    out = tmp_path / "default"
    ac.main(_args(world, out), transport=net)
    assert _wallet(out)["status"] == "reconciled"
    assert {url for url, m, _ in net.calls if m == "eth_getLogs"} == {PUB}
    assert {url for url, m, _ in net.calls if m in ("eth_call", "eth_getCode")} == {DRPC}
    assert max(s for _, m, s in net.calls if m == "eth_getLogs") == 91  # whole window in one chunk


def test_log_only_on_a_100_block_node_backs_off_the_span(world, tmp_path):
    net = Measured(world["chain"])
    out = tmp_path / "drpc"
    assert ac.main(_wide(world, out, "--rpc", DRPC), transport=net) == 0
    r = _wallet(out)
    assert r["status"] == "reconciled" and r["completeness"]["chain_scan_complete"]
    spans = [s for _, m, s in net.calls if m == "eth_getLogs"]
    assert spans[:2] == [291, 145] and max(s for s in spans[2:]) <= 100  # halved until served
    stats = orjson.loads((out / "cohort.json").read_bytes())["summary"]["rpc"]
    assert stats[DRPC]["benched"] == 0


def test_archive_refusal_with_no_archive_node_names_the_cause_and_stops(world, tmp_path):
    net = Measured(world["chain"])
    out = tmp_path / "noarchive"
    assert ac.main(_args(world, out, "--state-rpc", PUB), transport=net) == 3
    attempt = orjson.loads((out / f"wallet-{W}.attempt.json").read_bytes())
    assert "state_archive_unavailable" in attempt["causes"]
    summary = orjson.loads((out / "cohort.json").read_bytes())["summary"]
    assert summary["stopped"] == ARCHIVE_REFUSED and summary["not_started"] == 1
    assert not (out / f"wallet-{W}.json").exists()
    # A corrected route resumes and completes from the wallet cache.
    assert ac.main(_args(world, out), transport=net) == 0
    assert _wallet(out)["status"] == "reconciled"


def test_pool_refusal_uses_no_attempt_and_raises_when_no_node_is_capable():
    calls = []

    def transport(url, method, params):
        calls.append(url)
        raise RpcError(ARCHIVE_REFUSED)
    pool = Pool(["a", "b"], transport, rate=1e9, attempts=1, sleep=lambda _: None)
    with pytest.raises(RpcError) as e:
        pool.call("eth_call", [])
    assert e.value.kind == ARCHIVE_REFUSED and sorted(calls) == ["a", "b"]
    with pytest.raises(RpcError):
        pool.call("eth_call", [])
    assert len(calls) == 2  # known incapable: not asked again
    assert all(ep.benched == 0 and ep.fails == 0 for ep in pool.eps)


# ---------------------------------------------------------------- runner
def test_runner_table_names_causes_and_never_refetches_completed_wallets(world, tmp_path):
    net = Measured(world["chain"])
    out = tmp_path / "run"
    assert ac.main(_args(world, out), transport=net) == 0
    doc = orjson.loads((out / "cohort.json").read_bytes())
    rows = {r["wallet"]: r for r in doc["rows"]}
    assert rows[W]["status"] == "reconciled" and rows[W]["state"] == "done"
    assert rows[W2]["status"] == "not_reconciled" and "goldsky_incomplete" in rows[W2]["causes"]
    assert doc["summary"]["causes"]["goldsky_incomplete"] == 1
    header = (out / "cohort.tsv").read_text().splitlines()[0].split("\t")
    assert header[:5] == ["wallet", "role", "state", "status", "causes"]
    n = len(net.calls)
    assert ac.main(_args(world, out), transport=net) == 0
    assert len(net.calls) == n  # nothing fetched again
    # A Goldsky re-pull for W2 changes its pagination record: recomputed from its cache, no requests.
    with world["pages"].open("ab") as fh:
        fh.write(orjson.dumps(_pages(W2, 0, 5000, [])[W2, 0, 5000]) + b"\n")
    assert ac.main(_args(world, out), transport=net) == 0
    assert len(net.calls) == n
    assert _wallet(out, W2)["status"] == "reconciled"
    lines = (out / "done.jsonl").read_bytes().count(b"\n")
    # A compaction (store state) or a token-map change recomputes every wallet, again from cache.
    for path in (world["tmp"] / "store" / "state.json", world["tokens"]):
        with path.open("ab") as fh:
            fh.write(b"\n")
        assert ac.main(_args(world, out), transport=net) == 0
        assert len(net.calls) == n
        lines, before = (out / "done.jsonl").read_bytes().count(b"\n"), lines
        assert lines == before + 2


def test_refusal_text_on_another_method_is_an_ordinary_error():
    def transport(url, method, params):
        raise RpcError(refusal_kind({"message": "exceeded: limited to 100 blocks range per request"}))
    pool = Pool(["a"], transport, rate=1e9, attempts=2, sleep=lambda _: None, backoff_s=0)
    with pytest.raises(RpcError) as e:
        pool.call("eth_call", [])
    assert e.value.kind == "rpc_error" and pool.eps[0].refused == set() and pool.eps[0].max_span is None
    assert refusal_kind({"message": "rate limited to 10 requests per second"}) == "rpc_error"


def test_budget_stop_resumes_to_the_same_ledger(world, tmp_path):
    net = Measured(world["chain"])
    full = tmp_path / "full"
    ac.main(_args(world, full), transport=net)
    total = len(net.calls)
    out = tmp_path / "budget"
    assert ac.main(_args(world, out, "--max-requests", str(total - 8)), transport=net) == 3
    summary = orjson.loads((out / "cohort.json").read_bytes())["summary"]
    assert summary["stopped"] == "BudgetExhausted" and summary["done"] < 2
    before = len(net.calls)
    assert ac.main(_args(world, out), transport=net) == 0
    assert len(net.calls) - before <= 8 + 2  # only what the budget cut off (plus the retried read)
    strip = lambda r: {k: v for k, v in r.items() if k != "completeness"}  # noqa: E731
    assert strip(_wallet(out)) == strip(_wallet(full))


def test_lock_refuses_a_concurrent_run_and_manifest_refuses_another_window(world, tmp_path):
    out = tmp_path / "locked"
    out.mkdir()
    with (out / ".lock").open("a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert ac.main(_args(world, out), transport=Measured(world["chain"])) == 4
    assert ac.main(_args(world, out), transport=Measured(world["chain"])) == 0
    other = _args(world, out)
    other[other.index("--to") + 1] = str(1000 + 2 * 150)
    assert ac.main(other, transport=Measured(world["chain"])) == 2


# ---------------------------------------------------------------- census v2
def test_census_filter_keeps_reconciled_wallets_and_recomputes_aggregates(world, tmp_path):
    out = tmp_path / "run"
    ac.main(_args(world, out), transport=Measured(world["chain"]))
    third = "0x" + "ef" * 20
    census = {"cohort_sha256": "ab" * 32, "wallets": {
        W: {"role": "cand", "eligible": True, "label": "COMPLETE_SET_MAKER", "window_economic_per_day": 5.0},
        W2: {"role": "ctrl", "eligible": True, "label": "MIXED", "window_economic_per_day": -1.0},
        third: {"role": "ctrl", "eligible": True, "label": "MIXED", "window_economic_per_day": 9.0}},
        "by_label": {"stale": {}}, "selfcheck": {"stale": True}}
    src = tmp_path / "census.json"
    src.write_bytes(orjson.dumps(census))
    dst = tmp_path / "census-absorbed.json"
    assert ac.census_main(["--census", str(src), "--absorb", str(out), "--out", str(dst)]) == 0
    res = orjson.loads(dst.read_bytes())
    assert list(res["wallets"]) == [W]
    assert res["absorption_filter"]["excluded"] == {"not_reconciled": 1, "no_absorption": 1}
    assert res["by_label"] == {"cand:COMPLETE_SET_MAKER": {"wallets": 1, "per_day": 5.0, "positive": 1}}
    split = res["wallets"][W]["absorption"]
    assert split["complete_set_buy_shares"] == 10 and split["directional_buy_shares"] == 10
    assert split["complete_set_share_of_buys"] == 0.5
    assert orjson.loads(src.read_bytes()) == census  # input untouched
    # A wallet file the current table does not list as done (stale after a budget stop) is ignored.
    doc = orjson.loads((out / "cohort.json").read_bytes())
    for row in doc["rows"]:
        row["state"] = "pending" if row["wallet"] == W else row["state"]
    (out / "cohort.json").write_bytes(orjson.dumps(doc))
    dst2 = tmp_path / "census-stale.json"
    assert ac.census_main(["--census", str(src), "--absorb", str(out), "--out", str(dst2)]) == 0
    assert orjson.loads(dst2.read_bytes())["wallets"] == {}
    src.write_bytes(orjson.dumps({**census, "cohort_sha256": "00" * 32}))
    assert ac.census_main(["--census", str(src), "--absorb", str(out), "--out", str(tmp_path / "x.json")]) == 2


# ---------------------------------------------------------------- planner
def test_plan_makes_no_request_and_prices_the_fixed_end_repull(world, tmp_path, capsys):
    def no_network(*_):
        raise AssertionError("the planner must not touch the network")
    out = tmp_path / "plan"
    assert ac.main(_args(world, out, "--plan"), transport=no_network) == 0
    p = orjson.loads(capsys.readouterr().out)
    assert p["wallets"] == p["wallets_to_absorb"] == 2
    assert p["blocks"] == 91 and p["blocks_source"].startswith("nominal")
    assert p["rpc"]["per_wallet_fixed"] == 5 * 1 + 6 + 1
    assert p["rpc"]["store_fills_in_window"] == 2
    assert p["goldsky"]["covered"] == 1 and p["goldsky"]["missing"] == 1
    assert p["goldsky"]["command_pulls"] == 2  # neither wallet holds the exact (from, to) record
    cmd = p["goldsky_command"]
    assert cmd[cmd.index("--end") + 1] == str(1000 + 2 * 101)
    assert p["goldsky_days_exact"] is True
    assert not (out / "run.json").exists()
    # A pre-binding terminal record covers the window but is not usable: re-attest, zero rows.
    legacy = {**_pages(W2, 0, 5000, [])[W2, 0, 5000], "trade_binding": None}
    with world["pages"].open("ab") as fh:
        fh.write(orjson.dumps(legacy) + b"\n")
    assert ac.main(_args(world, out, "--plan"), transport=no_network) == 0
    g = orjson.loads(capsys.readouterr().out)["goldsky"]
    assert (g["covered"], g["needs_reattestation"], g["missing"], g["rows_est"]) == (1, 1, 0, 0)
