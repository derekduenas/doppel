"""H1 family: guarded fair-value maker on PM US BTC 5m (hand-designed baseline, preregistered).

Six variants = half-spread {1, 2, 3} cents x toxicity guard {off, pause when |raw BRTI 5 s move| > $30}.
The family is fixed before any twin result exists; its winner must survive Holm across all six.
Evaluation window (preregistered, future data only): 2026-11-26T00:00Z .. 2026-11-29T00:00Z.
"""
import orjson
from pathlib import Path

from doppel.strategy.genome import (Condition, Exit, Genome, KillRule, MakerRule, Risk, Source,
                                    Universe, describe, to_dict, validate)

OUT = Path(__file__).parent / "h1"
for h in (1.0, 2.0, 3.0):
    for guard in (False, True):
        pause = (Condition("brti_move_5s", ">", 30.0), Condition("brti_move_5s", "<", -30.0)) if guard else ()
        g = Genome(
            name=f"h1-maker-h{int(h)}{'-guard30' if guard else ''}",
            mode="maker",
            universe=Universe(venue="pmus", assets=("btc",), horizons=("5m",), tau_min_s=5, tte_min_s=20),
            source=Source(kind="manual", mechanism="COMPLETE_SET_MAKER"),
            maker=MakerRule(quote_ref="fair", half_spread_c=h, size=10, sides="both",
                            skew_c_per_contract=0.05, requote_threshold_c=1.0, max_quote_age_s=20,
                            pause_when=pause, min_edge_vs_fair_c=0.5),
            exit=Exit(hold_to_settlement=True),
            risk=Risk(max_inventory=40, max_notional_per_window=40, max_loss_per_day=25, max_open_windows=1),
            kill=KillRule(min_windows=300, metric="net_c_per_window", pass_ci_low_gt=0.0, kill_ci_high_lt=0.0,
                          max_drawdown_usd=25.0),
            evidence={"basis": "census v0: crypto-window profit pool is maker-side; fair-v1 beats series-only "
                               "model (E20); taker fee 0.095 p(1-p) on PM US BTC 5m (E8)",
                      "family": "h1-maker-v1", "eval_window": ["2026-11-26T00:00:00Z", "2026-11-29T00:00:00Z"]},
            notes="")
        assert validate(g) == [], validate(g)
        (OUT / f"{g.name}.json").write_bytes(orjson.dumps(to_dict(g), option=orjson.OPT_INDENT_2))
        print(g.id[:12], describe(g)[:160])
