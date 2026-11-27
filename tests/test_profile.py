"""Wallet profile on synthetic order sets where every descriptor and status is computable by hand."""

import random

import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from doppel.lab.ordertape import SCHEMA
from doppel.lab.profile import (BIAS_LENGTH, BIAS_NONE, BIAS_UPPER, COPY, ESTIMATED, UNKNOWN, VERIFIED, WEIGHTS,
                                _ob, clusters, cohort, copyability, fingerprint, footprint, ledger, load_rows,
                                main, prepare, salt_kind, similarity)

MS = 1795629600_000  # ms, second-aligned
W, X, Y = "0xaa", "0xbb", "0xcc"
SIZES = (97.0, 98.0, 99.0, 100.0)


def frac(i):  # deciles 0.05..0.95 in a non-monotone order: median 0.5 over any 10 in a row
    return (i * 7 % 10 + 0.5) / 10


def row(i, maker=W, role="maker", size=None, limit=0.23, side="BUY", salt=None, step=1000, jitter=0,
        token=None, signer=None, verified=True):
    """Order i: created MS + i*step (+ jitter ms), filled in full 2 s later."""
    created = MS + i * step + jitter
    size = SIZES[i % 4] if size is None else size
    usdc, sh = round(size * limit * 1e6), round(size * 1e6)
    ma, ta = (usdc, sh) if side == "BUY" else (sh, usdc)
    bts = created // 1000 + 2
    return {"tx": f"0xt{maker}{i}", "block": 1000 + i, "block_ts": bts, "role": role,
            "order_id": f"{maker[2:]}{role}{i:06d}", "maker": maker, "signer": signer or maker,
            "token_id": token or f"tok{i % 2}", "side": side, "size": size, "limit": limit,
            "maker_amount": ma, "taker_amount": ta, "fill_amount": ma, "fee_amount": 0, "sig_type": 0,
            "created_ms": created, "age_s": round(bts - created / 1000, 3), "ts_flag": None,
            "verified": verified, "builder": "00" * 32,
            "salt": str(int(created * frac(i)) if salt is None else salt),
            "condition_id": "cc", "n_makers": 1}


def maker_rows(n=200, **kw):
    return [row(i, **kw) for i in range(n)]


def fp_of(rows, has_verified=True):
    return fingerprint(prepare(rows, has_verified))


# ---------------------------------------------------------------- salt
def test_salt_kinds():
    c = [MS + i * 1000 for i in range(40)]
    assert salt_kind([(int(x * frac(i)), x) for i, x in enumerate(c)]) == "uniform_below_now_ms"
    assert salt_kind([(int(x / 1000 * frac(i)), x) for i, x in enumerate(c)]) == "uniform_below_now_s"
    assert salt_kind([(x + 3, x) for x in c]) == "timestamp_ms"
    assert salt_kind([(x // 1000, x) for x in c]) == "timestamp_s"
    assert salt_kind([(x * 10**6 + 17, x) for x in c]) == "timestamp_ns"
    assert salt_kind([(5 + i, x) for i, x in enumerate(c)]) == "counter"
    rnd = random.Random(1)
    assert salt_kind([(rnd.getrandbits(200) | 1 << 199, x) for x in c]) == "other_le_256bit"
    assert salt_kind([(rnd.getrandbits(60) | 1 << 59, x) for x in c]) == "other_le_64bit"
    assert salt_kind([(1, x) for x in c[:29]]) == "insufficient"


def test_salt_stability_flags_a_change_of_generator():
    rows = maker_rows(200)
    s = fp_of(rows)["salt"]
    assert s["kind"] == "uniform_below_now_ms" and s["stable"] is True
    assert s["by_quarter"] == ["uniform_below_now_ms"] * 4 and "py-clob-client-v2" in s["reference"]
    # stamp ~1.796e12: decile 0.05 gives 11 digits, 0.15..0.55 give 12, 0.65..0.95 give 13
    assert s["digits"] == {"11": 20, "12": 100, "13": 80}
    for r in rows[100:]:  # second half: salt = the stamp itself
        r["salt"] = str(r["created_ms"])
    s = fp_of(rows)["salt"]
    assert s["by_quarter"] == ["uniform_below_now_ms"] * 2 + ["timestamp_ms"] * 2 and s["stable"] is False


# ---------------------------------------------------------------- descriptors
def test_descriptors_by_hand():
    rows = maker_rows(200) + [row(1000 + i, role="taker", size=10.0, limit=0.5) for i in range(8)]
    fp = fp_of(rows)
    assert (fp["n_orders"], fp["n_maker_orders"], fp["roles"]["taker_orders"]) == (208, 200, 8)
    z = fp["size"]
    assert z["rule"] == "fixed_sizes" and z["sizes_90"] == [97.0, 98.0, 99.0, 100.0]
    assert z["integer_share"] == 1.0 and z["tick_grid"] == "cent" and z["cent_share"] == 1.0
    assert fp["timestamp"]["granularity_ms"] == 1000 and fp["timestamp"]["flags"] == {"ok": 1.0}
    assert fp["signing"]["sig_type"] == {"0": 1.0} and fp["signing"]["maker_ne_signer_share"] == 0.0
    # shares: makers 50 * (97+98+99+100) = 19700, takers 8 * 10 = 80 (BUY fill is in USDC units)
    assert fp["roles"]["filled_shares"] == {"maker": 19700.0, "taker": 80.0}
    assert fp["roles"]["taker_share_of_shares"] == round(80 / 19780, 4)
    assert fp["roles"]["age_s"]["maker"] == {"p10": 2.0, "p50": 2.0, "p90": 2.0, "n": 200}
    assert fp["universe"]["tokens"] == 2


def test_timing_periodic_gaps():
    t = fp_of(maker_rows(200))["timing"]
    g = t["gap_ms_overall"]  # one order per second
    assert (g["p50"], g["n"], g["burstiness"], g["under_100ms_share"]) == (1000.0, 199, -1.0, 0.0)
    assert t["gap_ms_same_token"]["p50"] == 2000.0 and t["gap_ms_same_token"]["n"] == 198  # tokens alternate


def test_granularity_and_flags():
    fp = fp_of(maker_rows(200, jitter=7))
    assert fp["timestamp"]["granularity_ms"] == 1
    fp = fp_of(maker_rows(200, step=1010))
    assert fp["timestamp"]["granularity_ms"] == 10
    rows = maker_rows(200)
    for r in rows[:20]:
        r["created_ms"], r["ts_flag"], r["age_s"] = 0, "zero", None
    fp = fp_of(rows)
    assert fp["timestamp"]["flags"] == {"ok": 0.9, "zero": 0.1} and fp["salt"]["n"] == 180


def test_earliest_fill_block_overrides_a_stale_valid_clock_flag():
    order = row(0)
    early = order | {"block_ts": order["created_ms"] // 1000 - 1, "age_s": 2, "ts_flag": None}
    fp = fp_of([order, early])
    assert fp["n_maker_orders"] == 1 and fp["n_maker_plausible_stamp"] == 0
    assert fp["timestamp"]["flags"] == {"after_block": 1.0} and fp["timing"]["n"] == 0


def test_size_rules_and_tick():
    rnd = random.Random(2)
    px = [round(rnd.uniform(0.1, 0.9), 2) for _ in range(200)]
    fixed_usd = [row(i, size=round(50 / p, 2), limit=p) for i, p in enumerate(px)]
    z = fp_of(fixed_usd)["size"]
    assert z["rule"] == "fixed_notional" and z["sizes_90"] is None and z["cv_notional"] < 0.01
    var = [row(i, size=round(rnd.uniform(5, 500), 2), limit=p) for i, p in enumerate(px)]
    assert fp_of(var)["size"]["rule"] == "variable"
    sub = maker_rows(200, limit=0.235)
    assert fp_of(sub)["size"]["tick_grid"] == "subcent" and fp_of(sub)["size"]["cent_share"] == 0.0


# ---------------------------------------------------------------- keys and clusters
def test_key_is_order_independent_and_clusters():
    a = maker_rows(200)
    sh = a[:]
    random.Random(3).shuffle(sh)
    fa, fb = fp_of(a), fp_of(sh)
    assert fa["software_key"] == fb["software_key"] and fa["operator_key"] == fb["operator_key"]
    fx = fp_of(maker_rows(200, maker=X, size=50.0))  # same software, other size
    fy = fp_of([row(i, maker=Y, salt=MS + i * 1000) for i in range(200)])  # salt = stamp
    assert fx["software_key"] == fa["software_key"] and fx["operator_key"] != fa["operator_key"]
    assert fy["software_key"] != fa["software_key"]
    fps = {W: fa, X: fx, Y: fy}
    assert clusters(fps, "software_key") == {fa["software_key"]: [W, X]}
    assert clusters(fps, "operator_key") == {}
    assert similarity(fa["descriptor"], fa["descriptor"]) == 1.0
    assert similarity(fa["descriptor"], fx["descriptor"]) == 0.875  # 7 equal fields, size sets disjoint
    assert similarity(fa["descriptor"], fy["descriptor"]) == 0.875  # only the salt differs
    assert fp_of(maker_rows(29))["software_key"] is None and similarity(None, fa["descriptor"]) is None


def test_proxy_wallet_changes_the_software_key():
    a, p = fp_of(maker_rows(200)), fp_of(maker_rows(200, signer="0xSIGNER"))
    assert p["signing"]["maker_ne_signer_share"] == 1.0 and p["software_key"] != a["software_key"]


# ---------------------------------------------------------------- loaders
def test_parquet_and_jsonl_give_the_same_fingerprint(tmp_path):
    rows = maker_rows(200) + maker_rows(40, maker=X)
    d = tmp_path / "tape" / "day=2026-11-25"
    d.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(rows[:100], schema=SCHEMA), d / "part-000001.parquet")
    pq.write_table(pa.Table.from_pylist(rows[100:], schema=SCHEMA), d / "part-000002.parquet")
    (d / "part-000003.parquet.tmp").write_bytes(b"junk")
    j = tmp_path / "sample.jsonl"  # the older format: no verified, no ts_flag
    j.write_bytes(b"\n".join(orjson.dumps({k: v for k, v in r.items() if k not in ("verified", "ts_flag")})
                             for r in rows))
    pr, pv = load_rows(tmp_path / "tape", {W})
    jr, jv = load_rows(j, {W})
    assert (len(pr), pv, len(jr), jv) == (200, True, 200, False)
    wp, wj = prepare(pr, pv), prepare(jr, jv)
    assert fingerprint(wp) == fingerprint(wj)
    assert wp["input"]["unflagged_input"] is False and wj["input"]["unflagged_input"] is True
    assert len(load_rows(j)[0]) == 240
    start = rows[0]["block_ts"]
    for p in (tmp_path / "tape", j):
        bounded, _ = load_rows(p, {W}, (start + 1, start + 6))
        assert [r["block_ts"] for r in bounded] == list(range(start + 1, start + 6))


def test_only_verified_rows_count_when_the_column_exists():
    rows = maker_rows(200)
    for r in rows[:30]:
        r["verified"] = False
    for r in rows[30:50]:
        r["verified"] = None
    w = prepare(rows, True)
    assert w["input"] == {"rows": 200, "rows_used": 150, "rows_excluded_by_role": {"maker": 50},
                          "verified_false": 30, "verified_null": 20,
                          "unflagged_input": False}
    assert len(w["orders"]) == 150
    assert len(prepare(rows, False)["orders"]) == 200


def test_one_record_per_order_id():
    rows = maker_rows(200)
    again = dict(rows[0], tx="0xlater", block_ts=rows[0]["block_ts"] + 5, age_s=7.0)  # second fill
    fp = fp_of(rows + [again])
    assert fp["n_orders"] == 200 and fp["roles"]["age_s"]["maker"]["p90"] == 2.0
    assert fp["roles"]["filled_shares"]["maker"] == 19700.0 + 97.0


# ---------------------------------------------------------------- ledger
def q(p50, n):
    return {"p10": p50 / 2, "p50": p50, "p90": p50 * 4, "n": n}


def om_e39(**kw):
    """The E39 wallet-hour, as ordermatch.json reports a wallet."""
    d = {"wallet": W, "maker_orders": 340, "observable": 329, "matched": 323, "match_rate": 0.982,
         "placement_evidence_counts": {"unique_candidate": 340},
         "placebo_matched": 15, "placebo_rate": 0.046, "matched_all": 340,
         "sign_to_book_ms": q(149, 340),
         "outcomes": {"filled:chain": 141, "cancelled:exact_decrease": 123, "unknown:level_below": 76},
         "cancelled_alone": 60, "fill_before_placement": 2,
         "rest_ms_known": {"filled": q(1050, 141), "cancelled": q(2900, 123)},
         "fingerprint": {"sizes": list(SIZES), "active_token_hours": 40.0, "candidate_rest_ms_cancelled": q(245, 119941),
                         "total": {"own": 320, "other_known": 48, "candidates": 124429, "own_unmatched": 20,
                                   "control": 500, "expected_by_chance": 2942.0, "net_estimate": 121467.0,
                                   "identity_precision_upper": 0.87}}}
    return d | kw


def dossier(book_share=0.02):
    return {"mechanism": {"maker_share": 0.99, "mix": {"btc-5m": 900, "eth-5m": 100}},
            "coverage": {"book_share": book_share},
            "orders": {"maker_mid_dist_c": q(0.5, 300), "maker_fair_dist_c": q(1.2, 150)},
            "inventory": {"windows": 40, "flat_close_share": 0.9, "paired_share_of_gross": 0.95},
            "edge": {"all_resolved_crypto": {"fills": 500, "realized_usd": 123.4, "fees_usd": 1.0},
                     "maker": {"realized_usd": 100.0}, "taker": {"realized_usd": 5.0}},
            "policy": {"verdict": "insufficient"}}


SRC = {"orders": "tape/", "ordermatch": "om/ordermatch.json", "dossier": "dossiers/0xaa.json"}


def led(rows, om=None, dos=None, has_verified=True):
    w = prepare(rows, has_verified)
    return ledger(W, w, fingerprint(w), om, dos, SRC)


def statuses(doc):
    return {k: o["status"] for k, o in doc["obligations"].items()}


def check_invariants(doc):
    assert list(doc["obligations"]) == list(WEIGHTS)
    for k, o in doc["obligations"].items():
        assert o["bias"] in (BIAS_NONE, BIAS_UPPER, BIAS_LENGTH), k
        if o["status"] == VERIFIED:
            assert o["bias"] == BIAS_NONE, k  # a number from a selection-biased sample is never VERIFIED
            assert o["n"] and o["value"] is not None and o["source"], k
        elif o["status"] == ESTIMATED:
            assert o["false_positive_rate"] is not None and o["value"] is not None and o["source"], k
        else:
            assert o["status"] == UNKNOWN and o["resolve"], k


def test_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_ledger_e39_wallet_is_flicker():
    doc = led(maker_rows(200), om_e39(), dossier())
    check_invariants(doc)
    assert statuses(doc) == {
        "identity_software": VERIFIED, "universe": VERIFIED, "size_rule": VERIFIED,
        "price_rule": UNKNOWN,  # dossier has book state for 2 % of fills (E28)
        "placement_cadence": ESTIMATED,  # stamps validated, but fills are 0.3 % of quotes
        "cancel_replace": ESTIMATED,  # 76 of 340 outcomes unknown
        "fill_footprint": ESTIMATED, "inventory_rule": VERIFIED, "taker_behaviour": VERIFIED,
        "pnl_source": VERIFIED, "copyability": ESTIMATED}
    # 0.05 + 0.10 + 0.10 + 0 + 0.05 + 0.05 + 0.05 + 0.10 + 0.05 + 0.10 + 0.025
    assert doc["coverage"] == 0.675
    ob = doc["obligations"]
    cad = ob["placement_cadence"]
    assert cad["false_positive_rate"] == round(2942 / 124429, 4)
    assert cad["value"]["est_quotes_per_token_hour"] == round((340 + 121467) / 40, 1)
    assert cad["value"]["filled_order_gap_ms_same_token_p50"] == 2000.0 and cad["value"]["sign_to_book_ms_p50"] == 149
    assert ob["fill_footprint"]["value"]["fill_ratio"] == round(340 / (340 + 121467), 5)  # ~0.3 %
    assert ob["fill_footprint"]["false_positive_rate"] == round(2942 / 124429, 4)
    assert ob["cancel_replace"]["false_positive_rate"] == 0.046
    assert ob["cancel_replace"]["value"]["cancelled_or_replaced"] == 123
    assert ob["size_rule"]["value"] == {"rule": "fixed_sizes", "sizes": list(SIZES)}
    assert ob["taker_behaviour"]["value"]["negligible"] is True
    assert ob["universe"]["source"] == SRC["dossier"] and ob["size_rule"]["source"] == SRC["orders"]
    assert doc["copyability"] == "FLICKER" and len(ob["copyability"]["value"]["reasons"]) == 2


def test_ledger_orders_only():
    doc = led(maker_rows(200))
    check_invariants(doc)
    s = statuses(doc)
    assert {k for k, v in s.items() if v == VERIFIED} == {"identity_software", "size_rule", "taker_behaviour"}
    assert ESTIMATED not in s.values() and doc["coverage"] == 0.2 and doc["copyability"] == "UNKNOWN"
    assert doc["mechanism"] is None


def test_ledger_below_200_orders_verifies_nothing():
    doc = led(maker_rows(199), om_e39(), dossier(book_share=0.9))
    check_invariants(doc)
    s = statuses(doc)
    for k in ("identity_software", "universe", "size_rule", "taker_behaviour"):
        assert s[k] == UNKNOWN, k
    assert s["placement_cadence"] == ESTIMATED  # stamps validated, but only 199 orders
    assert doc["obligations"]["placement_cadence"]["false_positive_rate"] == 0.046
    assert s["price_rule"] == VERIFIED  # 300 maker orders with book state for 90 % of fills


def test_ledger_legacy_jsonl_is_marked_unflagged():
    rows = [{k: v for k, v in r.items() if k != "verified"} for r in maker_rows(200)]
    doc = led(rows, has_verified=False)
    assert doc["input"]["unflagged_input"] is True and statuses(doc)["size_rule"] == VERIFIED


def test_copyability_verdicts():
    fp = fp_of(maker_rows(200))
    assert copyability(fp, None)["verdict"] == "UNKNOWN"
    old = om_e39()
    del old["placement_evidence_counts"]
    assert copyability(fp, old)["verdict"] == "UNKNOWN"
    assert copyability(fp, om_e39())["verdict"] == "FLICKER"

    def fpd(net, life=5000.0):
        return {"sizes": [100.0], "candidate_rest_ms_cancelled": q(life, 50),
                "total": {"own": 300, "other_known": 0, "candidates": 400, "own_unmatched": 0, "control": 4,
                          "expected_by_chance": 40.0, "net_estimate": net, "identity_precision_upper": 1.0}}
    rest = {"filled": q(4000, 100), "cancelled": q(9000, 100)}
    r = copyability(fp, om_e39(rest_ms_known=rest, fingerprint=fpd(300.0)))  # ratio 300/600 = 0.5
    assert (r["verdict"], r["rate"]) == ("RESTING", 0.1)
    assert footprint(om_e39(fingerprint=fpd(300.0)))["fill_ratio"] == 0.5
    # one resting median under 2 s: not RESTING, and nothing says FLICKER
    r = copyability(fp, om_e39(rest_ms_known=rest | {"filled": q(1500, 100)}, fingerprint=fpd(300.0)))
    assert r["verdict"] == "UNKNOWN"
    # a median over fewer than min_rest_n orders is ignored
    r = copyability(fp, om_e39(rest_ms_known=rest | {"filled": q(100, COPY["min_rest_n"] - 1)}, fingerprint=fpd(300.0)))
    assert r["verdict"] == "RESTING"
    assert copyability(fp, om_e39(rest_ms_known=rest, fingerprint=fpd(300.0, life=499.0)))["verdict"] == "FLICKER"
    assert copyability(fp, om_e39(rest_ms_known=rest, fingerprint=fpd(5701.0)))["verdict"] == "FLICKER"  # 300/6001 < 0.05
    assert copyability(fp, om_e39(rest_ms_known=rest, fingerprint=None))["verdict"] == "UNKNOWN"  # no fill ratio
    # no excess quotes: their life is not consulted, fill ratio 1
    r = copyability(fp, om_e39(rest_ms_known=rest, fingerprint=fpd(0.0, life=10.0)))
    assert r["verdict"] == "RESTING"


def test_a_selection_biased_number_is_never_verified():
    """Cadence is measured between FILLED orders and cancel lives on partly filled orders: however
    clean the data, neither can be VERIFIED, and both say which bias they carry."""
    fpd = {"sizes": [100.0], "active_token_hours": 10.0, "candidate_rest_ms_cancelled": q(5000, 50),
           "total": {"own": 300, "other_known": 0, "candidates": 400, "own_unmatched": 0, "control": 4,
                     "expected_by_chance": 40.0, "net_estimate": 100.0, "identity_precision_upper": 1.0}}
    clean = om_e39(fingerprint=fpd, outcomes={"filled:chain": 200, "cancelled:exact_decrease": 140},
                   cancelled_alone=140)  # stamps validated, no unknown outcome, every cancel alone
    doc = led(maker_rows(200), clean)
    check_invariants(doc)
    cad, can = doc["obligations"]["placement_cadence"], doc["obligations"]["cancel_replace"]
    assert (cad["status"], cad["bias"], cad["n"]) == (ESTIMATED, BIAS_UPPER, 200)  # fill ratio 300/400 = 0.75
    assert cad["value"]["fill_ratio"] == 0.75 and cad["value"]["thinned_gap_ms_same_token"] == 0.75 * \
        cad["value"]["filled_order_gap_ms_same_token_p50"]
    assert (can["status"], can["bias"], can["value"]["clean"]) == (ESTIMATED, BIAS_LENGTH, True)
    assert can["value"]["unfilled_candidate_life_ms_p50"] == 5000
    cad = led(maker_rows(200), om_e39(fingerprint=None))["obligations"]["placement_cadence"]
    assert (cad["status"], cad["false_positive_rate"], cad["bias"]) == (ESTIMATED, 0.046, BIAS_UPPER)  # no footprint
    assert {k for k, o in doc["obligations"].items() if o["bias"] != BIAS_NONE} == {"placement_cadence", "cancel_replace"}
    # the constructor itself refuses the combination, so no future branch can produce it
    for bias in (BIAS_UPPER, BIAS_LENGTH):
        with pytest.raises(ValueError, match="cannot be VERIFIED"):
            _ob(VERIFIED, "src", n=500, value=1.0, bias=bias)
    assert _ob(VERIFIED, "src", n=500, value=1.0)["bias"] == BIAS_NONE


def test_taker_wallet():
    rows = [row(i, role="taker", size=10.0 + i % 50, limit=0.5) for i in range(180)] + maker_rows(20)
    doc = led(rows, om_e39(), None)
    check_invariants(doc)
    ob = doc["obligations"]
    assert doc["copyability"] == "TAKER" and ob["copyability"]["status"] == VERIFIED
    assert ob["taker_behaviour"]["status"] == UNKNOWN  # 180 taker orders < 200
    doc = led([row(i, role="taker", size=10.0 + i % 50, limit=0.5) for i in range(200)])
    assert doc["obligations"]["taker_behaviour"]["status"] == VERIFIED
    assert doc["obligations"]["taker_behaviour"]["value"]["age_s_p50"] == 2.0
    assert doc["obligations"]["size_rule"]["status"] == UNKNOWN and doc["fingerprint"]["size"]["rule"] is None


def test_ordermatch_without_observable_orders():
    om = om_e39(observable=0, matched=0, match_rate=None, placebo_rate=None, matched_all=0, outcomes={},
                cancelled_alone=0, rest_ms_known={"filled": None, "cancelled": None}, fingerprint=None)
    doc = led(maker_rows(200), om, None)
    check_invariants(doc)
    s = statuses(doc)
    assert s["placement_cadence"] == s["cancel_replace"] == s["fill_footprint"] == s["copyability"] == UNKNOWN


# ---------------------------------------------------------------- CLI
def test_cli_writes_ledgers_and_cohort(tmp_path, capsys):
    rows = maker_rows(200) + maker_rows(200, maker=X, size=50.0) + maker_rows(50, maker=Y)
    j = tmp_path / "s.jsonl"
    j.write_bytes(b"\n".join(orjson.dumps(r) for r in rows))
    omp = tmp_path / "ordermatch.json"
    omp.write_bytes(orjson.dumps({"wallets": {W: om_e39()}}))
    dd = tmp_path / "dossiers"
    dd.mkdir()
    (dd / f"{W}.json").write_bytes(orjson.dumps(dossier()))
    out = tmp_path / "out"
    assert main(["--orders", str(j), "--ordermatch", str(omp), "--dossiers", str(dd), "--out", str(out)]) == 0
    assert sorted(p.name for p in out.iterdir()) == [
        "0xaa.ledger.json", "0xaa.ledger.md", "0xbb.ledger.json", "0xbb.ledger.md", "cohort.json", "cohort.md"]
    doc = orjson.loads((out / "0xaa.ledger.json").read_bytes())
    assert doc["coverage"] == 0.675 and doc["sources"]["ordermatch"] == str(omp)
    c = orjson.loads((out / "cohort.json").read_bytes())
    assert [r["wallet"] for r in c["wallets"]] == [W, X]  # by coverage; Y has 50 orders < 200
    assert c["wallets"][0]["software_cluster_size"] == 2 and c["wallets"][0]["operator_cluster_size"] == 1
    assert c["similarity"] == [{"a": W, "b": X, "similarity": 0.875}]
    md = (out / "0xaa.ledger.md").read_text()
    assert "| fill_footprint | 0.1 | ESTIMATED |" in md and "FLICKER" in md and "not proof" in md
    assert "not proof" in capsys.readouterr().out
    out2 = tmp_path / "out2"  # an explicit wallet is profiled whatever its order count
    assert main(["--orders", str(j), "--wallet", Y.upper().replace("X", "x"), "--out", str(out2)]) == 0
    assert orjson.loads((out2 / "0xcc.ledger.json").read_bytes())["fingerprint"]["n_orders"] == 50


def test_cohort_of_nothing():
    doc, md = cohort([])
    assert doc["wallets"] == [] and "Cohort profile" in md
