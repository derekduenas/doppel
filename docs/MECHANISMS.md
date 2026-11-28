# Mechanism studies (STATE-AND-PLAN §9.2 item 4)

Code: `src/doppel/lab/mechanisms.py` (`doppel-mechanisms`); the fill markout curve is
`src/doppel/lab/markout.py` (`doppel-markout`). These are hypotheses to preregister, not findings.
No strategy search: every grid below is fixed and its size is reported in the output.

## Common rules

- **Clock.** All decisions use only data received by the decision time (`tw`). The book at t is the
  last book received at or before t, and a gap record after it makes the book unknown. The BRTI at t
  is the value received by t, using the twin's 2 s delay rule. fair-v1 is `fair_up_asof` with the
  opening known by t.
- **Statistics.** CIs are percentile bootstraps that resample 15-minute clock blocks (`start // 900`).
  Each study has ONE headline statistic per grid cell, tested at level `1 - 0.05 / grid_size`
  (Bonferroni over the stated family). Everything else is informational at 95%.
- **Execution (every net number).** A take decided at t walks the L2 book received by t for exactly
  `size` contracts, giving the decision VWAP. It executes 250 ms later on the book received by
  t + 0.25 s. It fills only if that book holds the full size at a VWAP at or better than the decision
  VWAP. Otherwise it is **missed**: there is no partial fill and no "the top level holds it".
  - Sizes are 25, 100 and 500 contracts. That gives a capacity curve, with the fill rate (filled /
    attempted) next to the net EV per filled attempt.
  - Each level crossed is one fill with its own cent-rounded fee.
  - Marks are the received mid, or the settlement at or after the end. They are not an exit cost.
- **Gross headlines.** Gross headlines (`flb`, `overreaction`, `opening`) are measured from the
  mid. They do not depend on whether our order filled, so the sample is not selected by fills.

## Fee tiers (four columns, never one)

| column | PM US taker coef | rebate | 50 c, 100 contracts | applies to |
|---|---|---|---|---|
| `gateway_0095` | 0.095 | 0 | 2.38 c/ct | the gateway's reported `feeCoefficient`; worst case if that is what is charged |
| `api_base_0695` | 0.0695 | 0 | 1.74 c/ct | API taker orders (E25) after the rebate ends (from 2027-01-01) |
| `api_live_rebated` | 0.0695 | 70% of the cent-rounded fee | 0.522 c/ct (≈0.02085·p(1−p)) | API taker orders through 2026-12-31: **our order path today** |
| `maker_0` | 0 | — | 0 | resting (maker) orders |

- **Source.** The `PMUS_COEF` comment in `src/doppel/lab/xvenue.py` ("gateway feeCoefficient; docs:
  API takers 0.0695 (+70% rebate to 2026-12-31)"), and E25. The rebate is modelled exactly as
  `xvenue.pmus_fee(rebate=0.70)` does: 30% of the cent-rounded fee.
- **Not verified.** None of the tiers has been reconciled to statements. The timing of the rebate
  (paid at the trade or later) is unverified.
- **`maker_0` on a crossing fill.** On a take, `maker_0` is a zero-fee upper bound, not a passive-fill
  model. There is no queue and no adverse selection of resting orders.
- **Reading a verdict.** A take has a positive EV under the current API path when `api_live_rebated`
  holds. It survives the end of the rebate only if `api_base_0695` holds, and survives any fee
  reading only if `gateway_0095` holds.

## (a) `flb`: favourite-longshot

- **Observation.** At start + f·L, f ∈ {0.25, 0.5, 0.75}: the mid, the ask and the outcome y.
- **Headline (gross).** The OLS slope b of (100y − mid) on (mid − 50). b > 0 means favourites are
  underpriced and longshots overpriced.
- **Informational.** Results per 5 c bucket: the YES rate, y − mid, and y − ask. Net to settlement of
  buying YES (at the ask VWAP) and buying NO (selling YES at the bid VWAP), at 100 contracts for each
  tier. The capacity curve for each size.
- **Prereg stub.**
  - H: b ≠ 0.
  - Grid: f (grid_size 3).
  - Threshold: a Bonferroni CI at 0.9833 that excludes 0.
  - Tier: gross.
  - Sample: ___ (dates, horizons) to be fixed before the run.

## (b) `final60`: final-minute mispricing vs fair-v1 (BTC only)

- **Observation.** At end − o, o ∈ {50, 30, 10} s: mid − fair, |mid − fair|, and the Brier difference.
- **Trade.** The trade is gated per tier and size: buy when fair − VWAP_ask − fee > 1 c, sell when
  VWAP_bid − fair − fee > 1 c. It executes under the latency rule and is held to settlement.
- **Headline.** Net c/contract per filled trade at 100 contracts, under each tier.
- **Prereg stub.**
  - H: the gap pays under tier T.
  - Grid: offsets × tiers (grid_size 12).
  - Threshold: a Bonferroni CI at 0.99583 with a lower bound > 0, **stated per tier**
    (`effect_by_tier`). There is no verdict collapsed across tiers.
  - Informational: 25 and 500 contracts (capacity), with `no_depth` and fired / filled counts.
  - Sample: ___.

## (c) `overreaction`: sharp moves outside the opening

- **Window.** The first trigger per window in **[start + 45 s, end − 60 s)**. The first 45 s belong
  to `opening`. A 50 c cross that happens inside the mask is consumed, not deferred.
- **Triggers.**
  - `cross50`: the received mid crosses 50 c.
  - `z1.5`, `z2.5`: on the integer receive-second grid, |z(s)| ≥ Z, where

    z(s) = [ln B(s) − ln B(s − 60)] / (σ(s) · √(end − s))

  - B(s) is the BRTI received by s.
  - σ(s)² is the mean squared 1-s log return of B over returns ending in (s − 960, s − 60]. That
    window lies strictly before the 60 s move, so a jump cannot inflate its own scale.
  - A z value needs ≥ 450 finite returns and σ > 0; otherwise there is no trigger.
  - z is the move's shift in the standardized distance to strike over the time remaining. Under
    pure diffusion its noise level is ≈ √(60/τ), so late-window triggers are more frequent by design.
- **Direction.** dir = sign of the move.
- **Headline (gross).** dm(+60 s) = dir · (mid(t + 0.25 + 60) − mid(t + 0.25)). Below 0 reads as
  overreaction (fading pays before costs); above 0 reads as momentum.
- **Informational.** dm at +10 s and at settlement. Follow and fade nets at +10 s, +60 s and
  settlement, for each size and tier.
- **Prereg stub.**
  - H: dm(+60 s) ≠ 0.
  - Grid: {cross50, z1.5, z2.5} (grid_size 3).
  - Threshold: a Bonferroni CI at 0.9833 that excludes 0.
  - Tier: gross.
  - Sample: ___.

## (d) `opening`: the first 45 s (BTC only)

- **Observation.** At start + o, o ∈ {10, 30} s: mid vs fair-v1. That covers the signed error,
  |error| and the Brier difference.
- **Direction.** dir = sign(mid − fair). Windows where fair-v1 or the book is not yet known (for
  example, the opening has not been received) are counted in `unknown_fair_or_book`, not dropped
  silently.
- **Headline (gross).** dm(+60 s), as in (c). Below 0 means the opening gap corrects toward fair-v1.
- **Legs.** Fade (trade toward fair) and follow nets, for each size and tier.
- **Prereg stub.**
  - H: dm(+60 s) ≠ 0.
  - Grid: o (grid_size 2).
  - Threshold: a Bonferroni CI at 0.975 that excludes 0.
  - Tier: gross.
  - Sample: ___.

## Running on real tape

```
TAPE=/mnt/volume_nyc1_1793485122853/doppel/tape
doppel-mechanisms --data $TAPE --from 2026-11-25T00:00:00Z --to 2026-11-27T00:00:00Z \
    --assets btc --horizons 5m,15m --study flb          --out out/mech-flb.json
#   ... --study final60 / overreaction / opening (or --study all), same arguments
```

`--from` and `--to` take unix seconds or ISO (`twin.cli.parse_ts`). Add `--boot` and `--seed` for the
bootstrap.

## Limitations

- **Venue.** PM US only. The Kalshi and intl books are recorded, but their settlement labels and the
  intl fee function are not wired in.
- **Execution model.**
  - The book walk assumes our order alone takes the displayed size, with no queue and no hidden
    liquidity.
  - The latency rule compares VWAPs. It does not model a real IOC limit order, which could fill at
    the landing book up to a worst-level limit price.
- **Volatility.** σ for z comes from received BRTI values. Repeated values (feed gaps of 30 s or
  less) count as zero returns and bias σ down.
- **Fees.** The tiers are not reconciled to statements, and the rebate end date is taken as stated.
