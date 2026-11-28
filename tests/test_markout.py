import time

import numpy as np
import orjson

from doppel.lab import markout as mo
from doppel.lab.markout import NS, Fill, MarketPath
from doppel.strategy.genome import Condition, Genome, KillRule, MakerRule, Risk, Source, TakerRule, Universe
from doppel.tape.segment import SegmentWriter
from doppel.tape.universe import pmus_slug
from doppel.twin.engine import SimConfig, WindowSim
from doppel.twin.features import Features, WindowRef
from doppel.twin.tape import Book, Trade, WindowTape

T0 = 1795046400
S = T0 + 3600
MS = 1_000_000


# ---------------------------------------------------------------- as-of rules
def _path(books, gaps=(), outcome=1, start=S, end=S + 300, key="k"):
    p = MarketPath(key=key, start=start, end=end, outcome=outcome, gaps=list(gaps))
    for tw, b, a in books:
        p.tws.append(tw); p.bids.append(b); p.asks.append(a)
    return p


def test_mid_asof_never_uses_next_book_gap_and_settlement():
    p = _path([(S * NS, 0.40, 0.42), ((S + 10) * NS, 0.60, 0.62)], gaps=[(S + 20) * NS])
    assert p.mid_asof(S * NS - 1) is None
    assert p.mid_asof(S * NS) == 41.0
    assert p.mid_asof((S + 10) * NS - 1) == 41.0          # the later book is not known yet
    assert p.mid_asof((S + 10) * NS) == 61.0
    assert p.mid_asof((S + 25) * NS) is None              # gap after the last book
    assert p.mid_asof((S + 300) * NS) == 100.0            # at the end: settlement
    one_side = _path([(S * NS, None, 0.42)])
    assert one_side.mid_asof(S * NS) is None


def test_marks_signed_net_of_fee_and_block_anchor():
    p = _path([(S * NS, 0.40, 0.42), ((S + 1) * NS, 0.50, 0.52)], outcome=0)
    buy = Fill("k", S * NS, "buy", 0.42, 10, 0.10, "taker", "receive")
    m = mo.marks(buy, p, fair=False)
    assert m["mid"]["0.5s"] == -1.0 and m["mid"]["2s"] == 9.0 and m["mid"]["settle"] == -42.0
    assert m["fee_c"] == 1.0
    sell = Fill("k", S * NS, "sell", 0.40, 10, 0.0, "maker", "receive")
    assert mo.marks(sell, p, fair=False)["mid"]["2s"] == -11.0
    blk = Fill("k", S * NS, "buy", 0.42, 10, 0.0, None, "block:0xabc")
    mb = mo.marks(blk, p, fair=False)
    assert mb["mid"]["0.5s"] is None and mb["mid"]["2s"] is None and mb["mid"]["10s"] == 9.0
    (re,) = mo.reanchor([blk], {"0xabc": S * NS + 7})
    assert re.anchor == "receive" and re.t_ns == S * NS + 7
    rows = [(buy, m, "w")]
    net = mo.curve(rows, net=True, n_boot=50)["by_h"]["2s"]["mean_c"]
    assert net == 8.0


# ---------------------------------------------------------------- planted shapes
def _shape_case(a, b, c, role, seed, n=40, noise=1.5):
    """n windows, one fill each at start+100; marks relative to px: +a at 0.3 s, +b at 5 s, +c at
    30 s (each plus noise), for buys and sells alike."""
    r = np.random.default_rng(seed)
    fills, paths = [], {}
    for i in range(n):
        st = S + 300 * i
        t0 = (st + 100) * NS
        side = "buy" if i % 2 else "sell"
        sg = 1 if side == "buy" else -1
        px = 0.50
        books = [(t0 - NS, 0.49, 0.51)]
        for dt, off in ((0.3, a), (5, b), (30, c)):
            m = px + sg * (off + r.normal(0, noise)) / 100
            books.append((t0 + int(dt * NS), m - 0.01, m + 0.01))
        key = f"w{i}"
        paths[key] = _path(books, outcome=int(r.random() < 0.5), start=st, end=st + 300, key=key)
        fills.append(Fill(key, t0, side, px, 5.0, 0.0, role, "receive"))
    return mo.analyse(fills, paths, n_boot=1000)["groups"]


def test_shape_information():
    assert _shape_case(3, 3, 3, "taker", 1)["taker"]["shape"] == "INFORMATION"


def test_shape_reversion():
    g = _shape_case(-3, -3, 3, "maker", 2)["maker"]
    assert g["shape"] == "REVERSION"
    assert g["mid_gross"]["paired"]["60s-minus-2s"]["ci95"][0] > 0


def test_shape_adverse_maker_only():
    assert _shape_case(-3, -3, -3, "maker", 3)["maker"]["shape"] == "ADVERSE"
    assert _shape_case(-3, -3, -3, "taker", 3)["taker"]["shape"] == "UNCLEAR"   # ADVERSE is for passive fills


def test_null_is_unclear_over_seeds():
    labels = [_shape_case(0, 0, 0, "maker", 100 + s)["maker"]["shape"] for s in range(20)]
    assert labels.count("UNCLEAR") >= 18, labels


# ---------------------------------------------------------------- twin parity
class _Fair(Features):
    def __init__(self, g, ref, v):
        super().__init__(g, ref)
        self.v = v

    def fair(self, d):
        return self.v


def test_reproduces_twin_markouts_at_1_5_30():
    g = Genome(name="m", mode="maker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
               maker=MakerRule(quote_ref="fair", half_spread_c=2.0, size=10.0, sides="bid_only",
                               requote_threshold_c=1.0, max_quote_age_s=30),
               risk=Risk(max_inventory=50), kill=KillRule(min_windows=30), created_tw=0)
    books = [Book(tw=(S - 1) * NS, bids=((0.48, 100.0),), asks=((0.52, 100.0),))]
    for s in range(S, S + 300, 3):
        m = 0.5 + 0.1 * np.sin(s / 17.0)
        books.append(Book(tw=s * NS + 3 * MS, bids=((round(m - 0.02, 2), 50.0),), asks=((round(m + 0.02, 2), 50.0),)))
    trades = [Trade(tw=(S + s) * NS + 5 * MS, t_exch=None, price=0.40, qty=5.0, aggressor="sell", id=f"T{s}")
              for s in range(20, 280, 25)]
    wt = WindowTape(slug=pmus_slug("btc", "5m", S), asset="btc", horizon="5m", start=S, end=S + 300,
                    source="ws", books=books, trades=trades)
    ref = WindowRef(asset="btc", start=S, end=S + 300, ptb=100.0, settle=101.0)
    res = WindowSim(g, wt, ref, SimConfig(), features=_Fair(g, ref, 0.5)).run()
    assert res["n_fills"] >= 3
    p = mo.path_from_window(wt, ref)
    for f in mo.fills_from_twin({"windows": [res]}):
        tf = next(x for x in res["fills"] if abs(S + x["t"] - f.t_ns / NS) < 1e-6)
        m = mo.marks(f, p, horizons=(1, 5, 30), fair=False)
        for k in ("1", "5", "30"):
            mine = m["mid"][f"{k}s"]
            assert (mine is None) == (tf["markout_c"][k] is None)
            assert mine is None or abs(mine - tf["markout_c"][k]) < 1e-9


# ---------------------------------------------------------------- CLI on a written tape
def _md(slug, bid, ask):
    return {"marketData": {"marketSlug": slug, "bids": [{"px": {"value": f"{bid:.4f}"}, "qty": "100"}],
                           "offers": [{"px": {"value": f"{ask:.4f}"}, "qty": "100"}],
                           "state": "MARKET_STATE_OPEN", "stats": {"sharesTraded": "0"}}}


def write_tape(data, n_windows=3, seed=4):
    """BTC raw BRTI, the published series and PM US 5m WS books for ``n_windows`` windows from S."""
    rng = np.random.default_rng(seed)
    lo, hi = S - 1500, S + 300 * n_windows + 400
    raw = {t: 90000 + float(v) for t, v in zip(range(lo, hi), np.cumsum(rng.normal(0, 3, hi - lo)))}
    w = SegmentWriter(data, "ref_brti_raw")
    for t, v in raw.items():
        w.append(orjson.dumps({"t_ms": t * 1000, "v": str(v), "received": (t + 0.3) * 1000}).decode(), tw=t * NS)
    w.close()
    series = {t: sum(raw[s] for s in range(t - 59, t + 1)) / 60 for t in range(lo + 60, hi)}
    w = SegmentWriter(data, "ref_prices")
    for t, v in series.items():
        if (t + 1) % 300 == 0:
            w.append(orjson.dumps({"series": [{"candles": [{"timestamp": t, "close": v}]}]}).decode(),
                     tw=(t + 1) * NS, meta={"asset": "crypto:btc", "status": 200, "from": t, "to": t})
    w.close()
    w = SegmentWriter(data, "pmus_ws_5m")
    w.append("", tw=(S - 400) * NS, meta={"ctl": "connect", "url": "wss://x"})
    for i in range(n_windows):
        st = S + 300 * i
        slug = pmus_slug("btc", "5m", st)
        for s in range(st - 30, st + 300, 2):
            m = float(np.clip(0.5 + (raw[s] - series[st - 1]) / 40.0, 0.03, 0.97))
            w.append(orjson.dumps(_md(slug, round(m - 0.01, 2), round(m + 0.01, 2))).decode(), tw=s * NS + 5 * MS)
    w.close()
    return raw, series


def test_cli_end_to_end_twin_doc(tmp_path, capsys):
    data = tmp_path / "data"
    write_tape(data)
    doc = {"windows": [{"slug": pmus_slug("btc", "5m", S + 300 * i), "start": S + 300 * i,
                        "fills": [{"t": 100.0, "role": "taker", "side": "buy", "px": 0.55, "qty": 10, "fee": 0.02},
                                  {"t": 200.0, "role": "maker", "side": "sell", "px": 0.45, "qty": 5, "fee": 0.0}]}
                       for i in range(3)]}
    fp = tmp_path / "twin.json"
    fp.write_bytes(orjson.dumps(doc))
    out = tmp_path / "mo.json"
    assert mo.main(["--data", str(data), "--fills", str(fp), "--out", str(out), "--boot", "200"]) == 0
    res = orjson.loads(out.read_bytes())
    g = res["groups"]["all"]
    assert res["fills_without_tape"] == 0 and g["mid_gross"]["n_fills"] == 6 and g["mid_gross"]["n_windows"] == 3
    assert all(g["mid_gross"]["by_h"][k]["n"] == 6 for k in ("0.5s", "2s", "10s", "60s", "settle"))
    assert g["fair_gross"]["by_h"]["2s"]["n"] == 6      # fair-v1 known from received BRTI
    assert "shape=" in capsys.readouterr().out


def test_journal_and_generic_rows(tmp_path):
    j = tmp_path / "journal-20261127.jsonl"
    j.write_bytes(b"\n".join(orjson.dumps(r) for r in (
        {"k": "intent", "slug": "s", "t": 1}, {"k": "fill", "slug": "s", "t": 5, "role": "maker", "side": "buy",
                                               "px": 0.4, "qty": 2, "fee": 0.0})) + b"\n")
    (f,) = mo.load_fills(tmp_path)
    assert (f.key, f.t_ns, f.side, f.role, f.anchor) == ("s", 5, "buy", "maker", "receive")
    g = tmp_path / "g.jsonl"
    g.write_bytes(orjson.dumps({"token_id": "123", "side": "SELL", "price": "0.3", "amount_shares": "4",
                                "fee": "0.01", "block_timestamp": int(time.time()), "transaction_hash": "0x1"}))
    (w,) = mo.load_fills(g)
    assert w.side == "sell" and w.anchor == "block:0x1" and w.qty == 4.0


def test_cli_intl_wallet_fill_reanchored_to_print(tmp_path):
    from pathlib import Path
    fix = orjson.loads((Path(__file__).parent / "fixtures_formats.json").read_bytes())
    recs = sorted(fix["clob_price_change"] + fix["clob_book"] + fix["clob_last_trade_price"], key=lambda r: r["tw"])
    data = tmp_path / "data"
    w = SegmentWriter(data, "clob_btc_5m")
    tok = fix["clob_book"][1]["msg"]["asset_id"]
    alive = {"market": recs[0]["msg"]["market"], "event_type": "price_change", "timestamp": "1795633209000",
             "price_changes": [{"asset_id": tok, "price": "0.01", "size": "5", "side": "BUY"}]}
    recs.append({"tw": recs[-1]["tw"] + 5 * NS, "msg": alive})   # a deep level changes 5 s later
    for r in recs:
        w.append(orjson.dumps(r["msg"]).decode(), tw=r["tw"])
    w.close()
    tx = fix["clob_last_trade_price"][0]["msg"]["transaction_hash"]
    t_blk = recs[0]["tw"] // NS
    fills = tmp_path / "wallet.jsonl"
    fills.write_bytes(orjson.dumps({"token_id": tok, "side": "BUY", "price": "0.65", "amount_shares": "10",
                                    "fee": "0", "block_timestamp": t_blk, "transaction_hash": tx}))
    outcomes = tmp_path / "outcomes.json"
    outcomes.write_bytes(orjson.dumps({tok: {"outcome": 1, "end": t_blk + 300}}))
    out = tmp_path / "o.json"
    assert mo.main(["--data", str(data), "--fills", str(fills), "--intl-stream", "clob_btc_5m",
                    "--outcomes", str(outcomes), "--out", str(out), "--boot", "50"]) == 0
    res = orjson.loads(out.read_bytes())
    assert res["anchors"] == {"receive": 1, "block": 0}           # joined to the print's receive time
    by = res["groups"]["all"]["mid_gross"]["by_h"]
    assert by["0.5s"]["mean_c"] == by["2s"]["mean_c"] == -15.0   # book 0.03 / 0.97 -> mid 50, bought at 65
    assert by["settle"]["mean_c"] == 35.0
    assert by["10s"]["n"] == 0                                    # feed ends: nothing claimed after it


def test_goldsky_fixture_role_and_tokens_jsonl_outcomes(tmp_path):
    from pathlib import Path
    fix = orjson.loads((Path(__file__).parent / "fixtures_formats.json").read_bytes())
    p = tmp_path / "g.jsonl"
    p.write_bytes(b"\n".join(orjson.dumps(r) for r in fix["goldsky_rows"]))
    fs = mo.load_fills(p)
    assert fs and {f.role for f in fs} <= {"taker", "maker"} and fs[0].role == "taker"
    assert fs[0].anchor == "block:" + fix["goldsky_rows"][0]["transaction_hash"]
    tj = tmp_path / "tokens.jsonl"
    tj.write_bytes(b"\n".join(orjson.dumps(r) for r in (
        {"token": "a", "idx": 0, "payout": 1.0, "status": "resolved", "end": "2026-11-26T22:25:00Z"},
        {"token": "b", "idx": 1, "payout": None, "status": "open", "end": None})))
    oc = mo.load_outcomes(tj)
    assert oc["a"]["outcome"] == 1 and oc["a"]["end"] == 1795731900 and oc["b"]["outcome"] is None
