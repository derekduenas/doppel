"""Reconstruction pipeline end to end on a synthetic tape, fill store and chain (no network)."""

import hashlib
from dataclasses import replace

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


def static(salt, maker, token, cents, size, created_ms, sell=False) -> bytes:
    """Encode a signed maker or taker order."""
    cash, shares = size * cents * 10**4, size * 10**6
    give, take = (shares, cash) if sell else (cash, shares)
    return b"".join(_w(v) for v in (salt, int(maker, 16), int(maker, 16), int(token), give,
                                    take, int(sell), 0, created_ms, 0, 0))


def calldata(taker: bytes, makers: list[bytes], fills: list[int]) -> str:
    n = len(makers)
    mo = 7 * 32 + len(taker)
    arr = _w(n) + b"".join(_w(n * 32 + i * 11 * 32) for i in range(n)) + b"".join(makers)
    ints = _w(n) + b"".join(_w(f) for f in fills)
    condition = int.from_bytes(taker[3 * 32:4 * 32], "big") // 100
    head = b"".join(_w(v) for v in (condition, 7 * 32, mo, 1, mo + len(arr), 0, mo + len(arr) + len(ints)))
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
    return ms, {"event_type": "book", "asset_id": asset, "market": f"0x{int(market[1:]):064x}", "bids": lv(bids),
                "asks": lv(asks), "timestamp": str(MS + ms)}


def pc(ms, asset, market, price, size):
    return ms, {"event_type": "price_change", "market": f"0x{int(market[1:]):064x}", "timestamp": str(MS + ms),
                "price_changes": [{"asset_id": asset, "price": str(price), "size": str(size), "side": "BUY"}]}


def trade(ms, asset, market, price, size, tx):
    return ms, {"event_type": "last_trade_price", "asset_id": asset, "market": f"0x{int(market[1:]):064x}", "price": str(price),
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
        chain[tx] = (calldata(static(k, TAKER, token, 1, size, MS + ms, sell=True), [st], [got * cents * 10**4]), BLOCK + k)
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
    f = out / ch["dir"] / "orders.jsonl"
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
    assert om["outcomes"] == {"filled:chain": 31, "unknown:level_zero": 4} and len(om["chunks"]) == 2
    assert om["rest_ms_known"]["filled"] == {"p10": 300.0, "p50": 300.0, "p90": 300.0, "n": 31}
    assert om["rest_ms_known"]["cancelled"] is None and om["rest_not_known"] == 4
    assert om["diagnostic_rows"] == 0 and om["sign_to_book_ms"]["p50"] == 100.0
    fp = om["fingerprint"]
    assert fp["sizes"] == [50.0] and fp["sizes_stable"]
    # W2's two 50-share orders are unverified, so they are not "other_known": they count as candidates.
    # own is 32, not 35: W1's 3 orders in hour 1 are below the fingerprint floor of that chunk
    assert (fp["total"]["own"], fp["total"]["other_known"], fp["total"]["candidates"], fp["total"]["control"]) == (
        32, 0, 3, 1)
    assert list(fp["sizes_by_chunk"]) == om["chunks"][:1]
    assert fp["candidate_rest_ms_cancelled"] is None
    assert sum(1 for _ in open(out / "wallets" / W1 / "orders.jsonl")) == 35

    # merged twin replay: alone at the level, so both queue modes reproduce the real fills
    rp = orjson.loads((out / "orderreplay" / "orderreplay.json").read_bytes())
    for m in orderreplay.MODES:
        d = rp["wallets"][W1]["conditional_on_real_fill"][m]
        assert (d["n"], d["markets"], d["real_shares"], d["twin_shares"], d["ratio"]) == (31, 2, 1550.0, 1550.0, 1.0)
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


def test_manifest_connects_reconstruction_to_rulefit_and_fidelity(ran, tmp_path):
    from doppel.lab import fidelity, rulefit
    from doppel.lab.research_manifest import read_reconstruction
    from doppel.strategy.genome import Genome, MakerRule, Source, Universe, to_dict

    root, _, _ = ran
    manifest = root / "lab" / "reconstruct" / "run1" / "manifest.json"
    files = read_reconstruction(manifest, W1, T0, T0 + 7200, exact=True)
    assert files.matched_orders.is_file() and files.summary.is_file() and files.orders.is_dir()
    # A perfectly checksummed old summary can still contain legacy placement claims.
    archive = tmp_path / "legacy"
    legacy = orjson.loads(manifest.read_bytes())
    old_summary = orjson.loads(files.summary.read_bytes())
    del old_summary["params"]["placement_evidence_version"]
    name = "ordermatch/ordermatch.json"
    (archive / name).parent.mkdir(parents=True)
    (archive / name).write_bytes(orjson.dumps(old_summary))
    legacy["steps"]["ordermatch"]["outputs"][name]["sha256"] = reconstruct.sha_file(archive / name)
    (archive / "wallets").symlink_to(manifest.parent / "wallets", target_is_directory=True)
    (archive / "ordertape").symlink_to(files.orders, target_is_directory=True)
    (archive / "manifest.json").write_bytes(orjson.dumps(legacy))
    with pytest.raises(ValueError, match="regenerate legacy reconstruction"):
        read_reconstruction(archive / "manifest.json", W1, T0, T0 + 7200)
    with pytest.raises(ValueError, match="untrusted"):
        read_reconstruction(manifest, W2, T0, T0 + 7200)
    with pytest.raises(ValueError, match="exact"):
        read_reconstruction(manifest, W1, T0, T0 + 3600, exact=True)
    # Older manifests could set the trust flags when only a subset of the wallet's
    # range was fetched. A perfect verified share among those rows is insufficient.
    old = orjson.loads(manifest.read_bytes())
    old.pop("status", None)
    old["wallets"][W1]["tape"]["fills_in_range"] += 1
    copy = tmp_path / "partial.json"
    copy.write_bytes(orjson.dumps(old))
    with pytest.raises(ValueError, match="incomplete"):
        read_reconstruction(copy, W1, T0, T0 + 7200)
    copy.write_bytes(orjson.dumps({"status": "running"}))
    with pytest.raises(ValueError, match="unfinished"):
        read_reconstruction(copy, W1, T0, T0 + 7200)

    tokens = tmp_path / "tokens.jsonl"
    tokens.write_bytes(b"".join(
        orjson.dumps({"token": token, "slug": f"btc-updown-5m-{T0}", "idx": idx,
                      "condition": f"0x{1:064x}", "status": "resolved", "payout": 1 - idx}) + b"\n"
        for idx, token in enumerate(("101", "102"))
    ))
    args = ["--data", str(root / "tape"), "--wallet", W1, "--reconstruct-manifest", str(manifest),
            "--from", str(T0), "--to", str(T0 + 7200)]
    fit = tmp_path / "fit"
    assert rulefit.main([*args, "--tokens", str(tokens), "--out", str(fit)]) == 1
    assert (fit / "fit_report.json").is_file()  # two observed markets are insufficient for a draft

    g = Genome(name="manifest test", universe=Universe(venue="intl_sim"), mode="maker",
               source=Source(kind="clone"), maker=MakerRule(), created_tw=0)
    genome = tmp_path / "genome.json"
    genome.write_bytes(orjson.dumps(to_dict(g)))
    out = tmp_path / "fidelity"
    assert fidelity.main([*args, "--lab", str(tmp_path), "--genome", str(genome), "--out", str(out)]) == 0
    result = orjson.loads((out / "fidelity.json").read_bytes())
    assert result["verdict"]["decision"] == "INCONCLUSIVE"
    assert result["coverage"]["verified_holdout"]["reason"] == "no coverage receipt"


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
    cd = out / ch["dir"]
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
    newest = ot._write_parts(d, 1, [
        _prow(1, "a", True, block_ts=T0 + 700, fill=2_000_000),
        _prow(2, "x", False, maker=TAKER),  # another maker: not verified
        _prow(3, "n", None),                # store had no order_id
    ])
    oldest = ot._write_parts(d, 2, [
        _prow(4, "a", True), _prow(5, "t", True, role="taker"),
        _prow(6, "late", True, created=MS + 4_000_000, block_ts=T0 + 4001),
    ])
    legacy, _, diag = ordermatch.load_orders(d, T0, T0 + 3600)
    assert [(o.order_id, o.verified) for o in legacy] == [("a", False)]
    assert diag["legacy_diagnostic_rows"] == 2
    ot._commit(d, 1, newest, dict.fromkeys(("0x01", "0x02", "0x03"), "decoded"))
    ot._commit(d, 2, oldest, dict.fromkeys(("0x04", "0x05", "0x06"), "decoded"))
    orders, active, stats = ordermatch.load_orders(d, T0, T0 + 3600)
    assert [o.order_id for o in orders] == ["a"] and active == {W1: {"101"}}
    assert orders[0].verified
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


# ---------------------------------------------------------------- review regressions (2026-11-26)
def _write_world(root, frames, fills, candidates):
    w = SegmentWriter(root / "tape", "clob_btc_5m")
    for i, (ms, m) in enumerate(sorted(frames, key=lambda f: f[0])):
        w.append(orjson.dumps([m]).decode(), tw=(MS + ms) * 1_000_000)
        if i % 100 == 99:
            w.flush()  # small zstd frames, as the recorder writes them (one per second)
    w.close()
    g = SegmentWriter(root / "tape", "goldsky_activity")
    g.append(orjson.dumps({"data": fills}).decode(), tw=T0 * 10**9,
             meta={"job": "cohort-a", "status": 200, "rows": len(fills)})
    g.close()
    compact(root / "tape", root / "lab" / "fills")
    (root / "cohort.json").write_bytes(orjson.dumps({"candidates": candidates, "controls": [], "sha256": "ab" * 32}))


def world2(root, shared_tx=False, n_cand=1):
    """W1: 32 orders in EACH hour and ``n_cand`` cancelled 50-share candidates in each hour.
    shared_tx: W3 has a 100-share maker order in each of W1's hour-0 txs."""
    frames, fills, chain = [], [], {}
    k = 0

    def an_order(wallet, ms, cents, extra=None):
        nonlocal k
        k += 1
        tx = f"0x{k:064x}"
        makers = [(wallet, static(1000 + k, wallet, "101", cents, 50, MS + ms), cents, 50)]
        if extra:
            makers.append((extra[0], static(5000 + k, extra[0], "101", extra[1], 100, MS + ms), extra[1], 100))
        chain[tx] = (calldata(static(k, TAKER, "101", 1, 50, MS + ms, sell=True), [m[1] for m in makers],
                              [z * ct * 10**4 for _, _, ct, z in makers]), BLOCK + k)
        for j, (w, st, ct, z) in enumerate(makers):
            fills.append({"id": f"r{k}_{j}", "address": w, "event_type": "TRADE", "token_id": "101", "side": "BUY",
                          "amount_shares": float(z), "amount_usdc": z * ct / 100, "fee": 0.0,
                          "block_timestamp": T0 + (ms + 400) // 1000 + 1, "block_number": BLOCK + k,
                          "order_type": "maker", "price": ct / 100, "order_id": "0x" + ot.order_hash(st, EX),
                          "transaction_hash": tx})
            frames.extend([pc(ms + 100, "101", "m1", ct / 100, z), trade(ms + 400, "101", "m1", ct / 100, z, tx),
                           pc(ms + 400, "101", "m1", ct / 100, 0)])

    for h in (0, 3_600_000):
        for a, m in (("101", "m1"), ("102", "m1"), ("301", "m3")):
            frames.append(book(h, a, m, [(0.02, 10)], [(0.98, 10)]))
        for i in range(32):
            an_order(W1, h + 1000 + 1000 * i, 10 + i, extra=(W3, 50 + i) if shared_tx and h == 0 else None)
        for j in range(n_cand):
            frames += [pc(h + 50_000 + 100 * j, "101", "m1", 0.05, 50), pc(h + 50_050 + 100 * j, "101", "m1", 0.05, 0)]
        frames += [pc(h + 52_000, "301", "m3", 0.05, 50), pc(h + 52_300, "301", "m3", 0.05, 0)]
        frames += [pc(h + 500_000, a, m, 0.01, 1) for a, m in (("101", "m1"), ("301", "m3"))]
    _write_world(root, frames, fills, [W1, W3])
    return chain


def one(root, **kw) -> Cfg:
    return cfg(root, **({"wallets": (W1,), "roles": {W1: "cand"}} | kw))


def _replay(root, run_="run1"):
    return orjson.loads((root / "lab" / "reconstruct" / run_ / "orderreplay" / "orderreplay.json").read_bytes())


def _match(root, run_="run1"):
    return orjson.loads((root / "lab" / "reconstruct" / run_ / "ordermatch" / "ordermatch.json").read_bytes())


def test_m1_never_filled_counts_merge_over_two_chunks_with_candidates(tmp_path):
    """Both chunks have a cancelled candidate: the merge used to raise TypeError on by_twin_source."""
    a, b = tmp_path / "a", tmp_path / "b"
    run(one(a), Chain(world2(a)))
    run(one(a), lambda *x: 1 / 0)  # and the resume does not crash either
    run(one(b, chunk_s=7200), Chain(world2(b)))
    for root, chunks in ((a, 2), (b, 1)):
        rp = _replay(root)
        assert len(rp["params"]["chunks"]) == chunks
        for doc in (rp["cohort"], rp["wallets"][W1]):
            nf = doc["never_filled_quotes_estimate"]
            for m in orderreplay.MODES:
                assert (nf[m]["n"], nf[m]["twin_filled"], nf[m]["quoted_shares"], nf[m]["by_twin_source"]) == (
                    2, 0, 100.0, {})
    assert (a / "lab" / "reconstruct" / "run1" / "manifest.json").exists()


def test_m1_by_twin_source_survives_the_merge():
    t = {m: {"n": 3, "twin_filled": 2, "quoted_shares": 150.0, "phantom_shares": 20.0, "src:trade": 2}
         for m in orderreplay.MODES}
    nf = orderreplay.nf_doc(reconstruct._sum_tallies([t, t, {}]))
    assert nf is not None and nf["pessimistic"] == {
        "n": 6, "twin_filled": 4, "phantom_fill_rate": 0.66667, "quoted_shares": 300.0, "phantom_shares": 40.0,
        "by_twin_source": {"trade": 4}}
    assert orderreplay.nf_doc(reconstruct._sum_tallies([{}])) is None


def world3(root):
    """32 W1 orders early in hour 0, and 5 placed late in hour 0 whose fill (print and block) is in hour 1."""
    frames, fills, chain = [], [], {}
    k = 0

    def an_order(ms, cents, fill_delay=300):
        nonlocal k
        k += 1
        tx = f"0x{k:064x}"
        st = static(1000 + k, W1, "101", cents, 50, MS + ms)
        chain[tx] = (calldata(static(k, TAKER, "101", 1, 50, MS + ms, sell=True), [st], [50 * cents * 10**4]), BLOCK + k)
        fills.append({"id": f"r{k}", "address": W1, "event_type": "TRADE", "token_id": "101", "side": "BUY",
                      "amount_shares": 50.0, "amount_usdc": 50 * cents / 100, "fee": 0.0,
                      "block_timestamp": T0 + (ms + 100 + fill_delay) // 1000 + 1, "block_number": BLOCK + k,
                      "order_type": "maker", "price": cents / 100, "order_id": "0x" + ot.order_hash(st, EX),
                      "transaction_hash": tx})
        frames.extend([pc(ms + 100, "101", "m1", cents / 100, 50),
                       trade(ms + 100 + fill_delay, "101", "m1", cents / 100, 50, tx),
                       pc(ms + 100 + fill_delay, "101", "m1", cents / 100, 0)])

    for h in (0, 3_600_000):
        for a in ("101", "102"):
            bids = [(0.02, 10)]
            if h and a == "101":
                # These quotes rest across the chunk boundary and execute later in hour 1.
                bids += [(0.01, 1)] + [(cents / 100, 50) for cents in range(50, 55)]
            frames.append(book(h + (5 if h else 0), a, "m1", bids, [(0.98, 10)]))
    for i in range(32):
        an_order(1000 + 1000 * i, 10 + i)
    for i in range(5):
        an_order(3_400_000 + 20_000 * i, 50 + i, fill_delay=400_000)
    frames += [pc(3_599_990, "101", "m1", 0.01, 1), pc(3_600_000 + 500_000, "101", "m1", 0.01, 2)]
    _write_world(root, frames, fills, [W1])
    return chain


def test_m2_an_order_filled_after_its_chunk_is_censored_not_a_twin_miss(tmp_path):
    res = {}
    for cs in (3600, 7200):
        root = tmp_path / str(cs)
        man = run(one(root, chunk_s=cs), Chain(world3(root)))
        out = root / "lab" / "reconstruct" / "run1"
        late = [orjson.loads(line) for line in open(out / "wallets" / W1 / "orderreplay.jsonl", "rb")][-5:]
        d = _replay(root)["wallets"][W1]
        res[cs] = ([r["cut"] for r in late], {m: (d["conditional_on_real_fill"][m]["n"],
                                                  d["conditional_on_real_fill"][m]["ratio"],
                                                  d["conditional_on_real_fill"][m]["missed"]) for m in orderreplay.MODES},
                   man["wallets"][W1]["gates"]["orderreplay.bracket_violation_share"]["status"])
    # two 1 h chunks: the 5 late orders are cut at the chunk end and stay out of the headline (was ratio 0.8649)
    assert res[3600] == (["chunk_end"] * 5, dict.fromkeys(orderreplay.MODES, (32, 1.0, 0)), "pass")
    assert res[7200] == ([None] * 5, dict.fromkeys(orderreplay.MODES, (37, 1.0, 0)), "pass")
    unit = orjson.loads((tmp_path / "3600" / "lab" / "reconstruct" / "run1" / "manifest.json").read_bytes())[
        "steps"]["orderreplay"]["chunks"][0]
    assert unit["counts"]["cut_chunk_end"] == 5 and unit["counts"]["counts"]["cut_short"] == {"chunk_end": 5}


def _cand_world(root, n):
    """One hour: 32 W1 orders (a 50-share fingerprint) and ``n`` cancelled 50-share candidates."""
    frames, fills, chain = [], [], {}
    for i in range(32):
        k, ms, cents = i + 1, 1000 + 1000 * i, 10 + i
        tx, st = f"0x{k:064x}", static(1000 + k, W1, "101", cents, 50, MS + ms)
        chain[tx] = (calldata(static(k, TAKER, "101", 1, 50, MS + ms, sell=True), [st], [50 * cents * 10**4]), BLOCK + k)
        fills.append({"id": f"r{k}", "address": W1, "event_type": "TRADE", "token_id": "101", "side": "BUY",
                      "amount_shares": 50.0, "amount_usdc": 50 * cents / 100, "fee": 0.0,
                      "block_timestamp": T0 + (ms + 400) // 1000 + 1, "block_number": BLOCK + k,
                      "order_type": "maker", "price": cents / 100, "order_id": "0x" + ot.order_hash(st, EX),
                      "transaction_hash": tx})
        frames += [pc(ms + 100, "101", "m1", cents / 100, 50), trade(ms + 400, "101", "m1", cents / 100, 50, tx),
                   pc(ms + 400, "101", "m1", cents / 100, 0)]
    frames += [book(0, a, "m1", [(0.02, 10)], [(0.98, 10)]) for a in ("101", "102")]
    for j in range(n):
        frames += [pc(100_000 + 100 * j, "101", "m1", 0.05, 50), pc(100_050 + 100 * j, "101", "m1", 0.05, 0)]
    frames.append(pc(3_000_000, "101", "m1", 0.01, 1))
    _write_world(root, frames, fills, [W1])
    return chain


def test_m3_candidate_rows_are_not_held_in_memory(tmp_path):
    """10x the candidates must cost far less than 10x candidate rows: per extra candidate the
    traced peak of ordermatch + orderreplay grows by its two level events, not by a row dict
    (~1.6 kB), a Spec with two QuoteSims (~3 kB) and a result row (~2 kB) as before."""
    import tracemalloc

    peak, rows = {}, {}
    for n in (300, 3000):
        root = tmp_path / str(n)
        chain = Chain(_cand_world(root, n))
        run(one(root, t_to=T0 + 3600, steps=("ordertape",)), chain)
        tracemalloc.start()
        man = run(one(root, t_to=T0 + 3600, steps=("ordermatch", "orderreplay")), chain)
        peak[n] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        unit = man["steps"]["ordermatch"]["chunks"][0]
        rows[n] = unit["outputs"]["quotes.jsonl"]["rows"]
        assert man["steps"]["orderreplay"]["chunks"][0]["peak_rss_mb"] > 0
        nf = _replay(root)["wallets"][W1]["never_filled_quotes_estimate"]
        assert nf["pessimistic"]["n"] == n and _match(root)["wallets"][W1]["fingerprint"]["total"]["candidates"] == n
    assert rows == {300: 300, 3000: 3000}  # every candidate is on disk
    # measured 2026-11-26: ~720 B (two LevelEvents and their index entries in ordermatch's tape; the
    # replay's pending tuple is ~520 B and comes after). The old replay path alone was ~6,500 B.
    per_candidate = (peak[3000] - peak[300]) / 2700
    assert per_candidate < 1000, per_candidate


def test_m3_reduced_candidates_give_the_same_numbers_as_candidate_rows(ran):
    """orderreplay.run(reduce_quotes=True) against the tool's default path on the same unit."""
    root, _, man = ran
    u = man["steps"]["ordermatch"]["chunks"][0]
    d = root / "lab" / "reconstruct" / "run1" / u["dir"]
    full, rows = orderreplay.run(root / "tape", d, u["from"], u["to"])
    red, rrows = orderreplay.run(root / "tape", d, u["from"], u["to"], reduce_quotes=True)
    assert [r for r in rows if r["kind"] == "order"] == rrows and any(r["kind"] == "quote" for r in rows)
    nf = full["never_filled_quotes_estimate"]
    assert red["never_filled_quotes_estimate"] == nf and nf["pessimistic"]["n"] == 1
    assert red["counts"]["placement_not_found"] == full["counts"]["placement_not_found"] == 0
    assert red["conditional_on_real_fill"] == full["conditional_on_real_fill"]
    assert red["never_filled_by_wallet"][W1]["pessimistic"]["n"] == 1


def test_m3_wallet_batches_give_the_same_wallet_numbers(tmp_path):
    """--wallet-batch 1: W1 and W3 run in separate ordermatch / orderreplay calls on the same tape."""
    root = tmp_path
    chain = Chain(world2(root, shared_tx=True))
    both = {"wallets": (W1, W3), "roles": {W1: "cand", W3: "cand"}}
    a = run(cfg(root, **both), chain)
    b = run(cfg(root, out=root / "lab" / "reconstruct" / "b1", orders=root / "lab" / "reconstruct" / "run1" / "ordertape",
                wallet_batch=1, steps=("ordermatch", "orderreplay", "profile"), **both), lambda *x: 1 / 0)
    first = a["steps"]["ordermatch"]["chunks"][0]["chunk"]
    assert [(u["chunk"] == first, u["batch"], u["status"]) for u in b["steps"]["ordermatch"]["chunks"]] == [
        (True, "b00", "ran"), (True, "b01", "ran"), (False, "b00", "ran"), (False, "b01", "empty")]
    ma, mb = _match(root)["wallets"], _match(root, "b1")["wallets"]
    for w in (W1, W3):
        for k in ("maker_orders", "observable", "matched", "placebo_matched", "outcomes", "rest_ms_known"):
            assert ma[w][k] == mb[w][k], (w, k)
        assert a["wallets"][w]["gates"] == b["wallets"][w]["gates"]
    # other cohort wallets' orders are still loaded in a batch: W3's 100-share orders are known, not candidates
    assert ma[W1]["fingerprint"]["total"] == mb[W1]["fingerprint"]["total"]
    ra, rb = _replay(root)["wallets"][W1], _replay(root, "b1")["wallets"][W1]
    assert ra["conditional_on_real_fill"] == rb["conditional_on_real_fill"]


def test_m3_sub_hour_chunks_through_the_cli(ran):
    root, _, _ = ran
    out = root / "lab" / "reconstruct" / "m30"
    rc = main(["--data", str(root / "tape"), "--lab", str(root / "lab"), "--cohort", str(root / "cohort.json"),
               "--from", str(T0), "--to", str(T0 + 7200), "--chunk-minutes", "30", "--out", str(out),
               "--orders", str(root / "lab" / "reconstruct" / "run1" / "ordertape"),
               "--steps", "ordermatch,orderreplay,profile", "--size-tol-rel", "0.02"])
    man = orjson.loads((out / "manifest.json").read_bytes())
    assert rc == 0 and man["args"]["chunk_minutes"] == 30 and man["args"]["size_tol_rel"] == 0.02
    assert [u["status"] for u in man["steps"]["ordermatch"]["chunks"]] == ["ran", "empty", "ran", "empty"]
    om = orjson.loads((out / "ordermatch" / "ordermatch.json").read_bytes())
    assert om["params"]["per_unit"]["size_tol_rel"] == 0.02 and om["wallets"][W1]["matched"] == 35


def test_m3_memory_budget_stops_the_run_cleanly(tmp_path, monkeypatch):
    root = tmp_path
    chain = Chain(world2(root))
    monkeypatch.setattr(reconstruct, "peak_rss_mb", lambda: 999.0)
    man = run(one(root, mem_budget_mb=500), chain)
    assert "999.0 MB" in man["stopped"] and "--mem-budget-mb 500" in man["stopped"]
    assert [u["status"] for u in man["steps"]["ordermatch"]["chunks"]] == ["ran", "absent"]  # stopped after one unit
    assert man["steps"]["ordermatch"]["chunks"][0]["peak_rss_mb"] == 999.0
    assert man["steps"]["orderreplay"]["status"] == "absent" and man["steps"]["profile"]["status"] == "absent"
    assert all("ordermatch:mem_budget_stop" in man["wallets"][W1]["untrusted"][s]
               for s in ("ordermatch", "orderreplay", "profile"))
    assert "RUN STOPPED" in (root / "lab" / "reconstruct" / "run1" / "report.md").read_text()
    # resume under a budget that holds: the finished unit is kept, the rest runs
    man = run(one(root, mem_budget_mb=5000), chain)
    assert man["stopped"] is None
    assert [u["status"] for u in man["steps"]["ordermatch"]["chunks"]] == ["skipped", "ran"]
    assert man["wallets"][W1]["untrusted"] == {s: [] for s in reconstruct.STEPS}


def test_s1_shared_tape_reverifies_cached_orders_and_recovers_a_missing_part(tmp_path):
    root = tmp_path
    chain = Chain(world2(root, shared_tx=True))
    shared = root / "lab" / "orders_shared"
    c = one(root, orders=shared, steps=("ordertape",), t_to=T0 + 3600, max_txs=1)
    con = reconstruct._connect(c.store, "256MB", 1)[0]
    try:
        todo = ot.select_txs(con, [W1], c.t_from, c.t_to, c.max_txs)
        ot.run(todo, ot.Pool(list(ot.DEFAULT_RPCS), chain), shared,
               expected=lambda txs: ot.goldsky_ids(con, [W1], txs))
    finally:
        con.close()
    cohort = replace(c, wallets=(W1, W3), roles={W1: "cand", W3: "cand"})
    assert reconstruct.verified_shares(cohort)[W3]["share"] == 0.0
    cache = {}
    before = reconstruct.orders_digest(shared, cache)["sha256"]
    man = run(c, chain)
    assert chain.calls == 1
    assert man["steps"]["ordertape"]["summary"]["txs_reverified"] == 1
    after = reconstruct.orders_digest(shared, cache)["sha256"]
    assert after != before and man["steps"]["ordertape"]["outputs"]["orders"]["sha256"] == after

    man = run(cohort, chain)
    g = man["wallets"][W3]["gates"]["ordertape.verified_share"]
    assert (g["value"], g["n"], g["status"]) == (1.0, 1, "pass")
    assert chain.calls == 1
    assert man["wallets"][W1]["gates"]["ordertape.verified_share"]["status"] == "pass"

    next(shared.glob("day=*/*.parquet")).unlink()
    man = run(cohort, chain)
    assert chain.calls == 2
    assert man["steps"]["ordertape"]["complete"] is True
    assert man["wallets"][W3]["gates"]["ordertape.verified_share"]["status"] == "pass"


@pytest.mark.parametrize("budget", [10, 40])
def test_s2_an_incomplete_order_tape_is_untrusted_and_said_so(tmp_path, budget):
    root = tmp_path
    man = run(one(root, chunk_s=7200, max_requests=budget), Chain(world2(root)))
    s = man["steps"]["ordertape"]
    assert s["complete"] is False and s["summary"]["stopped"] == "max_requests" and s["left"]["txs_not_attempted"] > 0
    assert all("ordertape:incomplete" in v for v in man["wallets"][W1]["untrusted"].values())
    assert man["wallets"][W1]["tape"] == {"fills_in_range": 64, "fills_expected": budget, "fills_matched": budget,
                                          "share": 1.0, "coverage": round(budget / 64, 6)}
    md = (root / "lab" / "reconstruct" / "run1" / "report.md").read_text()
    assert "ORDER TAPE INCOMPLETE" in md and f"({budget} / 64)" in md
    row = next(line for line in md.splitlines() if line.startswith(f"| {W1}"))
    assert "UNTRUSTED" in row


def test_s3_this_file_and_its_constants_are_in_every_key(tmp_path, monkeypatch):
    root = tmp_path
    chain = Chain(world2(root))
    run(one(root, chunk_s=7200), chain)
    assert _match(root)["wallets"][W1]["fingerprint"] is not None
    real = reconstruct.code_sha
    monkeypatch.setattr(reconstruct, "code_sha", lambda: real() | {"lab/reconstruct.py": "changed"})
    man = run(one(root, chunk_s=7200), chain)
    assert {s: man["steps"][s]["status"] for s in reconstruct.STEPS} == dict.fromkeys(reconstruct.STEPS, "ran")
    monkeypatch.setattr(reconstruct, "code_sha", real)
    monkeypatch.setattr(reconstruct, "MIN_FP_ORDERS", 1000)
    man = run(one(root, chunk_s=7200), chain)
    assert man["steps"]["ordermatch"]["status"] == "ran" and _match(root)["wallets"][W1]["fingerprint"] is None
    assert {"twin/engine.py", "twin/features.py", "strategy/genome.py"} <= set(reconstruct.CODE["orderreplay"])
    assert all("lab/reconstruct.py" in fs for fs in reconstruct.CODE.values())
    assert {"lab/tradeallocation.py", "lab/ordertape.py"} <= set(reconstruct.CODE["ordermatch"])


def test_s4_a_kill_in_the_second_chunk_resumes_there(tmp_path, monkeypatch):
    root = tmp_path
    chain = Chain(world2(root))
    real, n, calls = ordermatch.run, [0], []

    def die2(*a, **k):
        n[0] += 1
        if n[0] == 2:
            raise KeyboardInterrupt
        return real(*a, **k)

    monkeypatch.setattr(ordermatch, "run", die2)
    with pytest.raises(KeyboardInterrupt):
        run(one(root, steps=("ordertape", "ordermatch")), chain)
    monkeypatch.setattr(ordermatch, "run", lambda *a, **k: calls.append(a[2] - T0) or real(*a, **k))
    man = run(one(root, steps=("ordertape", "ordermatch")), chain)  # the lock was released by the kill
    assert calls == [3600] and [u["status"] for u in man["steps"]["ordermatch"]["chunks"]] == ["skipped", "ran"]


def test_s4_a_half_written_merge_is_stale_then_heals(tmp_path, monkeypatch):
    root = tmp_path
    chain = Chain(world2(root))
    real = reconstruct._partition

    def half(files, *a):
        real(files[:1], *a)  # only the first chunk's rows reach the wallet files, then the process dies
        raise KeyboardInterrupt

    monkeypatch.setattr(reconstruct, "_partition", half)
    with pytest.raises(KeyboardInterrupt):
        run(one(root, steps=("ordertape", "ordermatch")), chain)
    monkeypatch.setattr(reconstruct, "_partition", real)
    out = root / "lab" / "reconstruct" / "run1"
    assert sum(1 for _ in open(out / "wallets" / W1 / "orders.jsonl")) == 32
    assert not (out / "_state" / "ordermatch.json").exists()
    man = run(one(root, steps=("orderreplay", "profile")), chain)
    assert man["steps"]["ordermatch"]["stale"]  # chunk outputs without a merge
    g = man["wallets"][W1]["gates"]["orderreplay.placement_not_found_share"]
    assert (g["value"], g["status"]) == (-1.0, "fail")  # 64 replayed rows against 32 matched: not a pass
    assert "ordermatch:stale" in man["wallets"][W1]["untrusted"]["orderreplay"]
    assert "ordermatch:stale" in man["wallets"][W1]["untrusted"]["profile"]
    man = run(one(root), chain)  # a full run redoes the ordermatch merge, and the replay merge that read it
    assert man["steps"]["orderreplay"]["status"] == "ran" and not man["steps"]["orderreplay"]["stale"]
    assert _replay(root)["wallets"][W1]["counts"]["placement_not_found"] == 0
    assert man["wallets"][W1]["untrusted"] == {s: [] for s in reconstruct.STEPS}
    assert not list(out.glob("wallets/*/*.tmp"))


def test_s4_a_negative_share_fails_its_gate():
    assert reconstruct.gate(GATES, "orderreplay.placement_not_found_share", -1.0, 32)["status"] == "fail"
    assert reconstruct.gate(GATES, "orderreplay.placement_not_found_share", 0.0, 32)["status"] == "pass"


def test_same_length_changed_checkpoint_is_repaired(tmp_path):
    c = cfg(tmp_path)
    chain = Chain(world(tmp_path))
    man = run(c, chain)
    first = c.out / man["steps"]["ordermatch"]["chunks"][0]["dir"] / "orders.jsonl"
    original = first.read_bytes()
    first.write_bytes(original.replace(W1.encode(), W2.encode(), 1))
    assert first.read_bytes() != original and first.stat().st_size == len(original)
    repaired = run(c, chain)
    assert [u["status"] for u in repaired["steps"]["ordermatch"]["chunks"]] == ["ran", "skipped"]
    assert first.read_bytes() == original


def test_capped_wallet_sample_never_reads_as_full_coverage(tmp_path):
    chain = Chain(world2(tmp_path))
    man = run(one(tmp_path, chunk_s=7200, max_txs=1, steps=("ordertape",)), chain)
    assert man["steps"]["ordertape"]["complete"]
    wallet = man["wallets"][W1]
    assert wallet["tape"]["fills_expected"] == 1 < wallet["tape"]["fills_in_range"]
    assert all("ordertape.full_store_coverage:fail" in reasons for reasons in wallet["untrusted"].values())


def test_interrupted_replacement_publishes_its_own_range(tmp_path, monkeypatch):
    chain = Chain(world(tmp_path))
    run(cfg(tmp_path), chain)
    c = cfg(tmp_path, t_to=T0 + 3600)
    def interrupt(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(reconstruct, "step_ordertape", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(c, chain)
    for name in ("manifest.json", "report.json"):
        doc = orjson.loads((c.out / name).read_bytes())
        assert doc["status"] == "running" and doc["range"] == {"from": T0, "to": T0 + 3600}
    assert "in progress" in (c.out / "report.md").read_text()


def test_a_same_length_change_to_a_sealed_tape_is_rejected(tmp_path):
    root = tmp_path
    chain = Chain(world(root))
    man = run(cfg(root), chain)
    tape = root / "tape" / next(iter(man["steps"]["ordermatch"]["chunks"][0]["inputs"]["tape"]))
    data = tape.read_bytes()
    tape.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
    with pytest.raises(ValueError, match="recorded seal"):
        run(cfg(root), chain)


def test_s5_s6_report_totals_and_identity_precision(tmp_path):
    root = tmp_path
    strict = GATES | {"ordermatch.match_rate": GATES["ordermatch.match_rate"] | {"min": 1.01}}
    run(one(root, chunk_s=7200), Chain(world2(root)), strict)
    out = root / "lab" / "reconstruct" / "run1"
    md = (out / "report.md").read_text()
    totals = {line.split(":")[0][2:]: line for line in md.splitlines() if line.startswith("- ")}
    for k, step in (("match", "ordermatch"), ("twin", "orderreplay"), ("copyability", "profile")):
        assert totals[k].endswith(f"(includes 1 wallets not trusted for {step})"), totals[k]
    assert "includes" not in totals["wallets"]
    # identity precision: null with the reason everywhere, and said in the report
    assert "Identity precision of the footprint is NOT MEASURED" in md and "| sizes stable |" in md
    fp = _match(root)["wallets"][W1]["fingerprint"]
    assert fp["total"]["identity_precision_upper"] is None and "not measured" in fp["identity_precision_note"]
    assert all(v["identity_precision_upper"] is None for v in fp["per_size"].values())
    unit = orjson.loads(next(out.glob("chunks/*/*/ordermatch/ordermatch.json")).read_bytes())["wallets"][W1]
    assert unit["fingerprint"]["total"]["identity_precision_upper"] is None
    led = (out / "profile" / f"{W1}.ledger.json").read_text()
    assert '"identity_precision_upper": null' in led and '"identity_precision_upper": 1' not in led
    rep = orjson.loads((out / "report.json").read_bytes())
    assert rep["cohort_totals_include_untrusted_wallets"]["match"] == 1
    assert rep["wallets"][0]["footprint"]["identity_precision_upper"] is None


def test_s7_a_second_run_on_the_same_directory_is_refused(tmp_path):
    root = tmp_path
    chain = Chain(world2(root))
    c = one(root, chunk_s=7200)
    with reconstruct._lock(c.out), pytest.raises(reconstruct.Locked):
        run(c, chain)
    with reconstruct._lock(c.orders):  # someone else is writing the order tape
        with pytest.raises(reconstruct.Locked):
            run(c, chain)
    assert run(c, chain)["steps"]["ordertape"]["complete"]


def test_s7_the_timer_queue_retries_a_range_before_new_ones(tmp_path):
    state, seen = tmp_path / "_pending.json", []

    def go(ok, used=30):
        def f(a, b, left):
            seen.append((a, b, left))
            return {"stopped": None,
                    "steps": {"ordertape": {"status": "ran", "complete": ok(a), "summary": {"requests": used}}}}
        return f

    reconstruct.run_pending(state, (0, 6), go(lambda a: False), 100)       # firing 1: range 0 does not finish
    assert seen == [(0, 6, 100)] and [p["from"] for p in orjson.loads(state.read_bytes())["pending"]] == [0]
    seen.clear()
    reconstruct.run_pending(state, (6, 12), go(lambda a: True), 100)       # firing 2: the old range first, then the new
    assert seen == [(0, 6, 100), (6, 12, 70)] and orjson.loads(state.read_bytes())["pending"] == []
    seen.clear()
    with pytest.raises(ZeroDivisionError):                                 # a crash keeps the range
        reconstruct.run_pending(state, (24, 30), lambda a, b, left: 1 / 0, 100)
    assert [p["from"] for p in orjson.loads(state.read_bytes())["pending"]] == [24]
    for _ in range(reconstruct.MAX_TRIES):                                 # a range that never finishes is given up
        reconstruct.run_pending(state, (24, 30), go(lambda a: False), 100)
    st = orjson.loads(state.read_bytes())
    assert st["pending"] == [] and [p["from"] for p in st["gave_up"]] == [24]


def test_wallet_gates_use_exact_counts_at_thresholds():
    rp = {"counts": {"orders": 100001, "headline_orders": 100001, "placement_not_found": 1001},
          "adapter_gate": {"print_seen_in_replay": 100001, "routed_to_order_price_and_side": 95000,
                           "delivered_to_live_quote": 90249, "routed_share": 0.95, "delivered_share": 0.95},
          "bracket_violations": {"pessimistic_overfilled": 5001, "optimistic_underfilled": 0}}
    g = reconstruct.wallet_gates(GATES, None, None, rp)
    for k in ("bracket_violation_share", "placement_not_found_share", "routed_share", "delivered_share"):
        assert g[f"orderreplay.{k}"]["status"] == "fail", k


@pytest.mark.parametrize("how", ["missing", "not_match_orders"])
def test_audit_one_of_35_txs_decoded_is_never_complete(tmp_path, how):
    """Audit 2026-11-26: 1 of 35 selected txs decoded was reported complete with 100% verified and
    the retry was skipped. The node lacks 34 txs (or answers them with a call that is not
    matchOrders): the tape is incomplete, every wallet says so, and the next run asks again."""
    root = tmp_path
    txs = _cand_world(root, 0)
    first = sorted(txs)[0]
    extra = [f"0x{k:064x}" for k in range(900, 903)]  # 35 selected txs in all
    for i, tx in enumerate(extra):
        txs[tx] = (txs[first][0], BLOCK + 900 + i)
    g = SegmentWriter(root / "tape", "goldsky_activity")
    g.append(orjson.dumps({"data": [
        {"id": f"x{i}", "address": W1, "event_type": "TRADE", "token_id": "101", "side": "BUY",
         "amount_shares": 1.0, "amount_usdc": 0.5, "fee": 0.0, "block_timestamp": T0 + 100,
         "block_number": BLOCK + 900 + i, "order_type": "maker", "price": 0.5, "order_id": "0x" + "ee" * 32,
         "transaction_hash": tx} for i, tx in enumerate(extra)]}).decode(), tw=(T0 + 3600) * 10**9,
        meta={"job": "cohort-a", "status": 200, "rows": 3})
    g.close()
    compact(root / "tape", root / "lab" / "fills")
    assert len(txs) == 35
    good = Chain(txs)

    def flaky(url, method, params):
        if params[0] == first:
            return good(url, method, params)
        return None if how == "missing" else {"input": "0xdeadbeef", "to": EX, "blockNumber": hex(BLOCK)}

    c = one(root, t_to=T0 + 3600, steps=("ordertape",))
    man = run(c, flaky)
    s = man["steps"]["ordertape"]
    assert (s["txs_selected"], s["txs_decoded_on_tape"], s["complete"]) == (35, 1, False)
    assert s["left"]["txs_selected_not_decoded"] == 34
    g1 = man["wallets"][W1]["gates"]["ordertape.verified_share"]
    assert (g1["value"], g1["n"], g1["status"]) == (1.0, 1, "pass")  # the one decoded tx verifies; not enough:
    assert "ordertape:incomplete" in man["wallets"][W1]["untrusted"]["ordertape"]
    assert man["wallets"][W1]["tape"]["coverage"] == round(1 / 35, 6)
    md = (root / "lab" / "reconstruct" / "run1" / "report.md").read_text()
    assert "ORDER TAPE INCOMPLETE** (1 of 35 selected txs decoded" in md
    # the next run is not skipped
    again: list[str] = []
    man = run(c, lambda url, method, params: again.append(params[0]) or good(url, method, params))
    if how == "missing":  # ordertape retries exactly the 34 txs it does not have
        assert sorted(again) == sorted(set(txs) - {first})
        assert man["steps"]["ordertape"]["complete"] is True and man["steps"]["ordertape"]["left"] == {}
        assert "ordertape:incomplete" not in man["wallets"][W1]["untrusted"]["ordertape"]
    else:  # ordertape recorded them as done: it cannot refetch, so the tape stays incomplete, loudly
        assert again == [] and man["steps"]["ordertape"]["status"] == "ran"
        assert man["steps"]["ordertape"]["complete"] is False
        assert man["steps"]["ordertape"]["left"] == {"txs_selected_not_decoded": 34}
