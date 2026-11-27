from copy import deepcopy
from urllib.parse import urlencode

import orjson
import pytest

from doppel.lab.fillstore import iter_wallet_fills
from doppel.lab.global_activity_proof import project
from doppel.lab.goldsky import BASE, pagination_state, trade_binding
from doppel.tape.segment import SegmentWriter

W, O, IDLE = ["0x" + c * 40 for c in "abc"]
NAME = "20261126/18.dtape.zst"
JOB = "global-test"


def row(w, i, stamp=12, kind="TRADE"):
    r = {"event_type": kind, "address": w, "block_timestamp": stamp,
         "transaction_hash": "0x" + "d" * 64, "id": f"log_0x{'e' * 64}_{i}"}
    if kind == "TRADE":
        r.update(token_id="123", side="BUY", order_type="maker", order_id="f" * 64,
                 amount_shares=20, amount_usdc=4, fee=0, price=0.2,
                 block_number=33, counterparty_address=None)
    return r


@pytest.fixture
def captured(tmp_path):
    tape, out = tmp_path / "tape", tmp_path / "out"
    writer = SegmentWriter(tape, "goldsky_activity")

    def page(rows, current=None, nxt=None, params=None, body=None, status=200, sent=25 * 10**9):
        query = {"start_time": 10, "end_time": 20, "limit": 3}
        if current is not None:
            query["cursor"] = current
        query.update(params or {})
        payload = {"data": rows, "next_cursor": nxt, "limit": 3} if body is None else body
        writer.append(orjson.dumps(payload), tw=1795716000 * 10**9,
                      meta={"job": JOB, "url": BASE + "?" + urlencode(query), "status": status, "sent_tw": sent})

    def run():
        writer.close()
        return project(tape, 10, 20, JOB, [NAME], [W, IDLE], out)

    return page, run, out


def test_projects_bound_parent_interval_and_proves_idle_wallet(captured):
    page, run, out = captured
    t1, t2 = row(W, 1), row(W, 4, 19)
    page([t1, row(O, 2), row(W, 3, 15, "TRANSFER")], nxt="opaque+/=")
    page([t2, row(W, 5, 20)], current="opaque+/=")
    result = run()
    assert (result["source_rows"], result["source_pages"]) == (5, 2)
    states = pagination_state(out / "pagination.jsonl")
    assert states[W, 10, 20]["trade_binding"] == trade_binding([t2, t1])
    assert states[W, 10, 20]["rows"] == 3
    assert states[IDLE, 10, 20]["trade_binding"] == trade_binding([])
    assert states[IDLE, 10, 20]["complete"]
    fills = dict(iter_wallet_fills(out / "fills"))
    assert list(fills) == [W]
    assert [x["id"] for x in fills[W]] == [t1["id"], t2["id"]]
    assert trade_binding(fills[W]) == states[W, 10, 20]["trade_binding"]
    with pytest.raises(FileExistsError):
        run()


@pytest.mark.parametrize("case", ["filtered", "gap", "cycle", "incomplete", "missing_cursor",
                                  "after_done", "duplicate_outsider", "bad_owner", "bad_time",
                                  "bad_kind", "bad_id", "bad_token", "failed", "body_limit", "premature", "no_clock"])
def test_rejects_broken_archive_including_unselected_population(captured, case):
    page, run, out = captured
    r = row(O, 1)
    nr = row(O, 2, kind="TRANSFER")
    if case == "filtered":
        page([], params={"side": "BUY"})
    elif case in ("gap", "cycle", "incomplete"):
        page([r], nxt="next")
        if case != "incomplete":
            page([nr], current="wrong" if case == "gap" else "next",
                 nxt="next" if case == "cycle" else None)
    elif case == "missing_cursor":
        page([], body={"data": [], "limit": 3})
    elif case == "after_done":
        page([r])
        page([nr])
    elif case == "duplicate_outsider":
        page([r], nxt="next")
        page([deepcopy(r)], current="next")
    elif case == "body_limit":
        page([], body={"data": [], "next_cursor": None, "limit": 9})
    elif case in ("premature", "no_clock"):
        page([], sent=19 * 10**9 if case == "premature" else None)
    elif case == "failed":
        page([], status=403)
    else:
        key, value = {"bad_owner": ("address", "unknown"), "bad_time": ("block_timestamp", 9),
                      "bad_kind": ("event_type", "Trade"), "bad_id": ("id", ""),
                      "bad_token": ("token_id", "-2")}[case]
        bad = r if case == "bad_token" else nr
        bad[key] = value
        page([bad])
    with pytest.raises(ValueError):
        run()
    assert not out.exists()


def test_changed_seal_prevents_publication(captured, tmp_path):
    page, run, out = captured
    page([])
    run()
    source = tmp_path / "tape"
    with (source / "goldsky_activity" / NAME).open("ab") as f:
        f.write(b"suffix")
    with pytest.raises(ValueError, match="unsealed"):
        project(source, 10, 20, JOB, [NAME], [W], out.parent / "second")
    assert not (out.parent / "second").exists()
