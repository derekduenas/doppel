# Draft · First-wallet admission runbook

For Opus. Commands below match `746cf42` except the explicitly labelled replay exporter. Perform research on a detached, sealed tape snapshot. Keep wallet evidence and signing material out of the repository. Use an operator-approved registry workspace and a fresh output directory per attempt.

## 1. Reconstruct and freeze the candidate

Start with a frozen cohort entry, signed order evidence, a compacted fill store, and a wallet absorption report with `status=reconciled`, `not_reconciled=false` and exact on-chain coverage. Pin all inputs. Set the development interval `[FIT_FROM,FIT_TO)` and a disjoint, predeclared fidelity interval `[HOLD_FROM,HOLD_TO)`. The later canonical forward replay interval must also be disjoint from both. Keep the entire attempt inventory, including wallets that abstain or fail.

**Maker or structural TWAP.** Use separate reconstruction manifests for development and holdout. Provide the independent coverage receipt for the wallet and exact holdout order tape.

```sh
doppel-reconstruct --data "$RESEARCH_TAPE" --lab "$FROZEN_LAB" --store "$FILLS" \
  --cohort "$COHORT" --wallets "$WALLET" --from "$FIT_FROM" --to "$FIT_TO" \
  --chunk-minutes 15 --out "$FIT_RECON"
doppel-rulefit --data "$RESEARCH_TAPE" --brti-lab "$FROZEN_LAB" \
  --reconstruct-manifest "$FIT_RECON/manifest.json" --wallet "$WALLET" \
  --from "$FIT_FROM" --to "$FIT_TO" --out "$FIT"
doppel-reconstruct --data "$RESEARCH_TAPE" --lab "$FROZEN_LAB" --store "$FILLS" \
  --cohort "$COHORT" --wallets "$WALLET" --from "$HOLD_FROM" --to "$HOLD_TO" \
  --chunk-minutes 15 --out "$HOLD_RECON"
doppel-fidelity --data "$RESEARCH_TAPE" --lab "$FROZEN_LAB" --genome "$FIT/genome.json" \
  --wallet "$WALLET" --reconstruct-manifest "$HOLD_RECON/manifest.json" \
  --coverage-receipt "$HOLD_COVERAGE" --from "$HOLD_FROM" --to "$HOLD_TO" --out "$FIDELITY"
jq -e '.verdict.decision == "PASS"' "$FIDELITY/fidelity.json"
```

If the fitter emits no `genome.json`, record the reason from `fit_report.json`. Fidelity `INCONCLUSIVE` or `DIAGNOSTIC` is missing qualifying proof. A `FAIL` is measured failure. Record `verdict.binding` and `verdict.reason` in either case.

**Taker.** Use chronological development data with the inverter's registered internal fit/holdout split. The absorption report must extend at least 120 seconds beyond the decisions interval. The fee coefficient describes the source wallet's venue. Use a separate untouched forward replay to judge the sealed candidate.

```sh
uv run python -m doppel.lab.wallet_decisions --data "$RESEARCH_TAPE" --brti "$REFERENCE" \
  --orders "$WALLET_ORDERS" --absorption "$ABSORPTION" --wallet "$WALLET" \
  --from "$DECISIONS_START_S" --to "$DECISIONS_END_S" --horizon "$HORIZON" --out "$DECISIONS"
jq -e '.status == "OBSERVED_DECISIONS"' "$DECISIONS/evidence.json"
doppel-invert --decisions "$DECISIONS/decisions.jsonl" --venue intl \
  --fee-coef "$SOURCE_TAKER_COEF" --out "$INVERSION"
jq -e '.verdict == "IDENTIFIED"' "$INVERSION"
```

Use `HORIZON=5m` or `15m` with this decision exporter. An `ABSTAIN` decision export reports `unverified_order_tape`, `missing_complete_reconciled_absorption`, or `missing_qualified_takes_or_passes` in `evidence_gap`. Inversion `NOT_IDENTIFIED` records its exact `reasons`. Its output is a research report. It takes an independently reviewed translation into a supported executable candidate before the registry can accept it.

Seal the executable genome as `candidate` with fit provenance, supported live features and its actual archetype. Keep a single supported PM US asset and horizon for the first Shadow session. Review the genome's expression and lock the candidate-specific probability fit upstream. Seal the full plan before the forward period, including variants that were tried and controls. If a lead signal or cancel guard is unavailable to the live interpreter, record the candidate as blocked.

## 2. Register and produce the canonical replay

Run the registration commands before the forward interval opens. The current `doppel-genome register` CLI pins `published-opening`; use this example only when the preregistration specifies that protocol. Opus must use the matching registration policy and protocol binding for any other approved protocol. Keep scorer and simulation settings pinned through admission.

```sh
doppel-genome register --root "$REGISTRY_ROOT" --genome "${FAMILY_GENOMES[@]}" \
  --family "$FAMILY" --hypothesis "$HYPOTHESIS" --start "$FORWARD_START" --end "$FORWARD_END"
doppel-genome bind-protocol --root "$REGISTRY_ROOT" --family "$FAMILY" \
  --protocol published-opening --execute
doppel-genome verify --root "$REGISTRY_ROOT"
doppel-genome dna --root "$REGISTRY_ROOT"
```

After the registered interval closes and the snapshot receipts verify, record the first binding verdict for each registered executable genome. Use the sealed snapshot command below, including `--lab` if the registry replay calls for a frozen token map. The `--results` import and `--p` produce nonbinding entries.

```sh
doppel-genome result --root "$REGISTRY_ROOT" --id "$GENOME_ID" \
  --data "$SEALED_FORWARD_TAPE" --lab "$FROZEN_LAB"
```

**Pending implementation.** The next command is the proposed interface in the [exporter spec](REPLAY-EXPORTER-TRIAL-RUNNER-DRAFT.md). The current CLI cannot produce the signed-input replay and trial ledger end-to-end. Stop here if the reviewed exporter or complete equivalent sealed evidence is unavailable.

```sh
doppel promote replay-export --run-spec "$RUN_SPEC" --plan "$PLAN" \
  --plan-sha256 "$PLAN_SHA" --data "$SEALED_FORWARD_TAPE" --lab "$FROZEN_LAB" \
  --registry-root "$REGISTRY_ROOT" --commit-sha "$RELEASE_SHA" --out "$RUN"
REPLAY="$RUN/candidates/$TRIAL_ID/replay.json"
TRIAL_LEDGER="$RUN/trials.json"
doppel-promotion --results "$REPLAY" --trials "$TRIAL_LEDGER" --out "$PROMOTION_DIR"
jq -e --arg gid "$GENOME_ID" \
  'any(.candidates[]; .genome.id == $gid and .decision == "PROMOTE")' "$PROMOTION_DIR/promotion.json"
cp "$PLAN" "$PROMOTION_DIR/plan.json"
```

## 3. Review the receipts and sign

Place evidence files beside `$GATES`. Source the release SHA, plan digest, validity, public key and full active roster independently of the candidate. Use the existing [schema and signing contract](../SHADOW-ADMISSION-EVIDENCE.md).

| Applies to | Required proof |
|---|---|
| All | First binding twin `PASS`, replay with all nine fee/depth cells, and complete plan-matched ledger. DSR ≥ 0.95, PBO < 0.15, BH at q = 0.05, HAC t > 0 and one-sided p ≤ 0.05, bootstrap p ≤ 0.05. At least one 0.02085 cell with positive net and ≥ 50% full-target completion. Measured absolute paper/replay slippage ≤ 0.3¢, residual correlation < 0.30 for every active genome, and signed `probability.json` with `fit_end_ns` strictly before the registered replay start. An independently verified empty active roster permits an empty correlation map with provenance. |
| Maker or structural TWAP | `maker.json` binds the replay and registry input digest. Pessimistic L2 FIFO with verified fills, known queue observations and zero gap windows. +2 s and +10 s post-fill marks > 0. Preregistered delayed-cancel stress covers the same windows, leaves mean > 0 and lower CI ≥ 0. Set taker timing to `NOT_APPLICABLE_ARCHETYPE`. A cancel guard using offshore inputs requires both native receipts covering the maker's eligible replay windows. |
| Lead or fair taker | `taker-measurements.json` binds both nonnegative short marks, slippage and correlation to this replay. Include native `binance.json`, `hyperliquid.json` and recorded `layer5b.json`. Match the sealed trigger to the benchmark family. Both scripted subjects need precision, recall and F1 ≥ 0.90, ≥ 20 truth intents across ≥ 10 markets, signed median within ±1 s and median absolute error ≤ 1 s. Require both zero-leakage checks and the random control's measured abstention and zero-leakage check. Forecast and scoring RSS each < 1.5 GB. Maker-only gates are `NOT_APPLICABLE_ARCHETYPE`. |

In Bash, set `ACTIVE_IDS=()` and `ACTIVE_ARGS=()` for the first genome. Otherwise use the independently verified roster and build `ACTIVE_ARGS` with one `--active-genome ID` pair per entry. On the approval host run this first with a fresh unsigned output and omit the signature argument. Review the inputs, obtain a detached signature over the exact `approval.json` bytes prefixed by `DOPPEL-SHADOW-APPROVAL-v2` and a NUL byte, then rerun into a fresh `$SIGNED_BUNDLE` using that binary signature.

```sh
doppel promote bundle --promotion-dir "$PROMOTION_DIR" --replay-artifact "$REPLAY" \
  --trial-ledger "$TRIAL_LEDGER" --gate-results "$GATES" --genome "$GENOME" \
  --registry-root "$REGISTRY_ROOT" --commit-sha "$RELEASE_SHA" --plan-sha256 "$PLAN_SHA" \
  --public-key-hex "$PUBLIC_KEY_HEX" --valid-from-ns "$FROM_NS" --valid-until-ns "$UNTIL_NS" \
  "${ACTIVE_ARGS[@]}" --signature "$DETACHED_SIG" --out "$SIGNED_BUNDLE"
```

## 4. Preflight admission and verify activation

On approval compute, this CLI invocation runs the public admission API and prints the full rejection reason. Use a separate preflight receipt root. The operator can rerun the actual runtime's admission using the signed bundle. In Bash pass the independently pinned active IDs as positional arguments after the receipt root.

```sh
uv run python - "$GENOME" "$SIGNED_BUNDLE" "$REGISTRY_ROOT" "$RELEASE_SHA" "$PLAN_SHA" \
  "$PUBLIC_KEY_HEX" "$PREFLIGHT_RECEIPTS" "${ACTIVE_IDS[@]}" <<'PY'
import sys
from pathlib import Path

import orjson
from doppel.strategy.genome import from_dict
from doppel.strategy.registry import Registry
from doppel.strategy.shadow_admission import AdmissionRejected, AdmissionTrust, admit_shadow, simulation_digest
from doppel.twin.engine import SimConfig

genome_file, bundle, root, commit, plan, pubkey, receipts, *active = sys.argv[1:]
genome = from_dict(orjson.loads(Path(genome_file).read_bytes()))
root = Path(root)
trust = AdmissionTrust(bytes.fromhex(pubkey), root, commit, plan, tuple(active))
binding = Registry(root).protocol_binding(genome.id)
if binding is None:
    sys.exit("REJECT_NONCANONICAL_REPLAY: missing registry protocol binding")
try:
    receipt = admit_shadow(genome, Path(bundle), trust, Path(receipts),
                           simulation_sha256=simulation_digest(SimConfig(**binding["params"]["simulation"])),
                           require_probability=True)
except AdmissionRejected as exc:
    sys.exit(str(exc))
print(receipt["receipt_sha256"])
PY
```

| Diagnostic | Evidence to inspect |
|---|---|
| `REJECT_INCOMPLETE_LEDGER` | Compare pinned plan, exact trial count, rows/head hashes, window alignment and candidate returns. An unestimable family blocks canonical promotion upstream. Preserve every failed or abstained row. |
| `REJECT_NONCANONICAL_REPLAY` | Verify replay bytes, nine-cell grid, first binding result, protocol, source digest and simulation. Check that promotion used the same bytes and statistics. |
| `REJECT_UNMEASURED_GATE` | Preserve the returned reason. Check for missing or inconsistent archetype evidence, signed probability, or common gate provenance. Record the filename, hash and disposition. |
| `REJECT_MAKER_EVIDENCE` / `REJECT_GATE_FAILED` | Check named FIFO, markout or stress proof for maker rejection. For measured failure record the gate, value, threshold and denominator. Preserve the holdout. |
| `REJECT_UNTRUSTED_APPROVAL` / `REJECT_UNTRUSTED_OPERATOR` | Compare pinned key, release, artifact validity, live scope and root-owned operator configuration on the approved release. |
| `REJECT_INVALID_EVIDENCE` / `REJECT_RECEIPT_CONFLICT` / `REJECT_RECEIPT_IO` | Preserve original bytes. Check types, bounds, existing immutable receipt and write authorization. |
| `REJECT_DEMOTION_STORE` / `REJECT_SHADOW_DEMOTED` | Opus checks the persistent demotion store and retains any existing veto. |

A verified `admission_receipt.json` grants admission. Confirm actual activation from the verified journal's `run.admission.state=SHADOW_ACTIVE` with the same receipt hash. Only Opus starts the approved live paper session. Use the [session verification checklist](FIRST-SHADOW-SESSION-VERIFICATION-DRAFT.md) to capture the result.
