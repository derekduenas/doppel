# Layer 0 · Wallet selection

A wallet enters through a public identity-bearing fill on either RTDS socket. The scan
combines the two streams, preserves repeated identical fills within a socket, and counts
BTC, ETH, SOL and XRP five- and fifteen-minute activity. A new cohort freeze uses both event
time and local receipt time. It records the sealed input segments, scan digest, activity
ranking, control draw and freeze clock. The existing v1 cohort predates those safeguards.
Its original membership digest excludes its clock. Reproducing v1 also requires the
independently pinned bytes of its original scan and cohort file.

## From an address to a decision

1. **Intake.** Rank eligible wallets by observed notional. The discovery cohort contains
   the top 100 and a seeded pool of 50 from a lower activity band. Membership alone shows
   that the wallet was observed. The same public address can be a ledger test subject.
2. **Match before observing returns.** Run `doppel-select freeze` on the pinned scan and
   cohort. A wallet needs at least one eligible control. The policy takes up to three closest
   controls within 2× on notional and number of markets, 10 cents on median print price,
   0.25 on extreme-price share, and 0.25 total variation each on asset and horizon mix.
   The older scan reports asset and horizon counts separately, so activity at each specific
   asset–horizon combination can still differ. Record the printed plan hash independently
   before the first scoring window. A hash supplied by its author after observation cannot
   establish a prospective comparison.
3. **Observe four disjoint weeks.** The first week begins at the first UTC quarter-hour
   boundary after the plan freeze. For each week, store the candidate's and every assigned
   control's `doppel-absorb` receipts in `REPORTS/<start Unix seconds>/wallet-<address>.json`.
   The absorption request must use those exact half-open UTC bounds for each member. Retain
   complete pagination, closing and opening balances, condition-level trade cash, complete
   binary token metadata and resolution. The scorer verifies both token legs and the cash
   attributed to each qualifying event. A proven idle wallet contributes zero. Missing
   observations stay unknown.
4. **Decide.** `doppel-select wallet` grades the candidate against the equal-weight mean of
   its frozen controls. A pass requires complete evidence for all four weeks, positive
   candidate profit and positive excess in at least three, positive total candidate profit,
   and a positive lower 95% event-cluster bootstrap bound on paired excess. The bootstrap
   resamples whole conditions within each week using 2,000 draws with a fixed seed. The
   measured quantity is settled trading P&L after fill fees in the scoped crypto windows.
   Unattributed rebates, transfers of capital, other venues and external hedges are outside
   the profit measure. Unsupported in-scope share transfers or ambiguous cash prevent a verdict.

An individual pass hands a wallet to **Stage 1, Absorb**, for full-history research. It
cannot identify a strategy or establish the cohort-wide top-decile skill claim. The
programme-wide G0 ledger sample, PIT skill rankings, operator identity and a cohort-wide
matched G1 analysis have their own evidence requirements. Existing diagnostic tools can
inspect any wallet, including an unmatched wallet. Their outputs carry no Stage 0 promotion.
The older programme blueprint also calls for mechanism-adjusted returns. Ranking all
operators under that specification still needs attributable income, mechanism and
operator controls, and a prospective population comparison.
In the old census, `eligible` meant volume coverage on at least one interval. It could
coexist with a failed or noncomparable G0. The new decision reads reconciled weekly
observations directly. When auditing Stage 1, trace the frozen plan hash, candidate address
and decision into the production handoff. Manual dossier and extraction commands are still
available as research tools.

## Prospective walkthrough choice

For this first v1 study, follow **the highest-ranked candidate with an eligible frozen
control**. Keep that choice if it later fails. Also report the previously proposed pilot,
`0x0cb038487586d1119b165466072e9baf666f3a90`, regardless of control availability.
The primary demonstrates the full decision path without picking a winner after the fact.
Additional wallet comparisons are individual research results. Selecting a profitable
subset afterward would require separate validation.

## Improving control coverage on future cohorts

The original intake draws all controls from lower activity ranks. Large candidates often
have no comparable control in that pool. For a new study, the `--cohort-design interleaved`
option to `doppel-scan` can allocate one seeded control and two candidates in each successive
group of three activity ranks before applying the same price, activity and category requirements.
Use it with `--data`, `--out`, `--freeze-cohort` and explicit `--from` and `--to`
Unix seconds on sealed RTDS hours. It records the allocation rule and original activity ranks.
The lower-band design remains available for reproduction.

Replaying both designs on the saved November discovery features gave **69/100** eligible
matches under interleaving and **11/100** under the original design. The design changes
who is sampled. Sixty-seven of its 100 candidates were candidates in v1. This comparison
measures the availability of controls. The registered November study uses its original 11
matched candidates. [Reproduction script](../evidence/2026-11-27-wallet-selection/compare_intake.py) ·
[Apex comparison](../evidence/2026-11-27-wallet-selection/intake-design-comparison.json)

## Commands

Run from a checkout with the Stage 0 tools. On Apex, the equivalent source entry point is
`PYTHONPATH="$PWD/src" /opt/doppel/.venv/bin/python -m doppel.lab.selection`.
Specify the expected file hashes from an independent intake receipt.

```sh
doppel-select freeze --scan "$SCAN" --scan-sha256 "$SCAN_SHA" \
  --cohort "$COHORT" --cohort-sha256 "$COHORT_SHA" --out "$PLAN"
doppel-select wallet --plan "$PLAN" --plan-sha256 "$REGISTERED_PLAN_SHA" \
  --wallet "$WALLET" --reports "$REPORTS" --tokens "$TOKEN_MAP" --out "$DECISION"
```

The wallet command exits 0 for an individual pass, 1 for an evaluated hold or failure,
and 2 for an invalid invocation or plan. Output files are write-once. Use a new decision
filename for a later observation. A wallet with no comparable frozen controls needs a new
prospective plan with an appropriate discovery pool if it is to be evaluated for selection.

## Real v1 walkthrough registered November 27

The original input scan saw **9,329 wallets**. Of those, 5,959 met the activity requirement.
Replaying the original rule recovered all 100 candidates and 50 controls. The cohort actually
froze at 18:08:18 UTC on November 26. The saved pagination receipt for `0x0cb0…3a90`
extends 43 minutes and 48 seconds past that freeze. All three census reports inspected
belong to the earlier v0 cohort. [Original intake evidence](../evidence/2026-11-27-wallet-selection/intake.json)

The prospective plan froze on Apex at 03:24:59 UTC on November 27 with source `7a4cd7b`.
The canonical plan hash is `e235c99d177583e3c44156992cdd89985fc9c281e6b09fb335386ecb01d691d6`.
Its registration commit `a4c0d1f` was on both GitHub and Apex by 03:25:36 UTC, ahead
of the 03:30 start. The matching rules found suitable controls for **11 of 100** candidates.
[Registration and four exact windows](../evidence/2026-11-27-wallet-selection/registration.json)

| Real wallet | Selection evidence | Current decision |
| --- | --- | --- |
| `0x0cb038487586d1119b165466072e9baf666f3a90` | Rank 2, $239,151.72 notional on 304 markets. The largest frozen control traded $27,492.14. | No control within the activity limit. The wallet remains available for a ledger pilot. |
| `0xf9b7876ba2c35370e0bedb7f8ee772da722aa37c` | Rank 52, the highest-ranked candidate with a qualifying match. Its sole control is `0x28cc505fb60501dbb2199a9662ba175919af904f`. | Pending four weeks of observed outcomes. The single control limits how broadly the result can be interpreted. |

The candidate first appeared in the saved scan on November 25 at 18:02:26 UTC, buying
60 shares at $0.07 in a BTC five-minute market. Both archived RTDS sockets captured that
transaction independently. The two sealed segments matched their manifests, and all 301,048
record checksums verified. Across the scan the candidate traded $51,791.97 in 221 markets.
The control traded $27,492.14 in 222. Both traded BTC five-minute markets exclusively.
[Public trade and two feed receipts](../evidence/2026-11-27-wallet-selection/walkthrough-trade.json)

The first checkpoint is **December 4, 2026 at 03:30 UTC**. Later weeks close at the same
UTC time on December 11, 18 and 25. The earliest possible four-week decision is December 25
after the last evidence reconciles. The two-wallet [collection scope](../evidence/2026-11-27-wallet-selection/collection-scope.json)
is an input to Goldsky's cohort reader. It narrows collection to the frozen primary comparison.

Polygon block **98,029,815** opened the first week at 03:30:00 UTC. Its linked predecessor
was one second earlier. The free RPC check returned all four seven-day-old collateral
balance reads for the candidate and control. Full weekly share balances, log scans and
reconciliation still need to be measured. [Boundary and historical state receipt](../evidence/2026-11-27-wallet-selection/weekly-rpc-check.json)

For Claude on Apex, use the full plan and initial wallet receipts in
`/var/backups/doppel-sealed-20261126/research/wallet-selection-v1-7a4cd7b/`.
Run the first week's budget preview from a checkout of the layer review.

```sh
scope=evidence/2026-11-27-wallet-selection/collection-scope.json
labroot=/mnt/volume_nyc1_1793485122853/doppel
PYTHONPATH="$PWD/src" /opt/doppel/.venv/bin/python -m doppel.lab.goldsky \
  --data "$labroot/tape" --ledger "$labroot/goldsky/ledger.jsonl" \
  cohort --cohort "$scope" --n-candidates 1 --n-controls 1 --days 7 --end 1796355000
```

On Apex the preview returned 40,000 rows across the two wallets, an estimated $0.40 maximum
for one capped invocation at the recorded price. A larger history can require a resume.
The preview made zero Goldsky requests. This study registered the plan and its initial
decisions. Weekly collection and its budget remain operational work. At each close, check
existing coverage first. Any additional pull should
use the shared budget ledger and preserve cursor continuity for the same fixed interval.
If a cap interrupts pagination, the missing pages still need to be obtained before scoring.
Build the fill store, resolve both outcomes and run absorption for the two listed addresses
over the exact interval. The following command consumes the resulting receipts and token
snapshot. `registered` is the full canonical hash printed above.

```sh
study=/var/backups/doppel-sealed-20261126/research/wallet-selection-v1-7a4cd7b
PYTHONPATH="$PWD/src" /opt/doppel/.venv/bin/python -m doppel.lab.selection wallet \
  --plan "$study/plan.json" --plan-sha256 "$registered" \
  --wallet 0xf9b7876ba2c35370e0bedb7f8ee772da722aa37c \
  --reports "$REPORTS" --tokens "$TOKEN_MAP" --out "$DECISION"
```

## Verification

All **632 Python tests** passed on code revision `334d8d8` across two disjoint runs
(615 general tests and 17 answer-key tests). Pyright reported zero errors or warnings.
The offline wheel matched all 71 source modules and loaded all 33 CLI entry points.
The sealed strategy registry verified 21 entries and reproduced expression digest
`49083cf45e14`.

The end-to-end selection fixture generated 24 synthetic fills across eight markets, ran
real absorption and produced 12 wallet-week receipts. The public selection command
derived $47.84 candidate profit and $24 paired excess, then returned `INCONCLUSIVE`
when the fourth week's required control receipt was removed. This checks both the
admission and missing-data paths. The November registration is the test on real wallets.
[End-to-end regression](../tests/test_selection_pipeline.py)
