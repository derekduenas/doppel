# DOPPEL deep system audit · November 26, 2026

**The build is in M2 research validation. Ten H1 reference hours still prevent a binding
decision. The current Apex deployment can still produce the same class of discrepancy.**
The review branch contains a tested repair. Both recorders were active on Apex at 23:01 UTC.
The installed release marker was `44aa7e2` and main was `3cff434`.
The [blueprint milestone board](BUILD-STATUS-20261126.md) covers M0–M6.

## Input recovery and preservation

The original H1 preflight passed 8 of 20 hours. In twelve hours, the raw Bitcoin reference
segment grew after the recorder issued the hourly receipt. The original files and receipts
remain on Apex. A separate recovery view now covers Nov 26 00:00–22:00 UTC, exclusive end.

| H1 hours | What could be verified |
| --- | --- |
| 10 | The latest sealed generation already matches the original receipt, including hours 20 and 21 added in this audit. |
| 2 | The exact receipt-length prefix matches a manifest hash recorded before the receipt. All 5,450 recovered observations also match the upstream feed. |
| 10 | The recovered prefixes end at complete compression frames and pass CRC, counts, timestamps and gap checks. All 32,474 observations match the original upstream publication time, value and receipt time. The historical hash at that byte boundary is missing. |

The reviewed evaluator strictly read 187 input files and verified 22/22 hourly receipts
in diagnostic mode. The binding check rejected 02 UTC, the first of the ten marked hours.
The upstream feed supplied the original importer, so it corroborates the recovered readings
without establishing when the recorder sealed those particular bytes. No strategy was run
for this recovery. Preserve the classifications, originals and frozen token map. The
recoverable input history, proof digests and reproduction command are in the
[recovery record](../evidence/2026-11-26-brti-recovery/README.md).

The subsequent 22 UTC hour was archived separately. All 15 streams and the original receipt
were retained. The binding-input preflight passed for that hour alone. Through 22 UTC,
13 of 23 hours therefore have acceptable H1 input evidence, and ten remain diagnostic.
[22 UTC receipt and verification](../evidence/2026-11-26-brti-recovery/hour22-proof.json)

## Apex capacity at 23:05 UTC

| Measure | Observed or projected |
| --- | --- |
| Archive free after preserving hour 22 | 37,929,230,336 bytes, including the configured 35 GiB reserve. Only 348,266,496 bytes (332 MiB) remained above it. |
| Hot tape disk | 24.49 GB free at 23:06 UTC, including its 12 GiB cleanup floor. It is the source disk for the independent archive. |
| Fifteen archived streams, measured over 23 hours | 567 MB/hour across the latest six hours, 655 MB/hour across all 23. Individual hours ranged from 531 to 819 MB. |
| Remaining 96 hours from Nov 27 00:00 to Dec 1 00:00 UTC | 54.4–62.9 GB (50.7–58.6 GiB) at those two average rates. Warmup, post-close reference data, receipts, token map, filesystem overhead and the operating reserve are additional. This is a planning range. |

For Nov 26 23 UTC, prioritize both PM US lanes, the three reference streams, both RTDS
feeds and the BTC 5m order book for the first flow lookback. Retain earlier RTDS history
for the first midnight wallet scores. From Nov 27 00 UTC, the registered flow evaluations
need the receipt-declared international order books as well, even though the strategies trade
BTC 5m. Preserve the full 15-stream set, original receipts and manifests, the frozen token
map and the post-close reference updates. Estimate a new volume or expansion using **usable
space after reserve**. The only other measured option on the archive disk was about 1.16 GB of
verified duplicate segment bytes. Deduplication alone would buy roughly one or two hours.
No data was deleted and the reserve was kept in place. [Hourly inputs and forecast](../evidence/2026-11-26-retention-forecast.json)
· [Observed free space](../evidence/2026-11-26-brti-recovery/hour22-proof.json)

## Findings, repairs and evidence

| Area | Finding and repair | Verification |
| --- | --- | --- |
| Recorder and reference importer | The recorder could publish a receipt before the scheduled importer finished that hour. The importer now stages and validates changes, resumes interrupted publication and advances a durable watermark. The recorder waits for that watermark, records a SHA-256 per stream and publishes receipts atomically. Late observations retain their original receive times in a separate ledger. | An isolated Apex run imported 7,200 real observations in two hours. Hour 17 was initially withheld and then sealed with 3,600 reference observations and 158,085 PM US records. The input check passed and a retry added zero rows. [Producer rehearsal](../evidence/2026-11-26-brti-recovery/producer-results.json) |
| Archive | A checksum for the last file on disk could mask a mismatch with the original receipt. The archiver preserves source manifest history, checks the selected byte generation and reports missing segments or receipts. The strict primary-tape command is in the runbook. | The 20 and 21 UTC extension retained 30 segments, two receipts and 1,083,812,109 bytes across 15 streams. [Recovery record](../evidence/2026-11-26-brti-recovery/README.md) · [Archive procedure](../ops/RUNBOOK-M0.md) |
| Scoring and monitoring | The evaluator now checks receipt membership and v2 segment hashes, binds manifests and flow-token-map provenance, and refuses diagnostic-only revisions for scoring. Flow requires a positive RTDS data-record count on at least one socket per evaluation hour. Previously scored observations are bound to a digest before a sequential monitor can advance. Commercial accounting includes wholly excluded days and assigns a window ending at midnight to the preceding day. Reconstruction quality gates use the actual counts before rounding. | Targeted regressions exercised each case, including a complete verdict at a UTC day boundary. The 21 legacy registrations retain their original provenance. The audit verified the frozen token-map SHA-256 `72f6055444f589d317fb24716b0dc9bb222a6d63b00fbc540ec82534abac0875`. [Evaluator protocol](GENOME.md) · [Monitoring protocol](sequential.md) |
| Data spend and coverage | Two requests could both pass the same credit check. The append-only ledger now reserves each bounded page under a lock before issuing it. An indeterminate charge remains reserved on retry. Coverage reports require a completed, unfiltered reference query spanning the measured interval before reporting absence or market-wide coverage. | The concurrent cap regression used two four-row requests with room for five. The archived comparison found 60,390 distinct RTDS transactions and zero reference rows. Chain coverage and the number absent from chain correctly returned null. The comparison read local Apex tape and incurred no provider calls. [Comparison receipt](../evidence/2026-11-26-chain-audit.json) |
| Opportunity research | The structural scanner valued a persistent signal using a price and size only visible before confirmation. It now values the first qualifying second, uses the corresponding liquidity and measures the remaining-leg delay from that decision. Cross-venue matching excludes unknown or conflicting strikes and close times. A settlement conflict suppresses the aggregate guaranteed-dollar estimate. | In a synthetic zero-fee case, the first quote implied $45 on 100 contracts. At confirmation only three contracts and $0.30 remained. The wrong-strike fixture produced zero matched opportunities. Historical opportunity-dollar reports using the old assumptions need a rerun before reuse. [Structural regressions](../tests/test_structural.py) · [Cross-venue regressions](../tests/test_xvenue.py) |
| Release and rollback | A failed candidate build could return success if a later command on the old release succeeded. The installer now locks publication, checks the candidate and mirror revision, atomically exchanges release directories and restores the prior release after a failed activation check. | Five staged cases passed on both macOS and Apex Linux, including failed installs, a moved main, failed startup and rollback. On Apex the filesystem exchange was real and the package install, mirror and service were simulated. [Apex staging receipt](../evidence/2026-11-26-release-staging.json) |
| Disk cleanup | The installed GNU find expression missed a real closed day. The free-space check also rounded up. The repaired job tests exact bytes and considers only valid past UTC dates in the allowed high-volume streams. It skips symlinks, references, paid data and receipts. | Read-only Apex enumeration reproduced the failure on Nov 25. Disposable macOS and Linux tests passed at one byte below the floor and with current, future and malformed dates. The existing ten-minute timer will use the repaired job at the next tick after deployment. **Verify retention before release.** [Janitor check](../evidence/2026-11-26-janitor-check.json) |

The Goldsky command also accepted overrides above the project owner's one-time $100
credit. The maximum configurable ledger cap is now 10 million rows at the project's
recorded $10 per million rate. The default is 7 million. All concurrent pulls for that
credit must use the same durable ledger. Invalid caps fail before the client opens a
connection. An unfinished wallet page is identified from the pagination cursor, including
when the run budget runs out partway through a wallet. [Spending regressions](../tests/test_goldsky.py)

## Release sequence and open gates

1. **Operations** should secure enough Apex storage for the remaining registered hours. Archive each
   required closed hour with its original receipt and manifest history. Re-run the input preflight
   immediately after each copy. The archive shares a host with the hot data under the owner's
   Apex-only retention decision.
2. **Release lead** should review this branch before merging to main, reconcile the recorder unit
   and its required BRTI-import setting with the installed systemd configuration, and use the
   repository release procedure for the coordinated recorder restart. The release script swaps the
   application tree. Updating an installed systemd definition requires a separate unit install and
   daemon reload. Verify the next real closed hour yields one immutable v2 receipt, matching raw
   SHA-256 and byte length, passing CRCs, an advancing import watermark and active data capture.
   Exercise restart recovery on an isolated copy. [Operating instructions](../ops/RUNBOOK-M0.md)
3. **Strategy research** should keep H1 input-blocked unless contemporaneous hashes are found.
   Score each registered family only at its close with admissible inputs. H1 closes Nov 29 00:00 UTC,
   H2/H3 Nov 30 00:00 UTC, and H4 Dec 1 00:00 UTC. Future registrations can start with the
   repaired protocol. Existing registrations predate evaluator-hash pinning.
4. **Market data and ledger owners** still owe 24 hours of independently measured ≥99.5% chain
   coverage, G0 on ≥50 comparable wallets, and the post-freeze G1 comparison against controls.
   **Queue research** still needs independent order acknowledgements, price allocation and
   a populated prospective holdout. Four of 65 known-end orders exceeded the pessimistic fill
   bound in the earlier replay. Keep the reconstruction timer off. No family has earned shadow
   promotion. Paper/live runtime, risk and reconciliation are downstream. [Milestone board](BUILD-STATUS-20261126.md)

## Claude Code pickup

Use the `review/system-audit-20261126` worktree at `/Users/derekduenas/doppel-integration`.
The original `/Users/derekduenas/doppel` checkout contains pre-existing uncommitted work on main.
The earlier baseline is `3c24fbb`. Commit `2a76230` covers archive, spend and scanners,
`94fee12` covers receipts and imports, `42efbae` covers evaluator and registry gates,
and `230b83d` covers release and rollback. The cleanup correction is `65acf09`, the
historical recovery tool and proofs are `5f8e421`, and the absolute ledger ceiling and
bounded-wallet report are `02729c9`. The raw tape and upstream capture bytes remain
on Apex. [Handoff index](CLAUDE-HANDOFF.md)

### Build verification

- The final source revision `02729c9` passed **511 Python tests in 310.27 seconds** on
  the Mac. The suite used isolated local server fixtures for network protocols. The final
  review also checked the source digests against the Apex recovery, producer, release
  and cleanup receipts.
- The full `src` Pyright check reported zero errors and warnings. The offline dependency
  lock and package build passed. All 64 Python modules in the wheel matched source bytes
  and all 30 console entry points imported from the wheel. The verified local wheel SHA-256
  was `5589c4c92e421d32d9aaed185c9d083344c8ca66e3b5e099f280207ed15d69c1`.
- The Rust workspace passed all 72 tests with the locked offline dependency set, Clippy
  across all targets with warnings denied, and the formatting check. Socket fixtures
  exercised local test servers.
- The registry verified 21 entries at head `7e4843a45ad1dc25`. The expression probe
  remained v3, digest `49083cf45e14`. There were no changes to `genomes/` or `registry/`
  in this audit.
- An intermediate macOS recovery run exposed a permission failure when the staging
  directory was made read-only before publication. The committed helper verifies and
  protects the contents, renames the staging directory, then makes its top-level directory
  read-only. The end-to-end regression also checks that a repeat cannot overwrite the
  published result. [Recovery tests](../tests/test_brti_recovery.py)
