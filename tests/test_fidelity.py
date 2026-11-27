"""Clone fidelity on synthetic tape where every score is computable by hand.

The genome: TAKE UP 10 when mid_up_c < 45, TAKE DOWN 10 when mid_up_c > 55, once per window.
Window i (5 m, start T0 + 300 i) opens with the Up book at 0.49 / 0.51 (no rule fires). In an ACTIVE
window a new book arrives 0.5 s before second tau_i: cheap (ask p, i even) or rich (bid q, i odd).
The rule fires at tau_i, the order lands 250 ms later and takes 10 at the best price. So the clone's
only fill in window i is: tau_i + 0.25 s, 10 contracts, BUY Up at p (even) or SELL Up at q (odd).
"""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import orjson
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from doppel.lab import fidelity as F
from doppel.lab.fidelity import Fill
from doppel.lab.ledger import TokenMap
from doppel.lab.ordertape import SCHEMA, decode_match
from doppel.lab.intltwin import (Footprint, IntlRef, IntlWindow, Resting, flow_without, iter_stream,
                                 strict_exclusion, windows_from_tokens)
from doppel.strategy.genome import (FEATURES, Condition, Costs, Genome, KillRule, MakerRule, Risk, Source,
                                    TakerRule, Universe, to_dict)
from doppel.tape.segment import SegmentWriter
from doppel.twin.engine import SimConfig, WindowSim
from doppel.twin.features import NAN, WindowRef
from doppel.twin.tape import Book, Trade, WindowTape

T0 = 1795629600  # hour-aligned, multiple of 300
NS = 1_000_000_000
N = 40
WALLET = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
FX = json.loads((Path(__file__).parent / "fixtures_ordertape" / "match_0x4f5bc2.json").read_text())


def genome(fee: float = 0.0) -> Genome:
    rule = dict(size=10.0, cooldown_s=5, max_entries_per_window=1)
    return Genome(
        name="fid-test", universe=Universe(venue="intl_sim", assets=("btc",), horizons=("5m",)),
        mode="taker", source=Source(kind="clone", wallets=(WALLET,)),
        taker=(TakerRule(side="up", when=(Condition("mid_up_c", "<", 45.0),), **rule),
               TakerRule(side="down", when=(Condition("mid_up_c", ">", 55.0),), **rule)),
        risk=Risk(max_loss_per_day=1e9), costs=Costs(taker_fee_coef=fee, fee_rounding="none"),
        kill=KillRule(min_windows=30))


def plan(i: int) -> dict:
    """What happens in window i. Inactive every 5th window."""
    px = (0.30, 0.36, 0.42)[i % 3]
    return {"start": T0 + 300 * i, "active": i % 5 != 0, "tau": 20 + 37 * (i % 7), "up_cheap": i % 2 == 0,
            "px": px if i % 2 == 0 else 1.0 - px,   # the Up price the clone trades at
            "outcome": 1 if (i // 2) % 2 == 0 else 0, "up": f"U{i}", "down": f"D{i}"}


def frame(t_s: float, m: dict) -> tuple[int, str]:
    return int(round(t_s * 1000)), orjson.dumps([m | {"timestamp": str(int(round(t_s * 1000)))}]).decode()


def book(t_s, asset, bid, ask, market):
    lv = lambda p: [{"price": f"{p:.2f}", "size": "100"}]  # noqa: E731
    return frame(t_s, {"event_type": "book", "asset_id": asset, "market": market, "bids": lv(bid), "asks": lv(ask)})


def trade(t_s, asset, price, size, side, tx, market):
    return frame(t_s, {"event_type": "last_trade_price", "asset_id": asset, "market": market,
                       "price": f"{price:.2f}", "size": str(size), "side": side, "transaction_hash": tx})


def books_of(p: dict) -> list:
    """Both tokens' books (the venue mirrors them) at the open and, if active, at tau - 0.5 s."""
    S, mk = p["start"], f"m{p['start']}"
    out = [book(S - 30, p["up"], 0.49, 0.51, mk), book(S - 30, p["down"], 0.49, 0.51, mk)]
    if p["active"]:
        bid, ask = (p["px"] - 0.02, p["px"]) if p["up_cheap"] else (p["px"], p["px"] + 0.02)
        t = S + p["tau"] - 0.5
        out += [book(t, p["up"], bid, ask, mk), book(t, p["down"], 1 - ask, 1 - bid, mk)]
    return out


def taker_fill(p: dict, window: int, buy_up: bool, t_s: float, px_up: float, tx: str, size: float = 10.0,
               wallet_makes: bool = False) -> tuple[list, tuple]:
    """The wallet TAKES ``size`` shares at Up price ``px_up`` in ``window`` (order-tape rows + the
    print): BUY Up from a maker selling Up, or BUY Down from a maker selling Down at 1 - px_up.
    ``wallet_makes``: the same trade with the wallet as the resting seller instead."""
    w = plan(window)
    tk, mk = (OTHER, WALLET) if wallet_makes else (WALLET, OTHER)
    token, price = (w["up"], px_up) if buy_up else (w["down"], round(1 - px_up, 2))
    base = {"tx": tx, "block_ts": int(t_s) + 2, "token_id": token, "verified": True, "n_makers": 1}
    rows = [base | {"role": "taker", "order_id": tx + "t", "maker": tk, "side": "BUY", "limit": 0.99,
                    "size": size, "maker_amount": int(size * 990_000), "taker_amount": int(size * 1e6),
                    "fill_amount": int(round(price * size * 1e6))},
            base | {"role": "maker", "order_id": tx + "m", "maker": mk, "side": "SELL", "limit": price,
                    "size": 100.0, "maker_amount": 100_000_000, "taker_amount": int(round(price * 100e6)),
                    "fill_amount": int(size * 1e6)}]
    return rows, trade(t_s, token, price, size, "BUY", tx, f"m{w['start']}")


def build(tmp_path, wallet_of, n: int = N, lag_s: float = 0.0, parquet: bool = False, verified=True):
    """Tape + tokens + order tape. ``wallet_of(i, plan)`` -> list of taker_fill(...) results."""
    frames, rows, toks = [], [], []
    for i in range(n):
        p = plan(i)
        frames += books_of(p)
        mk = f"m{p['start']}"   # two trades of other participants per window: the raw material of the null
        frames += [trade(p["start"] + 250, p["up"], 0.51, 7, "BUY", f"0xo{i}a", mk),
                   trade(p["start"] + 255, p["up"], 0.49, 7, "SELL", f"0xo{i}b", mk)]
        for r, pr in wallet_of(i, p):
            rows += r
            frames.append(pr)
        slug = f"btc-updown-5m-{p['start']}"
        toks += [{"token": p["up"], "slug": slug, "idx": 0, "payout": float(p["outcome"]), "status": "resolved"},
                 {"token": p["down"], "slug": slug, "idx": 1, "payout": 1.0 - p["outcome"], "status": "resolved"}]
    w = SegmentWriter(tmp_path / "tape", "clob_btc_5m")
    for ms, raw in sorted(frames, key=lambda f: f[0]):
        w.append(raw, tw=ms * 1_000_000 + int(lag_s * NS))
    w.close()
    lab = tmp_path / "lab"
    lab.mkdir()
    (lab / "tokens.jsonl").write_bytes(b"".join(orjson.dumps(t) + b"\n" for t in toks))
    rows = [r | {"verified": verified} for r in rows]
    orders = tmp_path / "orders.jsonl"
    if parquet:   # the cohort layout of doppel-ordertape
        orders = tmp_path / "orders"
        for j, day in enumerate(("2026-11-25", "2026-11-26")):
            (orders / f"day={day}").mkdir(parents=True)
            part = [r for r in rows if (int(r["tx"], 16) % 2) == j]   # a tx stays in one part
            pq.write_table(pa.Table.from_pylist(part, schema=SCHEMA), orders / f"day={day}" / "part-000001.parquet")
    else:
        orders.write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in rows))
    return tmp_path / "tape", lab, orders


def mirror(i, p):
    if not p["active"]:
        return []
    return [taker_fill(p, i, p["up_cheap"], p["start"] + p["tau"] + 0.25, p["px"], f"0x{i:04d}")]


def receipt(tmp_path, orders, n=N, **override):
    p = tmp_path / "coverage.json"
    p.write_bytes(orjson.dumps({"wallet": WALLET, "from": T0, "to": T0 + n * 300, "complete": True,
                                "order_tape_sha256": F.order_tape_sha256(orders)} | override))
    return p


def run(tmp_path, wallet_of, n=N, g=None, verified_coverage=True, **kw):
    root, lab, orders = build(tmp_path, wallet_of, n, kw.pop("lag_s", 0.0), kw.pop("parquet", False),
                              kw.pop("verified", True))
    coverage = receipt(tmp_path, orders, n) if verified_coverage else None
    return F.run(root, lab, g or genome(), WALLET, orders, T0, T0 + 300 * n, coverage_receipt=coverage, **kw)


def val(doc, name):
    return doc["scores"][name]["value"]


# ---------------------------------------------------------------- end to end
OK_GATES = {"verified_holdout": 1, "usable_rows_share": 1.0, "out_of_window_share": 0.0, "volume_share_scored": 1.0,
            "print_lag_s": 0.0, "side_conflict_share": 0.0}
GATED = [t[0] for t in F.THRESHOLDS]


def test_a_clone_that_mirrors_the_wallet_passes_with_scores_of_one(tmp_path):
    doc, rows = run(tmp_path, mirror)
    assert doc["coverage"]["verified_holdout"]["verified"]
    assert doc["coverage"]["order_tape_block_ts"] == [T0 + 300 + 57 + 2, T0 + 300 * 39 + 168 + 2]
    assert doc["windows"] == {"eligible": 40, "comparable": 32, "neither_traded": 8}
    assert doc["clone_shares"] == doc["wallet_shares"] == 320.0 and doc["scale_k"] == 1.0
    for name in ("activity.agreement", "fills.precision_count", "fills.recall_count", "fills.precision_shares",
                 "fills.recall_shares", "fills.precision_shares_scaled", "fills.recall_shares_scaled", "path.corr",
                 "path.end_corr", "pnl.corr", "pnl.slope", "pnl.slope_scaled", "pnl.level_ratio",
                 "pnl.level_ratio_scaled"):
        assert val(doc, name) == pytest.approx(1.0), name
    for name in ("activity.wallet_only", "activity.clone_only", "path.mad", "path.end_mad", "path.mad_scaled",
                 "path.end_mad_scaled"):
        assert val(doc, name) == pytest.approx(0.0), name
    # the clone's own P&L by hand: window 1 sells 10 Up at 0.64 and Up wins -> 10 * (0.64 - 1)
    r1 = next(r for r in rows if r["start"] == T0 + 300)
    assert (r1["clone_fills"], r1["matched"], r1["clone_pnl_usd"], r1["wallet_pnl_usd"]) == (1, 1, -3.6, -3.6)
    assert r1["clone_end_inventory"] == r1["wallet_end_inventory"] == -10.0
    # read against the same-window null: the wallet's fills moved onto other people's prints
    assert doc["null_draws"] == F.N_NULL and doc["null"]["fills.recall_count"] == {"mean": 0.0, "max": 0.0, "n": 20}
    assert doc["null"]["pnl.corr"]["max"] < 1.0
    assert doc["gates_measured"] == {
        "verified_holdout": 1, "print_lag_s": 0.0, "usable_rows_share": 1.0, "out_of_window_share": 0.0,
        "volume_share_scored": 1.0,
        "side_conflict_share": 0.0, "comparable_starts": 32, "wallet_fills": 32, "located_share": 1.0,
        "match_lag_s": 0.0}
    assert doc["footprint"]["taker_txs_dropped"] == 32 and doc["adapter"]["outcome_src"] == {"payout": 40}
    v = doc["verdict"]
    assert v["decision"] == "PASS" and [c["result"] for c in v["checks"]] == ["pass"] * len(F.THRESHOLDS)
    assert doc["diagnostic"] == [] and len(doc["code_sha256"]) == 64
    assert "PASS" in F.card(doc) and "PROVISIONAL" in F.card(doc)


def test_a_clone_on_the_opposite_side_fails(tmp_path):
    def opposite(i, p):   # same window, time and price, the other direction
        if not p["active"]:
            return []
        return [taker_fill(p, i, not p["up_cheap"], p["start"] + p["tau"] + 0.25, p["px"], f"0x{i:04d}")]
    doc, _ = run(tmp_path, opposite)
    assert val(doc, "activity.agreement") == 1.0
    assert val(doc, "fills.recall_count") == 0.0 and val(doc, "fills.precision_count") == 0.0
    assert val(doc, "pnl.corr") == pytest.approx(-1.0) and val(doc, "path.corr") == pytest.approx(-1.0)
    assert val(doc, "path.end_mad") == pytest.approx(20.0)   # +10 vs -10 in every comparable window
    v = doc["verdict"]
    assert v["decision"] == "FAIL" and v["binding"] == GATED[1:]   # everything but activity


def test_a_clone_trading_windows_the_wallet_did_not_fails(tmp_path):
    def elsewhere(i, p):  # the wallet only trades the windows the clone sits out
        if p["active"]:
            return []
        return [taker_fill(p, i, True, p["start"] + 100.0, 0.51, f"0x{i:04d}")]
    doc, _ = run(tmp_path, elsewhere)
    assert doc["windows"]["comparable"] == 40 and doc["coverage"]["verified_holdout"]["verified"]
    assert (val(doc, "activity.agreement"), val(doc, "activity.wallet_only"), val(doc, "activity.clone_only")) \
        == (0.0, 0.2, 0.8)
    assert val(doc, "fills.recall_count") == 0.0 and val(doc, "fills.precision_count") == 0.0
    assert doc["verdict"]["decision"] == "FAIL" and "activity.agreement" in doc["verdict"]["binding"]


def test_holdout_includes_clone_only_windows_outside_observed_wallet_fill_span(tmp_path):
    def quiet_edges(i, p):
        return [] if i in (1, N - 1) else mirror(i, p)

    doc, rows = run(tmp_path, quiet_edges)
    assert val(doc, "activity.clone_only") == 0.0625
    assert [(r["wallet_fills"], r["clone_fills"]) for r in (rows[1], rows[-1])] == [(0, 1), (0, 1)]


def test_missing_receipt_keeps_an_apparent_pass_inconclusive(tmp_path):
    doc, _ = run(tmp_path, mirror, verified_coverage=False)
    assert val(doc, "activity.agreement") == 1.0
    assert doc["gates_measured"]["verified_holdout"] == 0
    assert doc["verdict"]["decision"] == "INCONCLUSIVE"
    assert doc["coverage"]["verified_holdout"]["reason"] == "no coverage receipt"


def test_receipt_requires_the_exact_wallet_range_complete_assertion_and_tape(tmp_path):
    _, _, orders = build(tmp_path, mirror, n=2)
    for overrides, reason in [
        ({"wallet": OTHER}, "different wallet"),
        ({"to": T0 + 300}, "exact holdout"),
        ({"complete": False}, "does not establish completeness"),
        ({"order_tape_sha256": "a" * 64}, "hash mismatch"),
    ]:
        cov = F.holdout_coverage(receipt(tmp_path, orders, n=2, **overrides), orders, WALLET, T0, T0 + 600)
        assert cov["verified"] is False and reason in cov["reason"]


@pytest.mark.parametrize("fit_range", [None, [], {"from": T0 - 300}, {"from": T0, "to": T0 - 300},
                                       {"from": True, "to": T0}])
def test_malformed_draft_fit_range_is_rejected(tmp_path, fit_range):
    g = replace(genome(), evidence={"fit_range": fit_range})
    with pytest.raises(ValueError, match=r"evidence\.fit_range must contain increasing integer Unix seconds"):
        F.run(tmp_path, tmp_path, g, WALLET, tmp_path / "unread.jsonl", T0, T0 + 600)


def test_holdout_must_start_at_or_after_the_draft_fit_end(tmp_path):
    root, lab, orders = build(tmp_path, mirror, n=2)
    fit_range = {"from": T0 - 300, "to": T0}
    g = replace(genome(), evidence={"fit_range": fit_range})
    with pytest.raises(ValueError, match=r"before genome evidence\.fit_range ends"):
        F.run(root, lab, g, WALLET, orders, T0 - 1, T0 + 600)
    doc, _ = F.run(root, lab, g, WALLET, orders, T0, T0 + 600,
                   coverage_receipt=receipt(tmp_path, orders, n=2))
    assert doc["genome"]["fit_range"] == fit_range
    assert doc["windows"]["eligible"] == 2


def test_fewer_than_30_comparable_windows_is_inconclusive_even_for_a_perfect_clone(tmp_path):
    doc, _ = run(tmp_path, mirror, n=30)   # 24 active windows
    assert doc["windows"]["comparable"] == 24 and val(doc, "fills.recall_count") == 1.0
    v = doc["verdict"]
    assert v["decision"] == "INCONCLUSIVE" and "comparable_starts 24 outside [30, inf]" in v["reason"]


def test_parquet_order_tape_gives_the_same_answer(tmp_path):
    doc, _ = run(tmp_path, mirror, parquet=True)
    assert doc["verdict"]["decision"] == "PASS" and doc["wallet_shares"] == 320.0
    assert doc["coverage"]["wallet_rows"] == {"used": 32}


def test_an_order_tape_without_usable_wallet_rows_is_inconclusive_not_fail(tmp_path):
    doc, _ = run(tmp_path, mirror, verified=None)   # the store could not check any row
    assert doc["coverage"]["wallet_rows"] == {"unchecked": 32} and doc["wallet_shares"] == 0.0
    assert doc["gates_measured"]["wallet_fills"] == 0 and doc["gates_measured"]["usable_rows_share"] == 0.0
    v = doc["verdict"]
    assert v["decision"] == "INCONCLUSIVE" and "wallet_fills 0 outside" in v["reason"]
    assert "usable_rows_share 0.0 outside [0.9, 1.0]" in v["reason"]


def test_skewed_clocks_are_a_data_gate(tmp_path):
    doc, _ = run(tmp_path, mirror, lag_s=5.0)   # every record received 5 s after its venue stamp
    assert doc["gates_measured"]["print_lag_s"] == 5.0
    assert doc["verdict"]["decision"] == "INCONCLUSIVE" and "print_lag_s 5.0 outside [-0.5, 0.5]" in doc["verdict"]["reason"]


def test_non_default_knobs_make_the_verdict_diagnostic(tmp_path):   # S1
    doc, _ = run(tmp_path, mirror, n_boot=300, n_null=5)
    v = doc["verdict"]
    assert v["decision"] == "DIAGNOSTIC" and doc["diagnostic"] == ["--boot", "--null-shuffles"]
    assert v["would_be"] == "INCONCLUSIVE"   # 5 draws cannot be "all N_NULL draws"... of the default 20
    doc, _ = run(tmp_path / "b", mirror, cfg=SimConfig(latency_ms=100.0))
    assert doc["verdict"]["decision"] == "DIAGNOSTIC" and doc["verdict"]["would_be"] == "PASS"


def test_cli_writes_json_summary_and_windows(tmp_path, capsys):
    root, lab, orders = build(tmp_path, mirror)
    gp = tmp_path / "g.json"
    gp.write_bytes(orjson.dumps(to_dict(genome())))
    out = tmp_path / "out"
    cov = receipt(tmp_path, orders)
    rc = F.main(["--data", str(root), "--lab", str(lab), "--genome", str(gp), "--wallet", WALLET.upper(),
                 "--orders", str(orders), "--from", str(T0), "--to", str(T0 + 300 * N), "--out", str(out),
                 "--coverage-receipt", str(cov)])
    assert rc == 0 and "VERDICT (PROVISIONAL thresholds): PASS" in capsys.readouterr().out
    doc = orjson.loads((out / "fidelity.json").read_bytes())
    assert doc["verdict"]["decision"] == "PASS" and doc["provisional"] is True
    assert doc["code_sha256"] == F.code_sha256()
    assert [t["score"] for t in doc["thresholds"]] == GATED
    assert len((out / "windows.jsonl").read_bytes().splitlines()) == N
    assert (out / "fidelity.txt").read_text().startswith("fid-test")


# ---------------------------------------------------------------- M4: only in-window fills are scored
def test_pre_open_fills_become_the_opening_position_and_are_gated(tmp_path):
    def pre(size):
        def f(i, p):
            out = mirror(i, p)
            if p["active"]:   # the wallet also buys Up 30 s BEFORE the open and sells 5 s AFTER the close
                out += [taker_fill(p, i, True, p["start"] - 30.0, 0.51, f"0x{1000 + i:04d}", size),
                        taker_fill(p, i, False, p["start"] + 305.0, 0.51, f"0x{2000 + i:04d}", size)]
            return out
        return f
    doc, rows = run(tmp_path / "a", pre(1.0))
    assert doc["coverage"]["wallet_shares"] == {"in_window": 320.0, "pre_open": 32.0, "post_close": 32.0}
    assert val(doc, "fills.recall_count") == 1.0 and val(doc, "pnl.level_ratio") == pytest.approx(1.0)
    r1 = next(r for r in rows if r["start"] == T0 + 300)
    assert (r1["wallet_fills"], r1["wallet_opening"], r1["wallet_end_inventory"]) == (1, 1.0, -9.0)
    assert val(doc, "path.mad") == pytest.approx(1.0) and val(doc, "path.end_mad_scaled") == pytest.approx(0.1)
    assert doc["gates_measured"]["out_of_window_share"] == pytest.approx(64 / 384, abs=1e-6)
    assert doc["gates_measured"]["volume_share_scored"] == pytest.approx(320 / 384, abs=1e-6)
    assert doc["verdict"]["decision"] == "PASS"
    doc, _ = run(tmp_path / "b", pre(10.0))   # half of the wallet's volume is outside the window
    assert val(doc, "fills.recall_count") == 1.0
    assert doc["verdict"]["decision"] == "INCONCLUSIVE" and "out_of_window_share" in doc["verdict"]["reason"]


# ---------------------------------------------------------------- scorer, by hand
C0 = Costs(taker_fee_coef=0.0, maker_fee_coef=0.0, fee_rounding="none")


def test_match_is_one_to_one_on_direction_price_and_time():
    clone = [Fill(100.0, 1, 0.40, 10, "maker"), Fill(100.5, 1, 0.40, 10, "maker"), Fill(200.0, -1, 0.60, 5, "maker")]
    wallet = [Fill(101.0, 1, 0.41, 30, "maker"),     # within 1 c and 2 s of both clone buys
              Fill(103.1, 1, 0.40, 7, "maker"),      # 2.6 s after the second buy: too late
              Fill(200.0, 1, 0.60, 5, "maker"),      # wrong direction
              Fill(201.0, -1, 0.62, 5, "maker")]     # 2 c away
    assert F.match(clone, wallet) == [(1, 0)]        # the closer buy (100.5) takes the pair


def test_match_reassigns_the_nearest_pair_when_it_can_match_more_fills():
    clone = [Fill(t, 1, 0.4, 10, "maker") for t in (100, 101.9)]
    wallet = [Fill(t, 1, 0.4, 10, "maker") for t in (98.1, 100.1)]
    assert F.match(clone, wallet) == [(0, 0), (1, 1)]
    assert F.match([clone[0]] * 2, [wallet[0]] * 2) == [(0, 0), (1, 1)]


def test_window_record_pnl_fees_inventory_path_and_scaled_shares():
    costs = Costs(taker_fee_coef=0.1, maker_fee_coef=0.0, fee_rounding="none")
    clone = [Fill(1010.0, 1, 0.40, 10, "taker")]
    wallet = [Fill(1010.5, 1, 0.40, 100, "maker"), Fill(1200.0, -1, 0.70, 40, "taker")]
    r, lags = F.window_record(clone, wallet, 1000, 1300, 1, costs, k=14.0)
    assert lags == [-0.5]                                                             # clone - wallet
    assert r["g_x"] == pytest.approx(10 * (1 - 0.40) - 0.1 * 0.4 * 0.6 * 10)          # 5.76
    assert r["g_y"] == pytest.approx(100 * 0.60 - 40 * 0.30 - 0.1 * 0.7 * 0.3 * 40)   # 47.16
    assert (r["e_x"], r["e_y"], r["m"], r["both"]) == (10.0, 60.0, 1.0, 1.0)
    assert (r["alloc"], r["alloc_k"]) == (10.0, 100.0)    # min(10, 100); min(14 * 10, 100)
    # seconds 0..300: clone holds 10 from second 10; wallet 100 from second 11, 60 from second 200
    assert r["p_n"] == 301 and r["p_x"] == 10 * 291 and r["p_y"] == 100 * 189 + 60 * 101
    assert r["p_ad"] == 10 * 1 + 90 * 189 + 50 * 101
    assert r["p_ad_k"] == pytest.approx(140 * 1 + 40 * 189 + 80 * 101)
    # an opening position of 25 shifts the wallet's whole path
    r2, _ = F.window_record(clone, wallet, 1000, 1300, 1, costs, k=14.0, opening=25.0)
    assert r2["e_y"] == 85.0 and r2["p_y"] == r["p_y"] + 25 * 301 and r2["g_y"] == r["g_y"]


def market(n=40, others=6):
    """Hand-fed market: per window one wallet maker fill, and ``others`` prints of other people."""
    wins, wallet, prints = {}, {}, {}
    for i in range(n):
        s = 1000 + 300 * i
        key = ("btc", "5m", s)
        wins[key] = (s, s + 300, i % 2)
        d, p = (1, 0.4) if i % 3 else (-1, 0.6)
        wallet[key] = [Fill(s + 50.0, d, p, 1000.0, "maker")]
        prints[key] = [(s + 150.0 + 10 * j, 0.5, "sell" if j % 2 else "buy") for j in range(others)]
    return wins, wallet, prints


def test_scale_free_scores_compare_a_small_clone_with_a_large_wallet():
    """Clone trades 10, wallet 1,000, same windows, 1 s later, same prices: raw share recall is 1%,
    the scale-free scores are 1."""
    wins, wallet, prints = market()
    clone = {k: [Fill(f.t + 1.0, f.d, f.px, 10, "maker") for f in fs] for k, fs in wallet.items()}
    doc = F.evaluate(clone, wallet, wins, C0, OK_GATES, others=prints)
    sc = {k: v["value"] for k, v in doc["scores"].items()}
    assert doc["scale_k"] == 100.0 and sc["fills.recall_shares"] == 0.01 and sc["pnl.level_ratio"] == 0.01
    assert sc["fills.recall_count"] == sc["fills.precision_count"] == sc["fills.precision_shares"] == 1.0
    assert sc["fills.recall_shares_scaled"] == sc["fills.precision_shares_scaled"] == 1.0
    assert sc["pnl.level_ratio_scaled"] == pytest.approx(1.0) and sc["pnl.slope_scaled"] == pytest.approx(1.0)
    assert sc["path.end_mad"] == 990.0 and sc["path.end_mad_scaled"] == 0.0
    assert sc["path.mad_scaled"] == pytest.approx(1000 / 301 / 1000, abs=1e-6)   # 1 s later, in wallet volumes
    # ...but a clone that is systematically 1 s late is not centred on the wallet (S3)
    assert doc["gates_measured"]["match_lag_s"] == 1.0
    assert doc["verdict"]["decision"] == "INCONCLUSIVE" and "match_lag_s 1.0 outside" in doc["verdict"]["reason"]
    on_time = {k: [Fill(f.t + 0.25, f.d, f.px, 10, "maker") for f in fs] for k, fs in wallet.items()}
    assert F.evaluate(on_time, wallet, wins, C0, OK_GATES, others=prints)["verdict"]["decision"] == "PASS"


def test_size_gates_catch_a_clone_with_the_right_fills_and_the_wrong_position(tmp_path):
    """Same fills as the wallet plus an extra unmatched 3x position per window: every fill of the
    wallet is recalled, but the closing inventory and the P&L level are not the wallet's (M3)."""
    wins, wallet, prints = market()
    clone = {k: [Fill(f.t + 0.25, f.d, f.px, 10, "maker"), Fill(f.t + 100, f.d, 0.5, 30, "taker")]
             for k, fs in wallet.items() for f in fs}
    doc = F.evaluate(clone, wallet, wins, C0, OK_GATES, others=prints)
    by = {c["score"]: c["result"] for c in doc["verdict"]["checks"]}
    assert doc["scores"]["fills.recall_count"]["value"] == 1.0 and by["fills.recall_count"] == "pass"
    assert doc["scores"]["fills.precision_count"]["value"] == 0.5
    assert doc["scores"]["path.end_mad_scaled"]["value"] == 0.0     # k rescales the total: 40 * 25 = 1000
    assert by["fills.precision_shares_scaled"] == "fail" and doc["verdict"]["decision"] == "FAIL"


def test_a_clone_that_loses_what_the_wallet_earns_does_not_pass():
    """Audit case: 40 windows, zero fees, equal gross volume. The wallet buys 10 Up at 0.495 and
    earns +$2 in total; the clone buys the same 10 a cent higher at the same moments and LOSES $2.
    Every fill matches and the per-window P&L correlation is exactly 1, but the P&L level has the
    opposite sign: the level-ratio row (-1, allowed 0.5..2) must keep it from passing."""
    wins, wallet, clone, prints = {}, {}, {}, {}
    for i in range(40):
        s = 1000 + 300 * i
        key = ("btc", "5m", s)
        wins[key] = (s, s + 300, i % 2)
        wallet[key] = [Fill(s + 50.0, 1, 0.495, 10.0, "maker")]
        clone[key] = [Fill(s + 50.1, 1, 0.505, 10.0, "maker")]
        prints[key] = [(s + 150.0 + 10 * j, 0.5, "sell") for j in range(6)]
    doc = F.evaluate(clone, wallet, wins, C0, OK_GATES, others=prints)
    sc = {k: v["value"] for k, v in doc["scores"].items()}
    assert sum(F.pnl_usd(wallet[k], o, C0) for k, (_, _, o) in wins.items()) == pytest.approx(2.0)
    assert sum(F.pnl_usd(clone[k], o, C0) for k, (_, _, o) in wins.items()) == pytest.approx(-2.0)
    assert doc["clone_shares"] == doc["wallet_shares"] == 400.0 and sc["pnl.corr"] == pytest.approx(1.0)
    assert sc["fills.recall_count"] == sc["fills.precision_count"] == 1.0
    assert sc["pnl.level_ratio_scaled"] == pytest.approx(-1.0)
    by = {c["score"]: c["result"] for c in doc["verdict"]["checks"]}
    assert by["pnl.level_ratio_scaled"] != "pass" and all(r == "pass" for n, r in by.items()
                                                          if n != "pnl.level_ratio_scaled")
    assert doc["verdict"]["decision"] != "PASS" and "pnl.level_ratio_scaled" in doc["verdict"]["binding"]


def test_pseudo_wallet_keeps_count_direction_and_size_on_other_peoples_prints():
    wallet = [Fill(10.0, 1, 0.40, 5.0, "maker"), Fill(11.0, 1, 0.41, 6.0, "maker"), Fill(12.0, -1, 0.6, 7.0, "taker"),
              Fill(13.0, 1, 0.42, 8.0, "maker", timed=False)]
    prints = [(100.0, 0.50, "sell"), (101.0, 0.51, "sell"), (102.0, 0.52, "sell"), (103.0, 0.53, "buy")]
    ps = F.pseudo_wallet(wallet, prints, np.random.default_rng(0))
    # maker buys of Up sit on prints whose taker SOLD; the taker sell of Up also needs a SELL print
    assert sorted((f.d, f.qty, f.role) for f in ps) == [(-1, 7.0, "taker"), (1, 5.0, "maker"), (1, 6.0, "maker")]
    assert {f.t for f in ps} == {100.0, 101.0, 102.0} and all(f.px in (0.50, 0.51, 0.52) for f in ps)
    assert F.pseudo_wallet(wallet, prints[3:], np.random.default_rng(0)) == []   # only a BUY print: nothing fits


def test_without_enough_other_prints_there_is_no_null_and_no_pass():
    wins, wallet, _ = market()
    clone = {k: [Fill(f.t + 0.25, f.d, f.px, 10, "maker") for f in fs] for k, fs in wallet.items()}
    doc = F.evaluate(clone, wallet, wins, C0, OK_GATES, others={})   # a perfect clone, nobody else trading
    assert doc["null"] is None and doc["null_draws"] == 0
    v = doc["verdict"]
    assert v["decision"] == "INCONCLUSIVE" and v["binding"] == [t[0] for t in F.THRESHOLDS if t[3]]


def test_decide_uses_the_whole_ci_all_null_draws_and_every_gate():   # S1
    good = {t[0]: {"value": 0.9, "ci95": [0.8, 1.0]} for t in F.THRESHOLDS} | {
        "path.end_mad_scaled": {"value": 0.1, "ci95": [0.0, 0.2]}}
    null = {t[0]: {"max": 0.1, "n": F.N_NULL} for t in F.THRESHOLDS}
    gates = OK_GATES | {"comparable_starts": 40, "wallet_fills": 100, "located_share": 1.0}
    assert F.decide(good, null, gates)["decision"] == "PASS"
    # a point estimate above the threshold with the CI straddling it is open, not a pass
    d = F.decide(good | {"fills.recall_count": {"value": 0.6, "ci95": [0.45, 0.7]}}, null, gates)
    assert d["decision"] == "INCONCLUSIVE" and d["binding"] == ["fills.recall_count"]
    # a band: net per contract 3x the wallet's is a fail, 1.8x with a CI to 2.4 is open
    assert F.decide(good | {"pnl.level_ratio_scaled": {"value": 3.0, "ci95": [2.5, 3.5]}}, null, gates)["decision"] == "FAIL"
    assert F.decide(good | {"pnl.level_ratio_scaled": {"value": 1.8, "ci95": [1.2, 2.4]}}, null, gates)["binding"] \
        == ["pnl.level_ratio_scaled"]
    # the null: every draw must exist and be beaten
    few = null | {"fills.recall_count": {"max": 0.1, "n": 1}}
    assert F.decide(good, few, gates)["binding"] == ["fills.recall_count"]
    assert F.decide(good, null | {"fills.recall_count": {"max": 0.95, "n": F.N_NULL}}, gates)["binding"] \
        == ["fills.recall_count"]
    assert F.decide(good, None, gates)["decision"] == "INCONCLUSIVE"
    # gates: unmeasured or out of range is INCONCLUSIVE, even when a score fails
    assert "volume_share_scored not measured" in F.decide(good, null, {k: v for k, v in gates.items()
                                                                       if k != "volume_share_scored"})["reason"]
    bad = good | {"fills.recall_count": {"value": 0.0, "ci95": [0.0, 0.0]}}
    assert F.decide(bad, null, gates)["decision"] == "FAIL"
    for name, x in (("volume_share_scored", 0.7), ("side_conflict_share", 0.02), ("located_share", 0.5),
                    ("maker_footprint_partial", 3), ("out_of_window_share", 0.3)):
        d = F.decide(bad, null, gates | {name: x})
        assert d["decision"] == "INCONCLUSIVE" and name in d["reason"], name


def test_bootstrap_resamples_start_times_not_windows():
    """Two assets sharing every start move together: 60 windows are 30 clusters."""
    wins, clone, wallet = {}, {}, {}
    for i in range(30):
        for a in ("btc", "eth"):
            s = 1000 + 300 * i
            wins[(a, "5m", s)] = (s, s + 300, i % 2)
            wallet[(a, "5m", s)] = [Fill(s + 5.0, 1, 0.4, 10, "maker")]
            clone[(a, "5m", s)] = [Fill(s + 5.0, 1, 0.4, 10, "maker")] if a == "btc" else []
    doc = F.evaluate(clone, wallet, wins, C0, OK_GATES, n_boot=200)
    assert doc["windows"]["comparable"] == 60 and doc["gates_measured"]["comparable_starts"] == 30
    # every cluster holds one agreeing and one wallet-only window: no resample can move the share
    assert doc["scores"]["activity.agreement"] == {"value": 0.5, "ci95": [0.5, 0.5]}


def test_undefined_scores_are_null_and_never_pass():
    wins, wallet, prints = market()
    doc = F.evaluate({}, wallet, wins, C0, OK_GATES, others=prints)   # the clone never trades
    assert doc["scores"]["fills.precision_count"] == {"value": None, "ci95": None}
    assert doc["scores"]["pnl.corr"]["value"] is None and doc["scale_k"] is None
    assert doc["verdict"]["decision"] == "FAIL" and "activity.agreement" in doc["verdict"]["binding"]


# ---------------------------------------------------------------- M3 regression: the reviewer's adversaries
def adversary_market(seed=7, nw=40, n_others=120):
    """adv1's synthetic market: a 1 c random-walk mid per window, a two-sided maker wallet with 40
    fills per window at the touch, and other participants trading at the touch too."""
    rng = np.random.default_rng(seed)
    wins, mids, wallet, prints = {}, {}, {}, {}
    for i in range(nw):
        s = 1_000_000 + 300 * i
        key = ("btc", "5m", s)
        mid = np.clip(np.round(0.5 + rng.normal(0, 0.012, 300).cumsum(), 2), 0.03, 0.97)
        mids[key], wins[key] = mid, (s, s + 300, int(mid[-1] >= 0.5))
        ts = np.sort(rng.integers(1, 299, 40))
        wallet[key] = [Fill(s + int(t) + 0.4, d, float(mid[t] - 0.01 * d), float(rng.integers(5, 100)), "maker")
                       for t in ts for d in [int(rng.choice([-1, 1]))]]
        prints[key] = [(s + int(t) + 0.7, float(mid[t] - 0.01 * d), "sell" if d > 0 else "buy")
                       for t in rng.integers(1, 299, n_others) for d in [int(rng.choice([-1, 1]))]]
    return wins, mids, wallet, prints


def test_trivial_always_on_clones_do_not_pass_and_a_selective_clone_does():
    wins, mids, wallet, prints = adversary_market()
    # adv1 "spam": 0.01 on BOTH sides at the touch every second. It "recalls" every wallet fill...
    spam = {k: [Fill(s + t + 0.25, d, float(mids[k][t] - 0.01 * d), 0.01, "maker") for t in range(299) for d in (1, -1)]
            for k, (s, e, o) in wins.items()}
    doc = F.evaluate(spam, wallet, wins, C0, OK_GATES, others=prints)
    by = {c["score"]: c for c in doc["verdict"]["checks"]}
    assert doc["scores"]["fills.recall_count"]["value"] > 0.9
    # ...as it recalls anyone's (the same-window null), and nearly all of its own fills match nothing
    assert doc["null"]["fills.recall_count"]["max"] > 0.9   # so its recall says nothing about THIS wallet
    assert by["fills.precision_count"]["result"] == "fail" and doc["verdict"]["decision"] == "FAIL"
    # the same clone trading once per second on a random side: still not the wallet
    rng = np.random.default_rng(1)
    rnd = {k: [Fill(s + int(t) + 0.25, d, float(mids[k][t] - 0.01 * d), 1.0, "maker")
               for t in np.sort(rng.integers(1, 299, 150)) for d in [int(rng.choice([-1, 1]))]]
           for k, (s, e, o) in wins.items()}
    assert F.evaluate(rnd, wallet, wins, C0, OK_GATES, others=prints)["verdict"]["decision"] != "PASS"
    # a clone that trades when and where the wallet does, at a tenth of the size, passes
    sel = {k: [Fill(f.t + 0.1, f.d, f.px, f.qty / 10, "maker") for f in fs] for k, fs in wallet.items()}
    doc = F.evaluate(sel, wallet, wins, C0, OK_GATES, others=prints)
    assert doc["verdict"]["decision"] == "PASS" and doc["null"]["fills.recall_count"]["max"] < 0.5


# ---------------------------------------------------------------- M1 + M2: the wallet's footprint
class BookFair:
    """adv5's features: fair = a constant at one second, or the mid of the 500-lot levels of the
    book received by the decision second (nothing else is known)."""

    def __init__(self, S, const=None, only=None):
        self.S, self.const, self.only = S, const, only

    def at(self, d, book, inv):
        x = {k: NAN for k in FEATURES}
        x["inventory_up"], x["tau_s"], x["tte_s"] = inv, float(d - self.S), float(self.S + 300 - d)
        if self.const is not None:
            if d == self.only:
                x["fair_up"] = self.const
        elif book is not None and book.bids and book.asks:
            b, a = max(p for p, q in book.bids if q >= 500), min(p for p, q in book.asks if q >= 500)
            x["fair_up"], x["mid_up_c"] = (a + b) / 2, 50 * (a + b)
        return x


def maker_genome(size, sides="both", h=1.0):
    return Genome(name="adv", universe=Universe(venue="intl_sim"), mode="maker", source=Source(kind="clone"),
                  maker=MakerRule(quote_ref="fair", half_spread_c=h, size=size, sides=sides,
                                  requote_threshold_c=0.5, max_quote_age_s=3),
                  risk=Risk(max_inventory=1e6, max_notional_per_window=1e9, max_loss_per_day=1e9, max_open_windows=99),
                  costs=C0, kill=KillRule(min_windows=30))


def sim(g, wt, feats, outcome=1):
    ref = WindowRef("btc", wt.start, wt.end, ptb=1.0, settle=2.0 if outcome else 0.0)
    return WindowSim(g, wt, ref, SimConfig(), features=feats).run()


@pytest.mark.parametrize("hit", [50.0, 100.0])
def test_an_identical_maker_clone_recovers_the_wallets_queue_position_and_fills(tmp_path, hit):
    """adv5 A through the adapter. The wallet bids 100 at 0.50 (book at 10.05 s); a taker sells
    ``hit`` into it at 10.60 s. A clone with the same rule decides at second 10 and lands at 10.25,
    BEHIND the wallet. With the wallet's order in the book it gets nothing; with the wallet's
    footprint removed it stands where the wallet stood and gets the wallet's fill."""
    S, mk = T0, "m"
    w = SegmentWriter(tmp_path, "clob_btc_5m")

    def put(t_s, m):
        ms, raw = frame(t_s, m)
        w.append(raw, tw=ms * 1_000_000)

    lv = lambda xs: [{"price": f"{p:.2f}", "size": str(q)} for p, q in xs]  # noqa: E731
    put(S - 200, {"event_type": "book", "asset_id": "U", "market": mk, "bids": lv([(0.48, 500)]), "asks": lv([(0.52, 500)])})
    put(S + 10.05, {"event_type": "price_change", "market": mk, "price_changes": [
        {"asset_id": "U", "price": "0.50", "size": "100", "side": "BUY"}]})
    put(S + 10.60, {"event_type": "last_trade_price", "asset_id": "U", "market": mk, "price": "0.50",
                    "size": str(hit), "side": "SELL", "transaction_hash": "0xw"})
    put(S + 10.61, {"event_type": "price_change", "market": mk, "price_changes": [
        {"asset_id": "U", "price": "0.50", "size": str(100 - hit), "side": "BUY"}]})
    put(S + 20.0, {"event_type": "price_change", "market": mk, "price_changes": [   # the rest is cancelled
        {"asset_id": "U", "price": "0.50", "size": "0", "side": "BUY"}]})
    w.close()
    win = IntlWindow("btc", "5m", S, "U", "D", 1.0)
    g = maker_genome(100.0, "bid_only", 0.0)
    fills = lambda wt: [(f["t"], f["side"], f["px"], f["qty"], f["via"]) for f in  # noqa: E731
                        sim(g, wt, BookFair(S, 0.50, only=S + 10))["fills"]]
    (raw_wt,) = iter_stream(tmp_path, [win])
    assert [b.bids[0] for b in raw_wt.books[:2]] == [(0.48, 500.0), (0.50, 100.0)]
    assert fills(raw_wt) == []                                   # queued behind the wallet's 100
    order = Resting("BUY", 0.50, 100.0, placed_tw=int((S + 10.05) * NS), end_ms=int((S + 20.0) * 1000),
                    fills=((int((S + 10.60) * 1000), hit),))
    (wt,) = iter_stream(tmp_path, [win], fp=Footprint(resting={win.key: [order]}))
    assert all(b.bids[0] == (0.48, 500.0) for b in wt.books)     # the wallet's bid is gone for its whole life
    assert [t.id for t in wt.trades] == ["0xw"]                  # the third party's sell order stays
    assert fills(wt) == [(10.6, "buy", 0.5, hit, "trade")]       # exactly the wallet's fill
    # had the wallet been the TAKER of that tx, the print is its own aggression and is dropped
    (wt,) = iter_stream(tmp_path, [win], fp=Footprint(taker_txs={"0xw"}))
    assert wt.trades == [] and wt.stats["wallet_taker_prints_dropped"] == 1


def bk(t, bids, asks):
    return Book(tw=int(round(t * NS)), bids=tuple(bids), asks=tuple(asks))


@pytest.mark.parametrize("other_rate,strip_levels", [(0.0, False), (0.05, False), (0.05, True), (0.5, True)])
def test_adv5_no_signal_always_on_maker_clone_does_not_pass(other_rate, strip_levels):
    """The reviewer's adv5: the wallet has a PRIVATE signal (posts q inside the spread in 15% of
    seconds and is hit 0.3 s later). The clone has none: it quotes mid +- 1 c all the time, size 1,
    and is filled by the orders that came for the wallet. It passed at 0 and 0.05 other prints/s."""
    rng = np.random.default_rng(1)
    wins, clone, wallet, prints = {}, {}, {}, {}
    g = maker_genome(1.0)
    for i in range(40):
        S = 9_000_000 + 300 * i
        key = ("btc", "5m", S)
        mid = np.clip(np.round(0.5 + rng.normal(0, 0.008, 300).cumsum(), 2), 0.10, 0.90)
        o = int(mid[-1] >= 0.5)
        books, trades, wf, oth = [bk(S - 30, [(mid[0] - 0.02, 500.0)], [(mid[0] + 0.02, 500.0)])], [], [], []
        for t in range(1, 299):
            m = float(mid[t])
            ob, oa = (round(m - 0.02, 2), 500.0), (round(m + 0.02, 2), 500.0)
            books.append(bk(S + t + 0.01, [ob], [oa]))
            if rng.random() < 0.15:
                d, q = int(rng.choice([-1, 1])), float(rng.integers(5, 100))
                p, te = round(m - 0.01 * d, 2), S + t + 0.60
                if not strip_levels:   # without --ordermatch-dir the wallet's order stays in the book
                    books.append(bk(S + t + 0.30, [(p, q), ob] if d > 0 else [ob], [oa] if d > 0 else [(p, q), oa]))
                    books.append(bk(S + t + 0.66, [ob], [oa]))
                trades.append(Trade(int((te + 0.05) * NS), int(te * NS), p, q, "sell" if d > 0 else "buy", f"w{i}_{t}"))
                wf.append(Fill(te, d, p, q, "maker"))
            if rng.random() < other_rate:
                d, te = int(rng.choice([-1, 1])), S + t + 0.80
                pr = (ob[0] if d > 0 else oa[0], "sell" if d > 0 else "buy")
                trades.append(Trade(int((te + 0.05) * NS), int(te * NS), pr[0], float(rng.integers(5, 50)), pr[1], f"o{i}_{t}"))
                oth.append((te, *pr))
        books.sort(key=lambda b: b.tw)
        trades.sort(key=lambda x: x.tw)
        wt = WindowTape(slug="x", asset="btc", horizon="5m", start=S, end=S + 300, source="ws", books=books, trades=trades)
        wins[key], wallet[key], prints[key] = (S, S + 300, o), wf, oth
        clone[key] = F.clone_fills(sim(g, wt, BookFair(S), o) | {"start": S})
    doc = F.evaluate(clone, wallet, wins, C0, OK_GATES, others=prints)
    assert doc["scores"]["fills.recall_count"]["value"] > 0.5      # it does capture the wallet's flow
    assert doc["verdict"]["decision"] != "PASS", doc["verdict"]
    # and as a MAKER genome it cannot pass while any window's footprint is only partly removed
    d = F.decide(doc["scores"], doc["null"], doc["gates_measured"] | {"maker_footprint_partial": 40})
    assert d["decision"] == "INCONCLUSIVE" and "maker_footprint_partial 40 outside [0, 0]" in d["reason"]


def om_row(oid, token, side, price, size, placed_s, end_s, outcome, fills, wrong=False):
    return {"order_id": oid, "maker": WALLET, "token_id": token, "side": side, "price": price, "size": size,
            "placed_venue_ms": int(placed_s * 1000), "placed_tw_ns": int(placed_s * NS),
            "end_ms": None if end_s is None else int(end_s * 1000), "outcome": outcome, "reason": "x",
            "fills_at": [[tx, None if t is None else int(t * 1000), sh] for tx, t, sh in fills],
            "fill_before_placement": wrong}


def test_footprint_from_ordermatch_rows_and_what_stays_partial():
    wins = [IntlWindow("btc", "5m", T0 + 300 * i, f"U{i}", f"D{i}", 1.0) for i in range(5)]
    row = lambda i, oid, role="maker": {"tx": f"0x{oid}", "order_id": oid, "role": role, "maker": WALLET,  # noqa: E731
                                        "token_id": f"U{i}"}
    rows = [row(0, "a"), row(1, "b"), row(2, "c"), row(3, "d"), row(4, "t", "taker"),
            {"tx": "0xz", "order_id": "z", "role": "maker", "maker": OTHER, "token_id": "U4"}]
    om = [om_row("a", "U0", "BUY", 0.40, 100, T0 + 5, T0 + 9, "cancelled", [("0xa", T0 + 6, 30.0)]),
          om_row("b", "D1", "BUY", 0.30, 50, T0 + 305, None, "unknown", [("0xb", T0 + 307, 10.0)]),
          om_row("c", "U2", "SELL", 0.60, 50, T0 + 605, T0 + 606, "filled", [("0xc", T0 + 606, 50.0)], wrong=True)]
    fp, partial = F.footprint(rows, WALLET, wins, om)
    assert fp.taker_txs == {"0xt"}
    a, = fp.resting[wins[0].key]
    assert a == Resting("BUY", 0.40, 100.0, (T0 + 5) * NS, (T0 + 9) * 1000, (((T0 + 6) * 1000, 30.0),))
    assert a.remaining((T0 + 5) * 1000) == 100.0 and a.remaining((T0 + 6) * 1000) == 70.0
    # a bid of 50 at 0.30 on the DOWN token is an Up ask at 0.70; its end is unknown: subtracted to its last fill
    b, = fp.resting[wins[1].key]
    assert (b.side, b.price, b.end_ms) == ("SELL", 0.70, (T0 + 307) * 1000)
    assert partial == {wins[1].key: "order_end_unknown",      # located, life open-ended
                       wins[2].key: "order_not_located",      # a proven wrong match is not used
                       wins[3].key: "order_not_located"}      # not in orders.jsonl at all
    # without an ordermatch run nothing can be removed: every window with a maker fill is partial
    fp0, partial0 = F.footprint(rows, WALLET, wins, None)
    assert fp0.resting == {} and sorted(partial0) == [w.key for w in wins[:4]]


def test_a_maker_genome_is_inconclusive_until_the_wallets_orders_are_out_of_the_book(tmp_path):
    def makes(i, p):   # the mirror trades, with the wallet as the RESTING side
        if not p["active"]:
            return []
        return [taker_fill(p, i, p["up_cheap"], p["start"] + p["tau"] + 0.25, p["px"], f"0x{i:04d}", wallet_makes=True)]
    g = Genome(name="fid-maker", universe=Universe(venue="intl_sim"), mode="maker", source=Source(kind="clone"),
               maker=MakerRule(), risk=Risk(max_loss_per_day=1e9), costs=C0, kill=KillRule(min_windows=30))
    doc, rows = run(tmp_path / "a", makes, g=g)
    assert doc["footprint"]["windows_partial"] == {"order_not_located": 32}
    assert doc["gates_measured"]["maker_footprint_partial"] == 32 and rows[1]["footprint_partial"] == "order_not_located"
    v = doc["verdict"]
    assert v["decision"] == "INCONCLUSIVE" and "maker_footprint_partial 32 outside [0, 0]" in v["reason"]
    assert "no --ordermatch-dir" in v["reason"]
    # with every order located the gate clears (this clone never quotes: no BRTI, so no fair -> FAIL)
    om = []
    for i in range(N):
        p = plan(i)
        if p["active"]:
            t = p["start"] + p["tau"] + 0.25
            tok, price = (p["up"], p["px"]) if p["up_cheap"] else (p["down"], round(1 - p["px"], 2))
            om.append(om_row(f"0x{i:04d}m", tok, "SELL", price, 100.0, t - 1, t + 1, "cancelled", [(f"0x{i:04d}", t, 10.0)]))
    d = tmp_path / "om"
    d.mkdir()
    (d / "orders.jsonl").write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in om))
    doc, _ = run(tmp_path / "b", makes, g=g, ordermatch_dir=d)
    assert doc["footprint"]["resting_orders_subtracted"] == 32 and doc["footprint"]["windows_partial"] == {}
    assert doc["gates_measured"]["maker_footprint_partial"] == 0 and doc["verdict"]["decision"] == "FAIL"


# ---------------------------------------------------------------- wallet fills
def test_taker_fill_is_priced_from_the_makers_of_its_tx_real_fixture():
    """Real tx 0x4f5bc2: the taker's limit is 0.87 but it bought 6.4935 shares at 0.77 (two makers
    bidding 0.23 on the other token); the limit would give 5.7471 shares at 0.87."""
    rows = [o | {"tx": FX["tx"], "block_ts": T0 + 12, "verified": True} for o in decode_match(FX["input"], FX["to"])]
    taker, maker = rows[0], rows[2]
    win_t = IntlWindow("btc", "5m", T0, taker["token_id"], maker["token_id"], 1.0)
    tm = TokenMap(Path("/nonexistent/tokens.jsonl"))
    wal = F.wallet_fills(rows, taker["maker"], [win_t], {(FX["tx"], win_t.key): (T0 + 10) * 1000 + 250},
                         tm, T0, T0 + 300)
    fs = wal.fills[win_t.key]
    assert [(f.d, round(f.px, 6), f.role, f.timed, f.t) for f in fs] == [(1, 0.77, "taker", True, T0 + 10.25)] * 2
    assert sum(f.qty for f in fs) == pytest.approx(6.493504, abs=1e-6) and wal.rows == {"used": 1}
    assert wal.txs == {FX["tx"]}
    # the cohort maker 0x0cb0: BUY Down 1.4935 at 0.23 = SELL Up at 0.77; no print -> block time - 2 s
    (f,) = F.wallet_fills(rows, maker["maker"], [win_t], {}, tm, T0, T0 + 300).fills[win_t.key]
    assert (f.d, round(f.px, 6), round(f.qty, 6), f.role, f.timed, f.t) == (-1, 0.77, 1.493504, "maker", False, T0 + 10.0)
    # an unverified row is not a fill
    bad = [r | {"verified": None} for r in rows]
    wal = F.wallet_fills(bad, maker["maker"], [win_t], {}, tm, T0, T0 + 300)
    assert wal.fills == {} and wal.rows == {"unchecked": 1}
    assert len(F.wallet_fills(bad, maker["maker"], [win_t], {}, tm, T0, T0 + 300, allow_unverified=True).fills) == 1
    # S5: a taker row whose makers are not all in the tape has no price: unusable, never its limit
    wal = F.wallet_fills(rows[:2], taker["maker"], [win_t], {}, tm, T0, T0 + 300)
    assert wal.fills == {} and wal.rows == {"taker_unpriced": 1}


def test_wallet_volume_outside_the_scored_windows_is_reported(tmp_path):
    def extra(i, p):   # mirror, plus one fill on a token of no window in the map
        out = mirror(i, p)
        if i == 3:
            rows, pr = taker_fill(p, i, True, p["start"] + 5.0, 0.5, "0xffff")
            out.append(([r | {"token_id": "ZZ"} for r in rows], pr))
        return out
    doc, _ = run(tmp_path, extra)
    cov = doc["coverage"]
    assert cov["wallet_rows"] == {"used": 32, "unknown_token_or_other_market": 1}
    assert doc["gates_measured"]["volume_share_scored"] == pytest.approx(320 / 330, abs=1e-6)
    assert doc["verdict"]["decision"] == "PASS"


# ---------------------------------------------------------------- adapter
def test_adapter_books_prints_mirror_dedupe_gap_and_clocks(tmp_path):
    S, mk = T0, "m"
    w = SegmentWriter(tmp_path, "clob_btc_5m")

    def put(t_s, m, lag=0.2):
        ms, raw = frame(t_s, m)
        w.append(raw, tw=ms * 1_000_000 + int(lag * NS))

    put(S - 200, {"event_type": "book", "asset_id": "U", "market": mk,
                  "bids": [{"price": "0.40", "size": "5"}, {"price": "0.39", "size": "7"}, {"price": "0.38", "size": "9"}],
                  "asks": [{"price": "0.42", "size": "6"}]})
    put(S - 200, {"event_type": "book", "asset_id": "D", "market": mk, "bids": [], "asks": []})
    put(S + 10, {"event_type": "price_change", "market": mk, "price_changes": [
        {"asset_id": "U", "price": "0.41", "size": "3", "side": "BUY"},
        {"asset_id": "D", "price": "0.59", "size": "3", "side": "SELL"}]})
    put(S + 20, {"event_type": "last_trade_price", "asset_id": "D", "market": mk, "price": "0.58", "size": "4",
                 "side": "BUY", "transaction_hash": "0xa"}, lag=3.0)     # buy Down at 0.58 = sell Up at 0.42
    put(S + 20, {"event_type": "last_trade_price", "asset_id": "U", "market": mk, "price": "0.42", "size": "4",
                 "side": "SELL", "transaction_hash": "0xa"}, lag=3.0)    # the same execution, printed again
    put(S + 25, {"event_type": "last_trade_price", "asset_id": "U", "market": mk, "price": "0.42", "size": "2",
                 "side": "SELL", "transaction_hash": "0xb"})
    put(S + 25, {"event_type": "last_trade_price", "asset_id": "D", "market": mk, "price": "0.58", "size": "2",
                 "side": "SELL", "transaction_hash": "0xb"})             # S7: the Down print says the taker BOUGHT Up
    w.append("", tw=(S + 30) * NS, meta={"ctl": "disconnect"})
    put(S + 31, {"event_type": "price_change", "market": mk, "price_changes": [
        {"asset_id": "U", "price": "0.41", "size": "8", "side": "BUY"}]})   # state unknown: dropped
    put(S + 40, {"event_type": "book", "asset_id": "U", "market": mk,
                 "bids": [{"price": "0.30", "size": "1"}], "asks": [{"price": "0.70", "size": "1"}]})
    w.close()
    tx_ms: dict = {}
    (wt,) = iter_stream(tmp_path, [IntlWindow("btc", "5m", S, "U", "D", 1.0)], depth=2, watch={"0xa"}, tx_ms=tx_ms)
    assert (wt.source, wt.slug, wt.start, wt.end) == ("ws", f"btc-updown-5m-{S}", S, S + 300)
    # as-of snapshot when capture starts (S - 60), the change (received 0.2 s late), the re-sync book
    assert [b.tw for b in wt.books] == [(S - 60) * NS, int((S + 10.2) * NS), int((S + 40.2) * NS)]
    assert wt.books[0].bids == ((0.40, 5.0), (0.39, 7.0)) and wt.books[0].asks == ((0.42, 6.0),)   # depth 2
    assert wt.books[1].bids == ((0.41, 3.0), (0.40, 5.0)) and wt.books[2].bids == ((0.30, 1.0),)
    t = wt.trades[0]
    assert (t.price, t.qty, t.aggressor, t.id) == (0.42, 4.0, "sell", "0xa")
    assert (t.tw, t.t_exch, t.t_venue) == ((S + 23) * NS, (S + 20) * NS, (S + 20) * NS)   # receive vs venue
    assert wt.gaps == [(S + 30) * NS]
    assert wt.stats["mirror_dup_prints"] == 1 and wt.stats["print_lag_s"] == [0.2, 0.2, 2.44]
    # S7: tx 0xb is printed with opposite takers on the two tokens: both kept, the conflict counted
    assert [(x.id, x.aggressor) for x in wt.trades[1:]] == [("0xb", "sell"), ("0xb", "buy")]
    assert wt.stats["side_conflicts"] == 1
    assert tx_ms == {("0xa", ("btc", "5m", S)): (S + 20) * 1000}
    # S2: a gap inside [S, E) excludes the window in a fidelity run, whatever the genome's mode
    assert strict_exclusion(wt) == "feed_gap"


def test_a_book_that_arrives_after_the_open_excludes_the_window(tmp_path):   # S2 (reviewer's case 3)
    S = T0
    w = SegmentWriter(tmp_path, "clob_btc_5m")
    lv = lambda p: [{"price": p, "size": "5"}]  # noqa: E731
    for t_s, m in ((S - 200, {"event_type": "book", "asset_id": "U", "market": "m", "bids": lv("0.40"), "asks": lv("0.42")}),
                   (S + 200, {"event_type": "book", "asset_id": "U", "market": "m", "bids": lv("0.30"), "asks": lv("0.70")})):
        ms, raw = frame(t_s, m)
        w.append(raw, tw=ms * 1_000_000)
        if t_s < S:
            w.append("", tw=(S - 100) * NS, meta={"ctl": "disconnect"})
    w.close()
    (wt,) = iter_stream(tmp_path, [IntlWindow("btc", "5m", S, "U", "D", 1.0)])
    assert wt.gaps == [] and wt.stats["first_book_s"] == 200.0 and strict_exclusion(wt) == "late_book"


def test_intl_flow_features_leave_out_the_target_wallet(tmp_path):   # S3
    S = T0

    def rtds(tx, wallet, side, size, t):
        return (int(t * NS), orjson.dumps({"topic": "activity", "type": "trades", "payload": {
            "transactionHash": tx, "proxyWallet": wallet, "eventSlug": f"btc-updown-5m-{S}", "asset": "U",
            "side": side, "outcomeIndex": 0, "price": 0.5, "size": size, "fee": 0.1, "timestamp": int(t)}}).decode())
    w = SegmentWriter(tmp_path, "rtds_a")
    for tw, raw in (rtds("0x1", WALLET, "BUY", 100, S + 10), rtds("0x2", OTHER, "SELL", 20, S + 11)):
        w.append(raw, tw=tw)
    w.close()
    both = flow_without(tmp_path, "btc", "5m", S, {}, None)[S + 12]["intl_flow_up_usd_5s"]
    alone = flow_without(tmp_path, "btc", "5m", S, {}, WALLET)[S + 12]["intl_flow_up_usd_5s"]
    assert (both, alone) == (40.0, -10.0)   # 100 * 0.5 - 20 * 0.5 with the wallet; only the other without


def test_windows_come_from_the_token_map_and_unresolved_windows_fall_back(tmp_path):
    p = tmp_path / "tokens.jsonl"
    slug = f"btc-updown-5m-{T0}"
    p.write_bytes(b"".join(orjson.dumps(t) + b"\n" for t in (
        {"token": "U", "slug": slug, "idx": 0, "payout": None, "status": "open"},
        {"token": "D", "slug": slug, "idx": 1, "payout": None, "status": "open"},
        {"token": "X", "slug": f"btc-updown-5m-{T0 + 300}", "idx": 0, "payout": 1.0, "status": "resolved"})))
    wins, missing = windows_from_tokens(TokenMap(p), ("btc",), ("5m",), T0, T0 + 900)
    assert wins == [IntlWindow("btc", "5m", T0, "U", "D", None)]
    assert missing == [("btc", "5m", T0 + 300), ("btc", "5m", T0 + 600)]
    ref = IntlRef("btc", T0, T0 + 300, ptb=100.0, settle=99.0)
    assert ref.outcome == 0 and IntlRef("btc", T0, T0 + 300, 100.0, 99.0, payout_up=1.0).outcome == 1
    assert IntlRef("btc", T0, T0 + 300, None, None).outcome is None


def test_the_pm_us_twin_code_hash_does_not_cover_the_intl_adapter():   # S8
    from doppel.twin import cli
    twin_dir = Path(cli.__file__).parent
    assert not (twin_dir / "intl.py").exists()
    assert sorted(p.name for p in twin_dir.glob("*.py")) == ["__init__.py", "cli.py", "engine.py", "evaluate.py", "features.py",
                                                             "flowfeat.py", "tape.py", "verdict.py"]
