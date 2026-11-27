# DOPPEL · State of the union (2026-11-27 21:30 UTC)

Engineering and project report: the North Star, what is built and where, how the layers connect,
what is running, why it is slow, and the gaps. Companion documents: `BLUEPRINT-V3.md` (design),
`STATE-AND-PLAN-20261127.md` (build plan), `SYSTEM-DESIGN.md` on `pod/system-design` (institutional
design), `evidence/2026-11-25-probes/FINDINGS.md` (evidence).

## 1. North Star

Find Polymarket-international traders whose profit is real and persistent; recover the rules they
trade by (the genome); prove the recovered rules act like the trader on markets never seen; run them
on paper beside the trader, then on US venues (Polymarket US, Kalshi); make money. Not copy trading:
a rule twin reads the same inputs and fires on the same trigger. Built to institutional standard:
every number traceable, every stage with a test that can fail, nothing live without the owner arming it.

## 2. The system as layers

```
                ┌──────────────────────────────────────────────────────────────┐
 OPERATIONS     │ Apex host (DigitalOcean, 4 cores / 8 GB): release via        │
                │ ops/deploy.sh (main only, atomic swap, rollback), receipts,  │
                │ janitor (protected days), archive, systemd units             │
                └──────────────────────────────────────────────────────────────┘
 PRODUCT        console / desk ............................ static desk only (NOT BUILT as console)
 EXECUTION      paper runtime ─► (live adapter, risk daemon, OMS) ...... paper BUILT on branch; live NOT BUILT
 REGISTRY &     sealed genomes ─ evaluation protocols ─ results ─ e-value monitor ─ promotion gate
 EVALUATION     (registry/genomes.jsonl, 21 genomes + 4 protocol bindings; twin/evaluate.py; lab/promotion.py*)
 RESEARCH       0 select ─► 1 absorb ─► 2 inspect ─► 3 dissect ─► 4 reverse-engineer ─► 5 prove ─► 6 fidelity
 PIPELINE       selection    absorb*    ordertape   decision      rulefit, rivals,       answerkey  fidelity*
                             ledger     ordermatch  context       simulator*             exam
 DATA           recorders: doppel-tape (Python, of record) · doppel-taped (Rust shadow) · PERELMAN BRTI
                (to be absorbed*) · Goldsky fills · chain RPC · perps feeds* · Rust BRTI replica (offline)
                                                                       * = on a review branch, not main
```

How they connect: the **data** layer records markets and identities with receive times. The
**research pipeline** turns one wallet into a verified ledger, its signed orders, the market state at
each decision, a fitted rule, and an accuracy grade. A rule that passes becomes a **sealed genome** in
the registry, bound to an **evaluation protocol** (code hash + parameters) before its window opens. The
**twin** scores it on future data; the **promotion gate** decides whether it may move to **paper**,
which runs the same engine live and journals every decision. Paper journals feed back into research as
ground truth (our own accounts, where every post, cancel and skip is known).

## 3. What is built, and where

### On main and live on Apex (release `23f3d15`; main head `8c83cb3`, docs only since)

| Component | Proof |
|---|---|
| Recorders: 18 streams, hourly receipts, receipt-time gating | Running; 0 recorder restarts since 16:44 UTC |
| Twin replay engine with execution-time trade accounting, inferred unprinted volume, pessimistic queue | E31, E33; 798 Python + 72 Rust tests at release |
| Registry: sealed, hash-chained, append-only; results bind only to the preregistered window; non-PASS never wins | Astra audit fixes, regression tests |
| Evaluation protocols: `received-raw-opening` v1 bound to H1–H4 (registry seq 21–24) | Parity on 960 windows: max $0.005, opening known ~0.4 s before window start |
| Scoring freeze until H4 is scored (Dec 1): evaluator code hash `c2d01f50…` | Written in CLAUDE.md; every branch checked |
| Layers 0–4 on two real wallets (Codex): selection study, absorption, inspection, dissection, rivals | Controlled rivals test reproduced by me: both rules recovered, random control abstained |
| Order tape: signed orders decoded from chain calldata, hash-verified | 8,077 / 8,077 fills (E36) |
| Order matching in the public book | 98% matched vs 4.6% placebo for one wallet-hour (E37, E39) |
| Kalshi bridge study | Kalshi leads PM US by ~0.08 s; S5 as designed loses (E38) |
| Answer-key lab (scripted wallets with known rules) | First score: 67% recovered, 40% of confident claims wrong; calibration fixes merged |
| Rule fitting (out-of-sample honest, exports only what it tested) | Real wallets: abstains correctly |

### On review branches (pushed to GitHub and Apex, not merged)

| Branch | What | State |
|---|---|---|
| `pod/simulator` `44a3dd2` | Strategy simulator (simulation-based inference): candidate rules simulated on the real book, parameter ranges, identifiability, family selection, recovery score | Dev seeds: 0 confidently wrong; hidden-signal and decoys always "not explained". Confirmation run not yet done |
| `pod/paper` `968e49f` | Paper runtime: twin engine live, kill switch, max-loss, hash-chained journal, restart recovery, journal→footprint, twin agreement; BRTI relay | 2 reviews, all findings fixed; full suite running on Apex |
| `pod/promotion` `e918da2` | Promotion gate: deflated Sharpe, PBO, Benjamini–Hochberg, Newey–West; ghost test and disagreement P&L audit in fidelity | Known-answer tests pass; suite queued on Apex |
| `pod/absorb-scale` `0d0f7c1` | Cohort absorption: endpoint routing, resumable runner, census v2 filter, cost planner | **Running on Apex now: 6 of 7 wallets reconciled to chain balances** |
| `pod/brti-absorb` `2c95e85` | PERELMAN's BRTI recorder inside DOPPEL, byte-compatible; parity tool; gap-free cutover plan | Suite queued; cutover after Dec 1 |
| `pod/perps-feeds` `a71edbf` | Lawful US BTC perps (Coinbase Derivatives, Kalshi BTCPERP) recorded; `doppel-leadlag` | 300 s live smoke clean; suite running |
| `pod/system-design` `e9feab2` | Institutional system design document | Done |

## 4. What is running and why it is slow

| Job (all on Apex) | Why it takes long | Expected |
|---|---|---|
| Layer 5 exam, seed 1 of 3 (2 more queued) | Each seed simulates six scripted wallets on six hours of dense real BTC books, then runs five reconstruction tools per wallet; order matching replays every book level change. It runs at the lowest priority, one CPU core, under a memory cap, so the recorders can never lose data. My first launch ran all three seeds at once and starved them of memory for ~5 hours (fixed) | Seed 1 tonight or tomorrow; all three ~1–1.5 days |
| Cohort absorption, 150 wallets | Every share and cash movement is read from the chain through free public endpoints, rate-limited to stay polite (~2 requests/s, ~450 requests per wallet) | ~9 h for the cohort; resumes in budget chunks |
| Five branch test suites, one at a time | ~860 tests each (~15 min alone), run sequentially on purpose (they collide when parallel) | ~1.5 h total |

The structural reason: one 4-core, 8 GB server does production recording **and** all research
compute, and production always wins. The Mac is a 4-core laptop that filled its disk twice. A
dedicated compute host (a second droplet, or cloud runners) would cut these times several-fold
without touching the recorders.

## 5. Gaps (what still separates us from the North Star)

| # | Gap | Effect | Closing step |
|---|---|---|---|
| 1 | No real strategy identified yet | Nothing to run on paper | Phase D: persistent takers / complete-set builders first; simulator for makers |
| 2 | Reconstruction accuracy not yet proven on the official exam | Can't trust a real reconstruction | Layer 5 exam (running) + simulator confirmation run |
| 3 | Public data shows executions, not decisions to post, cancel, skip | Makers hard to recover directly | Simulator; our own paper journals as ground truth |
| 4 | Persistence unproven; small matched pool (11 of 100) | Few trustworthy subjects | Selection study (Dec 4, ~Dec 25); wider cohort intake |
| 5 | Execution plane: no live adapter, risk daemon, OMS, reconciliation | No money can move | After a genome passes paper; owner arms riskd |
| 6 | Fast lead signals: perps recorded only on a branch; offshore feeds undecided | Possible hidden trigger | Deploy perps streams; owner decision on Binance/Hyperliquid data |
| 7 | BRTI depends on PERELMAN | External dependency | Absorb (branch ready), shadow, cut over after Dec 1; later own Rust replica live |
| 8 | Transfer intl → US and capacity unmeasured | A twin may not pay on US venues | Shared-liquidity replay and stress suite (after Dec 1 freeze) |
| 9 | Platform: no CI, no staging, no alerting, shared credentials, one host | Release and outage risk; slow research | Compute/staging host; CI; alerts; secret store |
| 10 | Console not built | Hard to see the system | After paper runs |

## 6. Key dates

| Date (UTC) | Event |
|---|---|
| Nov 29 00:00 | H1 window closes; score under the bound protocol |
| Nov 30 00:00 | H2, H3 close |
| Dec 1 00:00 | H4 closes; scoring freeze lifts; BRTI cutover and twin stress suite may proceed |
| Dec 4 03:30 | First selection-study checkpoint |
| ~Dec 25 | Selection decision (four weeks vs matched controls) |
| Dec 31 | PM US API taker rebate ends |

## 7. Decisions for the owner

1. Compute: a dedicated research/staging host (or cloud runners) to stop research competing with recording.
2. Apex tape volume resize (55 GB free; ~4 days at current rate with perps added).
3. Offshore feeds already recorded by PERELMAN (Hyperliquid, Binance public data): keep or drop.
4. Merge order for the review branches after the suites pass (recommended: promotion, absorb-scale,
   perps-feeds, paper, simulator; brti-absorb with its cutover after Dec 1).
