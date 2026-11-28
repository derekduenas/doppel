# Draft · First Shadow session verification

For Opus after admission. Run these read-only checks against a consistent snapshot of each genome's journal and JSONL projection. Use the deployed release to interpret the evidence. Save the commit, receipt and journal-head hashes in the session report. Report each row as `PASS`, `FAIL`, or `PENDING` with the observed denominator and any `UNMEASURED` reason.

## Startup and probability seal

| Check | Pass evidence | Fail or pending |
|---|---|---|
| Activation | Verified `run.admission.state == "SHADOW_ACTIVE"` binds the expected genome, signed probability digest, simulation and `admission_receipt.json` hash. Approval is current and the persistent demotion store is available. Fresh v4 journal for the first session. | Fail on chain or binding mismatch, approval rejection, a demotion veto, or a session in `RESEARCH_OBSERVE`. |
| Pre-close durability | For every `stage=forecast,status=SEALED`, verify `book_tw_ns < ts_ns <= observation_ns <= cycle.forecast_fsynced_ns < window_end_ns`. The book is at most 3 s old, the reference cutoff is `ts_ns - 1_000_000_000`, and `model_sha256` matches the signed model. Check both probabilities are finite and in [0,1]. Recompute `record_sha256` over the sorted-key orjson row with that field removed. | Fail on a post-cutoff witness, missing/late durability marker, digest mismatch or outcome on a forecast. Keep `UNMEASURED` rows in coverage. If every opportunity is unmeasured, the seal still needs a measured observation. |
| Outcome reconciliation | An append-only `stage=settlement` row references the original `forecast_sha256` for the same genome and window. Both probabilities match the forecast. On observed settlement, `realized_y` is 0 or 1 and `net_return_02085` is finite. The projection matches committed journal rows in order. | Fail on update-in-place, duplicates, digest divergence, or settlement without a prior committed forecast. A lagging projection suffix remains pending until reconciled. Unresolved outcomes remain null and visible. |

Authenticate pre-close timing from the journal's committed cycle marker. A projection write can occur after the journal commits, so the file modification time cannot establish the forecast seal. Ignore any journal tail beyond its last commit when counting observations.

## E-value and markout telemetry

| Check | Pass evidence | Fail or pending |
|---|---|---|
| E-value | For a single asset and horizon, the first complete 12-window epoch produces finite `shadow_monitor.e_value.e_n` and `n_blocks=1`, with threshold 20. Compare to the settled row's `e_value_running`; follow the block and `through_s` counters across later settlements. A fully observed no-trade window contributes zero. | Pending with `awaiting_complete_block` until enough observations. Treat gaps, late data, unresolved settlement, halt, risk bound violations and incomplete maker trade coverage as unmeasured with the journal's reason. Fail on a value fabricated across one of these conditions, a missing field, or a disagreement with the projection. Crossing 20 is a separate result from telemetry verification. |
| +2 s and +10 s marks | Verify at least one qualifying paper-fill window has finite `markout_2s` and `markout_10s`. Spot-check from received midpoints and fill receipts: cents per contract, signed from the fill side, weighted by quantity. Each contributing midpoint must be fresh, two-sided, and strictly inside the market at its horizon. Report usable settles and exclusions for both horizons. | No fills or insufficient marks are pending evidence with null values. Fail on synthetic zeros, future or stale mids, or averaging away an unmeasured fill. A genuine negative mark is still a measurement and belongs in the report. |

The signed simulation settings must match the paper startup flags. On this release the default canonical simulation binds `--markouts-s 1 5 30`. The Shadow probability settlement separately computes `markout_2s` and `markout_10s` from the paper fills and books. Use those projection fields and their authoritative journal rows for this check. If a future bound protocol measures 2 and 10 directly, also check the epoch's `paper_markouts.by_horizon_c` fields. Use the epoch's exact simulator `net_c` for the E-value and the projection's `net_return_02085` for promo-fee telemetry.

## Watchdog cadence and state

1. **Verify block accounting.** In `settle.shadow_monitor.decay_watchdog`, require `markets_per_block == 100`; `markets_in_block` advances through 0–99 in scheduled forecast order. Check at least one 99 → 0 boundary. Only a closed block can advance a metric or emit a transition. Multiple previously pending settlements can drain in one cycle. Follow their forecast order before treating a counter jump as a failure.
2. **Verify completeness by metric.** Track log-loss skill `LL(mid) − LL(p_hat)`, +2 s and +10 s independently. Each needs 100 valid observations to feed a block mean. A missing metric observation invalidates that metric's block and clears its recent confirmation and detector count. Its established baseline is preserved. Fail if partial blocks drive state changes or missing observations disappear from the denominator.
3. **Record arm state separately from health.** The first 20 complete blocks per metric establish a baseline, with its mean less 2.86 standard errors required to exceed the metric's floor. Until then record `PENDING · WARMUP` and valid block counts. A sealed baseline below its floor leaves `armed: false`; record `UNMEASURED · INSUFFICIENT EDGE` for that metric. Early `HEALTHY` alone passes neither arming nor decay detection. The 60-market end-to-end fixture proves early pipeline and recovery behavior. The separate noisy validation uses 100-market blocks and shows limited detection for weak edges.
4. **Verify state transitions if observed.** Match `shadow_watchdog` journal events to projection `stage=watchdog` rows with the same window, metrics, and state. `WARN_THROTTLE_50` must halve new paper size and preserve exits. `KILL_DEMOTE_SHADOW` must stop new simulated decisions, cancel quotes, drain existing positions, and create the persistent `.shadow-demotions/<genome_id>.json` marker before the kill cycle commits. Verify that marker's receipt binding and retain it. Report a live transition as pending if no decline has occurred. The validation horizon of 38 detector blocks represents 3,800 markets.

## Read-only inspection

```sh
doppel-paper verify --journal "$SNAPSHOT_JOURNAL"
jq -c 'select(.stage == "forecast" or .stage == "settlement" or .stage == "watchdog")
       | {window_id, genome_id, stage, status, state, p_hat, p_mkt_mid, forecast_sha256,
          realized_y, e_value_running, markout_2s, markout_10s}' \
  "$SNAPSHOT_JOURNAL/shadow_probs.jsonl"
uv run python - "$SNAPSHOT_JOURNAL" <<'PY'
import sys
from pathlib import Path

import orjson
from doppel.paper.journal import COMMITS, iter_records

pending = []
for row in iter_records(Path(sys.argv[1]), allow_torn_tail=True):
    pending.append(row)
    if row["k"] in COMMITS:
        for record in pending:
            if record["k"] == "run":
                print(orjson.dumps(record["admission"]).decode())
            elif record["k"] == "settle" and "shadow_monitor" in record:
                print(orjson.dumps({"window_id": record["slug"], **record["shadow_monitor"]}).decode())
            elif record["k"] == "shadow_probability" and record["row"]["stage"] == "forecast":
                print(orjson.dumps({"forecast": record["row"],
                                    "forecast_fsynced_ns": row.get("forecast_fsynced_ns")}).decode())
        pending.clear()
PY
```

Close the first session report with the receipt hash, last committed journal head and time, measured forecasts/eligible opportunities, resolved settlements, E-value and block count, +2 s/+10 s sample counts and means, and each watchdog metric's complete block count, arm state and last transition. Record sample insufficiency separately from integrity failures.

[Signed probability contract](../SHADOW-PROBABILITY.md) · [Epoch telemetry and noisy validation](../SHADOW-EPOCH-MONITOR.md) · [Admission runbook](FIRST-WALLET-ADMISSION-DRAFT.md)
