"""Causal real-wallet export exercised with closed synthetic native tape and ledger inputs."""

import orjson
import pytest

from doppel.lab.goldsky_proof import digest
from doppel.lab.wallet_decisions import COVERAGE, export, main
from doppel.tape.segment import SegmentWriter
from test_rulefit import MS, NS, T0, W, book, trade

MARKET = "a" * 64
OTHER_WALLET = "0x" + "e" * 40
STREAM = "clob_btc_5m"


def write(root, stream, rows):
    with_rows = SegmentWriter(root, stream)
    try:
        for ms, msg, meta in rows:
            with_rows.append(orjson.dumps(msg), tw=(MS + ms) * 1_000_000, meta=meta)
    finally:
        with_rows.close()


def inputs(root, *, late_price=.55, delay_ms=1000, stale=False, gap=False,
           no_opening=False, extra_movement=False, bounded_to=170):
    root.mkdir()
    data = root / "tape"
    for i, token in enumerate(("U", "D")):
        obj = {"payload": {"eventSlug": f"btc-updown-5m-{T0}", "side": "BUY", "outcomeIndex": i,
                           "proxyWallet": OTHER_WALLET, "asset": token, "price": ".42", "size": "5",
                           "transactionHash": f"identity-{token}"}}
        write(data, ("rtds_a", "rtds_b")[i], [(1000, obj, None)])
    quotes = []
    for ms in ([16_000] if stale else [19_000]) + [29_000, 31_000, 39_000, 41_000]:
        quotes.extend([(*book(ms, "U", [(.40, 80)], [(.42, 100)], MARKET), None),
                       (*book(ms + 1, "D", [(.56, 70)], [(.59, 120)], MARKET), None)])
    label = trade(20_000, "U", .42, 10, "tx", MARKET)[1] | {"side": "BUY"}
    quotes.append((20_000 + delay_ms, label, None))
    # These quotes arrive before the print, but after exchange execution. They cannot be features.
    quotes.append((*book(20_500, "U", [(.40, 80)], [(late_price, 100)], MARKET), None))
    if gap:
        quotes.append((19_999, {}, {"ctl": "disconnect"}))
    write(data, STREAM, sorted(quotes, key=lambda r: r[0]))

    # Fair v1 will read actual source files and use only published data observable before each cut.
    ref = root / "reference"
    write(ref, "ref_brti_live", [(s * 1000 + 100, {}, {"new": [[MS + s * 1000, 50_000 + (s % 9)]]})
                                 for s in range(-320, 41)])
    if not no_opening:
        write(ref, "ref_prices", [(1000, {"series": [{"candles": [{"timestamp": T0 - 1, "close": 50_000}]}]},
                                  {"asset": "crypto:btc", "status": 200})])

    orders = root / "orders.jsonl"
    shared = {"tx": "tx", "condition_id": MARKET, "n_makers": 1, "token_id": "U", "block_ts": T0 + 21,
              "created_ms": MS + 17_000, "verified": True}
    taker = shared | {"order_id": "take", "side": "BUY", "role": "taker", "maker": W,
                      "maker_amount": 4_300_000, "taker_amount": 10_000_000, "fill_amount": 4_200_000}
    maker = shared | {"order_id": "make", "side": "SELL", "role": "maker", "maker": OTHER_WALLET,
                      "maker_amount": 10_000_000, "taker_amount": 4_200_000, "fill_amount": 10_000_000}
    orders.write_bytes(b"".join(orjson.dumps(o) + b"\n" for o in (taker, maker)))
    ledger = root / "wallet.json"
    movements = [{"tx": "tx", "kind": "buy", "chain_log_index": 4, "shares": {"U": 10_000_000}}]
    if extra_movement:
        movements.append({"tx": "extra", "kind": "transfer", "chain_log_index": 5, "shares": {"D": 5_000_000}})
    ledger.write_bytes(orjson.dumps({
        "wallet": W, "window_time": [T0, T0 + bounded_to], "status": "reconciled", "not_reconciled": False,
        "causes": [], "completeness": dict.fromkeys(COVERAGE, True), "condition_cash": movements,
        "tokens": {"U": {"idx": 0, "condition": MARKET, "open": 2, "close": 12},
                   "D": {"idx": 1, "condition": MARKET, "open": 1, "close": 1}},
        "reconciliation": {"U": {"start": 2, "end": 12, "residual": 0, "log_residual": 0},
                           "D": {"start": 1, "end": 1, "residual": 0, "log_residual": 0}},
    }))
    return data, orders, ledger, ref


def run(root, **options):
    return export(*inputs(root, **options), W, T0, T0 + 45, "5m")


def test_export_uses_pre_execution_state_and_prior_verified_positions(tmp_path):
    rows, report = run(tmp_path / "a")
    changed, _ = run(tmp_path / "b", late_price=.81)
    assert rows == changed
    assert report["status"] == "OBSERVED_DECISIONS"
    assert report["counts"] == {"take": 1, "none": 1}
    take, no_take = rows
    assert take["cutoff_tw_ns"] == (T0 + 20) * NS
    assert take["receipt_tw_ns"] == (T0 + 21) * NS
    assert take["up_ask_tw_ns"] == (T0 + 19) * NS
    assert take["tte_s"] == 280
    assert (take["bb_c"], take["ba_c"], take["depth_bid"], take["depth_ask"]) == (41, 42, 120, 100)
    assert (take["up_side"], take["size"], take["inv"]) == ("bid", 10, 1)
    assert take["px_c"] == pytest.approx(42)
    assert 0 < take["fair_c"] < 100
    assert no_take["cutoff_tw_ns"] == (T0 + 30) * NS and no_take["inv"] == 11
    assert no_take["label_source"] == "no_reconciled_wallet_execution_in_fixed_cell"
    assert not {"px_c", "size", "receipt_tw_ns"} & no_take.keys()


@pytest.mark.parametrize(("options", "reason"), [
    ({"delay_ms": 3_001}, "unbounded_fill_receipt_delay"),
    ({"extra_movement": True}, "ledger_signed_fill_mismatch_or_nonfill_movement"),
    ({"stale": True}, "missing_stale_or_crossed_book"),
    ({"gap": True}, "missing_stale_or_crossed_book"),
    ({"no_opening": True}, "missing_causal_fair"),
    ({"bounded_to": 45}, "insufficient_chain_right_margin"),
])
def test_explicit_abstention_on_missing_evidence(tmp_path, options, reason):
    rows, report = run(tmp_path / "case", **options)
    assert report["status"] == "ABSTAIN"
    assert not any(r["action"] == "take" for r in rows)
    assert reason in report["window_gaps"].values() or report["excluded_cuts"].get(reason)


def test_cli_records_input_and_output_hashes_and_refuses_overwrite(tmp_path):
    data, orders, ledger, ref = inputs(tmp_path / "source")
    out = tmp_path / "out"
    argv = ["--data", str(data), "--orders", str(orders), "--absorption", str(ledger), "--brti", str(ref),
            "--wallet", W, "--from", str(T0), "--to", str(T0 + 45), "--horizon", "5m", "--out", str(out)]
    assert main(argv) == 0
    evidence = orjson.loads((out / "evidence.json").read_bytes())
    assert evidence["decisions_sha256"] == digest(out / "decisions.jsonl")
    assert {"path": str(ledger), "sha256": digest(ledger)} in evidence["inputs"]
    assert len(evidence["book_sources"]) == 1
    with pytest.raises(FileExistsError):
        main(argv)
