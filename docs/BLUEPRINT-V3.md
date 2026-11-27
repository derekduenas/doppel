# DOPPEL — System Blueprint v3 (rule twins)

Status: draft for owner review, 2026-11-26. Supersedes the framing of `BLUEPRINT.md` (v2); v2's
architecture, data contracts and gates stay in force where this document does not change them.
Every number below cites a row in `evidence/2026-11-25-probes/FINDINGS.md`.

## 0. Thesis

Polymarket international is the only deep prediction market where every fill is public with the
trader's identity. DOPPEL reads those traders off the chain, works out the **rules** each one trades
by, and runs the same rules itself.

> **Not copy trading.** A copier sees a trade and follows it, late, at a worse price. A rule twin
> reads the same inputs the trader reads and fires on the same trigger, so it acts in the same
> moment — on venues (Polymarket US, Kalshi) those traders cannot reach.

The unit of work is one trader's strategy, written down as a **genome**: an explicit, sealed,
executable rule set. "DNA" is its measured behaviour. A genome is accepted only when it has been
shown to behave like the trader, on data it has never seen.

## 1. What "1:1" means

A genome is a 1:1 rule twin of a trader when all four hold:

1. **Ledger truth.** Everything the trader executed is absorbed and reconciles to their on-chain
   share and cash balances.
2. **Rule recovery.** Each part of the rule (which markets, when, which side, price, size, exit) is
   recovered with stated uncertainty, and rival explanations have been tested against each other.
3. **Forward agreement.** The frozen genome, run on later markets it never saw, makes the trader's
   decisions: same windows, same side, same price within a cent, same second, beating a chance
   baseline and a simple baseline.
4. **Live agreement.** Running on paper beside the real trader in real time, it keeps agreeing.

Anything short of that is reported as what it is: partial, estimated, or unknown. The machine itself
is graded on scripted traders whose true rules we hold as an answer key (§4, stage 5).

## 2. What the evidence says so far

| Finding | Consequence for the design |
|---|---|
| The signed order behind every fill is on chain: creation time, full size, limit price (E34). 8,077 of 8,077 cohort fills verified (E36 and the 12-wallet run). | Dissection works on orders, not just fills. |
| Those orders can be found and followed in the public book: 98% matched vs 4.6% placebo for one wallet-hour (E37, E39). | Earlier single-wallet placement estimates require independent checking. Ownership and exact withdrawals are unresolved in anonymous depth. |
| One studied maker appears to post sub-second quotes and fill on ~0.3% of them (E39, an estimate). | Some traders' rule is speed. A 250 ms system cannot be that trader; see lanes (§3). |
| Every cohort wallet has one comparable day (E28). | Persistence must be shown before reconstruction effort is spent. |
| Plain US makers lose 0.6–1.6 c/contract (E35). Kalshi leads Polymarket US by ~0.08 s (E38). | The US engine needs signals and speed; whether intl leads the US venues at our latency is still unmeasured. |
| Independent audits found the system could label a loser a winner, project profit from windows it never traded, and pass a clone that lost money. | Fixed or in review; v3 makes every stage carry a test that can fail. |

## 3. Three lanes, one pipeline

Every persistent trader is triaged before heavy work.

| Lane | Who goes here | What DOPPEL does with them | Proof |
|---|---|---|---|
| **A. Rule twin** (the core) | Traders whose rule can be executed at our latency: complete-set builders, endgame and structural traders, resting makers, slower takers | Full reconstruction to a 1:1 genome; paper beside the trader; then the same rules on US venues | The four conditions in §1 |
| **B. Sensor** | Traders too fast to twin, or explained only by inputs we lack, but whose activity carries information | Their live activity becomes a labelled signal: pull, widen or lean | Dollars added to a US strategy versus a placebo that acts equally often |
| **C. Engine** | No single trader | One fast fair-value maker/taker on US venues that consumes lane A triggers and lane B signals | Preregistered families, future days only |

Lane A is DOPPEL's identity and its accuracy proof. Lanes B and C are how knowledge about traders we
cannot twin still earns.

## 4. The pipeline

| # | Stage | Output | Test that can fail |
|---|---|---|---|
| 0 | **Select** | Traders whose profit persists after the cohort freeze, versus matched controls | G1: beats controls in 3 of 4 rolling windows |
| 1 | **Absorb** | Complete history: fills, splits, merges, redemptions, transfers; signer vs account; wallets grouped by operator | Every pull reaches its last page; shares and cash equal on-chain balances |
| 2 | **Inspect** | Signed order, located execution, candidate placements, bounds on residence, and unresolved ends | Verify order hash and execution economics, compare paired book matches against a shifted placebo |
| 3 | **Dissect** | What the trader could see at each decision (book, fair value, Kalshi, flow, own inventory), for markets traded and markets skipped; complete-set pairing; outside-hedge test | No-look-ahead tests on every input; share of behaviour the inputs explain |
| 4 | **Reverse-engineer** | A draft genome with uncertainty, plus rival explanations and the moments that separate them | Out-of-sample on a held-out slice; a draft that cannot quote like the trader is not emitted |
| 5 | **Prove the machine** | Accuracy score on scripted traders with known rules, including a decoy and a trader using a signal we do not record | Recovery accuracy and false-confidence rate; the decoy and hidden-signal trader must not get a confident rule |
| 6 | **Verify the twin** | Fidelity verdict on later markets the genome never saw | PASS needs activity, one-to-one fills, position path, P&L level and correlation, all beating a same-window chance baseline; too little data is INCONCLUSIVE |
| 7 | **Paper twin** | The sealed genome running live on paper beside the trader, then on US venues | Real-time agreement over ≥14 days; then twin economics on US venues |
| 8 | **Live** | Micro-size, owner-armed only | Paper-versus-live agreement; automatic demotion on drift |

Stages 1–6 are built or in review. Stage 5 (the answer key) and the full stage-1 ledger are being
built. Stages 0 and 7 are the next builds.

**November 27 implementation update.** Stage 0 now has a review implementation and a
prospectively frozen wallet-level comparison. The first real walkthrough wallet has one
matched control and four weeks to observe. Cohort-wide PIT skill and the later
strategy-identification stages remain open. [Layer 0 audit and evidence](WALLET-SELECTION.md)

Stage 1 has now been exercised on that same candidate and control. The first 15 minutes and
a subsequent 24-hour capture reconciled independently through source fills, Polygon events
and both collateral balances. The intervening 37 minutes and 7 seconds lack a supporting
Goldsky capture. Stage 2 starts with a signed order whose timestamp is later than its fill.
[Layer 1 audit and receipts](WALLET-ABSORPTION.md)

Stage 2 has now followed those same wallets from signed fill to public book. Execution
anchors, eligible search windows, ambiguous placements and unknown cancellations have
separate counts. The first candidate buy has two compatible additions and a 1.329-second
upper bound on continuous displayed residence. The day had 59 sole additions versus 60
shifted for the candidate and 241 versus 25 shifted for the control. Layer 3 needs a decision-time anchor before
it can label posting behaviour. [Layer 2 audit and receipts](WALLET-INSPECTION.md)

## 5. Genome v2

Rule fitting on real orders listed what today's genome format cannot say. v2 adds: quotes pegged to
the best price on a side; a different reference per side; several price levels; size drawn from a
set; sub-second re-post and quote life; re-post on own fill; conditional one-sided quoting; paired
legs (complete sets, hedges); per-asset parameters; sub-cent prices. A genome also carries its
provenance: the trader, the evidence window, the fit report, the fidelity verdict and the lane.
Sealed v1 genomes keep their ids.

## 6. Intelligence, and what decides

Used where it adds accuracy, never as the judge:

- **Rival hypotheses.** An LLM proposes two or three candidate rules per trader; the system searches
  the record for moments where they would act differently and scores them there.
- **Posterior over rules.** Simulation-based inference gives a range for each parameter and says
  which ones the data cannot pin down.
- **Choice models** over every eligible market, including the ones the trader skipped.
- **Point-process models** for cancels and never-filled quotes, kept as stated uncertainty.
- **Operator graphs** from funding flows and order-signing fingerprints.

The reconciled ledger says what happened. The answer key says how good the machine is. The forward
test says what was learned. Models that fit the past and fail those are rejected.

## 7. Known limits

- Orders that never fill are not on chain; their attribution is an estimate with a stated chance rate.
- Order timestamps are self-reported; they are checked against the book.
- A trader may use inputs we do not record (a fast perps feed is the likeliest) or hedge elsewhere.
  Those traders are labelled, not twinned.
- A rule that works on intl may not transfer: fees, flow, queue and our latency differ. Transfer and
  capacity are measured before paper.
- Strategies change. Reconstruction is per regime, and live agreement is monitored for drift.

## 8. Build order

1. Answer-key lab and absorption ledger (in progress).
2. Stage 0: persistence on cohort v1; triage into lanes.
3. Rival hypotheses and the forward test; dossier verdict as four separate scores.
4. Genome v2.
5. One trader per lane, end to end.
6. Paper runtime: the same strategy code for paper and live, with risk limits and reconciliation.
7. US transfer for passing twins; lane B and C experiments (first: Kalshi as the pull signal).
8. Micro-live when a family passes on future days and the owner arms it.

## 9. Owner decisions

1. Apex storage for the registered evaluation inputs (resize planned).
2. Release of the reviewed integration branch.
3. Whether to record a fast BTC perps feed (likeliest hidden input; needs disk and a US-reachable source).
4. A small owner-armed live calibration probe on Polymarket US: the only ground truth for our own
   fills. Off unless armed.
