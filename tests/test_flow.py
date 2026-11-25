"""W3 intl informed-flow guard: point-in-time scores, receive-time features, predictiveness."""

from pathlib import Path

import numpy as np
import orjson
import pytest
from pytest import approx

from doppel.lab import flow as fl
from doppel.lab.dossier import NS, BookIndex
from doppel.lab.flow import (EMBARGO_S, FEATURE_NAMES, INFORMED_EXCESS_C, Fill, Scorer,
                             features_from_fills, flow_predictiveness, informedness,
                             informedness_doc, iter_rtds, window_flow_features, write_snapshot)
from doppel.lab.ledger import TokenMap
from doppel.strategy.genome import (FEATURES, Condition, Genome, MakerRule, Source, Universe,
                                    validate)
from doppel.tape.segment import SegmentWriter
from doppel.tape.universe import pmus_slug

FIX = orjson.loads((Path(__file__).parent / "fixtures_formats.json").read_bytes())
T0 = 1795046400  # UTC midnight
L = 300


def tok(start, idx, coin="btc", hz="5m"):
    return f"{coin}{hz}{start}-{idx}"


def slug(start, coin="btc", hz="5m"):
    return f"{coin}-updown-{hz}-{start}"


def rtds(tx, wallet, start, side, idx, price, size, fee, ts, coin="btc", hz="5m", asset=None):
    return orjson.dumps({"topic": "activity", "type": "trades", "payload": {
        "transactionHash": tx, "proxyWallet": wallet, "eventSlug": slug(start, coin, hz),
        "asset": asset or tok(start, idx, coin, hz), "side": side, "outcomeIndex": idx,
        "price": price, "size": size, "fee": fee, "timestamp": ts}}).decode()


def write(root, stream, recs):
    w = SegmentWriter(root, stream)
    for r in sorted(recs, key=lambda r: r[0]):
        w.append(r[1], tw=r[0], meta=r[2] if len(r) > 2 else None)
    w.close()


def tmap_for(path, outcomes):
    """outcomes: {window start: Up payout} for btc 5m windows."""
    tm = TokenMap(path)
    for s, up in outcomes.items():
        for i in (0, 1):
            t = tok(s, i)
            tm.m[t] = {"token": t, "slug": slug(s), "idx": i, "payout": up if i == 0 else 1 - up,
                       "status": "resolved"}
    return tm


def mkfill(wallet, start, side, idx, price, size, tw_s, fee=0.1, ts=None, token=None):
    return Fill(tw=int(round(tw_s * NS)), ts=int(tw_s) - 2 if ts is None else ts, wallet=wallet,
                slug=slug(start), coin="btc", hz="5m", start=start,
                token=token or tok(start, idx), idx=idx, side=side, price=price, size=size, fee=fee)


def released(start):
    return start + L + EMBARGO_S + 1


# ---------------------------------------------------------------- markout + shrinkage
def test_direction_mapping_and_markout():
    S = T0
    for side, idx, d, ud, want in [("BUY", 0, 1, 1, 60.0), ("SELL", 0, -1, -1, -60.0),
                                   ("BUY", 1, 1, -1, -40.0), ("SELL", 1, -1, 1, 40.0)]:
        f = mkfill("w", S, side, idx, 0.4, 10, S + 10)
        assert (f.dir, f.udir) == (d, ud)
        sc = Scorer({slug(S): 1.0}, k=0)  # Up wins: Up pays 1, Down pays 0
        sc.park(f)
        sc.finalize_until(released(S))
        r = sc.score("w", released(S))
        assert r is not None and r["raw"] == approx(want) and r["score"] == approx(want)


def test_shrinkage_math_in_window_units():
    S1, S2 = T0, T0 + L
    out = {slug(S1): 1.0, slug(S2): 0.0}
    sc = Scorer(out, k=2)
    sc.park(mkfill("a", S1, "BUY", 0, 0.5, 10, S1 + 5))  # Up wins: +50
    sc.park(mkfill("a", S2, "BUY", 1, 0.7, 4, S2 + 5))   # Down wins: +30
    sc.park(mkfill("b", S2, "BUY", 0, 0.2, 7, S2 + 6))   # Up loses: -20
    T = released(S2)
    sc.finalize_until(T)
    mu = (50 + 30 - 20) / 3  # equal weight per wallet x window cell
    assert sc.mu(T)[0] == approx(mu)
    a, b = sc.score("a", T), sc.score("b", T)
    assert a is not None and b is not None
    assert a["score"] == approx((80 + 2 * mu) / (2 + 2)) and a["n"] == 2 and a["n_trades"] == 2
    assert a["excess"] == approx(a["score"] - mu) and a["notional"] == approx(0.5 * 10 + 0.7 * 4)
    assert b["score"] == approx((-20 + 2 * mu) / (1 + 2)) and b["n"] == 1
    # inside one cell, rows are share-weighted
    sc2 = Scorer(out, k=0)
    sc2.park(mkfill("c", S1, "BUY", 0, 0.5, 30, S1 + 5))  # +50 x 30 shares
    sc2.park(mkfill("c", S1, "BUY", 1, 0.5, 10, S1 + 6))  # -50 x 10 shares
    sc2.finalize_until(T)
    assert sc2.score("c", T)["raw"] == approx((50 * 30 - 50 * 10) / 40)  # type: ignore[index]


def test_order_slicing_does_not_change_score():
    out = {slug(T0 + i * L): float(i % 3 != 0) for i in range(20)}
    sc = Scorer(out, k=5)
    for i in range(20):
        S = T0 + i * L
        sc.park(mkfill("whale", S, "BUY", 0, 0.5, 20, S + 5))
        for j in range(20):
            sc.park(mkfill("slicer", S, "BUY", 0, 0.5, 1, S + 5 + j * 0.01))
        sc.park(mkfill("other", S, "SELL", 0, 0.45, 3, S + 7))
    T = released(T0 + 19 * L)
    sc.finalize_until(T)
    a, b = sc.score("whale", T), sc.score("slicer", T)
    assert a is not None and b is not None
    assert a["score"] == approx(b["score"]) and a["n"] == b["n"] == 20
    assert a["notional"] == approx(b["notional"]) and (a["n_trades"], b["n_trades"]) == (20, 400)


def test_mid30_label_and_coverage():
    S = T0
    idx = BookIndex()
    idx.add_quote(tok(S, 0), (S + 40) * NS - 1, 0.49, 0.51)  # mid 50c just before ts + 30
    idx.finalize()
    sc = Scorer({slug(S): 0.0}, k=0, k_mid30=0, books=idx)
    sc.park(mkfill("w", S, "BUY", 0, 0.40, 10, S + 12, ts=S + 10))   # labeled: +10c
    sc.park(mkfill("w", S, "BUY", 0, 0.40, 10, S + 102, ts=S + 100))  # no quote at +30 s
    sc.finalize_until(released(S))
    r = sc.score("w", released(S))
    assert r is not None and r["score_mid30"] == approx(10.0) and r["n_mid30"] == 1
    assert r["raw"] == approx(-40.0)  # settlement label is independent of the mid label
    assert sc.counts["mid30_labeled_rows"] == 1 and sc.counts["taker_rows"] == 2


# ---------------------------------------------------------------- point in time
def _pit_tape(root, with_b: bool):
    SA = T0 + 10 * L
    SB = SA + L
    rows = [((SA + 52) * NS, rtds("0x1", "w", SA, "BUY", 0, 0.5, 10, 0.1, SA + 50)),
            # received after end + EMBARGO_S: never part of the cell
            ((SA + L + EMBARGO_S + 1) * NS, rtds("0x2", "late", SA, "BUY", 0, 0.5, 10, 0.1, SA + 60))]
    if with_b:
        rows.append(((SB + 52) * NS, rtds("0x3", "w", SB, "BUY", 0, 0.5, 10, 0.1, SB + 50)))
    write(root, "rtds_a", rows)
    return SA, SB


def test_window_ending_inside_embargo_never_affects_score(tmp_path):
    tm = tmap_for(tmp_path / "t.jsonl", {T0 + 10 * L: 1.0, T0 + 11 * L: 0.0})  # w wins A, loses B
    SA, SB = _pit_tape(tmp_path / "with", True)
    _pit_tape(tmp_path / "without", False)
    as_of = SB + L + EMBARGO_S - 1  # B ended at as_of - 59: still embargoed
    with_b = informedness(tmp_path / "with", tm, as_of, k=0)
    without_b = informedness(tmp_path / "without", tm, as_of, k=0)
    assert with_b == without_b
    assert with_b["w"]["n"] == 1 and with_b["w"]["raw"] == approx(50.0) and "late" not in with_b
    later = informedness(tmp_path / "with", tm, SB + L + EMBARGO_S + 1, k=0)  # B now released
    assert later["w"]["n"] == 2 and later["w"]["raw"] == approx(0.0)


def test_snapshot_is_never_overwritten(tmp_path):
    tm = tmap_for(tmp_path / "t.jsonl", {T0 + 10 * L: 1.0, T0 + 11 * L: 0.0})
    _pit_tape(tmp_path / "data", True)
    as_of = T0 + 12 * L + EMBARGO_S + 1
    doc = informedness_doc(tmp_path / "data", tm, as_of, k=0)
    p = write_snapshot(tmp_path / "lab", doc)
    assert p.name == "informedness-20261119.json" and orjson.loads(p.read_bytes())["as_of"] == as_of
    assert doc["mid30"]["coverage"] == 0.0 and doc["mu_c"] == approx(0.0) and doc["cells"] == 2
    with pytest.raises(FileExistsError):
        write_snapshot(tmp_path / "lab", doc)


# ---------------------------------------------------------------- RTDS dedup
def test_rtds_dedup_keeps_earliest_receipt_and_genuine_repeats(tmp_path):
    S = T0 + 3 * 3600 - 100
    r1 = rtds("0xA", "W", S, "BUY", 0, 0.5, 10, 0.1, S + 10)
    edge = rtds("0xB", "W", S, "SELL", 1, 0.3, 5, 0.1, S + 95)
    hour = T0 + 3 * 3600
    write(tmp_path, "rtds_a", [((S + 12) * NS + 500, r1), ((S + 12) * NS + 600, r1),  # 2 real fills
                               (hour * NS - 1000, edge),                               # 10:59:59.99
                               ((S + 13) * NS, rtds("0xC", "V", 0, "BUY", 0, 0.5, 1, 0.1, 0)
                                .replace("btc-updown-5m-0", "nba-lal-bos")),            # not a window
                               ((S + 14) * NS, "PONG")])
    write(tmp_path, "rtds_b", [((S + 12) * NS + 100, r1), (hour * NS + 1000, edge)])   # 11:00:00.00
    fills = [f for _, fs in iter_rtds(tmp_path, [hour - 3600, hour]) for f in fs]
    assert [f.tw for f in fills] == [(S + 12) * NS + 100, (S + 12) * NS + 600, hour * NS - 1000]
    assert fills[2].udir == 1 and fills[2].dir == -1 and fills[0].wallet == "w"


# ---------------------------------------------------------------- features
def test_features_use_receive_time_not_trade_time():
    S = T0
    f = mkfill("w", S, "BUY", 0, 0.5, 10, S + 110, ts=S + 100)   # printed at 100, received 110
    late = mkfill("w", S, "SELL", 0, 0.5, 4, S + 112.4, ts=S + 101)  # received 112.4
    maker = mkfill("m", S, "BUY", 0, 0.5, 999, S + 105, fee=0.0)     # maker rows never count
    out = features_from_fills([f, late, maker], S, S + L, {}, seconds=range(S + 100, S + 150))
    x = {d: v["intl_flow_up_usd_30s"] for d, v in out.items()}
    assert x[S + 109] == 0 and x[S + 110] == 5.0 and x[S + 112] == 5.0  # 112.4 not yet received
    assert x[S + 113] == 3.0 and x[S + 139] == 3.0 and x[S + 140] == -2.0 and x[S + 143] == 0
    assert out[S + 114]["intl_flow_up_usd_5s"] == 3.0 and out[S + 115]["intl_flow_up_usd_5s"] == -2.0
    assert out[S + 109]["intl_informed_share_30s"] is None
    scores = {"w": {"excess": 25.0}}
    out2 = features_from_fills([f], S, S + L, scores, seconds=[S + 120])
    assert out2[S + 120]["intl_informed_flow_30s"] == 5.0          # weight clipped to 1
    assert out2[S + 120]["intl_informed_share_30s"] == 1.0
    out3 = features_from_fills([f], S, S + L, {"w": {"excess": -3.0}}, seconds=[S + 120])
    assert out3[S + 120]["intl_informed_flow_30s"] == approx(-1.5)  # weight -0.3
    assert out3[S + 120]["intl_informed_share_30s"] == 0.0
    assert set(out[S + 120]) == set(FEATURE_NAMES)


def test_wallet_known_only_from_this_window_weighs_zero():
    S0, S1 = T0, T0 + 2 * L
    sc = Scorer({slug(S0): 1.0, slug(S1): 1.0}, k=1)
    sc.park(mkfill("h", S0, "BUY", 0, 0.5, 10, S0 + 50))  # h won S0
    sc.park(mkfill("u", S0, "BUY", 1, 0.5, 10, S0 + 50))  # u lost S0
    x_f = mkfill("x", S1, "BUY", 0, 0.5, 100, S1 + 50)     # x's only window is the one under test
    h_f = mkfill("h", S1, "BUY", 0, 0.5, 10, S1 + 50)
    sc.park(x_f)
    sc.park(h_f)
    sc.finalize_until(released(S1))  # worst case: S1 itself is already released in the scorer
    v = sc.view(S1)
    assert v.get("x") is None and "x" not in v and "h" in v
    w_h = fl.weight(v["h"])
    feats = features_from_fills([x_f, h_f], S1, S1 + L, v)
    assert w_h > 0 and feats[S1 + 60]["intl_informed_flow_30s"] == approx(5.0 * w_h)
    # x only gets a score once S1 is settled and embargoed, i.e. after the window
    later = sc.score("x", released(S1))
    assert later is not None and later["excess"] > 0


def test_intl_mid_is_as_of():
    S = T0
    idx = BookIndex()
    idx.add_quote("UP", (S + 10) * NS - 1, 0.40, 0.42)
    idx.add_quote("UP", (S + 10) * NS, 0.50, 0.52)       # received exactly at S+10: not before it
    idx.add_quote("UP", (S + 20) * NS + 1, 0.60, 0.62)
    idx.add_quote("UP", (S + 30) * NS - 5, 0.70, 1.0)    # empty ask side
    idx.finalize()
    f = mkfill("w", S, "BUY", 0, 0.5, 1, S + 1, token="UP")
    out = features_from_fills([f], S, S + L, {}, idx=idx)  # Up token found from the idx-0 row
    m = {d: v["intl_mid_up_c"] for d, v in out.items()}
    assert m[S + 9] is None and m[S + 10] == 41.0 and m[S + 11] == 51.0 and m[S + 13] == 51.0
    assert m[S + 14] is None                      # 4 s old
    assert m[S + 20] is None and m[S + 21] == 61.0  # the S+20+1ns quote is not visible at S+20
    assert m[S + 31] is None                      # one-sided book -> no mid


def test_window_features_on_real_clob_shapes(tmp_path):
    a_up = "37219683974299077682722837222089109669361807475653020816058588933719117325533"
    w = SegmentWriter(tmp_path, "clob_btc_5m")
    for k in ("clob_price_change", "clob_book", "clob_last_trade_price"):
        for r in FIX[k]:
            w.append(orjson.dumps(r["msg"]).decode(), tw=r["tw"])
    w.close()
    S = FIX["clob_book"][0]["tw"] // NS // L * L
    write(tmp_path, "rtds_a", [((S - 9) * NS, rtds("0x1", "w", S, "BUY", 0, 0.65, 10, 0.1, S - 10,
                                                   asset=a_up))])
    feats = window_flow_features(tmp_path, "btc", "5m", S, {})
    d = FIX["clob_book"][1]["tw"] // NS + 1
    assert feats[d]["intl_mid_up_c"] == 50.0 and feats[d - 1]["intl_mid_up_c"] is None
    assert feats[d + 4]["intl_mid_up_c"] is None  # > 3 s old
    assert feats[S]["intl_flow_up_usd_30s"] == 6.5 and feats[S + 21]["intl_flow_up_usd_30s"] == 0


# ---------------------------------------------------------------- predictiveness (synthetic)
def synth_tape(root, n=80, informed=True, seed=11):
    """btc 5m windows. Settlement series = random walk + a +-40 USD jump at start+155. The informed
    wallet buys the eventual winner at start+120 (received +122); two random wallets trade random
    directions at fixed offsets; a maker row is noise that must be ignored. PM US mid tracks the
    series and jumps with it."""
    rng = np.random.default_rng(seed)
    first = T0 + 3600
    starts = [first + i * L for i in range(n)]
    lo, hi = first - 400, starts[-1] + L + 100
    steps = rng.normal(0.0, 1.0, hi - lo)
    for s in starts:
        steps[s + 155 - lo] += 40.0 * rng.choice([-1.0, 1.0])
    ser = {lo + i: float(v) for i, v in enumerate(90000.0 + np.cumsum(steps))}
    refs = []
    for c0 in range(lo, hi, 3600):
        c1 = min(c0 + 3599, hi - 1)
        body = {"series": [{"candles": [{"timestamp": t, "close": ser[t]} for t in range(c0, c1 + 1)]}]}
        refs.append(((c1 + 5) * NS, orjson.dumps(body).decode(),
                     {"asset": "crypto:btc", "status": 200, "from": c0, "to": c1}))
    write(root, "ref_prices", refs)
    outcomes, rt, pm = {}, [], []
    for s in starts:
        up = 1.0 if ser[s + L - 1] >= ser[s - 1] else 0.0
        outcomes[s] = up
        if informed:
            rt.append(((s + 122) * NS, rtds(f"0xi{s}", "0xinf", s, "BUY", 0 if up else 1, 0.5, 100,
                                            1.0, s + 120)))
        for who, offs in (("0xrnd", (75, 130)), ("0xrnd2", (80, 135))):
            for o in offs:
                side = "BUY" if rng.random() < 0.5 else "SELL"
                rt.append(((s + o + 2) * NS, rtds(f"0x{who}{s}{o}", who, s, side, int(rng.random() < 0.5),
                                                  0.5, float(rng.integers(5, 21)), 0.1, s + o)))
        rt.append(((s + 140) * NS, rtds(f"0xm{s}", "0xmaker", s, "SELL", 0, 0.5, 500, 0.0, s + 138)))
        ptb = ser[s - 1]
        for t in range(s, s + L):
            mid = min(0.97, max(0.03, round(0.5 + 0.005 * (ser[t] - ptb), 2)))
            md = {"marketSlug": pmus_slug("btc", "5m", s), "state": "MARKET_STATE_OPEN",
                  "bids": [{"px": {"value": f"{mid - 0.01:.4f}", "currency": "USD"}, "qty": "100.0000"}],
                  "offers": [{"px": {"value": f"{mid + 0.01:.4f}", "currency": "USD"}, "qty": "100.0000"}]}
            pm.append((t * NS + 300_000_000, orjson.dumps(
                {"requestId": "md", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA",
                 "marketData": md}).decode()))
    write(root, "rtds_a", rt)
    write(root, "rtds_b", [(tw + 7_000_000, raw) for tw, raw in rt])  # second socket, later
    write(root, "pmus_ws_5m", pm)
    return starts, outcomes


@pytest.fixture(scope="module")
def informed_tape(tmp_path_factory):
    root = tmp_path_factory.mktemp("informed")
    starts, outcomes = synth_tape(root)
    return root, starts, tmap_for(root / "tokens.jsonl", outcomes)


def _cell(res, feat, tgt, var="feature", split="late", fr="0.5"):
    return res["results"][feat][tgt][var][split][fr]


def test_informed_wallet_scores_high(informed_tape):
    root, starts, tm = informed_tape
    sc = informedness(root, tm, released(starts[-1]), k=10)
    assert sc["0xinf"]["excess"] >= INFORMED_EXCESS_C and sc["0xinf"]["raw"] == approx(50.0)
    assert max(sc, key=lambda w: sc[w]["score"]) == "0xinf"
    assert sc["0xrnd"]["excess"] < INFORMED_EXCESS_C and "0xmaker" not in sc
    assert sc["0xinf"]["n"] == len(starts) and sc["0xinf"]["n_trades"] == len(starts)  # deduped


def test_informed_flow_predicts_and_placebo_is_chance(informed_tape):
    root, starts, tm = informed_tape
    res = flow_predictiveness(root, tm, starts[0], starts[-1] + 1, assets=("btc",), hzs=("5m",),
                              k=10, tau_fracs=(0.5,))
    n = len(starts)
    assert res["windows"] == n and res["coverage"]["settle"] == n and res["coverage"]["pm10"] == n
    assert res["coverage"]["pmus_source"] == {"ws": n}
    # scores are as of each window START: the first two windows have no released cell yet
    # (as-of-end scoring would give n - 1, a global snapshot n)
    act = res["coverage"]["active_feature"]
    assert act["intl_informed_flow_30s"] == n - 2 and act["intl_flow_up_usd_30s"] == n
    for tgt in ("settle", "pm10"):
        c = _cell(res, "intl_informed_flow_30s", tgt)
        assert c["auc"] is not None and c["auc"] > 0.9, (tgt, c)
        p = _cell(res, "intl_informed_flow_30s", tgt, "placebo")
        assert p["auc"] is not None and abs(p["auc"] - 0.5) < 0.2, (tgt, p)
        p = _cell(res, "intl_flow_up_usd_30s", tgt, "placebo", "all")
        assert p["auc"] is not None and abs(p["auc"] - 0.5) < 0.2, (tgt, p)


def test_random_wallets_do_not_predict(tmp_path):
    starts, outcomes = synth_tape(tmp_path, informed=False, seed=5)
    tm = tmap_for(tmp_path / "tokens.jsonl", outcomes)
    res = flow_predictiveness(tmp_path, tm, starts[0], starts[-1] + 1, assets=("btc",),
                              hzs=("5m",), k=10, tau_fracs=(0.5,))
    for feat in ("intl_flow_up_usd_30s", "intl_informed_flow_30s"):
        for tgt in ("settle", "pm10"):
            c = _cell(res, feat, tgt, split="all")
            assert c["auc"] is not None and abs(c["auc"] - 0.5) < 0.2, (feat, tgt, c)


def test_cli_end_to_end_without_network(informed_tape, tmp_path, capsys):
    root, starts, tm = informed_tape
    lab = tmp_path / "lab"
    TokenMap(lab / "tokens.jsonl")._save(list(tm.m.values()))
    as_of = released(starts[-1])
    base = ["--data", str(root), "--lab", str(lab), "--no-fetch", "--k", "10"]
    assert fl.main(["scores", *base, "--as-of", str(as_of)]) == 0
    assert fl.main(["scores", *base, "--as-of", str(as_of)]) == 1  # never overwritten
    snap = next((lab / "flow").glob("informedness-*.json"))
    win = ["--asset", "btc", "--hz", "5m", "--start", str(starts[-1])]
    assert fl.main(["features", *base, *win, "--scores", str(snap)]) == 1  # snapshot after start
    assert fl.main(["features", *base, *win]) == 0
    feats = orjson.loads((lab / "flow" / f"features-btc-5m-{starts[-1]}.json").read_bytes())
    assert feats[str(starts[-1] + 150)]["intl_informed_flow_30s"] != 0
    assert fl.main(["predict", *base, "--from", str(starts[0]), "--to", str(starts[10]),
                    "--assets", "btc", "--hz", "5m"]) == 0
    assert next((lab / "flow").glob("predict-*.json")).exists()
    capsys.readouterr()


def test_genome_vocabulary_has_flow_features():
    assert set(FEATURE_NAMES) <= set(FEATURES)
    g = Genome(name="guarded", mode="maker", universe=Universe(venue="pmus"),
               source=Source(kind="manual"),
               maker=MakerRule(pause_when=(Condition("intl_informed_flow_30s", ">", 200.0),
                                           Condition("intl_informed_flow_30s", "<", -200.0))))
    assert validate(g) == []
    assert g.maker_quotes({"tau_s": 10, "tte_s": 200, "fair_up": 0.5, "inventory_up": 0.0,
                           "intl_informed_flow_30s": 500.0}) == (None, None)
