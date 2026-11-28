import pytest

from doppel.lab.layer5b_fair import FairWitnesses
from doppel.twin.tape import NS

START = 1_800_000_000


def poll(tw, samples):
    return {"stream": "ref_brti_live", "tw": tw,
            "meta": {"status": 200, "new": [[s * 1000, value] for s, value in samples]}}


def test_fair_uses_published_opening_and_latest_strictly_prior_witness():
    tick = START * NS
    history = [(s, 100_000 + s % 14) for s in range(START - 302, START)]
    updates = [poll(tick + NS // 2, history), poll(tick + 2 * NS, [(START, 100_040)]),
               poll(tick + 3 * NS, [(START + 1, 99_999)])]
    witness = FairWitnesses(updates)
    at = lambda cutoff: witness.at(cutoff, START, START + 300)
    assert at(tick + 2 * NS) is None
    before_later_packet = at(tick + 2 * NS + NS // 4)
    assert before_later_packet and before_later_packet["tw_ns"] == tick + 2 * NS
    assert .5 < before_later_packet["p_up"] < 1
    assert at(tick + 3 * NS) is None
    assert at(tick + 3 * NS + NS // 4)["tw_ns"] == tick + 3 * NS
    with pytest.raises(ValueError, match="ordered"):
        at(tick + 2 * NS)


def test_fair_refuses_holes_and_future_publication():
    tick = START * NS
    points = [(s, 100_000 + s % 14) for s in range(START - 300, START + 1) if s != START - 63]
    witness = FairWitnesses([poll(tick + NS, points)])
    assert witness.at(tick + 2 * NS, START, START + 300) is None
    with pytest.raises(ValueError, match="publication timestamp"):
        FairWitnesses([poll(tick, [(START + 1, 100_000)])])
