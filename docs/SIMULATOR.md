# Strategy simulator: a posterior over a wallet's rule from its public footprint

`doppel-simulate` (`src/doppel/lab/simulator.py`) is Phase C of
[the build plan](STATE-AND-PLAN-20261127.md) (section 4). It uses simulation-based inference.
Candidate rules are simulated on the recorded book. The candidates whose **public footprint**
matches a wallet's footprint are kept. The result is a range for each parameter, a flag saying how
tightly the data pins it down, and an `explained` verdict. The module docstring is the full
specification.

```sh
# 1. reference table: n prior draws per family, each simulated once on the tape
doppel-simulate table --data TAPE --tokens LAB/tokens.jsonl --stream clob_btc_5m \
    --from 2026-11-26T06:00 --to 2026-11-26T12:00 --n 150 --workers 1 --seed 1 --out table.jsonl
# 2. one wallet: its order-tape rows and the tape that holds its public book
doppel-simulate infer --table table.jsonl --tape TAPE --orders orders.jsonl --tokens LAB/tokens.jsonl \
    --wallet 0x... --out DIR
# 3. validation on scripted subjects with known parameters (answer-key path)
doppel-simulate validate --data TAPE --tokens LAB/tokens.jsonl --from F --to T --n 120 \
    --per-family 4 --hidden 4 --loo 10 --workers 4 --seed 4242 --out DIR
```

Seeds 271031, 271067 and 271091 are reserved for the answer-key exam. Every entry point refuses
them, derived seeds skip them, and a harness subject that drew an exam parameter set stops the run.

## How it works

**Simulator.** Each candidate runs through `answerkey.Sim`. That is the twin's `QuoteSim` with
the candidate's own quotes injected into the book, the pessimistic queue and seeded latency.
`simulate` drives `Sim` in its own loop, which writes nothing to disk. It folds each finished window
into slim public rows and drops that window's orders, so memory holds one window of orders.
`FootSim` adds one thing: at every fill it records the public book of both tokens, meaning the real
levels plus the candidate's own resting size. A replay of the published tape shows exactly that
book.

**Same function on both sides.** A candidate's evidence is `answerkey.ordertape_rows`, the signed
rows of filled orders only. A wallet's evidence is the same rows read from its `orders.jsonl`. Both
become slim BUY-equivalent tuples (`slim`) and go through one function, `footprint`. A test checks
that a subject written by answerkey's public-evidence writer, read back from its files, gives the
same footprint as the in-memory candidate with the same rule and seed. Candidates never read truth
fields such as landing time, queue, status or unfilled orders.

**Footprint (28 statistics).**

| group | statistics |
|---|---|
| activity | fills and filled orders per eligible window; share of eligible windows entered |
| timing | fill time within the window (q10, q50, q90) |
| price | fill price − public best bid, fill price − public mid, at the fill (q10, q50, q90) |
| size | median signed full size, number of distinct sizes, q90/q10 spread |
| filled size | mean filled/full; share fully filled |
| age | log(1 + block time − signed `created_ms`) at the first fill (q10, q50, q90) |
| inventory | path |net Up| / median size (q50, q90); end-of-window |inv| |
| side | Up share of filled shares; share of entered windows with ≥ 90% on one token; taker share |
| outcome | share of filled shares on the resolved winner; P&L per filled share (c) |

Outcome statistics read resolved payouts after the fact. They describe what happened and are never
a decision input. Policies decide only from `answerkey.Ctx`, the book and own state received by the
decision time.

**Families and priors.**

| family | parameters (prior) | decisions |
|---|---|---|
| midpoint_maker | h_c U(0.5, 5); sizes {10,25,50 / 20 / 5,10,15 / 50,100}; timer_s U(1, 20); move_c {1,2,3}; band_c U(10, 50) | price, size, timing, exit, market |
| fair_maker | h_c U(1, 6); k U(0, 0.15); size {10, 20, 40}; timer_s U(2, 30); move_c {1,2,3}; band_c U(10, 50) | price, side, size, timing, exit, market |
| complete_set | m_c U(0, 6); size {10, 25, 50}; max_age_s logU(1, 60); band_c U(10, 50) | market, size, exit |
| touch_ladder | levels {0,1 / 0,1,2 / 0,2,4 / 0,1,2,3}; sizes {97–100 / 20,30 / 50}; max_age_s logU(1, 60); band_c U(10, 50) | price, size, exit, market |
| random_taker | rate_per_s logU(0.005, 0.1); sizes as midpoint | timing, size |
| null | rate_per_s logU(0.01, 1); taker_share U(0, 1); life_s logU(1, 60) | timing, side, exit |

The first five families are answerkey's policies, unchanged. `Gated` wraps them with two gates of
the simulator's own:
- `band_c`: the participation band. The family quotes only while |mid − 50| ≤ band_c.
- `max_age_s`: the cancel age. A live quote older than this is cancelled.

`null` (`RandomPolicy`) acts at Poisson times on a random token. A share of its actions are takes
at the ask. The rest are bids 0–3 c behind the best bid, with random sizes and exponential lives.
random_taker has no rule beyond its activity; it is answerkey's decoy. It therefore sits in the
**null class** together with `null`.

`fair_maker.move_c` is inert, because `FairMaker` requotes on any 1 c target change. It is kept in
the prior on purpose, as a parameter the method must flag UNIDENTIFIED.

**Inference (rejection ABC on a reference table).**
- Each statistic is scaled by its robust spread over the whole table. The distance is the RMS of
  the standardized differences. A statistic missing on one side counts as 3 sd.
- Each family keeps its k = 10% nearest simulations. Their parameters are the posterior: the median
  and 90% interval for each parameter, or the 90% credible set for a choice.
- **Identifiability**: the posterior width divided by the median width of a random k-subset of the
  family's prior draws. A ratio ≤ 0.5 is IDENTIFIED, ≤ 0.8 is PARTIAL, and anything higher is
  UNIDENTIFIED.
- **Model evidence**: the pooled k × (number of families) nearest simulations, counted per family
  (equal n per family), with Dirichlet 90% intervals.
- **Adequacy**: two checks.
  - The observed mean k-NN distance must sit inside the family's own leave-one-out distribution
    (p ≥ 0.05).
  - Every observed statistic must lie inside the accepted particles' range ± 0.5 sd. A miss is a
    `misfit`.
- **Explained**: the best rule family is adequate, more probable than the null class, and nearer
  than every null-class family. Otherwise the verdict is "not explained by observed inputs" and no
  posterior is reported.
- **Decision-weighted recovery**: each parameter belongs to one of six decisions (market, timing,
  side, price, size, exit).
  - Concentration = 1 − width ratio.
  - Weight = the first-order share of simulated P&L variance that the decision's parameters
    explain in the family's table.
  - `score_within_family` = Σ weight × concentration; `score` multiplies it by the family's
    probability.
  - Decisions with no free parameter in the family are listed as `fixed_by_family`.

`sbi` (neural posterior estimation) is not used. It would add torch, and rejection ABC with a
reference table already answers the acceptance questions. See Limits.

## Validation harness

RESULTS_PLACEHOLDER

## Limits

- **The simulator inherits the fill model.** Candidates use the answer-key fill path, so a bias in
  `QuoteSim` on real books is fitted, not detected. Plan section 4 condition 1 (real-order replay,
  bracket violations ≤ 5%) must be met before reading a real wallet's posterior.
- **The harness runs candidates on the pristine tape.** Subjects publish into their own copy. A real
  wallet's tape already contains its quotes, which candidates never see in their book. The
  harness numbers are optimistic on this point.
- **Ordermatch placement and outcome rows are not used.** They need each candidate's rewritten tape
  on disk and an ordermatch run per candidate. Age at the first fill, from the signed `created_ms`,
  carries most of that information, but cancels of unfilled quotes stay invisible.
- **Only these families are tested.** A strategy outside them (signal-driven side choice, hedges,
  size schedules) can at best come back "not explained". The hidden-signal subject is not explained
  because no family produces one-sided, outcome-correlated fills, not because the method detects a
  hidden signal as such.
- **Rejection ABC with a few hundred draws per family is coarse.** k is small, so intervals are
  wide and the 0.5-sd predictive pad is a judgment call fixed before the harness ran. Coverage below
  comes from one tape and one seed.
- **P&L weights are first-order binned variance shares on the table.** Interactions are not
  attributed, and noise inflates small shares.
- **A taker family with a rule is missing.** answerkey's taker is random, so takers can only be
  "matched by a random policy" until sniper or endgame families are added.
