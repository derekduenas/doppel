# Reconstruction: from a wallet's fills to its order-level behaviour

`doppel-reconstruct` takes a frozen cohort and a time range and produces, for each wallet, four
things in order: the verified order tape, the book match, the twin replay and the ledger card. A
manifest records what went in, what came out, and the gate results to read before trusting any of
it. This page says what each stage proves, how, and what it cannot see.

Evidence rows (E34, E36, E37, E39) are in `evidence/2026-11-25-probes/FINDINGS.md`. Where a claim
has no row yet, this page says so.

## The four stages

| stage | tool | what it establishes | how it is checked |
|---|---|---|---|
| 1. Order tape | `doppel-ordertape` | For every FILLED order of a wallet: when it was signed, its full size and its limit, not only the filled part (E34). | Chain hash check, per order. |
| 2. Book match | `doppel-ordermatch` | Where that order sat in the anonymous public book, when it left, and an estimate of the quotes that never filled (E37, E39). | Placebo-controlled match rate. |
| 3. Twin replay | `doppel-orderreplay` | How the twin's maker fill model behaves on those real orders: its fills against the real fills. | Adapter gate and bracket. No FINDINGS row yet. |
| 4. Ledger | `doppel-profile` | Per wallet, which parts of the strategy are measured, which are estimated and which are unknown. | Fixed status rules with n and a stated false-positive rate. |

### 1. Order tape: the chain hash check

Every intl fill is settled by a `matchOrders` call whose calldata carries each signed order in
full (E34). The tool fetches the cohort's transactions from a public RPC, decodes the orders and
recomputes each order's EIP-712 hash. An order row is `verified` only when that hash equals an
`order_id` the fill store reports for a fill in the same transaction. On the first sample all
1,534 fills of one wallet had a decoded order with the identical id (E36).

Proven by this stage: the order's maker, token, side, size, limit and the fills it received.
Not proven: `created_ms`. The signer writes that stamp; the chain does not check it. Stage 2 tests it.

### 2. Book match: the placebo control

An order is "matched" when the book shows an increase of exactly its size, at its price and side,
within -0.3 to +2.5 s of its signed stamp. Someone else could have posted that size then, so the
same test is run with every stamp shifted by 60 s. The placebo rate is how often a match appears
by chance. On one wallet-hour 98% of observable orders matched against a 4.6% placebo (E39). The
first probe (E37) measured the same wallet in the same hour with a looser rule and got 93%: the
two rows are one sample, not two. A high match rate with a low placebo rate is also the evidence
that a wallet's `created_ms` stamps are real. How close they are is per wallet: for that one
wallet the venue stamp trailed the signed stamp by 42 / 147 / 751 ms at p10 / p50 / p90 (E37).

A matched order is then followed forward. Its fills are known from chain and timed by the print
that carries their transaction hash. If the remainder leaves the level in one exact decrease that
prints do not explain, the order is `cancelled` (cancel and replace look the same). Anything else
is `unknown`. On the E39 wallet-hour: 141 filled, 123 partly filled then cancelled, 76 unknown, 2
proven wrong matches.

The fingerprint estimate: if a wallet uses a few fixed sizes, the tool counts unmatched bid
increases of those sizes in the wallet's tokens and compares them with the rate in control tokens.
E39 found 124,429 such increases against 2,942 expected by chance. This is an estimate and never
an identification; identity precision was at most 0.87 there.

### 3. Twin replay: brackets

Each matched order is replayed as one quote through the twin's own maker fill code, on the intl
tape, under two queue assumptions: `pessimistic` (everything at the level when the order arrived
is ahead of it) and `optimistic` (nothing is). Under first-in-first-out with truthful prints the
real fill must lie between the two. So:

- `ratio` = twin shares / real shares, with a CI from resampling markets. It measures the fill
  model's bias **given that the order did fill**. It is not a fill probability.
- A pessimistic over-fill or an optimistic under-fill is a `bracket_violation`: a data, adapter or
  match problem, not model bias.
- `adapter_gate` checks the plumbing first: `routed_share` is the share of real fills' prints that
  arrive at the order's price with the right aggressor side; `delivered_share` is the share of
  those that reached the quote while it was live. If either is low, the ratios measure the adapter.
- `placement_not_found` counts orders whose placement event the replay could not find again.

The [2026-11-26 audit](SYSTEM-AUDIT-20261126.md) records observed routing and delivery for two
archived BTC samples. For reconstructed calls, the adapter checks maker-leg count, token pair,
direction, fill-store verification and quantity conservation before allocating the print by price.
The report keeps raw and allocated routing counts, placement-candidate counts and orders exposed
to unresolved same-side prints.

### 4. Ledger: named obligations

The ledger lists eleven parts of a strategy (software identity, universe, size rule, price rule,
placement cadence, cancel/replace, fill footprint, inventory rule, taker behaviour, P&L source,
copyability). Each gets a status: VERIFIED (measured directly, with n), ESTIMATED (inferred, with
a stated false-positive or placebo rate) or UNKNOWN (with what data would resolve it). `coverage`
is the weighted share that is VERIFIED, plus half weight for ESTIMATED. The rules and weights are
in the `profile.py` docstring. The copyability thresholds are provisional, set from one
wallet-hour (E39).

## Identified, estimated, invisible

| | what | why |
|---|---|---|
| Identified | A filled order's maker, token, side, size, limit, fills | Hash check against the fill store (E34, E36) |
| Measured or inferred | Signed `created_ms` is visible. Book placement and sign-to-book latency are inferred | Candidate counts and placebo measure ambiguity. Anonymous L2 has no order ID (E37, E39) |
| Identified when classified | End of a matched order: `filled`, or `cancelled` by an exact decrease | Follow-forward rules; `unknown` otherwise (E39) |
| Estimated | Quotes that never filled, fill ratio, re-post rate | Fingerprint sizes against a control rate; precision at most 0.87 on E39 |
| Estimated | Copyability verdict for makers | Provisional thresholds from one wallet-hour (E39) |
| Invisible | Any single order that never filled | It is not on chain (E34) and the book is anonymous |
| Invisible | Whether a remainder was cancelled or replaced | Both are one decrease in the book |
| Self-reported | `created_ms` | Written by the signer. 113 of 6,492 stamps in E36 were after their block; they are flagged and screened |
| Lower bound | An order's chain fills | Only fetched transactions are known (`--max-txs-per-wallet`) |

Three limits come from the pipeline itself.

- **Identity precision is not measured.** A cohort run selects transactions from the cohort's
  fills and verifies all available claims for those transactions. `other_known` covers only
  the other makers verified in that partial sample, so
  `identity_precision_upper` is written as null, with the reason, in every document the pipeline
  writes (unit and merged `ordermatch.json`, ledgers, report). The 0.87 of E39 came from a JSONL
  sample that kept every maker.
- **Fingerprint floor.** A wallet needs at least 30 maker orders inside one chunk to get a size
  fingerprint there; below that its footprint is absent, not zero.
- **Verification follows the fill store.** Only rows marked `verified` count, in the gate and
  in the book match. The order tape step checks the current store's maker address and order hash
  for the selected transactions. It reuses decoded rows from a shared tape and fetches transactions
  whose parts are missing.

## Gates

One table, in `reconstruct.py` (`GATES`), copied into every manifest. **All thresholds are
provisional.**

| gate | passes when | needs at least | rests on |
|---|---|---|---|
| `ordertape.verified_share` | >= 0.99 of the store's fills in fetched txs have a verified decoded order with that id | 1 fill | E36: 1,534 of 1,534 |
| `ordermatch.match_rate` | >= 0.90 | 30 observable orders | E39 0.98 (E37 0.93 is the same wallet-hour, looser rule); same numbers as `profile.T` |
| `ordermatch.placebo_rate` | <= 0.10 | 30 observable orders | E39 0.046 (the same wallet-hour) |
| `orderreplay.routed_share` | >= 0.95 | 30 prints seen | no FINDINGS row yet |
| `orderreplay.delivered_share` | >= 0.95 | 30 prints routed | no FINDINGS row yet |
| `orderreplay.placement_not_found_share` | <= 0.01 of orders to replay | 30 orders | no FINDINGS row yet; 0 expected |
| `orderreplay.bracket_violation_share` | <= 0.05 of headline orders | 30 orders | no FINDINGS row yet; 0 under FIFO |

The two ordermatch thresholds rest on one wallet in one hour.

Each gate is evaluated per wallet and is `pass`, `fail` or `not_evaluable` (too few cases, or the
step did not run). A negative share is `fail`: it means the inputs disagree. A wallet's output of
a step is trusted only when every gate of that step and of the steps it reads from is `pass`:

- order tape gate -> book match, twin replay, ledger
- book match gates -> twin replay, ledger
- twin replay gates -> the twin ratios

Three conditions make a step untrusted for every wallet, whatever the gates say:

- `ordertape:incomplete`: the last order tape call left transactions failed, missing, undecodable
  or not attempted (for example it hit `--max-requests`). Chain fills are then missing.
- `<step>:stale`: a kept output was made from other inputs or code than the current ones, or
  chunk outputs exist without their merge.
- `<step>:mem_budget_stop`: the run stopped on `--mem-budget-mb`.

The run does not stop on a gate. It finishes and writes the reasons under `untrusted` in the
manifest. In the report a cell reads `UNTRUSTED` for any of the above, and `NO GATE` when a gate
could not be evaluated, so nothing vouches for the number. A wallet with few orders in the range,
or a pure taker, is `NO GATE` on everything after the order tape; its ledger already holds the
match-dependent obligations at UNKNOWN. Cohort totals in the report are over all wallets; each
line says how many of them are not trusted for the step it rests on.

## Running it

```sh
doppel-reconstruct --data TAPE --lab LAB --cohort COHORT.json \
    --from 2026-11-26T12:00 --to 2026-11-26T18:00 --max-txs-per-wallet 500 \
    --chunk-minutes 15 --mem-budget-mb 1800 --max-requests 60000
```

| option | meaning |
|---|---|
| `--store` | fill store, default `LAB/fills`. It must already be compacted (`doppel-census` and `doppel-dossier` do that); this command does not. |
| `--wallets` | file or comma list; default is every candidate and control of the cohort |
| `--max-txs-per-wallet N` | order tape: each wallet's most recent N transactions in the range |
| `--steps a,b` | subset of `ordertape,ordermatch,orderreplay,profile` |
| `--force STEP` | rerun that step (repeatable) |
| `--chunk-hours H`, `--chunk-minutes M` | chunk length for book match and twin replay, default 1 hour |
| `--wallet-batch N` | wallets per book match / twin replay call inside a chunk, default 10 |
| `--mem-budget-mb B` | stop cleanly when the process's peak memory, checked after every unit, is above B |
| `--size-tol-rel X` | book match relative size tolerance, default 0 (exact to 0.02 shares). E39's numbers used 0.02, at which sizes 97..100 match each other. Recorded in the manifest. |
| `--out` | run directory, default `LAB/reconstruct/<run-id>`; the id is built from cohort, range and options, so the same command resumes |
| `--orders DIR` | use an existing order tape instead of `<out>/ordertape` (saves refetching) |
| `--dossiers DIR` | dossiers for the ledger, default `LAB/dossiers` when present |
| `--rpc`, `--rate`, `--max-requests` | passed to the order tape's RPC pool |
| `--last-hours N` | timer use, instead of `--from/--to`: the last N closed hours, after any earlier range that did not finish |

Exit code: 0 done; 1 order tape incomplete (run again); 2 stopped on the memory budget; 3 the run
directory or the order tape is locked by another run.

**RPC cost.** The order tape costs one RPC request per transaction on free public endpoints. A
12-wallet run on Apex on 2026-11-26 measured 9.91 transactions per second (lead's run; not yet a
FINDINGS row). The configured ceiling is 10 (two endpoints at 5 requests per second); do not plan
on more than the measured figure, and less when another order tape job shares the endpoints.
Size `--max-txs-per-wallet` and `--max-requests` to that. If the order tape stops early, every
wallet is marked `ordertape:incomplete` and the report says so at the top; run the same command
again and it continues.

**Memory.** The host has 8 GB and runs the recorders. On 2026-11-26 one unchunked book match of
12 wallets over one hour used 5.8 GB there (lead's run; not yet a FINDINGS row). Two things were
large: every book event of the wallets' markets for the hour, and one row per fingerprint
candidate (on the order of 100,000 per hour for a quote-flickering wallet, E39). What the
pipeline does about each:

- Candidate rows are never held. Book match writes each to `quotes.jsonl` as it is found and keeps
  counts and a histogram. Twin replay reads that file line by line, keeps one small tuple per
  candidate until its placement, and reduces each finished candidate to counts. In the synthetic
  test, ten times the candidates cost about 0.7 kB of peak memory per extra candidate (its two
  book events); the old twin replay alone cost about 6.5 kB.
- Book events are bounded by the chunk length (`--chunk-minutes`) and by `--wallet-batch`: events
  are kept only for the markets of the wallets in the batch. Wallets that trade the same markets
  share those events, so the chunk length is the stronger control.
- `--mem-budget-mb` is the backstop. The peak memory of the process is written into the manifest
  after every unit (`peak_rss_mb`; a high-water mark, so a unit raised it only where the number
  goes up). Above the budget the run stops: finished units stay, nothing else runs, the report
  says RUN STOPPED. Run again with a shorter chunk or a smaller batch.

No real-tape memory figure exists for the chunked pipeline yet; the first Apex run should record
`peak_rss_mb` per unit in a FINDINGS row.

**Resume.** Each unit of work (the order tape call, each chunk x wallet batch of book match and
twin replay, each merge, the ledgers) stores a key: a sha256 over its code files (the pipeline's
own file included), the constants and options that change outputs, its arguments and its input
hashes. A unit is skipped when its key is unchanged and its outputs are still there. A second
identical run does no work. Outputs are written under a temporary name and renamed.

The run verifies output SHA-256 when resuming. It reads committed order parts and checks each
available CLOB segment against its seal. A missing segment marks dependent wallet results
untrusted. A transaction cap that omits known wallet fills fails the full-store-coverage gate.
This compares with the compacted fill store. Independent chain coverage is a separate measurement.
While a run is in progress, its top-level manifest and reports identify the requested range
and the pending status. A crash preserves that status and completed chunk checkpoints.

**Locks.** A run holds an exclusive lock on its run directory and on the order
tape directory. A second run on either exits with code 3. `doppel-ordertape` itself takes no lock:
do not run it on a directory this pipeline is using.

**Chunks and merging.** Chunks do not overlap, so nothing is counted twice. Per-wallet results are
merged as **sums of counts, never averages of rates**: match rate is total matched over total
observable; the chance rate is total control increases over total control token-hours; quantiles
are recomputed from the pooled rows; the twin summary and its CI are recomputed by the tool's own
code on the wallet's rows from all chunks. The cohort twin ratio and its CI use the same rows.
Fingerprint sizes are derived per chunk; `sizes_stable: false` means the summed counts mix
different size sets.

What a chunk edge costs (the manifest shows the counts per unit):

- The book is unknown at the start of a chunk until each asset's next snapshot. Nothing is warmed
  up from the previous chunk, so short chunks pay this more often.
- An order still resting at the chunk's end is `unknown:censored` in the book match.
- An order created in the last ~62.5 s of a chunk is not `observable`.
- An order whose fills are not all located by a print inside its chunk, or whose end or any fill
  is at or after the chunk end, cannot be replayed on a tape that stops at the edge. Its twin row
  is marked `cut: "chunk_end"` and kept out of the headline ratio. Without that it reads as a twin
  miss: in the test, five such orders out of 37 moved the ratio from 1.0 to 0.86 and failed the
  bracket gate.
- A quiet stretch across the edge is not counted in token-hours.
- A cancel is claimed by one candidate within a wallet batch; two wallets in different batches
  can both claim the same one. Within a batch, wallets are served in address order.

**Timer.** With `--last-hours N` the command keeps `LAB/reconstruct/_pending.json`: the ranges
that have not finished. Each firing adds the newest range, then runs ranges oldest first until
one does not finish or the firing's `--max-requests` is spent. A range that fails three firings is
moved to `gave_up` in that file. At most eight ranges wait.

**Disk.** Each unit keeps `quotes.jsonl` (every fingerprint candidate).

### Output

```
<out>/manifest.json            what ran, on what, gates, trust, peak memory
<out>/report.md, report.json   the cohort report
<out>/ordertape/               day=YYYY-MM-DD/part-*.parquet (verified column per row)
<out>/chunks/<start>/<batch>/ordermatch/   ordermatch.json, orders.jsonl, quotes.jsonl
<out>/chunks/<start>/<batch>/orderreplay/  orderreplay.json, orderreplay.jsonl (order rows, with maker)
<out>/ordermatch/ordermatch.json   merged, per wallet
<out>/orderreplay/orderreplay.json merged, per wallet and cohort
<out>/wallets/<wallet>/        orders.jsonl (book match rows), orderreplay.jsonl (twin rows)
<out>/profile/                 <wallet>.ledger.json, <wallet>.ledger.md, cohort.json, cohort.md
```

`manifest.json` holds: run id, git commit (`DEPLOYED_COMMIT` on a deployed tree, else
`git rev-parse`), sha256 of each code file used, the cohort file's sha256, the range, the options,
and per step its inputs and outputs with sha256 and row counts, wall time and peak memory, and per
wallet every gate with its value, n, threshold and status, its order tape coverage, plus the
sha256 of the wallet's own files.

## Reading a wallet card

Start in `report.md`, one row per wallet. Read left to right, and stop at the first `UNTRUSTED`
or `NO GATE`.

1. **verified orders, verified share, tape coverage.** How many filled orders the rest is built
   on. A share below 1 means the decoder and the fill store disagree, or the tape was not verified
   for this wallet. Coverage is the wallet's fills in decoded transactions over its fills in the
   range; below 1 (a cap, or an incomplete tape) its chain fills are a lower bound.
2. **matched / observable, placebo rate.** If the match rate is not far above the placebo rate,
   nothing to the right means anything: the stamps are not validated.
3. **rest ms p50 (filled / cancelled).** How long its filled orders rested. These are orders that
   traded; only filled orders are on chain, so this bounds cadence from above (E36).
4. **fill ratio (chance share).** An estimate. The chance share is the part of the candidate
   quotes expected with no involvement of the wallet. Check `sizes_stable` and remember that
   identity precision is not measured in a cohort run.
5. **twin pessimistic / optimistic with CI.** Conditional on a real fill. The real fills should
   sit between the two; check the bracket and adapter gates in the manifest first.
6. **coverage, copyability.** From the ledger. Open `profile/<wallet>.ledger.md` for the eleven
   obligations; each UNKNOWN row says what data would resolve it.
7. **clusters.** Wallets sharing a software or operator key. Evidence of shared software, never
   proof; a cluster on a stock client's salt pattern carries little information.

## Rulefit and Fidelity handoff

Pass the training run's `manifest.json` to `doppel-rulefit --reconstruct-manifest` with the
wallet, tape and training interval. It checks the order tape and placement results against the
manifest and writes a fit report. It emits a draft genome when a pricing rule can be fitted and
records the full fit interval in `evidence.fit_range`.

Use `doppel-fidelity --reconstruct-manifest` with a separate reconstruction covering the exact
holdout and the frozen draft genome. The holdout must begin at or after the fit interval ends.
Fidelity compares simulated fills against the verified order tape and removes the wallet's
identified resting orders using the placement rows. A verdict requires an independent
`--coverage-receipt` confirming the wallet, the exact UTC interval and the order-tape checksum.
Otherwise the result is inconclusive. Orders that never filled remain anonymous.
