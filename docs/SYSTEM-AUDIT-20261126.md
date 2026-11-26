# DOPPEL system audit and replay validation

## Baseline and scope

Audit of the recovered integration tree, 2026-11-26. GitHub and Apex independently returned main
at `3cff434`, fidelity at `304f01d`, reconstruct at `7f05f38`, and rulefit at `bd1a4fe`.
The installed Apex release marker was `44aa7e2`. The historical investigations and original
pilot paths are in [Claude's handoff](CLAUDE-HANDOFF.md).

At 21:33 UTC both recorders were active. The 20:00 UTC receipt listed 20 streams with data.
Every stream reported CRC success. RTDS A captured 53,317 of the 53,740 distinct transactions
seen by either socket and B captured 53,047. These are socket agreement measures. The receipt
has no independent chain denominator. The tape volume had 25 GiB free and the archive filesystem
had 38 GiB free. The owner selected Apex-only retention for this pass.

## Findings before changes

- A public transaction can execute at several maker prices. The simulator assigned the entire
  quantity to the displayed price. A 1,005.14-share public print on the complementary token
  consequently failed the exact-price check for a real 97¢ BUY. The same public print could
  already trigger a simulated fill by trading through that order's limit. This corrects the
  earlier inference that a different transaction filled it.
- The reconstruction cache checked the length of retained artifacts during reuse. A same-length
  change would evade this check. An interrupted invocation using an existing output directory
  could leave the previous run's top-level manifest visible. A transaction-capped selection could
  also be treated as complete for its wallet despite omitted in-range fills.
- Some downstream readers globbed parquet files. A process crash before writing an order-tape
  flush manifest could leave an orphan visible to those readers.
- Tape receipt parsing tolerated an incomplete final compression frame. This is appropriate when
  tailing a recorder, but a closed-hour verifier could report success for the readable prefix.
- The canonical final evaluator hashed files present in a snapshot. There was no check that
  a receipt and the required market stream existed for each evaluation hour. The monitoring CLI
  could append an early binding result directly from an imported JSON file.
- Review of the repaired recording path found that the first monitoring event in a family could
  still select alpha after inspecting outcomes. New registrations now pin the sequential code and
  alpha. An unpinned family defaults to 0.05 unless an existing record already froze a value.
  The canonical evaluator also reads every selected compressed segment with strict frame and CRC
  checks, including segments sealed under the older receipt format.

## Validation design

Use the original 14-order sample to test the mechanism. Freeze a second BTC 5m interval before
reading its outcomes. Retain every qualifying placement, the ambiguity groups, missing-data counts
and denominator for independently identified placements. Repeat the original 13-minute reconstruction
with zero RPC requests, a separate output directory and a measured resource envelope.

## Operational limits

The sealed archive lives under `/var/backups/doppel-sealed-20261126` on a different filesystem
on Apex. Both copies share a host. The latest archived chain comparison has no Goldsky reference
for the sampled hour. The Rust shadow still relies on Python for hourly receipts. Project
milestones for full-chain coverage, queue calibration, shadow performance, and live execution
have separate acceptance criteria. The research runs here do not alter the installed services.

The archive re-read checked all 586 manifested segments across 22 primary and three shadow
stream directories, totalling 20,294,458,157 bytes. Every stored size and SHA-256 matched.
An independent `zstd -t` pass decompressed every archived segment successfully.
The live desk also built from the installed bundle and current lab data, with both recorders,
19 displayed streams, 21 genomes across four families, 30 twin files and zero input problems.
The rendered desk is in `/var/tmp/doppel-audit-desk.lSkUnm` on Apex.

## Repairs and measured replay results

The order adapter now reconciles complete, verified-anchor calldata against the public print's
market, token pair, direction, price and total shares. It applies the decoded maker quantities at
their respective levels once. Public-only and inconsistent prints have explicit reason counts.
The research pipeline hashes cached outputs, uses published flush parts for managed order tapes
and records interrupted runs. Each wallet's fill-store coverage has its own denominator.
Closed-tape verification now fails on invalid frames or an empty manifest set. The final verdict
and recorded sequential CLI paths both replay sealed inputs and check hourly receipts.
Imported sequential results remain available for inspection.

The Apex working root for the following artifacts is
`/var/backups/doppel-sealed-20261126/research/ordertape-pilot.TUedd4`.

| BTC 5m sample | Orders placed / known end | Own fills routed on public headline | Routed after allocation | Delivered to live quote | Known-end bracket violations |
| --- | ---: | ---: | ---: | ---: | --- |
| Original diagnostic | 14 / 14 | 31/32 | 32/32 | 32/32 | 1 pessimistic overfill |
| Frozen follow-up | 16 / 14 | 29/32 | 32/32 | 30/32 | 2 optimistic underfills |

The original sample's [97¢ transaction](../evidence/2026-11-26-orderreplay/route-97c.json) contained
40 maker legs and only one public frame in the archived hour. Quantities were 419.45 at 97¢ and
585.69 at 96¢ in the order's token view. The other problematic order at 7¢ had seven feasible
placements. Its public print contained 95 shares at 8¢ and 839.2 at 7¢. The selected placement
still gave a 100-share simulated fill against 27.096743 observed shares. Seven of the 14 orders
had complete same-side price allocation while live. None of those seven violated the bracket.
The complete 52.09-second run lives in `allocation-final.emH0Go/results`. Its `receipt.json`
has SHA-256 `c657ad3fc43d03cc000f20e20aff7bd3b483485c19c1a067c34850b0617f043d`.

The [frozen sample definition](../evidence/2026-11-26-orderreplay/fresh-sample.json) has SHA-256
`15d7c2810cc9493245dbd371ed04a4d1961606ef285af87414d66b8b0d31cbc7`. The interval
was `[1795630500,1795630620)`, immediately after the earlier 13-minute range and within a
wallet-hour studied previously. Its order-level replay results were unseen when selected.
The known local fill store reconciled 34/34 on block time.
Two of 14 known-end orders had full price allocation. Both bracket violations occurred among
the other twelve. Neither the original nor the follow-up sample has an order-ID-bearing placement
acknowledgement. A single anonymous candidate remains an inference.

[The two delivery traces](../evidence/2026-11-26-orderreplay/fresh-delivery.json) reproduce the frozen
misses. An unrelated book update arrived 104,113 ns before the 98-share order's fill print and
205,930 ns before the 97-share order's print. Each book update carried a venue timestamp 1 ms
beyond the order's inferred end. Processing in receipt order had already expired both quotes.
The archived trace is `audit-validation.4OcRq1/delivery-trace.NX7c2e/trace.json` with SHA-256
`9db2a67931aedf86ea0a9518305fb89870e59947cf562fc21e7b41ae60e61117`.

Before the next calibration trial, specify how out-of-order venue timestamps interact with
quote lifetime, keep the receive clock for all as-of decisions, and freeze another untouched
interval. Scoring against an order's own fill as proof that its simulated quote remained live
would be circular. Preserve the observed 30/32 delivery result as the outcome of this trial.

The repeat of the frozen trial used `audit-validation.4OcRq1/final/fresh`. Its per-order JSONL
matched the preceding run byte-for-byte, SHA-256
`b037cfa40ddd29c9503f87a6b11d5d826c4bd494457aa21b721e96b06a95ac04`.
Matching took 65.18 seconds and replay took 64.14 seconds on the shared host.

## Build and reproduction

The reconstruction source is commit `2ec08ab` on `review/system-audit-20261126`. Registry-only
hardening followed in `5d93138`. The final source
distribution and wheel built offline. All 64 source modules in the wheel matched the checkout
byte-for-byte, and all 30 declared command entry points imported. The wheel SHA-256 in this run
was `f350873974e48fa513b11a084f0840bc24423da493f38c33982ae680c4840fce`. The dependency lock
validated offline. Registry verification found 21 valid records with head `7e4843a45ad1dc25`,
and the expression check passed at semantics `49083cf45e14`, probe v3.
The final integration suite passed all 446 Python tests in 256.05 seconds. Pyright reported
zero errors and warnings. An earlier suite ran while checkpoint signatures were being edited and
failed a cache-reuse assertion. Both the isolated reconstruction suite and the final full run
passed against stable source.

Use a unique temporary pytest directory when several workers share this Mac. Python tests
include loopback mock servers. The Mac initially exhausted free space during concurrent Python
and Rust checks. Cleaning the two crates' regenerable debug products reclaimed 1.2 GiB.
The unchanged Rust workspace and its committed parity fixtures were then tested in isolation on
Apex under `/var/tmp/doppel-rust-audit.EJDQb2`, offline, with one low-priority build job.
Rust formatting and Clippy with warnings denied passed, as did all 72 Rust tests.
After verification, `cargo clean` removed the 614.8 MiB disposable target there. The logs,
source and fixtures remain available.

```sh
uv lock --check --offline
uv build --offline --out-dir "$(mktemp -d)"
PYTHONPATH=src /Users/derekduenas/doppel/.venv/bin/python -m pytest -q --basetemp="$(mktemp -d)"
/Users/derekduenas/doppel/.venv/bin/pyright --pythonpath /Users/derekduenas/doppel/.venv/bin/python src
PYTHONPATH=src /Users/derekduenas/doppel/.venv/bin/doppel-genome verify
PYTHONPATH=src /Users/derekduenas/doppel/.venv/bin/doppel-genome dna
cargo fmt --check
cargo test --locked --offline -j1
cargo clippy --locked --offline --all-targets -j1 -- -D warnings
```
