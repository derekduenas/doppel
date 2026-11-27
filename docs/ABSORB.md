# Absorption ledger (`doppel-absorb`)

The first stage of DOPPEL is absorbing a wallet completely and exactly. A wallet's history is
usable (for G0, G1, census rankings, extraction) only when three independent sources agree:

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
  [--rpc URL ...] [--log-rpc URL ...] [--state-rpc URL ...] [--max-requests N] [--rate R] [--chunk-blocks B] \
  [--pages TAPE/goldsky_activity/pagination.jsonl] [--tokens LAB/tokens.jsonl] [--orders LAB/orders/...]
```

- **Window.** `--from`/`--to` are unix seconds. The window is the block range `[first block with ts ≥ from, last block with ts < to]`. `--from-block`/`--to-block` set it in blocks directly.
- **Outputs.** `DIR/wallet-<addr>.json` per wallet, `DIR/cohort.tsv` and `DIR/cohort.json` with the cohort table and RPC statistics.
- **Cache.** Every RPC answer is cached in `DIR/_chain/rpc-cache.jsonl`. A run stopped by `--max-requests` or by endpoint errors is resumed by running the same command again. Answers already received are never requested again, so a resumed scan adds no duplicate logs: they are keyed by (tx, logIndex).
- **Endpoint roles.** By default logs go to publicnode and historical balance/code reads to drpc. Use `--rpc` to override all roles, or `--log-rpc` and `--state-rpc` for role-specific overrides. All routes share the same per-run request budget and per-endpoint rate state.
- **Block span.** `--chunk-blocks` is the `eth_getLogs` span. 1rpc.io allows 50 blocks. Find the largest span publicnode or drpc accept on Apex before a large run.
- **Pagination file.** Without `--pages`, Goldsky counts as incomplete and no wallet can be `reconciled`.
- **Token pairs.** `--tokens` is the ledger's Gamma cache (token → condition, idx). Pairs are also learned from split, merge and redemption batches on chain. A token with no known pair is listed in `pairing.tokens_without_pair`, and no pairing is claimed for it.
- **Signers.** `--orders` is a `doppel-ordertape` output directory. It gives each order's signer and signature type.

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
- **Other transactions.** Any other tx that moves shares is classified from its receipt's CTF events, whoever the stakeholder:
  - `redeem` > `merge` > `split` > `transfer` (no CTF event).
  - `unclassified` if the receipt was not fetched.
  - Collateral-only txs are `cash` (external flows).
- **Not covered.** V1 exchanges and the V1 NegRiskAdapter are not covered. Their activity would surface as `transfer` / `unclassified` movements, and as a residual if Goldsky reports them as fills.

## Definitions

Amounts are integers in micro-units (1 share = 1 USDC = 10⁶). Goldsky floats are rounded once per row.

- **Fill cash.** V2 convention (`Trading.sol`): a BUY pays `usdc + fee`; a SELL receives `usdc − fee`.
- **Opening inventory.** Balances at `b0 − 1` for every touched token and all its known condition partners. Positions start there, not at zero, so inventory from before the window is no longer "left-censored".
- **`residual`.** Per token and for cash (pUSD + USDC.e together, since wrapping moves value between them): `(end − start balance) − reconstructed`.
- **`log_residual`.** Uses all chain legs instead of the reconstruction. Zero proves the log scan explains the balances.
- **Tolerance.** One micro-unit per Goldsky row touching the asset.
- **`reconciled`.** Requires all of the following:
  - Goldsky complete: complete pagination records for the wallet cover `[from, to]`.
  - Chain scan complete.
  - Both balance reads succeeded for the full requested token batch.
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
  - **Ordering.** Events are ordered by block. Goldsky rows within one block are ordered by tx hash, because Goldsky rows have no log index.
- **Identities.**
  - `trading_account`: the order maker, which holds funds and tokens.
  - `signers` / `sig_types`: from decoded orders.
  - `tx_senders`: the `from` of the wallet's split, merge, redeem and transfer txs.
  - `is_contract`, and `safe_owners` (`getOwners()`, asked only when the orders show signature type 2).
  - A Polymarket proxy's owner is not readable this way and stays unknown.

## Pagination record (`goldsky_activity/pagination.jsonl`)

There is one line per page and one per stop, written under the pull lock:

`{tw, address, start_time, end_time, job, pages, rows, cursor, stop, complete}`

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
- **Apex, step 1.** Build pagination records: re-pull the cohort with `--end` fixed. Older pulls have none, so every wallet is `goldsky_incomplete` until then.
- **Apex, step 2.** Find the publicnode/drpc `eth_getLogs` span limit, and check that historical `eth_call` works at day-old blocks (archive state). If it does not, every wallet is `balances_unavailable`.
- **Apex, step 3.** Run on a few G0-passing wallets for one day.
  - Check that `fill_check.order_filled.matched` equals the chain count: Goldsky `amount_*`/`fee` match `OrderFilled`.
  - Check that cash residuals are zero. This confirms the fee convention for both roles. Taker BUYs in `_matchBuyOrders` may receive a surplus refund, which would show as `cash_residual`.
- **Apex, step 4.** Only then use `not_reconciled` to filter census and G1 rankings.
