# Doppel Sprint v3-B handoff · 2026-11-28 UTC

Opus owns merges, Apex deployments, live data jobs and Shadow activation. These changes are review branches. Keep the Layer 5a scorecards, the frozen Cohort V1 payload and the evaluator at `c2d01f506f6dcb32ab935df3632e500b798bab5444779c910a7a683e0f3d25e4` intact through H4 scoring.

| Review branch | Work prepared | Live acceptance still due |
|---|---|---|
| `sprint/v3b-platform-storage` | Policy-aligned archival, checksum verification, retention interlocks, deterministic Cohort V2 slice export | Opus merges and deploys PR #1, publishes archive and feed health receipts, and seals the actual cohort snapshot |
| `sprint/v3b-shadow-admission` | Independent admission validator, immutable receipts and admission enforcement at the paper runtime entry point | Canonical evidence production, lead approval and a live shadow session |
| `sprint/v3b-layer5b` | Precommitted forecast and score phases with causal timestamps, a measured control, source validation and RSS reporting | A registered panel from continuous, overlapping BTC books, BRTI, Binance and Hyperliquid captures |

## V3.1 platform and freeze

[PR #1](https://github.com/derekduenas/doppel/pull/1) head `aa62079` passed all 12 hosted checks and remained a draft on the 00:44 UTC check. Apex reported release `23f3d15`, an active recorder, storage timer and R2 backfill. The active backfill alone is insufficient evidence of complete, checksum-verified offload. Keep the 1,088 closed tape segments and 81 PERELMAN hours as named inventories, and publish the count and hash of each independent verification receipt. Check headroom on the actual tape mount. After the deployment, record the full 15-minute health interval and drop/write-error deltas for every active stream, including both offshore feeds.

Use the single-writer cutover in [STORAGE.md](../STORAGE.md). Follow Opus's per-class allocation in [the sprint sync](SYNC-CODEX-SPRINT-20261128.md). Gate retention on a current verified archive report.

The deterministic freeze requires the pre-cutoff token map with SHA-256 `72f6055444f589d317fb24716b0dc9bb222a6d63b00fbc540ec82534abac0875` and Cohort V1 with SHA-256 `1775c0998506abf721e27ed3cb0043b5816b2dde587462034e55a4622a396b54`. The Apex live copy and the Nov 26 sealed token map matched at inspection. Run the pinned `pod/cohort-v2` builder at `d1eb412` on a sealed copy of closed, pre-cutoff tape on the research node. Pass the historical cutoff explicitly.

```sh
doppel-cohort-v2 --data "$TAPE" --tokens "$TOKENS" --cohort-v1 "$COHORT_V1" \
  --freeze-at 2026-11-28T00:00:00Z --out "$BUILDER_OUTPUT"
python ops/seal_cohort_v2.py --tape "$TAPE" --tokens "$TOKENS" --cohort-v1 "$COHORT_V1" \
  --manifest "$BUILDER_OUTPUT/cohort-v2.json" --stats "$BUILDER_OUTPUT/cohort-v2-stats.json" \
  --out "$FROZEN_EXPORT"
```

The exporter verifies the pinned builder fingerprint, the original source bytes and every listed native tape record's receive time. The two slice files and `freeze-receipt.json` record the exact membership, observed null and shortfall, source inventory and strict exclusive cutoff. Preserve the original combined manifest and stats alongside the receipt. Publish the SHA-256 of each artifact after the live freeze. The pinned builder's RTDS parser covers 5m and 15m windows. Hourly and daily triage require separate registered coverage. Preserve the midnight cohort definition when adding those markets.

## V3.2 shadow admission

`doppel-paper run --shadow-bundle BUNDLE` requires Opus to install `/etc/doppel-shadow/approval.json` as a root-owned, read-only file plus the approved release and registry. The config pins the verification key, trial-plan SHA-256, active genome roster, and absolute data, journal and reference roots. The deployed commit is read from the protected release marker. The pass verdict, ledger, scorecard, fee/depth scenarios and explicit measurements are checked against that trust configuration before a session can start. The process stops on expiry or when approval changes. Keep diagnostic paper sessions in `RESEARCH_OBSERVE`. This path has no order submission or canary activation.

The signed closure commits the plan and the complete research trial inventory. Its independent reviewer must reconcile actual trial execution to the frozen plan, including every failed or discarded wallet/variant. A chain digest authenticates the ledger bytes and structure. Completeness outside the logged inventory rests with the signing reviewer. Use the evidence contract on the admission branch when preparing the bundle.

## V3.3 Layer 5b

The Layer 5a timing gap is separately recorded as `UNMEASURED` in `evidence/2026-11-28-layer5b/layer5a-timing-status.json`. The synthetic CI fixture tests recall, precision, one-to-one intent matching, abstention and perturbed future inputs. The real result requires a sealed native data inventory and a reviewable, pinned raw-to-panel producer. Predeclare the lead-to-BRTI transform on development data. Use disjoint BTC market groups, publish the plan and forecast hashes before opening holdout intent, and keep both phases under 1.5 GB on a research compute host. Leave the Nov 28–Dec 4 PM US confirmation tape out of study design. [Layer 5b preregistration](https://github.com/derekduenas/doppel/blob/sprint/v3b-layer5b/evidence/prereg/2026-11-28-layer5b-taker-timing.md)

## V3.4 real-wallet acceptance

The scorecards and admissions are pending. Build the cohort decision table from causal book, fair and lead inputs; produce split-period +2s/+10s/+60s markouts, out-of-sample log loss versus the contemporaneous mid and reconstructability denominators. Preregister the wallet/variant inventory, then replay qualifying wallets with the full 3-fee × 3-depth grid. Before requesting activation, produce a complete admission bundle and wire the live runtime to epoch-level E-value and decay measurements. A market pivot should use the completed BTC triage result.
