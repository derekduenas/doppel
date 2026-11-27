from pathlib import Path

import orjson
import pytest

from doppel.lab.dissect_wallet import main
from doppel.lab.goldsky_proof import digest
from test_inspection_book import tape, STREAM
from test_ordermatch import MS, T0, book, pc, trade


def write(path: Path, value):
    path.write_bytes(orjson.dumps(value))


def test_dissect_cli_preserves_denominators_and_checks_native_alternatives(tmp_path):
    market = "0xf1"
    down = lambda t: book(t, "down", [(.07, 10)], [(.08, 90)], market)
    up = lambda t: book(t, "up", [(.92, 110)], [(.93, 10)], market)
    other = pc(11100, ("other", .1, 10, "BUY"), market=market)[1]
    path = tape(tmp_path, [
        down(1), up(2), pc(100, ("down", .07, 70, "BUY"), market=market),
        pc(101, ("down", .07, 10, "BUY"), market=market), down(11000), up(11001),
        (11100, [other, pc(11100, ("down", .07, 70, "BUY"), market=market)[1]]),
        pc(11120, ("other", .1, 10, "BUY"), ("down", .07, 130, "BUY"), market=market),
        (11200, [other, trade(11200, "down", .07, 120, "tx", market=market, side="SELL")[1]]),
        down(11300)])
    anchor = {"tw_ns": (MS+11200)*10**6, "venue_ms": MS+11200, "seq": 9, "ordinal": 1,
              "asset": "down", "side": "SELL", "price": .07, "size": 120}
    order = {"wallet": "alice", "order_id": "o1", "token": "down", "mate": "up", "market": "f1",
             "first_observed_tx": "tx", "price": .07, "side": "BUY", "size": 60, "continuous_coverage": True,
             "anchor_status": "located", "anchor": anchor, "actual": {"size_compatible_adds": 2}}
    orders = tmp_path / "orders.jsonl"
    orders.write_bytes(b"\n".join(orjson.dumps(r) for r in
                                 [order, order | {"order_id": "o2", "anchor_status": "no_transaction_print"}]))
    source = {"file": path.relative_to(tmp_path).as_posix(), "bytes": path.stat().st_size, "sha256": digest(path)}
    summary_path = tmp_path / "inspection.json"
    write(summary_path, {"schema": "wallet-inspection/2", "orders_sha256": digest(orders), "stream": STREAM,
                         "source_segments": [source], "missing_segments": [], "window_time": [T0, T0+900],
                         "wallets": {"alice": {"maker_orders": 2, "anchor_status": {"located": 1}}}})
    marks = [{"seq": seq, "venue_ms": MS+t, "tw_ns": (MS+t)*10**6, "old_size": old, "span": 1,
              "new_size": old+60, "message_index": msg, "change_index": ch}
             for seq, t, old, msg, ch in [(7, 11100, 10, 1, 0), (8, 11120, 70, 0, 1)]]
    case = {"schema": "wallet-inspection-first-order-crosscheck/1", "wallet": "alice",
            "order_hash": "o1", "signed_price": .07, "signed_size": 60, "signed_maker_side": "BUY",
            "transaction": "tx", "source": source | {"selected_utc": [T0, T0+900]},
            "tx_print": {"tw_ns": anchor["tw_ns"], "venue_ms": anchor["venue_ms"], "seq": 9, "span": 1,
                         "change_index": 0, "message_index": 1, "token": "down", "market": market,
                         "price": .07, "aggregate_shares": 120, "taker_side": "SELL"},
            "arms": {"actual": {"fragment_candidates": marks, "continuous": True, "fragment_candidate_count": 2,
                                "endpoint_ms": MS+11200, "endpoint_tw": (MS+11200)*10**6, "search_start_ms": MS+1200,
                                "search_start_tw": (MS+1200)*10**6}}}
    case_path = tmp_path / "case.json"
    write(case_path, case)
    args = ["--inspection-summary", str(summary_path), "--orders", str(orders), "--data", str(tmp_path),
            "--case", str(case_path), "--out"]
    out = tmp_path / "result"
    assert main([*args, str(out)]) == 0
    result = orjson.loads((out/"summary.json").read_bytes())
    assert result["wallets"]["alice"]["missing_anchor"] == 1
    assert [r["signed"]["state"]["level_size"] for r in result["primary"].values()] == [10, 70, 130]
    assert all(r["complement"]["state"]["best_bid"] == .92 for r in result["primary"].values())
    assert result["posting_training_labels"] == 0
    rows = [orjson.loads(line) for line in (out/"orders.jsonl").read_bytes().splitlines()]
    assert rows[1]["context"] is None
    actual = case["arms"]["actual"]
    outside_arm = marks[0] | {"seq": 3, "tw_ns": (MS+100)*10**6, "venue_ms": MS+100, "message_index": 0}
    for vals in ([outside_arm, marks[1]], list(reversed(marks)), [marks[0], marks[0]],
                 [marks[0] | {"span": 2}, marks[1]], [marks[0] | {"message_index": 0}, marks[1]],
                 [marks[0], marks[1] | {"change_index": 0}], [marks[0] | {"tw_ns": marks[0]["tw_ns"] - 1}, marks[1]]):
        write(case_path, case | {"arms": {"actual": actual | {"fragment_candidates": vals}}})
        with pytest.raises(ValueError, match="case addition differs"):
            main([*args, str(tmp_path/"forged")])
    write(case_path, case | {"tx_print": case["tx_print"] | {"message_index": 0}})
    with pytest.raises(ValueError, match="case execution differs"):
        main([*args, str(tmp_path/"forged")])
    write(case_path, case | {"arms": {"actual": actual | {"search_start_tw": (MS+1199)*10**6}}})
    with pytest.raises(ValueError, match="search bounds"):
        main([*args, str(tmp_path/"forged")])
    orders.write_bytes(orders.read_bytes()+b"\n")
    with pytest.raises(ValueError, match="bound Layer 2"):
        main([*args, str(tmp_path/"damaged")])
