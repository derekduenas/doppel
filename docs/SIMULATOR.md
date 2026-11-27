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
    --wallet 0x... --calibration calibration.json --out DIR
# 3. validation on scripted subjects with known parameters (answer-key path)
doppel-simulate validate --data TAPE --tokens LAB/tokens.jsonl --from F --to T --n 120 \
    --per-family 4 --hidden 4 --calib-rows 60 --workers 4 --seed 4242 --out DIR
# 4. re-infer saved validate runs under the current rule (no simulation)
doppel-simulate rescore --table table.jsonl --calibration calibration.json DIR [DIR=SEED ...]
```

`validate` writes each subject's public evidence (its own copy of the tape) to a temporary directory
and deletes it once the footprint is read; `--work DIR` keeps the copies there instead. The output
directory keeps only the table, `calibration.json`, each subject's `inference.json` (with its
footprint) and `validation.json`, which is all `rescore` needs.

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

`validate` draws scripted subjects from each family's prior, runs them through answerkey's `Sim` and
`write_public`, and reads each subject's footprint back from its `public/` files, as for a real
wallet. Inference runs against a reference table built on the pristine tape. The harness also
draws hidden-signal subjects: answerkey policy (f), accuracy U(0.65, 0.9).

Scoring:
- **coverage**: the share of true parameters inside the true family's 90% interval or set.
- **false confidence**: a true value outside an interval flagged IDENTIFIED.
- **family selection**: the explained best family equals the true family.
- The table self-check (`--calib-rows`, or a saved `--calibration`): held-out table rows serve as
  pseudo-subjects (simulation-based calibration). It gates IDENTIFIED per parameter (iteration 2).

All numbers below are from one **synthetic** tape (`tests/test_answerkey.synth`, 60 min, 3 Hz,
seed 2026, 12 windows of 5m BTC), measured on macOS on 2026-11-27. The reference table holds
6 families × 120 draws, with `--seed 4242`.

| run | subjects | coverage (95% CI) | IDENTIFIED cells / wrong | family selection | hidden signal | decoys | table LOO coverage (95% CI) / wrong |
|---|---|---|---|---|---|---|---|
| development, seed 4242, first code | 28 | 0.877 of 57 | 10 / 2 | 12/16 | 4/4 not explained | 8/8 not explained | 0.863 of 139 / 3 of 22 |
| development, seed 4242, after the two fixes below | 28 | 0.865 of 52 (0.75–0.93) | 10 / 2 | 11/16 | 4/4 not explained | 8/8 not explained | 0.890 of 127 / 1 of 16 |
| **fresh, seed 9091** (subjects first drawn after the fixes) | 28 | **0.833 of 66 (0.73–0.90)** | **11 / 1** | **14/16 (0.64–0.97)** | **4/4 not explained** | **8/8 not explained** | **0.918 of 134 (0.86–0.95) / 0 of 18** |

The first run exposed two defects, both fixed before the fresh run:
- A simulation with no fills is missing every statistic. It scored 3 sd per statistic and so sat
  "nearer" than real but dissimilar simulations. Finite differences are now capped at 3 sd.
- A subject with zero fills was "explained". It is now refused: there is no footprint.

**Against the plan's acceptance (section 4, condition 2):**
- *Coverage at the stated rate*: the table calibration meets it (0.92). The answer-key subjects come
  in a little under (0.83), but the 95% CI includes 0.9.
- *Zero confident-wrong*: **not met**. One of 11 IDENTIFIED cells was wrong on the fresh run, and 2
  of 10 on the development run. All three were the midpoint offset `h_c` or the complete-set band
  `band_c`:
  - `h_c` truth 0.81 against an interval of 0.85–1.88, and 3.71 against 2.53–3.30;
  - `band_c` truth 49.9, at the prior's upper edge, against 33.9–48.8.

  MidMaker's floor/ceil rounding puts `h_c` on 0.5 c plateaus, and k = 12 particles give coarse
  interval ends.
- *Explained by the wrong family*: 0 subjects in either final run (it did happen once in a
  12-draw smoke run). Coverage counts explained subjects only: 5 of 16 rule subjects (seed 4242) and
  2 of 16 (seed 9091) were not explained and are absent from the coverage cells.
- *Hidden-signal subject "not explained"*: met, 8 of 8 across both runs. Random and decoy subjects
  were never explained either.

**What the footprint pins down on this tape:**
- *IDENTIFIED or PARTIAL in most subjects*: the midpoint offset `h_c`, the size or size set, the
  complete-set margin `m_c`, and often the participation band.
- *Almost always UNIDENTIFIED*: the re-post timer, the move trigger, the cancel age `max_age_s`
  and the ladder levels.
- *fair_maker* (70–130 fills an hour) leaves nearly everything UNIDENTIFIED. Its inert `move_c` is
  UNIDENTIFIED in every fair subject, as it should be. In the unit test, an inert `dummy` parameter
  is UNIDENTIFIED.
- *Recovery scores* run 0.07–0.58. The most is recovered for touch ladders and complete sets, whose
  P&L is driven by size and the band, and the least for fair makers.

**Runtime and memory** (4 cores, `--workers 4`):
- Table: 720 simulations of the 60-min tape in 1,845 s, a median of 8.5 s per candidate-hour.
- 28 subjects (simulate, publish, replay, infer) took 260–300 s in one process.
- Peak RSS: 198–216 MB for the main process and 156 MB for a worker. A candidate holds one window
  of orders: finished windows are folded into slim rows (tested).
- Real BTC books are denser than the synthetic one, so measure `sim_s` and `peak_rss_mb` on Apex
  before sizing `--n` and `--workers` under the 1.5 GB limit.

## Iteration 2: regression adjustment, gated IDENTIFIED, 300-draw table

**Status: the confirmation run (seed 7777) has not been run.** Everything below is development data
(table self-check and dev seeds 5101–5104, never 4242, 9091 or the reserved exam seeds).

- **Regression adjustment** (local-linear, Beaumont 2002) on the accepted draws, in each prior's
  logit coordinate, ridge 1, kernel weights. Every continuous parameter reports an `adjusted` median
  and 90% interval beside the rejection one.
- **IDENTIFIED** needs all three: a narrow primary interval (width ratio ≤ 0.5); ends more than 2% of
  the prior range from its bounds (a bound-touching interval is at most PARTIAL); and a passing table
  self-check for that parameter (≥ 10 held-out rows with a narrow interval, truth covered ≥ 90%).
  `capped_by` names the condition that held a cell back. Without `--calibration`, `infer` flags
  nothing IDENTIFIED.
- **Table**: 300 draws per family (`build_table(start=...)` extends without recomputing).

| table | self-check coverage, rejection interval | adjusted interval |
|---|---|---|
| 120 per family | 0.865 | 0.769 |
| 300 per family | 0.918 (95% CI 0.898–0.934) | 0.840 |

The adjustment under-covers at both sizes (28 statistics, 12–30 accepted draws), so the **frozen
rule** uses the rejection interval as primary and reports the adjusted one beside it. On the 300
table only `complete_set.size` and `touch_ladder.sizes` pass the self-check; `h_c` covers 0.81 in
its 21 narrow held-out cells and is capped at PARTIAL.

Dev seeds 5101 and 5102 under the earlier adjusted-primary rule: 0 wrong IDENTIFIED cells (2 and 3
IDENTIFIED); the gate stopped 2 and 3 narrow cells that missed; coverage 0.873 and 0.893; hidden
signal 4/4 and decoys 8/8 not explained. Rescoring 5101–5104 under the frozen rule (`rescore`) and
the one confirmation run are to be run on Apex.

## Limits

- **The simulator inherits the fill model.** Candidates use the answer-key fill path, so a bias in
  `QuoteSim` on real books is fitted, not detected. Plan section 4 condition 1 (real-order replay,
  bracket violations ≤ 5%) must be met before reading a real wallet's posterior.
- **The harness runs candidates on the pristine tape.** Subjects publish into their own copy. A real
  wallet's tape already contains its quotes, which candidates never see in their book. The size of
  this gap is unmeasured, and the harness numbers are likely optimistic on this point.
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

## Next on Apex (real book; the lead runs it)

Run inside the low-priority research unit, which has a 1,500 MiB memory limit. Use a range that
does not overlap the reserved exam (the exam is Nov 26, 06:00–12:00).

1. Real-order replay calibration first (plan Phase A item 3). Read no real posterior until bracket
   violations are ≤ 5%.
2. `doppel-simulate validate --data SEALED_TAPE --tokens LAB/tokens.jsonl --stream clob_btc_5m
   --from F --to T --n 150 --per-family 4 --hidden 4 --calib-rows 60 --workers 1 --seed <new>
   --out SCRATCH/sim-validate`. Start with `--workers 1` and read `peak_rss_mb` and `sim_s_median`
   before raising either.
   Cost: `validate` writes one rewritten copy of the stream per subject, one at a time, into a temp
   dir it deletes as it goes (unless `--work`), and 6 × 150 candidates at one worker run about 900 × the per-candidate time (8.5 s per
   synthetic hour). That takes hours on a dense real book, so start with a short range.
3. Only if (2) shows no wrongly confident cells, run `doppel-simulate table` on the wallet's range
   and `doppel-simulate infer` with its order-tape JSONL.
