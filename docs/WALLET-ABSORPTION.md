# Layer 1 · Wallet absorption

The registered candidate and control both passed economic reconciliation over the first
15 minutes of the walkthrough and a separate day of wallet activity. All 6,638 day fills
matched an exchange event and a signed-order hash. Share and cash movements reconciled per
transaction, and both collateral contracts closed exactly. The first candidate trade witnessed by Layer 0 was a
60-share maker buy at 7¢ for $4.20. It appeared on both RTDS sockets, in a sealed
Goldsky page, and in a successful Polygon transaction. [First-trade receipt](../evidence/2026-11-27-wallet-absorption/first-trade.json)

## What the wallet carried into the next layer

UTC intervals are half-open. The block ranges in the reports include both endpoints.
The five-minute check is contained in the 15-minute run.

| Window | Candidate `0xf9b7…a37c` | Control `0x28cc…904f` | Ledger result |
|---|---:|---:|---|
| Nov 25 18:00–18:05 | 8 signed fills | 2 signed fills, 1 redemption, 1 cash-only tx | Both reconcile |
| Nov 25 18:00–18:15 | 30 signed fills | 35 signed fills, 3 redemptions, 3 cash-only txs | Both reconcile |
| Nov 25 18:15–18:52:07 | 70 Polygon fills | 37 Polygon fills | Neither reconciles. No retained Goldsky query covers these 2,227 seconds |
| Nov 25 18:52:07–Nov 26 18:52:07 | 4,556 signed fills, 364 merges, 278 redemptions | 2,082 signed fills, 249 redemptions | Both reconcile |

The day included 206 candidate and 198 control collateral-only transactions. All fills
were maker buys. The ledger identified both outcome tokens for 289 candidate conditions and 250 control conditions.
Across the reconciled windows the largest share residual and both individual collateral
residuals were zero. Every share-event block in the day had a log timestamp. Counts,
coverage decisions, exact UTC and block boundaries, per-wallet evidence hashes and the
retained failed attempt are in the [run summary](../evidence/2026-11-27-wallet-absorption/run-summary.json).

**Account identity.** The candidate's signed orders name its own trading address as signer
(signature type 0). Its closing bytecode is a delegation marker. The control has deployed
contract bytecode and its orders name its own trading address (type 3). The sample has no
shared verified signer. These observations establish the signing fields approved on chain.
Ultimate ownership and control of other accounts remain unknown. [EIP-7702 delegation indicator](https://eips.ethereum.org/EIPS/eip-7702)

**Placement evidence.** All 4,556 candidate order observations in the day and all 30 in the
separate first 15 minutes have a self-reported creation time after their Polygon block.
The first order's timestamp is 896 seconds late. The control had 27 after-block timestamps
and 2,055 plausible ones in the day. A plausible stamp alone does not prove when an order
first rested. The time of the fill remains available independently. Running the existing
`ordermatch.load_orders` on the actual tapes returned zero eligible candidate orders in
both intervals. It admitted 17 unique control orders in the first 15 minutes and 981 in
the day. These are input-screen counts. Book matches still require a separate test.

**Selection and scope.** These historical samples are outside the registered four-week
selection period. Week one closes Dec 4 at 03:30 UTC and the fourth closes Dec 25 at
03:30 UTC. Prospective selection, a cohort-wide audit, estimates of profitability,
and strategy identification remain open. Holdings in unrelated markets with no movements
in the interval are outside this inventory. A single log provider could omit a net-zero
nonfill round trip without changing either endpoint balance. [Selection registration](WALLET-SELECTION.md)

## What changed under audit

- Bound the compacted fills to every supporting Goldsky query by trade count and a sum
  of SHA-256 hashes of the source fields. Archived proofs verify sealed segments and the cursor
  chain through an explicit terminal page. Requests must be sent after the interval closes.
  A new verifier projects a selected wallet from a complete, unfiltered capture. Legacy
  cursor-only receipts need re-attestation. The weekly scorer also requires the stronger proof.
- Checked the full UTC boundary against adjacent blocks, the transaction and block of each
  fill, its on-chain log order, gross token movements and both pUSD and USDC.e independently.
  Missing balances, a failed or inconsistent receipt, foreign collateral in a V2 fill,
  conflicting logs and ambiguous CTF operations have explicit failure outcomes.
- Required the wallet or its share-transfer adapter to participate before attributing a
  split, merge or redemption. Binary partition order determines outcome indices. A split
  moves both sides in one operation. Incomplete position coverage withholds whole-wallet
  pairing fractions. The weekly scorer independently checks sibling inventory and collateral.
- Joined verified order-tape rows to in-window chain events before attributing signers.
  Historical bytecode distinguishes delegated accounts. The holding-time report now uses
  consistent block timestamps provided by the log endpoint and identifies any time estimates.
  A disagreement across fill, log or cached header evidence disqualifies a report.

The first candidate day run stopped at 600 RPC requests with 167 outstanding receipts.
It returned `not_reconciled`. Resuming from the cache took 191 additional reads for the
remaining receipts, balance queries, and other calls. The later fully signed runs retained
the original failure report. The gap test independently exercised missing source coverage
while both endpoints and the chain scan were available. [Ledger semantics](ABSORB.md)

## Reproduce the evidence on Apex

The research directory is
`/var/backups/doppel-sealed-20261126/research/wallet-absorption.crsLww`.
The source archive stayed on Apex. The study used sealed Goldsky pages, free Polygon reads,
and a scratch Gamma token map. It made no paid Goldsky requests. The largest per-job
memory cap was 1,000 MB. The working directory for the retained audit occupied 53 MiB
at the operational check. The installed recorder release was `44aa7e2`.

1. The source for `reviewed/first-source` was `goldsky_activity/20261125/18.dtape.zst`,
   job `val-20261125T1800-15m`. Run `doppel.lab.global_activity_proof` on the selected
   two addresses for `[1795629600,1795630500)`. It read 47 pages and verified 46,152
   unique activity IDs globally. The source for `reviewed/day-proof` was
   `goldsky_activity/20261126/{18,19}.dtape.zst`, with the registered
   `collection-scope.json` and `[1795632727,1795719127)`. Run `doppel.lab.goldsky_proof`
   for these per-address pages. Both commands require a fresh destination.
2. Compact the day sources with `fillstore.compact` using the **full job string** from each
   terminal row as `job_prefix`. The two compacted stores are `day-store-0xf9b7` and
   `day-store-0x28cc`. `first-orders` and `day-orders-<tag>` are committed order-tape
   parts produced from those specific source fills. The day decoded 4,551 candidate
   transactions and 2,080 control transactions. All 6,638 fill-to-order links matched.
3. Use the archived `lab/tokens.jsonl` for the short intervals. For the day, the frozen
   map knew only 37/460 candidate and 57/411 control traded token IDs. Populate a scratch
   map using `TokenMap.ensure` on traded IDs, then add independently observed chain-only
   tokens and their siblings. `day-with-chain-tokens.jsonl` contains the resulting inputs.
   This is retrospective identity metadata. Decision-time rule features require their own
   as-of evidence.
4. Run `doppel.lab.absorb` with the store, wallet, UTC bounds, pages, tokens and order
   directory for each interval. The final artifacts are `signed-five`, `signed-fifteen`,
   `signed-gap`, and `signed-day-<tag>`. Each has a wallet JSON and a cohort receipt.
   Historical RPC caches are shared with the recorded first attempts. Check the economic
   status in each wallet receipt. A successful CLI exit also occurs for an unreconciled wallet.

The following command reproduces the published aggregate using only the retained files.
Audit code is pinned at `c458a29b46c8dc3d459456ad4e7f14ae982a6797`.

```sh
cd /var/backups/doppel-sealed-20261126/research/wallet-absorption.crsLww
PYTHONPATH="$PWD/final-code/src" /opt/doppel/.venv/bin/python \
  final-code/evidence/2026-11-27-wallet-absorption/summarize.py --study "$PWD" | sha256sum
```

Expected SHA-256 is `652d8f5225786fc83ebca8c5cd3b8e06d6eb4803465977778fd8a3973103eae2`.

The final code passed all 719 Python tests (702 general and 17 answer-key tests).
Pyright reported zero issues. All 72 Rust tests, Clippy with warnings denied, formatting,
the offline build and lock check passed. Registry verification passed for 21 records and
the expression DNA check returned `49083cf45e14`. The Mac copy of the run summary has
the same SHA-256 as the Apex copy.

## Next layer · Inspect

Start with transaction `0x32aa9fa6cb2815070bc6e3a9d9d56163e24c3aa09a5ca78017f27fc977a2b6d8`.
Use its verified maker side, token, size, price, order hash and fill block to look for
independently identifiable placements on the sealed public book. Record ambiguity,
possible quote life and ending evidence. Compare attribution against a same-window placebo.
The current matcher screens out negative order age. Keep the candidate's placement
unknown where the book cannot distinguish competing orders. Check tape coverage before matching.
The retained RTDS sockets and BTC book have no Nov 25 20:00 UTC hour and the preceding
hour is partial. The day ledger can therefore reconcile fills that lack a recorded book.
An order's true signing time, never-filled orders and off-venue hedges are outside what
this ledger established.

At 04:35 UTC on Nov 27 the Apex root had 32.67 GiB free against the configured 35 GiB
archive reserve. Hot tape had 13.97 GiB free against its 12 GiB cleanup floor. Both
recorders and the original baseline were active. Capacity is still an open dependency for
retaining future book evidence. [Operational handoff](RULE-TWIN-REVIEW-20261127.md)
