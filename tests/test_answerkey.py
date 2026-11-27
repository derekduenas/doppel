"""Answer-key lab on a synthetic intl tape: policies are deterministic, the truth books itself, the
public evidence carries nothing private, and the whole loop produces a scorecard."""

import os
import re

import numpy as np
import orjson
import pytest

from doppel.lab import answerkey as ak
from doppel.lab.intlbook import IntlBook, LevelEvent
from doppel.tape.segment import SegmentWriter, read_segment

T0 = 1795629600  # hour-aligned, a 5m boundary
NS = 1_000_000_000
L = 300


def tok(k, up):
    return f"{'7' if up else '8'}{k:04d}{'0' * 70}"


def market(k):
    return f"0x{k + 1:064x}"


def synth(root, minutes=30, seed=3, rate_hz=4):
    """Btc 5m windows: a mirrored Up/Down book (bids and asks 1..6 c off a random-walk mid), level
    noise, and prints that hit the touch or sweep a level. Plus raw BRTI, its settlement series and
    tokens.jsonl with resolved payouts."""
    rng = np.random.default_rng(seed)
    w = SegmentWriter(root, "clob_btc_5m")
    steps = int((minutes * 60 + 120) * rate_hz)  # from 60 s before T0 to 60 s after the end
    t_end = T0 + minutes * 60
    mid = 50.0
    state: dict[int, dict] = {}
    pay = {}
    n_tx = 0

    def emit(tw_ns, msgs):
        for m in msgs:
            m["timestamp"] = str(tw_ns // 1_000_000 - 40)
        w.append(orjson.dumps(msgs).decode(), tw=tw_ns)

    def levels(k):
        s = state[k]
        return {("BUY", round(s["mid"] - j - s["half"])): s["sz"][("BUY", j)] for j in range(6)} | {
            ("SELL", round(s["mid"] + j + s["half"])): s["sz"][("SELL", j)] for j in range(6)}

    def pcs(k, lv):  # both views of every Up level
        out = []
        for (side, c), z in lv.items():
            out.append({"asset_id": tok(k, True), "price": f"{c / 100:.2f}", "size": str(z), "side": side})
            out.append({"asset_id": tok(k, False), "price": f"{(100 - c) / 100:.2f}", "size": str(z),
                        "side": "SELL" if side == "BUY" else "BUY"})
        return out

    for i in range(steps):
        tw = (T0 - 60) * NS + int(i * NS / rate_hz)
        t = tw // NS
        k = (t + 30) // L - T0 // L  # the window whose book is live (subscribed 30 s early)
        if k not in state:
            state[k] = {"mid": 50.0, "half": 1, "sz": {(s, j): int(rng.integers(100, 400)) for s in ("BUY", "SELL")
                                                         for j in range(6)}}
            lv = levels(k)
            for up in (True, False):
                bids = [{"price": f"{(c if up else 100 - c) / 100:.2f}", "size": str(z)} for (s, c), z in lv.items()
                        if (s == "BUY") == up]
                asks = [{"price": f"{(c if up else 100 - c) / 100:.2f}", "size": str(z)} for (s, c), z in lv.items()
                        if (s == "SELL") == up]
                emit(tw, [{"event_type": "book", "asset_id": tok(k, up), "market": market(k),
                           "bids": sorted(bids, key=lambda x: x["price"]),
                           "asks": sorted(asks, key=lambda x: x["price"], reverse=True)}])
            continue
        s = state[k]
        old = levels(k)
        if rng.random() < 0.3:
            s["mid"] = float(np.clip(s["mid"] + rng.choice([-1, 1]), 15, 85))
            s["half"] = int(rng.integers(1, 3))
        for key in list(s["sz"]):
            if rng.random() < 0.2:
                s["sz"][key] = int(rng.integers(100, 400))
        new = levels(k)
        ch = {kk: v for kk, v in new.items() if old.get(kk) != v} | {kk: 0 for kk in old if kk not in new}
        msgs = [{"event_type": "price_change", "market": market(k), "price_changes": pcs(k, ch)}] if ch else []
        if rng.random() < 0.5:  # a print at the touch or one level through
            side = "SELL" if rng.random() < 0.5 else "BUY"
            j = 0 if rng.random() < 0.8 else 1
            c = round(s["mid"] - j - s["half"]) if side == "SELL" else round(s["mid"] + j + s["half"])
            q = float(rng.integers(20, 600))
            n_tx += 1
            up = rng.random() < 0.5
            msgs.append({"event_type": "last_trade_price", "asset_id": tok(k, up), "market": market(k),
                         "price": f"{(c if up else 100 - c) / 100:.2f}", "size": str(q), "fee_rate_bps": "0",
                         "side": side if up else ("BUY" if side == "SELL" else "SELL"),
                         "transaction_hash": f"0x{n_tx:064x}"})
            hit = "BUY" if side == "SELL" else "SELL"
            s["sz"][(hit, 0)] = max(10, s["sz"][(hit, 0)] - int(q) // 2)
            c0 = round(s["mid"] - s["half"]) if hit == "BUY" else round(s["mid"] + s["half"])
            msgs.append({"event_type": "price_change", "market": market(k),
                         "price_changes": pcs(k, {(hit, c0): s["sz"][(hit, 0)]})})
        if msgs:
            emit(tw, msgs)
        pay[k] = 1.0 if s["mid"] >= 50 else 0.0
    w.close()
    rows = []
    for k in range(-1, (t_end - T0) // L + 1):
        for up in (True, False):
            p = pay.get(k)
            rows.append({"token": tok(k, up), "slug": f"btc-updown-5m-{T0 + L * k}", "idx": 0 if up else 1,
                         "condition": market(k), "status": "resolved" if p is not None else "open",
                         "payout": (p if up else 1 - p) if p is not None else None})
    (root / "tokens.jsonl").write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in rows))
    lo, hi = T0 - 1200, t_end + 400
    raw = {t: 90000 + float(v) for t, v in zip(range(lo, hi), np.cumsum(rng.normal(0, 3, hi - lo)))}
    b = SegmentWriter(root, "ref_brti_raw")
    for t, v in raw.items():
        b.append(orjson.dumps({"t_ms": t * 1000, "v": str(v), "received": (t + 0.3) * 1000}).decode(),
                 tw=int((t + 0.3) * NS))
    b.close()
    series = {t: sum(raw[x] for x in range(t - 59, t + 1)) / 60 for t in range(lo + 60, hi)}
    r = SegmentWriter(root, "ref_prices")
    # Record live boundary observations before the later full historical response.
    for t, v in series.items():
        if (t + 1) % 300 == 0:
            r.append(orjson.dumps({"series": [{"candles": [{"timestamp": t, "close": v}]}]}).decode(),
                     tw=(t + 1) * NS, meta={"asset": "crypto:btc", "status": 200, "from": t, "to": t})
    r.append(orjson.dumps({"series": [{"candles": [{"timestamp": t, "close": v} for t, v in series.items()]}]}).decode(),
             tw=hi * NS, meta={"asset": "crypto:btc", "status": 200, "from": lo + 60, "to": hi - 1})
    r.close()
    return root


@pytest.fixture(scope="module")
def tape(tmp_path_factory):
    return synth(tmp_path_factory.mktemp("ak") / "tape")


def sim_only(tape, out, key, seed=1, minutes=30):
    return ak.run_strategy(key, tape, tape / "tokens.jsonl", "clob_btc_5m", T0, T0 + minutes * 30, T0 + minutes * 60,
                           out, seed, tools=False)


# ---------------------------------------------------------------- policies: determinism and bookkeeping
@pytest.mark.parametrize("key", list(ak.POLICIES))
def test_policy_is_deterministic_and_books_itself(tape, tmp_path, key):
    a = sim_only(tape, tmp_path / "1", key)
    b = sim_only(tape, tmp_path / "2", key)
    name = a["policy"]
    ta = (tmp_path / "1" / name / "truth" / "orders.jsonl").read_bytes()
    assert ta == (tmp_path / "2" / name / "truth" / "orders.jsonl").read_bytes()
    assert (tmp_path / "1" / name / "public" / "orders.jsonl").read_bytes() == \
        (tmp_path / "2" / name / "public" / "orders.jsonl").read_bytes()
    orders = [orjson.loads(x) for x in ta.splitlines()]
    assert orders, f"{name} placed nothing"
    t = a["truth"]
    assert t["fills"] > 0, f"{name} never filled: {t['status']}"
    for o in orders:
        filled = sum(f[2] for f in o["fills"])
        assert filled <= o["size"] + 1e-6
        assert o["status"] != "censored"
        if o["status"] == "filled" and o["kind"] == "maker":
            assert abs(filled - o["size"]) <= ak.TOL
        if o["status"].startswith("rejected") or o["status"] == "unfilled":
            assert not o["fills"]
        assert o["created_ms"] < (o["land_ms"] or o["created_ms"] + 1)
    # inventory = Up bought - Down bought, per window
    inv: dict = {}
    for o in orders:
        for f in o["fills"]:
            inv[o["slug"]] = inv.get(o["slug"], 0) + (f[2] if o["up"] else -f[2])
    assert {k: round(v, 4) for k, v in inv.items()} == {k: v for k, v in t["inventory_end"].items() if k in inv}
    assert all(v == 0 for k, v in t["inventory_end"].items() if k not in inv)
    sim = a  # every subject's resting size is gone from the book at the end (STOP_S)
    truth = orjson.loads((tmp_path / "1" / name / "truth" / "truth.json").read_bytes())
    assert truth["sim_stats"].get("resting_size_left_at_end", 0) == 0, sim


def test_policies_follow_their_rules(tape, tmp_path):
    rows = {}
    for key in ("a", "c", "d", "f"):
        r = sim_only(tape, tmp_path, key)
        rows[key] = [orjson.loads(x) for x in (tmp_path / r["policy"] / "truth" / "orders.jsonl").read_bytes().splitlines()]
    for o in rows["a"]:  # bid = floor(mid - 2), ask Up = ceil(mid + 2)
        mid = o["ref"]["mid_c"]
        lim = 100 * o["price"] if o["up"] else 100 - 100 * o["price"]
        assert round(lim, 6) == (np.floor(mid - 2 + 1e-9) if o["up"] else np.ceil(mid + 2 - 1e-9))
        assert o["size"] in (10, 25, 50)
    assert {o["size"] for o in rows["d"]} <= {97, 98, 99, 100} and {o["tag"] for o in rows["d"]} == {"L0", "L1", "L2"}
    for o in rows["c"]:
        assert o["ref"]["bb_c"] is not None
    sig = orjson.loads((tmp_path / "hidden_signal" / "truth" / "truth.json").read_bytes())["hidden_signal_by_up_token"]
    for o in rows["f"]:  # one token per window, the signal's
        up = o["token"] if o["up"] else None
        assert o["up"] == sig[up or next(k for k in sig if k[1:5] == o["token"][1:5])]


# ---------------------------------------------------------------- the public evidence
def test_public_evidence_holds_no_cancel_unfilled_order_or_parameter(tape, tmp_path):
    for key in ak.POLICIES:
        r = sim_only(tape, tmp_path, key)
        d = tmp_path / r["policy"]
        truth = [orjson.loads(x) for x in (d / "truth" / "orders.jsonl").read_bytes().splitlines()]
        filled = {o["oid"] for o in truth if o["fills"]}
        hidden = {o["oid"] for o in truth if not o["fills"]}
        pub = [orjson.loads(x) for x in (d / "public" / "orders.jsonl").read_bytes().splitlines()]
        mine = [x for x in pub if x["maker"] == r["wallet"]]
        assert {x["order_id"] for x in mine} == filled
        assert all(x["verified"] is True for x in mine) and all(x["verified"] is False for x in pub if x not in mine)
        assert set(pub[0]) <= set(ak_schema()) and all(set(x) == set(pub[0]) for x in pub)
        assert all(re.fullmatch(r"0x[0-9a-f]{64}", x["tx"]) and re.fullmatch(r"0x[0-9a-f]{40}", x["maker"]) for x in pub)
        # nothing private in any public file (the tape copy, the order tape, the fill store, the lab)
        # decoded content only: compressed tape and parquet bytes can hold any short string by chance
        files = [p for p in (d / "public").rglob("*") if p.is_file() and not p.is_symlink()]
        blob = b"".join(p.read_bytes() for p in files if p.suffix in (".json", ".jsonl"))
        blob += b"".join(rec["raw"].encode() for p in files if p.name.endswith(".dtape.zst") for rec in read_segment(p))
        from doppel.lab.fillstore import iter_wallet_fills
        blob += b"".join(orjson.dumps(rs) for _, rs in iter_wallet_fills(d / "public" / "fills"))
        for word in [*r["truth"].keys(), "cancel", "hidden", "signal", "accuracy", "h_c", "timer_s", "midpoint"]:
            if word not in ("orders", "fills", "status", "sizes", "landed"):
                assert word.encode() not in blob, (key, word)
        for oid in hidden:
            assert oid.encode() not in blob
        # injected frames carry only the keys real frames carry
        keys = {"price_change": {"market", "price_changes", "timestamp", "event_type"},
                "last_trade_price": {"market", "asset_id", "price", "size", "fee_rate_bps", "side", "timestamp",
                                     "event_type", "transaction_hash"},
                "book": {"market", "asset_id", "bids", "asks", "timestamp", "event_type"}}
        for p in sorted((d / "public" / "tape" / "clob_btc_5m").glob("*/*.dtape.zst")):
            for rec in read_segment(p):
                for m in orjson.loads(rec["raw"]):
                    assert set(m) == keys[m["event_type"]], m
                    for c in m.get("price_changes") or []:
                        assert set(c) <= {"asset_id", "price", "size", "side", "hash"}


def ak_schema():
    from doppel.lab.ordertape import SCHEMA
    return SCHEMA.names


def test_injected_book_equals_real_book_plus_resting_size(tape, tmp_path):
    """Replaying the public stream gives, at every level, the real size plus the subject's
    resting remainder; with the subject's orders taken out it is the original book."""
    r = sim_only(tape, tmp_path, "d")
    d = tmp_path / r["policy"]
    truth = [orjson.loads(x) for x in (d / "truth" / "orders.jsonl").read_bytes().splitlines()]
    real, pub = IntlBook(), IntlBook()
    for p in sorted((tape / "clob_btc_5m").glob("*/*.dtape.zst")):
        for rec in read_segment(p):
            real.feed(rec)
    for p in sorted((d / "public" / "tape" / "clob_btc_5m").glob("*/*.dtape.zst")):
        for rec in read_segment(p):
            pub.feed(rec)
    # at the end every quote is gone: the two books agree level by level (prints only shrink, they never add depth)
    assert pub.levels
    for a, lv in pub.levels.items():  # the public copy holds only the hours of the range
        assert lv == real.levels[a]
    # placements show as exact-size increases at the signed price
    incs = set()
    book = IntlBook()
    for p in sorted((d / "public" / "tape" / "clob_btc_5m").glob("*/*.dtape.zst")):
        for rec in read_segment(p):
            for it in book.feed(rec):
                if isinstance(it, LevelEvent) and it.delta > 0:
                    incs.add((it.asset, it.price, it.side, round(it.delta, 6)))
    landed = [o for o in truth if o["land_ms"] and o["status"] in ("filled", "cancelled")]
    assert landed and all((o["token"], o["price"], "BUY", o["size"]) in incs for o in landed)


def test_a_book_cross_fill_is_published_once(tape, tmp_path):
    """The opposite side reaching a resting quote is already in the public book. Its print carries
    the tx and the time with size 0, so a replay that fills on the crossed book does not fill again
    on the print. And the wallet's own quotes never cross each other in the public book."""
    r = sim_only(tape, tmp_path, "b")
    d = tmp_path / r["policy"]
    truth = [orjson.loads(x) for x in (d / "truth" / "orders.jsonl").read_bytes().splitlines()]
    cross = {f[3]: f[2] for o in truth for f in o["fills"] if f[4] == "book_cross"}
    trade = {f[3]: f[2] for o in truth for f in o["fills"] if f[4] == "trade"}
    assert cross and trade and r["truth"]["fill_via"]["book_cross"] == len(cross)
    seen = {}
    for p in sorted((d / "public" / "tape" / "clob_btc_5m").glob("*/*.dtape.zst")):
        for rec in read_segment(p):
            for m in orjson.loads(rec["raw"]):
                if m["event_type"] == "last_trade_price" and m["transaction_hash"] in cross | trade:
                    assert m["transaction_hash"] not in seen  # one print per fill
                    seen[m["transaction_hash"]] = float(m["size"])
    assert seen == {tx: 0.0 for tx in cross} | trade
    # the order tape still holds the cross fill with its real quantity: it is on chain
    pub = [orjson.loads(x) for x in (d / "public" / "orders.jsonl").read_bytes().splitlines()]
    assert {x["tx"] for x in pub if x["maker"] == r["wallet"]} == set(cross) | set(trade)
    # own quotes on the two tokens: the best Up bid stays under the Up ask its own Down bid makes
    r = sim_only(tape, tmp_path, "d")
    orders = [orjson.loads(x) for x in (tmp_path / r["policy"] / "truth" / "orders.jsonl").read_bytes().splitlines()]
    live = sorted((o for o in orders if o["land_ms"] and o["end_ms"] and o["status"] in ("filled", "cancelled")),
                  key=lambda o: o["land_ms"])
    by = {}
    for o in live:
        by.setdefault(o["slug"], []).append(o)
    n = 0
    for os_ in by.values():
        ups, downs = [o for o in os_ if o["up"]], [o for o in os_ if not o["up"]]
        for u in ups:
            for dn in downs:
                if max(u["land_ms"], dn["land_ms"]) < min(u["end_ms"], dn["end_ms"]):
                    n += 1
                    assert u["price"] + dn["price"] < 1 - 1e-9, (u, dn)
    assert n > 100


# ---------------------------------------------------------------- the whole loop
def test_end_to_end_scorecard(tape, tmp_path):
    out = tmp_path / "run"
    rc = ak.main(["--data", str(tape), "--tokens", str(tape / "tokens.jsonl"), "--stream", "clob_btc_5m",
                  "--from", str(T0), "--to", str(T0 + 1800), "--strategies", "a,b,e,f", "--out", str(out)])
    assert rc == 0
    card = orjson.loads((out / "scorecard.json").read_bytes())
    assert set(card["cells"]) == {"midpoint_maker", "fair_maker", "random_taker", "hidden_signal"}
    # a holdout of 3 windows cannot test fidelity: the card says so, and no gate-INCONCLUSIVE counts as an answer
    assert card["holdout_window_starts"] == 3 and card["fidelity_readable"] is False
    assert "NO fidelity cell of this run can be read" in (out / "scorecard.md").read_text()
    states = {k: row["fidelity_verdict"]["state"] for k, row in card["cells"].items()}
    assert set(states.values()) <= {"gate_inconclusive", "not_run"} and card["totals"]["fidelity_states"]
    for k, row in card["cells"].items():
        c = row["fidelity_verdict"]
        assert c["category"] in ("gate_inconclusive", "not_scored", "correctly_unknown", "missed"), (k, c)
        assert c["category"] != "correctly_unknown" or c["state"] == "not_run"
    # Three short training windows lack observations to test these controls independently.
    # Absence of a draft cannot earn credit for a successful negative control.
    for k, key in (("random_taker", "e"), ("hidden_signal", "f")):
        hd = card["hidden_and_decoy"][k]
        assert hd["rulefit_genome_emitted"] is False and hd["rulefit_says_explained"] is None and hd["rulefit_reasons"]
        assert card["cells"][k]["explained_by_observed_inputs"]["category"] == "missed"
        assert hd["fidelity_state"] == "not_run" and hd["fidelity_passed"] is False
        assert not (out / k / "tools" / "rulefit" / "genome.json").exists()
    assert card["strategies"]["e"]["tools"]["exit"]["rulefit"] == 0
    rep = orjson.loads((out / "random_taker" / "tools" / "rulefit" / "fit_report.json").read_bytes())
    assert rep["taker_rule"]["found"] is False and rep["taker_rule"]["n_takes"] > 20
    # every timing cell states its bias, is hedged, and none is confidently wrong
    for k in ("midpoint_maker", "fair_maker", "hidden_signal"):
        for ob in ("cadence", "cancel_rule"):
            c = card["cells"][k][ob]
            assert c["status"] != "claim" and c["category"] != "wrongly_confident", (k, ob, c)
    assert card["cells"]["midpoint_maker"]["ladder"]["recovered"] is not True  # no false ladder from cancel/replace
    for k in "abf":
        assert "book_cross" not in card["strategies"][k]["pipeline"]["pessimistic_overfill_causes"]
    for row in card["cells"].values():
        assert set(row) == set(ak.OBLIGATIONS)
        for c in row.values():
            assert c["category"] in ak.SHORT
    t = card["totals"]
    assert 0 <= t["recovery_accuracy"] <= 1 and t["cells_with_true_value"] > 0
    assert "| midpoint_maker |" in (out / "scorecard.md").read_text()
    # the decoy never comes out as a validated rule
    assert card["cells"]["random_taker"]["fidelity_verdict"]["category"] != "wrongly_confident"
    assert card["cells"]["random_taker"]["explained_by_observed_inputs"]["category"] != "wrongly_confident"
    # the tools ran on the public directory only and found the subject's orders
    a = card["strategies"]["a"]
    assert a["tools"]["exit"]["ordermatch"] == 0 and a["tools"]["exit"]["profile"] == 0
    maker = out / "midpoint_maker"
    train = (maker / "public/train-orders.jsonl").read_bytes().splitlines()
    all_orders = (maker / "public/orders.jsonl").read_bytes().splitlines()
    split = card["range"][1]
    assert 0 < len(train) < len(all_orders)
    assert train == [line for line in all_orders if orjson.loads(line)["block_ts"] < split]
    train_match = orjson.loads((maker / "tools/ordermatch-train/ordermatch.json").read_bytes())
    assert (train_match["params"]["from"], train_match["params"]["to"]) == (T0, split)
    assert a["pipeline"]["ordermatch_matched"] > 0
    assert a["pipeline"]["ordermatch_match_rate"] >= 0.9
    assert set(card["hidden_and_decoy"]) == {"random_taker", "hidden_signal"}


def test_categories():
    c = ak._cell
    assert c(None, None, "unknown", None)["category"] == "correctly_unknown"
    assert c(None, 3, "claim", None)["category"] == "wrongly_confident"
    assert c(None, 3, "hedged", None)["category"] == "wrong"
    assert c(2, None, "unknown", None)["category"] == "missed"
    assert c(2, 2, "claim", True)["category"] == "correct"
    assert c(2, 5, "hedged", False, True)["category"] == "wrong"
    assert c(2, 5, "hedged", False, False)["category"] == "wrongly_confident"
    assert c(ak.NA, 5, "claim", False)["category"] == "not_scored"
    rows = {"s": {"x": c(2, 2, "claim", True), "y": c(None, 3, "claim", None), "z": c(2, None, "unknown", None)}}
    t = ak.totals(rows)
    assert t["recovery_accuracy"] == 0.5 and t["false_confidence_rate"] == 0.5
    # a fidelity run stopped by a data gate is in no rate: neither correct nor a miss
    rows["s"]["fidelity_verdict"] = c(True, None, "unknown", None) | {"category": "gate_inconclusive", "state": "gate_inconclusive"}
    t2 = ak.totals(rows)
    assert (t2["recovery_accuracy"], t2["cells_with_true_value"]) == (t["recovery_accuracy"], t["cells_with_true_value"])
    assert t2["fidelity_states"] == {"gate_inconclusive": 1} and t2["categories"]["gate_inconclusive"] == 1


def test_fidelity_and_timing_cells_score_what_the_tools_state():
    exp = ak.EXPECT
    t = {"sizes": [20.0], "skew_c_per_contract": 0.075, "offset_c_p50": {"bid": -3.0, "ask": 3.0}, "repost_gap_s_p50": 1.0, "cancel_life_ms_p50": 900.0,
         "fill_ratio": 0.2, "taker_share_of_shares": 0.0, "unfilled_cancel_life_ms_p50": 900.0,
         "rest_ms_p50": {"filled": None, "cancelled_after_fill": None}}
    led = {"obligations": {"placement_cadence": {"status": "ESTIMATED"}, "cancel_replace": {"status": "ESTIMATED"}}}

    def cells(gap, life, ladder=None, fid=None, genome=None, explained=None):
        rf = {"timing_rule": {"gap_s": gap, "cancel": {"life_ms": life}}, "ladder": ladder or {},
              "explained_by_observed_inputs": explained or {}}
        return ak.score("fair_maker", t, {"profile": led, "rulefit": rf, "fidelity": fid, "genome": genome})

    est = {"value": 1.2, "status": "ESTIMATED", "ci95": [0.6, 2.0]}
    c = cells(est, {"value": 5000.0, "status": "UPPER_BOUND", "ci95": None, "upper_bound": 6000.0})
    assert c["cadence"]["category"] == "correct" and c["cadence"]["ci_covers_truth"] is True
    assert c["cancel_rule"]["category"] == "wrong" and c["cancel_rule"]["bound_holds"] is True  # hedged, bound holds
    # a stated interval or bound that excludes the truth is a confident error, whatever the status says
    c = cells(est | {"value": 3.0, "ci95": [2.5, 3.5]}, {"value": 500.0, "status": "UPPER_BOUND", "upper_bound": 600.0})
    assert c["cadence"]["category"] == c["cancel_rule"]["category"] == "wrongly_confident"
    assert c["skew"]["truth"] == 0.075
    c = cells({"value": 3.0, "status": "UNKNOWN"}, None)  # evidence shown, nothing stated
    assert c["cadence"]["category"] == c["cancel_rule"]["category"] == "missed"
    # ladder: "ladder" is a claim, "no_ladder_seen" is hedged, anything else is unknown
    assert cells(est, None, {"status": "ladder"})["ladder"]["category"] == "wrongly_confident"   # fair_maker has none
    assert cells(est, None, {"status": "no_ladder_seen"})["ladder"]["category"] == "correct"
    assert cells(est, None, {"status": "unknown"})["ladder"]["category"] == "missed"
    assert exp["fair_maker"]["expressible"] is True
    gate = {"verdict": {"decision": "INCONCLUSIVE", "reason": "data gate: comparable_starts 3 outside [30, inf]"}}
    open_ = {"verdict": {"decision": "INCONCLUSIVE", "reason": "binding: pnl.corr"}}
    assert cells(est, None, fid=gate)["fidelity_verdict"]["category"] == "gate_inconclusive"
    assert cells(est, None, fid=open_)["fidelity_verdict"] | {} and cells(est, None, fid=open_)["fidelity_verdict"]["state"] == "inconclusive"
    assert cells(est, None, fid=open_)["fidelity_verdict"]["category"] == "missed"
    assert cells(est, None)["fidelity_verdict"]["state"] == "not_run"
    assert cells(est, None, fid={"verdict": {"decision": "PASS", "reason": ""}})["fidelity_verdict"]["category"] == "correct"
    # explained: what rulefit states wins over the mere presence of a genome file
    c = cells(est, None, genome={"x": 1}, explained={"verdict": False, "reasons": ["side"]})
    assert c["explained_by_observed_inputs"]["recovered"] is False and c["explained_by_observed_inputs"]["category"] == "wrong"


def test_tool_rss_is_read_in_megabytes(tmp_path):
    rc, mb = ak.run_tool("profile", ["--help"], tmp_path / "log")
    assert rc == 0 and 5 < mb < 1500, mb
    assert os.path.getsize(tmp_path / "log") > 0


def test_confidence_denominator_and_failed_tool_admission():
    cells = {"one": ak._cell(1, 0.98, "hedged", True, False),
             "two": ak._cell(1, 0.95, "hedged", True, False),
             "three": ak._cell(1, 1, "claim", True)}
    counts = ak.totals({"fixture": cells})
    assert counts["confident_claims"] == 3
    assert counts["categories"]["wrongly_confident"] == 2
    assert counts["false_confidence_rate"] == 0.6667
    for name in ("hidden_signal", "random_taker"):
        with pytest.raises(ValueError, match="without its rule-fit report"):
            ak.score(name, {}, {"exit": {"rulefit": 1}, "rulefit": None})
        with pytest.raises(ValueError, match="ordermatch exited"):
            ak.score(name, {}, {"exit": {"ordermatch": 1}, "rulefit": {"explained_by_observed_inputs": {"verdict": False}}})
    assert ak.documented_no_draft({"genome": {"status": "not_emitted", "reasons": ["side unmeasured"]},
                                   "explained_by_observed_inputs": {"verdict": None}})
    assert not ak.documented_no_draft({"explained_by_observed_inputs": {"verdict": False}})


def test_params_are_frozen_and_reject_invalid_values():
    assert ak.policy_overrides(b'{"b":{"k_c_per_contract":0.075}}') == {"b": {"k_c_per_contract": 0.075}}
    for value in (b'{"b":{"size":false}}', b'{"a":{"timer_s":0}}', b'{"d":{"levels_c":[0,0]}}', b'{"x":{}}'):
        with pytest.raises(ValueError):
            ak.policy_overrides(value)


def test_selected_parameter_uses_the_matching_interval():
    side = {"model": "skew", "rule": {"offset_c": 3, "skew_c_per_contract": 0.05},
            "offset_c": {"ci95": [-4, -2]}, "skew_c_per_contract": {"ci95": [0.04, 0.06]}}
    assert ak.selected_parameter(side, "offset_c") == (3, None)
    assert ak.selected_parameter(side, "skew_c_per_contract") == (0.05, [0.04, 0.06])
    side["selected_parameters"] = {"offset_c": {"value": 3, "ci95": [2.9, 3.1]}}
    assert ak.selected_parameter(side, "offset_c") == (3, [2.9, 3.1])
    side = {"model": "const", "rule": {"skew_c_per_contract": 0}, "skew_c_per_contract": {"ci95": [2, 3]}}
    assert ak.selected_parameter(side, "skew_c_per_contract") == (0, None)
    cell = ak._timing_cell(3, {"status": "ESTIMATED", "value": 2, "ci95": [1, 4], "upper_bound": 2.5},
                           {"status": "ESTIMATED"})
    assert cell["category"] == "wrongly_confident" and cell["bound_holds"] is False
    assert ak._covers([1., 1.], 0.9999999999999929) and not ak._covers([1., 1.], 0.9999)
    args = ({"bid": -1, "ask": 1}, ["bid", "ask"], {"bid": 0}, [True])
    assert ak.price_parameter_cell(*args, [False])["category"] == "wrongly_confident"
    assert ak.price_parameter_cell(*args, [True])["category"] == "missed"
