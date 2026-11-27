import json
from pathlib import Path

from doppel.lab.xvenue import kalshi_fee, kalshi_top, pmus_fee, pmus_top

FIX = json.loads((Path(__file__).parent / "fixtures_formats.json").read_text())


def test_fees():
    assert pmus_fee(0.5, 1) == 0.02          # 0.02375 -> half-even cents -> 0.02
    assert pmus_fee(0.5, 100) == 2.38
    assert kalshi_fee(0.5, 1) == 0.02        # ceil(1.75 c) = 2 c
    assert kalshi_fee(0.5, 100) == 1.75


def test_tops_parse():
    p = pmus_top(FIX["pmus_book_rest"]["body"])
    assert p is not None and 0 < p[0] < p[2] < 1
    k = kalshi_top({"orderbook_fp": {"yes_dollars": [["0.40", "10"], ["0.42", "5"]],
                                     "no_dollars": [["0.55", "7"], ["0.50", "3"]]}})
    assert k == (0.42, 5.0, 0.45, 7.0)     # yes bid 0.42; yes ask = 1 - best no bid 0.55


def test_episodes_count_once_and_need_persistence():
    from doppel.lab.xvenue import episodes
    ser = [(10, 0.03, 100), (11, 0.02, 80), (12, -0.01, 50), (20, 0.05, 10), (30, 0.04, 5), (31, 0.04, 5)]
    eps = episodes(ser, persist=2)
    # valued at the second the run qualifies (2nd second here): 0.02 x 80, 0.04 x 5 (audit A1-F4)
    assert [(e["start"], e["secs"], e["dollars"]) for e in eps] == [(10, 2, 1.6), (30, 2, 0.2)]


def test_different_strikes_never_count_as_locked_pairs(tmp_path, monkeypatch):
    import doppel.lab.xvenue as xv
    import doppel.lab.pmus_tape as pm
    start = xv._iso("2026-11-26T00:00:00Z")
    t = start + 10
    slug = "cpc-btc-updown-15m-2026-11-26-0000z"
    monkeypatch.setattr(pm, "ws_books", lambda *args: {slug: [(t, .1, 10, .2, 10), (t+1, .1, 10, .2, 10)]})
    book = {"orderbook_fp": {"yes_dollars": [["0.8","10"]], "no_dollars": [["0.1","10"]]}}
    monkeypatch.setattr(xv, "load_books", lambda *args: {"k": [(t, book), (t+1, book)]})
    meta = {"open": start, "close": start+900, "strike": 110, "result": "yes"}
    monkeypatch.setattr(xv, "kalshi_meta", lambda *args: {"k": meta})
    monkeypatch.setattr(xv, "pmus_meta", lambda *args: {"ptb": 100, "settle": 120})
    report = xv.run(tmp_path, tmp_path)
    assert report["summary"]["episodes"] == 0
    assert report["equivalence"]["excluded"][0]["reason"] == "strike_mismatch"
    meta["strike"] = 100
    assert xv.run(tmp_path, tmp_path)["summary"]["episodes"] == 1
    meta["result"] = "no"
    assert xv.run(tmp_path, tmp_path)["summary"]["episode_dollars"] is None
