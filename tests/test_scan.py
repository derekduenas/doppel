import orjson

from doppel.lab.scan import scan, select_cohort
from doppel.tape.segment import SegmentWriter


def _row(tx, wallet, slug, side, outcome, price, size, fee, ts):
    return orjson.dumps({"payload": {"transactionHash": tx, "proxyWallet": wallet, "eventSlug": slug,
                                     "asset": f"tok{outcome}", "side": side, "outcomeIndex": outcome,
                                     "price": price, "size": size, "fee": fee, "timestamp": ts,
                                     "name": wallet}}).decode()


def test_scan_dedupes_sockets_and_measures(tmp_path):
    slug = "btc-updown-5m-1795622400"  # ends 1795622700
    rows = [_row("0x1", "W", slug, "BUY", 0, 0.5, 10, 0.1, 1795622640),
            _row("0x2", "W", slug, "BUY", 1, 0.45, 10, 0.0, 1795622650),
            _row("0x3", "V", "nba-game", "BUY", 0, 0.5, 10, 0.1, 1795622650)]  # not a window
    for s in ("rtds_a", "rtds_b"):
        w = SegmentWriter(tmp_path, s)
        for r in rows:
            w.append(r, tw=1795622660 * 10**9)
        w.close()
    doc = scan(tmp_path)
    assert set(doc["wallets"]) == {"w"}
    x = doc["wallets"]["w"]
    assert x["fills"] == 2 and x["taker_fills"] == 1 and x["maker_fills"] == 1
    assert x["both_sides_windows"] == 1 and x["tte_taker_p50"] == 60 and x["tte_maker_p50"] == 50
    assert x["usd"] == 9.5


def test_cohort_is_deterministic_and_profit_blind():
    ws = {f"w{i:04d}": {"usd": 10000 - i, "n_windows": 10} for i in range(400)}
    doc = {"span": [0, 1], "wallets": ws}
    a, b = select_cohort(doc, n_cand=10, n_ctrl=5), select_cohort(doc, n_cand=10, n_ctrl=5)
    assert a["sha256"] == b["sha256"] and a["candidates"] == [f"w{i:04d}" for i in range(10)]
    assert len(a["controls"]) == 5 and all(c not in a["candidates"] for c in a["controls"])
