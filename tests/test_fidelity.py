"""Clone fidelity on synthetic tape where every score is computable by hand.

The genome: TAKE UP 10 when mid_up_c < 45, TAKE DOWN 10 when mid_up_c > 55, once per window.
Window i (5 m, start T0 + 300 i) opens with the Up book at 0.49 / 0.51 (no rule fires). In an ACTIVE
window a new book arrives 0.5 s before second tau_i: cheap (ask p, i even) or rich (bid q, i odd).
The rule fires at tau_i, the order lands 250 ms later and takes 10 at the best price. So the clone's
only fill in window i is: tau_i + 0.25 s, 10 contracts, BUY Up at p (even) or SELL Up at q (odd).
"""

import json
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
from doppel.strategy.genome import (Condition, Costs, Genome, KillRule, Risk, Source, TakerRule,
                                    Universe, to_dict)
from doppel.tape.segment import SegmentWriter
from doppel.twin.intl import IntlWindow, iter_stream, windows_from_tokens

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


def taker_fill(p: dict, window: int, buy_up: bool, t_s: float, px_up: float, tx: str) -> tuple[list, tuple]:
    """The wallet TAKES 10 shares at Up price ``px_up`` in ``window`` (order-tape rows + the print):
    BUY Up from a maker selling Up, or BUY Down from a maker selling Down at 1 - px_up."""
    w = plan(window)
    token, price = (w["up"], px_up) if buy_up else (w["down"], round(1 - px_up, 2))
    base = {"tx": tx, "block_ts": int(t_s) + 2, "token_id": token, "verified": True, "n_makers": 1}
    rows = [base | {"role": "taker", "order_id": tx + "t", "maker": WALLET, "side": "BUY", "limit": 0.99,
                    "size": 10.0, "maker_amount": 9_900_000, "taker_amount": 10_000_000,
                    "fill_amount": int(round(price * 10e6))},
            base | {"role": "maker", "order_id": tx + "m", "maker": OTHER, "side": "SELL", "limit": price,
                    "size": 100.0, "maker_amount": 100_000_000, "taker_amount": int(round(price * 100e6)),
                    "fill_amount": 10_000_000}]
    return rows, trade(t_s, token, price, 10, "BUY", tx, f"m{w['start']}")


def build(tmp_path, wallet_of, n: int = N, lag_s: float = 0.0, parquet: bool = False, verified=True):
    """Tape + tokens + order tape. ``wallet_of(i, plan)`` -> list of taker_fill(...) results."""
    frames, rows, toks = [], [], []
    for i in range(n):
        p = plan(i)
        frames += books_of(p)
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


def run(tmp_path, wallet_of, n=N, **kw):
    root, lab, orders = build(tmp_path, wallet_of, n, kw.pop("lag_s", 0.0), kw.pop("parquet", False),
                              kw.pop("verified", True))
    return F.run(root, lab, genome(), WALLET, orders, T0, T0 + 300 * n, n_boot=300, **kw)


def val(doc, name):
    return doc["scores"][name]["value"]


# ---------------------------------------------------------------- end to end
def test_a_clone_that_mirrors_the_wallet_passes_with_scores_of_one(tmp_path):
    doc, rows = run(tmp_path, mirror)
    assert doc["windows"] == {"eligible": 40, "comparable": 32, "neither_traded": 8}
    assert doc["clone_shares"] == doc["wallet_shares"] == 320.0 and doc["scale_k"] == 1.0
    for name in ("activity.agreement", "fills.precision_count", "fills.recall_count", "fills.precision_shares",
                 "fills.recall_shares", "fills.precision_volume", "fills.recall_volume", "path.corr",
                 "path.end_corr", "pnl.corr", "pnl.slope", "pnl.slope_scaled", "pnl.level_ratio"):
        assert val(doc, name) == pytest.approx(1.0), name
    for name in ("activity.wallet_only", "activity.clone_only", "path.mad", "path.end_mad", "path.mad_scaled"):
        assert val(doc, name) == pytest.approx(0.0), name
    assert doc["scores"]["pnl.corr"]["ci95"][0] > 0.99
    # the clone's own P&L by hand: window 1 sells 10 Up at 0.64 and Up wins -> 10 * (0.64 - 1)
    r1 = next(r for r in rows if r["start"] == T0 + 300)
    assert (r1["clone_fills"], r1["matched"], r1["clone_pnl_usd"], r1["wallet_pnl_usd"]) == (1, 1, -3.6, -3.6)
    assert r1["clone_end_inventory"] == r1["wallet_end_inventory"] == -10.0
    # read against chance: the shuffled wallet does far worse on every gated score
    assert doc["null_shuffles"] == F.N_NULL
    assert doc["null"]["fills.recall_volume"]["max"] < 0.5 and doc["null"]["pnl.corr"]["max"] < 1.0
    assert doc["gates_measured"] == {"print_lag_s": 0.0, "usable_rows_share": 1.0, "located_share": 1.0,
                                     "wallet_fills": 32}
    assert doc["coverage"]["wallet_rows_block_ts"] == [T0 + 300 + 57 + 2, T0 + 300 * 39 + 168 + 2]   # tau 57 and 168
    assert doc["coverage"]["wallet_volume_share_scored"] == 1.0
    assert doc["adapter"]["outcome_src"] == {"payout": 40}
    v = doc["verdict"]
    assert v["decision"] == "PASS" and [c["result"] for c in v["checks"]] == ["pass"] * 4
    assert "PASS" in F.card(doc) and "PROVISIONAL" in F.card(doc)


def test_a_clone_on_the_opposite_side_fails(tmp_path):
    def opposite(i, p):   # same window, time and price, the other direction
        if not p["active"]:
            return []
        return [taker_fill(p, i, not p["up_cheap"], p["start"] + p["tau"] + 0.25, p["px"], f"0x{i:04d}")]
    doc, _ = run(tmp_path, opposite)
    assert val(doc, "activity.agreement") == 1.0
    assert val(doc, "fills.recall_count") == 0.0 and val(doc, "fills.precision_volume") == 0.0
    assert val(doc, "pnl.corr") == pytest.approx(-1.0) and val(doc, "path.corr") == pytest.approx(-1.0)
    assert val(doc, "path.end_mad") == pytest.approx(20.0)   # +10 vs -10 in every comparable window
    v = doc["verdict"]
    assert v["decision"] == "FAIL"
    assert v["binding"] == ["fills.recall_volume", "fills.precision_volume", "pnl.corr"]


def test_a_clone_trading_windows_the_wallet_did_not_fails(tmp_path):
    def elsewhere(i, p):  # the wallet only trades the windows the clone sits out
        if p["active"]:
            return []
        return [taker_fill(p, i, True, p["start"] + 100.0, 0.51, f"0x{i:04d}")]
    doc, _ = run(tmp_path, elsewhere)
    assert doc["windows"]["comparable"] == 40
    assert (val(doc, "activity.agreement"), val(doc, "activity.wallet_only"), val(doc, "activity.clone_only")) \
        == (0.0, 0.2, 0.8)
    assert val(doc, "fills.recall_count") == 0.0 and val(doc, "fills.precision_count") == 0.0
    assert doc["verdict"]["decision"] == "FAIL" and "activity.agreement" in doc["verdict"]["binding"]


def test_fewer_than_30_comparable_windows_is_inconclusive_even_for_a_perfect_clone(tmp_path):
    doc, _ = run(tmp_path, mirror, n=30)   # 24 active windows
    assert doc["windows"]["comparable"] == 24 and val(doc, "fills.recall_volume") == 1.0
    v = doc["verdict"]
    assert v["decision"] == "INCONCLUSIVE" and "24 comparable windows < 30" in v["reason"]


def test_parquet_order_tape_gives_the_same_answer(tmp_path):
    doc, _ = run(tmp_path, mirror, parquet=True)
    assert doc["verdict"]["decision"] == "PASS" and doc["wallet_shares"] == 320.0
    assert doc["coverage"]["wallet"]["rows"] == {"used": 32}


def test_an_order_tape_without_usable_wallet_rows_is_inconclusive_not_fail(tmp_path):
    doc, _ = run(tmp_path, mirror, verified=None)   # the store could not check any row
    assert doc["coverage"]["wallet"]["rows"] == {"unchecked": 32} and doc["wallet_shares"] == 0.0
    assert doc["gates_measured"]["wallet_fills"] == 0 and doc["gates_measured"]["usable_rows_share"] == 0.0
    v = doc["verdict"]
    assert v["decision"] == "INCONCLUSIVE" and "0 scored wallet fills < 1" in v["reason"]
    assert "usable wallet rows 0.0 < 0.9" in v["reason"]


def test_a_score_whose_shuffle_is_undefined_stays_open():
    sc = {t[0]: {"value": 0.9, "ci95": [0.8, 1.0]} for t in F.THRESHOLDS}
    null = {t[0]: {"max": 0.1} for t in F.THRESHOLDS if t[0] != "pnl.corr"}
    assert F.decide(40, sc, null, {})["binding"] == ["pnl.corr"]
    assert F.decide(40, sc, null | {"pnl.corr": {"max": 0.2}}, {})["decision"] == "PASS"
    assert F.decide(40, sc, null | {"pnl.corr": {"max": 0.95}}, {})["decision"] == "INCONCLUSIVE"


def test_skewed_clocks_and_unlocated_fills_are_data_gates(tmp_path):
    doc, _ = run(tmp_path, mirror, lag_s=5.0)   # every record received 5 s after its venue stamp
    assert doc["gates_measured"]["print_lag_s"] == 5.0
    assert doc["verdict"]["decision"] == "INCONCLUSIVE" and "clocks disagree" in doc["verdict"]["reason"]
    d = F.decide(40, {}, None, {"located_share": 0.5, "print_lag_s": 0.0})
    assert d["decision"] == "INCONCLUSIVE" and "timed by a print 0.5 < 0.9" in d["reason"]


def test_cli_writes_json_summary_and_windows(tmp_path, capsys):
    root, lab, orders = build(tmp_path, mirror)
    gp = tmp_path / "g.json"
    gp.write_bytes(orjson.dumps(to_dict(genome())))
    out = tmp_path / "out"
    rc = F.main(["--data", str(root), "--lab", str(lab), "--genome", str(gp), "--wallet", WALLET.upper(),
                 "--orders", str(orders), "--from", str(T0), "--to", str(T0 + 300 * N), "--out", str(out),
                 "--boot", "200"])
    assert rc == 0 and "VERDICT (PROVISIONAL thresholds): PASS" in capsys.readouterr().out
    doc = orjson.loads((out / "fidelity.json").read_bytes())
    assert doc["verdict"]["decision"] == "PASS" and doc["provisional"] is True
    assert [t["score"] for t in doc["thresholds"]] == [t[0] for t in F.THRESHOLDS]
    assert len((out / "windows.jsonl").read_bytes().splitlines()) == N
    assert (out / "fidelity.txt").read_text().startswith("fid-test")


# ---------------------------------------------------------------- scorer, by hand
C0 = Costs(taker_fee_coef=0.0, maker_fee_coef=0.0, fee_rounding="none")


def test_match_is_one_to_one_on_direction_price_and_time():
    clone = [Fill(100.0, 1, 0.40, 10, "maker"), Fill(100.5, 1, 0.40, 10, "maker"), Fill(200.0, -1, 0.60, 5, "maker")]
    wallet = [Fill(101.0, 1, 0.41, 30, "maker"),     # within 1 c and 2 s of both clone buys
              Fill(103.1, 1, 0.40, 7, "maker"),      # 2.6 s after the second buy: too late
              Fill(200.0, 1, 0.60, 5, "maker"),      # wrong direction
              Fill(201.0, -1, 0.62, 5, "maker")]     # 2 c away
    m, alloc, any_c, any_w = F.match(clone, wallet)
    assert (m, alloc, any_c, any_w) == (1, 10.0, 20.0, 30.0)   # the closer buy (100.5) takes the pair


def test_window_record_pnl_fees_and_inventory_path():
    costs = Costs(taker_fee_coef=0.1, maker_fee_coef=0.0, fee_rounding="none")
    clone = [Fill(1010.0, 1, 0.40, 10, "taker")]
    wallet = [Fill(1010.5, 1, 0.40, 100, "maker"), Fill(1200.0, -1, 0.70, 40, "taker")]
    r = F.window_record(clone, wallet, 1000, 1300, 1, costs, k=14.0)
    assert r["g_x"] == pytest.approx(10 * (1 - 0.40) - 0.1 * 0.4 * 0.6 * 10)          # 5.76
    assert r["g_y"] == pytest.approx(100 * 0.60 - 40 * 0.30 - 0.1 * 0.7 * 0.3 * 40)   # 47.16
    assert (r["e_x"], r["e_y"], r["m"], r["both"]) == (10.0, 60.0, 1.0, 1.0)
    # seconds 0..300: clone holds 10 from second 10; wallet 100 from second 11, 60 from second 200
    assert r["p_n"] == 301 and r["p_x"] == 10 * 291 and r["p_y"] == 100 * 189 + 60 * 101
    assert r["p_ad"] == 10 * 1 + 90 * 189 + 50 * 101
    assert r["p_ad_k"] == pytest.approx(140 * 1 + 40 * 189 + 80 * 101)


def test_scale_free_scores_compare_a_small_clone_with_a_large_wallet():
    """Clone trades 10, wallet 1,000, same windows, times, prices: raw share recall is 1%, the
    scale-free scores are 1."""
    wins, clone, wallet = {}, {}, {}
    for i in range(40):
        key, s = ("btc", "5m", 1000 + 300 * i), 1000 + 300 * i
        wins[key] = (s, s + 300, i % 2)
        d, p = (1, 0.4) if i % 3 else (-1, 0.6)
        clone[key] = [Fill(s + 50.0, d, p, 10, "maker")]
        wallet[key] = [Fill(s + 51.0, d, p, 1000, "maker")]
    doc = F.evaluate(clone, wallet, wins, C0, n_boot=200)
    sc = {k: v["value"] for k, v in doc["scores"].items()}
    assert doc["scale_k"] == 100.0 and sc["fills.recall_shares"] == 0.01 and sc["pnl.level_ratio"] == 0.01
    assert sc["fills.recall_volume"] == sc["fills.precision_volume"] == sc["fills.precision_shares"] == 1.0
    assert sc["pnl.level_ratio_scaled"] == pytest.approx(1.0) and sc["pnl.slope_scaled"] == pytest.approx(1.0)
    assert sc["path.end_mad"] == 990.0 and sc["path.end_mad_scaled"] == 0.0
    assert sc["path.mad_scaled"] == pytest.approx(1000 / 301 / 1000, abs=1e-6)   # 1 s later, in units of wallet volume
    assert doc["verdict"]["decision"] == "PASS"


def test_shuffle_moves_every_fill_set_to_another_window_keeping_time_in_window():
    keys = [("btc", "5m", 1000 + 300 * i) for i in range(6)] + [("eth", "5m", 1000)]
    wallet = {k: [Fill(k[2] + 7.0 + i, 1, 0.4, float(i + 1), "maker")] for i, k in enumerate(keys[:4])}
    wallet[keys[6]] = [Fill(1003.0, -1, 0.5, 9.0, "taker")]
    sh = F.shuffled(wallet, keys, np.random.default_rng(3))
    assert sh is not None and sh[keys[6]] == wallet[keys[6]]   # a group of one window cannot move
    moved = {k: fs for k, fs in sh.items() if k[0] == "btc"}
    assert sorted(f.qty for fs in moved.values() for f in fs) == [1.0, 2.0, 3.0, 4.0]
    for k, fs in moved.items():
        for f in fs:
            src = keys[int(f.qty) - 1]
            assert src != k and f.t - k[2] == pytest.approx(7.0 + f.qty - 1)
    assert F.shuffled(wallet, [keys[6]], np.random.default_rng(0)) is None


def test_undefined_scores_are_null_and_never_pass():
    wins = {("btc", "5m", 300 * i): (300 * i, 300 * i + 300, 1) for i in range(40)}
    wallet = {k: [Fill(k[2] + 5.0, 1, 0.4, 10, "maker")] for k in wins}
    doc = F.evaluate({}, wallet, wins, C0, n_boot=100)   # the clone never trades
    assert doc["scores"]["fills.precision_volume"] == {"value": None, "ci95": None}
    assert doc["scores"]["pnl.corr"]["value"] is None and doc["scale_k"] is None
    assert doc["verdict"]["decision"] == "FAIL" and "activity.agreement" in doc["verdict"]["binding"]
    d = F.decide(40, {"activity.agreement": {"value": 0.9, "ci95": [0.8, 1.0]}}, None, {})
    assert d["decision"] == "INCONCLUSIVE" and "pnl.corr" in d["binding"]


# ---------------------------------------------------------------- wallet fills
def test_taker_fill_is_priced_from_the_makers_of_its_tx_real_fixture():
    """Real tx 0x4f5bc2: the taker's limit is 0.87 but it bought 6.4935 shares at 0.77 (two makers
    bidding 0.23 on the other token); the limit would give 5.7471 shares at 0.87."""
    rows = [o | {"tx": FX["tx"], "block_ts": T0 + 12, "verified": True} for o in decode_match(FX["input"], FX["to"])]
    taker, maker = rows[0], rows[2]
    win_t = IntlWindow("btc", "5m", T0, taker["token_id"], maker["token_id"], 1.0)
    tm = TokenMap(Path("/nonexistent/tokens.jsonl"))
    fills, left = F.wallet_fills(rows, taker["maker"], [win_t], {(FX["tx"], win_t.key): (T0 + 10) * 1000 + 250},
                                 tm, T0, T0 + 300)
    fs = fills[win_t.key]
    assert [(f.d, round(f.px, 6), f.role, f.timed, f.t) for f in fs] == [(1, 0.77, "taker", True, T0 + 10.25)] * 2
    assert sum(f.qty for f in fs) == pytest.approx(6.493504, abs=1e-6) and left["rows"] == {"used": 1}
    # the cohort maker 0x0cb0: BUY Down 1.4935 at 0.23 = SELL Up at 0.77; no print -> block time - 2 s
    fills, _ = F.wallet_fills(rows, maker["maker"], [win_t], {}, tm, T0, T0 + 300)
    (f,) = fills[win_t.key]
    assert (f.d, round(f.px, 6), round(f.qty, 6), f.role, f.timed, f.t) == (-1, 0.77, 1.493504, "maker", False, T0 + 10.0)
    # an unverified row is not a fill
    bad = [r | {"verified": None} for r in rows]
    fills, left = F.wallet_fills(bad, maker["maker"], [win_t], {}, tm, T0, T0 + 300)
    assert fills == {} and left["rows"] == {"unchecked": 1}
    assert len(F.wallet_fills(bad, maker["maker"], [win_t], {}, tm, T0, T0 + 300, allow_unverified=True)[0]) == 1


def test_wallet_volume_outside_the_scored_windows_is_reported(tmp_path):
    def extra(i, p):   # mirror, plus one fill on a token of no window in the map
        out = mirror(i, p)
        if i == 3:
            rows, pr = taker_fill(p, i, True, p["start"] + 5.0, 0.5, "0xffff")
            out.append(([r | {"token_id": "ZZ"} for r in rows], pr))
        return out
    doc, _ = run(tmp_path, extra)
    cov = doc["coverage"]
    assert cov["wallet"]["rows"] == {"used": 32, "unknown_token_or_other_market": 1}
    assert cov["wallet_volume_share_scored"] == pytest.approx(320 / 330)
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
    (t,) = wt.trades
    assert (t.price, t.qty, t.aggressor, t.id) == (0.42, 4.0, "sell", "0xa")
    assert (t.tw, t.t_exch, t.t_venue) == ((S + 23) * NS, (S + 20) * NS, (S + 20) * NS)   # receive vs venue
    assert wt.gaps == [(S + 30) * NS]
    assert wt.stats["mirror_dup_prints"] == 1 and wt.stats["print_lag_s"] == [3.0, 3.0, 3.0]
    assert tx_ms == {("0xa", ("btc", "5m", S)): (S + 20) * 1000}


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
    from doppel.twin.intl import IntlRef
    ref = IntlRef("btc", T0, T0 + 300, ptb=100.0, settle=99.0)
    assert ref.outcome == 0 and IntlRef("btc", T0, T0 + 300, 100.0, 99.0, payout_up=1.0).outcome == 1
    assert IntlRef("btc", T0, T0 + 300, None, None).outcome is None
