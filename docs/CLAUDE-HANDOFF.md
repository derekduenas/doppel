# Claude Code handoff

## Latest wallet walkthrough · Layer 3

Open `/Users/derekduenas/doppel-wallet-dissection-20261127` on `review/wallet-dissection-20261127`.
The [Layer 3 evidence checkpoint](https://github.com/derekduenas/doppel/blob/a0c96476f8d85d050653625fb6ac671a2c1e1ea0/docs/WALLET-DISSECTION.md)
covers the two registered wallets, both anonymous posting hypotheses, the independently
observed no-fill market, and the two real-wallet abstentions. The branch tip carries
verification and reproducible Apex receipts. Start Layer 4 by registering rival rules
and acquiring a separately labelled holdout. Production tape and archive capacity on
Apex are below their reserves.

## Latest wallet walkthrough · Layer 2

Open `/Users/derekduenas/doppel-wallet-inspection-20261127` on
`review/wallet-inspection-20261127` at `5ef9086`. The real candidate produced 59 sole
uncontested book additions versus 60 in the shifted comparison across 1,650 day orders.
The control produced 241 versus 25 across 537. Neither short-window run qualified for a
strategy draft. Layer 3 starts with strictly prior information at the first 60-share fill
and two competing posting-time hypotheses. Check Apex storage headroom before capturing
new data. [Layer 2 report and reproducible receipts](https://github.com/derekduenas/doppel/blob/5ef90866a4e4d6231a46a01e065645693f92e396/docs/WALLET-INSPECTION.md)

## Wallet-by-wallet audit · Layer 1

Resume on `review/wallet-absorption-20261127` in
`/Users/derekduenas/doppel-wallet-absorption-20261127`. Follow the [Layer 1 audit](WALLET-ABSORPTION.md)
for the two real ledgers, the uncovered interval, the scratch paths on Apex and instructions
for Layer 2. Begin inspection with the candidate's first witnessed 60-share order. Its signed
timestamp falls 896 seconds after the block. Book evidence is necessary to date placement.
The prospective Layer 0 selection clock continues in parallel.

## Wallet-by-wallet audit · Layer 0

Resume on `review/wallet-selection-20261127` in
`/Users/derekduenas/doppel-wallet-selection-20261127`. The [wallet selection audit](WALLET-SELECTION.md)
records the intake fixes, decision rules, status of both real wallets, and the fixed weekly
collection boundaries. The primary wallet ranks 52 in cohort v1 and has one qualifying
control. The earlier rank-2 pilot has no control within the registered activity bound.
The [registration receipt](../evidence/2026-11-27-wallet-selection/registration.json) was
mirrored before the study opened. The first evidence checkpoint is December 4 at 03:30 UTC.

## November 27 rule-twin pickup

Open `/Users/derekduenas/doppel-rule-twin-readiness` on
`review/rule-twin-readiness-20261127`. The [rule-twin review](RULE-TWIN-REVIEW-20261127.md)
records the interrupted builder recovery, the regraded development runs, the six-hour Apex
input check, wallet pagination and the remaining gates. Start with Apex storage headroom and
the [reserved three-seed exam](../evidence/2026-11-27-answerkey-inputs/EXAM.md).
The owner chose to keep the original baseline running and the tape archive on Apex.

## November 26 integration pickup

Open `/Users/derekduenas/doppel-integration`. The integration worktree now uses the review branch
`review/system-audit-20261126`, based on `3cff434`. The first audit status baseline is
`3c24fbb`. The original checkout is `/Users/derekduenas/doppel`; its working
tree and the three published pods are separate from this integration.

For the whole build, start with the [milestone board](BUILD-STATUS-20261126.md) and
[deep audit and release handoff](DEEP-AUDIT-20261126.md). Twelve raw Bitcoin reference
discrepancies were reproduced and investigated. Two byte ranges match their original seals.
Ten match the upstream readings and original receipt summaries but lack a contemporaneous hash.
The evaluator rejects those ten for a binding decision. Keep the frozen recovery view for diagnosis.
The immediate operations tasks are to secure capacity for the remaining H1–H4 inputs on Apex
and for the lead to review and release the recorder/importer repair. The audit lists the
acceptance checks and evidence locations. Queue calibration can proceed independently.

For queue calibration, start with the [paired quote-expiry trial](../evidence/2026-11-26-quote-expiry/RESULTS.md).
The protocol was frozen at `b787e7a` and candidate `2688f00` recovered both fills lost to
inverted timestamps. Delivery on the original 16 orders rose from 30/32 to 32/32. The
two-minute holdout contained six screened wallet orders and zero eligible BTC 5m orders.
Only 3/14 known-end discovery orders had complete price allocation. The trial is closed and
the reconstruction timer stays off. The next calibration step requires independently identifiable
placements and ends, such as an existing order-owner acknowledgement export. Register the next
collection and sample requirement before viewing outcomes. The public archive alone does not
provide those order IDs. The [paired receipt](../evidence/2026-11-26-quote-expiry/paired-results.json)
contains all four run results and the retained Apex paths.

The [system audit](SYSTEM-AUDIT-20261126.md) records the earlier fixes. Allocation at maker
prices resolved the 97¢ routing miss. The cold 13-minute run took 16m 22.90s, used 1,054.8 MiB
peak memory and made zero RPC requests. Four of 65 known-end orders overfilled the pessimistic
bound. The [full run receipt](../evidence/2026-11-26-orderreplay/validation-results.json)
preserves the cold and resumed outputs. The sections below preserve the earlier investigation.

The integration combines Claude's receive-time patch, the canonical final-verdict evaluator,
and the Reconstruct, Rulefit, and Fidelity fixes. The combined suite passed 423 tests on the
previous pass. The final profile/handoff and receive-time checks passed separately. The registry
and sealed genomes were verified. The Apex archive at `/var/backups/doppel-sealed-20261126`
holds 20.3 GB of SHA-checked segments, 25 receipts, the frozen token map, and cohort v1.

## Replay investigation

The previous pilot is at
`/var/backups/doppel-sealed-20261126/research/ordertape-pilot.TUedd4` on Apex. The
`conditional-replay-2m` subdirectory contains the saved 14-order sample, baseline report,
and baseline replay rows. Its BTC 5m interval is `[1795629720, 1795629840)` UTC epoch seconds
with book warmup at `1795629600`. The pessimistic estimate filled 100 shares on one cancelled
7¢ order with 27.096743 observed chain shares. The sample routed 31 of 32 prints. A larger
pilot reconciled all 840 selected fill records before the memory stop. The 13-minute retry timed
out and its copied top-level manifest still describes the earlier 65-minute run.

### Trigger and evidence

The Apex trace `replay-trace.o6Sp7w` in the pilot directory captures the public frames,
the quote's queue changes, and the full baseline result. The target order begins
`3296e8e24b9e`. The print at `1795629743926` venue milliseconds was a native 7¢ SELL
of 934.2 shares in transaction `0x32aa9fa6cb28…`, before this order's first own fill at
`1795629744472`. The pessimistic queue before the print was 728.697873 shares.
The quote was still live, and the print entered the simulator once.

The cached calldata for the triggering transaction includes 95 shares executed at
8¢, including the mirrored 92¢ SELL, leaving 839.2 at 7¢. That price allocation would
still consume the nominal queue and all 100 simulated shares. The transaction
contains another 100-share 7¢ BUY signed 2 ms before the target. Multiple 100-share
book increases fit the target signature and predate its first own fill. The anonymous
level never fell below the target's remaining quantity before its last fill, so the
trace does not prove any one candidate belongs to the target.

### Code and verification

- `ordermatch.py` records the number of price, side, size and time candidates up to
  the first located fill. A one-to-one assignment is still a selected hypothesis.
- `orderreplay.py` carries the count into every row and reports bracket violations
  against one candidate, multiple candidates, or unknown placement evidence. All
  violations continue to count toward the existing gate. `reconstruct.py` carries
  the breakdown into the merged cohort report.
- `tests/test_orderreplay.py` covers an ambiguous placement that overfills and must
  remain in the bracket totals.

Run the Apex scripts from a scratch checkout of the relevant source with that
checkout's `src` on `PYTHONPATH`. The original pilot source is available in
`$pilot/src` for the baseline. The scripts write no recorder state.

```sh
base=/var/backups/doppel-sealed-20261126
pilot="$base/research/ordertape-pilot.TUedd4"
py=/opt/doppel/.venv/bin/python
output="$(mktemp -d "$pilot/recheck.XXXXXX")/results"
PYTHONPATH="$pilot/src" "$py" evidence/2026-11-26-orderreplay/trace.py "$base/tape" "$pilot/conditional-replay-2m"
PYTHONPATH="$PWD/src" "$py" evidence/2026-11-26-orderreplay/recheck.py "$base/tape" "$pilot/conditional-replay-2m" "$output"
```

The calibration gate remains open. Use independently attributable placements and
price-level quantities before drawing conclusions about order priority. The routine
reconstruction timer remains outside the scope of this calibration.

### Apex recheck result

The saved run is `$pilot/placement-recheck.Gbaiq4`. Its `recheck.log`, `results/report.json`,
and `results/placements/replay.jsonl` preserve the new results. Of the 14 known-end
orders, nine had one candidate and five had multiple candidates. The sole
pessimistic overfill was in the latter group. The aggregate still reports it.

For the target, the seven feasible placement candidates produced these simulated
fill ranges against 27.096743 observed shares. The original assignment is the first
row. These are sensitivity cases holding the observed cancellation boundary and
public print stream fixed.

| Venue milliseconds | Queue at placement | Pessimistic shares | Optimistic shares |
| --- | ---: | ---: | ---: |
| 1795629742703 | 737.30 | 100.00 | 100.00 |
| 1795629742723 | 982.30 | 9.97 | 100.00 |
| 1795629742728 | 1082.30 | 0.00 | 100.00 |
| 1795629742768 | 1529.60 | 0.00 | 100.00 |
| 1795629744129 | 4073.55 | 0.00 | 49.46 |
| 1795629744175 | 4378.55 | 0.00 | 49.46 |
| 1795629744271 | 902.31 | 0.00 | 27.08 |

Local verification used `pytest -q tests/test_ordermatch.py tests/test_orderreplay.py
tests/test_reconstruct.py` with the DOPPEL virtualenv and `src` on `PYTHONPATH`.
All 48 passed. The focused regression also passed after adding a candidate
that arrives after the first fill. Pyright and `git diff --check` passed. The 14-order replay is a
conditional diagnostic. Its separate routing miss and the 13-minute runtime
remain open. Next evidence should provide transaction-level allocation by price
and a placement observation independent of size and signed timestamp.
