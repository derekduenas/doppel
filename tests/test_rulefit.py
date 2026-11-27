"""Rule fit on a synthetic wallet whose quoting rule is known exactly.

The wallet re-posts every 2 s in each 5m window: a bid joining the best bid (BUY Up) and an ask at
round(mid + 2 - 0.05 * inventory) placed as a BUY on the Down token; sizes 97..100. Spread, mid and
inventory vary independently, so touch, mid and the skew are separately identified."""

import dataclasses
import hashlib
import math
import sys
import time
import tracemalloc
from types import SimpleNamespace

import numpy as np
import orjson
import pytest

from doppel.lab import rulefit
from doppel.lab.rulefit import STATE_COLS, Fair, Inventory, main, run, thresholds, up_order, up_view
from doppel.strategy.genome import from_dict, validate
from doppel.strategy.registry import Registry, RegistryError
from doppel.tape.segment import SegmentWriter

T0 = 1795629600  # hour-aligned, a 5m boundary
MS = T0 * 1000
NS = 1_000_000_000
W = "0xabc0000000000000000000000000000000000001"
L = 300


def slug(k, asset="btc"):
    return f"{asset}-updown-5m-{T0 + L * k}"


def book(ms, asset, bids, asks, market):
    lv = lambda xs: [{"price": f"{p:.2f}", "size": str(s)} for p, s in xs]  # noqa: E731
    return ms, {"event_type": "book", "asset_id": asset, "market": market, "bids": lv(bids),
                "asks": lv(asks), "timestamp": str(MS + ms)}


def pc(ms, asset, price, size, side, market, ts=None):
    return ms, {"event_type": "price_change", "market": market, "timestamp": str(MS + (ms if ts is None else ts)),
                "price_changes": [{"asset_id": asset, "price": f"{price:.2f}", "size": str(size), "side": side}]}


def trade(ms, asset, price, size, tx, market):
    return ms, {"event_type": "last_trade_price", "asset_id": asset, "market": market, "price": f"{price:.2f}",
                "size": str(size), "side": "SELL", "timestamp": str(MS + ms), "transaction_hash": tx}


def tape_row(tx, order_id, token, price, size, q, block_ts, created_ms):
    return {"tx": tx, "block": 1, "block_ts": block_ts, "role": "maker", "order_id": order_id, "maker": W,
            "token_id": token, "side": "BUY", "size": size, "limit": price,
            "maker_amount": round(size * price * 1e6), "taker_amount": round(size * 1e6),
            "fill_amount": round(q * price * 1e6), "created_ms": created_ms, "salt": "1"}


def world(seed=1, windows=6, candidates=0, ask_fn=None, observe_open=True):
    """frames, ordermatch rows, order-tape rows, candidate rows. ``ask_fn(k, ms, bb, ba, inv)`` ->
    the ask in Up cents (default: round(mid + 2 - 0.05 * inv))."""
    rng = np.random.default_rng(seed)
    frames, orders, tape, quotes = [], [], [], []
    n = 0
    for k in range(windows):
        U, D, mk = f"U{k}", f"D{k}", f"m{k}"
        inv, bb = 0.0, int(rng.integers(35, 60))
        if observe_open:
            # The policy waits until +10 s. Publish its book before the first decision so
            # participation can distinguish a quiet cell from one with missing data.
            for tok in (U, D):
                frames.append(book(k * L * 1000 - 500, tok, [(0.5, 500)], [(0.52, 500)], mk))
        for t in range(k * L * 1000 + 10_000, (k + 1) * L * 1000 - 8_000, 2000):
            bb = int(np.clip(bb + rng.integers(-1, 2), 25, 70))
            ba = bb + int(rng.integers(1, 5))
            frames.append(book(t, U, [(bb / 100, 500), ((bb - 5) / 100, 300)], [(ba / 100, 400)], mk))
            frames.append(book(t, D, [((100 - ba) / 100, 400)], [((100 - bb) / 100, 500)], mk))
            ask = int(round((bb + ba) / 2 + 2 - 0.05 * inv)) if ask_fn is None else int(ask_fn(k, t + 310, bb, ba, inv))
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
                     "rest_min_ms": 1500, "rest_max_ms": 1500, "rest_known": True,
                     "placement_evidence": "unique_candidate", "placement_candidates": 1, "fill_before_placement": False}
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
            tape.append(tape_row(tx, o["order_id"], o["token_id"], o["price"], o["size"], q,
                                 T0 + (t + 600) // 1000 + 2, o["created_ms"]))
    return frames, orders, tape, quotes


def jl(rows):
    return b"".join(orjson.dumps(r) + b"\n" for r in rows)


def fixture_match_controls(orders):
    """Assume perfect placements only for tests isolating model fit on the synthetic known-rule world."""
    n = len(orders)
    return {"params": {"from": T0, "to": T0 + 3600, "placement_evidence_version": rulefit.PLACEMENT_EVIDENCE_VERSION},
            "wallets": {W: {"placement_evidence_counts": {"unique_candidate": n},
                            "observable": n, "matched": n, "placebo_matched": 0}}}


def write(tmp, frames, orders, tape, quotes=None, om_doc=None, windows=6, asset="btc"):
    tmp.mkdir(parents=True, exist_ok=True)
    w = SegmentWriter(tmp / "tape", f"clob_{asset}_5m")
    for ms, m in sorted(frames, key=lambda f: f[0]):
        w.append(orjson.dumps([m]).decode(), tw=(MS + ms) * 1_000_000)
    w.close()
    om = tmp / "om"
    om.mkdir(exist_ok=True)
    (om / "orders.jsonl").write_bytes(jl(orders))
    if quotes is not None:
        (om / "quotes.jsonl").write_bytes(jl(quotes))
    if om_doc is not None:
        (om / "ordermatch.json").write_bytes(orjson.dumps(om_doc))
    (tmp / "ordertape.jsonl").write_bytes(jl(tape))
    (tmp / "tokens.jsonl").write_bytes(jl([{"token": f"{'UD'[i]}{k}", "slug": slug(k, asset), "idx": i,
                                            "status": "open", "known_tw_ns": (T0 - 3600) * NS,
                                            "witness_source": "answerkey_config"} for k in range(windows) for i in (0, 1)]))
    (tmp / "coverage_receipt.json").write_bytes(orjson.dumps({"wallet": W, "simulated_flat_start_fills": {
        "ordertape.jsonl": {"from": T0, "to": T0 + 3600, "sha256": hashlib.sha256(jl(tape)).hexdigest()}},
        "simulated_market_universe": {"source": "answerkey_config", "configured_tw_ns": (T0 - 3600) * NS,
                                      "from": T0, "to": T0 + 3600,
                                      "sha256": hashlib.sha256((tmp / "tokens.jsonl").read_bytes()).hexdigest()}}))


def brti_lab(lab, lo=T0 - 900, hi=T0 + 1300, bump_received_after=None):
    """Raw BRTI received 0.3 s after each second, plus the settlement series. With
    ``bump_received_after`` (unix s) every value received later than that is moved by 500."""
    rng = np.random.default_rng(7)
    raw = {t: 90000 + float(v) for t, v in zip(range(lo, hi), np.cumsum(rng.normal(0, 3, hi - lo)))}
    series = {t: sum(raw[s] for s in range(t - 59, t + 1)) / 60 for t in range(lo + 60, hi)}
    w = SegmentWriter(lab, "ref_brti_raw")
    for t, v in raw.items():
        if bump_received_after is not None and t + 0.3 > bump_received_after:
            v += 500
        w.append(orjson.dumps({"t_ms": t * 1000, "v": str(v), "received": (t + 0.3) * 1000}).decode(),
                 tw=int((t + 0.3) * NS))
    w.close()
    w = SegmentWriter(lab, "ref_prices")
    # Record live boundary observations before the later full historical response.
    for t, v in series.items():
        if (t + 1) % 300 == 0:
            w.append(orjson.dumps({"series": [{"candles": [{"timestamp": t, "close": v}]}]}).decode(),
                     tw=(t + 1) * NS, meta={"asset": "crypto:btc", "status": 200, "from": t, "to": t})
    w.append(orjson.dumps({"series": [{"candles": [{"timestamp": t, "close": v} for t, v in series.items()]}]}).decode(),
             tw=hi * NS, meta={"asset": "crypto:btc", "status": 200, "from": lo + 60, "to": hi - 1})
    w.close()
    return lab


def fit(tmp, out=None, **kw):
    return run(tmp / "tape", tmp / "om", tmp / "ordertape.jsonl", W, T0, T0 + 3600, out,
               token_files=[tmp / "tokens.jsonl"], **kw)


@pytest.fixture(scope="module")
def fitted(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rulefit")
    frames, orders, fills, _ = world()
    write(tmp, frames, orders, fills, om_doc=fixture_match_controls(orders))
    return fit(tmp, tmp / "out") + (tmp,)


# ---------------------------------------------------------------- the rule
def test_known_rule_is_recovered(fitted):
    genome, rep, rows, _ = fitted
    assert rep["label"] == "rule conditional on fill" and "0.3%" in rep["selection_caveat"]
    bid, ask = rep["price_rule"]["bid"], rep["price_rule"]["ask"]
    # bid = best bid exactly
    assert (bid["chosen"], bid["tie_with"]) == ("touch", "mid")
    assert bid["runner_up"]["ci95"] is None  # only one validation window
    assert bid["offset_c"]["value"] == 0 and bid["offset_c"]["iqr"] == 0 and bid["offset_c"]["oos"]["mae_c"] == 0
    assert bid["within_1c_of_const_rule"]["value"] == 1 and bid["offset_c"]["oos"]["within_1c"] == 1
    assert bid["offset_c"]["ci95"] == [0, 0] and bid["offset_c"]["clusters"] == 6
    # ask = mid + 2 - 0.05 * inventory (cent rounding leaves ~0.25 c of error)
    assert (ask["chosen"], ask["model"], ask["tie_with"]) == ("mid", "skew", "touch")
    assert abs(ask["intercept_c_with_skew"] - 2) < 0.1 and 0.2 < ask["offset_c"]["oos"]["mae_c"] < 0.3
    k = ask["skew_c_per_contract"]
    assert abs(k["value"] - 0.05) < 0.002 and 0.045 < k["ci95"][0] <= k["value"] <= k["ci95"][1] < 0.055
    assert k["oos"]["val_mae_c_with_skew"] < 0.35 < k["oos"]["val_mae_c_const"]
    assert ask["candidates"]["touch"]["val_mae_skew_c"] > 2 * ask["candidates"]["mid"]["val_mae_skew_c"]
    assert ask["inventory_range"][1] - ask["inventory_range"][0] > 60
    assert ask["n_fit"] + ask["n_val"] + ask["n_test"] == ask["n"] and ask["n_val"] > 50
    # every ask was a BUY on the Down token: Up terms
    assert {r["up_side"] for r in rows if r["idx"] == 1} == {"ask"} and bid["n_rows"] == ask["n_rows"] == len(rows) // 2
    # size, timing
    size = rep["size_rule"]
    assert size["descriptor"]["rule"] == "fixed_sizes" and set(size["descriptor"]["sizes_90"]) <= {97.0, 98.0, 99.0, 100.0}
    assert size["train_size_set_covers_test"]["value"] == 1 and 97 <= size["median_size"]["value"] <= 100
    gap = rep["timing_rule"]["gap_s"]
    # no fingerprint and no candidates: nothing corrects the gap between FILLED orders, so it is an
    # upper bound with its bias named and NO confidence interval
    assert gap["value"] == 2 and gap["ci95"] is None and gap["status"] == "UPPER_BOUND" and gap["upper_bound"] == 2
    assert gap["bias"] == "fill-conditional upper bound" and gap["filled"]["ci95"] is None
    assert gap["filled"]["oos"]["holdout_value"] == 2
    shares = {k: v["value"] for k, v in rep["timing_rule"]["trigger_shares"].items()}
    assert abs(sum(shares.values()) - 1) < 1e-3 and shares["own_fill"] > 0.3 and shares["unknown"] == 0
    life = rep["timing_rule"]["cancel"]["life_ms"]
    assert life["value"] == 1500 and life["ci95"] is None and life["bias"] == "length-biased"
    assert life["status"] == "UPPER_BOUND" and life["upper_bound"] == 1500
    part = rep["participation_rule"]
    assert part["windows_seen"] == part["windows_with_orders"] == 6 and part["presence_rate"]["value"] > 0.9
    assert part["both_sides_share_when_present"]["value"] == 1 and part["pre_open_order_share"] == 0
    assert rep["ladder"]["ladder_share"]["value"] == 0 and rep["ladder"]["depth_max"] == 1
    assert rep["ladder"]["status"] == "no_ladder_seen"
    ex = rep["explained_by_observed_inputs"]  # quotes both sides in every window: exempt from the side test
    assert ex["verdict"] is True and ex["side"]["kind"] == "two_sided" and ex["price"]["explained"] is True
    assert ex["price"]["value"] > 0.5 and ex["timing"]["null"]


def test_genome_is_a_valid_draft_that_acts_like_the_fit_and_cannot_be_registered(fitted, tmp_path):
    genome, rep, rows, tmp = fitted
    assert genome["status"] == "draft" and rep["genome"]["registered"] is False and rep["genome"]["validate"] == []
    g = from_dict(orjson.loads((tmp / "out" / "genome.json").read_bytes()))
    assert validate(g) == [] and g.id == rep["genome"]["id"] and rep["genome"]["reasons"] == []
    assert g.source.wallets == (W,) and g.source.parents == () and g.universe.venue == "intl_sim"
    assert g.universe.assets == ("btc",) and g.universe.horizons == ("5m",)
    assert g.evidence["wallet"] == W and g.evidence["label"] == "rule conditional on fill"
    assert g.evidence["fit_range"] == {"from": T0, "to": T0 + 3600}
    m = g.maker
    assert m is not None and m.quote_ref == "mid" and m.sides == "both" and 97 <= m.size <= 100
    assert 1.5 < m.half_spread_c < 2.5 and 0.015 < m.skew_c_per_contract < 0.035 and m.max_quote_age_s == 2
    # the registry refuses the draft status (main 7778177): nothing rulefit writes can seal itself
    reg = Registry(tmp_path)
    with pytest.raises(RegistryError, match="status 'draft' cannot be registered"):
        reg.register(orjson.loads((tmp / "out" / "genome.json").read_bytes()), "rulefit-test", "h",
                     (time.time() + 3600, time.time() + 7200))
    assert not reg.path.exists() or reg.lines() == []
    # every number that reaches the genome carries n, a window-clustered CI and a holdout score
    e, part = rep["expressible_price_rule"], rep["participation_rule"]
    fitted_numbers = [e["half_spread_fit"], e["centre_shift_fit"], e["skew_fit"], rep["size_rule"]["median_size"],
                      part["tte_s_p1"], part["tau_s_p1"]]
    # ... except the quote life: its sample is length-biased, so it says so and carries no CI
    assert rep["timing_rule"]["cancel"]["life_ms_p90"] == {"value": 1500, "method": "cancels of partly filled orders",
                                                           "bias": "length-biased"}
    assert all(x["n"] > 100 and x["clusters"] == 6 and len(x["ci95"]) == 2 and x["oos"] for x in fitted_numbers)
    assert e["half_spread_fit"]["ci95"][0] <= m.half_spread_c <= e["half_spread_fit"]["ci95"][1]
    # the printed rule is the selected one: intercept at zero inventory with the skew, not the raw median
    assert g.evidence["price_rule"]["ask"]["rule"] == rep["price_rule"]["ask"]["rule"]
    u = rep["price_rule"]["ask"]["rule"]
    assert u["ref"] == "mid" and abs(u["offset_c"] - 2) < 0.1 and abs(u["skew_c_per_contract"] - 0.05) < 0.002
    assert rep["price_rule"]["bid"]["rule"] | {"fitted_on": 0} == {"ref": "touch", "offset_c": 0,
                                                                  "skew_c_per_contract": 0, "fitted_on": 0}
    card = (tmp / "out" / "rule.md").read_text()
    assert f"ask = mid {u['offset_c']:+g} c - {u['skew_c_per_contract']:g} c x inventory" in card

    # universe from p1, not p10: it keeps ~99 % of the wallet's own orders (was ~80 %)
    uni = rep["universe_fit"]
    assert g.universe.tte_min_s == uni["tte_min_s"] == int(part["tte_s_p1"]["value"])
    assert uni["own_orders_excluded_share"] <= 0.025
    # risk measured from the wallet, so one fill does not silence a side (default max_inventory is 50)
    rf = rep["risk_fit"]
    assert g.risk.max_inventory == rf["max_inventory"] >= max(m.size, rf["abs_inventory_at_placement"]["p99"])
    assert g.risk.max_notional_per_window == math.ceil(rf["window_opening_notional_usd"]["p95"])
    # The emitted genome's static intent check describes the same observations used to fit it.
    ck = rep["emitted_genome_check"]
    assert ck["would_quote_share"] >= 0.95 and ck["fair_substituted_by_mid"] is True
    assert "in-sample" in ck["note"].lower()
    test = [r for r in rows if r["window_start_ns"] >= rep["inputs"]["split"]["t_test_ns"]]
    errs = []
    for r in rows:
        b, a = g.maker_quotes({"mid_up_c": r["mid_c"], "fair_up": r["mid_c"] / 100, "inventory_up": r["inv"],
                               "tau_s": r["tau_s"], "tte_s": r["tte_s"]})
        q = b if r["up_side"] == "bid" else a
        if q is not None:
            errs.append(abs(q - r["px_c"]))
    assert ck["rows"] == len(rows) and ck["would_quote"] <= len(errs)
    assert len(errs) - ck["would_quote"] == sum(ck["silent_by"].get(k, 0)
                                                for k in ("no_room", "would_cross_book"))
    assert ck["mae_c_when_quoting"] < 1.2 and sum(ck["silent_by"].values()) == ck["rows"] - ck["would_quote"]
    # with the schema-default risk the same rule goes silent on the rows where |inventory| >= 50
    capped = rulefit.check_genome(dataclasses.replace(g, risk=dataclasses.replace(g.risk, max_inventory=50.0)),
                                  rows, False)
    assert capped["would_quote"] < ck["would_quote"] and capped["silent_by"]["inventory_cap_or_side"] > 0

    items = {c["item"]: c for c in rep["cannot_express"]}
    assert "bid: quote pegged to the same-side best" in items and "a different reference per side" in items
    assert "size drawn from a set / randomised" in items and "status draft" in items
    assert "inventory skew differs by side" in items  # the bid has none, the ask 0.05
    cost = items["cost of the schema on the price rule"]["measured"]
    assert cost["oos_cost_of_schema_c"] > 0 and cost["n_test_common"] == e["n_test"] == len(test)
    assert "bid: several price levels" not in items
    # same inputs -> same id (evidence is part of the id: no clock, seeded bootstrap)
    assert fit(tmp)[0] == genome
    assert sorted(p.name for p in (tmp / "out").iterdir()) == ["decision_table.jsonl", "fit_report.json",
                                                              "genome.json", "rule.md"]


def test_selection_never_sees_the_test_slice(fitted):
    """Reference, model and keep-skew are chosen on the validation slice; the test slice is scored
    once. Wrecking every test-slice order must change the test MAE and nothing that was chosen."""
    rows = [r for r in fitted[2] if r["kind"] == "order"]
    th = thresholds(rows)
    assert th == (fitted[1]["inputs"]["split"]["t_val_ns"], fitted[1]["inputs"]["split"]["t_test_ns"])
    assert th == ((T0 + 3 * L) * NS, (T0 + 4 * L) * NS)
    bad = [r | {k: r[k] + 9 for k in ("px_c", "d_touch_c", "d_mid_c")}
           if r["window_start_ns"] >= th[1] else r for r in rows]
    a, b = rulefit.fit_price(rows, th), rulefit.fit_price(bad, th)
    for s in ("bid", "ask"):
        for k in ("chosen", "model", "tie_with", "candidates", "train_rule", "runner_up"):
            assert a[s][k] == b[s][k], (s, k)
        assert abs(b[s]["offset_c"]["oos"]["mae_c"] - a[s]["offset_c"]["oos"]["mae_c"] - 9) < 0.6
    ea, eb = rulefit.fit_expressible(rows, a, th), rulefit.fit_expressible(bad, b, th)
    assert all(ea[k] == eb[k] for k in ("quote_ref", "skew_kept", "validation_mae_c"))
    assert eb["oos_mae_c"] > ea["oos_mae_c"] + 4
    # ... and wrecking the validation slice does change the choice (the test above is not vacuous)
    worse = [r | {"d_mid_c": r["d_mid_c"] + (r["inv"] or 0)} if th[0] <= r["window_start_ns"] < th[1] else r
             for r in rows]
    assert rulefit.fit_price(worse, th)["ask"]["chosen"] != "mid"

    # A reference known only in test windows cannot enter model selection. Missing test books
    # leave the test score undefined without turning a trained candidate into a different rule.
    late_fair = [r | {"d_fair_c": 0.0} for r in rows if r["window_start_ns"] >= th[1]]
    early = [r for r in rows if r["window_start_ns"] < th[1]]
    late_only = rulefit.fit_price(early + late_fair, th)
    assert all("fair" not in late_only[s]["candidates"] for s in ("bid", "ask"))
    no_test = rulefit.fit_price(early, th)
    assert all(no_test[s]["train_rule"] == a[s]["train_rule"] for s in ("bid", "ask"))
    assert all(no_test[s]["offset_c"]["oos"]["mae_c"] is None for s in ("bid", "ask"))
    assert rulefit.fit_expressible(early + late_fair, late_only, th)["refs_compared"] == ["mid"]
    assert rulefit._phase([{"window_start_ns": th[0] - NS, "window_end_ns": th[1] + NS}], th).tolist() == [-1]


def test_mid_and_fair_share_one_mask_and_only_fair_assets_enter_the_genome(fitted):
    rows = [dict(r) for r in fitted[2] if r["kind"] == "order"]
    rng = np.random.default_rng(0)
    for r in rows:  # fair known for ~70 % of the orders, near the mid
        if rng.random() < 0.7:
            f = r["mid_c"] + float(rng.normal(0, 0.3))
            r["fair_c"], r["d_fair_c"] = f, r["px_c"] - f
    eth = [r | {"asset": "eth", "window": "eth-" + r["window"]} for r in rows[:200]]
    th = thresholds(rows + eth)
    price = rulefit.fit_price(rows + eth, th)
    e = rulefit.fit_expressible(rows + eth, price, th)
    no_fair = sum(r["fair_c"] is None for r in rows)
    assert e["refs_compared"] == ["mid", "fair"] and e["rows_dropped_by_fair_requirement"] == no_fair > 400
    assert e["n"] == len(rows) - no_fair and set(e["validation_mae_c"]) == {"mid", "mid+skew", "fair", "fair+skew"}
    assert e["rows_dropped_no_fair_model"] == 200 and e["assets"] == ["btc"]
    assert e["n_test_common"] == e["n_test"]  # schema cost: both rules on the same test rows
    assert abs(e["oos_cost_of_schema_c"] - (e["oos_mae_c_same_rows"] - e["per_side_fit_oos_mae_c"])) < 2e-4
    # too little fair: mid alone, nothing dropped for it
    for r in rows[100:]:
        r["fair_c"] = r["d_fair_c"] = None
    e2 = rulefit.fit_expressible(rows, rulefit.fit_price(rows, th), th)
    assert e2["refs_compared"] == ["mid"] and e2["rows_dropped_by_fair_requirement"] == 0 and e2["n"] == len(rows)


# ---------------------------------------------------------------- when no genome may be written
def test_no_genome_for_an_asset_without_fair_v1(tmp_path):
    write(tmp_path, *world(seed=2, windows=3)[:3], windows=3, asset="eth")
    genome, rep, rows = fit(tmp_path, tmp_path / "out")
    assert len(rows) > 500 and rep["price_rule"]["bid"]["chosen"] == "touch"  # the rule itself is still fitted
    assert genome is None and rep["genome"]["status"] == "not_emitted" and not (tmp_path / "out" / "genome.json").exists()
    assert "no order on an asset with fair-v1" in rep["genome"]["reason"]
    assert rep["expressible_price_rule"]["rows_dropped_no_fair_model"] == len(rows)
    assert "NOT EMITTED because" in (tmp_path / "out" / "rule.md").read_text()


def test_no_genome_when_it_could_never_quote_or_would_not_act_like_the_wallet(tmp_path, monkeypatch):
    # both quotes 1 c inside each other around the mid: half-spread ~0, the twin would self-cross
    frames, orders, tape, _ = world(seed=4, windows=3, ask_fn=lambda k, ms, bb, ba, inv: math.floor((bb + ba) / 2 - 0.7))
    write(tmp_path / "tight", frames, orders, tape, windows=3)
    genome, rep, _ = fit(tmp_path / "tight")
    assert genome is None and rep["expressible_price_rule"]["half_spread_c"] < 0.5
    assert any("half_spread_c" in x and "self-cross" in x for x in rep["genome"]["reasons"])
    # one window: no clustered CI
    write(tmp_path / "one", *world(seed=4, windows=1)[:3], windows=1)
    genome, rep, _ = fit(tmp_path / "one")
    assert genome is None and any("1 market window" in x for x in rep["genome"]["reasons"])
    # the 70 % rule: a genome that would stay silent where the wallet quoted is not emitted
    frames, orders, tape, _ = world(seed=4, windows=6)
    write(tmp_path / "ok", frames, orders, tape, windows=6, om_doc=fixture_match_controls(orders))
    assert fit(tmp_path / "ok")[0] is not None
    monkeypatch.setattr(rulefit, "MIN_QUOTE_SHARE", 1.01)
    genome, rep, _ = fit(tmp_path / "ok")
    assert genome is None and any("would quote" in x for x in rep["genome"]["reasons"])
    assert rep["genome"]["few_windows"] is True and rep["emitted_genome_check"]["would_quote_share"] > 0.9


# ---------------------------------------------------------------- no look-ahead
def lookahead_diffs(tmp, asof):
    """Build a world twice: as it was, and with EVERYTHING received at or after one order's
    placement rewritten (books, the order's own record, prints, a new own fill 1 ms later, BRTI
    received later, later candidates, later order-tape amounts) plus every ordermatch outcome
    field of every order. Returns (early rows, early rows whose state changed, later rows whose
    state changed, checks)."""
    frames, orders, tape, quotes = world(seed=3, windows=2, candidates=1)
    cut = orders[len(orders) // 2]
    P = cut["placed_tw_ns"]
    t = cut["placed_venue_ms"] - MS
    write(tmp / "a", frames, orders, tape, quotes, windows=2)
    brti_lab(tmp / "a" / "lab")
    future = [book(ms, f"{c}{k}", [(0.01, 1)], [(0.99, 1)], f"m{k}")
              for ms in range(t, t + 180_000, 500) for k in (0, 1) for c in "UD"]
    leak = trade(t + 1, cut["token_id"], cut["price"], 500, "0xleak", "m0" if cut["token_id"][1] == "0" else "m1")
    orders_b = [o | {"outcome": "filled", "reason": "chain", "end_ms": o["end_ms"] + 777, "rest_min_ms": 9,
                     "rest_max_ms": 9, "remainder": 0.0, "filled": o["size"], "fills_at": [], "n_fills": 3,
                     "created_ms": o["created_ms"] - (500 if o["placed_tw_ns"] > P + NS else 0)}
                for o in orders]
    tape_b = [r | {"fill_amount": 7 * r["fill_amount"]} if r["block_ts"] * NS >= P else r for r in tape]
    tape_b.append(tape_row("0xleak", "leak", cut["token_id"], cut["price"], 500.0, 500.0, P // NS + 2, MS + t))
    write(tmp / "b", [f for f in frames if f[0] < t] + future + [leak], orders_b, tape_b,
          [q for q in quotes if q["tw_ns"] < P], windows=2)
    brti_lab(tmp / "b" / "lab", bump_received_after=P / NS)

    def table(d):
        rows = rulefit.decision_table(d / "tape", d / "om", d / "ordertape.jsonl", W, T0, T0 + 3600,
                                      [d / "tokens.jsonl"], d / "lab", asof)["rows"]
        return {r["id"]: r for r in rows}

    a, b = table(tmp / "a"), table(tmp / "b")
    state = lambda r: [r["cutoff_tw_ns"]] + [r[c] for c in STATE_COLS]  # noqa: E731
    early = [i for i, r in a.items() if r["cutoff_tw_ns"] <= P and i in b]
    later = [i for i, r in a.items() if r["cutoff_tw_ns"] > P and i in b]
    checks = {"fair": sum(a[i]["fair_c"] is not None for i in early),
              "candidates": sum(a[i]["kind"] == "candidate" for i in early),
              "inventory": sum(bool(a[i]["inv"]) for i in early), "cut_row": cut["order_id"] in early}
    return early, [i for i in early if state(a[i]) != state(b[i])], [i for i in later if state(a[i]) != state(b[i])], checks


@pytest.mark.parametrize("asof", ["placement", "created"])
def test_state_uses_nothing_received_at_or_after_the_cutoff(tmp_path, asof):
    early, changed, later_changed, checks = lookahead_diffs(tmp_path, asof)
    assert len(early) > 150 and changed == []
    # the comparison has teeth: fair, candidates and inventory are in the early rows, and the rewritten
    # future does reach the later rows
    assert checks["fair"] > 100 and checks["inventory"] > 100 and checks["cut_row"]
    if asof == "created":
        assert checks["candidates"] == 0
    else:
        assert checks["candidates"] > 40
    assert len(later_changed) > 100
    assert {"gap_s", "px_c", "d_touch_c", "placed_venue_ms"}.isdisjoint(STATE_COLS)


@pytest.mark.parametrize("leak", ["inventory", "notional", "fair", "book"])
def test_the_lookahead_test_catches_the_reviewers_leaks(tmp_path, monkeypatch, leak):
    """Reviewer s6: a 250 ms inventory leak and a 5 s fair leak used to pass. Each leak below must
    change at least one early state row."""
    if leak == "inventory":
        orig = Inventory.at
        monkeypatch.setattr(Inventory, "at", lambda self, slug, tw: orig(self, slug, tw + 250_000_000))
    elif leak == "notional":
        orig_n = Inventory.notional_at
        monkeypatch.setattr(Inventory, "notional_at", lambda self, slug, tw: orig_n(self, slug, tw + 250_000_000))
    elif leak == "fair":
        orig_f = Fair.at
        monkeypatch.setattr(Fair, "at", lambda self, info, tw: orig_f(self, info, tw + 5 * NS))
    else:  # the placement's own record (and anything else with tw == cutoff) answered into the state
        orig_scan = rulefit.scan
        monkeypatch.setattr(rulefit, "scan", lambda root, s, a, b, qs, *rest, **kwargs: orig_scan(
            root, s, a, b, [(q[0] + 1, q[1], q[2]) for q in qs], *rest, **kwargs))
    _, changed, _, _ = lookahead_diffs(tmp_path, "placement")
    assert changed != []


def test_own_placement_is_not_in_its_state(fitted):
    rows = fitted[2]
    # the bid joins a 500 best bid: its own increase (same tw as the cutoff) is not in the state
    assert all(r["depth_bid"] == 500 and r["d_touch_c"] == 0 for r in rows if r["up_side"] == "bid")
    assert all(r["since_prev_s"] == 2 for r in rows if r["gap_s"] is not None)


def test_narrow_rulefit_range_filters_orders_and_candidates_but_reads_prior_book_state(tmp_path):
    frames, orders, tape, quotes = world(windows=2, candidates=1)
    orders[0]["created_ms"] = orders[0]["placed_venue_ms"] + 1
    write(tmp_path, frames, orders, tape, quotes, windows=2)
    start, end = T0 + 10, T0 + 150
    table = rulefit.decision_table(tmp_path / "tape", tmp_path / "om", tmp_path / "ordertape.jsonl", W,
                                   start, end, [tmp_path / "tokens.jsonl"], asof="created")
    expected = {o["order_id"] for o in orders if start * NS <= o["placed_tw_ns"] < end * NS}
    expected.remove(orders[0]["order_id"])
    assert {r["id"] for r in table["rows"] if r["kind"] == "order"} == expected
    assert table["stats"]["orders_outside_range_dropped"] == len(orders) - len(expected) - 1
    assert table["stats"]["order_created_time_after_placement"] == 1
    assert table["inputs"]["candidates"]["outside_range"] > 0
    assert table["stats"]["anonymous_created_time_unavailable"] > 0
    assert all(r["kind"] == "order" for r in table["rows"])
    assert all(start * 1000 <= s["placed_ms"] < end * 1000 for s in table["specs"])
    first = min(table["rows"], key=lambda r: r["cutoff_tw_ns"])
    assert first["mid_c"] is not None


def test_manifest_cli_uses_per_wallet_matches_and_separate_summary(fitted, tmp_path, monkeypatch):
    genome, _, _, data = fitted
    manifest_path = tmp_path / "manifest.json"
    own = tmp_path / "wallet-orders.jsonl"
    own.write_bytes((data / "om" / "orders.jsonl").read_bytes())
    summary = tmp_path / "match-summary.json"
    summary.write_bytes((data / "om" / "ordermatch.json").read_bytes())

    def read(path, wallet, start, end):
        assert (path, wallet, start, end) == (manifest_path, W, T0, T0 + 3600)
        return SimpleNamespace(orders=data / "ordertape.jsonl", matched_orders=own, summary=summary)

    monkeypatch.setitem(sys.modules, "doppel.lab.research_manifest", SimpleNamespace(read_reconstruction=read))
    out = tmp_path / "out"
    assert main(["--data", str(data / "tape"), "--reconstruct-manifest", str(manifest_path), "--wallet", W,
                 "--from", str(T0), "--to", str(T0 + 3600), "--tokens", str(data / "tokens.jsonl"),
                 "--out", str(out)]) == 0
    assert orjson.loads((out / "genome.json").read_bytes()) == genome
    report = orjson.loads((out / "fit_report.json").read_bytes())
    assert report["inputs"]["reconstruct_manifest"] == str(manifest_path)
    assert report["inputs"]["candidates"]["unavailable"] and report["selection_check"] is None


def test_inventory_and_notional_count_only_fills_located_before_the_cutoff():
    tok = {"U": {"slug": "s", "idx": 0}, "D": {"slug": "s", "idx": 1}}
    fills = [{"tx": "a", "token": "U", "side": "BUY", "shares": 10.0, "price": 0.4, "block_ts": 100},
             {"tx": "b", "token": "D", "side": "BUY", "shares": 4.0, "price": 0.7, "block_ts": 100},
             {"tx": "c", "token": "D", "side": "SELL", "shares": 1.0, "price": 0.5, "block_ts": 50},
             {"tx": "d", "token": "X", "side": "BUY", "shares": 1.0, "price": 0.5, "block_ts": 50}]
    inv = Inventory(fills, tok, {"a": 90 * NS, "b": 95 * NS})
    assert (inv.unmapped, inv.without_receipt) == (1, 1)
    assert [inv.at("s", t * NS) for t in (50, 51, 90, 91, 95, 96)] == [0, 0, 0, 10, 10, 6]
    # Only the received Up purchase opens exposure. The later Down purchase closes it.
    assert [round(inv.notional_at("s", t * NS), 6) for t in (50, 51, 91, 96)] == [0, 0, 4, 4]
    assert inv.window_notional({"s"}) == [4]
    assert inv.any_between("s", 90 * NS, 96 * NS) and not inv.any_between("s", 90 * NS, 95 * NS)
    assert inv.at("other", 10**20) == 0 and inv.notional_at("other", 10**20) == 0


def test_up_terms_mirror_the_down_token():
    assert up_order("BUY", 0.3, 0) == ("bid", 30.0) and up_order("SELL", 0.3, 0) == ("ask", 30.0)
    assert up_order("BUY", 0.3, 1) == ("ask", 70.0) and up_order("SELL", 0.3, 1) == ("bid", 70.0)
    v = up_view((0.3, 5.0, 0.34, 9.0), 1)  # Down bid 30 x5, ask 34 x9 -> Up bid 66 x9, ask 70 x5
    assert (v["bb_c"], v["depth_bid"], v["ba_c"], v["depth_ask"], v["mid_c"], v["spread_c"]) == (66, 9, 70, 5, 68, 4)
    assert up_view(None, 0)["mid_c"] is None and up_view((0.3, 5.0, None, None), 0)["mid_c"] is None


# ---------------------------------------------------------------- candidates
def test_selection_refit_moves_the_rule_when_unfilled_quotes_sit_elsewhere(tmp_path):
    frames, orders, tape, quotes = world(seed=5, windows=4, candidates=2)
    doc = {"params": {"from": T0, "to": T0 + 3600, "placement_evidence_version": rulefit.PLACEMENT_EVIDENCE_VERSION},
           "wallets": {W: {"placement_evidence_counts": {"unique_candidate": len(orders)},
                            "fingerprint": {"total": {"identity_precision_upper": 0.8}}}}}
    write(tmp_path, frames, orders, tape, quotes, doc, windows=4)
    _, rep, rows = fit(tmp_path)
    sel = rep["selection_check"]
    assert sel["identity_precision"] == 0.8 and sel["n_candidates"] == len(quotes) == len(orders)
    assert sel["reading"].startswith("IF THE ATTRIBUTION HELD") and "Not a bound" in sel["reading"]
    assert rep["inputs"]["candidates"] | {"sampling": 0} == {"cap": 5000, "total": len(quotes), "kept": len(quotes),
                                                           "outside_range": 0, "seed": rulefit.SEED, "strata": 4,
                                                           "sampling": 0}
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


def test_candidates_are_streamed_capped_and_stratified_and_memory_does_not_grow(tmp_path):
    frames, orders, tape, all_quotes = world(seed=6, windows=2, candidates=40)
    d = tmp_path
    write(d, frames, orders, tape, windows=2)

    def peak(step):
        # Both measurements read the same compressed bytes. Re-recording identical messages
        # can produce differently sized zstd frames because the writer flushes on wall time.
        quotes = all_quotes[::step]
        junk = {"wallet": "0xother", "token_id": "U0", "price": 0.5, "venue_ms": 1, "tw_ns": 1, "size": 97.0}
        with open(d / "om" / "quotes.jsonl", "wb") as fh:
            fh.write(jl(quotes))
            fh.write(jl([junk] * 50))
        tracemalloc.start()
        T = rulefit.decision_table(d / "tape", d / "om", d / "ordertape.jsonl", W, T0, T0 + 3600,
                                   [d / "tokens.jsonl"], max_candidates=100)
        _, top = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return top, T, len(quotes)

    small, _, n_small = peak(10)
    big, T, n_big = peak(1)
    assert n_big == 10 * n_small > 10_000
    assert big < 1.15 * small  # 10x the candidates on disk, the same 100 in memory
    info = T["inputs"]["candidates"]
    assert (info["total"], info["kept"], info["cap"], info["strata"]) == (n_big, 100, 100, 2)
    kept = [r for r in T["rows"] if r["kind"] == "candidate"]
    assert len(kept) == 100 and sorted(sum(r["window"] == slug(k) for r in kept) for k in (0, 1)) == [50, 50]
    assert T["stats"]["candidates_in_file"] == n_big
    # seeded: the same file gives the same sample; under the cap nothing is dropped
    tok = rulefit.token_map(d / "tape", T0, T0 + 3600, [d / "tokens.jsonl"])
    a, _ = rulefit.sample_quotes(d / "om" / "quotes.jsonl", W, tok, 100)
    assert a == rulefit.sample_quotes(d / "om" / "quotes.jsonl", W, tok, 100)[0]
    assert set(a[0]) == set(rulefit.QUOTE_KEYS)  # needed columns only
    assert len(rulefit.sample_quotes(d / "om" / "quotes.jsonl", W, tok, 10**6)[0]) == n_big
    # 7 of 3 strata sizes 10/20/70 -> 1/1/5 (largest remainder), never more than the cap
    q = [{"wallet": W, "token_id": t, "price": 0.5, "venue_ms": i, "tw_ns": i, "size": 97.0}
         for t, n in (("x", 10), ("y", 20), ("z", 70)) for i in range(n)]
    (tmp_path / "q.jsonl").write_bytes(jl(q))
    got, info = rulefit.sample_quotes(tmp_path / "q.jsonl", W, {}, 7)
    assert sorted(sum(r["token_id"] == t for r in got) for t in "xyz") == [1, 1, 5] and info["kept"] == 7


# ---------------------------------------------------------------- fair
def test_fair_cache_does_not_mix_horizons_that_share_a_start(tmp_path):
    """Reviewer s5: a 5m and a 15m window start together; keyed by start alone the 15m window got
    the 5m window's fair (90.07 instead of 65.14 in the reviewer's run)."""
    lab = brti_lab(tmp_path / "lab", lo=T0 - 1200, hi=T0 + 1200)
    i5 = {"asset": "btc", "start": T0, "end": T0 + 300}
    i15 = {"asset": "btc", "start": T0, "end": T0 + 900}
    cut = (T0 + 200) * NS
    f = Fair(lab, T0 - 600, T0 + 1200)
    a5, a15 = f.at(i5, cut), f.at(i15, cut)
    assert a15 == Fair(lab, T0 - 600, T0 + 1200).at(i15, cut) and a5 == Fair(lab, T0 - 600, T0 + 1200).at(i5, cut)
    assert a5 is not None and a15 is not None and abs(a5 - a15) > 1 and len(f.feat) == 2
    assert f.at(i15, cut) == a15 and f.at(i5, cut) == a5  # either order


def test_fair_reference_is_found_and_skipped_cleanly_without_brti(tmp_path, capsys):
    lab = brti_lab(tmp_path / "lab")
    fair = Fair(lab, T0, T0 + 900)
    info = lambda k: {"asset": "btc", "start": T0 + L * k, "end": T0 + L * (k + 1)}  # noqa: E731
    assert fair.at(info(0), (T0 - 1) * NS) is None and fair.at({**info(0), "asset": "eth"}, (T0 + 50) * NS) is None
    f50 = fair.at(info(0), (T0 + 50) * NS + 999_999_999)
    assert f50 is not None and 0 <= f50 <= 100

    def ask_fn(k, ms, bb, ba, inv):
        v = fair.at(info(k), (MS + ms) * 1_000_000)
        assert v is not None
        return round(v + 2 - 0.05 * inv)

    frames, orders, tape, _ = world(seed=9, windows=3, ask_fn=ask_fn)
    write(tmp_path, frames, orders, tape, windows=3)
    (lab / "tokens.jsonl").write_bytes((tmp_path / "tokens.jsonl").read_bytes())
    rc = main(["--data", str(tmp_path / "tape"), "--ordermatch-dir", str(tmp_path / "om"), "--orders",
               str(tmp_path / "ordertape.jsonl"), "--wallet", W.upper().replace("0X", "0x"), "--from", str(T0),
               "--to", str(T0 + 3600), "--brti-lab", str(lab), "--out", str(tmp_path / "out")])
    assert "RULE CONDITIONAL ON FILL" in capsys.readouterr().out
    rep = orjson.loads((tmp_path / "out" / "fit_report.json").read_bytes())
    assert rc == (0 if rep["genome"]["id"] else 1) and (tmp_path / "out" / "genome.json").exists() == (rc == 0)
    ask = rep["price_rule"]["ask"]
    assert ask["chosen"] == "fair" and ask["ref_coverage"]["fair"] == 1 and abs(ask["intercept_c_with_skew"] - 2) < 0.15
    assert abs(ask["skew_c_per_contract"]["value"] - 0.05) < 0.01
    assert rep["price_rule"]["bid"]["chosen"] == "touch" and rep["inputs"]["fair"] is True
    # with fair supplied the emitted-genome check uses it: no substitution, and the clamp is measured
    e, ck = rep["expressible_price_rule"], rep["emitted_genome_check"]
    assert e["refs_compared"] == ["mid", "fair"] and e["rows_dropped_by_fair_requirement"] == 0
    assert ck["fair_substituted_by_mid"] is False and ck["fair_clamp_binds_share"] is not None
    # without BRTI: same table, no fair column, the fit falls back to the book references
    _, rep2, rows2 = fit(tmp_path)
    assert all(r["fair_c"] is None and r["d_fair_c"] is None for r in rows2)
    assert rep2["price_rule"]["ask"]["ref_coverage"]["fair"] == 0 and rep2["price_rule"]["ask"]["chosen"] in ("mid", "touch")
    assert rep2["genome"]["fields"]["maker.min_edge_vs_fair_c"].startswith("default 0")
    assert rep2["expressible_price_rule"].get("refs_compared", ["mid"]) == ["mid"]


# ---------------------------------------------------------------- token map, grid
def test_token_map_from_rtds_rows_and_unmapped_rows_are_counted(tmp_path):
    frames, orders, tape, _ = world(seed=11, windows=2)
    write(tmp_path, frames, orders, tape, windows=2)
    w = SegmentWriter(tmp_path / "tape", "rtds_a")  # window 0 only, and only its DOWN token
    w.append(orjson.dumps({"topic": "activity", "type": "trades", "payload": {
        "transactionHash": "0x1", "proxyWallet": "0xother", "eventSlug": slug(0), "asset": "D0", "side": "BUY",
        "outcomeIndex": 1, "price": 0.5, "size": 1, "fee": 0.1, "timestamp": T0 + 5}}).decode(), tw=(T0 + 6) * NS)
    w.close()
    T = rulefit.decision_table(tmp_path / "tape", tmp_path / "om", tmp_path / "ordertape.jsonl", W, T0, T0 + 3600, [])
    rows = T["rows"]
    n0 = sum(o["token_id"] in ("U0", "D0") for o in orders)
    assert len(rows) == n0 and {r["window"] for r in rows} == {slug(0)}  # U0 mapped as D0's mate
    assert T["stats"]["tokens_mapped_by_mate"] == 1 and T["stats"]["order_unmapped_token"] == len(orders) - n0
    assert {r["up_side"] for r in rows if r["token"] == "D0"} == {"ask"}
    assert {r["up_side"] for r in rows if r["token"] == "U0"} == {"bid"}
    assert rulefit.token_map(tmp_path / "tape", T0, T0 + 3600, [])["D0"]["idx"] == 1
    # the participation grid exists although the Up token was mapped only after the scan: it comes
    # from the Down book, mirrored
    part = rulefit.fit_participation(T)
    # An unmapped economic fill may belong to any recorded market. The known public book
    # still supplies opportunity cells, but these ambiguous fills censor absence labels.
    assert part["eligible_cells"] >= 27 and part["cells"] == 0
    assert slug(0) in part["windows_with_unlocated_maker_fills"]
    assert {k[2] for k in T["res"]["grid"]} == {1}
    down = next(v for v in T["res"]["grid"].values() if v is not None)
    u = up_view(down, 1)
    # The synthetic Down quote can lock the observed book. Its mirror preserves that state,
    # and the static intent check handles marketable quotes separately.
    assert u["bb_c"] == pytest.approx(100 * (1 - down[2]))
    assert u["ba_c"] == pytest.approx(100 * (1 - down[0]))
    assert 25 <= u["bb_c"] <= u["ba_c"] <= 75


# ---------------------------------------------------------------- timing, cancels, ladders
def test_triggers_and_the_state_just_before_a_cancel(tmp_path):
    U, mk = "U0", "m0"

    def o(oid, placed, **kw):
        return {"order_id": oid, "maker": W, "token_id": U, "side": "BUY", "price": 0.5, "size": 100.0,
                "created_ms": MS + placed - 100, "placed_venue_ms": MS + placed, "placed_tw_ns": (MS + placed) * 1_000_000,
                "filled": 0.0, "remainder": 100.0, "n_fills": 0, "fills_at": [], "outcome": "unknown",
                "reason": "censored", "end_ms": None, "rest_min_ms": 0, "rest_max_ms": None,
                "fill_before_placement": False, "placement_evidence": "unique_candidate",
                "placement_candidates": 1, "rest_known": kw.get("outcome") == "cancelled"} | kw

    frames = [book(1000, U, [(0.50, 500)], [(0.54, 400)], mk),
              pc(2000, U, 0.50, 600, "BUY", mk),      # a
              pc(3000, U, 0.50, 700, "BUY", mk),      # b: nothing changed since a landed -> timer
              pc(3500, U, 0.54, 0, "SELL", mk), pc(3501, U, 0.55, 400, "SELL", mk),
              pc(4000, U, 0.50, 800, "BUY", mk),      # c: the ask moved 1 c -> book_move
              trade(4200, U, 0.50, 10, "0xf1", mk),   # c is hit for 10
              pc(4990, U, 0.50, 699, "BUY", mk, ts=5000),  # 101 leave, stamped the cancel's ms: NOT the closest
              pc(4995, U, 0.51, 50, "BUY", mk, ts=5000),  # improves the bid in the same venue ms
              pc(5000, U, 0.50, 599, "BUY", mk),      # a's 100 leave: the closest decrease at that ms
              pc(6000, U, 0.50, 699, "BUY", mk)]      # d: an own fill since c -> own_fill
    orders = [o("a", 2000, outcome="cancelled", reason="exact_decrease", end_ms=MS + 5000, rest_min_ms=3000,
                rest_max_ms=3000), o("b", 3000), o("d", 6000),
              o("c", 4000, filled=10.0, remainder=90.0, n_fills=1, fills_at=[["0xf1", MS + 4200, 10.0]]),
              o("x", 2000, maker="0xother"), o("bad", 3000, fill_before_placement=True)]
    tape = [tape_row("0xf1", "c", U, 0.5, 100.0, 10.0, T0 + 9, MS + 3900)]
    write(tmp_path, frames, orders, tape, windows=1)
    args = (tmp_path / "tape", tmp_path / "om", tmp_path / "ordertape.jsonl", W, T0, T0 + 3600, [tmp_path / "tokens.jsonl"])
    T = rulefit.decision_table(*args)
    rows = {r["id"]: r for r in T["rows"]}
    assert list(rows) == ["a", "b", "c", "d"]
    assert (T["stats"]["orders_other_makers"], T["stats"]["orders_wrong_match_dropped"]) == (1, 1)
    assert [rows[i]["depth_bid"] for i in "abcd"] == [500, 600, 700, 50]  # size at the best, never its own increase
    assert [rows[i]["inv"] for i in "abcd"] == [0, 0, 0, 10] and [rows[i]["gap_s"] for i in "abcd"] == [None, 1, 1, 2]
    assert [rows[i]["notional_usd"] for i in "abcd"] == [0, 0, 0, 5] and rows["d"]["since_prev_s"] == 2
    assert (rows["d"]["bb_c"], rows["d"]["ba_c"], rows["d"]["d_touch_c"], rows["d"]["tau_s"]) == (51, 55, -1, 6)
    # two decreases carry the cancel's venue ms and both are within tolerance: the CLOSEST (100, not
    # 101) is taken, and its record is undone: 0.51 x 50 is already the best (it was not at the first)
    assert T["res"]["at_cancel"] == {"a": (0.51, 50.0, 0.55, 400.0)}
    assert (T["stats"]["cancels_asked"], T["stats"]["cancels_state_found"]) == (1, 1)
    tm = rulefit.fit_timing(T)
    assert {k: v["value"] for k, v in tm["trigger_shares"].items()} == {
        "own_fill": 0.3333, "book_move": 0.3333, "timer": 0.3333, "unknown": 0}
    T["res"]["state_integrity"][("post", "a")] = "unbracketed_gap"
    assert rulefit.fit_timing(T)["trigger_shares"]["unknown"]["value"] == 0.3333
    c = tm["cancel"]
    assert c["life_ms"]["value"] == 3000 and c["opposite_best_moved_1c"]["value"] == 1
    assert c["behind_best_at_cancel"]["value"] == 1 and c["replaced_within_1s"]["value"] == 1
    # The signed-time hypothesis subtracts this order's own 100 ms placement delay.
    early = rulefit.decision_table(*args, asof="created")["rows"]
    assert [r["cutoff_tw_ns"] - rows[r["id"]]["cutoff_tw_ns"] for r in early] == [-100_000_000] * 4
    # a tape with a ``verified`` column: only orders with a verified row survive
    (tmp_path / "ordertape.jsonl").write_bytes(jl([tape[0] | {"verified": True},
                                                   tape[0] | {"order_id": "a", "verified": False}]))
    T2 = rulefit.decision_table(*args)
    assert [r["id"] for r in T2["rows"]] == ["c"] and T2["stats"]["orders_not_verified_dropped"] == 3
    (tmp_path / "ordertape.jsonl").write_bytes(jl(tape))
    # too few orders for a rule: says so and emits NO genome (schema defaults are not this wallet's rule)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "genome.json").write_text("{}")  # an older run's file must not survive
    genome, rep, _ = fit(tmp_path, tmp_path / "out")
    assert rep["price_rule"]["bid"]["chosen"] is None and "reason" in rep["price_rule"]["bid"]
    assert genome is None and rep["genome"]["status"] == "not_emitted" and rep["genome"]["id"] is None
    assert not (tmp_path / "out" / "genome.json").exists()
    assert "NO GENOME EMITTED" in (tmp_path / "out" / "rule.md").read_text()
    cli = ["--data", str(tmp_path / "tape"), "--ordermatch-dir", str(tmp_path / "om"), "--orders",
           str(tmp_path / "ordertape.jsonl"), "--from", str(T0), "--to", str(T0 + 3600), "--out", str(tmp_path / "out")]
    assert main(cli + ["--wallet", W, "--tokens", str(tmp_path / "tokens.jsonl")]) == 1
    (tmp_path / "out" / "genome.json").write_text("older draft")
    with pytest.raises(SystemExit, match="no order of .* could be put in a window"):
        main(cli + ["--wallet", W])  # no token map at all
    assert not (tmp_path / "out" / "genome.json").exists()
    assert (tmp_path / "out" / "decision_table.jsonl").read_bytes() == b""
    report = orjson.loads((tmp_path / "out" / "fit_report.json").read_bytes())
    assert report["genome"]["status"] == "not_emitted" and "token map" in report["genome"]["reason"]


def test_ladders_are_counted_from_overlapping_lives_at_distinct_prices():
    def spec(i, token, side, price, placed, rest_max, rest_min=None):
        return {"kind": "order", "id": str(i), "token": token, "side": side, "price": price, "placed_ms": placed,
                "om": {"rest_max_ms": rest_max, "rest_min_ms": rest_min if rest_min is not None else rest_max}, "row": {"window": f"w{i % 2}"}}

    specs = [spec(0, "U", "BUY", 0.50, 0, 5000),       # live 0..5000
             spec(1, "U", "BUY", 0.49, 1000, 1000),    # overlaps 0 at another price: both laddered, depth 2
             spec(2, "U", "BUY", 0.48, 1500, 100),     # overlaps 0 and 1: depth 3
             spec(3, "U", "BUY", 0.50, 3000, 100),     # same price as 0, 1 and 2 are gone: not a ladder
             spec(4, "U", "SELL", 0.55, 1200, 9000),   # other side
             spec(5, "D", "BUY", 0.47, 1200, 9000),    # other token
             spec(6, "U", "BUY", 0.40, 6000, None, 50),  # censored: rest_min; 0 ended at 5000
             spec(7, "U", "BUY", 0.41, 6050, 10),      # 6's life ends AT 6050: no overlap
             {"kind": "candidate", "id": "c", "token": "U", "side": "BUY", "price": 0.3, "placed_ms": 100, "om": {}}]
    lad = rulefit.fit_ladder(specs)
    # 0 and 1 rest together for 1000 ms (the floor): a ladder pair. 2 overlaps them for 100 ms only.
    assert lad["n"] == 8 and lad["floor_ms"] == 1000 and lad["ladder_share"]["value"] == 0.25 and lad["depth_max"] == 2
    assert lad["brief_overlap_share"] == 0.375  # the old count: any overlap, however short
    assert lad["depth"]["p50"] == 1 and rulefit.fit_ladder([])["n"] == 0
    # the floor follows the wallet's own re-post gap: at a 4 s gap an overlap must last 2 s
    slow = rulefit.fit_ladder(specs, gap_s=4.0)
    assert slow["floor_ms"] == 2000 and slow["ladder_share"]["value"] == 0


def lad_spec(i, price, placed, rest, window):
    return {"kind": "order", "id": f"{i:05d}", "token": "U", "side": "BUY", "price": price, "placed_ms": placed,
            "om": {"rest_max_ms": rest, "rest_min_ms": rest}, "row": {"window": f"w{window}"}}


def test_a_cancel_and_its_replacement_are_not_a_ladder():
    """A re-quoting wallet: every 5 s the old bid is cancelled and a new one lands 1 c away. The
    two latencies differ, so old and new overlap for 300 ms at two prices. That is not a ladder.
    A wallet that keeps two levels up together is one, with its depth and a CI."""
    requote = [lad_spec(i, 0.50 + 0.01 * (i % 2), 5000 * i, 5300, i // 25) for i in range(200)]
    lad = rulefit.fit_ladder(requote, gap_s=5.0)
    assert lad["brief_overlap_share"] == 1  # the pre-floor definition called this a ladder at every order
    assert lad["ladder_share"]["value"] == 0 and lad["ladder_share"]["ci95"] == [0, 0]
    assert lad["status"] == "no_ladder_seen" and lad["depth_max"] == 1 and lad["depth_when_laddered"] is None
    assert "lower bound" in lad["bias"]
    # three levels re-posted together every 5 s and resting 4 s
    ladder = [lad_spec(3 * i + j, 0.50 - 0.01 * j, 5000 * i + 10 * j, 4000, i // 25) for i in range(200) for j in range(3)]
    lad = rulefit.fit_ladder(ladder, gap_s=5.0)
    d = lad["depth_when_laddered"]
    assert lad["status"] == "ladder" and lad["ladder_share"]["value"] == 1 and lad["ladder_share"]["ci95"] == [1, 1]
    assert d["value"] == 3 and d["ci95"] == [3, 3] and lad["depth_max"] == 3
    # too few orders, or one market window: neither answer is given
    assert rulefit.fit_ladder(ladder[:9], gap_s=5.0)["status"] == "unknown"
    assert rulefit.fit_ladder([s | {"row": {"window": "w"}} for s in ladder], gap_s=5.0)["status"] == "unknown"


# ---------------------------------------------------------------- selection: cadence and quote life
def stream(kind, n, gap, rng):
    """Placement times (ms) of one token: strictly periodic or Poisson, true median gap known."""
    steps = np.full(n, gap) if kind == "periodic" else rng.exponential(gap, n)
    return np.cumsum(steps), float(np.median(steps))


@pytest.mark.parametrize("kind", ["periodic", "poisson"])
@pytest.mark.parametrize("p", [0.1, 0.3, 0.6])
def test_thinned_cadence_is_recovered_on_a_known_fill_share(kind, p):
    """Only a share p of the placements is seen (the filled ones). The gap between the seen ones is
    1/p too long; the thinning range holds the true median gap for both a periodic and a Poisson
    stream, and without p the tool states an upper bound and no interval."""
    rng = np.random.default_rng(int(p * 100))
    g, cl, truth = [], [], []
    for w in range(20):  # 20 market windows
        t, med = stream(kind, 3000, 2000.0, rng)
        seen = t[rng.random(len(t)) < p]
        g += list(np.diff(seen) / 1000)
        cl += [f"w{w}"] * (len(seen) - 1)
        truth.append(med / 1000)
    g, cl, true = np.array(g), np.array(cl), float(np.median(truth))
    m = float(np.median(g))
    if kind == "poisson" or p < 0.5:
        assert m > 1.4 * true  # the bias being corrected (a periodic stream seen more than half the time has none)
    lo, hi = rulefit.thinned_gap(m, p, p)
    assert lo * 0.95 <= true <= hi * 1.05
    fb = {"p": p, "p_range": [0.8 * p, min(1.0, 1.2 * p)], "chance_known": True, "chance_share": 0.0,
          "chance_share_range": [0.0, 0.0]}
    c = rulefit.fit_cadence(g, cl, {}, {}, fb)
    assert c["status"] == "ESTIMATED" and c["ci95"][0] <= true <= c["ci95"][1] and c["bias"] == "fill-conditional upper bound"
    assert abs(math.log(c["value"] / true)) < math.log(2) and c["filled"]["ci95"] is None
    assert c["ci95"][1] / c["ci95"][0] > 1.4  # never a tight interval on a biased sample
    blind = rulefit.fit_cadence(g, cl, {}, {}, None)  # p cannot be estimated
    assert blind["status"] == "UPPER_BOUND" and blind["ci95"] is None and blind["value"] == pytest.approx(m, abs=1e-3)
    assert blind["upper_bound"] >= true
    few = rulefit.fit_cadence(g[:5], cl[:5], {}, {}, fb)  # five gaps: not even a bound
    assert few["status"] == "UNKNOWN" and few["ci95"] is None and few["upper_bound"] is None


def test_cadence_from_located_orders_merged_with_unfilled_candidates():
    """With ordermatch's fingerprint candidates the unfilled placements are in the series again
    (plus other people's quotes at the stated chance share): the merged gap is the estimate."""
    rng = np.random.default_rng(4)
    own, cand, g, cl = {}, {}, [], []
    for w in range(12):
        t, _ = stream("periodic", 600, 500.0, rng)
        t = t + rng.uniform(-50, 50, len(t))
        hit = rng.random(len(t)) < 0.25
        noise = rng.uniform(t[0], t[-1], 60)  # other makers posting the same size: 10 % of the candidates
        own[f"t{w}"], cand[f"t{w}"] = [int(x) for x in t[hit]], [int(x) for x in np.concatenate([t[~hit], noise])]
        g += list(np.diff(t[hit]) / 1000)
        cl += [f"w{w}"] * (hit.sum() - 1)
    fb = rulefit.footprint_bounds({"active_token_hours": 12.0, "control_token_hours": 12.0, "total": {
        "own": 1800, "own_unmatched": 0, "candidates": 6120, "control": 720, "expected_by_chance": 720.0}})
    assert fb["chance_known"] and 0.1 < fb["chance_share"] < 0.13 and fb["p_range"][0] < 0.25 < fb["p_range"][1]
    c = rulefit.fit_cadence(np.array(g), np.array(cl), own, cand, fb)
    assert c["status"] == "ESTIMATED" and c["ci95"][0] <= 0.5 <= c["ci95"][1] and abs(c["value"] - 0.5) < 0.1
    assert c["filled"]["value"] > 1.2 and "candidate" in c["method"]
    # Without controls every candidate might belong to another wallet.
    fb0 = rulefit.footprint_bounds({"active_token_hours": 12.0, "control_token_hours": 0.0, "total": {
        "own": 1800, "own_unmatched": 0, "candidates": 6120, "control": 0, "expected_by_chance": None}})
    assert not fb0["chance_known"] and fb0["p_range"][1] == 1 and fb0["chance_share"] is None
    c0 = rulefit.fit_cadence(np.array(g), np.array(cl), own, cand, fb0)
    assert c0["status"] == "UPPER_BOUND" and c0["ci95"] is None and c0["value"] == c0["filled"]["value"]
    assert "unfilled share" in c0["correction_note"]
    assert rulefit.footprint_bounds(None) is None and rulefit.footprint_bounds({"total": None}) is None


def test_quote_life_from_cancels_of_filled_orders_is_length_biased():
    """A quote that rests longer is more likely to fill, so the cancels DOPPEL can see (remainders
    of filled orders) over-state the quote life. Alone they are an upper bound without a CI; pooled
    with the cancelled unfilled candidates the median comes back, inside a contamination band."""
    rng = np.random.default_rng(9)
    life = rng.exponential(1000.0, 6000)                      # true median ~693 ms
    filled = rng.random(len(life)) < 1 - np.exp(-life / 2000)  # fill chance grows with the life
    win = np.array([f"w{i % 15}" for i in range(len(life))])
    true = float(np.median(life))
    vis, vcl = life[filled], win[filled]
    tok = {f"t{k}": {"slug": f"w{k}"} for k in range(15)}
    alone = rulefit.fit_life(vis, vcl, {}, tok, None)["life_ms"]
    assert alone["value"] > 1.5 * true and alone["ci95"] is None and alone["bias"] == "length-biased"
    assert alone["status"] == "UPPER_BOUND" and alone["upper_bound"] >= true and alone["visible"]["ci95"] is None
    cand = {f"t{k}": [int(v) for v in life[~filled][k::15]] + [int(v) for v in rng.exponential(5000.0, 27)]
            for k in range(15)}  # the unfilled quotes, plus other makers' longer ones (~10 %)
    fb = {"chance_known": True, "chance_share_range": [0.05, 0.15]}
    both = rulefit.fit_life(vis, vcl, cand, tok, fb)
    pooled = both["life_ms"]
    assert pooled["status"] == "ESTIMATED" and abs(pooled["value"] - true) < 0.15 * true
    assert pooled["ci95"][0] <= true <= pooled["ci95"][1] and pooled["ci95"][1] - pooled["ci95"][0] > 0.2 * true
    assert both["life_ms_p90"]["value"] < np.percentile(vis, 90)  # the genome's quote age no longer uses the biased p90
    # Unknown or uninformative controls cannot correct the visible quote life or change the draft age.
    for chance in (None, {"chance_known": False, "chance_share_range": [0.0, 1.0]},
                  {"chance_known": True, "chance_share_range": [0.5, 1.0]}):
        blind = rulefit.fit_life(vis, vcl, cand, tok, chance)
        assert blind["life_ms"]["status"] == "UPPER_BOUND" and blind["life_ms"]["ci95"] is None
        assert blind["life_ms"]["value"] == alone["value"] and "control" in blind["life_ms"]["correction_note"]
        assert blind["life_ms_p90"]["value"] == pytest.approx(np.percentile(vis, 90), abs=0.001)
        unknown = rulefit.fit_life(np.array([]), np.array([], dtype=str), cand, tok, chance)
        assert unknown["life_ms"]["status"] == "UNKNOWN" and unknown["life_ms_p90"]["value"] is None
    # three visible cancels and no candidates: not even a bound
    tiny = rulefit.fit_life(vis[:3], vcl[:3], {}, tok, None)["life_ms"]
    assert tiny["status"] == "UNKNOWN" and tiny["ci95"] is None and tiny["upper_bound"] is None


# ---------------------------------------------------------------- explained share
def side_rows(n_windows, side_of, seed=0, per_window=8):
    """Decision rows of a wallet that quotes ONE side per window. ``side_of(k, mid, rng)`` -> bid|ask."""
    rng = np.random.default_rng(seed)
    rows = []
    for k in range(n_windows):
        mid = float(rng.choice([35, 40, 45, 55, 60, 65]))
        s = side_of(k, mid, rng)
        for j in range(per_window):
            rows.append({"window": slug(k), "window_start_ns": (T0 + L * k) * NS, "window_end_ns": (T0 + L * k + L) * NS,
                         "cutoff_tw_ns": (T0 + L * k + 5 + 20 * j) * NS, "up_side": s,
                         # the book drifts with the side after the first order: a per-order test would leak it
                         "mid_c": mid if j == 0 else (70.0 if s == "bid" else 30.0), "fair_c": None})
    return rows


def test_a_side_chosen_by_a_hidden_signal_is_not_explained():
    hidden = side_rows(40, lambda k, mid, rng: "bid" if rng.random() < 0.5 else "ask")
    th = thresholds(hidden)
    t = rulefit.side_test(hidden, th)
    assert t["kind"] == "one_sided_by_window" and t["explained"] is False and t["ci95"][0] <= 0.5
    assert "includes chance" in t["reason"] and t["n_held_out"] >= 10
    # the same wallet whose side follows the book at its first order: explained, on the held-out windows
    seen = side_rows(40, lambda k, mid, rng: "bid" if mid > 50 else "ask")
    t = rulefit.side_test(seen, thresholds(seen))
    assert t["explained"] is True and t["predictor"] == "mid_minus_50" and t["balanced_accuracy"] == 1 and t["ci95"][0] > 0.5
    # too few held-out windows to tell: not explained (never the benefit of the doubt)
    few = side_rows(6, lambda k, mid, rng: "bid" if mid > 50 else "ask", seed=3)
    assert rulefit.side_test(few, thresholds(few))["explained"] is not True
    # always the same side: nothing to explain. Both sides in most windows: exempt.
    const = side_rows(20, lambda k, mid, rng: "bid")
    assert rulefit.side_test(const, thresholds(const))["agreement_ci95"][0] > 0.5
    assert rulefit.side_test(const, thresholds(const))["kind"] == "one_sided_constant"
    assert rulefit.side_test(const, thresholds(const))["explained"] is True
    two = [r | {"up_side": "bid" if i % 2 else "ask"} for i, r in enumerate(hidden)]
    assert rulefit.side_test(two, th)["kind"] == "two_sided" and rulefit.side_test(two, th)["explained"] is None
    shifted = [r if r["window_start_ns"] < th[0] else r | {"up_side": "bid"} for r in two]
    assert rulefit.side_test(shifted, th)["kind"] == "undetermined"
    thin = side_rows(10, lambda k, mid, rng: "bid", per_window=2)  # two visible orders a window say nothing
    assert rulefit.side_test(thin, thresholds(thin))["kind"] == "undetermined"


def test_no_genome_for_a_wallet_whose_side_follows_a_hidden_signal(tmp_path):
    """The known-rule world, but in each window the wallet quotes ONE side, picked by a coin DOPPEL
    never sees. Its price rule fits perfectly; the genome is still not emitted, with the numbers."""
    frames, orders, tape, _ = world(seed=5, windows=12)
    coin = np.random.default_rng(1).random(12) < 0.5
    keep = [o for o in orders if o["token_id"].startswith("U") == bool(coin[int(o["token_id"][1:])])]
    ids = {o["order_id"] for o in keep}
    write(tmp_path, frames, keep, [r for r in tape if r["order_id"] in ids],
          windows=12, om_doc=fixture_match_controls(keep))
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "genome.json").write_text("{}")
    genome, rep, _ = run(tmp_path / "tape", tmp_path / "om", tmp_path / "ordertape.jsonl", W, T0, T0 + 3600,
                         tmp_path / "out", [tmp_path / "tokens.jsonl"])
    ex = rep["explained_by_observed_inputs"]
    assert genome is None and rep["genome"]["status"] == "not_emitted" and not (tmp_path / "out" / "genome.json").exists()
    assert ex["verdict"] is False and ex["side"]["kind"] == "one_sided_by_window" and ex["side"]["explained"] is False
    assert any(r.startswith("not explained by observed inputs: side") for r in rep["genome"]["reasons"])
    assert "explained by observed inputs: NO" in (tmp_path / "out" / "rule.md").read_text()


# ---------------------------------------------------------------- takers
class FlatFair:
    def __init__(self, fair):
        self.fair = fair

    def at(self, info, tw):
        return self.fair.get((info["slug"], tw // NS))


def taker_world(rule, windows=30, seed=2):
    """Cells of ``windows`` 5m windows with a wandering mid and fair; ``rule(mid, fair, tte_frac, rng)``
    -> list of (side, idx) takes in the cell."""
    rng = np.random.default_rng(seed)
    tok, grid, fair, takes = {}, {}, {}, []
    for k in range(windows):
        s = slug(k)
        start = T0 + L * k
        for i in (0, 1):
            tok[f"{'UD'[i]}{k}"] = {"slug": s, "asset": "btc", "hz": "5m", "start": start, "end": start + L, "idx": i,
                                    "known_tw_ns": (T0 - 1) * NS}
        mid = 50.0
        for b in range(start + 10, start + L, 10):
            mid = float(np.clip(mid + rng.integers(-4, 5), 10, 90))
            f = mid + float(rng.integers(-5, 6))
            grid[(s, b, 0)] = ((mid - 1) / 100, 100.0, (mid + 1) / 100, 100.0)
            fair[(s, b)] = f
            for j, (side, idx) in enumerate(rule(mid, f, (start + L - b) / L, rng)):
                takes.append({"tw": (b + 1 + j % 9) * NS, "token": f"{'UD'[idx]}{k}", "idx": idx, "side": side,
                              "slug": s, "located": True})
    panel = rulefit.opportunity_panel(T0, T0 + windows * L, [("btc", "5m")], tok, grid, dict.fromkeys(grid, "covered"))
    return {"takes": takes, "tok": tok, "res": {"grid": grid}, "fair": FlatFair(fair), "opportunity_panel": panel}


def test_a_random_taker_gets_no_rule_and_a_rule_following_taker_gets_one():
    def decoy(mid, fair, frac, rng):   # Poisson times, a coin for the token: nothing to find
        return [("BUY", int(rng.random() < 0.5)) for _ in range(rng.poisson(1.5))]

    def edge(mid, fair, frac, rng):    # takes when fair is >= 3 c from the mid, on the side of fair
        return [("BUY", 0 if fair > mid else 1) for _ in range(rng.poisson(3.0))] if abs(fair - mid) >= 3 else []

    r = rulefit.fit_taker(taker_world(decoy))
    assert r["found"] is False and r["n_takes"] > 500 and r["reason"].startswith("no rule found")
    assert r["when"]["explained"] is False and r["when"]["ci95"][0] <= rulefit.SKILL_MIN
    assert r["side"]["explained"] is False and r["side"]["ci95"][0] <= 0.5 <= r["side"]["ci95"][1]
    r = rulefit.fit_taker(taker_world(edge))
    assert r["found"] is True and r["reason"] is None
    assert r["when"]["feature"] == "edge_vs_fair" and r["when"]["value"] > 0.3 and r["when"]["ci95"][0] > rulefit.SKILL_MIN
    assert r["side"]["predictor"] == "fair_minus_mid" and r["side"]["balanced_accuracy"] == 1
    # always the same token: the side needs no rule, the timing still does
    r = rulefit.fit_taker(taker_world(lambda mid, fair, frac, rng: [("BUY", 0)] * rng.poisson(1.5)))
    assert r["side"]["explained"] is True and r["found"] is False
    assert rulefit.fit_taker(taker_world(decoy, windows=2))["reason"].startswith("fewer than 3 market windows")
    assert rulefit.fit_taker({"takes": [], "tok": {}, "res": {"grid": {}}, "fair": None})["found"] is False


def test_a_wallet_with_only_taker_fills_gets_a_report_and_exit_0(tmp_path):
    """No maker order to fit: rulefit says so in a report (exit 0), writes no genome, and tests the
    taker fills for a rule. A wallet with no order at all is still an error."""
    frames, orders, tape, _ = world(seed=6, windows=6)
    rng = np.random.default_rng(0)
    takes = [dict(r, role="taker", order_id=f"t{i}", tx=r["tx"]) for i, r in enumerate(tape) if rng.random() < 0.5]
    buy_taker_txs = {r["tx"] for r in takes}
    frames = [(ms, m | {"side": "BUY"} if m.get("transaction_hash") in buy_taker_txs else m)
              for ms, m in frames]
    write(tmp_path, frames, [], takes, windows=6)
    out = tmp_path / "out"
    out.mkdir()
    (out / "genome.json").write_text("{}")
    cli = ["--data", str(tmp_path / "tape"), "--ordermatch-dir", str(tmp_path / "om"), "--orders",
           str(tmp_path / "ordertape.jsonl"), "--from", str(T0), "--to", str(T0 + 3600), "--out", str(out),
           "--tokens", str(tmp_path / "tokens.jsonl")]
    assert main(cli + ["--wallet", W]) == 0
    rep = orjson.loads((out / "fit_report.json").read_bytes())
    assert not (out / "genome.json").exists() and rep["genome"]["status"] == "not_emitted"
    assert rep["taker_rule"]["n_takes"] == len(takes) and rep["taker_rule"]["located_by_print"] == len(takes)
    assert rep["taker_rule"]["found"] is False and rep["explained_by_observed_inputs"]["verdict"] is None
    assert rep["taker_rule"]["takes_in_known_cells"] < len(takes)
    assert "no maker order" in rep["genome"]["reasons"][0] and "INSUFFICIENT EVIDENCE" in (out / "rule.md").read_text()
    with pytest.raises(SystemExit, match="no order of .* could be put in a window"):
        main(cli + ["--wallet", "0x" + "9" * 40])


def test_unqualified_maker_placements_retire_an_older_draft(tmp_path):
    frames, orders, fills, _ = world(windows=1)
    observed = {r["order_id"] for r in fills}
    receipts = [o for o in orders if o["order_id"] in observed][:5]
    del receipts[0]["placement_evidence"]
    receipts[1]["placement_candidates"] = 2
    receipts[2]["placement_evidence"] = "snapshot_only"
    receipts[3]["side"] = "SELL"   # the file claims a unique add for a different signed side
    receipts.append(receipts[4])
    write(tmp_path, frames, receipts, [f | {"verified": True} for f in fills], windows=1)
    out = tmp_path / "out"
    out.mkdir()
    (out / "genome.json").write_text("{}")
    (out / "decision_table.jsonl").write_text("older decisions")
    genome, rep, rows = fit(tmp_path, out)
    assert genome is None and rows == [] and rep["genome"]["status"] == "not_emitted"
    assert rep["label"] == "insufficient maker placement evidence"
    assert rep["inputs"]["counts"]["orders_unqualified_placement_dropped"] == 3
    assert rep["inputs"]["counts"]["orders_not_verified_dropped"] == 1
    assert rep["inputs"]["counts"]["orders_duplicate_receipt_dropped"] == 2
    assert rep["explained_by_observed_inputs"]["verdict"] is None
    assert not (out / "genome.json").exists() and (out / "decision_table.jsonl").read_bytes() == b""
    assert "INSUFFICIENT MAKER PLACEMENT EVIDENCE" in (out / "rule.md").read_text()


def test_a_laddering_wallet_is_flagged_even_when_each_level_fits_tightly(tmp_path):
    """Two bids per step, at the best and 1 c behind: the residual IQR stays under 2 c, so only
    the overlap count can see the ladder."""
    frames, orders, tape, _ = world(seed=8, windows=3)
    extra = []
    for o in [o for o in orders if o["token_id"].startswith("U")]:
        ms = o["placed_venue_ms"] - MS + 5
        p = round(o["price"] - 0.01, 2)
        frames.append(pc(ms, o["token_id"], p, 98, "BUY", "m" + o["token_id"][1:]))
        extra.append(o | {"order_id": o["order_id"] + "b", "price": p, "size": 98.0, "placed_venue_ms": MS + ms,
                          "placed_tw_ns": (MS + ms) * 1_000_000, "filled": 0.0, "fills_at": [], "remainder": 98.0})
    write(tmp_path, frames, orders + extra, tape, windows=3)
    _, rep, _ = fit(tmp_path)
    assert rep["ladder"]["ladder_share"]["value"] > 0.6 and rep["ladder"]["depth_max"] == 2
    assert rep["price_rule"]["bid"]["residual_iqr_c"] < 2
    item = next(c for c in rep["cannot_express"] if c["item"] == "bid: several price levels")
    assert item["measured"]["ladder_share"] == rep["ladder"]["ladder_share"]["value"]


def test_ladder_depth_requires_one_shared_confirmed_interval():
    # The long quote trades alongside two different levels successively. No three coexist.
    orders = [lad_spec(0, .5, 0, 12000, 0), lad_spec(1, .49, 1000, 3000, 0),
              lad_spec(2, .48, 6000, 3000, 0)]
    assert rulefit.fit_ladder(orders)["depth_max"] == 2
    # The rest maximum is the first moment the order was definitely gone. It cannot prove overlap.
    for o in orders:
        o["om"]["rest_min_ms"] = 0
    assert rulefit.fit_ladder(orders)["ladder_share"]["value"] == 0
    for o in orders:
        o["om"].update(rest_min_ms=9000, placement_candidates=2)
    assert rulefit.fit_ladder(orders)["depth_max"] == 1


def test_positive_price_fit_requires_proven_side_and_held_out_timing(tmp_path, monkeypatch):
    frames, orders, tape, _ = world(observe_open=False)  # constant activity wherever the book is visible
    write(tmp_path, frames, orders, tape, om_doc=fixture_match_controls(orders))
    genome, report, _ = fit(tmp_path)
    ex = report["explained_by_observed_inputs"]
    assert ex["side"]["kind"] == "two_sided" and ex["price"]["explained"]
    assert ex["verdict"] is False and not ex["timing"]["explained"] and ex["timing"]["ci95"] == [0, 0] and genome is None
    # A constrained constant-side policy does not exempt a wallet with insufficient side observations.
    monkeypatch.setattr(rulefit, "presence_skill", lambda *args: {"value": 0.5, "ci95": [0.3, 0.7]})
    monkeypatch.setattr(rulefit, "side_test", lambda *args: {"kind": "undetermined", "explained": None,
                                                             "reason": "too few qualifying windows"})
    genome, report, _ = fit(tmp_path)
    assert genome is None and report["explained_by_observed_inputs"]["verdict"] is None
    assert any("side: undetermined" in s for s in report["genome"]["reasons"])


def test_holdout_match_statistics_cannot_enter_training(tmp_path):
    orders = tmp_path / "orders.jsonl"
    orders.write_bytes(b"")
    path = tmp_path / "ordermatch.json"
    data = {"wallets": {W: {"fingerprint": {"total": {"identity_precision_upper": 1}}}}}
    path.write_bytes(orjson.dumps(data | {"params": {"from": T0, "to": T0 + 7200}}))
    with pytest.raises(ValueError, match="match the fit interval separately"):
        rulefit.load_orders(orders, path, W, (T0, T0 + 3600))
    path.write_bytes(orjson.dumps(data))  # legacy input permits rows, excludes unscoped aggregates
    _, doc, counts = rulefit.load_orders(orders, path, W, (T0, T0 + 3600))
    assert not doc and counts["unscoped_match_summary_ignored"] == 1


@pytest.mark.parametrize("control", ["missing", "high_placebo", "too_few", "low_real"])
def test_passing_model_cannot_emit_a_draft_without_a_passing_placement_clock(fitted, tmp_path, control):
    positive, _, _, data = fitted
    assert positive is not None
    om = tmp_path / "om"
    om.mkdir()
    (om / "orders.jsonl").symlink_to(data / "om" / "orders.jsonl")
    if control != "missing":
        summary = orjson.loads((data / "om" / "ordermatch.json").read_bytes())
        # Rounded display rates alone look acceptable. The gate uses the raw paired counts.
        counts = {"high_placebo": (10001, 10001, 1001), "too_few": (29, 29, 0), "low_real": (10001, 9000, 0)}
        n, real, decoy = counts[control]
        summary["wallets"][W].update(observable=n, matched=real, placebo_matched=decoy, match_rate=0.9, placebo_rate=0.1)
        (om / "ordermatch.json").write_bytes(orjson.dumps(summary))
    out = tmp_path / "out"
    out.mkdir()
    (out / "genome.json").write_bytes(orjson.dumps(positive))
    genome, report, rows = run(data / "tape", om, data / "ordertape.jsonl", W, T0, T0 + 3600, out,
                               token_files=[data / "tokens.jsonl"])
    ex = report["explained_by_observed_inputs"]
    assert rows and ex["price"]["explained"] and ex["timing"]["explained"]
    assert ex["placement_clock"]["passed"] is False and ex["verdict"] is None
    assert genome is None and not (out / "genome.json").exists() and "placement clock" in report["genome"]["reason"]


def test_observable_side_switch_does_not_emit_an_unconditional_two_sided_genome(fitted, monkeypatch):
    monkeypatch.setattr(rulefit, "side_test", lambda *args: {
        "kind": "one_sided_by_window", "explained": True, "ci95": [0.8, 1.0], "reason": None})
    genome, report, _ = fit(fitted[3])
    assert report["explained_by_observed_inputs"]["verdict"] is True and genome is None
    assert any("fixed sides" in reason for reason in report["genome"]["reasons"])


@pytest.mark.parametrize(("windows", "status"), [(2, "UNKNOWN"), (5, "UPPER_BOUND"), (6, "ESTIMATED")])
def test_timing_corrections_need_independent_windows(windows, status):
    # Hundreds of fills can all come from just two active windows. Their quantiles
    # cannot establish a 95% correction interval despite strong attribution controls.
    gaps = np.tile(np.linspace(1.0, 2.0, 150), windows)
    clusters = np.repeat([f"w{k}" for k in range(windows)], 150)
    fb = {"p": 0.35, "p_range": [0.25, 0.5], "chance_known": True, "chance_share": 0.1,
          "chance_share_range": [0.05, 0.2]}
    cand = {f"w{k}": [0.1 + i / 10 for i in range(120)] for k in range(windows)}
    cadence = rulefit.fit_cadence(gaps, clusters, {}, {}, fb)
    life = rulefit.fit_life(gaps, clusters, cand, {}, fb)["life_ms"]
    for metric in (cadence, life):
        assert metric["status"] == status
        assert (metric["ci95"] is not None) == (status == "ESTIMATED")
        assert (metric["upper_bound"] is not None) == (status != "UNKNOWN")


def test_selected_price_parameters_have_model_aligned_intervals(fitted):
    _, rep, rows, _ = fitted
    bid, ask = (rep["price_rule"][s] for s in ("bid", "ask"))
    const = bid["selected_parameters"]
    assert const["reference"] == "touch" and const["model"] == "const"
    assert const["offset_c"]["ci95"] == [0, 0]
    assert const["skew_c_per_contract"]["ci95"] is None and const["skew_c_per_contract"]["value"] == 0
    assert "fixed" in const["skew_c_per_contract"]["estimand"]

    skew = ask["selected_parameters"]
    assert skew["model"] == "skew" and "conditional" in skew["interval_scope"]
    for key in ("offset_c", "skew_c_per_contract"):
        parameter = skew[key]
        assert parameter["value"] == pytest.approx(ask["rule"][key], abs=5e-5)
        assert parameter["ci95"][0] <= parameter["value"] <= parameter["ci95"][1]
    assert "zero inventory" in skew["offset_c"]["estimand"]
    assert skew["offset_c"]["ci95"] != ask["offset_c"]["ci95"]  # raw median residual has a different target

    # Repeating many observations in two markets cannot establish parameter uncertainty.
    D = rulefit._side_data(rows, "ask", thresholds(rows))
    D["cl"] = np.where(np.arange(len(D["cl"])) % 2, "w1", "w2")
    sparse = rulefit.fit_price_side(D)["selected_parameters"]
    assert sparse["clusters"] == 2 and sparse["offset_c"]["value"] == skew["offset_c"]["value"]
    assert all(sparse[k]["ci95"] is None for k in ("offset_c", "skew_c_per_contract"))


def test_constant_training_inventory_cannot_identify_a_skew():
    # With flat inventory, least squares can divide the mean between an arbitrary
    # intercept and slope. It would win validation merely because mean > median.
    cl = np.repeat(np.arange(6), 30)
    ph = np.select([cl < 2, cl < 4], [0, 1], default=2)
    d = np.where(ph == 0, (np.arange(len(ph)) % 5 == 0) * 5.0, 1.0)
    mid = np.full(len(d), 50.0)
    D = {"y": mid + d, "w": np.ones_like(d), "inv": np.full_like(d, 10.0),
         "ref": {r: mid for r in rulefit.REFS}, "cl": cl, "phase": ph, "tte": mid, "spread": mid}
    fit = rulefit.fit_price_side(D, force="mid")
    assert fit["selected_parameters"]["model"] == "const"
    assert fit["selected_parameters"]["skew_c_per_contract"]["ci95"] is None
    assert fit["skew_c_per_contract"]["ci95"] is None

def test_real_price_draft_cannot_depend_on_attributed_fill_proxy(tmp_path):
    frames, orders, tape, _ = world(seed=4, windows=6)
    write(tmp_path, frames, orders, tape, windows=6, om_doc=fixture_match_controls(orders))
    # The orders still have the same retrospective flow. Change the study wallet in the
    # controlled source receipt so it cannot certify this wallet's holdings.
    path = tmp_path / "coverage_receipt.json"
    receipt = orjson.loads(path.read_bytes())
    receipt["wallet"] = "other"
    path.write_bytes(orjson.dumps(receipt))
    genome, rep, _ = fit(tmp_path)
    assert rep["inputs"]["fills"]["simulated_flat_start_fills"] is False
    assert rep["expressible_price_rule"]["inventory_skew_evidence"] == "owner_state_unobserved"
    assert not rep["expressible_price_rule"]["skew_kept"]
    assert rep["price_rule"]["ask"]["inventory_basis"] == "diagnostic_association_with_attributed_fills_from_zero"
    assert abs(rep["price_rule"]["ask"]["rule"]["skew_c_per_contract"]) > 0.01
    assert rep["expressible_price_rule"]["skew_c_per_contract"] == 0
    assert genome is None and any("missing reference" in reason for reason in rep["genome"]["reasons"])
