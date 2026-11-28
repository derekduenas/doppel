# Signed candidate probability and live paper watchdog

The V4 paper entry point requires the canonical `probability.json` file in
the signed admission bundle. The independently verified receipt binds
`probability_sha256` and the registered replay start. The model is a data-only
probability calibration. Training and its provenance remain separate from
the paper runtime.

| Field | Contract |
|---|---|
| `schema` | `doppel.shadow-probability/v1` |
| `genome_id` | The admitted genome's content hash |
| `offset_s` | An integer offset strictly inside each supported market horizon, fixed before replay |
| `fit_end_ns` | The pre-replay exclusive end of the calibration data, in Unix nanoseconds |
| `fit_sha256` | SHA-256 of the independently reviewed development fit receipt |
| `intercept`, `weights` | Finite logistic coefficients over the supported receipt-time features |

Use sorted-key canonical JSON, optionally ending in a single newline. Supported
inputs are `fair_up`, `mid_up_c`, `spread_c`, `depth_top_up`,
`depth_top_down`, `brti_move_1s`, `brti_move_5s`, `brti_move_30s`, and
`dist_to_open_usd`. Include only nonzero weights and use at least one.
The reviewer verifies the calibration data's actual end, feature definition,
and independent holdout before signing. Prediction is `sigmoid(intercept + Σ w*x)`.

The forecast uses the latest open, two-sided PM US WS book received strictly
before the scheduled decision. The book expires after three seconds and a later
feed gap invalidates it. Bound reference features use the preceding whole
second. A missing model input, stale book, late window, halted session, or
catch-up past the market end records an `UNMEASURED` forecast. The runtime
fsyncs a measured forecast to the authoritative hash-chained paper journal
before certifying the cycle as committed. The cycle marker records
`forecast_fsynced_ns` from the successful durability check. A check that
reaches market close aborts the cycle. Recovery discards its uncommitted tail.

`shadow_probs.jsonl` is the append-only projection of the committed journal.
The projection follows the commit and can lag it, including when a crash
requires suffix recovery. Use the cycle marker to verify pre-close durability.
A `forecast` line includes the strategy probability, comparator midpoint,
received book clock, reference cutoff, model digest and record digest.
`settlement` appends the resolved label and references the original forecast
digest. It also carries the simulated gross return less fees at the 0.02085
taker coefficient, the running paper E-value, and quantity-weighted cents per
contract at 500 ms, 2 s, 10 s and 60 s. Marks require a fresh observed midpoint
strictly inside the market for every contributing fill. Missing evidence is
null. On restart the projection is checked against the verified journal.
A missing suffix, including a torn matching line, is appended. Divergent
bytes refuse recovery.

The watchdog scores `ΔLL = LL(market) − LL(candidate)` and monitors +2 s and
+10 s markouts separately. It groups consecutive settled markets into
nonoverlapping blocks of 100 before feeding any detector. Each metric needs
all 100 valid observations for its block. An incomplete block clears that
metric's recent confirmation and Page–Hinkley count. The existing baseline
survives gaps.

The first 20 complete metric blocks establish its baseline. Standard
deviations are floored at 0.01 in log-loss units and 0.10 cents per contract
for markouts. A metric arms only if its baseline mean less 2.86 baseline
standard errors exceeds its metric's resolution floor. Weak or unresolved
baselines remain visible in the journal with `armed: false`.

The Page–Hinkley screen uses δ=0.5 and λ=8. Action also requires the most
recent 20 complete blocks to average no more than half the baseline and to
fall at least 3.5 combined standard errors below it. Measured false-demotion
rates apply to the held-out synthetic sequences. Shadow can collect E-values during warmup. A complete
38-detector-block validation horizon represents 3,800 observed markets.

A confirmed score above half the detector threshold emits
`WARN_THROTTLE_50`. The runtime halves new entry and quote sizes, replaces
working quote sizes through cancellation, and preserves exits. A confirmed
score above the full threshold latches `KILL_DEMOTE_SHADOW`. The runtime
cancels quotes, stops new simulated decisions and reconciles existing
positions. In-flight fills follow the simulator's ordinary latency rules.

## Deployment and recovery

Before opening a new v4 session, provision the operator-approved journal
root's `.shadow-demotions` directory so the paper service can read and write
it. Keep it separate from each genome's journal. Persist the first
`<genome_id>.json` demotion there before committing the watchdog event.
Moving or replacing a genome journal keeps a demoted genome blocked. Any
existing marker, including a torn file, is a durable veto until the operator
reviews it. Keep this directory across releases and journal rotation.

V4 paper source and checkpoint v4 carry new monitoring state. Preserve
earlier journals, including their matching release and checkpoints, for
audit. Start an approved v4 session on a fresh journal. Carry demotion vetoes forward when moving the
operator-approved journal root.

The 60-market runtime fixture verifies forecast durability and restart
during warmup. Separate seeded noisy tests validate the actual 100-market
blocks, and a runtime fixture verifies durable demotion and drain at a
calibrated block boundary.
