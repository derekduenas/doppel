"""Regression tests for the observation denominator and its independence from the wallet."""
import orjson
import pytest

from doppel.lab import rulefit
from doppel.lab.market_opportunities import observable_identity, public_listings, study_panel
from doppel.tape.segment import SegmentWriter
from test_rulefit import NS, T0, W, book, slug, world, write


def _rtds(writer, second, wallet, tx, token="U0", window=0):
    payload = {"eventSlug": slug(window), "asset": token, "side": "BUY", "outcomeIndex": 0,
               "timestamp": T0 - 1000, "proxyWallet": wallet, "transactionHash": tx, "price": 0.5, "size": 1}
    writer.append(orjson.dumps({"topic": "activity", "type": "trades", "payload": payload}), tw=(T0 + second) * NS)


def test_independent_first_receipt_and_untimed_map(tmp_path):
    first = SegmentWriter(tmp_path, "rtds_a")
    _rtds(first, -20, W.upper(), "own")
    _rtds(first, -10, "peer", "SHARED")
    _rtds(first, 20, "peer", "other")
    first.close()
    second = SegmentWriter(tmp_path, "rtds_b")
    _rtds(second, 10, "peer", "other")
    second.close()
    tokens, receipt = public_listings(tmp_path, T0 - 30, T0 + 30, exclude_wallets=[W], exclude_txs=["shared"],
                                      require_seals=True)
    assert len(receipt["sources"]) == 3
    assert tokens["U0"]["witness_source"] == "rtds_b" and tokens["U0"]["witness_seq"] == 1
    assert not observable_identity(tokens["U0"], (T0 + 10) * NS)
    assert observable_identity(tokens["U0"], (T0 + 10) * NS + 1)
    path = tmp_path / "later.jsonl"
    path.write_bytes(orjson.dumps({"token": "U1", "slug": slug(1), "idx": 0, "status": "resolved", "payout": 1,
                                  "known_tw_ns": (T0 - 100) * NS, "witness_source": "answerkey_config"}))
    info = rulefit.token_map(tmp_path, T0, T0 + 30, [path])["U1"]
    assert info["known_tw_ns"] is None and "payout" not in info


def test_sealed_panel_counts_quiet_cells_and_preserves_missing_windows(tmp_path):
    rtds = SegmentWriter(tmp_path, "rtds_a")
    _rtds(rtds, -2, "peer", "before")
    _rtds(rtds, 310, "peer", "second", token="U1", window=1)
    _rtds(rtds, 601, W, "mine", token="U2", window=2)
    rtds.close()
    clob = SegmentWriter(tmp_path, "clob_btc_5m")
    for t in range(-1, 901):
        k = min(2, max(0, t // 300))
        ms, msg = book(t * 1000, f"U{k}", [(0.47, 3)], [(0.53, 2)], f"m{k}")
        clob.append(orjson.dumps(msg), tw=T0 * NS + ms * 10**6)
    clob.close()
    activity = {W: {slug(1): 2}, "control": {}, "incomplete": None}
    result = study_panel(tmp_path, T0, T0 + 900, [("btc", "5m")], activity)
    windows = result["windows"]
    assert [r["scheduled_cells"] for r in windows] == [30, 30, 30]
    assert [r["eligible_cells"] for r in windows] == [30, 28, 0]
    assert windows[0]["wallets"][W]["observation"] == "no_observed_fill_on_covered_grid"
    assert windows[1]["wallets"]["control"]["observation"] == "no_observed_fill_in_partially_observed_market"
    assert windows[2]["wallets"]["control"]["observation"] == "no_observed_fill_market_unobservable"
    assert windows[2]["wallets"]["incomplete"]["reconciled_fills"] is None
    assert windows[1]["wallets"][W]["reconciled_fills"] == 2
    assert all(s["sha256"] for s in result["sources"]["sources"])
    assert result["cells"][31]["cell_start"] == T0 + 310 and not result["cells"][31]["identity_observed"]
    seal = tmp_path / "clob_btc_5m" / "manifest.jsonl"
    seal.write_bytes(b"")
    with pytest.raises(ValueError, match="unsealed"):
        study_panel(tmp_path, T0, T0 + 900, [("btc", "5m")], activity)


def test_rulefit_unlocated_fill_prevents_negative_market_labels(tmp_path):
    frames, orders, tape, _ = world(windows=2)
    missing_order = tape[0]["order_id"]
    write(tmp_path, frames, [o for o in orders if o["order_id"] != missing_order], tape, windows=2)
    table = rulefit.decision_table(tmp_path / "tape", tmp_path / "om", tmp_path / "ordertape.jsonl",
                                   W, T0, T0 + 600, [tmp_path / "tokens.jsonl"])
    assert table["unlocated_maker_slugs"] == {slug(0)}
    assert {r[0] for r in rulefit.grid_cells(table)} == {slug(0), slug(1)}
    assert {r[0] for r in rulefit.presence_cells(table)} == {slug(1)}
    assert rulefit.fit_participation(table)["scheduled_cells"] == 60

    # Retrospective labels can map economic activity. They establish no prior listing witness.
    metadata = tmp_path / "tokens.jsonl"
    # Even a semantically identical rewrite breaks the controlled source hash.
    metadata.write_bytes(metadata.read_bytes() + b"\n")
    changed = rulefit.token_map(tmp_path / "tape", T0, T0 + 600, [metadata])
    assert changed and all(info["known_tw_ns"] is None for info in changed.values())
    later = [orjson.loads(line) for line in metadata.read_bytes().splitlines() if line.strip()]
    for row in later:
        row.pop("known_tw_ns")
        row.pop("witness_source")
    metadata.write_bytes(b"\n".join(map(orjson.dumps, later)))
    blind = rulefit.decision_table(tmp_path / "tape", tmp_path / "om", tmp_path / "ordertape.jsonl",
                                   W, T0, T0 + 600, [metadata])
    assert not blind["rows"] and not rulefit.grid_cells(blind)
    assert len(blind["opportunity_panel"]) == 60 and blind["stats"]["order_identity_not_known"] == len(orders) - 1


def test_presence_stays_in_the_local_placement_receipt_cell():
    schedule = [{"slug": slug(0), "window_start": T0, "window_end": T0 + 300, "mid_c": 50,
                 "cell_start": T0 + offset, "eligible": True} for offset in (0, 10, 20)]
    spec = {"kind": "order", "placed_ms": T0 * 1000 + 9000, "placed_tw": (T0 + 12) * NS,
            "om": {"placement_candidates": 1, "rest_min_ms": 25_000}, "row": {"window": slug(0), "up_side": "ask"}}
    table = {"opportunity_panel": schedule, "specs": [spec]}
    assert [row[4] for row in rulefit.presence_cells(table)] == [set(), {"ask"}, set()]
