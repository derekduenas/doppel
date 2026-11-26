"""H3: four-arm flow-guard trial (Astra review, 2026-11-26). One frozen maker policy, four arms on the
SAME future windows, so the only difference is what the guard may see:
  base      : US book, BRTI, time left (|BRTI 5 s| > $30 guard only)
  anon      : + pause when |intl raw flow 30 s| > $1,725   (matched to identified pause rate 17.4%)
  ident     : + pause when |intl informed flow 30 s| > $100 (wallet identity, receive-time)
  placebo   : + pause when placebo_u < 0.1742              (same pause rate, no information)
Pause-rate matching used PRE-window data only (2026-11-25, 1,200 BTC 5m seconds).
Preregistered delay sweep on the ident arm: flow seen 0, 2, 5, 10, 30 s late (twin --flow-delay-s).
Scoring: incremental net $ per eligible window vs base (pairs on identical windows), incl. profitable
fills the guard cancelled; commercial hurdle reported. Window: 2026-11-27T00:00Z .. 2026-11-30T00:00Z.
"""
import orjson
from pathlib import Path

from doppel.strategy.genome import (Condition, Exit, Genome, KillRule, MakerRule, Risk, Source,
                                    Universe, to_dict, validate)

OUT = Path(__file__).parent / "h3"
BRTI = (Condition("brti_move_5s", ">", 30.0), Condition("brti_move_5s", "<", -30.0))
ARMS = {
    "base": (),
    "anon": (Condition("intl_flow_up_usd_30s", ">", 1725.0), Condition("intl_flow_up_usd_30s", "<", -1725.0)),
    "ident": (Condition("intl_informed_flow_30s", ">", 100.0), Condition("intl_informed_flow_30s", "<", -100.0)),
    "placebo": (Condition("placebo_u", "<", 0.1742),),
}
for arm, extra in ARMS.items():
    g = Genome(
        name=f"h3-{arm}", mode="maker",
        universe=Universe(venue="pmus", assets=("btc",), horizons=("5m",), tau_min_s=5, tte_min_s=20),
        source=Source(kind="manual", mechanism="COMPLETE_SET_MAKER"),
        maker=MakerRule(quote_ref="fair", half_spread_c=2.0, size=10, sides="both", skew_c_per_contract=0.05,
                        requote_threshold_c=1.0, max_quote_age_s=20, pause_when=BRTI + extra, min_edge_vs_fair_c=0.5),
        exit=Exit(hold_to_settlement=True),
        risk=Risk(max_inventory=40, max_notional_per_window=40, max_loss_per_day=25, max_open_windows=1),
        kill=KillRule(min_windows=300, metric="net_c_per_window", pass_ci_low_gt=0.0, kill_ci_high_lt=0.0,
                      max_drawdown_usd=25.0),
        evidence={"family": "h3-flowguard-4arm-v1", "arm": arm, "pause_rate_target": 0.1742,
                  "delay_sweep_s": [0, 2, 5, 10, 30],
                  "eval_window": ["2026-11-27T00:00:00Z", "2026-11-30T00:00:00Z"]})
    assert validate(g) == [], validate(g)
    (OUT / f"{g.name}.json").write_bytes(orjson.dumps(to_dict(g), option=orjson.OPT_INDENT_2))
    print(g.id[:12], g.name)
