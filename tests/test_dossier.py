import math
from pathlib import Path

import numpy as np
import orjson
import pytest

import doppel.lab.dossier as dz
from doppel.lab.dossier import (BLOCK_SLACK_S, NS, BookIndex, Brti, build_index, enrich,
                                fair_up_at, orders)
from doppel.lab.fair import fair_up_raw, to_array
from doppel.lab.fillstore import compact, iter_wallet_fills
from doppel.lab.ledger import TokenMap, wallet_ledger
from doppel.tape.segment import SegmentWriter

FIX = orjson.loads((Path(__file__).parent / "fixtures_formats.json").read_bytes())
A_UP = "37219683974299077682722837222089109669361807475653020816058588933719117325533"
A_DN = "70862148482917693841801498552888978138219154334126377414093133387561810181396"
MKT = "0xc5da016c3726c0650288d0ec5660b12ec63657e630b878ab5721d701e139b40a"
T0 = 1795046400  # UTC midnight
START = T0 + 3600
SLUG = f"btc-updown-5m-{START}"


def _tmap(tmp_path, recs):
    tm = TokenMap(tmp_path / "t.jsonl")
    tm.m = {r["token"]: r for r in recs}
    return tm


def _tok(token, slug, idx, payout=None, cond="0xm"):
    return {"token": token, "slug": slug, "idx": idx, "condition": cond, "payout": payout,
            "status": "resolved" if payout is not None else "open"}


def _row(i, tok, side, price, sh=10.0, t=START + 100, role="taker", oid=None, tx=None, cp=None,
         fee=0.0):
    return {"id": f"r{i}", "token_id": tok, "side": side, "amount_shares": sh,
            "amount_usdc": round(sh * price, 6), "fee": fee, "block_timestamp": t, "block_number": i,
            "order_type": role, "price": price, "order_id": oid, "counterparty_address": cp,
            "transaction_hash": tx}


class FixedFair:
    """Stand-in for Brti: constant fair_up, records the decision seconds it was asked for."""

    def __init__(self, fu):
        self.fu, self.asked, self.lag = fu, [], 0

    def fair(self, start, end, d):
        self.asked.append(d)
        return self.fu


# ---------------------------------------------------------------- parsing (real fixture shapes)
def test_fixture_shapes_parse(tmp_path):
    w = SegmentWriter(tmp_path, "clob_btc_5m")
    w.append("", tw=FIX["clob_book"][0]["tw"] - 10, meta={"ctl": "connect"})
    w.append("PONG", tw=FIX["clob_book"][0]["tw"] - 5)
    for k in ("clob_price_change", "clob_book", "clob_last_trade_price"):
        for i, r in enumerate(FIX[k]):
            msg = [r["msg"]] if (k == "clob_book" and i == 0) else r["msg"]  # list-wrapped too
            w.append(orjson.dumps(msg).decode(), tw=r["tw"])
    w.close()
    idx = build_index(tmp_path)
    assert idx.counts["book"] == 2 and idx.counts["price_change"] == 4 and idx.counts["print"] == 2
    tw_pc, tw_book = FIX["clob_price_change"][1]["tw"], FIX["clob_book"][0]["tw"]
    # price_change carries best_bid/best_ask directly
    assert idx.quote(A_UP, tw_pc + 1) == (0.64, 0.65)
    assert idx.quote(A_DN, tw_pc + 1) == (0.35, 0.36)
    # book: best bid = max bid level, best ask = min ask level (fixture lists worst first)
    assert idx.quote(A_DN, tw_book + 1) == (0.03, 0.97)
    tx = FIX["clob_last_trade_price"][0]["msg"]["transaction_hash"]
    p = idx.print_for(tx, A_UP)
    assert p is not None and p[0] == 1795633204862 and p[1] == 0.65 and p[3] == "BUY"
    assert idx.sibling(A_UP) == A_DN and idx.market_of[A_DN] == MKT
    clk = idx.clock_report()["clob_btc_5m"]
    assert clk["n"] >= 1 and clk["tw_minus_server_ms_p50"] is not None


def test_market_and_tx_filters(tmp_path):
    w = SegmentWriter(tmp_path, "clob_btc_5m")
    for k in ("clob_price_change", "clob_last_trade_price"):
        for r in FIX[k]:
            w.append(orjson.dumps(r["msg"]).decode(), tw=r["tw"])
    w.close()
    assert not build_index(tmp_path, markets={"0xother"}).books
    tx = FIX["clob_last_trade_price"][1]["msg"]["transaction_hash"]
    idx = build_index(tmp_path, markets={MKT}, txs={tx})
    assert set(idx.prints) == {tx} and set(idx.books) == {A_UP, A_DN}


def test_goldsky_fixture_extra_columns(tmp_path):
    w = SegmentWriter(tmp_path, "goldsky_activity")
    w.append(orjson.dumps({"data": FIX["goldsky_rows"]}).decode(), tw=T0 * NS,
             meta={"job": "cohort-x", "status": 200})
    w.close()
    compact(tmp_path, tmp_path / "fills")
    [(addr, rows)] = list(iter_wallet_fills(tmp_path / "fills"))
    src = {r["id"]: r for r in FIX["goldsky_rows"]}
    for r in rows:
        assert r["order_id"] == src[r["id"]]["order_id"]
        assert r["transaction_hash"] == src[r["id"]]["transaction_hash"]
        assert r["counterparty_address"] is None


# ---------------------------------------------------------------- as-of and time
def test_asof_is_strictly_before_and_bounded():
    idx = BookIndex()
    idx.add_quote("A", 10 * NS, 0.40, 0.42)
    idx.add_quote("A", 11 * NS, 0.50, 0.52)
    idx.add_quote("A", 11 * NS, 0.55, 0.57)  # same receive time: later record wins after it
    idx.finalize()
    assert idx.quote("A", 10 * NS) is None  # nothing before the first quote
    assert idx.quote("A", 11 * NS) == (0.40, 0.42)  # a quote AT t is not before t
    assert idx.quote("A", 11 * NS + 1) == (0.55, 0.57)
    assert idx.quote("A", 14 * NS) == (0.55, 0.57)  # 3 s old: ok
    assert idx.quote("A", 14 * NS + 1) is None  # > 3 s old
    assert idx.quote("B", 11 * NS) is None
    rng = np.random.default_rng(1)
    for t in rng.integers(9 * NS, 16 * NS, 200):
        q = idx.quote("A", int(t))
        tw = idx.books["A"][0]
        i = int(np.searchsorted(tw, t, side="left")) - 1
        assert q is None or tw[i] < t


def test_print_ms_join_and_block_slack(tmp_path):
    tm = _tmap(tmp_path, [_tok("U", SLUG, 0, 1.0), _tok("D", SLUG, 1, 0.0)])
    idx = BookIndex()
    idx.add_print("0xA", (START + 99) * 1000 + 250, 0.5, 10, "BUY", "U")
    idx.add_print("0xB", (START + 50) * 1000, 0.5, 10, "BUY", "OTHER")  # other asset, same tx
    idx.finalize()
    ff = FixedFair(0.6)
    rows = [_row(1, "U", "BUY", 0.5, tx="0xA", t=START + 100),
            _row(2, "U", "BUY", 0.5, tx="0xB", t=START + 51),
            _row(3, "U", "BUY", 0.5, tx="0xC", t=START + 100)]
    fills, _ = enrich(rows, tm, idx, ff)  # type: ignore[arg-type]
    f = {x["id"]: x for x in fills}
    assert f["r1"]["time_src"] == "print_ms" and f["r1"]["t"] == START + 99.25
    assert f["r1"]["t_dec"] == f["r1"]["t"]
    assert f["r2"]["time_src"] == "print_ms" and f["r2"]["t"] == START + 50
    assert f["r3"]["time_src"] == "block_s" and f["r3"]["t"] == START + 100
    assert f["r3"]["t_dec"] == START + 100 - BLOCK_SLACK_S
    # fair second = floor(t_dec) - 1
    assert sorted(ff.asked) == sorted([START + 98, START + 49, START + 100 - BLOCK_SLACK_S - 1])


def test_book_state_never_after_fill(tmp_path):
    tm = _tmap(tmp_path, [_tok("U", SLUG, 0, 1.0)])
    idx = BookIndex()
    idx.add_quote("U", (START + 97) * NS, 0.30, 0.32)
    idx.add_quote("U", (START + 98) * NS + 500_000_000, 0.60, 0.62)  # the block_s fill's impact
    idx.add_quote("U", (START + 99) * NS + 300_000_000, 0.70, 0.72)  # after the print
    idx.finalize()
    idx.add_print("0xP", (START + 99) * 1000 + 200, 0.5, 10, "BUY", "U")
    rows = [_row(1, "U", "BUY", 0.5, tx="0xP", t=START + 100),
            _row(2, "U", "BUY", 0.5, tx="0xQ", t=START + 100)]
    f = {x["id"]: x for x in enrich(rows, tm, idx, None)[0]}
    assert (f["r1"]["bid"], f["r1"]["ask"]) == (0.60, 0.62)  # print at .2: .5 quote is before
    assert (f["r2"]["bid"], f["r2"]["ask"]) == (0.30, 0.32)  # block_s: cut at t - 2 s


# ---------------------------------------------------------------- signs
@pytest.mark.parametrize("tok,side,price,edge,mo1,settle", [
    ("U", "BUY", 0.50, 10.0, 5.0, 50.0),
    ("U", "SELL", 0.50, -10.0, -5.0, -50.0),
    ("D", "BUY", 0.30, 10.0, 15.0, -30.0),
    ("D", "SELL", 0.30, -10.0, -15.0, 30.0),
])
def test_sign_conventions(tmp_path, tok, side, price, edge, mo1, settle):
    tm = _tmap(tmp_path, [_tok("U", SLUG, 0, 1.0), _tok("D", SLUG, 1, 0.0)])
    idx = BookIndex()
    t = START + 100
    for a, (b, k) in {"U": (0.54, 0.56), "D": (0.44, 0.46)}.items():
        idx.add_quote(a, (t - 5) * NS, b - 0.1, k - 0.1)
        idx.add_quote(a, t * NS + 500_000_000, b, k)  # mid U 0.55, D 0.45 after the fill
    idx.finalize()
    idx.add_print("0x1", t * 1000, price, 10, side, tok)
    [f], _ = enrich([_row(1, tok, side, price, tx="0x1", t=t + 1)], tm, idx, FixedFair(0.6))  # type: ignore[arg-type]
    assert f["edge_c"] == pytest.approx(edge)
    assert f["mo1_c"] == pytest.approx(mo1)
    assert f["settle_c"] == pytest.approx(settle)
    assert f["settle_usd"] == pytest.approx(settle / 100 * 10)
    assert f["udir"] == (1 if (tok == "U") == (side == "BUY") else -1)


def test_settlement_pnl_matches_ledger(tmp_path):
    s2 = f"btc-updown-5m-{START + 300}"
    tm = _tmap(tmp_path, [_tok("U", SLUG, 0, 1.0), _tok("D", SLUG, 1, 0.0),
                          _tok("U2", s2, 0, 0.0), _tok("D2", s2, 1, 1.0)])
    rng = np.random.default_rng(5)
    rows = []
    for i in range(40):
        tok = ["U", "D", "U2", "D2"][i % 4]
        rows.append(_row(i, tok, "BUY" if i < 30 else "SELL", float(rng.integers(5, 95)) / 100,
                         sh=float(rng.integers(1, 20)), fee=0.01, t=START + i))
    fills, _ = enrich(rows, tm, BookIndex().finalize(), None)
    led = wallet_ledger(rows, tm)
    assert sum(f["settle_usd"] for f in fills) == pytest.approx(sum(p for _, p in led["tok_pnl"]))
    e = dz.edge_decomposition(fills)
    assert e["all_resolved_crypto"]["fees_usd"] == pytest.approx(0.4)


# ---------------------------------------------------------------- orders
def test_order_grouping(tmp_path):
    tm = _tmap(tmp_path, [_tok("U", SLUG, 0, 1.0)])
    idx = BookIndex()
    idx.add_quote("U", (START + 88) * NS, 0.48, 0.52)  # before t_dec = 92 - 2
    idx.finalize()
    rows = [_row(1, "U", "BUY", 0.47, sh=5, t=START + 92, role="maker", oid="o1", cp="0xAB"),
            _row(2, "U", "BUY", 0.47, sh=3, t=START + 95, role="maker", oid="o1", cp="0xab"),
            _row(3, "U", "BUY", 0.52, sh=2, t=START + 96, role="taker", oid="o1"),
            _row(4, "U", "SELL", 0.60, sh=1, t=START + 200, role="taker", oid="o2"),
            _row(5, "U", "SELL", 0.60, sh=1, t=START + 201, role="taker", oid=None)]
    fills, _ = enrich(rows, tm, idx, None)
    o, recs = orders(fills)
    assert o["orders"] == 2 and o["fills_without_order_id"] == 1
    r1 = {r["order_id"]: r for r in recs}["o1"]
    assert r1["n"] == 3 and r1["roles"] == {"maker": 2, "taker": 1} and r1["shares"] == 10
    assert o["maker_orders"] == 1 and o["taker_orders"] == 1 and o["mixed_role_orders"] == 1
    assert o["multi_price_orders"] == 1
    assert o["lifetime_s_maker"]["p50"] == pytest.approx(4.0)
    assert o["maker_mid_dist_c"]["p50"] == pytest.approx(3.0)  # BUY at .47 vs mid .50
    cp = dz.counterparties(fills)
    assert cp["top"][0] == {"address": "0xab", "notional": 3.76, "fills": 2, "share": 1.0}


def test_inventory_skew_sign(tmp_path):
    tm = _tmap(tmp_path, [_tok("U", SLUG, 0, 1.0), _tok("D", SLUG, 1, 0.0)])
    rng = np.random.default_rng(3)
    for against in (True, False):  # leans against inventory vs. trades at random
        rows, inv = [], 0.0
        for i in range(200):
            up = inv < 0 if inv != 0 else bool(rng.integers(0, 2))
            if not against or rng.random() < 0.25:  # noise, so both directions occur
                up = bool(rng.integers(0, 2))
            sh = float(rng.integers(1, 20))
            # Up exposure via BUY Up / SELL Down, or the reverse, mixing both tokens
            tok = "U" if rng.random() < 0.5 else "D"
            side = "BUY" if (tok == "U") == up else "SELL"
            rows.append(_row(i, tok, side, 0.5, sh=sh, t=START + i))
            inv += sh if up else -sh
        fills, _ = enrich(rows, tm, BookIndex().finalize(), None)
        summ, wins = dz.inventory(fills)
        c = summ["skew_vs_inventory"]["nonzero_inventory"]["corr"]
        assert wins[0]["up_close"] - wins[0]["down_close"] == pytest.approx(inv)
        assert wins[0]["net_close"] == pytest.approx(inv)
        assert c is not None and (c > 0.3 if against else abs(c) < 0.2)


# ---------------------------------------------------------------- fair equivalence
def test_fair_up_at_equals_fair_up_raw():
    rng = np.random.default_rng(7)
    raw = {T0 + i: 60000 + float(v) for i, v in enumerate(np.cumsum(rng.normal(0, 3, 2400)))}
    t0, a = to_array(raw)
    start, end = T0 + 900, T0 + 1800
    ptb = raw[start - 1] + 2.0
    for d in list(range(start - 1, end)) + [end + 5]:
        for lag in (0, 2):
            assert fair_up_at(a, t0, ptb, d, end, lag) == fair_up_raw(a, t0, ptb, d, end, lag)
    b = Brti(raw, {start - 1: ptb}, 0)
    assert b.fair(start, end, start - 2) is None  # priceToBeat not yet known
    assert b.fair(start, end, start - 1) == fair_up_raw(a, t0, ptb, start - 1, end)


# ---------------------------------------------------------------- policy inversion
def _synthetic(tmp_path, rule: str, n_win=60, seed=11):
    """BTC 5m windows with raw BRTI, both tokens' books every second, and a taker wallet."""
    rng = np.random.default_rng(seed)
    first = T0 + 1200
    n = (first - T0) + n_win * 300 + 100
    raw = {T0 + i: 60000 + float(v) for i, v in enumerate(np.cumsum(rng.normal(0, 4, n)))}
    brti = Brti(raw, dict(raw), 0)  # published series = raw: priceToBeat = raw[start-1]
    recs, rows, idx = [], [], BookIndex()
    k = 0
    for w in range(n_win):
        start = first + 300 * w
        end = start + 300
        slug = f"btc-updown-5m-{start}"
        up_pay = 1.0 if raw[end - 1] >= raw[start - 1] else 0.0
        recs += [_tok(f"U{w}", slug, 0, up_pay, cond=f"m{w}"),
                 _tok(f"D{w}", slug, 1, 1 - up_pay, cond=f"m{w}")]
        for s in range(start - 1, end):
            fu = brti.fair(start, end, s)
            if fu is None:
                continue
            ask = float(np.clip(round(fu + rng.uniform(-0.10, 0.10), 2), 0.02, 0.99))
            bid = round(ask - 0.02, 2)
            tw = s * NS + 500_000_000  # received at s + 0.5: the panel row for s + 1 sees it
            idx.add_quote(f"U{w}", tw, bid, ask)
            idx.add_quote(f"D{w}", tw, round(1 - ask, 2), round(1 - bid, 2))
            nxt = s + 1  # decision second
            if nxt >= end:
                continue
            if rule == "edge":
                take = (fu - ask > 0.05) and rng.random() < 0.3
                side_tok = f"U{w}"
                px = ask
            else:
                take = rng.random() < 0.02
                up = rng.random() < 0.5
                side_tok, px = (f"U{w}", ask) if up else (f"D{w}", round(1 - bid, 2))
            if take:
                # block_s fill: t_dec = block - slack = nxt
                rows.append(_row(k, side_tok, "BUY", px, sh=5, t=nxt + BLOCK_SLACK_S, oid=f"o{k}"))
                k += 1
    idx.finalize()
    return _tmap(tmp_path, recs), rows, idx, brti


def test_policy_inversion_recovers_edge_rule(tmp_path):
    tm, rows, idx, brti = _synthetic(tmp_path, "edge")
    doc, tables = dz.dossier("0xw", rows, tm, idx, brti)
    assert len(tables["fills"]) == len(rows) and len(tables["windows"]) == 60
    pol = doc["policy"]
    ta = pol["took_any"]
    assert ta["test_pos"] >= dz.MIN_POS_TEST, pol
    assert ta["auc_hgb"] > 0.85 and ta["auc_tree"] > 0.8, ta
    assert "edge_up" in ta["rules"]
    assert pol["verdict"] == "EXPLAINED"
    assert pol["side_given_took"]["result"] == "insufficient"  # never took Down
    assert pol["split"]["train_windows"] > pol["split"]["test_windows"] > 0
    assert doc["coverage"]["book_share"] == 1.0 and doc["coverage"]["fair_share_btc"] == 1.0
    # the per-fill model edge is positive by construction (> 5 c minus 1 s of drift)
    assert doc["edge"]["taker"]["model_edge_c_per_share"] > 3.0
    md = dz.card(doc)
    assert "edge_up" in md and "EXPLAINED" in md


def test_policy_inversion_random_taker_is_unexplained(tmp_path):
    tm, rows, idx, brti = _synthetic(tmp_path, "random", seed=13)
    doc, _ = dz.dossier("0xw", rows, tm, idx, brti)
    pol = doc["policy"]
    assert 0.38 < pol["took_any"]["auc_hgb"] < 0.62, pol["took_any"]
    sg = pol["side_given_took"]
    if "auc_hgb" in sg:
        assert 0.3 < sg["auc_hgb"] < 0.7
    assert pol["verdict"] == "UNEXPLAINED"


def test_policy_insufficient_guard(tmp_path):
    tm, rows, idx, brti = _synthetic(tmp_path, "edge", n_win=6)
    doc, _ = dz.dossier("0xw", rows[:5], tm, idx, brti)
    assert doc["policy"]["verdict"] == "insufficient"


# ---------------------------------------------------------------- CLI end to end
def test_cli_writes_dossier(tmp_path, monkeypatch):
    data, lab = tmp_path / "data", tmp_path / "lab"
    rng = np.random.default_rng(2)
    start = T0 + 1200
    raw = {T0 + i: 60000 + float(v) for i, v in enumerate(np.cumsum(rng.normal(0, 3, 2000)))}
    w = SegmentWriter(data, "ref_brti_raw")
    for t, v in raw.items():
        w.append(orjson.dumps({"t_ms": t * 1000, "v": str(v)}).decode(), tw=t * NS)
    w.close()
    w = SegmentWriter(data, "ref_prices")
    w.append(orjson.dumps({"series": [{"candles": [{"timestamp": t, "close": v}
                                                   for t, v in raw.items()]}]}).decode(),
             tw=T0 * NS, meta={"asset": "crypto:btc", "status": 200})
    w.close()
    slug = f"btc-updown-5m-{start}"
    w = SegmentWriter(data, "clob_btc_5m")
    for s in range(start - 10, start + 300):
        w.append(orjson.dumps({"market": "m1", "event_type": "price_change", "timestamp": str(s * 1000),
                               "price_changes": [{"asset_id": "U", "best_bid": "0.48", "best_ask": "0.50"},
                                                 {"asset_id": "D", "best_bid": "0.50", "best_ask": "0.52"}]}
                              ).decode(), tw=s * NS + 1000)
    w.append(orjson.dumps({"market": "m1", "event_type": "last_trade_price", "asset_id": "U",
                           "price": "0.50", "size": "10", "side": "BUY", "timestamp": str((start + 50) * 1000),
                           "transaction_hash": "0xt1"}).decode(), tw=(start + 50) * NS + 5000)
    w.close()
    gw = SegmentWriter(data, "goldsky_activity")
    gr = [{**_row(1, "U", "BUY", 0.5, t=start + 51, tx="0xt1", oid="o1"), "address": "0xW", "event_type": "TRADE"},
          {**_row(2, "D", "BUY", 0.52, t=start + 120, tx="0xt2", oid="o2", role="maker", cp="0xC"),
           "address": "0xW", "event_type": "TRADE"}]
    gw.append(orjson.dumps({"data": gr}).decode(), tw=T0 * NS, meta={"job": "cohort-a", "status": 200})
    gw.close()
    lab.mkdir()
    (lab / "tokens.jsonl").write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in [
        _tok("U", slug, 0, 1.0, cond="m1"), _tok("D", slug, 1, 0.0, cond="m1")]))
    cohort = tmp_path / "cohort.json"
    cohort.write_bytes(orjson.dumps({"sha256": "ab" * 32, "candidates": ["0xw"], "controls": []}))

    async def ensure(self, tokens, **kw):
        return None

    monkeypatch.setattr(dz.TokenMap, "ensure", ensure)
    assert dz.main(["--data", str(data), "--lab", str(lab), "--cohort", str(cohort), "--top", "1"]) == 0
    res = dz.run(data, lab, cohort, wallets=["0xW", "0xnobody"])
    assert list(res["wallets"]) == ["0xw"] and res["missing"] == ["0xnobody"]
    doc = orjson.loads((lab / "dossiers" / "0xw.json").read_bytes())
    assert doc["coverage"]["fills"] == 2 and doc["coverage"]["print_ms_share"] == 0.5
    assert doc["coverage"]["book_share"] == 1.0 and doc["coverage"]["fair_share_btc"] == 1.0
    assert doc["settlement_basis"]["windows"] == 1
    assert doc["counterparties"]["top"][0]["address"] == "0xc"
    assert doc["policy"]["verdict"] == "insufficient"
    md = (lab / "dossiers" / "0xw.md").read_text()
    assert "Edge decomposition" in md and "Data coverage" in md
    for name in ("fills", "orders", "windows"):
        assert (lab / "dossiers" / f"0xw.{name}.parquet").exists()
    assert math.isfinite(doc["clock"]["clob_btc_5m"]["tw_minus_server_ms_p50"])


def test_print_with_implausible_clock_is_rejected(tmp_path):
    tm = _tmap(tmp_path, [_tok("U", SLUG, 0, 1.0)])
    idx = BookIndex()
    idx.add_print("0xA", (START + 100 - 9000) * 1000, 0.5, 10, "BUY", "U")  # 2.5 h early
    idx.finalize()
    fills, cnt = enrich([_row(1, "U", "BUY", 0.5, tx="0xA", t=START + 100)], tm, idx, None)
    assert fills[0]["time_src"] == "block_s" and cnt["print_rejected_clock"] == 1


def _reactive_book(tmp_path, seed=21, n_win=50):
    """Random taker whose trade moves the book (ask +0.15 for 3 s) and whose block lands 1-2 s
    after the true trade time. Without the block slack the panel would see the impact."""
    rng = np.random.default_rng(seed)
    first = T0 + 1200
    n = (first - T0) + n_win * 300 + 100
    raw = {T0 + i: 60000 + float(v) for i, v in enumerate(np.cumsum(rng.normal(0, 4, n)))}
    brti = Brti(raw, dict(raw), 0)
    recs, rows, idx = [], [], BookIndex()
    k = 0
    for w in range(n_win):
        start = first + 300 * w
        end = start + 300
        slug = f"btc-updown-5m-{start}"
        recs += [_tok(f"U{w}", slug, 0, 1.0, cond=f"m{w}"), _tok(f"D{w}", slug, 1, 0.0, cond=f"m{w}")]
        takes = sorted(start + 5 + rng.uniform(0, 1) + i for i in range(0, 290, 1) if rng.random() < 0.03)
        for tau in takes:
            rows.append(_row(k, f"U{w}", "BUY", 0.5, sh=5,
                             t=math.floor(tau) + int(rng.integers(1, BLOCK_SLACK_S + 1)), oid=f"o{k}"))
            idx.add_quote(f"U{w}", int((tau + 0.05) * NS), 0.48, 0.65)  # impact, just after tau
            k += 1
        for sec in range(start - 1, end):
            tw = sec * NS + 500_000_000
            hit = any(tau < sec + 0.5 <= tau + 3 for tau in takes)
            ask = 0.65 if hit else 0.50
            idx.add_quote(f"U{w}", tw, 0.48, ask)
            idx.add_quote(f"D{w}", tw, round(1 - ask, 2), 0.52)
    idx.finalize()
    return _tmap(tmp_path, recs), rows, idx, brti


def test_panel_has_no_post_trade_leak(tmp_path, monkeypatch):
    tm, rows, idx, brti = _reactive_book(tmp_path)
    doc, _ = dz.dossier("0xw", rows, tm, idx, brti)
    clean = doc["policy"]["took_any"]["auc_hgb"]
    assert 0.38 < clean < 0.62, doc["policy"]["took_any"]
    monkeypatch.setattr(dz, "BLOCK_SLACK_S", 0)  # trust block time: the impact leaks in
    doc, _ = dz.dossier("0xw", rows, tm, idx, brti)
    leaky = doc["policy"]["took_any"]["auc_hgb"]
    assert leaky > 0.75 and leaky > clean + 0.2, (clean, leaky)


def test_fit_drops_all_nan_and_constant_columns():
    import numpy as np

    from doppel.lab.dossier import FEATURES, _fit
    rng = np.random.default_rng(0)
    n, k = 400, len(FEATURES)
    X = rng.normal(size=(n, k))
    X[:, 0] = np.nan          # all-NaN column: used to crash sklearn's binning
    X[:, 1] = 3.0             # constant column
    y = (X[:, 2] > 0).astype(int)
    res = _fit(X[:300], y[:300], X[300:], y[300:])
    names = list(FEATURES)
    assert names[0] in res["features_dropped"] and names[1] in res["features_dropped"]
    assert res["auc_hgb"] > 0.9
    assert _fit(X[:300], np.zeros(300, int), X[300:], y[300:])["status"] == "insufficient"


def test_inventory_counts_short_pairs_of_a_mint_and_sell_maker(tmp_path):
    """Audit 2026-11-26: selling both Up and Down (mint-and-sell) is a complete set, like buying both."""
    tm = _tmap(tmp_path, [_tok("U", SLUG, 0, 1.0), _tok("D", SLUG, 1, 0.0)])
    rows = [_row(0, "U", "SELL", 0.52, sh=10.0, t=START + 1), _row(1, "D", "SELL", 0.49, sh=10.0, t=START + 2)]
    fills, _ = enrich(rows, tm, BookIndex().finalize(), None)
    summ, wins = dz.inventory(fills)
    assert wins[0]["pairs"] == pytest.approx(10.0)
    assert summ["paired_share_of_gross"] == pytest.approx(0.5)
