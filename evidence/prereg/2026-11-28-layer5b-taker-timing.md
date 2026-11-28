# Layer 5b scripted taker timing protocol

This is an additive exam. The reserved Layer 5a scorecards and their hashes remain the recovery and abstention record. Their one-second taker entry gate is **UNMEASURED**. Layer 5a has no recoverable taker or paired predicted entry timestamps.

## Public data and private intent

Use two disjoint sets of complete BTC markets. Training must close before the first holdout decision. Publish the plan digest before running the exam. A real plan fixes the half-open receipt-clock intervals, exact public JSONL digests, probe cutoffs, feature producer revision and SHA-256, and distinct native source receipts for the BTC books, BRTI, Binance and Hyperliquid. Recompute source file hashes from the referenced closed captures. Retain the staging directory and verify feature provenance against the native receive clocks before accepting the plan. The lead probability must use a precommitted lead-to-BRTI transform derived from both offshore feeds. Keep its code and parameters fixed before holdout. A real sample requires overlapping sealed streams across both intervals and cannot be represented by the short commissioning probe.

One JSONL row per decision cutoff and market supplies a two-sided book, BRTI fair probability, and lead-adjusted probability with both independent lead witness clocks. An observation is available only when its input arrived strictly before the cutoff and at most three seconds earlier. The lead-derived witness cannot predate either constituent receipt. Probabilities and prices use dollars per contract. Recorded clocks are integer Unix nanoseconds in the recorder's local receipt domain. Sort by cutoff then market ID. Use nulls for unavailable data. Preserve the full scheduled panel and missing rows.

Two independent oracle policies emit private buy intentions before simulated execution:

- **scripted_lead_taker** compares the lead-adjusted probability for each side with the fee-adjusted ask. It uses a 0.02 edge threshold and selects the side with greater eligible edge. Use Up on equal eligible edges.
- **scripted_fair_taker** does the same with BRTI fair probability, a 0.03 edge threshold, and a selected-side maximum 0.06 bid-ask spread.
- Both policies emit an intention on entering an eligible state or switching eligible side. A stale or missing witness clears the current state. The fee model is the specified 0.02085 effective coefficient times ask × (1 − ask) per contract. This exam scores intent timing and does not estimate settlement profit.
- **random_taker_control** chooses an intention independently of prices and edges on fully observable cuts, with a fixed 0.05 probability per cut and an independent fair side. Its training and holdout seeds are 50128 and 50129 respectively. This supplies timed activity with no feature rule.

The fitter receives the training public panel and timed training intents in unlabeled subject files. The forecasting phase writes one model and one public-only prediction stream for each subject. Publish the SHA-256 of the forecast manifest before invoking the scoring phase. Only the scoring phase generates holdout intentions. The commitment binds all model and prediction bytes, the preregistered plan, and the oracle and fitter source bytes. A source change requires a new plan before unblinding.

## Scoring

Match predictions to owner intents within ±1,000 ms, one to one within market and side. Match the earliest compatible pair in chronological order, discarding rows that are too early to match the next available row. Compute hit precision, recall and F1 over every prediction and truth event. Report signed median predicted minus intent time and median absolute pair error in nanoseconds. Report whether median signed timing is nonpositive as the simultaneous-entry target.

For each positive subject, require correct fitted trigger family, at least 20 holdout intentions across at least 10 distinct markets, precision and recall each at least 0.90, and median absolute matched timing error no greater than one second. All four prerequisites must be available. The random control must have observable timed intentions and an explicit fitter abstention with zero predictions. Report null metrics as null.

Use three precommitted interior holdout probes. With the training artifact held fixed, rerun each prefix on its own and rerun the full stream after shifting and perturbing all strictly later rows. Both forecasts through the cutoff must exactly equal the original prefix. Require a nonempty, changed future at each probe. The perturbation can change later predictions. This tests the consumer's prefix invariance, and does not attest the independently generated upstream feature transform.

A synthetic result reports the software checks and always leaves the real timing gate **UNMEASURED**. For a recorded pass, bind the source receipts and verify exact forecast commitment bytes before scoring. The result must carry the separate lead-owned canonical provenance for shadow admission. Never publish account data or raw tape in a public review branch.

## Reproduction interface

Stage the evidence off the recorder host. `training-public.jsonl` and `holdout-public.jsonl` live under `INPUTS`. Save `plan.json` using schema `doppel.layer5b-plan/v1`. It has:

- `source_kind` of `synthetic` or `recorded`.
- `training_range_ns` and `holdout_range_ns` as half-open clock pairs, `public_sha256` keyed by the two public filenames, and `probe_cutoffs_ns` containing three ordered interior holdout clocks.
- `source_receipts` of `{}` for synthetic fixtures. A recorded plan binds four role receipts by relative path and SHA-256. Each receipt has schema `doppel.layer5b-source/v1`, `role` of `books`, `brti`, `binance` or `hyperliquid`, `clock` of `local_receive_ns`, and a nonempty `segments` list. Each segment records its relative `path`, native `stream`, `sha256`, and half-open `covered_range_ns`. The scorer opens each native `.dtape.zst`, verifies complete frames and payload CRCs, checks stream names, and verifies actual payload clock support for the declared interval. The exact staging manifests remain independently verified by the lead before signing the admission evidence.
- Recorded plans bind `feature_producer` with `revision`, relative `path` and `sha256`. This file defines the feature code, parameters and raw-to-panel provenance, reviewed and pinned separately from the forecast output.

Run the first command and independently publish the manifest SHA before running the second. Direct git publication may use digests only.

```sh
python -m doppel.lab.layer5b --plan plan.json --plan-sha256 "$PLAN_SHA" --inputs INPUTS \
  forecast --out FORECASTS
python -m doppel.lab.layer5b --plan plan.json --plan-sha256 "$PLAN_SHA" --inputs INPUTS \
  score --forecasts FORECASTS --forecast-sha256 "$FORECAST_SHA" --out SCORE.json
```
