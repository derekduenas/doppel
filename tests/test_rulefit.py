"""Rule fit on a synthetic wallet whose quoting rule is known exactly.

The wallet re-posts every 2 s in each btc 5m window: a bid joining the best bid (BUY Up) and an
ask at round(mid + 2 - 0.05 * inventory) placed as a BUY on the Down token; sizes 97..100. Spread,
mid and inventory vary independently, so touch, mid and the skew are separately identified."""

import numpy as np
import orjson
import pytest

from doppel.lab import rulefit
from doppel.lab.rulefit import Fair, Inventory, main, run, up_order, up_view
from doppel.strategy.genome import from_dict, validate
from doppel.tape.segment import SegmentWriter

T0 = 1795629600  # hour-aligned, a 5m boundary
MS = T0 * 1000
NS = 1_000_000_000
W = "0xabc0000000000000000000000000000000000001"
L = 300


def slug(k):
    return f"btc-updown-5m-{T0 + L * k}"


def book(ms, asset, bids, asks, market):
    lv = lambda xs: [{"price": f"{p:.2f}", "size": str(s)} for p, s in xs]  # noqa: E731
    return ms, {"event_type": "book", "asset_id": asset, "market": market, "bids": lv(bids),
                "asks": lv(asks), "timestamp": str(MS + ms)}


def pc(ms, asset, price, size, side, market):
    return ms, {"event_type": "price_change", "market": market, "timestamp": str(MS + ms),
                "price_changes": [{"asset_id": asset, "price": f"{price:.2f}", "size": str(size), "side": side}]}


def trade(ms, asset, price, size, tx, market):
    return ms, {"event_type": "last_trade_price", "asset_id": asset, "market": market, "price": f"{price:.2f}",
                "size": str(size), "side": "SELL", "timestamp": str(MS + ms), "transaction_hash": tx}


def world(seed=1, windows=6, candidates=0, ask_ref=None):
    """frames, ordermatch rows, order-tape rows, candidate rows. ``ask_ref(k, ms)`` -> the ask's
    reference in Up cents (default: the mid)."""
    rng = np.random.default_rng(seed)
    frames, orders, tape, quotes = [], [], [], []
    n = 0
    for k in range(windows):
        U, D, mk = f"U{k}", f"D{k}", f"m{k}"
        inv, bb = 0.0, int(rng.integers(35, 60))
        for t in range(k * L * 1000 + 10_000, (k + 1) * L * 1000 - 8_000, 2000):
            bb = int(np.clip(bb + rng.integers(-1, 2), 25, 70))
            ba = bb + int(rng.integers(1, 5))
            frames.append(book(t, U, [(bb / 100, 500), ((bb - 5) / 100, 300)], [(ba / 100, 400)], mk))
            frames.append(book(t, D, [((100 - ba) / 100, 400)], [((100 - bb) / 100, 500)], mk))
            ref = (bb + ba) / 2 if ask_ref is None else ask_ref(k, t + 310)
            ask = int(round(ref + 2 - 0.05 * inv))
            placed = []
            for ms, tok, price, old in ((t + 300, U, bb / 100, 500),
                                        (t + 310, D, (100 - ask) / 100, 400 if ask == ba else 0)):
                if not 2 <= round(100 * price) <= 98:
                    continue  # no such price: this side is not quoted in this step
                n += 1
                size = float(rng.integers(97, 101))
                frames.append(pc(ms, tok, price, old + size, "BUY", mk))
                o = {"order_id": f"o{n}", "maker": W, "token_id": tok, "side": "BUY", "price": round(price, 2),
                     "size": size, "created_ms": MS + ms - 150, "placed_venue_ms": MS + ms,
                     "placed_tw_ns": (MS + ms) * 1_000_000, "filled": 0.0, "remainder": size, "n_fills": 0,
                     "fills_at": [], "outcome": "cancelled", "reason": "exact_decrease", "end_ms": MS + ms + 1500,
                     "rest_min_ms": 1500, "rest_max_ms": 1500, "fill_before_placement": False}
                orders.append(o)
                placed.append(o)
            for j in range(candidates):  # never-filled bids 3 c behind the best
                ms = t + 320 + j
                frames.append(pc(ms, U, (bb - 3) / 100, 97 * (j + 1), "BUY", mk))
                quotes.append({"attribution": "fingerprint_candidate", "wallet": W, "token_id": U,
                               "price": round((bb - 3) / 100, 2), "side_view": "BUY", "venue_ms": MS + ms,
                               "tw_ns": (MS + ms) * 1_000_000, "size": 97.0, "outcome": "cancelled",
                               "reason": "exact_decrease", "end_ms": MS + ms + 200})
            # one fill per step moves the inventory: + on the bid (Up bought), - on the ask (Down bought)
            o = placed[0] if len(placed) == 1 or inv < -60 or (inv <= 60 and rng.random() < 0.5) else placed[1]
            q = float(rng.integers(1, 21))
            inv += q if o is placed[0] else -q
            tx = f"0xt{n}"
            frames.append(trade(t + 600, o["token_id"], o["price"], q, tx, mk))
            o.update(filled=q, remainder=o["size"] - q, n_fills=1, fills_at=[[tx, MS + t + 600, q]])
            tape.append({"tx": tx, "block": 1, "block_ts": T0 + (t + 600) // 1000 + 2, "role": "maker",
                         "order_id": o["order_id"], "maker": W, "token_id": o["token_id"], "side": "BUY",
                         "size": o["size"], "limit": o["price"], "maker_amount": round(o["size"] * o["price"] * 1e6),
                         "taker_amount": round(o["size"] * 1e6), "fill_amount": round(q * o["price"] * 1e6),
                         "created_ms": o["created_ms"], "salt": "1"})
    return frames, orders, tape, quotes


def write(tmp, frames, orders, tape, quotes=None, om_doc=None, windows=6):
    w = SegmentWriter(tmp / "tape", "clob_btc_5m")
    for ms, m in sorted(frames, key=lambda f: f[0]):
        w.append(orjson.dumps([m]).decode(), tw=(MS + ms) * 1_000_000)
    w.close()
    jl = lambda rows: b"".join(orjson.dumps(r) + b"\n" for r in rows)  # noqa: E731
    om = tmp / "om"
    om.mkdir(exist_ok=True)
    (om / "orders.jsonl").write_bytes(jl(orders))
    if quotes is not None:
        (om / "quotes.jsonl").write_bytes(jl(quotes))
    if om_doc is not None:
        (om / "ordermatch.json").write_bytes(orjson.dumps(om_doc))
    (tmp / "ordertape.jsonl").write_bytes(jl(tape))
    (tmp / "tokens.jsonl").write_bytes(jl(
        [{"token": f"{'UD'[i]}{k}", "slug": slug(k), "idx": i, "status": "open"} for k in range(windows) for i in (0, 1)]))


def fit(tmp, out=None, **kw):
    return run(tmp / "tape", tmp / "om", tmp / "ordertape.jsonl", W, T0, T0 + 3600, out,
               token_files=[tmp / "tokens.jsonl"], **kw)


@pytest.fixture(scope="module")
def fitted(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rulefit")
    write(tmp, *world()[:3])
    return fit(tmp, tmp / "out") + (tmp,)


def test_known_rule_is_recovered(fitted):
    genome, rep, rows, _ = fitted
    assert rep["label"] == "rule conditional on fill" and "0.3%" in rep["selection_caveat"]
    bid, ask = rep["price_rule"]["bid"], rep["price_rule"]["ask"]
    # bid = best bid exactly
    assert (bid["chosen"], bid["tie_with"]) == ("touch", None)
    assert bid["offset_c"]["value"] == 0 and bid["offset_c"]["iqr"] == 0 and bid["offset_c"]["oos"]["mae_c"] == 0
    assert bid["within_1c_of_const_rule"]["value"] == 1 and bid["offset_c"]["oos"]["within_1c"] == 1
    assert bid["offset_c"]["ci95"] == [0, 0] and bid["offset_c"]["clusters"] == 6
    # ask = mid + 2 - 0.05 * inventory (cent rounding leaves ~0.25 c of error)
    assert (ask["chosen"], ask["model"], ask["tie_with"]) == ("mid", "skew", None)
    assert abs(ask["intercept_c_with_skew"] - 2) < 0.1
    k = ask["skew_c_per_contract"]
    assert abs(k["value"] - 0.05) < 0.002 and 0.045 < k["ci95"][0] <= k["value"] <= k["ci95"][1] < 0.055
    assert k["oos"]["mae_c_with_skew"] < 0.35 < k["oos"]["mae_c_const"]
    assert ask["candidates"]["touch"]["oos_mae_skew_c"] > 2 * ask["candidates"]["mid"]["oos_mae_skew_c"]
    assert ask["inventory_range"][1] - ask["inventory_range"][0] > 60
    # every ask was a BUY on the Down token: Up terms
    assert {r["up_side"] for r in rows if r["idx"] == 1} == {"ask"} and bid["n_rows"] == ask["n_rows"] == len(rows) // 2
    # size, timing
    size = rep["size_rule"]
    assert size["descriptor"]["rule"] == "fixed_sizes" and set(size["descriptor"]["sizes_90"]) <= {97.0, 98.0, 99.0, 100.0}
    assert size["train_size_set_covers_test"]["value"] == 1 and 97 <= size["median_size"]["value"] <= 100
    gap = rep["timing_rule"]["gap_s"]
    assert gap["value"] == 2 and gap["ci95"] == [2, 2] and gap["oos"]["holdout_value"] == 2
    shares = {k: v["value"] for k, v in rep["timing_rule"]["trigger_shares"].items()}
    assert abs(sum(shares.values()) - 1) < 1e-3 and shares["own_fill"] > 0.3 and shares["unknown"] == 0
    assert rep["timing_rule"]["cancel"]["life_ms"]["value"] == 1500
    part = rep["participation_rule"]
    assert part["windows_seen"] == part["windows_with_orders"] == 6 and part["presence_rate"]["value"] > 0.9
    assert part["both_sides_share_when_present"]["value"] == 1 and part["pre_open_order_share"] == 0


def test_genome_is_a_valid_unregistered_draft_and_lists_what_the_schema_drops(fitted):
    genome, rep, _, tmp = fitted
    assert genome["status"] == "draft" and rep["genome"]["registered"] is False and rep["genome"]["validate"] == []
    g = from_dict(orjson.loads((tmp / "out" / "genome.json").read_bytes()))
    assert validate(g) == [] and g.id == rep["genome"]["id"]
    assert g.source.wallets == (W,) and g.source.parents == () and g.universe.venue == "intl_sim"
    assert g.evidence["wallet"] == W and g.evidence["label"] == "rule conditional on fill"
    m = g.maker
    assert m is not None and m.quote_ref == "mid" and m.sides == "both" and 97 <= m.size <= 100
    assert 1.5 < m.half_spread_c < 2.5 and 0.015 < m.skew_c_per_contract < 0.035 and m.max_quote_age_s == 2
    items = {c["item"]: c for c in rep["cannot_express"]}
    assert "bid: quote pegged to the same-side best" in items and "a different reference per side" in items
    assert "size drawn from a set / randomised" in items and "status draft" in items
    assert items["cost of the schema on the price rule"]["measured"]["oos_cost_of_schema_c"] > 0
    assert "inventory skew differs by side" in items  # the bid has none, the ask 0.05
    # same inputs -> same id (evidence is part of the id: no clock, seeded bootstrap)
    assert fit(tmp)[0] == genome
    assert sorted(p.name for p in (tmp / "out").iterdir()) == ["decision_table.jsonl", "fit_report.json",
                                                              "genome.json", "rule.md"]


def test_state_excludes_the_order_itself_and_everything_received_later(tmp_path):
    frames, orders, tape, _ = world(seed=3, windows=2)
    (tmp_path / "a").mkdir()
    write(tmp_path / "a", frames, orders, tape, windows=2)
    rows = fit(tmp_path / "a")[2]
    by = {r["id"]: r for r in rows}
    # the bid joins a 500 best bid: its own increase (same tw as the cutoff) is not in the state
    assert all(r["depth_bid"] == 500 and r["d_touch_c"] == 0 for r in rows if r["up_side"] == "bid")
    # change everything received at or after one order's placement: its own record, later books,
    # later prints (so later fills are timed differently) and a different future for the wallet
    cut = orders[len(orders) // 2]
    t = cut["placed_venue_ms"] - MS
    future = [book(ms, "U0", [(0.01, 1)], [(0.99, 1)], "m0") for ms in range(t, t + 60_000, 500)]
    (tmp_path / "b").mkdir()
    write(tmp_path / "b", [f for f in frames if f[0] < t] + future, orders, tape, windows=2)
    after = {r["id"]: r for r in fit(tmp_path / "b")[2]}
    early = [o["order_id"] for o in orders if o["placed_tw_ns"] <= cut["placed_tw_ns"]]
    assert len(early) > 50 and all(after[i] == by[i] for i in early)
    later = [o["order_id"] for o in orders if o["placed_tw_ns"] > cut["placed_tw_ns"]]
    assert any(after[i] != by[i] for i in later)  # the test would notice a state that moved


def test_inventory_counts_only_fills_located_before_the_cutoff():
    tok = {"U": {"slug": "s", "idx": 0}, "D": {"slug": "s", "idx": 1}}
    fills = [{"tx": "a", "token": "U", "side": "BUY", "shares": 10.0, "block_ts": 100},
             {"tx": "b", "token": "D", "side": "BUY", "shares": 4.0, "block_ts": 100},
             {"tx": "c", "token": "D", "side": "SELL", "shares": 1.0, "block_ts": 50},
             {"tx": "d", "token": "X", "side": "BUY", "shares": 1.0, "block_ts": 50}]
    inv = Inventory(fills, tok, {"a": 90 * NS, "b": 95 * NS})  # c has no print: block time
    assert (inv.unmapped, inv.by_block_time) == (1, 1)
    assert [inv.at("s", t * NS) for t in (50, 51, 90, 91, 95, 96)] == [0, 1, 1, 11, 11, 7]
    assert inv.any_between("s", 90 * NS, 96 * NS) and not inv.any_between("s", 90 * NS, 95 * NS)
    assert inv.at("other", 10**20) == 0


def test_up_terms_mirror_the_down_token():
    assert up_order("BUY", 0.3, 0) == ("bid", 30.0) and up_order("SELL", 0.3, 0) == ("ask", 30.0)
    assert up_order("BUY", 0.3, 1) == ("ask", 70.0) and up_order("SELL", 0.3, 1) == ("bid", 70.0)
    v = up_view((0.3, 5.0, 0.34, 9.0), 1)  # Down bid 30 x5, ask 34 x9 -> Up bid 66 x9, ask 70 x5
    assert (v["bb_c"], v["depth_bid"], v["ba_c"], v["depth_ask"], v["mid_c"], v["spread_c"]) == (66, 9, 70, 5, 68, 4)
    assert up_view(None, 0)["mid_c"] is None and up_view((0.3, 5.0, None, None), 0)["mid_c"] is None


def test_selection_check_moves_the_rule_when_unfilled_quotes_sit_elsewhere(tmp_path):
    frames, orders, tape, quotes = world(seed=5, windows=4, candidates=2)
    doc = {"wallets": {W: {"fingerprint": {"total": {"identity_precision_upper": 0.8}}}}}
    write(tmp_path, frames, orders, tape, quotes, doc, windows=4)
    _, rep, rows = fit(tmp_path)
    sel = rep["selection_check"]
    assert sel["identity_precision"] == 0.8 and sel["n_candidates"] == len(quotes) == len(orders)
    # the filled bids sit AT the best bid; the unfilled candidates 3 c behind it, 2 per filled bid
    # at weight 0.8: the weighted median offset moves from 0 to -3
    b = sel["sides"]["bid"]["at_verified_ref"]
    assert b["offset_c"] == {"verified": 0, "pooled": -3, "move": -3, "pooled_ci95": [-3, -3]}
    assert b["within_1c"]["verified"] == 1 and abs(b["within_1c"]["pooled"] - 1.6 / 2.6) < 0.01
    assert sel["sides"]["ask"]["n_candidates"] == 0 and sel["sides"]["ask"]["at_verified_ref"]["offset_c"]["move"] == 0
    # the verified rule itself is untouched by the candidates
    assert rep["price_rule"]["bid"]["offset_c"]["value"] == 0 and rep["price_rule"]["bid"]["n_rows"] == len(orders) // 2
    assert sum(r["kind"] == "candidate" for r in rows) == len(quotes)
    # at half the weight the filled orders hold the median: no move
    half = fit(tmp_path, precision=0.4)[1]["selection_check"]["sides"]["bid"]["at_verified_ref"]
    assert half["offset_c"]["move"] == 0
    # no stated precision -> not refitted, said so
    (tmp_path / "om" / "ordermatch.json").unlink()
    assert "not refitted" in fit(tmp_path)[1]["selection_check"]["reason"]


def test_fair_reference_is_found_and_skipped_cleanly_without_brti(tmp_path, capsys):
    rng = np.random.default_rng(7)
    lo, hi = T0 - 900, T0 + 700
    raw = {t: 90000 + float(v) for t, v in zip(range(lo, hi), np.cumsum(rng.normal(0, 3, hi - lo)))}
    lab = tmp_path / "lab"
    w = SegmentWriter(lab, "ref_brti_raw")
    for t, v in raw.items():
        w.append(orjson.dumps({"t_ms": t * 1000, "v": str(v)}).decode(), tw=t * NS)
    w.close()
    series = {t: sum(raw[s] for s in range(t - 59, t + 1)) / 60 for t in range(lo + 60, hi)}
    w = SegmentWriter(lab, "ref_prices")
    w.append(orjson.dumps({"series": [{"candles": [{"timestamp": t, "close": v} for t, v in series.items()]}]}).decode(),
             tw=hi * NS, meta={"asset": "crypto:btc", "status": 200, "from": lo + 60, "to": hi - 1})
    w.close()
    fair = Fair(lab, T0, T0 + 600)
    info = lambda k: {"asset": "btc", "start": T0 + L * k, "end": T0 + L * (k + 1)}  # noqa: E731
    # a print for second s is known at s + 1 and only once received: the value at d ignores second d
    assert fair.at(info(0), (T0 - 1) * NS) is None and fair.at({**info(0), "asset": "eth"}, (T0 + 50) * NS) is None
    f50 = fair.at(info(0), (T0 + 50) * NS + 999_999_999)
    assert f50 is not None and 0 <= f50 <= 100

    def ask_ref(k, ms):
        v = fair.at(info(k), (MS + ms) * 1_000_000)
        assert v is not None
        return v

    frames, orders, tape, _ = world(seed=9, windows=2, ask_ref=ask_ref)
    write(tmp_path, frames, orders, tape, windows=2)
    (lab / "tokens.jsonl").write_bytes((tmp_path / "tokens.jsonl").read_bytes())
    rc = main(["--data", str(tmp_path / "tape"), "--ordermatch-dir", str(tmp_path / "om"), "--orders",
               str(tmp_path / "ordertape.jsonl"), "--wallet", W.upper().replace("0X", "0x"), "--from", str(T0),
               "--to", str(T0 + 3600), "--brti-lab", str(lab), "--out", str(tmp_path / "out")])
    assert rc == 0 and "RULE CONDITIONAL ON FILL" in capsys.readouterr().out
    rep = orjson.loads((tmp_path / "out" / "fit_report.json").read_bytes())
    ask = rep["price_rule"]["ask"]
    assert ask["chosen"] == "fair" and ask["ref_coverage"]["fair"] == 1 and abs(ask["intercept_c_with_skew"] - 2) < 0.15
    assert abs(ask["skew_c_per_contract"]["value"] - 0.05) < 0.01
    assert rep["price_rule"]["bid"]["chosen"] == "touch" and rep["inputs"]["fair"] is True
    # without BRTI: same table, no fair column, the fit falls back to the book references
    _, rep2, rows2 = fit(tmp_path)
    assert all(r["fair_c"] is None and r["d_fair_c"] is None for r in rows2)
    assert rep2["price_rule"]["ask"]["ref_coverage"]["fair"] == 0 and rep2["price_rule"]["ask"]["chosen"] in ("mid", "touch")
    assert rep2["genome"]["fields"]["maker.min_edge_vs_fair_c"].startswith("default 0")


def test_token_map_from_rtds_rows_and_unmapped_rows_are_counted(tmp_path):
    frames, orders, tape, _ = world(seed=11, windows=2)
    write(tmp_path, frames, orders, tape, windows=2)
    w = SegmentWriter(tmp_path / "tape", "rtds_a")  # window 0 only, and only its Up token
    w.append(orjson.dumps({"topic": "activity", "type": "trades", "payload": {
        "transactionHash": "0x1", "proxyWallet": "0xother", "eventSlug": slug(0), "asset": "U0", "side": "BUY",
        "outcomeIndex": 0, "price": 0.5, "size": 1, "fee": 0.1, "timestamp": T0 + 5}}).decode(), tw=(T0 + 6) * NS)
    w.close()
    _, rep, rows = run(tmp_path / "tape", tmp_path / "om", tmp_path / "ordertape.jsonl", W, T0, T0 + 3600)
    n0 = sum(o["token_id"] in ("U0", "D0") for o in orders)
    assert len(rows) == n0 and {r["window"] for r in rows} == {slug(0)}  # D0 mapped as U0's mate
    c = rep["inputs"]["counts"]
    assert c["tokens_mapped_by_mate"] == 1 and c["order_unmapped_token"] == len(orders) - n0
    assert {r["up_side"] for r in rows if r["token"] == "D0"} == {"ask"}
    assert rulefit.token_map(tmp_path / "tape", T0, T0 + 3600, [])["U0"]["idx"] == 0


def test_triggers_and_the_state_just_before_a_cancel(tmp_path):
    U, mk = "U0", "m0"

    def o(oid, placed, **kw):
        return {"order_id": oid, "maker": W, "token_id": U, "side": "BUY", "price": 0.5, "size": 100.0,
                "created_ms": MS + placed - 100, "placed_venue_ms": MS + placed, "placed_tw_ns": (MS + placed) * 1_000_000,
                "filled": 0.0, "remainder": 100.0, "n_fills": 0, "fills_at": [], "outcome": "unknown",
                "reason": "censored", "end_ms": None, "rest_min_ms": 0, "rest_max_ms": None,
                "fill_before_placement": False} | kw

    frames = [book(1000, U, [(0.50, 500)], [(0.54, 400)], mk),
              pc(2000, U, 0.50, 600, "BUY", mk),      # a
              pc(3000, U, 0.50, 700, "BUY", mk),      # b: nothing changed since a landed -> timer
              pc(3500, U, 0.54, 0, "SELL", mk), pc(3501, U, 0.55, 400, "SELL", mk),
              pc(4000, U, 0.50, 800, "BUY", mk),      # c: the ask moved 1 c -> book_move
              trade(4200, U, 0.50, 10, "0xf1", mk),   # c is hit for 10
              pc(4500, U, 0.51, 50, "BUY", mk),       # someone improves the bid
              pc(5000, U, 0.50, 700, "BUY", mk),      # a's 100 leave
              pc(6000, U, 0.50, 800, "BUY", mk)]      # d: an own fill since c -> own_fill
    orders = [o("a", 2000, outcome="cancelled", reason="exact_decrease", end_ms=MS + 5000, rest_min_ms=3000,
                rest_max_ms=3000), o("b", 3000), o("d", 6000),
              o("c", 4000, filled=10.0, remainder=90.0, n_fills=1, fills_at=[["0xf1", MS + 4200, 10.0]]),
              o("x", 2000, maker="0xother"), o("bad", 3000, fill_before_placement=True)]
    tape = [{"tx": "0xf1", "block": 1, "block_ts": T0 + 9, "role": "maker", "order_id": "c", "maker": W,
             "token_id": U, "side": "BUY", "size": 100.0, "limit": 0.5, "maker_amount": 50_000_000,
             "taker_amount": 100_000_000, "fill_amount": 5_000_000, "created_ms": MS + 3900, "salt": "1"}]
    write(tmp_path, frames, orders, tape, windows=1)
    T = rulefit.decision_table(tmp_path / "tape", tmp_path / "om", tmp_path / "ordertape.jsonl", W, T0, T0 + 3600,
                               [tmp_path / "tokens.jsonl"])
    rows = {r["id"]: r for r in T["rows"]}
    assert list(rows) == ["a", "b", "c", "d"]
    assert (T["stats"]["orders_other_makers"], T["stats"]["orders_wrong_match_dropped"]) == (1, 1)
    assert [rows[i]["depth_bid"] for i in "abcd"] == [500, 600, 700, 50]  # size at the best, never its own increase
    assert [rows[i]["inv"] for i in "abcd"] == [0, 0, 0, 10] and [rows[i]["gap_s"] for i in "abcd"] == [None, 1, 1, 2]
    assert (rows["d"]["bb_c"], rows["d"]["ba_c"], rows["d"]["d_touch_c"], rows["d"]["tau_s"]) == (51, 55, -1, 6)
    # the record that removes a's remainder is undone: 0.50 still holds 800 behind a 50-lot at 0.51
    assert T["res"]["at_cancel"] == {"a": (0.51, 50.0, 0.55, 400.0)}
    tm = rulefit.fit_timing(T)
    assert {k: v["value"] for k, v in tm["trigger_shares"].items()} == {
        "own_fill": 0.3333, "book_move": 0.3333, "timer": 0.3333, "unknown": 0}
    c = tm["cancel"]
    assert c["life_ms"]["value"] == 3000 and c["opposite_best_moved_1c"]["value"] == 1
    assert c["behind_best_at_cancel"]["value"] == 1 and c["replaced_within_1s"]["value"] == 1
    # --asof created: the state when the order was signed (100 ms before it reached the book)
    early = rulefit.decision_table(tmp_path / "tape", tmp_path / "om", tmp_path / "ordertape.jsonl", W, T0,
                                   T0 + 3600, [tmp_path / "tokens.jsonl"], asof="created")["rows"]
    assert [r["cutoff_tw_ns"] - rows[r["id"]]["cutoff_tw_ns"] for r in early] == [-100_000_000] * 4
    # a tape with a ``verified`` column: only orders with a verified row survive
    (tmp_path / "ordertape.jsonl").write_bytes(orjson.dumps(tape[0] | {"verified": True}) + b"\n"
                                               + orjson.dumps(tape[0] | {"order_id": "a", "verified": False}) + b"\n")
    T2 = rulefit.decision_table(tmp_path / "tape", tmp_path / "om", tmp_path / "ordertape.jsonl", W, T0, T0 + 3600,
                                [tmp_path / "tokens.jsonl"])
    assert [r["id"] for r in T2["rows"]] == ["c"] and T2["stats"]["orders_not_verified_dropped"] == 3
    (tmp_path / "ordertape.jsonl").write_bytes(orjson.dumps(tape[0]) + b"\n")
    # too few orders for a rule: says so, still emits a valid draft from schema defaults
    genome, rep, _ = fit(tmp_path)
    assert rep["price_rule"]["bid"]["chosen"] is None and "reason" in rep["price_rule"]["bid"]
    assert genome["status"] == "draft" and validate(from_dict(genome)) == []
