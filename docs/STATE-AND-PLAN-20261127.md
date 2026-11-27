# DOPPEL · State of the system and build plan (2026-11-27)

Systems-engineering report: where DOPPEL stands against its North Star, the gaps, and a concrete
build plan. Numbers cite `evidence/2026-11-25-probes/FINDINGS.md` rows (E…), dated reports in
`docs/`, or measurements on Apex on 2026-11-27.

## 1. North Star

Find the traders on Polymarket international whose profit is real and persistent, recover the
**rules** each one trades by (the genome), prove the recovered rules behave like the trader on
markets they have not seen, run those rules ourselves on paper beside the trader, then on US
venues they cannot reach, and make money. Built to institutional standard: every number
traceable, every stage with a test that can fail, nothing live without the owner arming it.

A genome is a 1:1 rule twin only when four things hold (BLUEPRINT-V3 §1): ledger truth, rule
recovery with rivals tested, forward agreement on unseen markets, live agreement on paper.

## 2. Where the system is, stage by stage

| Stage | Purpose | Built | Proven on real data | Main limit today |
|---|---|---|---|---|
| Data | Record every market and identity feed | Yes; release `ac09403` live on Apex | Yes; 18 streams, receipts sealing hourly | Chain coverage of the identity tape unmeasured (89.2% in one 14-min check vs 99.5% target); one host, no alerting |
| 0 · Select | Traders whose profit persists vs matched controls | Yes | Running; checkpoint Dec 4, decision ~Dec 25 | Only 11 of 100 candidates found a matched control |
| 1 · Absorb | Complete history; shares and cash reconcile to chain balances | Yes (`doppel-absorb`) | Two wallets reconciled (Layer 1 report) | Cohort-wide run needs a fixed-end Goldsky re-pull and endpoint routing (logs via publicnode, balances via drpc) |
| 2 · Inspect | Signed order behind each fill; its life in the book | Yes | 8,077 / 8,077 fills hash-verified; book match 98% vs 4.6% placebo for one wallet-hour (E36–E39) | Placement owner often ambiguous; anonymous book |
| 3 · Dissect | What the trader could see at each decision | Yes | 33 contexts for two wallets (Layer 3) | Skipped markets and decisions not observable; signals we do not record |
| 4 · Reverse-engineer | Rival rules, decided where they disagree | Yes | Controlled test: both rules recovered, random control abstained (reproduced). Real wallets: **neither strategy identified** | Public data shows executions, not decisions to post, cancel or skip |
| 5 · Prove the machine | Answer key: scripted traders with known rules | Yes | First synthetic score: 67% recovered, **40% of confident claims wrong**; reserved three-seed exam running now | Selection bias (only filled orders seen) drives the wrong claims |
| 6 · Verify twin | Fidelity on unseen markets | Yes (`doppel-fidelity`) | No real clone has been scored | Needs a recovered rule first |
| 7 · Paper twin | Run beside the trader live | **Not built** | — | No execution plane |
| 8 · Live | Owner-armed micro size | **Not built** | — | Same |
| Strategy families | Sealed US-venue experiments H1–H4 | Yes | H1 interim loses 0.6–1.6 c/contract (E35) | Opening-price rule undecided; fair-value makers barely quote under the old rule |

**Honest summary.** The front half of the machine (record, select, absorb, inspect, dissect) is
built and verified. The reverse-engineering step works on controlled data and correctly refuses
to name a real strategy it cannot prove. The back half (paper, live) does not exist. No strategy
has been shown to make money.

## 3. The central gap: public data shows outcomes, not decisions

A trader's strategy is a set of decisions: which market, when, which side, what price, what size,
when to cancel or exit. Public data records only what **executed**. For a taker each decision
is its execution, so the strategy is largely visible. For a maker most decisions are quotes that
never fill (one studied maker: about 0.3% of quotes filled, estimated, E39) and cancels that
leave no record we can attribute with certainty. That is why Layer 4 abstained on both real
makers, and why the answer key's wrong claims were all about cadence and quote life.

How much of a strategy public data can reveal depends on the type:

| Strategy type | Recoverable from public data | Why |
|---|---|---|
| Taker (sniper, endgame, arbitrage) | Most of it | Decision = execution |
| Complete-set builder | Most of it | The logic is in the inventory ledger |
| Slow resting maker | Part | Quotes rest long enough to match in the book |
| Fast maker (sub-second quotes) | Little directly | The rule lives in unattributable, unfilled quotes |

## 4. The strategy simulator idea: yes, with four conditions

The owner's idea, simulating candidate strategies to get closer to 100% than public data alone
allows, is the right one. In the literature it is **simulation-based inference**: when the process
that generated the data cannot be observed directly but can be simulated, propose candidate
rules, simulate each on the real recorded market, and keep the rules whose **simulated public
footprint** matches the trader's **real** footprint. The output is not one guess but a range of
rules consistent with the evidence, with a measure of how tightly each part is pinned down.

How it would work in DOPPEL:

1. **Candidate space.** Genome v2 rule families with free parameters: quote reference and offset,
   inventory skew, size set, re-post trigger and timer, cancel rule, participation conditions.
2. **Simulator.** The twin's fill engine on the real recorded book, with the candidate's own quotes
   injected into the book exactly as the answer-key lab already does, so queue position and
   competition are realistic.
3. **Footprint.** Everything public data records about a wallet: fill times, prices and sizes; the
   signed orders' created time and full size; matched book-level changes; inventory path; which
   markets it entered.
4. **Inference.** Simulate many candidates, compare footprints, keep the consistent ones (neural
   posterior estimation via the open-source `sbi` library, or approximate Bayesian computation for
   a first version). Report each parameter's range and flag the ones the data cannot pin down.

Four conditions make it trustworthy rather than a confident guess:

1. **Calibrate the simulator first.** If the twin's fill model is biased, inference fits the bias.
   Real-order replay already measures it: 4 of 65 orders over-filled the pessimistic bound
   (6.15% against a 5% limit). Fix and re-measure before inferring anything real.
2. **Prove it on the answer key.** Run inference on scripted traders whose true rules we hold.
   Acceptance: the reported ranges contain the true value at their stated rate (for example 90% of
   90% intervals), and the hidden-signal trader comes back "not explained by observed inputs".
3. **Make "how much of the strategy" a measured number.** For each of the six decisions, report how
   narrowly the posterior pins it down, weighted by how much of the trader's P&L that decision
   drives. "75% recovered" then means something checkable, not a feeling.
4. **Check it forward.** A rule recovered this way must still pass the forward test and fidelity on
   markets it never saw. The simulator narrows the candidates; the forward test decides.

It cannot recover what leaves no trace: a signal we do not record, a hedge on another venue, or a
rule change we have no data for. For those, the honest output is a range or "unknown", and the
trader is better used as a sensor (lane B) than as a twin.

## 5. Gaps to close

| # | Gap | Effect | Closed by |
|---|---|---|---|
| G1 | Selection bias in cadence and quote-life estimates | Confidently wrong rules | Calibration fixes + clean Layer 5 exam |
| G2 | Twin fill model not calibrated on real orders | Simulator and fidelity inherit bias | Real-order replay on the cohort, bound ≤ 5% |
| G3 | Decisions to post, cancel, skip invisible | Makers unrecoverable directly | Strategy simulator (§4); our own decision journals |
| G4 | Small matched pool (11/100) and one comparable day per wallet | Few trustworthy subjects | Wider cohort intake with control coverage; persistence study |
| G5 | Operators with several wallets; hedges elsewhere | Half-strategies | Operator grouping (funding + signing fingerprints); hedge test |
| G6 | Signals we do not record (fast perps feed most likely) | Unexplainable traders | Record a US-reachable perps feed; "explained share" gate |
| G7 | No execution plane | No paper twin, no money | Paper runtime, shared with live |
| G8 | Intl-to-US transfer unmeasured | A twin may not transfer | Transfer and capacity model before paper |
| G9 | Opening-price rule for H1–H4 | Sealed families cannot quote | Owner decision (rule built, parity passed) |
| G10 | Platform: no CI, no staging, no alerting, shared credentials | Release and outage risk | Platform track (§6) |

## 6. Build plan

Each phase ends with an acceptance test. Phases A and B run in parallel with the platform track.

**Phase A · Measurement integrity (now → ~Dec 3)**
1. Finish the reserved Layer 5 exam (three seeds); report per seed. *Accept: exam result recorded.*
2. Calibration fixes for selection bias, ladders, takers and the explained-share gate; new
   prospective exam with fresh seeds. *Accept: zero wrongly confident cells; decoy and hidden-signal
   trader get no rule and no PASS.*
3. Real-order replay on the cohort to calibrate the twin. *Accept: bracket violations ≤ 5%.*
4. H1–H4 scored at their closes under the rule the owner chooses.

**Phase B · Find wallets at scale (~Dec 1 → Dec 10)**
1. Fixed-end Goldsky re-pull for the cohort (~$6 of credit) and cohort-wide absorption with
   endpoint routing. *Accept: every wallet reconciled or flagged with a named cause.*
2. Census v2 on reconciled wallets only: mechanism labels from the absorption ledger (complete
   sets vs directional), operator grouping. *Accept: label agreement on a hand-checked sample.*
3. Widen control coverage for the next cohort. *Accept: ≥ 50% of candidates matched.*
4. Persistence checkpoints Dec 4 and ~Dec 25 (already registered).

**Phase C · Strategy simulator (~Dec 3 → Dec 17)**
1. Genome v2 rule families (from rulefit's "cannot express" list).
2. Footprint extractor and simulator harness on the answer-key injection path.
3. Inference engine (`sbi`) with identifiability report and decision-weighted recovery score.
4. *Accept on the answer key: interval coverage at stated rates, zero confident-wrong, hidden-signal
   trader "unexplained".*

**Phase D · First real reconstructions (~Dec 17 → Dec 31)**
1. Persistent, reconciled takers and complete-set builders first; then slow makers via the simulator.
2. Freeze, forward-test on unseen markets, fidelity. *Accept: first genome with a fidelity PASS.*

**Phase E · Paper and money (~Dec 20 → Jan)**
1. Paper runtime: the same strategy code for paper and live; risk limits, kill switch,
   reconciliation, restart recovery; full decision journal (which also becomes ground truth for G3).
2. Live paper beside the source trader; then the US transfer. *Accept: ≥ 14 days paper-vs-twin
   agreement (G5); positive after-cost dollars on untouched days, stressed for the Dec 31 rebate end.*
3. Owner-armed micro-live (G6).

**Platform track (continuous)**
CI on every branch (Python + Rust), a staging host, alerting for recorder gaps and disk, a
secret store with least-privilege service accounts, the console (data health, dossiers, strategy
book, paper P&L, audit trail). See `docs/SYSTEM-DESIGN.md`.

## 7. Risks

- Persistence may thin the pool to very few traders; the plan still works with a handful.
- The most profitable makers may be speed strategies no 250 ms system can twin; they then serve as
  sensors, and the profit path is the engine lane, not the clone lane.
- The simulator can only be as good as the fill model; Phase A item 3 is not optional.
- Capacity on US venues may be small; scale is earned on measured volume, not assumed.

## 9. Adopted from external review (Neon, 2026-11-27)

Checked against the code. Already present, not re-built: anytime-valid e-processes (`strategy/sequential.py`,
aGRAPA, Ville), window/condition-clustered inference (block bootstrap; selection resamples whole
conditions), placebo-block arms (H3), rebate-free profit measure (selection scores settled trading P&L
after fees), and intent time from the signed order's own `created_ms` (E34, more exact than a
latency rollback).

Adopted:
1. **Wallet triage buckets** after Layer 3: A cross-venue / latency, B toxicity-aware maker, C structural,
   D not explained by public inputs (quarantined; sensor lane at most). Feeds the lanes in BLUEPRINT-V3 §3.
2. **Hard negatives**: every fit and the simulator score the wallet's skipped eligible moments and markets,
   not only its trades (participation model; simulator "markets entered vs eligible").
3. **Simultaneous-entry delta** as an explicit fidelity metric: median (clone fire − wallet signed creation),
   reported with its CI next to the existing match-lag gate.
4. **Shared-liquidity replay** before paper: the clone and the source wallet (and visible rivals) consume
   the same depth; a genome whose edge dies when queue use doubles is rejected.
5. **Edge-decay watchdog** for paper and live: one-sided Page–Hinkley on post-fee return per window and on
   paper-vs-wallet agreement; warning halves size, break demotes to shadow; the matrix "fidelity down +
   P&L down = stale clone, re-absorb; fidelity down + P&L up = standalone structural genome; both
   down with fidelity up = regime/crowding, throttle".

Deferred to the execution plane (after a genome passes paper): capital ladder (shadow → canary →
scaled → full), cross-genome netting, orthogonality limit (ρ < 0.30), uncertainty-scaled fractional
Kelly from the simulator's posterior, Rust policy evaluator. Not adopted: offshore venues whose terms
exclude US persons (Binance, Bybit, OKX, Deribit); US-lawful perps feeds are being added instead.

## 8. Decisions needed from the owner

1. Opening-price rule for H1–H4 (bind before Nov 29 00:00 UTC for H1, or keep the old rule).
2. Approve Phase C (the strategy simulator) as the next major build.
3. Apex volume resize before cohort-wide absorption and the simulator runs.
4. Whether to record a fast BTC perps feed (needs a US-reachable public source and disk).
