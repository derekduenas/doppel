# Layer 2 · Wallet inspection

The walkthrough follows candidate `0xf9b7876ba2c35370e0bedb7f8ee772da722aa37c`
and control `0x28cc505fb60501dbb2199a9662ba175919af904f` from the reconciled ledger
to the international BTC five-minute book. An execution can be located even when the
wallet's signed creation field is later than the execution. Public depth can bound the
time a quote was continuously displayed. Identifying its owner requires further evidence.
[Registered protocol](../evidence/2026-11-27-wallet-inspection/PROTOCOL.md) · [Layer 1 ledgers](WALLET-ABSORPTION.md)

## The first trade

The candidate bought 60 shares at 7¢, spending $4.20. The public sell print at 7¢
totalled 934.2 shares across the transaction's maker legs. Its venue time was
1795629743926 ms and its recorder receipt was 1795629744042136014 ns. The signed
timestamp falls 896 seconds after the Polygon block. Fitting a constant offset to that
clock would use the answer this experiment is meant to test.
[Signed fill](../evidence/2026-11-27-wallet-absorption/first-trade.json)

The actual ten-second window had continuous depth on both clocks. Two 60-share bid
increases at 7¢ appeared 1,031 ms and 1,011 ms before the print, at record sequences
39659 and 39674. Either could belong to another account. The level was empty immediately
before an earlier native update, sequence 39305. That bounds this order's **continuous
displayed residence at 1,329 ms**. Its exact placement, total lifetime and any cancel
instruction remain unknown. Three genuine depth fragments intersected the shifted
window. This trade is excluded from the paired comparison. Two independent readers
agreed on the print, both competing additions, the empty level and the bound.
[Raw-tape cross-check](../evidence/2026-11-27-wallet-inspection/first-trade.json)

## Short-window results

The first interval was Nov 25 18:00–18:15 UTC. Both wallets traded three conditions.

| Fill-anchored measure | Candidate | Control |
|---|---:|---:|
| Reconciled fills → unique signed orders | 30 → 17 | 35 → 18 |
| First executions located | 17 | 16 |
| Continuous ten-second actual windows | 11 | 4 |
| Orders eligible in both arms / distinct conditions | 6 / 2 | 2 / 1 |
| Any compatible addition, actual / shifted | 6 / 3 | 2 / 0 |
| Sole uncontested addition, actual / shifted | 0 / 1 | 2 / 0 |
| Orders with an upper bound on continuous displayed residence | 11 | 2 |

The short sample provides no unique actual addition for the candidate among the paired
orders. For the control, one of the two sole actual additions occurred 136 ms before
the fill. A later observation put the continuous displayed upper bound at 65 ms.
That rules out the addition as the beginning of an uninterrupted quote through execution.
The other control addition remained possible with unknown owner. This check follows
the registered size comparison and keeps its counts visible. [Short receipt](../evidence/2026-11-27-wallet-inspection/first15-summary.json)

**Separate signing-time comparator.** The candidate had zero eligible orders. All 17
signed stamps fell after both the first block second and the end of the short interval.
The control had 17 eligible signed orders and 10 observable in both comparisons. Six
qualified against the actual signing window and zero against the +60-second placebo.
Across all 17, seven offered a unique hypothesis, five lacked coverage, two were ambiguous,
two hit an empty level before the fill and one had no candidate. No exact lifetime was
established. The comparator used a different anchor, searched −300 to +2,500 ms, and
therefore has a different denominator from the fill-anchored study above. Its provisional
wallet clock gate needs at least 30 comparable orders, 90% actual and at most 10% placebo.
[Native matcher receipt](../evidence/2026-11-27-wallet-inspection/standard-first15.json)

## How the comparison works

- Select by the verified fill interval and group on account and signed order hash.
  Check the report, fill store, and committed order-tape files and flush manifests
  against the previously published Layer 1 hashes. Check source rows, roles, quantities,
  transactions, blocks and the chain-reconciled report before reading the book.
- Bind the first observed maker fill to the complete decoded transaction. Direct and
  complementary print representations can describe one execution. The join checks
  market, asset, aggressor side, price, quantity and the 120-second block-time bound.
  Contradictory or multiply timed reports have their own unresolved status.
- Test a ten-second window before execution and an equal window 60 seconds earlier.
  Require continuous state across each individual window on both exchange and recorder
  clocks. A reset between the two windows is allowed. Only native increases at the
  signed limit, side and full size within 0.02 shares qualify. Count all compatible
  increases before testing whether one event competes for more than one order.
- Read every present file through the end with strict frame/CRC checks and compare it
  with its published SHA-256. A missing file, relevant recorder discontinuity or reversed
  depth clock splits coverage. Depth resumes at a snapshot. A last-trade message dates
  an execution and cannot advance the depth clock. Requiring that distinction fixed
  a real reader defect. In the short window, 1,959 of 2,027 reversals under the
  superseded rule involved a trade message. Comparing only depth records left 104
  genuine reversals over the eight registered tokens.
- The paired denominator contains orders with both arms observable. A sole candidate
  in the headline comparison must be uncontested among those paired orders. The evidence
  verifier also counts sole candidates after including competitors observed in a single
  arm. Both tests are limited to observed orders. The market can contain other traders
  whose orders or cancellations never appear with an identity.

## Corrections to the production research tools

The normal matcher now distinguishes one compatible native addition from a contested
or ambiguous one. It verifies the available execution evidence, rejects contradictory
prints and reversals, and censors intervals that cross lost state. A full signed fill
can establish that the signed amount was executed. Anonymous depth reductions supply
no owner-linked cancellation. The replay and fidelity book adjustment consume qualified
placement hypotheses. Older summary formats and stale cached reconstruction outputs
must be regenerated before a trusted handoff.

Rule fitting can retain its diagnostics when evidence is thin. Publishing a maker
draft requires the existing wallet-level clock and placebo gate plus its side, price and
participation evidence. Losing coverage or token mapping clears a prior draft from
that output directory. The profile checks unrounded counts for the clock decision.
This keeps a plausible-looking model from promoting a wallet with unsupported timing.

## Reproduce on Apex

The retained research directory is
`/var/backups/doppel-sealed-20261126/research/wallet-inspection.jOPOHm`.
The working copies and computed per-order results are on the root disk. The sealed
archive is on Apex. The registration was mirrored at `33a967c` before placement results
were read. The paired studies ran from `016d7ceb2e0d3e4fa7961567de42a72ca6edf586`.
They used a 1,100 MiB memory cap, no swap, nice 19, and idle I/O. The short run took
106 seconds and used 165,600 KiB peak RSS. All inputs come from the retained archive.

Set `src` to `$PWD/rev2000dac` for the final residence labels in the research directory, `arc` to
`/var/backups/doppel-sealed-20261126`, and `ledger` to
`$arc/research/wallet-absorption.crsLww`. Use the full registered addresses above for
`candidate` and `control`. Use fresh output names when repeating a run.

```sh
export PYTHONPATH="$src/src" OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
summary="$src/evidence/2026-11-27-wallet-absorption/run-summary.json"
/opt/doppel/.venv/bin/python -m doppel.lab.inspect_wallet --data "$arc/tape" \
  --layer1-summary "$summary" --out repeat-first15 \
  --subject "$ledger/signed-fifteen/wallet-$candidate.json" "$ledger/reviewed/first-source/fills" "$ledger/first-orders" \
  --subject "$ledger/signed-fifteen/wallet-$control.json" "$ledger/reviewed/first-source/fills" "$ledger/first-orders"
/opt/doppel/.venv/bin/python -m doppel.lab.inspect_wallet --data "$arc/tape" \
  --layer1-summary "$summary" --out repeat-day \
  --subject "$ledger/signed-day-0xf9b7/wallet-$candidate.json" "$ledger/day-store-0xf9b7" "$ledger/day-orders-0xf9b7" \
  --subject "$ledger/signed-day-0x28cc/wallet-$control.json" "$ledger/day-store-0x28cc" "$ledger/day-orders-0x28cc"
```

The original comparisons live in `paired-first15` and `paired-day`. The final check at
`2000dac8b263f13ccafd801481ca8e160a1713d7` applied the witnessed empty level to
each actual sole candidate. It validated the original row hashes and reproduced every
registered count before publishing `qualified-first15` and `qualified-day`. A direct
run from the raw short tape also reproduces the qualified per-order output. Recompute the compact
cross-check from the research directory with the script and the independent first case
from this review branch. It validates the SHA-256 of the per-order results against each
summary, the signed manifests against Layer 1, and the first-order result against the
independent raw-tape walk. [Evidence verifier](../evidence/2026-11-27-wallet-inspection/summarize.py)

```sh
/opt/doppel/.venv/bin/python "$src/evidence/2026-11-27-wallet-inspection/summarize.py" --study "$PWD" \
  --layer1-summary "$summary" --first-case independent-first.json
```

The first 15-minute output at `first15/` came from the superseded print-driven clock
rule and is retained as a sensitivity check. Its day scan was stopped after the
corrected scan began to release CPU. A still earlier attempt failed before producing
results because the compact store supplies the wallet as a grouping key. A regression
test now uses the real storage layout. The separate six-hour answer-key baseline was
left running at the owner's request.

## Where Layer 3 starts

Carry forward the signed order economics, independent execution times on both clocks,
and reconciled inventory events. These support a study of the state visible **before
execution**. A study of posting decisions also needs an owner-linked placement, an
independently validated placement method, or analyses over the whole admissible
decision interval that give the same result. Keep retrospective receipts outside
decision features. A wallet with inadequate evidence stays unclassified.

1. At the first 60-share fill, reconstruct the state at both compatible posting times
   and at the pre-execution receive cutoff. Label the first two as hypotheses. Verify
   that perturbing later book updates, disclosures and fills cannot change any earlier
   feature. Never put the aggregate print itself into the decision row.
2. Audit `rulefit.scan` for stale state through a missing hour, a disconnected socket
   and an unannounced sequence break. A silent interval needs an observability mask.
   Replace whole-study anonymous signing-delay calibration in `decision_table` with
   past-only calibration before using its inferred cutoffs.
3. Build the eligible market list from time-stamped availability, including markets the
   wallet never traded. Distinguish an observed fill, a covered market with no observed
   fill, and an unobservable market. Audit `token_map`, `grid_cells` and
   `fit_participation` before interpreting any absence as a choice.
4. Check first-availability time of opening reference prices in `RefStore.load_boundaries`.
   Record uncertainty where fill inventory or taker features use block-time fallbacks.
   Reserve separate markets/time for evaluating rival explanations.

The prospective selection study closes its first week Dec 4 at 03:30 UTC and its fourth
week Dec 25 at 03:30 UTC. These November replays provide historical evidence about the
measurement system. Strategy, persistence and a real-wallet twin remain open.
