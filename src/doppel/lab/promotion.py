"""doppel-promotion: a conservative gate ON TOP of the existing verdict, for research -> paper/live.

    doppel-promotion --results TWIN.json [TWIN2.json LEDGER.json ...] --trials TRIALS.json --out DIR

It never replaces the twin verdict (``twin.verdict``) or the registry's Holm ledger: a candidate can
only be promoted when its own verdict is already PASS, and then it must also clear every row below.
Nothing here places orders or writes the registry; the output is a decision record in ``--out``.

SERIES. Per-window net cents of the eligible windows (``net_c``; a window that did not trade is 0),
in (end, start) order, exactly the series the verdict bootstraps. Sharpe is per window, not annualised.

1. DEFLATED SHARPE RATIO (Bailey & Lopez de Prado 2014, "The Deflated Sharpe Ratio").
   SR = mean / sd (ddof 1); g3 = sample skewness, g4 = sample kurtosis (Pearson, normal = 3), both
   from population moments. Expected maximum Sharpe of N null trials:
       SR0 = sqrt(V) * ((1 - e) * Z^-1(1 - 1/N) + e * Z^-1(1 - 1/(N exp(1)))),  e = Euler-Mascheroni
   (SR0 = 0 when N = 1). V is the variance of the Sharpe estimates across the N trials: the trials
   ledger's ``sr_variance`` when recorded, else the null estimator variance 1 / (T - 1); the larger
   of the two when both exist (never deflate less than the null does). Then
       DSR = Z( (SR - SR0) sqrt(T - 1) / sqrt(1 - g3 SR + (g4 - 1) / 4 SR^2) ).
   Gate: DSR >= 0.95. T < 3 or zero variance gives null, and null never passes.
2. FALSE DISCOVERY RATE across the study: Benjamini-Hochberg step-up at q = 0.05 over the family of
   p-values (every candidate's one-sided verdict p, every p in the trials ledger, every row of an
   imported ``doppel-genome ledger --json``), a candidate's p being the larger of the time-ordered
   bootstrap p below and its verdict's ``p_value``, padded with p = 1 up to ``n_trials`` (a trial without
   a recorded p counts as a failure, as pending genomes do in the registry). Holm (registry.holm) is
   reported beside it. Gate: the candidate's p survives BH.
3. OVERLAP-ROBUST MEAN: Newey-West HAC t of the mean net c/window, Bartlett kernel, Andrews (1991)
   AR(1) plug-in bandwidth S = 1.1447 (a T)^(1/3), a = 4 rho^2 / ((1 - rho)^2 (1 + rho)^2), rho the
   lag-1 autocorrelation clipped to [-0.97, 0.97]; weights w_j = 1 - j / S for j < S. One-sided p
   from the normal. Reported next to the verdict's centred block-bootstrap p (``block_bootstrap_p``,
   recomputed here with the verdict's block and seed). Gate: both means > 0 and both p <= 0.05
   (they must agree in sign and significance).

4. PROBABILITY OF BACKTEST OVERFITTING (Bailey, Borwein, Lopez de Prado & Zhu 2015), CSCV over the
   T x M matrix of per-window net of ALL M variants tried (``--variants``), S = 16 contiguous
   blocks: for each of the C(S, S/2) in-sample halves, the in-sample best Sharpe's out-of-sample
   relative rank w, logit ln(w / (1 - w)); PBO = share of logits <= 0. Gate: PBO < 0.15. Not
   applicable when n_trials = 1; otherwise a missing matrix fails the row. A matrix with more
   variants than n_trials is refused (the ledger must count every simulator / forge variant tried).

DECISION per candidate: PROMOTE only when every row passes; otherwise HOLD. Binding reason = the
first failing row in this order: base verdict, DSR, PBO, BH, HAC/bootstrap agreement. Refused
outright (exit 2, nothing written) when the trials ledger does not state ``n_trials``, or n_trials
is smaller than the trials it lists, the candidates supplied or the variants in the matrix.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import math
import sys
from collections.abc import Set
from pathlib import Path
from statistics import NormalDist

import numpy as np
import orjson

from ..strategy.registry import holm
from ..twin.verdict import block_bootstrap_ci, block_bootstrap_p
from . import shadow_trial_ledger

DSR_MIN = 0.95
PBO_MAX = 0.15
PBO_S = 16
FDR_Q = 0.05
ALPHA = 0.05
EULER = 0.5772156649015329
RHO_CAP = 0.97
Z = NormalDist()
CANONICAL_LEDGER_SCHEMA = "doppel.shadow-trial-ledger/v1"
ABSTENTION_PLAN_SCHEMA = shadow_trial_ledger.PLAN_SCHEMA
ABSTENTION_LEDGER_SCHEMA = shadow_trial_ledger.LEDGER_SCHEMA
ABSTENTION_PROOF_SCHEMA = shadow_trial_ledger.ABSTENTION_SCHEMA


def moments(x) -> tuple[float, float, float, float] | None:
    """(mean, sd ddof 1, skewness, Pearson kurtosis) or None when undefined."""
    v = np.asarray(x, dtype=float)
    if len(v) < 3:
        return None
    m, c = float(v.mean()), v - v.mean()
    m2 = float((c ** 2).mean())
    if m2 <= 1e-18:
        return None
    return m, float(v.std(ddof=1)), float((c ** 3).mean() / m2 ** 1.5), float((c ** 4).mean() / m2 ** 2)


def expected_max_sr(n_trials: int, sr_var: float) -> float:
    if n_trials < 1:
        raise ValueError("n_trials must be >= 1")
    if n_trials == 1:
        return 0.0
    return math.sqrt(sr_var) * ((1 - EULER) * Z.inv_cdf(1 - 1 / n_trials)
                                + EULER * Z.inv_cdf(1 - 1 / (n_trials * math.e)))


def psr(sr: float, sr_star: float, t: int, skew: float, kurt: float) -> float:
    """Probabilistic Sharpe ratio: P(true SR > sr_star)."""
    den = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    return Z.cdf((sr - sr_star) * math.sqrt(t - 1) / math.sqrt(den)) if den > 0 else float("nan")


def deflated_sharpe(x, n_trials: int, sr_variance: float | None = None) -> dict:
    mo = moments(x)
    if mo is None:
        return {"dsr": None, "reason": "fewer than 3 windows or zero variance"}
    m, sd, g3, g4 = mo
    t = len(x)
    null_var = 1 / (t - 1)
    v = null_var if sr_variance is None else max(float(sr_variance), null_var)
    sr = m / sd
    sr0 = expected_max_sr(n_trials, v)
    d = psr(sr, sr0, t, g3, g4)
    return {"dsr": _r(d) if math.isfinite(d) else None, "sr": _r(sr), "sr0": _r(sr0), "t": t,
            "skew": _r(g3), "kurt": _r(g4), "n_trials": n_trials, "sr_variance": _r(v),
            "sr_variance_source": "null 1/(T-1)" if sr_variance is None or v == null_var else "trials ledger",
            "psr_vs_0": _r(psr(sr, 0.0, t, g3, g4))}


def bh(pvals: list[float], q: float = FDR_Q) -> tuple[list[float], list[bool]]:
    """Benjamini-Hochberg adjusted p-values (input order) and which survive at q."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: (pvals[i], i))
    adj = [1.0] * m
    run = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        run = min(run, m * pvals[i] / rank)
        adj[i] = min(1.0, run)
    return adj, [a <= q for a in adj]


def andrews_bandwidth(x) -> tuple[float, float]:
    """(Bartlett bandwidth S, lag-1 autocorrelation used) from the AR(1) plug-in."""
    c = np.asarray(x, dtype=float) - np.mean(x)
    den = float(c[:-1] @ c[:-1])
    rho = float(c[1:] @ c[:-1]) / den if den > 0 else 0.0
    rho = max(-RHO_CAP, min(RHO_CAP, rho))
    a = 4 * rho * rho / ((1 - rho) ** 2 * (1 + rho) ** 2)
    return 1.1447 * (a * len(c)) ** (1 / 3), rho


def hac_var(x, bandwidth: float) -> float:
    """Newey-West long-run variance: g0 + 2 sum_{j < S} (1 - j/S) g_j, autocovariances / T."""
    c = np.asarray(x, dtype=float) - np.mean(x)
    t = len(c)
    s = float(c @ c) / t
    j = 1
    while j < bandwidth and j < t:
        s += 2 * (1 - j / bandwidth) * float(c[j:] @ c[:-j]) / t
        j += 1
    return s


def newey_west(x) -> dict:
    v = np.asarray(x, dtype=float)
    if len(v) < 3:
        return {"t": None, "p_one_sided": None, "reason": "fewer than 3 windows"}
    bw, rho = andrews_bandwidth(v)
    lrv = hac_var(v, bw)
    if lrv <= 0:
        return {"t": None, "p_one_sided": None, "reason": "zero long-run variance"}
    tt = float(v.mean()) / math.sqrt(lrv / len(v))
    return {"mean": _r(float(v.mean())), "t": _r(tt), "p_one_sided": _r(1 - Z.cdf(tt), 6),
            "bandwidth": _r(bw), "lags": max(0, math.ceil(bw) - 1), "rho1": _r(rho),
            "long_run_var": _r(lrv), "kernel": "bartlett, w_j = 1 - j/S, Andrews AR(1) plug-in"}


def pbo(matrix, s: int = PBO_S) -> dict:
    """Probability of Backtest Overfitting by CSCV (Bailey, Borwein, Lopez de Prado & Zhu 2015).
    ``matrix``: T x M per-window returns of all M variants tried, rows in time order and aligned.
    Rows are cut into ``s`` contiguous equal blocks (trailing T mod s rows dropped). For every choice
    of s/2 blocks as in-sample (the rest out-of-sample): pick the variant with the best in-sample
    Sharpe, take its out-of-sample relative rank w = rank / (M + 1) (average rank on ties, 1 =
    worst), logit = ln(w / (1 - w)). PBO = share of logits <= 0 (the in-sample best is at or below
    the out-of-sample median)."""
    X = np.asarray(matrix, dtype=float)
    if X.ndim != 2 or X.shape[1] < 2:
        raise ValueError("PBO needs a T x M matrix with M >= 2 variants")
    if s < 2 or s % 2:
        raise ValueError("S must be even and >= 2")
    t, m = X.shape
    rows = t // s
    if rows < 2:
        raise ValueError(f"PBO needs at least {2 * s} windows for S = {s}, got {t}")
    B = X[:rows * s].reshape(s, rows, m)
    s1, s2 = B.sum(1), (B ** 2).sum(1)          # per block and variant
    mask = np.zeros((math.comb(s, s // 2), s))
    for i, combo in enumerate(itertools.combinations(range(s), s // 2)):
        mask[i, list(combo)] = 1.0
    n = rows * s / 2                            # windows per half

    def sharpe(mk):
        a, b = mk @ s1, mk @ s2
        return (a / n) / np.sqrt(np.maximum((b - a * a / n) / (n - 1), 1e-18))

    ins, oos = sharpe(mask), sharpe(1.0 - mask)
    v = oos[np.arange(len(mask)), np.argmax(ins, axis=1)][:, None]
    rank = (oos < v).sum(1) + ((oos == v).sum(1) + 1) / 2     # average rank on ties, 1 = worst
    w = rank / (m + 1)
    lg = np.log(w / (1 - w))
    counts, edges = np.histogram(lg, 10)
    return {"pbo": _r(float((lg <= 0).mean())), "variants": m, "windows": t, "windows_used": rows * s,
            "s": s, "combinations": len(lg),
            "logit": {"mean": _r(float(lg.mean())),
                      "quantiles": {str(q): _r(float(np.quantile(lg, q))) for q in (0.05, 0.25, 0.5, 0.75, 0.95)},
                      "hist_edges": [_r(float(e)) for e in edges], "hist_counts": [int(c) for c in counts]}}


def canonical_trial_statistics(values, *, abstentions: Set[int] = frozenset()) -> tuple[float, dict, list[dict]]:
    """Compute full-precision statistics for the same aligned trial family at report and admission."""
    from ..twin.evaluate import VERDICT_OPTIONS

    rows = np.asarray(values, dtype=float)
    if rows.ndim != 2 or rows.shape[0] < 2:
        raise ValueError("canonical statistics require at least two aligned trials")
    n, size = rows.shape
    if any(type(i) is not int or not 0 <= i < n or np.any(rows[i] != 0) for i in abstentions):
        raise ValueError("verified abstentions must be aligned all-zero trials")
    trial_moments = [None if i in abstentions else moments(row) for i, row in enumerate(rows)]
    if any(m is None or not all(math.isfinite(x) for x in m)
           for i, m in enumerate(trial_moments) if i not in abstentions):
        raise ValueError("a planned trial lacks estimable return moments")
    usable = [m for m in trial_moments if m is not None]
    if len(usable) < 2:
        raise ValueError("canonical statistics require at least two estimable trials")
    sharpes = np.asarray([m / sd for m, sd, _, _ in usable])
    variance = float(sharpes.var(ddof=1))
    sr_star = expected_max_sr(n, max(variance, 1 / (size - 1)))
    overfit = pbo(rows.T)
    boot = [1.0 if i in abstentions else
            block_bootstrap_p(row, VERDICT_OPTIONS["block"], VERDICT_OPTIONS["n_boot"], VERDICT_OPTIONS["seed"])
            for i, row in enumerate(rows)]
    if any(p is None or not math.isfinite(p) for p in boot):
        raise ValueError("trial p-values are unavailable")
    q, keep = bh([p for p in boot if p is not None])
    stats = []
    for i, (row, m) in enumerate(zip(rows, trial_moments, strict=True)):
        common = {"pbo": overfit["pbo"], "fdr_q": q[i], "bootstrap_p": boot[i],
                  "bh_survives": keep[i], "trial_count": n}
        if m is None:
            stats.append({**common, "dsr": None, "hac_t": None, "hac_p": None})
            continue
        mean, sd, skew, kurt = m
        dsr = psr(mean / sd, sr_star, size, skew, kurt)
        bw, _ = andrews_bandwidth(row)
        long_var = hac_var(row, bw)
        t = mean / math.sqrt(long_var / size) if long_var > 0 else float("nan")
        if not all(math.isfinite(x) for x in (dsr, t)):
            raise ValueError("a required statistic is undefined")
        stats.append({**common, "dsr": dsr, "hac_t": t, "hac_p": math.erfc(t / math.sqrt(2)) / 2})
    return variance, overfit, stats


def verified_abstentions(ledger: dict, input_sha256: str, *, plan: dict | None = None) -> set[int]:
    """Check the v2 chain and bind abstention evidence to the verified replay and pinned plan."""
    shadow_trial_ledger.validate_ledger_v2(ledger)
    actual = {}
    indices = set()
    for i, row in enumerate(ledger["trials"]):
        if row["disposition"] == "ERROR":
            raise ValueError("a planned trial lacks a finite aligned return series")
        if row["disposition"] != "ABSTAIN":
            continue
        proof = row["abstention"]
        if proof["input_sha256"] != input_sha256:
            raise ValueError("abstention proof refers to a different sealed replay input")
        actual[row["trial_id"]] = proof["policy_sha256"]
        indices.add(i)
    if plan is not None and plan.get("abstentions") != actual:
        raise ValueError("abstention proofs differ from the preregistered policies")
    return indices


def load_variants(doc: dict) -> tuple[list[str], np.ndarray]:
    """``{"variants": [{"id": ..., "net_c": [...]}, ...]}``: every variant over the same windows in time order."""
    vs = doc.get("variants")
    if not isinstance(vs, list) or len(vs) < 2:
        raise ValueError("variants file must list at least 2 variants")
    n = {len(v.get("net_c") or []) for v in vs}
    if len(n) != 1:
        raise ValueError(f"variants have different window counts {sorted(n)}: they must be aligned")
    return [str(v.get("id")) for v in vs], np.array([v["net_c"] for v in vs], dtype=float).T


# ---------------------------------------------------------------- inputs
def series(doc: dict) -> list[float]:
    ev = sorted((r for r in doc.get("windows") or [] if r.get("eligible")), key=lambda r: (r["end"], r["start"]))
    return [float(r["net_c"]) for r in ev]


def load_trials(doc: dict) -> dict:
    n = doc.get("n_trials")
    if type(n) is not int or n < 1:
        raise ValueError("trials ledger must state n_trials (an integer >= 1): the number of strategy "
                         "variants, wallets and parameter sets tried in the search")
    trials = doc.get("trials") or []
    if len(trials) > n:
        raise ValueError(f"trials ledger lists {len(trials)} trials but n_trials = {n}")
    sv = doc.get("sr_variance")
    if sv is not None and not (isinstance(sv, (int, float)) and sv >= 0):
        raise ValueError("sr_variance must be a number >= 0")
    return {"n_trials": n, "trials": trials, "sr_variance": sv, "study": doc.get("study")}


def decide(c: dict) -> tuple[str, str | None, list[str]]:
    fails = []
    if c["base_decision"] != "PASS":
        fails.append(f"base verdict {c['base_decision']}, not PASS")
    d = c["dsr"]["dsr"]
    if d is None or d < DSR_MIN:
        fails.append(f"DSR {d} < {DSR_MIN}")
    pb = c["pbo"]
    if not pb.get("not_applicable") and (pb.get("pbo") is None or pb["pbo"] >= PBO_MAX):
        fails.append(f"PBO {pb.get('pbo')} not < {PBO_MAX}" + (f" ({pb['reason']})" if pb.get("reason") else ""))
    if not c["fdr"]["bh_survives"]:
        fails.append(f"BH adjusted p {c['fdr']['bh_q']} > {FDR_Q}")
    h, b = c["hac"], c["bootstrap"]
    if not (h.get("t") is not None and h["t"] > 0 and h["p_one_sided"] <= ALPHA
            and b["mean"] is not None and b["mean"] > 0 and b["p_one_sided"] is not None
            and b["p_one_sided"] <= ALPHA):
        fails.append(f"HAC (t {h.get('t')}, p {h.get('p_one_sided')}) and block bootstrap "
                     f"(mean {b['mean']}, p {b['p_one_sided']}) do not both show a mean > 0 at p <= {ALPHA}")
    return ("PROMOTE", None, []) if not fails else ("HOLD", fails[0], fails)


def promote(results: list[tuple[str, dict]], trials_doc: dict, sources: dict | None = None,
            variants: dict | None = None, s: int = PBO_S, *,
            canonical_stats: dict | None = None, canonical_pbo: dict | None = None) -> dict:
    """Decision record from (name, doc) inputs, the trials ledger and the variants matrix (for PBO).
    Twin docs (with ``windows``) are candidates; ``doppel-genome ledger --json`` docs (with ``rows``)
    only add p-values to the family."""
    tr = load_trials(trials_doc)
    if tr["n_trials"] == 1:
        pb: dict = {"pbo": None, "not_applicable": True, "reason": "a single trial: nothing was selected"}
    elif variants is None:
        pb = {"pbo": None, "reason": f"no variants matrix for the {tr['n_trials']} trials"}
    else:
        ids, X = load_variants(variants)
        if len(ids) > tr["n_trials"]:
            raise ValueError(f"variants matrix has {len(ids)} variants but n_trials = {tr['n_trials']}: "
                             "the trials ledger must count every variant tried")
        pb = (canonical_pbo if canonical_pbo is not None else pbo(X, s)) | {"variant_ids": ids}
        if len(ids) < tr["n_trials"]:
            pb["note"] = f"matrix covers {len(ids)} of {tr['n_trials']} trials"
    cands, family = [], []
    for name, doc in results:
        if "windows" in doc:
            cands.append((name, doc))
        elif "rows" in doc:
            family += [(f"{name}:{r['genome_id']}", float(r["p_value"]))
                       for r in doc["rows"] if r.get("p_value") is not None]
        else:
            raise ValueError(f"{name}: neither a doppel-twin result (windows) nor a ledger (rows)")
    if not cands:
        raise ValueError("no doppel-twin result among the inputs")
    if len(cands) > tr["n_trials"]:
        raise ValueError(f"{len(cands)} candidates but n_trials = {tr['n_trials']}")
    rows = []
    for name, doc in cands:
        v = doc.get("verdict") or {}
        bs = v.get("bootstrap") or {}
        x = series(doc)
        diag = v.get("decision") == "DIAGNOSTIC" or (doc.get("twin") or {}).get("diagnostic")
        p = block_bootstrap_p(x, bs.get("block_windows", 12), bs.get("n_boot", 2000), bs.get("seed", 0)) \
            if len(x) > 1 else None
        ci = block_bootstrap_ci(x, None, bs.get("block_windows", 12), bs.get("n_boot", 2000), bs.get("seed", 0)) \
            if len(x) > 1 else (None, None)
        genome = doc.get("genome")
        scope = doc.get("range")
        if doc.get("schema") == "doppel.shadow-replay/v1":
            genome, scope = {"id": doc["genome_id"]}, v["evaluation"]["range"]
        stats = (canonical_stats or {}).get((genome or {}).get("id"))
        dsr = deflated_sharpe(x, tr["n_trials"], tr["sr_variance"])
        hac = newey_west(x)
        if stats is not None:
            p = stats["bootstrap_p"]
            dsr["dsr"] = stats["dsr"]
            hac.update(t=stats["hac_t"], p_one_sided=stats["hac_p"])
        rows.append({"input": name, "genome": genome, "range": scope,
                     "base_decision": "DIAGNOSTIC" if diag else v.get("decision"),
                     "base_reason": v.get("reason"), "base_p_value": v.get("p_value"), "windows": len(x),
                     "dsr": dsr, "pbo": pb, "hac": hac,
                     "bootstrap": {"mean": _r(float(np.mean(x))) if x else None,
                                   "p_one_sided": p if stats is not None else _r(p, 6),
                                   "ci95": [_r(ci[0]), _r(ci[1])], "block": bs.get("block_windows", 12)}})
    ids = {(r["genome"] or {}).get("id") for r in rows}
    def family_p(row):
        p = row["bootstrap"]["p_one_sided"]
        if p is None:
            return 1.0
        if canonical_stats is not None:
            return p
        # Keep the existing conservative convention for legacy, noncanonical reports.
        return max(p, row["base_p_value"] or 0.0)

    fam = [(f"candidate:{row['input']}", family_p(row)) for row in rows]
    for t in tr["trials"]:   # a listed trial that is one of the candidates is not counted twice
        if t.get("id") in ids:
            continue
        fam.append((f"trial:{t.get('id')}", float(t["p_value"]) if t.get("p_value") is not None else 1.0))
    fam += family
    if len(fam) > tr["n_trials"]:
        raise ValueError(f"{len(fam)} p-values in the family but n_trials = {tr['n_trials']}")
    padded = tr["n_trials"] - len(fam)
    ps = [p for _, p in fam] + [1.0] * padded
    q, keep = bh(ps)
    hq = holm(ps)
    for i, r in enumerate(rows):
        r["fdr"] = {"p": ps[i], "bh_q": _r(q[i], 6), "bh_survives": keep[i], "holm_p": _r(hq[i], 6),
                    "holm_survives": hq[i] <= ALPHA}
        stats = (canonical_stats or {}).get((r["genome"] or {}).get("id"))
        if stats is not None:
            r["fdr"].update(bh_q=stats["fdr_q"], bh_survives=stats["bh_survives"])
        r["decision"], r["binding"], r["failures"] = decide(r)
    return {"code_sha256": code_sha256(), "sources": sources or {},
            "params": {"dsr_min": DSR_MIN, "pbo_max": PBO_MAX, "pbo_s": s, "fdr_q": FDR_Q, "alpha": ALPHA},
            "pbo": pb,
            "trials": {"study": tr["study"], "n_trials": tr["n_trials"], "listed": len(tr["trials"]),
                       "family_size": len(ps), "padded_with_p1": padded, "sr_variance": tr["sr_variance"]},
            "family": {"bh_survivors": [fam[i][0] for i in range(len(fam)) if keep[i]],
                       "holm_survivors": [fam[i][0] for i in range(len(fam)) if hq[i] <= ALPHA],
                       "members": [{"id": n, "p": p, "bh_q": _r(q[i], 6), "holm_p": _r(hq[i], 6)}
                                   for i, (n, p) in enumerate(fam)]},
            "candidates": rows,
            "note": "statistical gate on top of the verdict; no orders, no registry write, owner authorization separate"}


def code_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _r(x, nd: int = 6):
    return None if x is None else round(float(x), nd)


def _canonical_trial_inputs(ledger, results, sources):
    trials = ledger["trials"]
    v2 = ledger.get("schema") == ABSTENTION_LEDGER_SCHEMA
    shared_input = None
    selected = {}
    for path, replay in results:
        if replay.get("schema") != "doppel.shadow-replay/v1":
            continue
        if v2:
            input_digest = replay["verdict"]["evaluation"]["input_sha256"]
            if shared_input is not None and input_digest != shared_input:
                raise ValueError("canonical v2 replays do not share the same observed inputs")
            shared_input = input_digest
            windows = [[row["start"], row["end"]] for row in replay["windows"] if row["eligible"]]
            if windows != ledger["windows"]:
                raise ValueError("canonical v2 replay and trial ledger do not share the same observed windows")
        gid = replay["genome_id"]
        eligible = series(replay)
        matches = [row["trial_id"] for row in trials if row["genome_id"] == gid and row["returns_c"] == eligible
                   and (not v2 or (row.get("disposition") == "PASS" and replay.get("trial_id") == row["trial_id"]
                                   and row.get("replay_sha256") == sources[path]))
                   and ("trial_id" not in replay or replay["trial_id"] == row["trial_id"])]
        if len(matches) != 1 or gid in selected.values():
            raise ValueError("canonical ledger does not uniquely identify each scored candidate")
        selected[matches[0]] = gid
    if not selected:
        raise ValueError("canonical ledger requires a canonical replay candidate")
    if len(selected) != len(results):
        raise ValueError("a canonical family accepts only canonical replay candidates")
    if v2 and not isinstance(shared_input, str):
        raise ValueError("canonical v2 replay does not bind a sealed input digest")
    abstentions = verified_abstentions(ledger, shared_input) if isinstance(shared_input, str) else set()
    variance, overfit, stats = canonical_trial_statistics([row["returns_c"] for row in trials], abstentions=abstentions)
    if v2:
        overfit["verified_abstentions"] = len(abstentions)
    summary = {"n_trials": len(trials), "sr_variance": variance, "trials": [
        {"id": selected.get(row["trial_id"], row["trial_id"]), "p_value": stat["bootstrap_p"]}
        for row, stat in zip(trials, stats, strict=True)]}
    candidates = {selected[row["trial_id"]]: stat for row, stat in zip(trials, stats, strict=True)
                  if row["trial_id"] in selected}
    variants = {"variants": [{"id": row["trial_id"], "net_c": row["returns_c"]} for row in trials]}
    return summary, variants, candidates, overfit


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "bundle":
        from .shadow_bundle import main as bundle_main
        return bundle_main(argv[1:])
    ap = argparse.ArgumentParser(prog="doppel-promotion")
    ap.add_argument("--results", type=Path, nargs="+", required=True,
                    help="doppel-twin output JSON(s); doppel-genome ledger --json files add p-values only")
    ap.add_argument("--trials", type=Path, help="canonical Shadow trial ledger or legacy research summary")
    ap.add_argument("--variants", type=Path,
                    help="PBO input: {variants: [{id, net_c: [...]}]}, every variant tried over the same windows")
    ap.add_argument("--pbo-s", type=int, default=PBO_S, help="CSCV partitions (even)")
    ap.add_argument("--out", type=Path, required=True, help="directory")
    a = ap.parse_args(argv)
    try:
        if a.trials is None:
            raise ValueError("--trials is required: the number of trials in the search must be recorded")
        raws = {p: p.read_bytes() for p in [*a.results, a.trials, *([a.variants] if a.variants else [])]}
        srcs = {str(p): hashlib.sha256(b).hexdigest() for p, b in raws.items()}
        results = [(str(p), orjson.loads(raws[p])) for p in a.results]
        trials = orjson.loads(raws[a.trials])
        variants = orjson.loads(raws[a.variants]) if a.variants else None
        stats = overfit = None
        ledger_schema = trials.get("schema")
        if ledger_schema in (CANONICAL_LEDGER_SCHEMA, ABSTENTION_LEDGER_SCHEMA):
            if variants is not None or a.pbo_s != PBO_S:
                raise ValueError("canonical Shadow promotion takes its complete variants and PBO defaults from the ledger")
            trials, variants, stats, overfit = _canonical_trial_inputs(trials, results, srcs)
        doc = promote(results, trials, srcs, variants, a.pbo_s, canonical_stats=stats, canonical_pbo=overfit)
        if ledger_schema == ABSTENTION_LEDGER_SCHEMA:
            doc["trials"]["ledger_schema"] = ledger_schema
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as e:
        print(f"REFUSED {e}", file=sys.stderr)
        return 2
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "promotion.json").write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2))
    for r in doc["candidates"]:
        print(f"{r['input']}: {r['decision']}" + (f" ({r['binding']})" if r["binding"] else "")
              + f" | DSR {r['dsr']['dsr']} | BH q {r['fdr']['bh_q']} | HAC t {r['hac'].get('t')}"
              f" | boot p {r['bootstrap']['p_one_sided']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
