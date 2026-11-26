"""Astra #3a same-settlement structural scanner: synthetic fixtures only (no network)."""

from pathlib import Path

import orjson
import pytest

from doppel.lab import structural as st
from doppel.lab.structural import (Book, Contract, Excluded, Fees, Params, build_combos, classes,
                                   normalise_kalshi, normalise_pmus, relations, scan_class, summarize)
from doppel.lab.xvenue import kalshi_fee, pmus_fee
from doppel.tape.segment import SegmentWriter

T = 1795653900            # settlement instant used by the synthetic class (2026-11-26 00:45Z)
ISO_T = "2026-11-26T00:45:00Z"

PM_MEAN = ("Outcome sourced from CF Benchmarks' Bitcoin Real-Time Index (BRTI). The price at the beginning "
           "and at the end of the time range is determined the same way: in the last minute before each of "
           "those two times, 60 BRTI prices are collected. The official value for that time is the simple "
           "average of these prices, rounded to the nearest 2 decimal places.")
PM_LADDER = ("This market will settle to Yes if the price of Bitcoin is at or above $X at 5:00PM ET. Outcome "
             "sourced from CF Benchmarks' Bitcoin Real-Time Index (BRTI). The settlement value is determined "
             "as follows: in the last minute before 5:00PM ET, 60 BRTI prices are collected. The official and "
             "final value is the simple average of these prices, rounded to the nearest 2 decimal places.")
PM_TRIM = ("Outcome sourced from CF Benchmarks' Bitcoin Real-Time Index. The market settles based on the CF "
           "Bitcoin Real-Time Index (BRTI) using a trimmed mean calculation over the sixty second period ending "
           "at 12:00 AM ET on January 1, 2027, excluding the top 20% and bottom 20% of values.")
K15_PRIM = ("If the simple average of the sixty seconds of CF Benchmarks' BRTI before 7:45 PM EST on Nov 25, "
            "2026 is at least the simple average of the sixty seconds of CF Benchmarks' BRTI before 7:30 PM EST")
K15_SEC = ("At the last minute before expiration, 60 RTI prices are collected. The official and final value is "
           "the average of these prices, rounded to the nearest 2 decimal places.")
KD_PRIM = ("If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI) before "
           "5 PM EST is above 68749.99 at 5 PM EST on Nov 26, 2026, then the market resolves to Yes.")
KD_SEC = ("At the last minute before expiration, 60 RTI prices are collected. The official and final value is "
          "the average of these prices.")


def amt(v):
    return None if v is None else {"value": v, "currency": "USD"}


def pm(slug, mt, *, desc=PM_MEAN, end=ISO_T, start=None, ptb=None, strike=None, lo=None, hi=None,
       kind="ASSET_PRICE_RANGE_KIND_UNSPECIFIED", horizon="15m", index="BRTI", dec=2):
    return {"slug": slug, "description": desc, "assetPriceTerms": {
        "marketType": f"ASSET_PRICE_MARKET_TYPE_{mt}", "asset": {"assetClass": "ASSET_CLASS_CRYPTO", "symbol": "btc"},
        "indexSymbol": index, "horizon": horizon, "windowStart": start, "windowEnd": end, "priceToBeat": amt(ptb),
        "strike": amt(strike), "rangeLower": amt(lo), "rangeUpper": amt(hi), "rangeKind": kind,
        "direction": "ASSET_PRICE_DIRECTION_UNSPECIFIED", "priceDecimals": dec}}


def ka(ticker, stype, *, floor=None, cap=None, close=ISO_T, prim=KD_PRIM, sec=KD_SEC, event=None, open_t=None):
    return {"ticker": ticker, "event_ticker": event or ticker.rsplit("-", 1)[0], "strike_type": stype,
            "floor_strike": floor, "cap_strike": cap, "close_time": close, "open_time": open_t,
            "rules_primary": prim, "rules_secondary": sec}


def C(venue, key, lo, hi, *, instant=T, stat="mean60", rounding="stated", group="g", family="f", start=None,
      dec=2):
    return Contract(venue, key, "BRTI", stat, dec, rounding, instant, lo, hi, family, group, start, 0.0)


# ---------------------------------------------------------------------------------------------- normaliser

def test_pmus_updown_strike_and_ranges():
    up = normalise_pmus(pm("cpc-btc-updown-15m-2026-11-26-0030z", "UP_DOWN", start="2026-11-26T00:30:00Z",
                           ptb="60786.56"))
    assert isinstance(up, Contract)
    assert (up.stat, up.rounding, up.instant, up.lo, up.hi) == ("mean60", "stated", T, 6078656, None)
    assert up.start == T - 900 and up.family == "pmus:updown-15m"
    k = normalise_pmus(pm("above-38000", "ABOVE_BELOW", desc=PM_LADDER, strike="38000", horizon="weekly"))
    assert isinstance(k, Contract) and (k.lo, k.hi) == (3800000, None)        # at or above: tie pays Yes
    mid = normalise_pmus(pm("r-38000", "RANGE", desc=PM_LADDER, lo="38000.00", hi="38499.99",
                            kind="ASSET_PRICE_RANGE_KIND_INTERIOR", horizon="weekly"))
    below = normalise_pmus(pm("r-below", "RANGE", desc=PM_LADDER, hi="37999.99",
                              kind="ASSET_PRICE_RANGE_KIND_BELOW", horizon="weekly"))
    above = normalise_pmus(pm("r-above", "RANGE", desc=PM_LADDER, lo="38500.00",
                              kind="ASSET_PRICE_RANGE_KIND_ABOVE", horizon="weekly"))
    assert isinstance(mid, Contract) and (mid.lo, mid.hi) == (3800000, 3850000)   # inclusive both bounds
    assert isinstance(below, Contract) and (below.lo, below.hi) == (None, 3800000)
    assert isinstance(above, Contract) and (above.lo, above.hi) == (3850000, None)
    assert mid.group == below.group == above.group


def test_pmus_exclusions_never_guess():
    def reason(m):
        r = normalise_pmus(m)
        assert isinstance(r, Excluded)
        return r.reason
    assert reason(pm("hit", "ONE_TOUCH", strike="150000", horizon="monthly")) == "one_touch_path_dependent"
    assert reason(pm("u", "UP_DOWN", start="2026-11-26T00:30:00Z")) == "strike_pending"
    assert reason(pm("x", "UNSPECIFIED")) == "unknown_market_type"
    assert reason(pm("x", "ABOVE_BELOW", strike="1", desc="Settles on the BRTI price.")) == "statistic_unparsed"
    assert reason(pm("x", "ABOVE_BELOW", strike="1", dec=4)) == "decimals_conflict"
    assert reason(pm("x", "ABOVE_BELOW", strike="1", index="ETHUSD_RTI")) == "index_text_mismatch"
    assert reason(pm("x", "RANGE", lo="1.00", kind="ASSET_PRICE_RANGE_KIND_INTERIOR")) == "range_bounds_inconsistent"
    assert reason({"slug": "tesla", "description": "stablecoin", "assetPriceTerms": None}) == "no_asset_terms"


def test_pmus_yearly_range_is_trimmed_mean_from_text():
    # typed terms say RANGE, but the rules text says trimmed mean: a different statistic, own class
    r = normalise_pmus(pm("yr-147500", "RANGE", desc=PM_TRIM, lo="145000.00", hi="149999.99",
                          kind="ASSET_PRICE_RANGE_KIND_INTERIOR", horizon="yearly"))
    assert isinstance(r, Contract) and r.stat == "trim20_60s" and r.rounding == "unstated"


def test_kalshi_normaliser():
    k15 = normalise_kalshi(ka("KXBTC15M-26NOV251945-45", "greater_or_equal", floor=60786.56, prim=K15_PRIM,
                              sec=K15_SEC, open_t="2026-11-26T00:30:00Z"), {"KXBTC15M": {"fee_type": "quadratic"}})
    assert isinstance(k15, Contract)
    assert (k15.index, k15.stat, k15.rounding, k15.lo, k15.hi) == ("BRTI", "mean60", "stated", 6078656, None)
    assert k15.start == T - 900 and k15.maker_coef == 0.0
    kd = normalise_kalshi(ka("KXBTCD-26NOV2617-T68749.99", "greater", floor=68749.99))
    assert isinstance(kd, Contract) and (kd.lo, kd.hi, kd.rounding) == (6875000, None, "unstated")
    assert kd.maker_coef is None                                  # no series fee info -> maker unknown
    b = normalise_kalshi(ka("KXBTC-26NOV2617-B49375", "between", floor=49250, cap=49499.99))
    lt = normalise_kalshi(ka("KXBTC-26NOV2617-T49250", "less", cap=49250))
    assert isinstance(b, Contract) and (b.lo, b.hi) == (4925000, 4950000)
    assert isinstance(lt, Contract) and (lt.lo, lt.hi) == (None, 4925000)
    for m, why in [(ka("X-1", "functional", floor=1), "strike_type_unsupported"),
                   (ka("KXBTC15M-X-1", "greater_or_equal", prim=K15_PRIM, sec=K15_SEC), "strike_pending"),
                   (ka("X-2", "greater", floor=1, prim="If the price of bitcoin is above 1"), "index_unparsed")]:
        r = normalise_kalshi(m)
        assert isinstance(r, Excluded) and r.reason == why


def test_refuses_mismatched_instant_or_statistic():
    a = normalise_pmus(pm("u15", "UP_DOWN", start="2026-11-26T00:30:00Z", ptb="100.00"))
    b = normalise_kalshi(ka("KXBTC15M-B-00", "greater_or_equal", floor=100.0, prim=K15_PRIM, sec=K15_SEC,
                            close="2026-11-26T00:45:01Z"))
    c = normalise_pmus(pm("yr", "RANGE", desc=PM_TRIM, lo="100.00", kind="ASSET_PRICE_RANGE_KIND_ABOVE"))
    assert isinstance(a, Contract) and isinstance(b, Contract) and isinstance(c, Contract)
    cl = classes([a, b, c])
    assert len(cl) == 3 and all(len(v) == 1 for v in cl.values())
    assert all(build_combos(v)[0] == [] for v in cl.values())
    # a rounding grid mismatch inside one class is refused, not compared
    combos, skips = build_combos([C("pmus", "a", 100, None), C("pmus", "b", 10000, None, dec=4)])
    assert combos == [] and skips[0]["reason"] == "rounding_mismatch"


# ---------------------------------------------------------------------------------------------- relations

def test_relations_ordered_equal_disjoint_cover():
    lo_k, hi_k = C("pmus", "k1", 100, None), C("kalshi", "k2", 105, None)
    assert [r[:3] for r in relations(lo_k, hi_k)] == [("yes", "no", "contains")]   # X>=105 implies X>=100
    eq = relations(C("pmus", "a", 100, None), C("kalshi", "b", 100, None))
    assert {r[2] for r in eq} == {"contains", "contained"}                         # same contract: both ways
    assert [r[:3] for r in relations(C("x", "a", None, 100), C("x", "b", 100, 200))] == [("no", "no", "disjoint")]
    assert ("yes", "yes", "covers") in [r[:3] for r in relations(C("x", "a", None, 100), C("x", "b", 90, None))]
    assert relations(C("x", "a", 100, 200), C("x", "b", 150, 300)) == []          # overlap: nothing riskless


def test_tie_unverified_only_for_unstated_rounding_at_zero_margin():
    pmk = C("pmus", "pm", 6875000, None)
    kd = C("kalshi", "kd", 6875000, None, rounding="unstated")
    combos, _ = build_combos([pmk, kd])
    assert combos and all(c.tie_unverified for c in combos)
    combos, _ = build_combos([pmk, C("kalshi", "kd2", 6870000, None, rounding="unstated")])
    assert combos and not any(c.tie_unverified for c in combos)
    combos, _ = build_combos([pmk, C("kalshi", "k15", 6875000, None)])
    assert combos and not any(c.tie_unverified for c in combos)


def test_tiling_gapless_and_incomplete():
    g = [C("kalshi", "lo", None, 100, group="E"), C("kalshi", "m", 100, 200, group="E"),
         C("kalshi", "hi", 200, None, group="E")]
    combos, skips = build_combos(g)
    kinds = {c.kind: c for c in combos if c.kind.startswith("tiling")}
    assert kinds["tiling_no"].payout == 2 and kinds["tiling_yes"].payout == 1 and not skips
    combos, skips = build_combos([C("pmus", "lo", None, 100, group="E"), C("pmus", "m", 100, 200, group="E"),
                                  C("pmus", "m2", 200, 300, group="E")])
    assert {c.kind for c in combos} >= {"tiling_no"} and "tiling_yes" not in {c.kind for c in combos}
    assert skips == [{"type": "static_skip", "cls": f"BRTI|mean60|{T}", "reason": "incomplete_tiling", "count": 1}]


# ---------------------------------------------------------------------------------------------- fees

def test_fees_match_xvenue_and_are_parameterised():
    f = Fees()
    for p, n in [(0.5, 1), (0.5, 100), (0.37, 13)]:
        assert f.taker("pmus", p, n) == pmus_fee(p, n)
        assert f.taker("kalshi", p, n) == kalshi_fee(p, n)
    assert Fees(pmus_taker=0.0695).taker("pmus", 0.5, 100) == 1.74
    k = C("kalshi", "k", 1, None)
    assert f.maker(k, 0.5, 10) == 0.0
    assert f.maker(Contract("kalshi", "k", "BRTI", "mean60", 2, "stated", T, 1, None, "f", "g", None, None), 0.5, 10) is None


# ---------------------------------------------------------------------------------------------- scanner

def ladder():
    """PM US 5m and 15m windows ending at T with strikes 100.00 < 101.00 (same statistic, same instant)."""
    low = C("pmus", "pm5", 10000, None, family="pmus:updown-5m", start=T - 300)
    high = C("pmus", "pm15", 10100, None, family="pmus:updown-15m", start=T - 900)
    return low, high


def flat(book, t0, t1, bid, bq, ask, aq, step=1.0):
    t = t0
    while t <= t1:
        book.add(t, bid, bq, ask, aq)
        t += step


def test_monotonicity_violation_episode_valued_once():
    low, high = ladder()
    s0 = T - 200
    bl, bh = Book(), Book()
    flat(bl, s0 - 10, T, 0.38, 50, 0.40, 50)          # YES(X>=100) ask 0.40
    flat(bh, s0 - 10, s0 - 1, 0.20, 80, 0.22, 80)     # consistent before s0
    flat(bh, s0, s0 + 2, 0.55, 30, 0.57, 30)          # YES(X>=101) bid 0.55 > ask of the easier strike
    flat(bh, s0 + 3, T, 0.20, 80, 0.22, 80)
    books = {("pmus", "pm5"): bl, ("pmus", "pm15"): bh}
    rows = list(scan_class([low, high], books, T - 300, T))
    opps = [r for r in rows if r["type"] == "opportunity"]
    assert len(opps) == 1
    o = opps[0]
    assert (o["start"], o["secs"], o["relation"]) == (s0, 3, "contained")
    assert [(x["key"], x["side"], x["price"]) for x in o["legs"]] == [("pm15", "no", 0.45), ("pm5", "yes", 0.40)]
    assert o["gross_c"] == 15.0 and o["size"] == 30
    fee = (pmus_fee(0.40, 30) + pmus_fee(0.45, 30)) / 30
    assert o["net_c"] == round(100 * (0.15 - fee), 2)
    assert o["dollars"] == round((0.15 - fee) * 30, 2)               # first second x size, once
    assert o["flags"] == {"kalshi_rest": False, "tie_unverified": False}
    cov = [r for r in rows if r["type"] == "coverage"][0]
    assert cov["evaluated"] > 0


def test_stale_and_non_persistent_become_skips():
    low, high = ladder()
    s0 = T - 200
    bl, bh = Book(), Book()
    flat(bl, s0 - 10, T, 0.38, 50, 0.40, 50)
    bh.add(s0 - 4.5, 0.55, 30, 0.57, 30)              # fresh for one second, then stale (age > 1 s)
    bh.add(s0 + 10, 0.20, 80, 0.22, 80)
    bh.add(s0 + 50, 0.55, 30, 0.57, 30)               # single fresh second, then gone
    bh.add(s0 + 51, 0.20, 80, 0.22, 80)
    flat(bh, s0 + 52, T, 0.20, 80, 0.22, 80)
    rows = list(scan_class([low, high], {("pmus", "pm5"): bl, ("pmus", "pm15"): bh}, T - 300, T))
    assert not [r for r in rows if r["type"] == "opportunity"]
    sk = {r["reason"]: r for r in rows if r["type"] == "skip"}
    assert sk["stale"]["seconds"] == 13               # s0-3 .. s0+9
    assert sk["not_persistent"]["runs"] == 2 and sk["not_persistent"]["seconds"] == 2   # s0-4 and s0+50


def test_skew_is_a_skip_and_one_sided_wing_is_usable():
    low, high = ladder()
    s0 = T - 200
    bl, bh = Book(), Book()
    flat(bl, s0 - 10, T, None, 0, 0.40, 50, step=1.0)            # ask-only wing: still a usable YES leg
    flat(bh, s0 - 10.6, T, 0.55, 30, None, 0, step=1.0)          # bid-only, 0.6 s behind -> skew
    rows = list(scan_class([low, high], {("pmus", "pm5"): bl, ("pmus", "pm15"): bh}, T - 300, T))
    assert not [r for r in rows if r["type"] == "opportunity"]
    assert {r["reason"] for r in rows if r["type"] == "skip"} == {"skew"}


def test_one_leg_failure_moves():
    low, high = ladder()
    s0 = T - 200
    bl, bh = Book(), Book()
    flat(bl, s0 - 10, s0 + 2, 0.38, 50, 0.40, 50)
    bl.add(s0 + 3, 0.40, 50, 0.43, 50)                          # YES leg 3 c worse after +3 s
    flat(bl, s0 + 4, s0 + 30, 0.40, 50, 0.43, 50)
    flat(bh, s0 - 10, s0, 0.55, 30, 0.57, 30)
    bh.add(s0 + 1, 0.53, 30, 0.57, 30)                          # NO leg 2 c worse at +1 s
    bh.add(s0 + 2, None, 0, 0.57, 30)                           # bid pulled: NO leg unquotable afterwards
    rows = list(scan_class([low, high], {("pmus", "pm5"): bl, ("pmus", "pm15"): bh}, s0, s0 + 30))
    o = [r for r in rows if r["type"] == "opportunity"][0]
    first_no, first_yes = o["leg2"]                             # legs: (pm15 no, pm5 yes)
    assert (first_no["first"], first_yes["first"]) == ("pmus:pm15", "pmus:pm5")
    assert first_yes["move_1s_c"] == 2.0 and first_yes["move_5s_c"] is None   # missing is null, never zero
    assert first_no["move_1s_c"] == 0.0 and first_no["move_5s_c"] == 3.0
    s = summarize([{"type": "run", "run": "r"}] + rows)
    f = s["one_leg_failure"]
    assert f["1s_best_order"]["missing"] == 0 and f["1s_worst_order"]["missing"] == 0
    assert f["1s_worst_order"]["loss_if_leg2_chased_dollars"] == round(0.02 * o["size"], 2)
    assert f["5s_best_order"]["missing"] == 0                                 # one ordering has a quote
    assert f["5s_best_order"]["loss_if_leg2_chased_dollars"] == round(0.03 * o["size"], 2)
    assert f["5s_worst_order"]["missing"] == 1                                # the other does not


def test_one_leg_failure_stalled_stream_is_null():
    low, high = ladder()
    s0 = T - 200
    bl, bh = Book(), Book()
    flat(bl, s0 - 10, s0 + 1, 0.38, 50, 0.40, 50)                # pm5 stream stalls after s0+1
    flat(bh, s0 - 10, s0 + 30, 0.55, 30, 0.57, 30)
    rows = list(scan_class([low, high], {("pmus", "pm5"): bl, ("pmus", "pm15"): bh}, s0, s0 + 30))
    first_no = [r for r in rows if r["type"] == "opportunity"][0]["leg2"][0]
    assert first_no["move_1s_c"] == 0.0 and first_no["move_5s_c"] is None   # 4 s old quote: unknown


def test_fees_exceed_edge_is_recorded():
    low, high = ladder()
    bl, bh = Book(), Book()
    flat(bl, T - 250, T, 0.48, 50, 0.50, 50)
    flat(bh, T - 250, T, 0.51, 30, 0.53, 30)                     # gross 1 c, taker fees ~4.7 c
    rows = list(scan_class([low, high], {("pmus", "pm5"): bl, ("pmus", "pm15"): bh}, T - 300, T))
    assert not [r for r in rows if r["type"] == "opportunity"]
    fx = [r for r in rows if r["type"] == "skip" and r["reason"] == "fees_exceed_edge"][0]
    assert fx["best_gross_c"] == 1.0 and fx["best_net_c"] < 0 and fx["best_net_maker_c"] == 1.0
    assert fx["ok_seconds"] == fx["seconds"] > 0


def test_parse_pmus_ws_on_recorded_fixture():
    fx = orjson.loads((Path(__file__).parent / "fixtures_formats.json").read_bytes())["pmus_ws"]
    parsed = [st.parse_pmus_ws(orjson.dumps(r["msg"]).decode()) for r in fx if "msg" in r]
    books = [p for p in parsed if p is not None]
    assert books and all(p[0].startswith("cpc") for p in books)
    for _, bid, bq, ask, aq in books:
        assert (bid is None or 0 < bid < 1) and (ask is None or 0 < ask < 1)
        assert bid is None or ask is None or bid < ask


def test_cross_venue_equal_contract_flags_kalshi_rest():
    a = C("pmus", "pm15", 10000, None, start=T - 900)
    b = C("kalshi", "KXBTC15M-X", 10000, None, start=T - 900)
    bp, bk = Book(), Book(fresh=False)
    flat(bp, T - 400, T, 0.40, 20, 0.42, 20)
    flat(bk, T - 400, T, 0.50, 10, 0.52, 10)                    # kalshi yes bid 0.50 > pm yes ask 0.42
    rows = list(scan_class([a, b], {("pmus", "pm15"): bp, ("kalshi", "KXBTC15M-X"): bk}, T - 300, T))
    o = [r for r in rows if r["type"] == "opportunity"]
    assert len(o) == 1 and o[0]["flags"]["kalshi_rest"] is True
    assert [(x["venue"], x["side"]) for x in o[0]["legs"]] == [("kalshi", "no"), ("pmus", "yes")]
    s = summarize([{"type": "run", "run": "r"}] + rows)
    assert s["opportunities"] == 1 and s["headline_opportunities"] == 0 and s["headline_dollars_upper"] == 0


def test_leg_once_dollars():
    def opp(d, *legs):
        return {"dollars": d, "legs": [{"venue": "pmus", "key": k, "side": s} for k, s in legs]}
    os_ = [opp(5.0, ("a", "yes"), ("b", "no")), opp(4.0, ("a", "yes"), ("c", "no")), opp(1.0, ("d", "yes"), ("c", "no"))]
    assert st.leg_once_dollars(os_) == 6.0


# ---------------------------------------------------------------------------------------------- tape + CLI

def test_load_books_from_tape(tmp_path: Path):
    w = SegmentWriter(tmp_path, "pmus_ws_15m")
    t0 = T - 100
    def md(bids, offers):
        return orjson.dumps({"marketData": {"marketSlug": "pm15", "bids": bids, "offers": offers}}).decode()
    lvl = lambda p, q: {"px": {"value": str(p)}, "qty": str(q)}  # noqa: E731
    w.append(md([lvl(0.40, 5), lvl(0.41, 7)], [lvl(0.45, 3)]), tw=int(t0 * 1e9))
    w.append(md([], [lvl(0.46, 2)]), tw=int((t0 + 1) * 1e9))
    w.close()
    k = SegmentWriter(tmp_path, "kalshi_books")
    body = {"orderbook_fp": {"yes_dollars": [["0.30", "4"]], "no_dollars": [["0.60", "9"], ["0.65", "2"]]}}
    k.append(orjson.dumps(body).decode(), tw=int(t0 * 1e9), meta={"key": "KX-1", "status": 200})
    k.append("{}", tw=int((t0 + 1) * 1e9), meta={"key": "KX-1", "status": 429})
    k.close()
    books = st.load_books(tmp_path, {"pmus": {"pm15"}, "kalshi": {"KX-1"}}, int(t0), int(t0 + 5))
    pmb = books[("pmus", "pm15")]
    assert pmb.leg(t0, "no") == (pytest.approx(0.59), 7.0, t0)      # best bid 0.41 -> NO costs 0.59
    assert pmb.leg(t0 + 1, "no") is None                           # pulled bid is not resurrected
    assert pmb.leg(t0 + 1, "yes")[0] == 0.46                        # type: ignore[index]
    kb = books[("kalshi", "KX-1")]
    assert kb.fresh is False and len(kb.t) == 1                     # non-200 poll ignored
    assert kb.leg(t0 + 2, "yes")[:2] == (0.35, 2.0)                 # type: ignore[index]
    assert kb.leg(t0 + 2, "no")[:2] == (0.70, 4.0)                  # type: ignore[index]
    assert st.load_books(tmp_path, {}, int(t0), int(t0 + 5), kalshi_stream="kalshi_ws") == {}  # no kalshi_ws tape here; see test_kalshi_ws.py


def test_discover_scan_summary_cli(tmp_path: Path, monkeypatch):
    lab = tmp_path / "lab"
    pages = {"pmus": [pm("pm5", "UP_DOWN", start="2026-11-26T00:40:00Z", ptb="100.00", horizon="5m"),
                      pm("pm15", "UP_DOWN", start="2026-11-26T00:30:00Z", ptb="101.00"),
                      pm("hit", "ONE_TOUCH", strike="150000", horizon="monthly")]}
    seen = []

    def fetch(url):
        seen.append(url)
        if "gateway" in url and "offset=0" in url:
            return {"markets": pages["pmus"]}
        if "/series/" in url:
            return {"series": {"fee_type": "quadratic", "fee_multiplier": 1}}
        if "series_ticker=KXBTC15M" in url:
            return {"markets": [ka("KXBTC15M-26NOV251945-45", "greater_or_equal", floor=101.0, prim=K15_PRIM,
                                   sec=K15_SEC, open_t="2026-11-26T00:30:00Z")], "cursor": ""}
        return {"markets": []}

    cache = st.discover(lab / "structural" / "contracts.json", fetch=fetch, kalshi_series=["KXBTC15M"],
                        pmus_pages=3)
    assert len(cache["pmus"]) == 3 and len(cache["kalshi"]) == 1 and cache["kalshi_series"]["KXBTC15M"]
    assert sum("gateway" in u for u in seen) == 1                   # short page stops pagination
    cen = st.census(cache)
    assert cen["excluded"] == {"one_touch_path_dependent": 1}
    assert cen["classes"] == {"cross: kalshi:KXBTC15M + pmus:updown-15m + pmus:updown-5m": 1}

    data = tmp_path / "data"
    w = SegmentWriter(data, "pmus_ws_5m")
    w15 = SegmentWriter(data, "pmus_ws_15m")
    for s in range(T - 200, T):
        for wr, slug, b, a in ((w, "pm5", 0.38, 0.40), (w15, "pm15", 0.55, 0.57)):
            raw = {"marketData": {"marketSlug": slug, "bids": [{"px": {"value": str(b)}, "qty": "30"}],
                                  "offers": [{"px": {"value": str(a)}, "qty": "30"}]}}
            wr.append(orjson.dumps(raw).decode(), tw=int((s + 0.1) * 1e9))
    w.close()
    w15.close()
    assert st.main(["scan", "--data", str(data), "--lab", str(lab), "--from", str(T - 300), "--to", ISO_T]) == 0
    rows = [orjson.loads(x) for x in (lab / "structural" / "ledger.jsonl").read_bytes().splitlines()]
    assert rows[0]["type"] == "run" and rows[0]["schema"] == st.SCHEMA and "code_sha" in rows[0]
    assert len({r["run"] for r in rows}) == 1
    opp = [r for r in rows if r["type"] == "opportunity"]
    assert len(opp) == 1 and {x["key"] for x in opp[0]["legs"]} == {"pm5", "pm15"}   # no kalshi book
    cls_row = [r for r in rows if r["type"] == "class"][0]
    assert cls_row["unbooked_by_family"] == {"kalshi:KXBTC15M": 1} and len(cls_row["members"]) == 2
    assert st.main(["summary", "--lab", str(lab)]) == 0
    s = summarize(rows)
    assert s["opportunities"] == 1 and s["headline_opportunities"] == 1
    assert s["persistence_s"]["max"] == 199          # first quote at T-199 (book T-199.9) .. T-1
