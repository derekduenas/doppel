import orjson
import pytest

from doppel.lab import absorb
from test_absorb import AD, COND, NO, O, U, W, YES, FakeChain, _run


def test_partial_wallet_pairing_withholds_whole_wallet_share(tmp_path):
    c = FakeChain()
    c.cache = tmp_path / "rpc.jsonl"
    c.open[(absorb.PUSD, W, "cash")] = 100 * U
    c.single("0x02", 19, O, W, "900", U)
    fills = [c.fill("0x03", 20, "BUY", YES, 5, 0.4)]
    r = _run(c, fills, pairs={YES: (COND, 0), NO: (COND, 1)})
    assert r["status"] == "reconciled"
    p = r["pairing"]
    assert p["coverage"] == "incomplete" and p["tokens_without_pair"] == ["900"]
    assert p["completing_share_of_buys"] is None
    assert p["markets"][COND]["directional"] == 5
    assert p["interpolated_event_blocks"] == 1


def test_simultaneous_split_has_no_directional_peak(tmp_path):
    c = FakeChain()
    c.cache = tmp_path / "rpc.jsonl"
    c.open[(absorb.PUSD, W, "cash")] = 100 * U
    c.cash("0x10", 20, W, absorb.PUSD, 10 * U)
    c.batch("0x10", 20, AD, W, [YES, NO], [10 * U, 10 * U])
    c.ctf_event("0x10", 20, "split", AD, 10 * U)
    r = _run(c, [])
    assert r["pairing"]["coverage"] == "complete"
    m = r["pairing"]["markets"][COND]
    assert m["max_one_sided"] == 0
    assert m["paired_share_s"] == (1201 - 1040) * 10


def test_conflicting_cached_outcome_identity_rejected(tmp_path):
    path = tmp_path / "tokens.jsonl"
    rows = [{"token": t, "idx": 0, "condition": COND} for t in (YES, NO)]
    path.write_bytes(b"".join(orjson.dumps(row) + b"\n" for row in rows))
    with pytest.raises(ValueError, match="conflicting"):
        absorb.load_pairs(path)
