# Promotion gate: deciding whether anything leaves research

Code: `src/doppel/lab/promotion.py`, CLI `doppel-promotion`. Tests: `tests/test_promotion.py`.
The fidelity ghost test lives in `src/doppel/lab/fidelity.py` (`ghost`), tests in `tests/test_fidelity.py`.

## What it is and is not

- **On top of the verdict, never instead of it.** A candidate can be promoted only when its own
  `doppel-twin` verdict is already `PASS`. A `DIAGNOSTIC` run is never promotable.
- It does not touch `registry/genomes.jsonl`, the family Holm ledger, sequential monitoring or the
  commercial hurdle. Owner authorization stays a separate decision. No orders.
- It answers one question: after counting **every** trial that was tried to find this candidate, is
  the edge still distinguishable from the best of that many lucky nulls?

## Usage

```
doppel-promotion --results TWIN.json [TWIN2.json ...] [LEDGER.json] --trials TRIALS.json --out DIR
```

- Inputs with `windows` are `doppel-twin` outputs. These are the candidates.
- Inputs with `rows` (`doppel-genome ledger --json`) only add their recorded p-values to the
  multiple-testing family. `monitor --json` output has no per-window series and is not accepted.
- Output: `DIR/promotion.json`, a decision record with every statistic, the binding reason and the
  sha256 of every input file and of `promotion.py`.

### The trials ledger (required)

```json
{"study": "wallet-cohort-2026-12", "n_trials": 140,
 "trials": [{"id": "0xabc…", "kind": "wallet", "p_value": 0.31}, {"id": "variant-7", "p_value": null}],
 "sr_variance": 0.0004}
```

- `n_trials` is the number of strategy variants, wallets and parameter sets tried in the search that
  produced the candidates. It is an explicit, recorded input. **Without it the CLI refuses** (exit 2,
  nothing written). It is also refused when it is smaller than the trials listed, the candidates
  supplied, or the p-value family assembled from the inputs.
- `trials` lists what was tried. A listed trial with no p-value counts as p = 1.
- `sr_variance` (optional) is the variance of the per-window Sharpe estimates across the trials.

## The statistics

**Series.** Per-window net cents of the eligible windows (`net_c`), with a window that did not trade
counted as 0, in (end, start) order. This is the series the verdict bootstraps, put in time order
(the verdict itself does not sort, and HAC needs time order). Sharpe is per window, not annualised.

### 1. Deflated Sharpe Ratio (Bailey & López de Prado 2014)

- SR = mean / sd (ddof 1). Skewness g3 and kurtosis g4 come from population moments; g4 is Pearson
  kurtosis (3 for a normal).
- The expected maximum Sharpe of N null trials is
  SR0 = sqrt(V) · ((1 − γ) Φ⁻¹(1 − 1/N) + γ Φ⁻¹(1 − 1/(N e))), with γ the Euler–Mascheroni
  constant. SR0 = 0 when N = 1.
- V is the cross-trial variance of Sharpe. It is `sr_variance` from the ledger when recorded,
  otherwise the null estimator variance 1/(T − 1). When both exist the larger is used, so the
  deflation is never weaker than the null's.
- DSR = Φ((SR − SR0) √(T − 1) / √(1 − g3 SR + (g4 − 1)/4 SR²)).
- **Gate: DSR ≥ 0.95.** With T < 3 or zero variance the DSR is null, and null fails.
- Known answer: the paper's example (N = 100, V = ½ annualised, T = 1250, SR 2.5 annualised,
  skew −3, kurtosis 10) gives SR0 = 0.1132 per day and DSR = 0.9004. The test reproduces both.
- Caveat: the formula assumes independent windows. Overlapping horizons make the effective T
  smaller than the window count, so DSR is optimistic on overlapping data. Row 3 is the overlap-robust check.

### 2. False discovery rate across the study

- The family is made of:
  - each candidate's p: the larger of the time-ordered block-bootstrap p and its verdict's
    `p_value`;
  - each trial's p from the ledger (a trial that is one of the candidates is not counted twice);
  - each p from an imported ledger.
- The family is padded with p = 1 up to `n_trials`. A trial whose result was not recorded counts as
  a failure, the same rule the registry applies to pending genomes. Leaving trials out cannot help.
- Benjamini–Hochberg step-up at q = 0.05. **Gate: the candidate survives BH.** Holm
  (`registry.holm`) is reported beside it for comparison, along with each method's survivors.
- Known answer: on the Benjamini & Hochberg (1995) 15-value list, BH rejects 4 and Holm rejects 3.

### 3. Overlap-robust mean: Newey–West HAC vs block bootstrap

- The HAC t is the mean over sqrt(LRV / T), where
  LRV = γ0 + 2 Σ_{j<S} (1 − j/S) γj (Bartlett kernel; autocovariances divided by T).
- The bandwidth comes from the Andrews (1991) AR(1) plug-in:
  S = 1.1447 (a T)^{1/3}, with a = 4ρ² / ((1 − ρ)² (1 + ρ)²).
  ρ is the lag-1 autocorrelation, clipped to ±0.97. The p-value is one-sided, from the normal.
- The record shows this next to the verdict's centred moving-block bootstrap, recomputed on the
  time-ordered series with the verdict's own block, n_boot and seed: its one-sided p, mean and 95% CI.
- **Gate: they must agree.** Both must show mean > 0 with one-sided p ≤ 0.05.
- Known answer: on a 200k-step AR(1) with φ = 0.5, the LRV estimate is within 10% of σ²/(1 − φ)² = 4.
  Bartlett is biased low in finite samples. The exact test is against the Bartlett sum by definition.

### 4. Probability of Backtest Overfitting (Bailey, Borwein, López de Prado & Zhu 2015)

- **Input.** `--variants VARIANTS.json`, `{"variants": [{"id": …, "net_c": [...]}, …]}`. It holds the
  per-window net cents of **all** M variants tried (strategy variants, simulator or forge candidates)
  over the same windows, in time order. M must be at least 2. The trials ledger must count every
  one of them: a matrix with more variants than `n_trials` is refused.
- **Method: CSCV.** The T rows are cut into S = 16 contiguous equal blocks (`--pbo-s`, even); the
  trailing T mod S rows are dropped. For each of the C(16, 8) = 12,870 ways to choose 8 blocks as
  in-sample:
  - take the variant with the best in-sample Sharpe;
  - find its out-of-sample relative rank w = rank / (M + 1), where 1 is worst and ties get the
    average rank;
  - record its logit ln(w / (1 − w)).
- **Result.** PBO is the share of logits ≤ 0, i.e. how often the in-sample winner lands at or below
  the out-of-sample median. The record also carries the logit distribution (mean, quantiles,
  10-bin histogram).
- **Gate: PBO < 0.15.**
  - Not applicable when `n_trials` = 1, because nothing was selected.
  - With more trials and no matrix, PBO counts as not measured and the row fails.
  - PBO describes the selection process of the study, so it is the same for every candidate in a run.
- Known answers: 20 pure-noise variants give PBO ≈ 0.5 (mean over 5 seeds within 0.1). One
  variant with a +0.3 sd edge among 19 noise variants gives PBO < 0.05.

## Decision

- **PROMOTE** requires every row to pass. Otherwise the decision is **HOLD**, and `failures` lists
  every failing row.
- `binding` names the first failure in this fixed order: base verdict, DSR, PBO, BH, HAC/bootstrap
  agreement.

## Fidelity ghost test

`doppel-fidelity` now also reports `ghost`:

- `ghost.windows` counts the comparable windows where the clone traded and the wallet had no
  in-window fill. A pre-open position alone does not count as the wallet trading.
- `ghost.pnl_per_window` is the clone's mean net P&L there (USD, the genome's fee model), with a
  95% CI. The CI resamples distinct window start times, as the other fidelity scores do.
- If the whole CI is below 0, the document's top-level `warnings` gets `ghost_negative`.

It **does not gate PASS** by itself. It answers a different question from the fidelity scores. Those
ask whether the clone behaves like the wallet; the ghost asks whether the rule makes money when it
acts on its own.

How to read it:

| Ghost result | Reading |
|---|---|
| Few or no ghost windows | A faithful clone. The test has nothing to say. |
| CI around 0 or above | Consistent with a structural rule: its own extra trades are not losers. |
| Whole CI below 0 (`ghost_negative`) | The clone loses wherever it trades without the wallet. Its P&L on the shared windows likely comes from fitting this one wallet's timing (overfitting to one wallet), not from an edge in the rule. Do not promote the rule on its own merits. At most it is a sensor that copies the wallet. |

A negative ghost on a fidelity PASS is a reason to keep the clone as a copy of the wallet, not
as a standalone strategy. In the latter case the promotion gate above still has to be cleared on the
clone's own twin results.

## Fidelity disagreement P&L audit

`disagreement` extends the ghost into three buckets of comparable windows. Each bucket reports the
mean USD per window with the same start-time bootstrap CI, under the genome's fee model for both
sides:

| Bucket | Windows | P&L reported |
|---|---|---|
| `agreed` | both traded | the clone's and the wallet's |
| `wallet_only` | the wallet traded, the clone did not | the wallet's: what the clone left on the table |
| `clone_only` | the clone traded, the wallet did not (the ghost) | the clone's |

Hints in `disagreement.hints` are decided by which side of 0 the whole CI falls on. They are
informational only and never PASS or FAIL by themselves.

| Hint | Rule | Reading |
|---|---|---|
| `clone_captures_wallet_edge` | wallet-only CI contains 0 or is below 0 | The wallet earns nothing where the clone stands aside. The clone already captures the wallet's edge, so stop chasing match rate. |
| `structural_edge_beyond_wallet` | clone-only CI above 0 | The rule earns on its own trades too. |
| `missing_rule` | wallet-only CI above 0 **and** clone-only CI below 0 | The wallet earns where the clone is silent, and the clone loses where the wallet is silent. A rule is missing and worth investigating. |

A bucket with fewer than 2 distinct start times has no CI and gives no hint.
