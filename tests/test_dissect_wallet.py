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
    path = tape(tmp_path, [book(1, "down", [(.07, 10)], [(.08, 90)]), book(2, "up", [(.92, 110)], [(.93, 10)]),
                           pc(100, ("down", .07, 70, "BUY")), pc(120, ("down", .07, 130, "BUY")),
                           trade(200, "down", .07, 120, "tx", market="0xf1", side="SELL")])
    anchor = {"tw_ns": (MS+200)*10**6, "venue_ms": MS+200, "seq": 5}
    order = {"wallet": "alice", "order_id": "o1", "token": "down", "mate": "up", "market": "f1",
             "first_observed_tx": "tx", "price": .07, "side": "BUY", "size": 60,
             "anchor_status": "located", "anchor": anchor, "actual": {"size_compatible_adds": 2}}
    orders = tmp_path / "orders.jsonl"
    orders.write_bytes(b"\n".join(orjson.dumps(r) for r in
                                 [order, order | {"order_id": "o2", "anchor_status": "no_transaction_print"}]))
    source = {"file": path.relative_to(tmp_path).as_posix(), "bytes": path.stat().st_size, "sha256": digest(path)}
    summary_path = tmp_path / "inspection.json"
    write(summary_path, {"schema": "wallet-inspection/2", "orders_sha256": digest(orders), "stream": STREAM,
                         "source_segments": [source], "missing_segments": [], "window_time": [T0, T0+900],
                         "wallets": {"alice": {"maker_orders": 2, "anchor_status": {"located": 1}}}})
    marks = [{"seq": seq, "venue_ms": MS+t, "tw_ns": (MS+t)*10**6, "old_size": old,
              "new_size": old+60, "message_index": 0, "change_index": 0} for seq,t,old in [(3,100,10),(4,120,70)]]
    case = {"wallet": "alice", "order_hash": "o1", "signed_price": .07, "signed_size": 60,
            "signed_maker_side": "BUY", "transaction": "tx", "source": source,
            "tx_print": {**anchor, "token": "down", "market": "0xf1"},
            "arms": {"actual": {"fragment_candidates": marks, "continuous": True, "fragment_candidate_count": 2}}}
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
    marks[0]["tw_ns"] -= 1
    write(case_path, case)
    with pytest.raises(ValueError, match="addition differs"):
        main([*args, str(tmp_path/"forged")])
    orders.write_bytes(orders.read_bytes()+b"\n")
    with pytest.raises(ValueError, match="bound Layer 2"):
        main([*args, str(tmp_path/"damaged")])
