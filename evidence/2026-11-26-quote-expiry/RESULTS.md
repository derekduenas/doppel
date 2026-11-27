# Quote expiry under out-of-order venue timestamps

The candidate recovered both missed fills in the discovery interval. The reserved interval
contained zero BTC 5m maker orders from the wallet, so it supplied no independent test of
delivery. Keep the reconstruction timer off.

| Measure | Discovery control | Discovery candidate | Reserved interval, each arm |
| --- | ---: | ---: | ---: |
| Matched orders / known end | 16 / 14 | 16 / 14 | 0 / 0 |
| Own fills seen and routed to maker price | 32 / 32 | 32 / 32 | 0 / 0 |
| Own routed fills delivered | 30 / 32 | 32 / 32 | 0 / 0 |
| Pessimistic overfills / optimistic underfills, known-end orders | 0 / 2 | 0 / 0 | No eligible orders |
| Known-end orders with complete price allocation | 2 / 14 | 3 / 14 | No eligible orders |

All six screened orders in the reserved interval had identifiable market slugs. None qualified.
The protocol ended at the selected boundary. Delivery, allocation and bracket rates on the
holdout are undefined. Both recorders were active after all four archive-only replays, which
exited successfully and used zero RPC requests. At the end of the trial Apex reported release
`44aa7e2`, both recorder services active, and `doppel-reconstruct.timer` absent and inactive.
Tape retention is Apex-only by owner decision.

Of the 32 discovery fills, the candidate delivered 30 before observing expiry and two during
the grace period. Orders `df7fe3700…` (97 shares) and `f6f81419d…` (98 shares) recovered in
both queue modes. Their prints arrived 205,930 ns and 104,113 ns respectively after the first
message stamped beyond their end. For `12ae04d6c…`, the optimistic fill of 99 shares was
dated two venue milliseconds earlier. Those were the only three rows whose pre-existing fields
changed. One pessimistic fill from a crossing book was recorded in each arm. Eleven known-end
orders had one feasible placement and three had multiple candidates. The archive independently
identifies zero placements or expiries. The earlier 13-minute test still has four pessimistic
overfills in 65 known-end orders and an elapsed time longer than its input interval.

For the next independent trial, seek an existing order-owner export with venue-acknowledged
placement, cancellation or expiry, and fill identifiers that can be joined to public tape.
Use a separately registered collection period and minimum qualifying sample before evaluating
new outcomes. The present trial is closed. Preserve the price-allocation requirement and the
[earlier full-run thresholds and results](../../docs/SYSTEM-AUDIT-20261126.md).

## What was frozen

Commit `b787e7a` froze the [rule, selection and thresholds](protocol.json) before the candidate
was implemented or its results examined. Both arms used the same matched rows, sealed BTC 5m
stream and decoded order parts on Apex. The comparison used `45ccb92` as the control and
`2688f00` as the candidate. The discovery interval was `[1795630500, 1795630620)`. The holdout
was the next 120 seconds, `[1795630620, 1795630740)`. Its individual results were reserved for
this comparison. Earlier work had examined the wallet-hour in aggregate.

The candidate processes messages in capture order, including messages inside the same received
array. A book or print bearing a venue time beyond a quote's inferred end freezes its book.
For one second of receipt time, the twin can still consume public prints stamped inside the
original lifetime against the queue left at expiry. This duration comes from the existing twin
constant `DYING_GRACE_NS`. A print in the placement record, a reversed receipt across expiry,
a reset, or the sample boundary cannot provide a late fill. The rule applies to both token views
and to unrelated transactions. The report records both clocks for each own fill delivered late.

An order which fully trades can have its end inferred from its own last fill. Recovering that
print verifies the replay's handling of the two clocks. It takes independent order acknowledgements
to establish the wallet's actual queue entry and lifetime. The recorded public market channel
provides level sizes and executions. Its updates contain no wallet-specific placement ID.

## Build verification

All 467 Python tests passed in 303.12s with local sockets available to the mock servers.
An earlier sandboxed run blocked eight socket-based tests. All 35 focused replay tests
also passed after the final annotation change. Running these tests against the control yielded 16 passes,
16 behavioral failures and three failures caused by diagnostic fields introduced by the candidate.
The checks cover batched messages, unchanged depth, mirrored prints, equal venue milliseconds,
receipt-clock reversal, the exact grace boundary, resets, and sample-end truncation.
The final type check reported zero errors and warnings. Both registry checks passed with 21 genome
records, head `7e4843a45ad1dc25`, and expression semantics `49083cf45e14` at probe v3.
The offline dependency check and the source-distribution and wheel builds passed. All 64 Python
modules matched the checkout byte-for-byte and all 30 commands imported from the wheel. Its
SHA-256 was `893dc02f38288193e587ab4eb3b7c4b5d2126b6838412649cd25ca73ca20ca4e`.

The Mac had run low on disk space. Cleaning the regenerable Cargo dev cache in
`/Users/derekduenas/doppel/target` reclaimed 692.2 MiB. This pass changed Python replay code,
tests and documentation. The preceding audit records the 72 passing Rust tests and Clippy result.

## Reproduction

The [paired receipt](paired-results.json) records source revisions, times, peak memory, hashes,
selection counts, per-order changes and Apex paths. Generate it with the archived scratch
directory and an output file of your choice. Run from the review checkout.

```sh
base=/var/backups/doppel-sealed-20261126
trial="$base/research/ordertape-pilot.TUedd4/expiry-trial.g88jw6"
PYTHONPATH="$trial/candidate/src" /opt/doppel/.venv/bin/python \
  evidence/2026-11-26-quote-expiry/paired_receipt.py "$trial" "$base" \
  evidence/2026-11-26-quote-expiry/protocol.json 2688f00a6ff79dec880300d46d804af678f275a5
```

The archived copy of the committed writer lives at `$trial/receipt-tools/paired_receipt.py`.
The scratch root also holds an earlier draft writer. Using the committed writer reproduced
the recorded receipt byte-for-byte, SHA-256 `0c4d0d9af23d42ea40be0acbd502800c9b529718e6aa2ffe226f2001921a76b0`.
The discovery control matches the original order rows byte-for-byte. Matched input hashes
agree between arms for both intervals. Candidate replay took 39.40s on discovery and 28.70s
on the holdout. The only source change after candidate freeze was a return annotation and its
import in `intlbook.py` (`eabbab9`), correcting a Pyright error.
The final `orderreplay.py` and every function body in `intlbook.py` match the trial revision.
