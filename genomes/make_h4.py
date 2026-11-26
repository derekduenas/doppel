"""H4: edge-selective maker (from E24, fill-conditional edge on public PM US prints, pre-window data).
Never quote inside 2 c of fair-v1 (fills in the >=2 c cohort earned +4.6..9.2 c/contract), optional sit-out
in the toxic 60-120 s time-to-close zone, identity flow guard kept. 4 variants:
half-spread {2, 3} c x toxic-zone pause {off, on}. Window: 2026-11-28T00:00Z .. 2026-12-01T00:00Z.
"""
import orjson
from pathlib import Path

from doppel.strategy.genome import (Condition, Exit, Genome, KillRule, MakerRule, Risk, Source,
                                    Universe, to_dict, validate)

OUT = Path(__file__).parent / "h4"
GUARD = (Condition("brti_move_5s", ">", 30.0), Condition("brti_move_5s", "<", -30.0),
         Condition("intl_informed_flow_30s", ">", 100.0), Condition("intl_informed_flow_30s", "<", -100.0))
for h in (2.0, 3.0):
    for toxic in (False, True):
        pause = GUARD + ((Condition("tte_s", "between", 60.0, 120.0),) if toxic else ())
        g = Genome(
            name=f"h4-edge2-h{int(h)}{'-skip60to120' if toxic else ''}", mode="maker",
            universe=Universe(venue="pmus", assets=("btc",), horizons=("5m",), tau_min_s=5, tte_min_s=20),
            source=Source(kind="manual", mechanism="COMPLETE_SET_MAKER"),
            maker=MakerRule(quote_ref="fair", half_spread_c=h, size=10, sides="both", skew_c_per_contract=0.05,
                            requote_threshold_c=1.0, max_quote_age_s=20, pause_when=pause, min_edge_vs_fair_c=2.0),
            exit=Exit(hold_to_settlement=True),
            risk=Risk(max_inventory=40, max_notional_per_window=40, max_loss_per_day=25, max_open_windows=1),
            kill=KillRule(min_windows=300, metric="net_c_per_window", pass_ci_low_gt=0.0, kill_ci_high_lt=0.0,
                          max_drawdown_usd=25.0),
            evidence={"family": "h4-edge-selective-v1", "basis": "E24 fill-conditional edge (pre-window prints)",
                      "eval_window": ["2026-11-28T00:00:00Z", "2026-12-01T00:00:00Z"]})
        assert validate(g) == [], validate(g)
        (OUT / f"{g.name}.json").write_bytes(orjson.dumps(to_dict(g), option=orjson.OPT_INDENT_2))
        print(g.id[:12], g.name)
