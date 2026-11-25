Verdict P0: **YES, with a coverage caveat.** Polymarket perps fills, positions and PnL are public per address through unauthenticated REST (`/v1/info/fills`, `/portfolio`, `/leaderboard`). Fills join the anonymous tape on `trade_id`, verified on 2 fills from 1 account. Tape coverage by enumerable addresses is unmeasured; that is P1's first receipt. Kalshi perps are anonymous.

# M6 Gate P0: perps identity and data scope (2026-11-25)

Sources: PM [architecture](https://docs.polymarket.com/perps/learn-about-trading/architecture), [fees](https://docs.polymarket.com/perps/learn-about-trading/fees), [funding](https://docs.polymarket.com/perps/learn-about-trading/funding), [market data](https://docs.polymarket.com/perps/market-data), [WS](https://docs.polymarket.com/perps/realtime-updates), [OpenAPI](https://docs.polymarket.com/api-spec/perps-openapi.json), [rate limits](https://docs.polymarket.com/api-reference/perps/rate-limits), [deposits](https://docs.polymarket.com/perps/fund-your-account); Kalshi [Perps API](https://docs.kalshi.com/margin), [OpenAPI](https://docs.kalshi.com/perps_openapi.yaml), [CFTC fee filing](https://www.cftc.gov/filings/orgrules/rules10182634487.pdf). Probes ran 2026-11-25 22:58–23:05Z: 12 unauthenticated GETs (8 PM, 4 Kalshi) and one 20 s public WS.

## 1. Polymarket perps
- **Venue:** a hybrid CLOB. Matching, margin and funding run off-chain, with custody on Polygon (pUSD `0xC011…2DFB`, deposit contract `0xDCa4…17d2`). Only deposits and withdrawals go on-chain, plus periodic state roots. US order placement is geoblocked, so we stay read-only.
- **Instruments:** 90 live, e.g. BTC-USD id 6, ETH 7, SP500 1, GOLD 2. Leverage runs 3–50x (50x on BTC, ETH, SP500, NAS100), min notional $10. The docs' "BTC-PERP id 1, 10x" is illustrative.
- **Fees:** tiered by 30-day volume. Tier $0 pays 5 bp taker and 2 bp maker. Makers earn rebates from $500M (−0.5 bp) and $1B (−1 bp).
- **Funding:**
  - A 5 s premium sample (the $1k impact bid/ask versus Index), settled hourly.
  - `clamp(scale·(P̄+clamp(1bp−P̄,±5bp))/8, ±4%)`, with scale 1.0 for crypto and 0.5 otherwise.
  - Live rates sat at the 1 bp/8h baseline (BTC `0.0000125`, SP500 `0.00000625`). The formula gives that baseline for any premium from about −4 to +6 bp.

## 2. Identity
| Source | Account? | Evidence |
|---|---|---|
| `/v1/info/trades`, WS `trades::{id}` | No | `trade_id, side(taker), price, qty, ts, hash`. `hash` was `"0x"` on all 318 sampled tape and fill rows, the oldest from 2026-08-29. |
| `/v1/info/fills?address=` | **Yes** | `order_id, taker, fee` (signed), `previous_size, previous_entry_price, pnl, liquidation, adl`. Builder identity is omitted. |
| `/v1/info/portfolio`, `/position-fills` | **Yes** | Positions, entry, uPnL, equity. |
| `/v1/info/leaderboard` | **Yes, enumerates** | `account, pnl, notional, account_value, roi`. Top 1000 per sort and window; `total` was 7,358 for the week window. |
| `/v1/info/competition/leaderboard` | **Yes** | `account` at any depth. |
| On-chain `deposit(token,amt,to=signer)` | Likely | From the docs, not checked on-chain. It would enumerate accounts and link each to its funding wallet. |
| RTDS (`activity` etc.) / PolyBolt | No | No perps topic is documented, so none was probed. |

**Join receipt.** The account ranked #2 weekly (`0x9f07…F99a`) has a fill at 20:01:56Z: `trade_id 4329977891740449, short 59941×0.01548, taker false, fee 0.0928`, which is 1 bp maker. The tape has the same `trade_id` as `long 59941×0.01548`.

**Caveats:**
- The perps account is the **signer** address, not shown to equal the PM proxy wallet DOPPEL tracks.
- Accounts are not operators: liquidity rewards group a firm's linked accounts privately.
- The budget is 1,000 weight/min/IP and fills cost 10, so about 100 uncached pages/min. This binds.
- Fills from 2026-01-01 started at 2026-11-05, with `previous_size` 0. That fits account inception, but a retention cap is not ruled out.

## 3. Public market data
- **REST** (`api.perpetuals.polymarket.com`): instruments, tickers (≤10 s stale, otherwise 503), book (depth ≤1000, `sequence`), bbo, klines, mark-history, trades and funding (100/page), statistics.
- **WS** (`wss://ws.perpetuals.polymarket.com/v1/ws`, no auth): `book` (top 20/50 every 100 ms), `bbo`, `trades`, `tickers` (`fr`, `nxf`), `statistics`, `klines`. The envelope carries `ts/ets/sq`. Median `ts−ets` was 1–2 ms. One-way latency was unmeasured because of local clock skew.
- **Liquidity:** BTC 24h `vol` about $41.7M (assumed quote notional), OI 141.5 BTC, spread about $1.
- **History:**
  - Trades backfill to at least 2026-08-29. They are event-stamped, so expected to be as-of; confirm against our own tape.
  - There is no book history.
  - `mark-history` and klines stamp the **bucket open** with the last value. That is look-ahead (the E21 class), so restamp to bucket close.
  - The leaderboard is current-only.

## 4. Kalshi perps
- **Products:** 143 markets, 134 active: crypto, equities, indices, metals and energy. FX is inactive.
  - `KXBTCPERP` is 0.0001 BTC per contract with a $0.0001 tick.
  - 24h notional **$623M**, OI $15.8M; the top 10 levels hold about $1.1M per side within about 4 bp.
  - All perps traded about $2.77B in 24h.
- **Funding:** crypto settles every 8h (00/08/16 ET) on a TWAP of the premium index. Raw fields are `funding_cap 0.02` and `funding_deadband 0.0001`, unit "percentage", with scale unconfirmed.
- **Public vs auth:**
  - `/margin/markets`, `/orderbook`, `/trades` and `/funding_rates/estimate` returned 200 without auth. The spec also marks candlesticks, historical funding and premium index public.
  - The **WS needs an API key** at the handshake, which is an owner decision.
  - Fee tiers, fills and positions need auth.
  - Trades (`trade_id, taker_side, count, price, created_time, is_block_trade`) carry **no account**.
- **Fees** (2026-10-19 filing, latest found):
  - Self-clearing members (SCM) at tier 0: 4 bp taker, 2 bp maker; maker is 0 from $10M.
  - FCM-cleared tier 0: 12 bp taker, 2 bp maker.
  - Multipliers in both: metals and indices 0.06x, energy 0.2x, FX 0.14x.
  - The owner's schedule needs the authenticated `/margin/fee_tier_rates`.
- **US access:** CFTC-regulated. Production API is "rolling out member by member"; stock perps need an application.

## 5. Recommendation
**P1 record, in order:**
1. **PM WS** `book::50`, `trades`, `tickers` and `bbo` for BTC, ETH, SP500 and GOLD (later all 90), with receive time and `sq` gaps. No auth is needed, and books cannot be backfilled.
2. **PM identity:**
   - Snapshot all leaderboards and competition boards every 15 min.
   - Poll fills and portfolios for the account union within budget.
   - First receipts: the share of tape `trade_id`s joined, and the OI and `long_count/short_count` explained.
3. **Kalshi REST polling** of book, trades and funding for the same underlyings until the owner decides on a WS key. PM trades and funding can be backfilled later.

**If coverage stays low:** treat perps as an anonymous tape (toxicity, book, funding/basis). Learn operators from Hyperliquid's public per-address fills: read-only intelligence, since US persons cannot trade there. Not re-verified this pass.
