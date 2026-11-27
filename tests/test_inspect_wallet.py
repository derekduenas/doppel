from dataclasses import replace
import hashlib

import orjson
import pytest

from doppel.lab import fillstore, inspect_wallet as iw, ordertape
from doppel.lab.goldsky import trade_binding
from doppel.lab.inspection_book import InspectionBook, LevelMark, Span, TradeMark
from doppel.lab.tradeallocation import Allocation
from test_inspection_book import tape
from test_ordermatch import MS, T0, book, pc, trade

W = "0x" + "f" * 40
TX = "0x" + "3" * 64
OID = "b" * 64
A, B = "A", "B"
KEY = (A, .07, "BUY")
T = MS + 100_000


def mark(ms, span=1, src="price_change", old=0., new=60.):
    return LevelMark(ms, ms * 1_000_000, 42, 0, span, old, new, src)


def evidence():
    rows = [{"order_id": OID, "token": A, "wallet": W, "market": "f1", "side": "BUY", "price": .07,
             "size": 60., "created_ms": T + 896_000,
             "fills": [{"block": 10, "block_ts": T // 1000, "tx": TX, "shares_micro": 60_000_000, "role": "maker"}]}]
    alloc = Allocation("f1", A, B, "SELL", .07, ((.07, 60.),), 60., .000002)
    owner = iw.Subject(W, (T0, T0 + 180), rows, {TX: alloc}, {A: B, B: A}, {})
    b = InspectionBook()
    b.spans[A] = [Span(1, MS, T + 1000, MS * 1_000_000, (T + 1000)*1_000_000)]
    b.events[KEY] = [mark(T - 8000)]
    b.empty[KEY] = [mark(T - 8200, src="book", new=0)]
    b.prints[TX] = [TradeMark(T, T * 1_000_000, 50, 0, 1, A, "0xf1", .07, "SELL", 60, TX)]
    return owner, b


def test_after_block_order_survives_and_unique_anonymous_add_stays_conditional():
    o, b = evidence()
    row, = iw.inspect([o], b)
    assert row["clock_flag"] == "after_block" and row["anchor_status"] == "located"
    assert row["actual"]["sole_uncontested_add"] and row["placebo"]["size_compatible_adds"] == 0
    assert row["actual"]["conditional_add_to_anchor_ms"] == 8000 and row["residence_upper_ms"] == 8200
    assert row["placement_identity"] == "unknown" and row["fully_filled_by_observed_trades"]
    assert iw.summary([row])["paired_sole_uncontested"] == {"actual": 1, "placebo": 0}


def test_two_candidates_and_shared_add_cannot_supply_unique_placement():
    o, b = evidence()
    b.events[KEY].append(mark(T - 7000))
    assert iw.inspect([o], b)[0]["actual"] == {"sole_uncontested_add": False, "paired_sole_uncontested_add": False,
                                              "size_compatible_adds": 2}
    b.events[KEY].pop()
    o.orders.append(o.orders[0] | {"order_id": "c"*64})
    assert all(not r["actual"]["sole_uncontested_add"] for r in iw.inspect([o], b))


def test_coverage_on_both_clocks_and_reset_between_arms():
    o, b = evidence()
    b.spans[A] = [Span(8, MS, T-40_000, MS*1_000_000, (T-40_000)*1_000_000),
                  Span(1, T-30_000, T+1, (T-30_000)*1_000_000, (T+1)*1_000_000)]
    r, = iw.inspect([o], b)
    assert r["paired_coverage"] and not r["unbroken_between_arms"]
    b.spans[A][1].start_tw = (T-9000)*1_000_000
    r, = iw.inspect([o], b)
    assert not r["continuous_coverage"] and r["residence_upper_ms"] is None and "actual" not in r


def test_contradictory_print_wrong_size_ambiguous_time_and_mirror():
    o, b = evidence()
    p, = b.prints[TX]
    b.prints[TX] = [p, replace(p, asset=B, side="BUY", price=.93)]
    assert iw.inspect([o], b)[0]["anchor_status"] == "located"
    b.prints[TX].append(replace(p, size=1))
    assert iw.inspect([o], b)[0]["anchor_status"] == "incompatible_transaction_print"
    b.prints[TX] = [p, replace(p, venue_ms=T+1)]
    assert iw.inspect([o], b)[0]["anchor_status"] == "ambiguous_transaction_time"
    b.prints.clear()
    assert iw.summary(iw.inspect([o], b))["paired_orders"] == 0


def test_no_future_or_delayed_evidence_and_no_snapshot_as_a_post():
    o, b = evidence()
    b.events[KEY] = [mark(T-9000, src="book"), mark(T), replace(mark(T-30), tw_ns=(T+3)*1_000_000)]
    r, = iw.inspect([o], b)
    assert r["actual"]["size_compatible_adds"] == 0


def test_real_entrypoint_binds_native_store_and_managed_order_tape(tmp_path):
    rows = [{"id": "log_tx_1", "address": W, "transaction_hash": TX, "order_id": OID, "token_id": A,
             "side":"BUY", "order_type":"maker", "price":.07, "amount_shares":60., "amount_usdc":4.2,
             "fee":0., "block_number":10, "block_timestamp":T//1000, "counterparty_address":W}]
    store = tmp_path / "fills"
    store.mkdir()
    writer = fillstore._BatchWriter(store/"part.parquet")
    writer.add(rows[0] | {"seg":"s", "ord":0, "job":"sample"})
    writer.close()
    fillstore._save_state(store, {"version":fillstore.STATE_VERSION, "job_prefix":"sample",
                                  "segments":{"s":{"file":"part.parquet","rows":1,"bytes":1}}})
    orders = tmp_path / "orders"
    base = {"tx":TX, "block_ts":T//1000,"block":10,"condition_id":"f1","n_makers":1,"token_id":A,
            "created_ms":T+896000, "maker_amount":4_200_000,"taker_amount":60_000_000,"fill_amount":4_200_000}
    part = "day=2026-11-25/part-000001.parquet"
    ordertape._write_part(orders/part, [base | {"role":"maker","side":"BUY","order_id":OID,"maker":W,"verified":True},
                                       base | {"role":"taker","side":"SELL","order_id":"c"*64,"maker":"other",
                                               "maker_amount":60_000_000,"taker_amount":4_200_000,
                                               "fill_amount":60_000_000,"verified":False}])
    ordertape._commit(orders, 1, [part], {TX:"decoded"})
    ledger = tmp_path/"ledger.json"
    query = {"start_time":T0,"end_time":T0+180,"pages":1,"stop":"done","rows":1,
             "trade_binding":trade_binding(rows)}
    coverage = {k:True for k in ("goldsky_complete","goldsky_store_verified",
                                 "block_time_window_verified","chain_scan_complete")}
    ledger.write_bytes(orjson.dumps({"status":"reconciled","not_reconciled":False,"wallet":W,"causes":[],
                                     "window_time":[T0,T0+180],"window_blocks":[1,11],
                                     "completeness":coverage | {"goldsky_queries":[query],"receipts_missing":0,
                                                                "chain_error":None},
                                     "goldsky_fills":1,"fill_check":{"order_filled":{"matched":1}},
                                     "tokens":{A:{"condition":"f1"}},"pairing":{"markets":{"f1":{"tokens":[A,B]}}}}))
    tape(tmp_path,[book(1000,A,market="0xf1"),pc(92_000,(A,.07,60,"BUY"),market="0xf1"),
                   (100_000,trade(100_000,A,.07,60,TX,market="0xf1")[1] | {"side":"SELL"}),
                   pc(101_000, (A, .07, 0, "BUY"), market="0xf1")])
    out = tmp_path/"out"
    store_sha, tape_sha, _, _ = iw.source_seals(store, orders)
    def publish():
        return {"schema": "wallet-absorption-study-summary/1",
                "windows": {"short": {"wallets": [{"wallet": W, "receipt": {"sha256": hashlib.sha256(ledger.read_bytes()).hexdigest()}}],
                                      "store": "store", "order_tape": "orders"}},
                "inputs": {"store": {"manifest_sha256": store_sha}, "orders": {"manifest_sha256": tape_sha}}}
    study = tmp_path / "published.json"
    study.write_bytes(orjson.dumps(publish()))
    assert iw.main(["--layer1-summary",str(study),"--data",str(tmp_path),"--out",str(out),"--subject",str(ledger),str(store),str(orders)])==0
    result=orjson.loads((out/"orders.jsonl").read_bytes())
    assert result["anchor_status"]=="located" and result["paired_coverage"]
    doc=orjson.loads(ledger.read_bytes())
    doc["completeness"]["goldsky_queries"][0]["trade_binding"]["trades"]=0
    ledger.write_bytes(orjson.dumps(doc))
    with pytest.raises(ValueError, match="published Layer 1 receipt"):
        iw.subject(ledger, store, orders, orjson.loads(study.read_bytes()))
    with pytest.raises(ValueError, match="binding"):
        iw.subject(ledger, store, orders, publish())
    flush, = (orders / "_state").glob("flush-*.json")
    flush.write_bytes(flush.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="Layer 1 fileset"):
        iw.subject(ledger, store, orders, publish())
