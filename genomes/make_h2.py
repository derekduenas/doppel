"""H2 family: flow-guarded fair-value maker on PM US BTC 5m (preregistered).

H1 plus a pause when |intl informed flow over the last 30 s| > T. T is set from PRE-window data
(2026-11-25, 1,200 BTC 5m seconds): |informed_flow_30s| p50 $26, p90 $141, p99 $617.
Variants: half-spread {1, 2, 3} c x T {100, 300}; all keep the |BRTI 5 s| > $30 guard.
Evaluation window (future only): 2026-11-27T00:00Z .. 2026-11-30T00:00Z.
"""
import orjson
from pathlib import Path

from doppel.strategy.genome import (Condition, Exit, Genome, KillRule, MakerRule, Risk, Source,
                                    Universe, describe, to_dict, validate)

OUT = Path(__file__).parent / "h2"
for h in (1.0, 2.0, 3.0):
    for T in (100.0, 300.0):
        pause = (Condition("brti_move_5s", ">", 30.0), Condition("brti_move_5s", "<", -30.0),
                 Condition("intl_informed_flow_30s", ">", T), Condition("intl_informed_flow_30s", "<", -T))
        g = Genome(
            name=f"h2-maker-h{int(h)}-flow{int(T)}", mode="maker",
            universe=Universe(venue="pmus", assets=("btc",), horizons=("5m",), tau_min_s=5, tte_min_s=20),
            source=Source(kind="manual", mechanism="COMPLETE_SET_MAKER"),
            maker=MakerRule(quote_ref="fair", half_spread_c=h, size=10, sides="both", skew_c_per_contract=0.05,
                            requote_threshold_c=1.0, max_quote_age_s=20, pause_when=pause, min_edge_vs_fair_c=0.5),
            exit=Exit(hold_to_settlement=True),
            risk=Risk(max_inventory=40, max_notional_per_window=40, max_loss_per_day=25, max_open_windows=1),
            kill=KillRule(min_windows=300, metric="net_c_per_window", pass_ci_low_gt=0.0, kill_ci_high_lt=0.0,
                          max_drawdown_usd=25.0),
            evidence={"basis": "H1 diagnostics lean negative (unguarded maker picked off); W3 informed-flow guard; "
                               "thresholds from pre-window distribution p50 26 / p90 141 / p99 617",
                      "family": "h2-flowguard-v1", "eval_window": ["2026-11-27T00:00:00Z", "2026-11-30T00:00:00Z"]})
        assert validate(g) == [], validate(g)
        (OUT / f"{g.name}.json").write_bytes(orjson.dumps(to_dict(g), option=orjson.OPT_INDENT_2))
        print(g.id[:12], g.name)
