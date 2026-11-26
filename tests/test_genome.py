from doppel.strategy.genome import (Condition, Genome, MakerRule, Risk, Source, TakerRule, Universe,
                                   describe, fee_c, from_dict, to_dict, validate)


def _taker():
    return Genome(name="late-edge-taker", mode="taker",
                  universe=Universe(venue="pmus", tau_min_s=120, tte_min_s=5),
                  source=Source(kind="clone", wallets=("0xabc",), mechanism="LATE_TAKER"),
                  taker=(TakerRule(side="up", size=5,
                                   when=(Condition("edge_buy_up_c", ">", 3.0),
                                         Condition("tte_s", "between", 10, 120))),))


def _maker():
    return Genome(name="guarded-maker", mode="maker", universe=Universe(venue="pmus", tte_min_s=20),
                  source=Source(kind="manual"),
                  maker=MakerRule(half_spread_c=2, size=10, skew_c_per_contract=0.1,
                                  pause_when=(Condition("brti_move_5s", ">", 40.0),)),
                  risk=Risk(max_inventory=30))


def test_roundtrip_and_stable_id():
    g = _taker()
    g2 = from_dict(to_dict(g))
    assert g2 == g and g2.id == g.id
    # status/notes don't change identity; the rule does
    assert from_dict({**to_dict(g), "status": "twin_pass", "notes": "x"}).id == g.id
    d = to_dict(g)
    d["taker"][0]["when"][0]["value"] = 4.0
    assert from_dict(d).id != g.id


def test_validate():
    assert validate(_taker()) == [] and validate(_maker()) == []
    bad = Genome(name="b", mode="taker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
                 taker=(TakerRule(side="up", size=1, when=(Condition("nonsense", ">", 1),)),))
    assert any("unknown feature" in e for e in validate(bad))
    assert any("no conditions" in e for e in validate(Genome(
        name="c", mode="taker", universe=Universe(venue="pmus"), source=Source(kind="manual"),
        taker=(TakerRule(side="up", size=1, when=()),))))


def test_taker_rule_evaluation():
    g = _taker()
    x = {"tau_s": 200, "tte_s": 60, "edge_buy_up_c": 4.0}
    assert [r.side for r in g.taker_actions(x)] == ["up"]
    assert g.taker_actions({**x, "edge_buy_up_c": 2.0}) == []
    assert g.taker_actions({**x, "tau_s": 100}) == []          # before tau_min
    assert g.taker_actions({**x, "edge_buy_up_c": float("nan")}) == []


def test_maker_quotes_skew_pause_and_inventory():
    g = _maker()
    x = {"tau_s": 10, "tte_s": 200, "fair_up": 0.50, "inventory_up": 0.0, "brti_move_5s": 0.0}
    assert g.maker_quotes(x) == (48, 52)
    assert g.maker_quotes({**x, "inventory_up": 20.0}) == (46, 50)   # long -> shade both down
    assert g.maker_quotes({**x, "inventory_up": 30.0})[0] is None     # at max inventory: no bid
    assert g.maker_quotes({**x, "brti_move_5s": 50.0}) == (None, None)  # toxicity guard
    assert g.maker_quotes({**x, "tte_s": 10}) == (None, None)          # inside tte_min


def test_fee_and_describe():
    assert abs(fee_c(0.5, 0.095) - 2.375) < 1e-9
    text = describe(_taker())
    assert "TAKE UP 5 when edge_buy_up_c > 3.0 AND tte_s between 10..120" in text
    assert "Kill: after 300 windows" in text


def test_spread_mode_logit_and_sealed_ids_unchanged():
    import glob, orjson
    from doppel.strategy.genome import Genome, MakerRule, Source, Universe, from_dict
    reg = [orjson.loads(l) for l in open("registry/genomes.jsonl", "rb") if l.strip()]
    ids = {r.get("genome_id") or r.get("id") for r in reg}
    on_disk = {from_dict(orjson.loads(open(f, "rb").read())).id for f in glob.glob("genomes/h*/*.json")}
    assert on_disk <= ids
    base = dict(name="m", mode="maker", universe=Universe(venue="pmus"), source=Source(kind="manual"))
    flat = Genome(**base, maker=MakerRule(half_spread_c=2.0, min_edge_vs_fair_c=0.0))
    lg = Genome(**base, maker=MakerRule(half_spread_c=2.0, min_edge_vs_fair_c=0.0, spread_mode="logit"))
    x = {"fair_up": 0.5, "inventory_up": 0.0, "tau_s": 100, "tte_s": 100}
    assert flat.maker_quotes(x) == lg.maker_quotes(x) == (48, 52)
    x["fair_up"] = 0.9
    assert lg.maker_quotes(x) == (89, 91) and flat.maker_quotes(x) == (88, 92)
    assert flat.id != lg.id
