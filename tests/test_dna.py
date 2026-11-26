from pathlib import Path

import pytest

from doppel.strategy import dna
from doppel.strategy.genome import FEATURES, Genome, MakerRule, Source, Universe

ROOT = Path(__file__).resolve().parents[1]


def _g(**mk):
    return Genome(name="m", mode="maker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
                  maker=MakerRule(**mk))


def test_sealed_genomes_keep_ids_and_expression():
    """Pins what every sealed genome DOES: a code change that alters any decision fails here."""
    assert dna.check(ROOT) == []
    assert len(dna.registered_genomes(ROOT)) >= 20


def test_probe_set_covers_every_feature_and_is_deterministic():
    xs = dna.probes(200)
    assert set(FEATURES) <= set(xs[0])
    assert xs == dna.probes(200)


def test_expression_hash_sees_behaviour_not_names():
    a, b = _g(half_spread_c=2.0), _g(half_spread_c=2.0)
    assert dna.expression_trace(a)["expression_sha256"] == dna.expression_trace(b)["expression_sha256"]
    c = _g(half_spread_c=3.0)
    assert dna.expression_trace(a)["expression_sha256"] != dna.expression_trace(c)["expression_sha256"]


def test_mutate_and_crossover_record_lineage_and_new_ids():
    p = _g(half_spread_c=2.0)
    k = dna.mutate(p, "maker.half_spread_c", 3.0)
    assert k.source.parents == (p.id,) and k.source.operator == "mutate:maker.half_spread_c"
    assert k.maker is not None and k.maker.half_spread_c == 3.0 and k.id != p.id
    q = _g(half_spread_c=1.0, size=5.0)
    x = dna.crossover(p, q, ("maker",))
    assert set(x.source.parents) == {p.id, q.id} and x.maker == q.maker
    with pytest.raises(ValueError):
        dna.mutate(p, "maker.size", 0.0)  # invalid child refused
    with pytest.raises(KeyError):
        dna.mutate(p, "maker.no_such_field", 1)


def test_lineage_fields_do_not_move_pre_existing_ids():
    g = _g(half_spread_c=2.0)
    assert b'"parents"' not in g.canonical() and b'"operator"' not in g.canonical()


def test_phenotype_vector_shape():
    v = {"windows_evaluated": 10, "windows_traded": 5, "windows_stood_down": 1, "contracts": 50.0,
         "net_c_per_window": {"mean": 1.5}, "net_c_per_contract": {"value": 0.3},
         "markout_c": {"maker": {"1": -0.2, "5": -0.4, "30": -0.9}, "taker": {}},
         "maker": {"fill_rate": 0.1}, "capacity": {"share_of_venue_volume": 0.001}, "max_drawdown_usd": 3.0}
    ph = dna.phenotype(v)
    assert set(ph) == set(dna.PHENOTYPE_KEYS)
    assert ph["contracts_per_window"] == 5.0 and ph["traded_window_share"] == 0.5 and ph["markout_30s_c"] == -0.9
    assert dna.phenotype_drift(ph, {**ph, "maker_fill_rate": 0.05})["maker_fill_rate"] == -0.05
