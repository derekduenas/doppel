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

The watchdog scores `ΔLL = LL(market) − LL(candidate)` and independently
monitors +2 s and +10 s markouts. Each metric uses the existing Page–Hinkley
parameters and warmup of 10 measured windows. Missing samples stay missing.
During warmup `HEALTHY` is the phase and `log_loss_measured` explicitly reports
whether a log-loss baseline exists. A score above half the detector threshold
emits `WARN_THROTTLE_50`. The runtime halves new paper entry and quote sizes,
replaces working quote sizes through cancellation, and preserves exits.
Once any detector crosses its alarm threshold, `KILL_DEMOTE_SHADOW` latches.
The runtime cancels quotes, stops new simulated decisions, and continues
reconciling existing windows. In-flight fills follow the paper simulator's
ordinary latency rules. The projection includes both transition events.

The synthetic tape test drives the paper runtime across a scheduled 60-window
scenario, checks the first 30 windows, and verifies latching after edge loss
and restart reconciliation. That test exercises approval via a fixture
session. The signed bundle producer and its trust anchor integration are
reviewed in the companion admission PR. No deployment or actual wallet
has been verified by these fixtures.
