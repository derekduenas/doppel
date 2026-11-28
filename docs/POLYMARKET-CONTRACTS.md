# Polymarket contracts on Polygon that absorb reads

Established 2026-11-27 while investigating why cohort v1 absorption (Nov 26 window) failed for 27 of 31 not-reconciled wallets with the same three causes: `fill_transaction_legs_disagree`, `fill_orders_disagree` and `share_residual`.

**Cause.** Polymarket runs a second market system, "Polymarket V2", next to the classic ConditionalTokens system. It has its own ERC-1155 position contract and its own exchange. Absorb read neither, so:
- Goldsky reported the fills (`order_filled.goldsky_only > 0`), but absorb saw no matching `OrderFilled`.
- The share legs were missing, and balances were read only on ConditionalTokens.

Everything below comes from Polymarket's public source, the observed logs, and on-chain reads. Nothing here is guessed.

## Sources

- **S1, the V2 source code.** [github.com/Polymarket/polymarket-v2-external](https://github.com/Polymarket/polymarket-v2-external) at commit `741f8bbe88c3f29a1c55d1248a3dd411f622f111` (pushed 2026-10-29). These files were read:
  - `README.md`, which has the deployment table and the position ID layout
  - `docs/position-ids.md`, `docs/exchange.md`, `docs/position-manager.md`, `docs/migration.md`
  - `src/exchange/Exchange.sol`, which defines the events
  - `src/modules/abstract/BaseModule.sol`, which defines the events and `split`/`merge`/`redeem`
  - `src/positionManager/PositionManager.sol`, which shows the mint, burn and unsafe transfer log emission
  - `src/libraries/ModuleIds.sol`, `src/libraries/Ids.sol`, `src/routers/Router.sol` and `src/utils/AutoRedeemer.sol`
- **S2, on-chain reads.** Made on 2026-11-27 through `https://polygon-bor-rpc.publicnode.com`, from the Apex host, because the Mac's TLS to public RPCs was refused. Each read is listed below.
- **S3, the example transaction.** The receipt of `0x0189c27086445efa3e166977a350aecb00c3c49bc0d75969cb243652bf300c13` (block 97966413). It is committed unmodified, with a Router redemption receipt and one real `PositionsMerged` and `TransferBatch` log, as `tests/fixtures_absorb/pm-v2-polygon.json`.
- **Topic hashes.** Every topic0 below is `keccak256` of the canonical signature from S1, computed with pycryptodome. User-defined value types are encoded as their underlying type: `PositionId` is `uint256`, `ConditionId` is `bytes31`, and the `Side` enum is `uint8`. Each hash equals the topic observed in S3 or in a live `eth_getLogs` (S2).

## Contracts (Polygon, chainId 137)

| Contract | Address (proxy) | Role | Evidence |
|---|---|---|---|
| PositionManager | `0x006F54F7f9A22e0000CC2AB60031000000ae9fEF` | ERC-1155 for every V2 position (Solady ERC1155: `balanceOfBatch`, `TransferSingle`/`TransferBatch`). Holds no market logic. | S1 README table. S2: `COLLATERAL_TOKEN()` = pUSD `0xC011a7E1…2DFB`. |
| Exchange | `0xe3333700cA9d93003F00f0F71f8515005F6c00Aa` | Order matching for PositionManager positions (EIP-712 domain "Polymarket Exchange" v1). | S1 README table, `docs/exchange.md`. S3: it is the transaction's `to`. |
| BinaryModule | `0x1000008dD9001B968442c1000017eaE6E0dA00Ba` | Module 1: binary conditions, with `split`/`merge`/`redeem` and resolution. **This is the contract the task asked about.** It is a module, not a registry or adapter. The PositionManager routes mint and burn rights by the id's top byte. | S1 README table. S2: `PositionManager.moduleById(1)` returns it, and `BinaryModule.POSITION_MANAGER()` returns the PositionManager. |
| NegRiskModule | `0x200000900045e3B6259600682756002200028933` | Module 2 (multi-outcome events) | S1. S2: `moduleById(2)`. |
| CombinatorialModule | `0x30000034706C7d8e12009DAB006Be20000c031A8` | Module 3 (conjunctions) | S1. S2: `moduleById(3)`. |
| Router | `0x12121212006e4CD160D18e3f00711DA5c3372600` | The user entry point for split, merge and redeem | S1. S2: it is the `initiator` of a live `PositionRedeemed`. |
| AutoRedeemer | `0xa1200000d0002264C9a1698e001292D00E1b00af` | Operator batch redemptions | S1 |
| CollateralToken (pUSD / PMCT) | `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` | The same collateral the classic V2 exchanges use. Modules burn it on split and mint it on merge and redeem. | S1, S2, S3 |

**Implementation drift.** EIP-1967 implementation slot reads (S2) agree with the S1 table only for the PositionManager (`0xCc5De1e9…c2D0`). The Exchange proxy now points to `0x862f1cff1bd6246a2585e3622137e6f961b34e55` (S1 lists `0x641b40ec…8Bb7`). The BinaryModule proxy points to `0x66290cf3c362be3910c1b5572d381453fa7da2ba` (S1 lists `0xf6428c0b…e9A2`). Both were upgraded after S1. Verified source for those two implementations was not checked: Polygonscan was not queried, since that would have needed an API key. What absorb decodes is therefore limited to events whose topic0 *and* data layout match S1 in real logs from the live implementations:

- `OrderFilled`, `OrdersMatched`, `FeeCharged` and `PositionsSplit`, in S3
- `PositionsMerged`, `PositionRedeemed` and PositionManager `TransferBatch`, from live `eth_getLogs` over blocks 97,961,168–97,967,436 (S2), with fixtures committed
- `PositionMigrated` (`0xcfd569e5…fb84`) appeared 0 times in that range. Absorb does not decode it.

## Position IDs (S1 `README.md`, `docs/position-ids.md`)

```
[moduleId(8) | baseHash(128) | arity(16) | reserved(64) | resolutionChain(16) | conditionIndex(16) | outcomeIndex(8)]
 bits 248-255  bits 120-247    104-119     40-103         24-39                 8-23                 0-7
```

- **Condition ID.** The position ID with `outcomeIndex = 0`, typed `bytes31`, meaning the top 31 bytes. A position ID is `conditionId | outcomeIndex`.
- **Outcome index.** For binary markets, 0 is YES and 1 is NO. `BaseModule.split` mints `computePositionId(0)` and `computePositionId(1)` for a condition.
- **The example id.** `731943770273641369976619672786514356706835722247370577967377842544746430464` is `0x01 | 9e43f758b85f9a3498c1574d2133e204 | 0…0 | 00`. That decodes as module 1 (BinaryModule), a 128-bit base hash, arity 0, reserved 0, Polygon resolution, condition index 0, and outcome 0 (YES). The id ending `…01` is its NO.
- **Mapping to markets.** An id carries its module, condition and outcome, but not the market's question. `baseHash` is a truncated `keccak256(moduleId, data)` of the event data. Linking an id to a Gamma market (for example a BTC 15-minute up/down) needs an off-chain market listing. The listing was not consulted here, so `tokens.jsonl` does not map these ids. Legacy CTF conditions can be linked through `BinaryModule.prepareMigrationCondition`, which emits `MigrationConditionRegistered(v2ConditionId, legacyConditionId)` and can be read back with `legacyConditionId(conditionId)` (`docs/migration.md`). Absorb does not need it.
- **No inference from bits alone.** Classic CTF ids are hashes. Roughly 1% of them start with byte 0x01–0x03, so absorb never infers a contract from an id's bits. It derives the (condition, outcome) pair only for ids it **saw on the PositionManager** with module byte 1.

## Events

| Event (S1 signature) | topic0 | Emitter | Indexed → topics 1–3 | Data words |
|---|---|---|---|---|
| `OrderFilled(bytes32 orderHash, address maker, address taker, uint8 side, uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled, uint256 fee, bytes32 builder, bytes32 metadata)` | `0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee` | Exchange | orderHash, maker, taker | side, tokenId, makerAmt, takerAmt, fee, builder, metadata (7) |
| `OrdersMatched(bytes32 takerOrderHash, address takerOrderMaker, uint8 side, uint256 tokenId, uint256 makerAmountFilled, uint256 takerAmountFilled)` | `0x174b3811690657c217184f89418266767c87e4805d09680c39fc9c031c0cab7c` | Exchange | takerOrderHash, takerOrderMaker | 4 |
| `FeeCharged(address receiver, uint256 amount)` | `0x55bb3cade9d43b798a4fe5ffdd05024b2d7870df53920673bfc7e68047cd0ab1` | Exchange | receiver | amount |
| `PositionsSplit(address initiator, bytes31 conditionId, address recipient0, address recipient1, uint256 amount)` | `0xb6a6b8b17a43b07b9359dd12d304e42077eede9e672f8646144842c8604582a4` | module | initiator, conditionId, recipient0 | recipient1, amount |
| `PositionsMerged(address initiator, bytes31 conditionId, address recipient, uint256 amount)` | `0xf233f7be4ce81130a78e8eb2fa626497586c56094d27bfec2e196471e9c60301` | module | initiator, conditionId, recipient | amount |
| `PositionRedeemed(address initiator, uint256 positionId, address recipient, uint256 amount, uint256 payout)` | `0x4fd474b703f46b453536518b4cbc159dfbea6fd663233763d321a9bc8ccdf97f` | module | initiator, positionId, recipient | amount, payout |
| `TransferSingle` / `TransferBatch` (ERC-1155) | `0xc3d58168…0f62` / `0x4a39dc06…f7fb` | PositionManager | operator, from, to | standard ABI |

**The S3 prefixes.**
- `0xd543adfd` is `OrderFilled`. It has the same signature and topic as CTFExchangeV2's, so `absorb.order_filled` decodes it unchanged.
- `0x174b3811` is `OrdersMatched` and `0x55bb3cad` is `FeeCharged`.
- `0xc3d58168` is `TransferSingle` on the PositionManager.
- `0xb6a6b8b1` is BinaryModule `PositionsSplit`. A `bytes31` topic is left-aligned, so the condition id is the topic with its last byte `00`.

**Reading S3.** It is a MINT match:
- **Maker** `0x433f…a0dc` BUYs YES: 6 shares for 3.18 pUSD, no fee.
- **Taker** `0x0dbd…fcd5` BUYs NO: 6 shares for 2.82 pUSD plus a fee of 0.01494, which goes to `0x115f…ccc9` (`FeeCharged`).
- **Split and delivery.** The Exchange pre-transfers 6 pUSD to the BinaryModule. The module burns the pUSD (a `Transfer` to zero) and mints 6 YES and 6 NO to the Exchange. The Exchange delivers each side with `TransferSingle`.
- **Taker log.** The taker's own `OrderFilled` names the Exchange as `taker`, exactly as on CTFExchangeV2.

## What absorb does with this (`src/doppel/lab/absorb.py`)

- **Scan.** Three more `eth_getLogs` filters per chunk: the PositionManager's `TransferSingle`/`TransferBatch` from and to the wallet, and Exchange `OrderFilled` with the wallet as maker. They are separate filters, so the classic queries' RPC cache keys are unchanged.
- **Legs.** PositionManager transfers become share legs (tagged `contract`). Exchange `OrderFilled` rows are matched to Goldsky by (tx, order hash, block, side, token id, amounts, fee), in the token-id space Goldsky reports.
- **Balances.** `balanceOfBatch` on the PositionManager for the ids seen there, plus their BinaryModule siblings, is added to the ConditionalTokens balance. A wallet with no V2 positions makes no extra call.
- **Non-fill transactions.** BaseModule `PositionsSplit`, `PositionsMerged` and `PositionRedeemed` from the three modules classify a transaction as split, merge or redeem when the wallet is a recipient and its share counterparties are the zero address or a module. Anything else stays `transfer`, as migrations do, or `unclassified`.
- **Pairing.** A BinaryModule id gives its own (condition, outcome). The condition key is the 62-hex `bytes31`, which cannot collide with a classic 64-hex condition.
- **Not decoded.** `PositionMigrated`, bridge events and combinatorial wrap/unwrap are not decoded. They surface as transfer movements, and as residuals if balances disagree.
- **Signer identity.** `identities` reads signers from the order tape, which decodes only CTFExchangeV2 calldata. For V2 Exchange orders, signer coverage reads `partial`.
- **Recompute.** `absorb_cohort.DECODER_VERSION` (now 2) is part of every completed wallet's input key, so a rerun recomputes completed wallets.
