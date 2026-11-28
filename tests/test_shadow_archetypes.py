from dataclasses import replace

import pytest

from doppel.strategy.genome import Condition, Genome, MakerRule, Source, TakerRule, Universe
from doppel.strategy.shadow_archetypes import archetype, maker_evidence, requires_offshore


REPLAY_SHA = "a" * 64
INPUT_SHA = "b" * 64
DIGEST = "c" * 64


def maker(**evidence):
    return Genome(name="shadow archetype fixture", universe=Universe(venue="pmus"), mode="maker",
                  source=Source(kind="manual"), maker=MakerRule(), evidence=evidence)


def taker(trigger):
    return replace(maker(), mode="taker", maker=None, evidence={"taker_trigger_source": trigger},
                   taker=(TakerRule(side="up", when=(Condition("edge_buy_up_c", ">", 1),), size=25),))


@pytest.mark.parametrize("trigger", ("fair", "lead"))
def test_taker_archetype_requires_registered_trigger_and_offshore(trigger):
    g = taker(trigger)
    assert archetype(g) == f"taker_{trigger}"
    assert requires_offshore(g)
    with pytest.raises(ValueError, match="contradicts"):
        archetype(replace(g, evidence={**g.evidence, "archetype": "maker"}))
    with pytest.raises(ValueError, match="contradicts"):
        archetype(replace(g, evidence={}))


def test_makers_need_offshore_only_for_registered_guards():
    plain = maker(cancel_guard_sources=["brti", "pmus"])
    assert archetype(plain) == "maker"
    assert not requires_offshore(plain)
    guarded = maker(archetype="structural_twap", cancel_guard_sources=["lead_hyperliquid_l2"])
    assert archetype(guarded) == "structural_twap"
    assert requires_offshore(guarded)
    assert requires_offshore(replace(plain, maker=MakerRule(
        pause_when=(Condition("lead_binance_spot_l2", "<", 0.5),))))
    with pytest.raises(ValueError, match="cancel_guard_sources"):
        requires_offshore(maker(cancel_guard_sources=["BINANCE"]))
    with pytest.raises(ValueError, match="contradicts"):
        archetype(maker(archetype="taker_lead", taker_trigger_source="lead"))


@pytest.fixture
def receipt():
    g = maker()
    doc = {
        "schema": "doppel.shadow-maker/v1", "genome_id": g.id, "archetype": "maker",
        "replay_sha256": REPLAY_SHA, "input_sha256": INPUT_SHA,
        "queue": {
            "model": "pessimistic_l2_fifo", "source_sha256": DIGEST, "windows": 48,
            "quote_observations": 100, "known_queue_observations": 80, "fills": 60,
            "verified_fills": 60, "fill_windows": 47, "feed_gap_windows": 0,
            "min_trade_coverage": 0.9, "max_inferred_share": 0.4, "max_unidentified_share": 0.15,
        },
        "markouts": {
            "2s": {"value": 0.1, "observations": 50, "windows": 47, "source_sha256": DIGEST},
            "10s": {"value": 0.05, "observations": 47, "windows": 44, "source_sha256": DIGEST},
        },
        "stress": {"method": "delayed_cancel", "source_sha256": DIGEST, "plan_sha256": "e" * 64,
                   "windows": 48, "base_cancel_latency_ms": 250, "extra_cancel_latency_ms": 125,
                   "net_c_per_window": 0.5, "ci_low_c_per_window": 0.01},
    }
    return g, doc


@pytest.mark.parametrize("kind", ("maker", "structural_twap"))
def test_maker_receipt_binds_queue_marks_and_stress(receipt, kind):
    plain, doc = receipt
    g = replace(plain, evidence={"archetype": kind})
    doc.update(genome_id=g.id, archetype=kind)
    evidence = maker_evidence(g, doc, REPLAY_SHA, INPUT_SHA, expected_windows=48, base_cancel_latency_ms=250)
    assert evidence == {"queue": doc["queue"], "markout_2s": doc["markouts"]["2s"],
                        "markout_10s": doc["markouts"]["10s"], "stress": doc["stress"]}
    with pytest.raises(ValueError, match="bind"):
        maker_evidence(g, doc, REPLAY_SHA, "d" * 64)
    with pytest.raises(ValueError, match="window counts"):
        maker_evidence(g, doc, REPLAY_SHA, INPUT_SHA, expected_windows=49)
    with pytest.raises(ValueError, match="stress"):
        maker_evidence(g, doc, REPLAY_SHA, INPUT_SHA, base_cancel_latency_ms=251)


@pytest.mark.parametrize(
    "section,field,value,error",
    (
        ("queue", "model", "l1_as_l2", "queue"),
        ("queue", "verified_fills", 59, "queue"),
        ("queue", "known_queue_observations", 0, "queue"),
        ("queue", "feed_gap_windows", 1, "queue"),
        ("queue", "min_trade_coverage", 0.89, "L2 coverage"),
        ("queue", "max_inferred_share", 0.41, "L2 coverage"),
        ("queue", "max_unidentified_share", float("nan"), "L2 coverage"),
        ("2s", "observations", 61, "markout"),
        ("10s", "windows", 0, "markout"),
        ("10s", "value", 0, "markout"),
        ("stress", "plan_sha256", "", "stress"),
        ("stress", "extra_cancel_latency_ms", 0, "stress"),
        ("stress", "windows", 47, "stress"),
        ("stress", "ci_low_c_per_window", -0.01, "stress"),
    ),
)
def test_reject_sparse_or_unbound_maker_receipt(receipt, section, field, value, error):
    g, doc = receipt
    target = doc["markouts"][section] if section in ("2s", "10s") else doc[section]
    target[field] = value
    with pytest.raises(ValueError, match=error):
        maker_evidence(g, doc, REPLAY_SHA, INPUT_SHA)
