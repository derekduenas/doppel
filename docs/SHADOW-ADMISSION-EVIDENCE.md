# Shadow admission evidence

Shadow intake pins the operator Ed25519 public key, deployed commit, preregistered trial-plan SHA-256, current evaluator, and complete active-genome roster independently of candidate inputs. The registry must contain the first bound replay verdict for the exact sealed genome. On the approval machine, verify the raw source receipts, held-out evidence, and actual trial inventory before signing. The intake validator checks the signed receipts and the complete chain against the independently pinned plan. The signature is the operator's attestation of the receipt sources.

## Produce and sign a bundle

Run on research or approval compute with the immutable inputs. Keep actual wallet evidence and private keys out of Git.

```sh
doppel-promotion --results "$REPLAY" --trials "$TRIAL_LEDGER" --out "$PROMOTION_DIR"
cp "$FROZEN_PLAN" "$PROMOTION_DIR/plan.json"

doppel promote bundle --promotion-dir "$PROMOTION_DIR" --replay-artifact "$REPLAY" \
  --trial-ledger "$TRIAL_LEDGER" --gate-results "$GATES" --genome "$GENOME" \
  --registry-root "$REGISTRY_ROOT" --commit-sha "$RELEASE_SHA" --plan-sha256 "$PLAN_SHA" \
  --public-key-hex "$PUBLIC_KEY_HEX" --valid-from-ns "$FROM_NS" --valid-until-ns "$UNTIL_NS" \
  --out "$BUNDLE"
```

Supply each already-active genome as a repeatable `--active-genome` argument. The command produces the SHA-256 of the approval manifest and never overwrites a bundle directory. The unsigned output supports independent review. To produce signed output, run with the same inputs into a fresh directory and pass either `--signature` with a detached binary Ed25519 signature or `--signing-key` with an owner-only file containing a raw 32-byte Ed25519 seed for the pinned public key. Sign the exact canonical `approval.json` bytes prefixed by `DOPPEL-SHADOW-APPROVAL-v2` and a NUL byte. Both signing methods verify the signature before publishing the output. Admission requires the signed bundle.

The canonical trial ledger supplies the reporting command with its variants and returns. The bundle verifies the exact replay and ledger hashes used by that promotion report. It independently recomputes DSR, FDR, HAC, PBO and the block bootstrap from the verified ledger before signing, and admission repeats the calculations. Lock the trial plan before the registered replay interval. A changed plan needs a separate preregistration.

## Input files

Use sorted-key JSON with orjson and an optional final newline for canonical inputs. The promotion report, Layer 5b scorecard and native offshore receipts keep their original formatting since their digests cover exact file bytes. Put the applicable artifact files next to the gate results.

| File | Content |
| --- | --- |
| `plan.json` | `doppel.shadow-trial-plan/v1`, registry `family`, cutoff `frozen_ns` and ordered `attempts`. Every attempt has SHA-256 `trial_id`, `genome_id`, `subject_id`, `variant_id`. Preserve every wallet and variant evaluated. |
| `trials.json` | `doppel.shadow-trial-ledger/v1`, exact `plan_sha256`, ordered nonoverlapping `windows` and `trials` in plan order. Each row has its four identity fields, zero-based `seq`, `previous_sha256`, aligned `returns_c`, and the canonical hash of the row without `sha256`. The first predecessor is 64 zeros. |
| `replay.json` | `doppel.shadow-replay/v1`, `genome_id`, `commit_sha`, exact registry `verdict`, eligible `windows` and nine `scenarios`. Scenarios report `fee_tier` (`api_live_rebated` 0.02085, `api_base_0695` 0.0695, `maker_0` 0), `coefficient`, `size` (25, 100, 500), `execution=l2_book_walk_vwap`, `attempted`, `filled`, `net_c`. Diagnostic `gateway_0095` rows are accepted in addition. Include `trial_id` when multiple trials share the candidate genome ID. |
| Gate results | A `gates` map. Supply measured `paper_replay_slippage` as `status`, `value`, `observations`, `source_sha256`. Supply `residual_correlation` as `status`, `values` keyed by every active genome, `observations`, `source_sha256`. Takers also supply those numeric fields for `markout_2s` and `markout_10s`. The bundle fills statuses for recomputable gates and the exact provenance links for archetype artifacts. An explicit input status can never override a failed proof. |
| `layer5b.json` | Takers require the recorded `doppel.layer5b-score/v1` with both benchmark families, random abstention, matched-count evidence, zero-leakage checks, ±1 s timing and measured RSS. The candidate's sealed trigger must match its benchmark family. |
| `binance.json`, `hyperliquid.json` | Native `doppel.layer5b-source/v1` receipts with exact role and `local_receive_ns`, plus `segments` containing relative native paths, stream names, SHA-256 digests and half-open `covered_range_ns` intervals. The taker scorecard must name these exact receipt hashes. An offshore-guarded maker needs their coverage over the eligible replay windows. |
| `maker.json` | `doppel.shadow-maker/v1`, `genome_id`, `archetype`, `replay_sha256`, and `input_sha256` from the registry-bound replay. The `queue` receipt reports model `pessimistic_l2_fifo`, source digest, `windows`, `quote_observations`, `known_queue_observations`, `fills`, `verified_fills`, `fill_windows`, `feed_gap_windows`, `min_trade_coverage`, `max_inferred_share`, `max_unidentified_share`. The `markouts` map has `2s` and `10s` with positive `value`, `observations`, `windows` and `source_sha256`. The `stress` receipt reports `method=delayed_cancel`, `plan_sha256`, `source_sha256`, `windows`, `base_cancel_latency_ms`, `extra_cancel_latency_ms`, `net_c_per_window`, `ci_low_c_per_window`. Record the locked stress assumptions and all eligible windows. |
| `probability.json` | Candidate probability model for the Shadow watchdog. Its exact digest is included in the receipt when supplied. Its genome ID and fit end must match the candidate and predate the registered replay. |

The derived archetype comes from the executable genome mode and its hashed evidence. A taker uses `taker_trigger_source=lead` or `fair`. Makers may explicitly declare `archetype=structural_twap`. List public data sources referenced by a maker cancel guard in the hashed `cancel_guard_sources` field. A pure maker can supply both offshore receipts, or omit them. Both taker archetypes require them.

The assembled `doppel.shadow-gates/v2` map records `MEASURED_PASS`, `MEASURED_FAIL`, `UNMEASURED`, or `NOT_APPLICABLE_ARCHETYPE`. A promotable bundle requires every applicable gate to pass and every inapplicable gate to be explicitly marked. Version 1 signatures require reassembly and renewed approval under the archetype-aware contract.

Admission independently rechecks the hard gates, then issues `<receipt_dir>/<genome_id>/admission_receipt.json`. The exact canonical object without `receipt_sha256` reproduces that digest. Only a byte-identical retry can reuse an existing receipt. The receipt authorizes the paper Shadow path. Successful runtime startup separately records activation in its journal.
