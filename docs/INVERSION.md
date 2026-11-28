# Latent fair-value inversion (`doppel-invert`)

STATE-AND-PLAN 9.2 item 1. Module `src/doppel/lab/inversion.py`, tests `tests/test_inversion.py`.
Research only: no orders, no registry writes, and it does not touch frozen scoring files.

## What it estimates

Each wallet decision is an inequality on the wallet's hidden probability p_w that Up wins, net of
the taker fee per contract on the wallet's own venue:

| Decision | Inequality |
|---|---|
| bought Up at ask a | p_w ≥ a + fee(a) |
| sold Up (bought Down) at bid b | p_w ≤ b − fee(b) |
| pass on an executable book | b − fee(b) < p_w < a + fee(a) |
| maker post filled at bid b / ask a | p_w ≥ b / p_w ≤ a. **Censored** (see below) |

Model: p_w = σ(logit fair_v1 + β·x + β0). The inputs are the following:

- `fair_c` is taken as-is from the input row. In rulefit it is the twin's `Features.fair` on BRTI
  received by the cutoff. `fair_up_raw` is never used.
- x holds features known at the decision: `lead`, `basis` and `drift` when the row has them,
  book `imbalance` from the depths, `spread_c`, and `tte_s`.

Each executable side of a sampled moment becomes one row, with y = 1 when that side was taken.
Under logistic decision noise with scale T, P(y = 1) = σ(margin / T). That is a logistic regression
with design `[u, 1, s·x, s]`, where u = s·(logit fair − logit threshold) and s = ±1 for the Up and
Down sides.

- **T is fitted, not tuned.** 1/T is the coefficient on u, and β = γ_x / γ_u.
- **The plain intercept absorbs the analyst's pass-sampling ratio** (case-control).
- **The s-signed column is the constant belief offset β0.**
- **Fitting:** Newton with L2 = 1 on the standardized feature and bias coefficients, numpy only.

**Fees** come from the repo's models: `xvenue.pmus_fee(p, q)/q` (PM US, cent half-even) and
`xvenue.kalshi_fee(p, q)/q` (Kalshi, ceil to cent), the same per-contract convention as `bridge.Cfg`.
The repo has no intl fee model (rulefit: "intl fees are not fitted"), so `--venue intl` requires
`--fee-coef` and uses `twin.engine.fee_usd`. No venue is assumed by default. A take uses its own
size. A pass uses the median take size, which is reported as `reference_size_for_passes`.

## Hard negatives

A pass is a `none` moment that meets all of the following:

- it falls in a window where the wallet took at least once;
- fair is known;
- the side has depth > 0;
- the wallet is **not at its observed maximum position** on that side (`inv + q` above the
  maximum post-decision inventory seen in fit windows). Turn this off with `--no-position-filter`.

Each take is matched to at most `--ratio` (3) passes from the same window and the same 60 s
time-to-expiry bucket, using a fixed seed. Because of that matching, the `tte_s` coefficient cannot
be interpreted and is never claimed.

## Validation and verdict

- **Split:** whole windows, chronological, 70 % fit and 30 % held out.
- **Coefficient CIs:** 95 % percentile bootstrap over fit windows. Per-decision p_w bands come from
  the same draws.
- **Held-out margin accuracy:** the share of held-out inequalities satisfied by the fitted belief.
  The sampling intercept is excluded. The score is *balanced*: the mean over taken and not-taken
  rows, so it does not depend on the pass ratio. Plain accuracy and the take/non-take splits are
  also reported.
- **Baselines:** g ≡ 0 (pure fair_v1) and market mid, scored against the same fee-adjusted
  thresholds. Mid sits inside the spread, so it never predicts a take and scores 0.5 balanced.
  The accuracy-difference CIs bootstrap held-out windows.

**IDENTIFIED** requires all four of the following:

- at least 30 takes in the fit;
- at least 30 certified pass rows in the fit;
- a margin slope 1/T whose CI is above 0;
- held-out accuracy whose CI lower bound beats both baselines.

Anything else is **NOT_IDENTIFIED**, with reasons. `claimed_features` lists only the features whose
β CI excludes 0 in an IDENTIFIED result.

## Input, and what real data lacks today

Rows use rulefit's `decision_table.jsonl` STATE_COLS (`bb_c`, `ba_c`, `depth_bid`, `depth_ask`,
`fair_c`, `tte_s`, `inv`, `window`, `window_start_ns`, Up-exposure cents). Taker decisions add
`action` = `take` (with `up_side`, `px_c`, `size`) or `none`.

| Source | How it is read |
|---|---|
| rulefit `decision_table.jsonl` (`kind: order`) | maker posts, censored |
| Layer 3 `wallet-dissection/1` `orders.jsonl` (`context.signed.state`) | maker posts, censored, in signed-token terms. Layer 3 carries no fair, tte, inventory or outcome index |
| `action` rows | taker decisions and passes, fitted |

**No existing output writes taker moments or pass moments with state.**

- rulefit's `takes` have no book, fair or price.
- `market_opportunities.opportunity_panel` cells have `mid_c` only, with no bid, ask, depth or fair.
- Layer 3 covers maker orders only.

So on real wallets today the tool returns NOT_IDENTIFIED and names the missing fields
(`missing_fields`). The next data step is a pass and taker panel with the STATE_COLS above. That
step belongs in rulefit's decision table, not in a new data path.

## Confounds (also in every output)

1. A pass may be a capital, position or attention limit, not disagreement. The max-position filter
   removes only limits that are visible in the wallet's own inventory path.
2. Inversion gives a band on p_w per decision, not a point.
3. Maker cancels and unfilled quotes are unobserved. Posts have no negatives, so they are scored
   for band consistency only, never fitted. Makers go to the strategy simulator.
4. β0 and the sampling intercept separate only for a wallet that takes on both sides.
5. A feature missing from the input, such as a private feed, appears as a larger T, never as a
   coefficient.

## Answer key (`inversion.answer_key`, seeds 1, 2 and 3, 100 bootstraps)

**Setup.** Each seed has 60 synthetic 15-minute windows with one moment every 10 s:

- fair follows a logit random walk;
- the market mid is σ(logit fair + N(0, 0.3));
- the spread is 1–2 c;
- depths are random, and book imbalance is a decoy with no effect;
- the PM US fee is the default coefficient, with size 10.

The true belief is p_w = σ(logit fair + 1.0·lead), with logistic decision noise T = 0.25.

**Results.** Each cell is the estimate with its 95 % CI.

| Wallet | Seed | Verdict | β_lead | T | Held-out balanced acc: model / fair_v1 / mid |
|---|---|---|---|---|---|
| taker (β=1) | 1 | IDENTIFIED, claims `lead` | 0.980 [0.905, 1.063] | 0.246 [0.225, 0.265] | 0.881 / 0.564 / 0.500 |
| | 2 | IDENTIFIED, claims `lead` | 0.993 [0.920, 1.066] | 0.247 [0.224, 0.266] | 0.874 / 0.569 / 0.500 |
| | 3 | IDENTIFIED, claims `lead` | 1.008 [0.933, 1.097] | 0.261 [0.240, 0.282] | 0.883 / 0.577 / 0.500 |
| random | 1–3 | NOT_IDENTIFIED (1/T CI reaches 0; no gain over fair_v1) | unstable (e.g. 0.60 [−2.5, 11.3]) | 3.6–22 (CI includes negative values) | 0.47–0.52 / 0.52–0.54 / 0.500 |
| pure fair_v1 | 1 | NOT_IDENTIFIED (no gain over fair_v1), no claims | 0.008 [−0.012, 0.026] | 0.234 | 0.679 / 0.675 / 0.500 |
| | 2 | same | −0.008 [−0.024, 0.008] | 0.249 | 0.671 / 0.675 / 0.500 |
| | 3 | same | −0.007 [−0.019, 0.012] | 0.261 | 0.694 / 0.697 / 0.500 |
| capped ±20, no filter | 1 | IDENTIFIED | 1.023 [0.926, 1.157] | **0.458 [0.413, 0.503]** | 0.828 / 0.553 / 0.500 |
| | 2 | IDENTIFIED | 1.040 [0.905, 1.165] | **0.478 [0.414, 0.532]** | 0.821 / 0.555 / 0.500 |
| | 3 | IDENTIFIED | 0.993 [0.881, 1.127] | **0.491 [0.434, 0.549]** | 0.834 / 0.570 / 0.500 |
| capped, max-position filter | 1 | IDENTIFIED | 0.975 [0.898, 1.087] | 0.245 [0.222, 0.269] | 0.883 / 0.560 / 0.500 |
| | 2 | IDENTIFIED | 0.985 [0.905, 1.065] | 0.243 [0.217, 0.261] | 0.881 / 0.568 / 0.500 |
| | 3 | IDENTIFIED | 0.996 [0.917, 1.087] | 0.263 [0.240, 0.286] | 0.883 / 0.579 / 0.500 |

**Findings.**

- **Taker and decoy.** True β = 1 falls inside the CI on all three seeds. The decoy imbalance
  feature is never claimed.
- **Random wallet.** It never gets a verdict or a feature.
- **Pure fair_v1 wallet.** β ≈ 0 and nothing is claimed. On seed 3 the imbalance CI
  [0.001, 0.067] just excludes 0. It is not claimed, because the verdict is NOT_IDENTIFIED. Treat
  it as the expected rate of marginal exclusions, not as a signal.
- **Capital-limited wallet.** The bias from forced passes shows up as **decision noise**:
  T is roughly doubled (0.46–0.49 against a true 0.25, with the CI excluding the truth) and
  held-out accuracy falls. β itself stays roughly unbiased, because the forced passes shrink the
  price and feature terms by the same factor, so their ratio β is preserved. The max-position
  filter removes more than 90 % of the T bias and restores a CI that contains 0.25.

**Disclosure.** "Most of the bias" was measured on T, and the threshold (filtered bias ≤ 0.5 ×
unfiltered) was set after seeing that β was not the biased quantity.

**Deviation from the brief: balanced accuracy.** The verdict gates on balanced accuracy, not the
plain share of satisfied inequalities. Plain accuracy depends on the pass ratio the analyst
chooses. Under balanced accuracy the mid baseline sits at 0.5 by construction, so beating mid is
only a chance-level bar. The real bar is beating fair_v1.

Every verdict above is unchanged under plain accuracy, which is also in every output. Plain
model / fair_v1 / mid by wallet:

| Wallet | Model | fair_v1 | mid |
|---|---|---|---|
| taker | 0.88 | 0.58–0.59 | 0.54 |
| pure fair | 0.70–0.72 | 0.71–0.72 | 0.59–0.62 |
| random | 0.49–0.61 | 0.62–0.64 | 0.85 |

**Low-noise wallets (stated limitation, not tuned away).** With noise-free decisions the data is
nearly separable, so the ratio β = γ_x / γ_u is set partly by the L2 penalty and comes out about
1–2 % low. These runs use seeds 1–3 with 50 bootstraps.

| Noise T | β CI covers 1.0 | Notes |
|---|---|---|
| 0 | 2 of 3 seeds | seed 3: 0.979 [0.963, 0.995]; fitted T ≈ 0.06 |
| 0.05 | 3 of 3 seeds | seed 1 also claims the decoy `imbalance`, a false claim |

A symmetric penalty that also covers 1/T overshoots instead (β 1.03–1.05, with the CI excluding 1
on most seeds), so the asymmetric penalty is kept. On a near-deterministic wallet, treat β CIs as
too narrow by about the 2 % bias.

**Not tested here.**

- A capped wallet whose cap correlates with a feature. That could bias β itself.
- Real books.
- The forward test.
