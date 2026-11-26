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
