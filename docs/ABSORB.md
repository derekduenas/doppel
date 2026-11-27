# Absorption ledger (`doppel-absorb`)

Layer 1 reconstructs the wallet's economic movements within a specified interval. A wallet
receipt is eligible for downstream checks when three sources of evidence agree:

1. **Goldsky fills** from the fill store, with proof that the pull that produced them read every page.
2. **Chain events** from free public Polygon JSON-RPC (`eth_getLogs`, no keys).
3. **End balances** read by `eth_call` at the window's edges.

The audit's three faults and the fix for each:

| Audit finding | What absorb does |
|---|---|
| A budget stop could leave pages unread while reporting `truncated=false`. | `goldsky.py` records pagination state per (wallet, interval). `complete` is true only when the source returned no `next_cursor`. A retry resumes at the recorded cursor. |
| Fills were not reconciled to the wallet's real token and cash balances. | Per token and for cash: (end − start balance) − reconstructed = residual. The wallet is `reconciled` only when every residual is zero within tolerance. |
| A buy that completes a set read as directional, because splits, merges, redemptions and the other leg were not reconstructed. | Every share movement is taken from ERC-1155 logs, so splits, merges, redemptions, transfers and opening inventory are all seen. The two outcome tokens of a condition are paired, and each fill is split into completing and directional quantity. |

## Usage

```
doppel-absorb --store LAB/fills --wallets 0x... 0x... --from T0 --to T1 --out DIR \
  [--rpc URL ...] [--logs-rpc URL ...] [--state-rpc URL ...] [--max-requests N] [--rate R] [--chunk-blocks B] \
  [--pages TAPE/goldsky_activity/pagination.jsonl] [--tokens LAB/tokens.jsonl] [--orders LAB/orders/...]
```

- **Window.** `--from`/`--to` are unix seconds. The window is the block range `[first block with ts ≥ from, last block with ts < to]`. `--from-block`/`--to-block` set it in blocks directly.
- **Outputs.** `DIR/wallet-<addr>.json` per wallet, `DIR/cohort.tsv` and `DIR/cohort.json` with the cohort table and RPC statistics.
- **Cache.** Historical reads are reusable from `DIR/_chain/rpc-cache.jsonl`. A UTC run reads the current chain head again. A run stopped by a request limit can resume with the same historical cache. The scanner checks the block range, contract, topics, ABI and conflicting log positions on replay.
- **Endpoint roles.** Measured on Apex on Nov 27: publicnode serves `eth_getLogs` up to 10,000-block spans but refuses archive state ("Archive requests require a personal token"); drpc serves `eth_call` at past blocks but limits `eth_getLogs` to 100 blocks on the free plan. By default logs go to publicnode and historical balance/code reads to drpc. Use `--rpc` to override all roles, or `--logs-rpc` (alias `--log-rpc`) and `--state-rpc` for role-specific overrides. All routes share the same per-run request budget and per-endpoint rate state.
- **Refusals.** A node's archive or span refusal is a capability answer, not a failure. It uses no retry and never benches the node. The pool stops offering that node the method (archive) or that span and wider (span), and moves the request to another routed node. When no routed node can serve a log span, the scan halves its span and retries the same blocks. A transport error on `eth_getLogs` also halves the span. When no routed node serves archive state, the wallet gets the cause `state_archive_unavailable` beside `balances_unavailable`.
- **Block span.** `--chunk-blocks` is the `eth_getLogs` span, default 10,000 (publicnode's limit). A Polygon day is about 43,200 blocks, so one wallet-day costs 5 filters × 5 chunks = 25 log requests.

## Cohort runs (`doppel-absorb-cohort`)

```
doppel-absorb-cohort --store LAB/fills --cohort COHORT.json --from T0 --to T1 --out DIR \
  [--max-requests N] [--wallet-batch N] [--pages ...] [--tokens ...] [--orders ...] \
  [--rpc ...] [--logs-rpc ...] [--state-rpc ...] [--rate R] [--chunk-blocks B] [--plan [--max-rows-per-wallet M]]
```

- **Members.** Every candidate and control of the cohort file, over the half-open UTC window `[T0, T1)`.
- **Resume.** `DIR/run.json` fixes the cohort hash, the window and its resolved blocks. A resume with another cohort or window exits 2. Each wallet has its own RPC cache in `DIR/_chain/`, so memory holds one wallet's chain data and a retried wallet repeats no request. `--wallet-batch` bounds how many wallets' fills are loaded at once (default 10).
- **Completed wallets.** A wallet is done when its absorption ended without a transport failure. Its status, `reconciled` or `not_reconciled` with named causes, is then a property of the data, and it is never fetched again. `DIR/done.jsonl` binds each done wallet to a hash of its Goldsky pagination records. After a Goldsky re-pull changes them, the wallet is recomputed from its cache.
- **Pending wallets.** A transport failure (budget, endpoints down, timeouts, a refusal with no capable node) keeps the wallet pending, with the attempt in `wallet-<addr>.attempt.json`. Budget exhaustion, all endpoints down, or no archive-capable state node stops the run. Exit 0 means every wallet is done, 3 means wallets remain (rerun the same command), 4 means another run holds `DIR/.lock`.
- **Table.** `DIR/cohort.tsv` and `DIR/cohort.json` list every member with role, `state` (done, pending, not_started), status, causes and the last transport error, plus cause counts and RPC statistics. They are rewritten after every wallet batch.
- **Plan.** `--plan` makes no network request. It prints the blocks (from `run.json`, otherwise at a nominal 2 s per block), the RPC requests per wallet (5 `eth_getLogs` per chunk, 6 balance calls, 1 `eth_getCode`), a receipt floor of 0 and an empirical range of 0.12–0.15 receipts per stored fill (the Nov 26 reconciled day), and at most 113 window-search reads on a fresh run. Goldsky rows are 0 for a wallet whose terminal records cover the window. Otherwise the estimate is the wallet's row density in its longest terminal record times the window, else its stored fills, capped at `--max-rows-per-wallet`. It prints the matching fixed-end `doppel-goldsky cohort` command. That command bills every wallet without a terminal record for exactly `(T0, T1)`, so its count can exceed the wallets that are missing.
- **Census input.** `doppel-census-absorbed --census CENSUS.json --absorb DIR --out NEW.json` keeps census wallets whose absorption has `not_reconciled: false`. It adds each wallet's share split (`complete_set_buy_shares`, `directional_buy_shares`, sells as `derisk`/`unpair`, pairing coverage) and recomputes `by_label` and `selfcheck` over the survivors. `census.py` is unchanged.

## Contracts and events (Polygon 137)

| Contract | Address | Established by |
|---|---|---|
| ConditionalTokens (ERC-1155 outcome tokens) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | `CtfCollateralAdapter.CONDITIONAL_TOKENS()` on chain |
| USDC.e | `0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174` | `CtfCollateralAdapter.USDCE()` on chain |
| Polymarket collateral token (PMCT / pUSD, 6 decimals), **the V2 trading collateral** | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` | ctf-exchange-v2 README; `CTFExchangeV2.getCollateral()` and `CtfCollateralAdapter.COLLATERAL_TOKEN()` on chain |
| CTFExchangeV2 / NegRiskCtfExchangeV2 | `0xE111180000d2663C0091e4f400237545B87B996B` / `0xe2222d279d744050d28e00520010520000310F59` | ctf-exchange-v2 README |
| CtfCollateralAdapter / NegRiskCtfCollateralAdapter | `0xADa100874d00e3331D00F2007a9c336a65009718` / `0xAdA200001000ef00D07553cEE7006808F895c6F1` | ctf-exchange-v2 README |

Event signatures, all from the upstream source:

- **`OrderFilled(bytes32 orderHash, address maker, address taker, uint8 side, uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee, bytes32 builder, bytes32 metadata)`.** The first three are indexed. Topic `0xd543adfd…d8ee`. Source: `Polymarket/ctf-exchange-v2/src/exchange/mixins/Events.sol`. The taker's own fill is also an `OrderFilled`, with `maker` set to the taker wallet, so the topic-2 filter covers both roles.
- **`PositionSplit` and `PositionsMerge`.** Signature `(address indexed stakeholder, address collateralToken, bytes32 indexed parentCollectionId, bytes32 indexed conditionId, uint[] partition, uint amount)`.
- **`PayoutRedemption`.** Signature `(address indexed redeemer, address indexed collateralToken, bytes32 indexed parentCollectionId, bytes32 conditionId, uint[] indexSets, uint payout)`. Source for all three: `gnosis/conditional-tokens-contracts/contracts/ConditionalTokens.sol`.
- **Transfers.** ERC-1155 `TransferSingle` / `TransferBatch` and ERC-20 `Transfer` are the standard events.

**What V2 does, and why the reconstruction uses transfer legs rather than CTF events.**
- **Adapter calls.** V2 splits, merges and redemptions go through `CtfCollateralAdapter`. It pulls pUSD, unwraps it to USDC.e, calls the CTF and sends the tokens on. So the CTF event names the *adapter* as stakeholder. The wallet only sees a `TransferBatch` from or to the adapter, plus a pUSD movement.
- **Exchange matches.** Exchange MINT/MERGE matches split inside the fill transaction, with the exchange as stakeholder (`AssetOperations.sol`). These are fills, never the wallet's splits.
- **Peer-to-peer legs.** Complementary matches move tokens and collateral directly between the wallets (`Trading.sol`).

Hence:

- **Share movements.** Every share movement is a wallet leg of an ERC-1155 log on the CTF contract. Every cash movement is a wallet leg of an ERC-20 `Transfer` on pUSD or USDC.e. The task's event list omitted the ERC-20 legs, but cash cannot reconcile without them: deposits, withdrawals, wraps and rewards would all be unexplained.
- **Fill transactions.** A fill transaction is any tx with a Goldsky fill of the wallet or an `OrderFilled` naming it. In a fill transaction the Goldsky rows are the reconstruction and the chain legs are the check.
- **Other transactions.** The classifier checks the receipt against the wallet's scanned transfers. A direct CTF event must name the wallet. An adapter event must correspond to the wallet's share counterparty. A transaction with no attributable CTF event is a transfer. Ambiguous mixed operations, inconsistent receipts and missing receipts are unclassified.
  - Collateral-only txs are `cash` (external flows).
- **Not covered.** V1 exchanges and the V1 NegRiskAdapter are not covered. Their activity would surface as `transfer` / `unclassified` movements, and as a residual if Goldsky reports them as fills.

## Definitions

Amounts are integers in micro-units (1 share = 1 USDC = 10⁶). Goldsky floats are rounded once per row.

- **Fill cash.** V2 convention (`Trading.sol`): a BUY pays `usdc + fee`; a SELL receives `usdc − fee`.
- **Opening inventory.** Balances at `b0 − 1` for every touched token and all its known condition partners. A position held throughout the interval in an unrelated, untouched market lies outside this inventory.
- **`residual`.** Per token and for cash (pUSD + USDC.e together, since wrapping moves value between them): `(end − start balance) − reconstructed`.
- **`log_residual`.** Uses all chain legs instead of the reconstruction. Zero proves the log scan explains the balances.
- **Tolerance.** One micro-unit per Goldsky row touching the asset.
- **`reconciled`.** Requires all of the following:
  - Explicit terminal pagination covers the half-open UTC window. The compacted fills over each entire supporting query must match its canonical trade count and SHA-256 accumulator. Legacy cursor-only receipts require re-attestation from sealed pages.
  - Chain scan complete.
  - The blocks immediately before and after the interval confirm the UTC cutoffs. Both balance reads succeeded for the full requested token set. pUSD and USDC.e each reconcile exactly to their own transfers. The reconstructed trade cash uses the V2 settlement collateral.
  - Every fill-transaction asset total matches its chain transfer legs and every source fill matches one chain OrderFilled event. Equal balances across the whole interval can conceal offsetting transaction errors.
  - Every residual within tolerance.
  - No unclassified tx.
  - No `OrderFilled` transaction missing from Goldsky.

  Anything else is `not_reconciled: true`, with `causes`:
  - `goldsky_incomplete`, `chain_scan_incomplete`, `balances_unavailable`
  - `chain_logs_do_not_explain_balances`
  - `goldsky_missing_fill_txs`, `goldsky_fill_without_chain_legs`
  - `share_residual`, `cash_residual`, `cash_residual_equals_fees` (the fee convention is off)
  - `unclassified_chain_txs`

  **A `not_reconciled` wallet must be excluded from profit rankings.** Census is not changed here; it should read this flag.
- **Pairing**, per condition, from opening balances on:
  - **Imbalance.** `I = pos(idx 0) − pos(idx 1)`.
  - **Fills.** A fill that moves `I` by `d` is `completing` for `min(|d|, |I|)` shares when it moves `I` toward zero, and `directional` for the rest. Sells split the same way into `derisk` and `unpair`. A YES buy at 49c while holding NO at 48c is therefore `completing`.
  - **Time-weighted.** `paired_share_s = ∫ min(pos)`, `one_sided_share_s = ∫ |I|`, `longest_one_sided_s` (the longest spell with `|I| ≥ 1` share), `max_one_sided` and `one_sided_at_end`.
  - **Ordering.** Chain-matched fills use their exchange log index within a block. The holding-time metrics use the requested UTC boundaries. Chain-only event blocks between timestamp anchors use linear interpolation and are counted in the report.
- **Identities.**
  - `trading_account`: the order maker, which holds funds and tokens.
  - `signers` / `sig_types`: from decoded orders.
  - `tx_senders`: the `from` of the wallet's split, merge, redeem and transfer txs.
  - `is_contract`, and `safe_owners` (`getOwners()`, asked only when the orders show signature type 2).
  - `code_kind` distinguishes empty code, deployed contract bytecode, a delegated EOA, and an unknown response. The historical delegation marker supplies `delegation_target`. Sharing delegated software does not establish shared control. [EIP-7702](https://eips.ethereum.org/EIPS/eip-7702)
  - A Polymarket proxy's owner is not readable this way and stays unknown.

## Pagination record (`goldsky_activity/pagination.jsonl`)

There is one line per page and one per stop, written under the pull lock:

`{tw, address, start_time, end_time, job, pages, rows, cursor, stop, complete, trade_binding}`

- **Fields.** `cursor` is the next page to request. `stop` is `done` | `budget` | `error`, or `null` while running.
- **Stop reasons.**
  - `budget`: the row limit or the ledger cap stopped the pull.
  - `error`: HTTP error, malformed page, network failure, or an empty page that still advertises a cursor.
- **Resume.** A later pull of the same (address, start_time, end_time) starts at `cursor`. One that is already `complete` is skipped.
- **Explicit `--end` for `cohort`.** `goldsky cohort` defaults `--end` to now, so a resume needs an explicit `--end`.
- **Crash between page and record.** A crash between writing a page to the tape and writing its record repeats that one page. The fill store deduplicates by row id.
- **`max_rows = 0`.** This now reports pages remaining, where before it reported `truncated=false`.

## What the Mac check proved, and what to validate on Apex

- **Mac.** The real-wallet sanity run had no fill store, so it can only show chain self-consistency: the log scan explains the `balanceOf` deltas, and the `OrderFilled` logs line up with the transfer legs. See the commit report.
- **Apex, step 1.** For the captured cohort day ending Nov 26 at 18:52:07 UTC, generate a bound receipt from the sealed pages using the command below. For the registered pair the verified artifact already exists at `wallet-absorption.crsLww/reviewed/day-proof` under the archive's research directory. For another interval, verify coverage independently. Paid resumes must use the original window and the shared spending ledger.
- **Apex, step 2.** Verify availability for the selected historical blocks on the configured log and state routes. An unavailable historical balance prevents reconciliation.
- **Apex, step 3.** Run on a few G0-passing wallets for one day.
  - Check that `fill_check.order_filled.matched` equals the chain count: Goldsky `amount_*`/`fee` match `OrderFilled`.
  - Check that cash residuals are zero. This confirms the fee convention for both roles. Taker BUYs in `_matchBuyOrders` may receive a surplus refund, which would show as `cash_residual`.
- **Apex, step 4.** Only then use `not_reconciled` to filter census and G1 rankings.

## Recovering pagination from retained tape

For a closed capture, verify its manifest hash, strict Zstandard frame and record CRC, the
request wallet and interval, returned row ownership and timestamps, and continuity from the
first request through the terminal cursor. A wallet ending with a live cursor is explicitly
incomplete. Missing wallets stay absent. Output is a new directory, separate from the live
pagination ledger. The resulting `pagination.jsonl` is accepted directly by `doppel-absorb --pages`.

```sh
python -m doppel.lab.goldsky_proof --data "$tape" --cohort "$lab/cohorts/cohort-v1.json" \
  --start 1795632727 --end 1795719127 \
  --segments 20261126/18.dtape.zst 20261126/19.dtape.zst --out "$new_dir"
```

The [offline Apex proof](../evidence/2026-11-27-absorption-pagination.json) covers 148 complete
wallet histories among 150 frozen cohort members, at zero additional provider cost.
Two histories still have live cursors. Keep their incomplete status when selecting a pilot.
The production fill-store state inspected on Nov 27 had only Nov 25 input segments.
Compact the selected Nov 26 data to an isolated fill store before reconciling a wallet.
The proof establishes provider pagination over the captured filters. Independent event
matches and balance agreement remain requirements for wallet reconciliation.

The [Layer 1 walkthrough](WALLET-ABSORPTION.md) records the two-wallet real-data results and reproducible Apex inputs. For a sealed **unfiltered** global capture, use `python -m doppel.lab.global_activity_proof`. It checks the whole original cursor chain and unique activity IDs before writing a small native fill store and a bound terminal receipt per requested wallet. Use the per-address recovery command above for wallet-filtered captures.

The earlier 150-wallet pagination inventory is on Apex under
`/var/backups/doppel-sealed-20261126/research/pagination-proof.ZN8b5q/proof`.
It predates trade binding. Re-run the recovery command before using those wallet
histories for a new ledger.
