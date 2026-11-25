# DOPPEL roadmap

Status as of 2026-11-25. The detailed design is in [BLUEPRINT.md](BLUEPRINT.md). Each milestone ends on its
acceptance test or a recorded kill.

| M | Scope | Status |
|---|---|---|
| M0 | Recorders: intl CLOB (8 shards), RTDS x2, PM US REST + WS (signer), Kalshi books + live BRTI, spot ×3, reference series, raw BRTI import | **Live on Apex** |
| M1 | Census: exact ledger + G0, profit-blind cohorts, mechanism census, anticipation, dossiers + policy inversion | **Running** (census v0 done; cohort v1 freezes 2026-11-26 18:05Z) |
| M2 | 5m complex fair value (fair-v1 live) + H1 guarded PM US maker in the twin | Next |
| M3 | Shadow paper + riskd + desk | |
| M4 | Inversion → H3 taker clones | |
| M5 | Learned simulator, persona agents, sports/esports | |
| **M6** | **Perps expansion: Polymarket intl perps → Kalshi perps** | **Planned (owner request 2026-11-25)** |

## M6: Perps expansion (PM intl perps as the lab, Kalshi perps as the venue)

**Why.** The crypto-window census says the money goes to makers. Perps are the deepest continuous
crypto market on both sides. Kalshi perps (CFTC-approved, BTC) are US-legal, and Polymarket intl perps
(BTC-USD, GOLD-USD, SP500-USD, pUSD collateral, funding, public book/trades/ticker stream) are the
intelligence source. This is the same transplant as H1: learn on intl, execute on a US venue.

**Gate P0, identity (decides the design).** Determine whether Polymarket perps fills are attributable to
wallets (on-chain settlement events, a public account/fills API, builder codes, or RTDS activity).
- If yes: run the full DOPPEL pipeline on perps operators (ledger + G0 vs their account P&L, census by
  mechanism, dossiers, policy inversion).
- If no: perps become an anonymous-tape market (flow toxicity, book dynamics, funding/basis), and
  operator-level learning comes from Hyperliquid's public per-address fills (read-only; see SHADOWBOOK
  notes), mapped to the same instruments.

**P1, record.** Polymarket perps public stream (books, trades, ticker/funding) and Kalshi perps (book,
trades, funding, fee tiers) into the tape, with completeness receipts. The tape cannot be backfilled, so
start once P0 is answered.

**P2, fair value and basis.** Perp fair value = index + funding/basis model. Measure PM-perps vs
Kalshi-perps vs spot-index lead-lag on our own as-of tape (never venue history endpoints, see FINDINGS E21).

**P3, census and dissection** (if P0 = yes): who earns on PM perps, by mechanism, with rebates and funding
P&L split out.

**P4, transplant to Kalshi perps.** Strategy genome (maker quoting rule, inventory/funding policy) →
twin on the Kalshi perps tape (queue model, Kalshi fee tiers, funding) → shadow paper → owner-armed
micro-live through riskd. Hedging leg (Coinbase US perps or spot) is evaluated in the twin first.

**Kill / hold.** Hold if Kalshi perps depth cannot carry the strategy's size after fees. Kill a mechanism
if it fails G4 (twin economics) on held-out data. All live trading stays behind owner arming.
