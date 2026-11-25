import orjson
import pytest

import doppel.lab.census as census
from doppel.lab.fillstore import compact, iter_wallet_fills, wallet_tokens
from doppel.lab.ledger import load_fills
from doppel.tape.segment import SegmentWriter

H = 3600 * 10**9
T0 = 1795046400 * 10**9  # a UTC midnight, ns


def _row(i, addr, tok="T1", side="BUY", sh=10, usd=4.8, fee=0.0, bn=None, ev="TRADE", **kw):
    return {"id": f"r{i}", "address": addr, "event_type": ev, "token_id": tok, "side": side,
            "amount_shares": sh, "amount_usdc": usd, "fee": fee, "block_timestamp": 1795046400 + i,
            "block_number": i if bn is None else bn, "order_type": "maker", "price": 0.48,
            "transaction_hash": f"0x{i}", **kw}


def _page(w, rows, hour, status=200, job="cohort-a"):
    w.append(orjson.dumps({"data": rows}).decode(), tw=T0 + hour * H,
             meta={"job": job, "status": status, "rows": len(rows)})


def _tape(tmp_path):
    w = SegmentWriter(tmp_path, "goldsky_activity")
    # hour 0: r7 first appears in a failed page and a non-matching job; must still be kept later
    _page(w, [_row(7, "0xAA", usd=9.9)], 0, status=500)
    _page(w, [_row(7, "0xAA", usd=8.8)], 0, job="other-x")
    _page(w, [_row(1, "0xAA"), _row(2, "0xaa", sh=3.5, usd=1.75, fee=None, bn=5),
              _row(3, "0xBB", tok="T2", side="SELL"), _row(9, "0xAA", ev="REDEEM")], 0)
    # duplicate of r1 in a later page of the same segment (first occurrence wins)
    _page(w, [_row(1, "0xAA", usd=999.0), _row(4, "0xAA", bn=5, fee=0.02)], 0, job="cohort-b")
    # hour 1: cross-segment duplicates (r3 with a different value: first occurrence wins), tie on
    # block_number 5 between r2, r4 and r5, and r7 in a valid page
    _page(w, [_row(3, "0xBB", tok="T2", usd=123.0), _row(5, "0xAA", bn=5),
              _row(7, "0xAA", usd=7.7), _row(8, "0xCC", tok="T3", sh=1, usd=1)], 1)
    w.close()
    return w


def _store_rows(store, wallets=None):
    return list(iter_wallet_fills(store, wallets))


def test_iter_matches_load_fills(tmp_path):
    _tape(tmp_path)
    store = tmp_path / "fills"
    res = compact(tmp_path, store)
    assert res["compacted"] == 2 and res["dropped"] == 0
    old = load_fills(tmp_path)
    assert _store_rows(store) == sorted(old.items())
    sub = {"0xaa", "0xcc", "0xnobody"}
    assert _store_rows(store, sub) == sorted(load_fills(tmp_path, sub).items())
    rows = dict(_store_rows(store))
    aa = rows["0xaa"]
    assert [r["id"] for r in aa] == ["r1", "r2", "r4", "r5", "r7"]  # (block_number, id) order
    assert aa[0]["amount_usdc"] == 4.8 and aa[-1]["amount_usdc"] == 7.7
    assert aa[1]["fee"] is None and list(aa[0]) == list(old["0xaa"][0])  # same keys, same order
    assert rows["0xbb"][0]["amount_usdc"] == 4.8
    assert wallet_tokens(store, {"0xbb", "0xcc"}) == {"T2", "T3"}


def test_compaction_is_incremental(tmp_path):
    _tape(tmp_path)
    store = tmp_path / "fills"
    first = compact(tmp_path, store)
    assert first["rows"] == first["store_rows"]
    again = compact(tmp_path, store)
    assert again["compacted"] == 0 and again["rows"] == 0
    assert again["store_rows"] == first["store_rows"]
    w = SegmentWriter(tmp_path, "goldsky_activity")
    _page(w, [_row(10, "0xDD"), _row(1, "0xAA")], 2)  # a new segment: 2 rows (one a duplicate)
    w.close()
    third = compact(tmp_path, store)
    assert third["compacted"] == 1 and third["rows"] == 2
    assert third["store_rows"] == first["store_rows"] + 2
    assert _store_rows(store) == sorted(load_fills(tmp_path).items())
    # a segment that grows (current hour still being written) is recompacted, not doubled
    w = SegmentWriter(tmp_path, "goldsky_activity")
    _page(w, [_row(11, "0xDD")], 2)
    w.close()
    fourth = compact(tmp_path, store)
    assert fourth["compacted"] == 1 and fourth["rows"] == 3
    assert fourth["store_rows"] == first["store_rows"] + 3
    assert _store_rows(store) == sorted(load_fills(tmp_path).items())


def test_job_prefix_filter_and_mismatch(tmp_path):
    w = SegmentWriter(tmp_path, "goldsky_activity")
    _page(w, [_row(1, "0xAA")], 0, job="val-x")
    _page(w, [_row(2, "0xAA")], 0, job="cohort-1")
    w.close()
    store = tmp_path / "fills"
    compact(tmp_path, store)
    assert [r["id"] for _, rows in _store_rows(store) for r in rows] == ["r2"]
    with pytest.raises(ValueError, match="job_prefix"):
        compact(tmp_path, store, job_prefix="val-")
    other = tmp_path / "val"
    compact(tmp_path, other, job_prefix="val-")
    assert [r["id"] for _, rows in _store_rows(other) for r in rows] == ["r1"]
    assert _store_rows(other) == sorted(load_fills(tmp_path, job_prefix="val-").items())


def test_type_mismatch_raises_not_coerces(tmp_path):
    w = SegmentWriter(tmp_path, "goldsky_activity")
    _page(w, [_row(1, "0xAA", usd="4.8")], 0)
    w.close()
    with pytest.raises(ValueError, match="amount_usdc"):
        compact(tmp_path, tmp_path / "fills")
    assert not list((tmp_path / "fills").glob("*.parquet*"))


def test_census_run_uses_store(tmp_path, monkeypatch):
    _tape(tmp_path)
    cohort = tmp_path / "cohort.json"
    cohort.write_bytes(orjson.dumps({"sha256": "ab" * 32, "candidates": ["0xaa"],
                                     "controls": ["0xcc", "0xempty"]}))
    ensured = []

    async def ensure(self, tokens, **kw):
        ensured.append(set(tokens))

    async def points(wallet, interval="1w"):
        return []

    monkeypatch.setattr(census.TokenMap, "ensure", ensure)
    monkeypatch.setattr(census, "api_points", points)
    out = census.run(tmp_path, tmp_path / "lab", cohort)
    assert ensured == [{"T1", "T3"}]
    assert sorted(out["wallets"]) == ["0xaa", "0xcc"]  # wallet with no fills is skipped
    assert (tmp_path / "lab" / "fills" / "state.json").exists()


def test_ledger_main_uses_store(tmp_path, monkeypatch):
    import doppel.lab.ledger as ledger
    _tape(tmp_path)
    cohort = tmp_path / "cohort.json"
    cohort.write_bytes(orjson.dumps({"sha256": "cd" * 32, "candidates": ["0xaa"],
                                     "controls": ["0xbb", "0xempty"]}))

    async def ensure(self, tokens, **kw):
        assert set(tokens) == {"T1", "T2"}

    async def points(wallet, interval="1w"):
        return []

    monkeypatch.setattr(ledger.TokenMap, "ensure", ensure)
    monkeypatch.setattr(ledger, "api_points", points)
    lab = tmp_path / "lab"
    assert ledger.main(["--data", str(tmp_path), "--lab", str(lab), "--cohort", str(cohort)]) == 0
    rep = orjson.loads(next((lab / "g0").glob("g0-*.json")).read_bytes())
    assert {w: v["fills"] for w, v in rep["wallets"].items()} == {"0xaa": 5, "0xbb": 1}
