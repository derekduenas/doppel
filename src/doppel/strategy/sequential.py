"""Anytime-valid monitoring of a genome's net P&L: betting e-processes, Ville, family e-Holm.

The end-of-window verdict (``twin.verdict``, ``Registry.record_result``) looks once. Monitoring every
window with a CI or a t-test and stopping when it first looks good inflates the false-pass rate far
above alpha. An e-process can be watched continuously: under its null, P(sup_t E_t >= 1/alpha) <=
alpha (Ville's inequality), so "stop the first time E crosses 1/alpha" is a valid level-alpha test
at every peek.

Data. Eligible twin windows inside the genome's preregistered evaluation window, sorted by (end,
start) as ``twin.verdict`` does, net cents per window (``net_c``; a window that did not trade is 0).
Windows are correlated (overlapping horizons, volatility clusters), so the increments are means of
consecutive BLOCKS of ``block`` windows (default 12, the block length of ``verdict``'s moving-block
bootstrap). Only complete blocks count, anchored at the eval-window start, so a block never changes
once formed and later peeks only append blocks.

Bound. Let B (cents) bound a block mean. Z = Y / B lies in [-1, 1] when |Y| <= B.
* PASS process, H0: E[Y_j | blocks < j] <= 0. Wealth E_t = prod_j (1 + lam_j Z_j), lam_j in [0, 1/2]
  chosen from blocks < j only (predictable). Under H0 each factor has conditional mean <= 1 and is
  >= 1/2 > 0, so E_t is a nonnegative supermartingale. Clipping Y from ABOVE at +B only lowers the
  increments, so it is always conservative. Validity needs Y >= -B: the default B is the genome's
  physical worst-case loss per window, ``100 * max_notional_per_window * (1 + fee coef)`` cents
  (the twin caps cash put at risk per window at ``max_notional_per_window`` and the notional counter
  never decreases, so a zero-fee maker holding to settlement cannot lose more; the fee factor is a
  slack for genomes that cross the spread). The price is power: see docs/sequential.md. An
  observed block below -B falsifies the bound: it is counted in ``bound_violations`` and blocks an
  early PASS.
* KILL process, the mirror: H0': E[Y_j | past] >= 0, the same construction on -Y. Clipping Y from
  BELOW at -B is conservative; Y <= +B is an assumption (gains are not capped by the notional rule)
  and an observed block above +B blocks an early KILL.
* Betting fraction: aGRAPA (Waudby-Smith & Ramdas 2023), lam_j = clip(m / (v + m^2), 0, 1/2) with the
  running mean m and variance v of past Z, shrunk by one pseudo-observation (mean 0, variance 1/4).
  Fixed constants: nothing is tuned on the sample being tested.

The conditional-mean null is the honest assumption: block means are treated as a martingale
difference sequence. Positive correlation across blocks (beyond what a 12-window block absorbs) can
make the test anti-conservative; the peeking simulation in ``tests/test_sequential.py`` includes a
window-level AR(1) case. A PASS here says "mean net per window > 0"; for a ``net_c_per_contract``
genome the sign is the same (sum net / sum contracts > 0 iff mean net per window > 0 in expectation).

Family. A genome passes early only if its anytime-valid p-value p_i = min(1, 1 / max_s E_i(s))
survives Holm across EVERY genome registered in its family (pending ones p = 1, killed ones still
counted): e-Holm. Because the running max never decreases and a false rejection at any time implies
some true null's running max reached m0 / alpha, P(any false early pass, ever) <= alpha (Ville per
genome + union bound), so passes can be locked in as they happen. e-BH (Wang & Ramdas 2022) controls
FDR on current e-values at ONE stopping time; it is not valid on running maxima or for the union of
rejections over time, so it is not used for the decision. Early KILL is per genome at level alpha
(a kill is the conservative error and needs no family correction).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence

from .genome import Genome

BLOCK = 12
ALPHA = 0.05
LAMBDA_CAP = 0.5
PRIOR_VAR = 0.25
METHOD = "agrapa-block-v1"
LOG_E_MAX_JSON = 690.0  # exp() of this still fits a float; recorded e-values are capped here


def params(g: Genome, alpha: float = ALPHA, block: int = BLOCK) -> dict:
    """The frozen monitoring parameters for ``g``: all deterministic from the sealed genome and the
    module constants, never from the data."""
    return {"method": METHOD, "block": int(block), "alpha": float(alpha), "lambda_cap": LAMBDA_CAP,
            "prior_var": PRIOR_VAR, "bound_c": loss_bound_c(g)}


def loss_bound_c(g: Genome) -> float:
    """Worst-case loss of one window in cents: the cash the twin lets the genome put at risk in a
    window (``risk.max_notional_per_window`` dollars), plus a fee slack: the maker fee for a maker
    that holds to settlement (it never crosses), the larger of the two fee coefficients otherwise."""
    maker_only = g.mode == "maker" and g.exit.hold_to_settlement and g.exit.flatten_tte_s is None
    coef = max(g.costs.maker_fee_coef, 0.0) if maker_only else max(g.costs.taker_fee_coef, g.costs.maker_fee_coef, 0.0)
    return round(100.0 * g.risk.max_notional_per_window * (1.0 + coef), 6)


def eval_windows(windows: Iterable[dict], start_s: float, end_s: float,
                 as_of_s: float | None = None) -> list[dict]:
    """Eligible windows with start >= start_s and end <= min(end_s, as_of_s), in (end, start) order
    (the order ``twin.verdict`` uses). Later data only appends: earlier blocks never change."""
    cut = end_s if as_of_s is None else min(end_s, as_of_s)
    return sorted((r for r in windows if r.get("eligible") and r["start"] >= start_s and r["end"] <= cut),
                  key=lambda r: (r["end"], r["start"]))


def block_means(values: Sequence[float], block: int = BLOCK) -> list[float]:
    """Means of consecutive complete blocks of ``block`` values; a trailing partial block is dropped."""
    b = int(block)
    if b < 1:
        raise ValueError("block must be >= 1")
    return [sum(values[i:i + b]) / b for i in range(0, len(values) - b + 1, b)]


def betting_path(z: Sequence[float], cap: float = LAMBDA_CAP, prior_var: float = PRIOR_VAR) -> list[float]:
    """log E_t after each increment for H0: E[z_t | past] <= 0, z_t in [-1, 1]. aGRAPA bets: lam_t
    from z_1..z_{t-1} only."""
    log_e, s, ss, n = 0.0, 0.0, 0.0, 0
    path = []
    for zt in z:
        if not -1.0 <= zt <= 1.0:
            raise ValueError(f"increment {zt} outside [-1, 1]")
        m = s / (n + 1)
        v = (prior_var + ss) / (n + 1)
        lam = min(cap, max(0.0, m / (v + m * m)))
        log_e += math.log1p(lam * zt)
        path.append(log_e)
        ss += (zt - m) ** 2
        s += zt
        n += 1
    return path


def _direction(y: Sequence[float], bound: float, sign: int, alpha: float) -> dict:
    """One e-process. sign=+1: H0 mean <= 0 (PASS); sign=-1: H0 mean >= 0 (KILL)."""
    z, safe, unsafe = [], 0, 0
    for v in y:
        x = sign * v / bound
        if x > 1.0:          # clipping from above only lowers the bet's increment: conservative
            safe += 1
            x = 1.0
        elif x < -1.0:       # below the assumed bound: the validity assumption failed on this block
            unsafe += 1
            x = -1.0
        z.append(x)
    path = betting_path(z)
    log_e = path[-1] if path else 0.0
    log_max = max([0.0, *path])
    thr = math.log(1.0 / alpha)
    cross = next((i + 1 for i, le in enumerate(path) if le >= thr), None)
    return {"log_e": round(log_e, 6), "log_e_max": round(log_max, 6),
            "e": _e(log_e), "e_max": _e(log_max), "p_anytime": min(1.0, math.exp(-log_max)),
            "threshold": 1.0 / alpha, "rejects": cross is not None, "first_cross_block": cross,
            "clipped_conservative": safe, "bound_violations": unsafe}


def _e(log_e: float) -> float:
    return float(math.exp(min(log_e, LOG_E_MAX_JSON)))


def e_processes(values: Sequence[float], bound_c: float, block: int = BLOCK, alpha: float = ALPHA) -> dict:
    """Both e-processes over per-window net cents ``values`` (already filtered and ordered)."""
    if not bound_c > 0:
        raise ValueError("bound_c must be > 0")
    y = block_means(values, block)
    return {"n_windows": len(values), "n_blocks": len(y), "windows_used": len(y) * int(block),
            "mean_c": round(sum(values) / len(values), 4) if values else None,
            "pass": _direction(y, bound_c, +1, alpha), "kill": _direction(y, bound_c, -1, alpha)}


def anytime_p(e_max: float) -> float:
    return 1.0 if e_max <= 1.0 else min(1.0, 1.0 / e_max)


def e_holm(e_maxes: Sequence[float]) -> list[float]:
    """Holm-adjusted anytime-valid p-values from running-max e-values (input order). Rejecting
    p_holm <= alpha controls FWER uniformly over time, so rejections may be locked in."""
    from .registry import holm  # local import: registry imports this module
    return holm([anytime_p(e) for e in e_maxes])


def decide(stats: dict, p_holm: float, n_traded: int, min_windows: int, alpha: float = ALPHA) -> tuple[str, str]:
    """EARLY_PASS / EARLY_KILL / CONTINUE from both processes and the family Holm p-value."""
    ps, ks = stats["pass"], stats["kill"]
    if ps["rejects"] and ks["rejects"]:
        return "CONTINUE", "both e-processes crossed: the bound assumption is wrong, investigate"
    if ks["rejects"]:
        if ks["bound_violations"]:
            return "CONTINUE", (f"kill e-process crossed but {ks['bound_violations']} block(s) above +bound: "
                                "the assumption behind it failed")
        return "EARLY_KILL", (f"kill e-process max {ks['e_max']:.4g} >= 1/alpha {1 / alpha:g} "
                              f"(H0: mean net/window >= 0) at block {ks['first_cross_block']}")
    if ps["rejects"]:
        if ps["bound_violations"]:
            return "CONTINUE", (f"pass e-process crossed but {ps['bound_violations']} block(s) below -bound: "
                                "the loss bound failed, no early pass")
        if p_holm > alpha:
            return "CONTINUE", (f"pass e-process max {ps['e_max']:.4g} >= 1/alpha but family e-Holm "
                                f"p {p_holm:.4g} > {alpha:g}")
        if n_traded < min_windows:
            return "CONTINUE", f"pass e-process crossed but {n_traded} traded windows < min_windows {min_windows}"
        return "EARLY_PASS", (f"pass e-process max {ps['e_max']:.4g} >= 1/alpha and family e-Holm "
                              f"p {p_holm:.4g} <= {alpha:g}")
    return "CONTINUE", (f"neither e-process reached 1/alpha {1 / alpha:g} (pass max {ps['e_max']:.4g}, "
                        f"kill max {ks['e_max']:.4g})")
