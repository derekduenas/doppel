"""Reconstruction pipeline end to end on a synthetic tape, fill store and chain (no network)."""

import hashlib

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import doppel.lab.ordertape as ot
from doppel.lab import ordermatch, orderreplay, profile, reconstruct
from doppel.lab.fillstore import compact
from doppel.lab.reconstruct import GATES, Cfg, chunk_list, main, run
from doppel.tape.segment import SegmentWriter

T0 = 1795629600  # hour-aligned
MS = T0 * 1000
EX = "0xe111180000d2663c0091e4f400237545b87b996b"
W1, W2, W3, TAKER = "0x" + "a1" * 20, "0x" + "b2" * 20, "0x" + "d3" * 20, "0x" + "cc" * 20
BLOCK = 97_000_000


# ---------------------------------------------------------------- a matchOrders encoder
def _w(x: int) -> bytes:
    return x.to_bytes(32, "big")


def static(salt, maker, token, cents, size, created_ms) -> bytes:
    """The 11 static words of a BUY order of ``size`` shares at ``cents``."""
    return b"".join(_w(v) for v in (salt, int(maker, 16), int(maker, 16), int(token), size * cents * 10**4,
                                    size * 10**6, 0, 0, created_ms, 0, 0))


def calldata(taker: bytes, makers: list[bytes], fills: list[int]) -> str:
    n = len(makers)
    mo = 7 * 32 + len(taker)
    arr = _w(n) + b"".join(_w(n * 32 + i * 11 * 32) for i in range(n)) + b"".join(makers)
    ints = _w(n) + b"".join(_w(f) for f in fills)
    head = b"".join(_w(v) for v in (7, 7 * 32, mo, 1, mo + len(arr), 0, mo + len(arr) + len(ints)))
    return "0x" + ot.SELECTOR + (head + taker + arr + ints + _w(n) + _w(0) * n).hex()


def test_the_encoder_round_trips_through_decode_match():
    m = static(5, W1, "101", 23, 97, MS + 1000)
    rows = ot.decode_match(calldata(static(1, TAKER, "101", 90, 10, MS), [m], [343506]), EX)
    assert rows is not None and [r["role"] for r in rows] == ["taker", "maker"]
    r = rows[1]
    assert (r["maker"], r["token_id"], r["side"], r["size"], round(r["limit"], 6), r["created_ms"]) == (
        W1, "101", "BUY", 97.0, 0.23, MS + 1000)
    assert (r["fill_amount"], r["order_id"]) == (343506, ot.order_hash(m, EX))


# ---------------------------------------------------------------- the synthetic world
def book(ms, asset, market, bids, asks):
    lv = lambda xs: [{"price": str(p), "size": str(s)} for p, s in xs]  # noqa: E731
    return ms, {"event_type": "book", "asset_id": asset, "market": market, "bids": lv(bids),
                "asks": lv(asks), "timestamp": str(MS + ms)}


def pc(ms, asset, market, price, size):
    return ms, {"event_type": "price_change", "market": market, "timestamp": str(MS + ms),
                "price_changes": [{"asset_id": asset, "price": str(price), "size": str(size), "side": "BUY"}]}


def trade(ms, asset, market, price, size, tx):
    return ms, {"event_type": "last_trade_price", "asset_id": asset, "market": market, "price": str(price),
                "size": str(size), "side": "SELL", "timestamp": str(MS + ms), "transaction_hash": tx}


def world(root):
    """35 W1 orders (32 in hour 0 on two markets, 3 in hour 1), each alone at its price: 31 fully
    filled by one print, 4 filled 20 of 50 then cancelled. 2 W2 orders whose fill-store order_id
    is wrong. 2 verified W3 orders of 100 shares (too few for a fingerprint). Unfilled 50-share
    bids in an active token (candidate) and in a control token, and an unfilled 100-share bid."""
    frames, fills, chain = [], [], {}
    k = 0

    def an_order(wallet, ms, token, market, cents, partial=False, oid=None, size=50):
        nonlocal k
        k += 1
        price, tx, got = cents / 100, f"0x{k:064x}", 20 if partial else size
        st = static(1000 + k, wallet, token, cents, size, MS + ms)
        chain[tx] = (calldata(static(k, TAKER, token, 90, size, MS + ms), [st], [got * cents * 10**4]), BLOCK + k)
        fills.append({"id": f"r{k}", "address": wallet, "event_type": "TRADE", "token_id": token, "side": "BUY",
                      "amount_shares": float(got), "amount_usdc": got * price, "fee": 0.0,
                      "block_timestamp": T0 + (ms + 400) // 1000 + 1, "block_number": BLOCK + k,
                      "order_type": "maker", "price": price, "order_id": oid or "0x" + ot.order_hash(st, EX),
                      "transaction_hash": tx})
        frames.extend([pc(ms + 100, token, market, price, size), trade(ms + 400, token, market, price, got, tx),
                       pc(ms + 400, token, market, price, size - got)])
        if partial:
            frames.append(pc(ms + 700, token, market, price, 0))

    for h in (0, 3_600_000):  # a snapshot at the start of each hour: a chunk starts with an unknown book
        for a, m in (("101", "m1"), ("102", "m1"), ("201", "m2"), ("202", "m2"), ("301", "m3")):
            frames.append(book(h, a, m, [(0.02, 10)], [(0.98, 10)]))
    for i in range(32):
        an_order(W1, 1000 + 1000 * i, "101" if i % 2 == 0 else "201", "m1" if i % 2 == 0 else "m2",
                 10 + i, partial=i % 8 == 7)
    for i in range(3):
        an_order(W1, 3_600_000 + 1000 + 1000 * i, "101", "m1", 10 + i)
    for i in range(2):
        an_order(W2, 40_000 + 1000 * i, "101", "m1", 60 + i, oid="0x" + "00" * 32)
    for i in range(2):
        an_order(W3, 44_000 + 1000 * i, "201", "m2", 70 + i, size=100)
    frames += [pc(54_000, "201", "m2", 0.06, 100), pc(54_300, "201", "m2", 0.06, 0)]  # W3's size, unfilled
    frames += [pc(50_000, "101", "m1", 0.05, 50), pc(50_300, "101", "m1", 0.05, 0),   # candidate
               pc(52_000, "301", "m3", 0.05, 50), pc(52_300, "301", "m3", 0.05, 0)]   # control
    for h in (0, 3_600_000):  # keep every asset's book known past the placebo windows
        frames += [pc(h + 200_000, a, m, 0.01, 1) for a, m in (("101", "m1"), ("201", "m2"), ("301", "m3"))]
    w = SegmentWriter(root / "tape", "clob_btc_5m")
    for ms, m in sorted(frames, key=lambda f: f[0]):
        w.append(orjson.dumps([m]).decode(), tw=(MS + ms) * 1_000_000)
    w.close()
    g = SegmentWriter(root / "tape", "goldsky_activity")
    g.append(orjson.dumps({"data": fills}).decode(), tw=T0 * 10**9,
             meta={"job": "cohort-a", "status": 200, "rows": len(fills)})
    g.close()
    compact(root / "tape", root / "lab" / "fills")
    (root / "cohort.json").write_bytes(orjson.dumps({"candidates": [W1, W3], "controls": [W2], "sha256": "ab" * 32}))
    return chain


class Chain:
    def __init__(self, txs):
        self.txs, self.calls = txs, 0

    def __call__(self, url, method, params):
        self.calls += 1
        data, bn = self.txs[params[0]]
        return {"input": data, "to": EX, "blockNumber": hex(bn)}


def cfg(root, **kw) -> Cfg:
    out = kw.pop("out", root / "lab" / "reconstruct" / "run1")
    return Cfg(**({"data": root / "tape", "store": root / "lab" / "fills", "cohort": root / "cohort.json",
                   "t_from": T0, "t_to": T0 + 7200, "out": out, "orders": out / "ordertape",
                   "wallets": (W1, W2, W3), "roles": {W1: "cand", W2: "ctrl", W3: "cand"}} | kw))


@pytest.fixture(scope="module")
def ran(tmp_path_factory):
    root = tmp_path_factory.mktemp("recon")
    chain = Chain(world(root))
    return root, chain, run(cfg(root), chain)


def test_chunks_are_cut_at_whole_hours_and_do_not_overlap():
    assert [(a - T0, b - T0) for _, a, b in chunk_list(T0 + 600, T0 + 7300, 3600)] == [
        (600, 3600), (3600, 7200), (7200, 7300)]
    assert len({c[0] for c in chunk_list(T0 + 600, T0 + 7300, 3600)}) == 3


def test_end_to_end_all_four_steps(ran):
    root, chain, man = ran
    out = root / "lab" / "reconstruct" / "run1"
    assert chain.calls == 39  # one request per tx: the store gave block time and number
    assert {s: man["steps"][s]["status"] for s in reconstruct.STEPS} == dict.fromkeys(reconstruct.STEPS, "ran")
    ot_step = man["steps"]["ordertape"]
    assert ot_step["complete"] and ot_step["outputs"]["orders"]["rows"] == 78  # taker + maker per tx
    assert [c["status"] for c in man["steps"]["ordermatch"]["chunks"]] == ["ran", "ran"]

    # the manifest traces the run
    assert man["git_commit"] and man["git_commit_source"] in ("git rev-parse", "DEPLOYED_COMMIT")
    assert man["cohort"]["sha256"] == hashlib.sha256((root / "cohort.json").read_bytes()).hexdigest()
    assert man["cohort"]["declared_sha256"] == "ab" * 32 and man["range"]["to"] - man["range"]["from"] == 7200
    assert set(man["code_sha256"]["files"]) >= {"lab/ordertape.py", "lab/ordermatch.py", "lab/orderreplay.py",
                                                "lab/profile.py", "lab/reconstruct.py", "twin/engine.py"}
    ch = man["steps"]["ordermatch"]["chunks"][0]
    seg = next(iter(ch["inputs"]["tape"]))
    assert ch["inputs"]["tape"][seg] == hashlib.sha256((root / "tape" / seg).read_bytes()).hexdigest()
    f = out / "chunks" / ch["chunk"] / "ordermatch" / "orders.jsonl"
    assert ch["outputs"]["orders.jsonl"] == {"sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
                                             "bytes": f.stat().st_size, "rows": 34}
    wf = out / "wallets" / W1 / "orders.jsonl"
    assert man["wallets"][W1]["outputs"][f"wallets/{W1}/orders.jsonl"] == {
        "sha256": hashlib.sha256(wf.read_bytes()).hexdigest(), "bytes": wf.stat().st_size, "rows": 35}
    assert set(man["wallets"][W1]["outputs"]) == {
        "orders", f"wallets/{W1}/orders.jsonl", f"wallets/{W1}/orderreplay.jsonl",
        f"profile/{W1}.ledger.json", f"profile/{W1}.ledger.md"}
    assert man["gate_table"]["provisional"] and man["gate_table"]["gates"] == GATES

    # W1: every gate passes at the default thresholds
    g = man["wallets"][W1]["gates"]
    assert {k: v["status"] for k, v in g.items()} == dict.fromkeys(GATES, "pass")
    assert (g["ordertape.verified_share"]["value"], g["ordertape.verified_share"]["n"]) == (1.0, 35)
    assert (g["ordermatch.match_rate"]["value"], g["ordermatch.placebo_rate"]["value"]) == (1.0, 0.0)
    assert (g["orderreplay.routed_share"]["value"], g["orderreplay.delivered_share"]["value"]) == (1.0, 1.0)
    assert g["orderreplay.placement_not_found_share"]["value"] == 0.0
    assert g["orderreplay.bracket_violation_share"]["value"] == 0.0
    assert man["wallets"][W1]["untrusted"] == {s: [] for s in reconstruct.STEPS}

    # merged per-wallet book match: sums over the two chunks
    om = orjson.loads((out / "ordermatch" / "ordermatch.json").read_bytes())["wallets"][W1]
    assert (om["maker_orders"], om["observable"], om["matched"], om["placebo_matched"]) == (35, 35, 35, 0)
    assert om["outcomes"] == {"filled:chain": 31, "cancelled:exact_decrease": 4} and len(om["chunks"]) == 2
    assert om["rest_ms_known"]["filled"] == {"p10": 300.0, "p50": 300.0, "p90": 300.0, "n": 31}
    assert om["rest_ms_known"]["cancelled"]["p50"] == 600.0 and om["sign_to_book_ms"]["p50"] == 100.0
    fp = om["fingerprint"]
    assert fp["sizes"] == [50.0] and fp["sizes_stable"]
    # W2's two 50-share orders are unverified, so they are not "other_known": they count as candidates.
    # own is 32, not 35: W1's 3 orders in hour 1 are below the fingerprint floor of that chunk
    assert (fp["total"]["own"], fp["total"]["other_known"], fp["total"]["candidates"], fp["total"]["control"]) == (
        32, 0, 3, 1)
    assert list(fp["sizes_by_chunk"]) == om["chunks"][:1]
    assert fp["candidate_rest_ms_cancelled"] == {"p10": 300.0, "p50": 300.0, "p90": 300.0, "n": 1}
    assert sum(1 for _ in open(out / "wallets" / W1 / "orders.jsonl")) == 35

    # merged twin replay: alone at the level, so both queue modes reproduce the real fills
    rp = orjson.loads((out / "orderreplay" / "orderreplay.json").read_bytes())
    for m in orderreplay.MODES:
        d = rp["wallets"][W1]["conditional_on_real_fill"][m]
        assert (d["n"], d["markets"], d["real_shares"], d["twin_shares"], d["ratio"]) == (35, 2, 1630.0, 1630.0, 1.0)
        assert d["ratio_ci95"] == [1.0, 1.0]
        assert rp["cohort"]["conditional_on_real_fill"][m]["ratio"] == 1.0
        assert rp["wallets"][W1]["never_filled_quotes_estimate"][m]["n"] == 1
    assert rp["wallets"][W1]["counts"]["placement_not_found"] == 0
    assert rp["cohort"]["adapter_gate"]["routed_share"] == 1.0

    # ledgers, cohort table and report
    led = orjson.loads((out / "profile" / f"{W1}.ledger.json").read_bytes())
    assert led["fingerprint"]["n_maker_orders"] == 35 and led["input"]["unflagged_input"] is False
    assert len(orjson.loads((out / "profile" / "cohort.json").read_bytes())["wallets"]) == 3
    rep = orjson.loads((out / "report.json").read_bytes())
    r1 = next(r for r in rep["wallets"] if r["wallet"] == W1)
    assert (r1["role"], r1["verified_maker_orders"], r1["match"]["match_rate"]) == ("cand", 35, 1.0)
    assert r1["twin"]["pessimistic"]["ratio"] == 1.0 and r1["footprint"]["chance_share"] is not None
    assert r1["footprint"]["identity_precision_upper"] is None  # not measured in a cohort run
    assert rep["cohort"]["trusted_wallets"] == dict.fromkeys(reconstruct.STEPS, 1) | {"ordertape": 2}
    assert set(rep["ledger_status"]) == set(profile.WEIGHTS)
    md = (out / "report.md").read_text()
    assert W1 in md and "PROVISIONAL" in md and "## Verified / estimated / unknown" in md


def test_a_failing_gate_marks_downstream_untrusted(ran):
    root, _, man = ran
    # W2's decoded orders do not hash to the store's order_id: no verified row, nothing downstream
    w2 = man["wallets"][W2]
    g = w2["gates"]["ordertape.verified_share"]
    assert (g["value"], g["n"], g["status"]) == (0.0, 2, "fail")
    assert w2["gates"]["ordermatch.match_rate"]["status"] == "not_evaluable"
    assert all("ordertape.verified_share:fail" in w2["untrusted"][s] for s in reconstruct.STEPS)
    assert man["untrusted_wallets"]["profile"] == [W2, W3]
    assert "UNTRUSTED" in (root / "lab" / "reconstruct" / "run1" / "report.md").read_text()
    # a failing ordermatch gate leaves the order tape trusted and marks what reads the match
    strict = GATES | {"ordermatch.match_rate": GATES["ordermatch.match_rate"] | {"min": 1.01}}
    u = run(cfg(root), lambda *a: 1 / 0, strict)["wallets"][W1]["untrusted"]
    assert u["ordertape"] == []
    assert u["ordermatch"] == u["orderreplay"] == u["profile"] == ["ordermatch.match_rate:fail"]
    run(cfg(root), lambda *a: 1 / 0)  # put the default-table manifest and report back


def test_second_run_does_no_work(ran, monkeypatch):
    root, chain, man = ran

    def boom(*a, **k):
        raise AssertionError("resume must not rerun this")

    for mod in (ot, ordermatch, orderreplay, profile):
        monkeypatch.setattr(mod, "run", boom)
    calls = chain.calls
    again = run(cfg(root), boom)
    assert chain.calls == calls
    assert {s: again["steps"][s]["status"] for s in reconstruct.STEPS} == dict.fromkeys(reconstruct.STEPS, "skipped")
    for s in ("ordermatch", "orderreplay"):
        assert [c["status"] for c in again["steps"][s]["chunks"]] == ["skipped", "skipped"]
        assert [c["key"] for c in again["steps"][s]["chunks"]] == [c["key"] for c in man["steps"][s]["chunks"]]
    assert again["wallets"] == man["wallets"]


def test_force_reruns_only_that_step(ran, monkeypatch):
    root, chain, _ = ran
    seen = []
    real = orderreplay.run
    monkeypatch.setattr(orderreplay, "run", lambda *a, **k: seen.append(a[2]) or real(*a, **k))
    monkeypatch.setattr(ordermatch, "run", lambda *a, **k: 1 / 0)
    again = run(cfg(root, force=frozenset({"orderreplay"})), chain)
    assert seen == [T0, T0 + 3600] and again["steps"]["ordermatch"]["status"] == "skipped"
    assert again["steps"]["orderreplay"]["status"] == "ran" and again["steps"]["profile"]["status"] == "skipped"


def test_one_chunk_and_two_chunks_give_the_same_counts(ran):
    """Same order tape (reused, no fetch), run as a single 2 h chunk through the CLI."""
    root, _, _ = ran
    out = root / "lab" / "reconstruct" / "one"
    rc = main(["--data", str(root / "tape"), "--lab", str(root / "lab"), "--cohort", str(root / "cohort.json"),
               "--from", str(T0), "--to", str(T0 + 7200), "--chunk-hours", "2", "--out", str(out),
               "--orders", str(root / "lab" / "reconstruct" / "run1" / "ordertape"),
               "--steps", "ordermatch,orderreplay,profile"])
    assert rc == 0
    man = orjson.loads((out / "manifest.json").read_bytes())
    assert man["steps"]["ordertape"]["status"] == "kept" and len(man["steps"]["ordermatch"]["chunks"]) == 1
    a, b = (orjson.loads((root / "lab" / "reconstruct" / r / "ordermatch" / "ordermatch.json").read_bytes())
            ["wallets"][W1] for r in ("run1", "one"))
    for k in ("maker_orders", "observable", "matched", "placebo_matched", "outcomes", "fills", "rest_ms_known",
              "sign_to_book_ms"):
        assert a[k] == b[k], k
    ta, tb = a["fingerprint"]["total"], b["fingerprint"]["total"]
    # the fingerprint floor is per chunk: hour 1 alone has 3 orders (no fingerprint), the 2 h chunk has 35
    assert (ta["own"], tb["own"]) == (32, 35)
    assert all(ta[k] == tb[k] for k in ("other_known", "candidates", "own_unmatched", "control"))
    # token-hours are NOT the same: a known span is [first book, last frame] inside one replay, so
    # the quiet stretch across the hour edge counts only when both hours are replayed together
    assert a["fingerprint"]["active_token_hours"] < b["fingerprint"]["active_token_hours"]
    assert man["wallets"][W1]["untrusted"] == {s: [] for s in reconstruct.STEPS}


def test_a_thin_wallet_gets_no_fingerprint_and_reads_no_gate(ran):
    """W3: 2 verified orders. Its sizes must not become a fingerprint, and its cells are NO GATE
    (nothing failed; nothing could be evaluated)."""
    root, _, man = ran
    out = root / "lab" / "reconstruct" / "run1"
    ch = man["steps"]["ordermatch"]["chunks"][0]
    assert ch["counts"]["fingerprints_dropped"] == 1
    cd = out / "chunks" / ch["chunk"] / "ordermatch"
    raw = orjson.loads((cd / "ordermatch.json").read_bytes())["wallets"][W3]
    assert raw["fingerprint"] is None and "fewer than 30" in raw["fingerprint_dropped"]
    assert {orjson.loads(line)["wallet"] for line in open(cd / "quotes.jsonl", "rb")} == {W1}
    om = orjson.loads((out / "ordermatch" / "ordermatch.json").read_bytes())["wallets"][W3]
    assert (om["maker_orders"], om["matched"], om["fingerprint"]) == (2, 2, None)
    w3 = man["wallets"][W3]
    assert w3["gates"]["ordertape.verified_share"]["status"] == "pass" and w3["untrusted"]["ordertape"] == []
    assert w3["untrusted"]["ordermatch"] == ["ordermatch.match_rate:not_evaluable",
                                             "ordermatch.placebo_rate:not_evaluable"]
    row = next(line for line in (out / "report.md").read_text().splitlines() if line.startswith(f"| {W3}"))
    assert "NO GATE" in row and "UNTRUSTED" not in row


def test_a_kept_output_made_from_other_code_is_stale_and_untrusted(ran, monkeypatch):
    """--steps profile after ordermatch's code changed: its kept chunks no longer match their key."""
    root, _, _ = ran
    real = reconstruct.code_sha
    monkeypatch.setattr(reconstruct, "code_sha", lambda: real() | {"lab/ordermatch.py": "changed"})
    man = run(cfg(root, out=root / "lab" / "reconstruct" / "one", chunk_s=7200, steps=("profile",),
                  orders=root / "lab" / "reconstruct" / "run1" / "ordertape"), lambda *a: 1 / 0)
    assert man["steps"]["ordermatch"]["status"] == "kept" and man["steps"]["ordermatch"]["stale"]
    u = man["wallets"][W1]["untrusted"]
    assert u["ordertape"] == [] and u["ordermatch"] == ["ordermatch:stale"]
    assert "ordermatch:stale" in u["orderreplay"] and "ordermatch:stale" in u["profile"]


# ---------------------------------------------------------------- parquet input to ordermatch
def _prow(i, oid, verified, role="maker", maker=W1, block_ts=T0 + 5, created=MS + 1000, fill=1_000_000):
    return {"tx": f"0x{i:02x}", "block": 1, "block_ts": block_ts, "role": role, "order_id": oid, "maker": maker,
            "signer": maker, "token_id": "101", "side": "BUY", "size": 50.0, "limit": 0.2,
            "maker_amount": 10_000_000, "taker_amount": 50_000_000, "fill_amount": fill, "fee_amount": 0,
            "sig_type": 0, "created_ms": created, "age_s": block_ts - created / 1000, "ts_flag": None,
            "verified": verified, "builder": "", "salt": "1", "condition_id": "", "n_makers": 1}


def test_ordermatch_reads_the_parquet_directory(tmp_path):
    d = tmp_path / "orders"
    # written newest first, as a cohort run does: order "a" filled at T0+5 (age 4 s) and again at T0+700
    ot._write_parts(d, 1, [_prow(1, "a", True, block_ts=T0 + 700, fill=2_000_000),
                           _prow(2, "x", False, maker=TAKER),            # another maker: not verified
                           _prow(3, "n", None)])                          # store had no order_id
    ot._write_parts(d, 2, [_prow(4, "a", True), _prow(5, "t", True, role="taker"),
                           _prow(6, "late", True, created=MS + 4_000_000, block_ts=T0 + 4001)])
    orders, active, stats = ordermatch.load_orders(d, T0, T0 + 3600)
    assert [o.order_id for o in orders] == ["a"] and active == {W1: {"101"}}
    assert orders[0].fills == {"0x04": (5.0, T0 + 5), "0x01": (10.0, T0 + 700)}  # oldest fill screened first
    assert dict(stats) == {"rows_not_verified": 2, "rows_out_of_range": 1, "maker_orders": 1}
    # the same verified rows as JSONL give the same orders
    rows = [r for p in sorted(d.glob("day=*/*.parquet")) for r in pq.read_table(p).to_pylist() if r["verified"]]
    jl = tmp_path / "orders.jsonl"
    jl.write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in sorted(rows, key=lambda r: r["block_ts"])))
    assert ordermatch.load_orders(jl, T0, T0 + 3600)[0] == orders
    # a directory without the column is read whole
    old = tmp_path / "old" / "day=2026-11-25"
    old.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([{k: v for k, v in _prow(1, "x", False).items() if k != "verified"}]),
                   old / "part-000001.parquet")
    assert [o.order_id for o in ordermatch.load_orders(tmp_path / "old", T0, T0 + 3600)[0]] == ["x"]
