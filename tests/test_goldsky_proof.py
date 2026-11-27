from urllib.parse import urlencode

import orjson
import pytest

from doppel.lab.goldsky import BASE, pagination_state
from doppel.lab.goldsky_proof import recover
from doppel.tape.segment import SegmentWriter


@pytest.fixture
def captured(tmp_path):
    cohort = tmp_path / "cohort.json"
    cohort.write_bytes(orjson.dumps({"candidates": ["wallet"], "controls": ["other", "absent"]}))
    tape = tmp_path / "tape"
    writer = SegmentWriter(tape, "goldsky_activity")
    def page(wallet, current, nxt, *, row_wallet=None, filter_=None, sent=1795715999 * 10**9):
        params = {"address": wallet, "start_time": 10, "end_time": 20, "limit": 1}
        if current is not None:
            params["cursor"] = current
        if filter_:
            params.update(filter_)
        body = {"data": [{"address": row_wallet or wallet, "block_timestamp": 14}], "next_cursor": nxt}
        writer.append(orjson.dumps(body).decode(),
                      meta={"url": BASE + "?" + urlencode(params), "status": 200, "job": "cohort-wallet", "sent_tw": sent},
                      tw=1795716000 * 10**9)
    def run():
        writer.close()
        return recover(tape, cohort, 10, 20, ["20261126/18.dtape.zst"], tmp_path / "out")
    return page, run, tmp_path / "out"


def test_pagination_recovery_attests_only_contiguous_full_wallet_queries(captured):
    page, run, out = captured
    page("wallet", None, "c1")
    page("wallet", "c1", None)
    page("other", None, "c1")
    page("absent", None, None, filter_={"token_id": "filtered"})
    r = run()
    assert (r["complete"], r["incomplete"], r["missing"], r["pages"], r["rows"]) == (1, 1, 1, 3, 3)
    st = pagination_state(out / "pagination.jsonl")
    assert st["wallet", 10, 20]["complete"]
    assert st["other", 10, 20]["cursor"] == "c1" and not st["other", 10, 20]["complete"]
    with pytest.raises(FileExistsError):
        run()


@pytest.mark.parametrize("failure", ["gap", "cycle", "row"])
def test_invalid_cursor_or_query_rows_publishes_no_proof(captured, failure):
    page, run, out = captured
    page("wallet", None, "c1")
    page("wallet", "wrong" if failure == "gap" else "c1", "c1" if failure == "cycle" else None,
         row_wallet="wrong" if failure == "row" else None)
    with pytest.raises(ValueError):
        run()
    assert not out.exists()

def test_rejects_unsealed_segment_even_if_original_pages_are_readable(captured, tmp_path):
    page, run, out = captured
    page("wallet", None, None)
    run()
    source = tmp_path / "tape"
    segment = "20261126/18.dtape.zst"
    with (source / "goldsky_activity" / segment).open("ab") as fh:
        fh.write(b"unsealed suffix")
    with pytest.raises(ValueError, match="unsealed"):
        recover(source, tmp_path / "cohort.json", 10, 20, [segment], out.parent / "new")
    assert not (out.parent / "new").exists()


@pytest.mark.parametrize("when", [19 * 10**9, 1795716001 * 10**9, None])
def test_missing_premature_or_reversed_capture_time_publishes_no_proof(captured, when):
    page, run, out = captured
    page("wallet", None, None, sent=when)
    with pytest.raises(ValueError, match="page request must follow"):
        run()
    assert not out.exists()
