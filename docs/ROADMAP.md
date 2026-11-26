# DOPPEL roadmap

Status as of 2026-11-25. The detailed design is in [BLUEPRINT.md](BLUEPRINT.md). Each milestone ends on its
acceptance test or a recorded kill.

| M | Scope | Status |
|---|---|---|
| M0 | Recorders: intl CLOB (8 shards), RTDS x2, PM US REST + WS (signer), Kalshi books + live BRTI, spot ×3, reference series, raw BRTI import | **Live on Apex** |
| M1 | Census: exact ledger + G0, profit-blind cohorts, mechanism census, anticipation, dossiers + policy inversion | **Running** (census v0 done; cohort v1 freezes 2026-11-26 18:05Z) |
| M2 | **Edge core** (workstreams W1–W12 below): twin, genomes, flow guard, venue economics, cross-venue hedge, fast fair, execution, ML loops | **In progress** |
| M3 | Shadow paper + riskd + desk | |
| M4 | Inversion → H3 taker clones | |
| M5 | Learned simulator, persona agents, sports/esports | |
| **M6** | **Perps expansion: Polymarket intl perps → Kalshi perps** | **Planned (owner request 2026-11-25)** |

## M2 edge core: workstreams (owner: "all of them", 2026-11-25)

Ordered by impact on profit. Each workstream ends on a test or a recorded kill. LLM agents propose;
deterministic code and preregistered gates decide.

| W | Workstream | What it adds to the edge | Done when | Status |
|---|---|---|---|---|
| W1 | **Twin** (PM US replay of genomes) | Honest after-fee P&L with CI; pessimistic queue, latency, aggressor-tagged prints | Fill model passes synthetic tests; H1 family scored on preregistered window | agent building |
| W2 | **Genome extraction + registry** | Dossiers become executable candidate strategies; hash-chained preregistration; Holm/FDR ledger | Real dossiers yield valid genomes; registry verify() green | agent building |
| W3 | **Intl informed-flow guard** (the unique edge) | Wallet informedness labels (point-in-time, from markouts) + live feature "informed net flow on intl, last N s" from the RTDS identity tape; maker pulls/shades before informed flow reaches PM US | Feature in genome vocabulary + twin; guarded vs unguarded H1 compared on the same windows | next |
| W4 | **Venue economics** | PM US fees, maker rebates, liquidity incentives modelled exactly in `Costs`; quote where incentives pay | Costs reproduce real PM US fee/rebate statements; incentive-aware market selection | next |
| W5 | **Cross-venue hedge + arb** | PM US 15m ↔ Kalshi 15m (both BRTI): hedge maker inventory, capture gaps after both fees | Measured gap/lead-lag on our tape; hedge genome scored in twin | next |
| W6 | **Fast fair engine** | BRTI computed live from exchange books (PERELMAN replica) in ms, not a 5 s poll; Rust hot path | Replica within $0.01 of published BRTI p99; latency budget met | planned |
| W7 | **Execution + riskd + paper runtime** | Same genome runs live on paper; PM US order adapter behind riskd (sole key holder); tiny owner-armed probes calibrate the twin's queue model (gate G6) | Paper ≈ twin; probe fills within the twin's predicted band | planned |
| W8 | **Persistence (who to learn from)** | Cohort v1 scored on days after its freeze vs controls; only persistent operators feed extraction | G1 verdict on cohort v1 | scheduled 11-26 18:05Z |
| W9 | **Allocator + decay sentinel** | Capital across surviving genomes/assets/horizons; Kelly fraction gated on calibration; anytime-valid e-values + changepoint auto-demotion | Sentinel demotes a synthetic decaying genome within one window | planned |
| W10 | **ML loops** (below) | Learning that compounds without fooling itself | Each loop has a walk-forward test and a gate | planned |
| W11 | **Desk (dashboard)** | One live view: genome P&L/verdicts, fair vs market, flow guard state, tape health, spend, inventory | Every number links to its source | planned (after W7) |
| W12 | **Durable storage** | Tape never lost (object storage + Parquet compaction) | Owner provides Spaces/research box | **blocked on owner** |

### W10 ML loops (each walk-forward, preregistered, never trained on its own test window)
1. **Fair residual learner:** P(Up) = f(fair-v1, book state, intl flow, time-to-close). Gradient boosting on
   settled windows, refit daily, promoted only if held-out log loss beats fair-v1 and the market mid.
2. **Fill / adverse-selection model:** P(fill) and post-fill markout given quote distance, queue ahead,
   flow, volatility. Learned from twin replays and live probes; feeds the maker's half-spread and size.
3. **Policy inversion** (exists in dossiers): extract wallet rules; explainability ceiling decides cloneable
   vs private information.
4. **Genome search:** Bayesian optimisation inside a preregistered family. Every variant is charged to the
   family's FDR ledger, and winners must survive Holm on a later window.
5. **Wallet informedness:** online, point-in-time score per wallet from dir-adjusted markouts (shrunk to
   the population mean); feeds W3.
6. **Decay / regime detection:** BOCPD + e-processes on live edge per genome; feeds W9.

## Astra review (2026-11-26), adopted

Objective for every component: **which action adds the most net dollars to the portfolio at affordable risk.**
Sometimes that is posting, keeping queue, cancelling, hedging, or sitting out.

| # | Item | Status |
|---|---|---|
| A1 | Fill-conditional edge: settlement distribution + execution model + portfolio value | E24 measured on public prints; feeds W10 fill/adverse model |
| A2 | Flow guard test that can fail: base / anonymous / identified / rate-matched placebo + delay sweep | **H3 sealed** (window 11-27..11-30) |
| A3a | Same-settlement structural scanner (ordered strikes, both venues), prospective, incl. one-leg failures | agent building |
| A3b | Hedge-priced market making (US quote from the distribution of the executable Kalshi hedge) | needs `kalshi_ws` (Rust recorder) |
| A3c | Cancel-vs-keep policy; 5 Hz raw BRTI lead in the final minute | after Rust fast-fair |
| A4 | Research agents write competing explanations + a distinguishing future observation; deterministic evaluator alone sees locked windows; experiments ranked by $ of decision changed per cost | planned (M5) |
| A5 | Two-venue reconciled portfolio replay (shared cash, inventory, queue, hedge depth; partial fills, lost acks, restart) | planned after `kalshi_ws` |
| A7 | **Adverse-selection owner.** Charter: maximise spread earned − adverse selection in $ (never "least adverse": not quoting scores zero); every guard must beat a rate-matched placebo. Two halves: (1) a deterministic Rust toxicity guard with veto over every quote, ms latency (stale-quote cancels on raw-BRTI jumps, Kalshi-leads-US book moves from `kalshi_ws`, intl informed flow, time-to-close zones, edge floor, one-sided quoting, inventory skew, post-fill Kalshi hedge); (2) a research agent that reads per-fill markouts (1/5/30 s, settlement) daily, clusters losing fills, writes competing explanations, and proposes guard genomes that go through registry + twin. It cannot deploy or loosen a guard. | per-fill markout ledger next; Kalshi lead study after 24 h of `kalshi_ws` |
| A8 | Re-price every taker path at real API fees (E25: Θ 0.0695, 70% rebate to 12-31); structural scanner + xvenue on `kalshi_ws` books | xvenue done; scanner default fee + kalshi_ws reader next |
| A6 | Commercial hurdle replaces "$ > 0": lower bound $/day after operating cost, best-day share, ex-incentives | **in twin verdict** |

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
