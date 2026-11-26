# Claude Code handoff

## Where to resume

Open `/Users/derekduenas/doppel-integration`. The integration worktree now uses the review branch
`review/system-audit-20261126`, based on `3cff434`. Run `git status --short` to include
new files in the review. The original checkout is `/Users/derekduenas/doppel`; its working
tree and the three published pods are separate from this integration.

The [system audit and replay validation](SYSTEM-AUDIT-20261126.md) records the follow-up to this investigation.
Start there for current results. Allocation at maker prices fixed the 97¢ routing miss. The
fresh trial still missed two deliveries because a later venue timestamp arrived before the
corresponding fill. The next calibration trial needs a prespecified lifetime rule for this
clock inversion and another untouched interval. The cold 13-minute run completed in
16m 22.90s with 1,054.8 MiB peak memory and zero RPC requests. Four of 65 headline orders
overfilled the pessimistic bound. The reconstruction timer remains gated. See the
[run receipt](../evidence/2026-11-26-orderreplay/validation-results.json) for commands and
for the cold and resumed outputs. The sections below preserve the earlier investigation.

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
