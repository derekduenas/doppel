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

## Prospective walkthrough choice

For this first v1 study, follow **the highest-ranked candidate with an eligible frozen
control**. Keep that choice if it later fails. Also report the previously proposed pilot,
`0x0cb038487586d1119b165466072e9baf666f3a90`, regardless of control availability.
The primary demonstrates the full decision path without picking a winner after the fact.
Additional wallet comparisons are individual research results. Selecting a profitable
subset afterward would require separate validation.

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
