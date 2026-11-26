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
by chance. On one wallet-hour 98% of observable orders matched against a 4.6% placebo (E39; 93%
in the first probe, E37). A high match rate with a low placebo rate is also the evidence that the
wallet's `created_ms` stamps are real, to about 0.1 s (E37).

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

No orderreplay number has been measured on real tape and written to FINDINGS yet. The definitions
are in the `orderreplay.py` docstring and its tests.

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
| Identified, given the gate | `created_ms`, sign-to-book latency, placement in the book | Match rate against placebo (E37, E39) |
| Identified when classified | End of a matched order: `filled`, or `cancelled` by an exact decrease | Follow-forward rules; `unknown` otherwise (E39) |
| Estimated | Quotes that never filled, fill ratio, re-post rate | Fingerprint sizes against a control rate; precision at most 0.87 on E39 |
| Estimated | Copyability verdict for makers | Provisional thresholds from one wallet-hour (E39) |
| Invisible | Any single order that never filled | It is not on chain (E34) and the book is anonymous |
| Invisible | Whether a remainder was cancelled or replaced | Both are one decrease in the book |
| Self-reported | `created_ms` | Written by the signer. 113 of 6,492 stamps in E36 were after their block; they are flagged and screened |
| Lower bound | An order's chain fills | Only fetched transactions are known (`--max-txs-per-wallet`) |

One limit comes from the pipeline itself. A cohort run verifies only the cohort's own fills, so
other makers in the same transactions are unverified and are left out of the book match. With
this input `other_known` counts only other cohort wallets, so `identity_precision_upper` is
**not measured** (the report prints null). The 0.87 of E39 came from a JSONL sample that kept
every maker. A second limit: a wallet needs at least 30 maker orders inside one chunk to get a size
fingerprint there; below that its footprint is absent, not zero.

## Gates

One table, in `reconstruct.py` (`GATES`), copied into every manifest. **All thresholds are
provisional.**

| gate | passes when | needs at least | rests on |
|---|---|---|---|
| `ordertape.verified_share` | >= 0.99 of the store's fills in fetched txs have a decoded order with that id | 1 fill | E36: 1,534 of 1,534 |
| `ordermatch.match_rate` | >= 0.90 | 30 observable orders | E37 0.93, E39 0.98; same numbers as `profile.T` |
| `ordermatch.placebo_rate` | <= 0.10 | 30 observable orders | E37 / E39 0.046 |
| `orderreplay.routed_share` | >= 0.95 | 30 prints seen | no FINDINGS row yet |
| `orderreplay.delivered_share` | >= 0.95 | 30 prints routed | no FINDINGS row yet |
| `orderreplay.placement_not_found_share` | <= 0.01 of orders to replay | 30 orders | no FINDINGS row yet; 0 expected |
| `orderreplay.bracket_violation_share` | <= 0.05 of headline orders | 30 orders | no FINDINGS row yet; 0 under FIFO |

Each gate is evaluated per wallet and is `pass`, `fail` or `not_evaluable` (too few cases, or the
step did not run). A wallet's output of a step is trusted only when every gate of that step and of
the steps it reads from is `pass`:

- order tape gate -> book match, twin replay, ledger
- book match gates -> twin replay, ledger
- twin replay gates -> the twin ratios

The run does not stop on a gate. It finishes and writes the gates that did not pass under
`untrusted` in the manifest. In the report a cell reads `UNTRUSTED` when a gate behind it failed
(or a step's kept output is stale: made from other inputs or code than the current ones), and
`NO GATE` when a gate could not be evaluated, so nothing vouches for the number. A wallet with
few orders in the range, or a pure taker, is `NO GATE` on everything after the order tape; its
ledger already holds the match-dependent obligations at UNKNOWN.

## Running it

```sh
doppel-reconstruct --data TAPE --lab LAB --cohort COHORT.json \
    --from 2026-11-26T12:00 --to 2026-11-26T18:00 --max-txs-per-wallet 500
```

| option | meaning |
|---|---|
| `--store` | fill store, default `LAB/fills`. It must already be compacted (`doppel-census` and `doppel-dossier` do that); this command does not. |
| `--wallets` | file or comma list; default is every candidate and control of the cohort |
| `--max-txs-per-wallet N` | order tape: each wallet's most recent N transactions in the range |
| `--steps a,b` | subset of `ordertape,ordermatch,orderreplay,profile` |
| `--force STEP` | rerun that step (repeatable) |
| `--chunk-hours H` | chunk length for book match and twin replay, default 1 |
| `--out` | run directory, default `LAB/reconstruct/<run-id>`; the id is built from cohort, range and options, so the same command resumes |
| `--orders DIR` | use an existing order tape instead of `<out>/ordertape` (saves refetching) |
| `--dossiers DIR` | dossiers for the ledger, default `LAB/dossiers` when present |
| `--rpc`, `--rate`, `--max-requests` | passed to the order tape's RPC pool |

**Cost.** The order tape costs one RPC request per transaction on free public endpoints, about 10
transactions per second in total. Size `--max-txs-per-wallet` to that. If the order tape stops
early (budget, endpoints down), the command says so; run it again and it continues.

**Resume.** Each unit of work (the order tape call, each chunk of book match and twin replay, each
merge, the ledgers) stores a key: a sha256 over its code files, arguments and input hashes. A unit
is skipped when its key is unchanged and its outputs are still there. A second identical run does
no work.

**Chunks.** Book match and twin replay hold an hour of book events in memory, so the range is cut
at whole hours and each chunk runs alone. Chunks do not overlap, so nothing is counted twice.
Per-wallet results are merged as **sums of counts, never averages of rates**: match rate is total
matched over total observable; the chance rate is total control increases over total control
token-hours; quantiles are recomputed from the pooled rows; the twin summary and its CI are
recomputed by the tool's own code on the wallet's rows from all chunks. What chunking costs, and
the manifest shows per chunk: the book is unknown at the start of a chunk until each asset's next
snapshot; an order still resting at the chunk's end is `unknown:censored`; an order created in the
last ~62.5 s is not `observable`; a quiet stretch across the edge is not counted in token-hours.
Fingerprint sizes are derived per chunk; `sizes_stable: false` means the summed counts mix
different size sets.

**Disk.** Each chunk keeps `quotes.jsonl` (every fingerprint candidate). For a quote-flickering
wallet that is on the order of 100,000 rows per hour (E39).

### Output

```
<out>/manifest.json            what ran, on what, gates, trust
<out>/report.md, report.json   the cohort report
<out>/ordertape/               day=YYYY-MM-DD/part-*.parquet (verified column per row)
<out>/chunks/<start>/ordermatch/   ordermatch.json, orders.jsonl, quotes.jsonl
<out>/chunks/<start>/orderreplay/  orderreplay.json, orderreplay.jsonl (order rows, with maker)
<out>/ordermatch/ordermatch.json   merged, per wallet
<out>/orderreplay/orderreplay.json merged, per wallet and cohort
<out>/wallets/<wallet>/        orders.jsonl (book match rows), orderreplay.jsonl (twin rows)
<out>/profile/                 <wallet>.ledger.json, <wallet>.ledger.md, cohort.json, cohort.md
```

`manifest.json` holds: run id, git commit (`DEPLOYED_COMMIT` on a deployed tree, else
`git rev-parse`), sha256 of each code file used, the cohort file's sha256, the range, and per step
its inputs and outputs with sha256 and row counts, wall time, and per wallet every gate with its
value, n, threshold and status, plus the sha256 of the wallet's own files.

## Reading a wallet card

Start in `report.md`, one row per wallet. Read left to right, and stop at the first `UNTRUSTED`
or `NO GATE`.

1. **verified orders, verified share.** How many filled orders the rest is built on. A share
   below 1 means the decoder and the fill store disagree.
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

## Roadmap hooks (stubs)

Other workers are writing these files. Nothing below is implemented by this pipeline.

- **`fidelity.py` (fidelity test).** Takes the per-wallet twin rows
  (`wallets/<wallet>/orderreplay.jsonl`) and decides whether the twin's fill model is close enough
  to the real fills to be used for that wallet. Input contract: the row fields written by
  `orderreplay.result`, plus `maker`. Stub: not written here.
- **`rulefit.py` (rule fitting).** Fits the UNKNOWN and ESTIMATED obligations (price rule, size
  rule when variable, cancel rule) from the matched orders (`wallets/<wallet>/orders.jsonl`) joined
  to book state at placement. Stub: not written here.
- **Simulation-based inference.** For what stays invisible (never-filled quotes), fit a quoting
  rule by simulating it through the intl twin and comparing simulated footprints with the observed
  ones (filled orders, fingerprint counts, lives). It needs `rulefit.py` for the candidate rule
  and an intl twin to simulate in. Stub: not designed here.
