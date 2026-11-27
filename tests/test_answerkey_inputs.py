import hashlib
from pathlib import Path

import orjson
import pytest

from doppel.lab.answerkey_inputs import preflight_inputs
from doppel.tape.segment import SegmentWriter

START = 1795672800
STREAM = "clob_btc_5m"
NS = 10**9


def inputs(tmp_path, *, skip_quote=None, wrong_condition=False):
    writer = SegmentWriter(tmp_path, STREAM)
    rows = []
    for k in range(2):
        t = START + k * 300
        condition = f"0x{k:064x}"
        for idx, token in enumerate((f"up{k}", f"down{k}")):
            rows.append({"slug": f"btc-updown-5m-{t}", "condition": condition, "status": "resolved",
                         "idx": idx, "token": token, "payout": 1 - idx})
            if token == skip_quote:
                continue
            event = {"market": "wrong" if wrong_condition and k == 1 else condition}
            if idx == 0:
                event.update(event_type="book", asset_id=token, bids=[], asks=[])
            else:
                event.update(event_type="price_change", price_changes=[{"asset_id": token}])
            writer.append(orjson.dumps([event]).decode(), tw=(t + 1) * NS)
    writer.close()
    tokens = tmp_path / "tokens.jsonl"
    tokens.write_bytes(b"".join(orjson.dumps(r) + b"\n" for r in rows))
    return tokens


def check(root: Path, tokens: Path):
    return preflight_inputs(root, tokens, STREAM, START, START + 300, START + 600)


def test_coverage_and_pinned_inputs_in_both_halves(tmp_path):
    tokens = inputs(tmp_path)
    report = check(tmp_path, tokens)
    assert report["train"] == report["holdout"] == {"mapped": 1, "resolved": 1, "expected": 1, "book_pairs": 1}
    assert report["tokens_sha256"] == hashlib.sha256(tokens.read_bytes()).hexdigest()
    name = "20261126/06.dtape.zst"
    assert report["segment_sha256"] == {name: hashlib.sha256((tmp_path / STREAM / name).read_bytes()).hexdigest()}


@pytest.mark.parametrize("case", ["empty", "unresolved", "contradictory", "mismatched-condition", "duplicate-side"])
def test_bad_maps_fail_before_dense_tape_is_opened(tmp_path, case):
    tokens = inputs(tmp_path)
    rows = [orjson.loads(line) for line in tokens.read_bytes().splitlines()]
    if case == "empty":
        rows = []
    elif case == "unresolved":
        rows[-1].update(status="open", payout=None)
    elif case == "contradictory":
        rows[-1].update(payout=1)
    elif case == "mismatched-condition":
        rows[-1].update(condition="different")
    else:
        rows.append(rows[-1] | {"token": "duplicate"})
    tokens.write_bytes(b"\n".join(orjson.dumps(r) for r in rows))
    with pytest.raises(ValueError, match="token pairs|ambiguous token side"):
        check(tmp_path / "no-tape-here", tokens)


@pytest.mark.parametrize("skip,wrong", [("down1", False), (None, True)])
def test_requires_both_tokens_in_their_own_market_and_window(tmp_path, skip, wrong):
    tokens = inputs(tmp_path, skip_quote=skip, wrong_condition=wrong)
    with pytest.raises(ValueError, match="book evidence missing"):
        check(tmp_path, tokens)


def test_preflight_rejects_segment_different_from_seal(tmp_path):
    tokens = inputs(tmp_path)
    p = next((tmp_path / STREAM).glob("*/*.dtape.zst"))
    with p.open("ab") as out:
        out.write(b"x")
    with pytest.raises(ValueError, match="differs from manifest"):
        check(tmp_path, tokens)
