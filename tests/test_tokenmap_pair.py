import asyncio
from unittest.mock import AsyncMock

import orjson
import pytest

from doppel.lab import ledger
from doppel.lab.absorb import load_pairs


CONDITION = "0x" + "ab" * 32
END = "2026-11-26T09:05:00Z"
SLUG = "btc-updown-5m-1795683600"


def market(ids=("100", "101"), *, condition=CONDITION, prices=("1", "0"), closed=True):
    return {"conditionId": condition, "slug": SLUG, "endDate": END, "closed": closed,
            "umaResolutionStatus": "resolved" if closed else None, "outcomes": '["Up","Down"]',
            "clobTokenIds": orjson.dumps(ids).decode(), "outcomePrices": orjson.dumps(prices).decode()}


def token(tok, idx, payout):
    return {"token": tok, "idx": idx, "payout": payout, "status": "resolved", "slug": SLUG,
            "end": END, "condition": CONDITION}


def stub_venue(monkeypatch, closed, open_markets=()):
    calls = []

    class Response:
        status = 200

        def __init__(self, rows):
            self.rows = rows

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def read(self):
            return orjson.dumps(self.rows)

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def get(self, url, *, params):
            assert url == ledger.GAMMA
            calls.append(params)
            return Response(closed if dict(params)["closed"] == "true" else open_markets)

    monkeypatch.setattr(ledger.aiohttp, "TCPConnector", lambda **kw: None)
    monkeypatch.setattr(ledger.aiohttp, "ClientSession", lambda **kw: Session())
    monkeypatch.setattr(ledger.asyncio, "sleep", AsyncMock())
    return calls


@pytest.mark.parametrize("old_cache", [False, True])
def test_single_traded_token_supplies_untouched_sibling_to_absorption(tmp_path, monkeypatch, old_cache):
    path = tmp_path / "tokens.jsonl"
    if old_cache:
        path.write_bytes(orjson.dumps(token("100", 0, 1.0)))  # historical JSONL without trailing LF
    unrelated = market(("900", "901"), condition="0x" + "cd" * 32)
    calls = stub_venue(monkeypatch, [unrelated, market()])
    tm = ledger.TokenMap(path)
    asyncio.run(tm.ensure({"100"}))
    assert tm.m == {"100": token("100", 0, 1.0), "101": token("101", 1, 0.0)}
    assert load_pairs(path) == {"100": (CONDITION[2:], 0), "101": (CONDITION[2:], 1)}
    assert len(path.read_bytes().splitlines()) == 2
    # The completed cache survives a new process and does not query again for either side.
    complete = ledger.TokenMap(path)
    asyncio.run(complete.ensure({"100", "101"}))
    assert len(calls) == 2 and all(("clob_token_ids", "100") in c for c in calls)


def test_conflict_or_open_fallback_cannot_rewrite_resolved_cache(tmp_path, monkeypatch):
    path = tmp_path / "tokens.jsonl"
    original = orjson.dumps(token("100", 0, 1.0)) + b"\n"
    path.write_bytes(original)
    stub_venue(monkeypatch, [market(prices=("0", "1"))])
    with pytest.raises(ValueError, match="conflicting venue metadata"):
        asyncio.run(ledger.TokenMap(path).ensure({"100"}))
    assert path.read_bytes() == original
    stub_venue(monkeypatch, [], [])
    asyncio.run(ledger.TokenMap(path).ensure({"100"}))
    assert path.read_bytes() == original
    stub_venue(monkeypatch, [market()], [market(closed=False)])
    asyncio.run(ledger.TokenMap(path).ensure({"100"}))
    assert ledger.TokenMap(path).m["101"]["status"] == "resolved"


def test_duplicate_side_conflict_in_other_writer_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "tokens.jsonl"
    tm = ledger.TokenMap(path)
    stale = ledger.TokenMap(path)
    tm._save([token("101", 1, 0.0)])
    original = path.read_bytes()
    stub_venue(monkeypatch, [market(("100", "102"))])
    with pytest.raises(ValueError, match="contradictory tokens"):
        asyncio.run(stale.ensure({"100"}))
    assert path.read_bytes() == original


def test_malformed_binary_resolution_cannot_supply_a_sibling(tmp_path, monkeypatch):
    path = tmp_path / "tokens.jsonl"
    stub_venue(monkeypatch, [market(prices=["1", "1"])])
    with pytest.raises(ValueError, match="binary payouts"):
        asyncio.run(ledger.TokenMap(path).ensure({"100"}))
    assert not path.exists()
