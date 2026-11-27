# 2026-11-27: the opening price comes from received raw BRTI (evaluation protocol `received-raw-opening` v1)

Status: implemented on branch `pod/opening-rule`. It is not yet bound to any sealed family. Binding
needs owner approval and a passing parity run on Apex (see "Steps" below).

## Measurement

Measured on the real tape on 2026-11-27, over 484 BTC 5m windows in two days:

- Until now the twin learned a window's opening, A(S-1), only when the published PM US series row
  arrived in `ref_prices`. That stream is polled every 10 minutes.
- First receipt minus window start: p10 143 s, p50 311 s, p90 480 s. None of the 484 arrived within
  30 s, and only 50% arrived before the 5-minute window ended.
- So fair is NaN for most of every window. The fair-value genomes, which are all 21 sealed H1–H4
  genomes, almost never quote. Their verdicts would measure the reference poller, not the strategy.
- In the same two days, all 145,676 raw BRTI seconds have a finite receive stamp at or after their
  print time.

## The rule

The settlement definition (E18, E19) is A(t) = mean of raw BRTI over [t-59, t], at lag 0 (MAE
$0.0025 against the published series). So the opening is A(S-1) = mean(raw[S-60..S-1]).

- **Value.** The 60 raw prints are summed in ascending second order as Python floats and divided by
  60.0. This is the same arithmetic as `refprice.settlement_value`, `fair.alignment` and
  `fair_up_raw`. A test asserts `==` against `settlement_value`.
  Code: `twin/features.py:computed_opening`.
- **Known.** At decision second d, the opening is known iff every one of the 60 seconds has a
  receive stamp ≤ d. Receive stamps are PERELMAN `received` and the live poller's `tw`, the same
  stamps as the E29 gate. A missing second, a conflicting value (NaN) or a missing stamp means the
  computed opening is never known for that window.
- **`brti_delay_s` is not applied to the opening.** That delay chooses which BRTI second fair treats
  as "now". The opening is a fixed past quantity, and its availability is decided by the receive
  stamps alone. (This is an assumption read from the task wording. Adding the delay would postpone
  the opening by up to 2 s more.)
- **Whichever source is known first is used for the whole window.** The published row's known time
  is its first receipt with a usable value. A conflicting revision (None) is not usable.
  - On a tie, published is used.
  - If published comes first, the window behaves exactly as under the old protocol, including any
    later revisions.
  - If computed comes first, its value is used for the rest of the window, even after the published
    row arrives.
  - Each window row records `opening: {computed, computed_known_s, published_first_s}`.
- **Before the opening is known**, the opening is unknown: fair and `dist_to_open_usd` are NaN, and
  a fair-quoting maker does not quote.
- **Settlement labels are unchanged.** `ptb`, `settle` and `outcome` keep using the final published
  A(S-1) and A(E-1).

## Why it uses only received data

Each of the 60 inputs counts only once our own receive stamp is ≤ d. A live system holding the same
raw feed can do the same computation at the same instant. The rule never reads a print we had not
received by d. `test_no_look_ahead_raw_received_after_d_cannot_change_the_opening_at_d` shifts
every raw value received after d by $500 and asserts that the opening and the whole feature vector
at d are unchanged. Choosing "the first-known source" is also causal: at d, both known times are ≤ d
or only one is, so the choice never needs a future timestamp.

## Parity requirement

The computed opening may be used for a binding result only if it reproduces the published A(S-1)
within $0.01 on at least 99% of windows.

- **On the real tape:** `doppel-opening-parity --data TAPE --from --to` (`lab/opening_parity.py`)
  checks every 5m and 15m window in range.
  - It reads the references through the evaluator's own code path (`RefStore.window_ref`,
    `computed_opening`).
  - It reports the count, the max and p99 absolute difference in USD, the share within $0.01, the
    distribution of receipt time minus window start for each source, and which source was first.
  - It exits 1 on FAIL.
- **Inside every binding replay:** the `received-raw-opening` replay checks the same thing over its
  own replayed windows. The tolerance is part of the protocol parameters
  (`OPENING_PARITY = {tol_usd: 0.01, min_share: 0.99}`). Below 99%, or with nothing to compare,
  `replay_snapshot` refuses. That refusal covers both `result --data` and `monitor --record`.

## Protocols and binding

`twin/evaluate.py` names two protocols. Each is the full canonical replay:

| name | version | opening rule |
|---|---|---|
| `published-opening` | 1 | the old behaviour: published A(S-1) after its first receipt (still the default) |
| `received-raw-opening` | 1 | the rule above, with the parity gate |

- **What a protocol is:** its parameters (simulation config with `opening`, verdict options, flow
  delay, sequential alpha and, for the new protocol, the opening rule and parity), the sha256 of
  those parameters, and the evaluator code hash (`twin.cli.code_sha256`).
- **Binding record.** A new append-only registry record kind, `protocol`, written by
  `doppel-genome bind-protocol`, binds a protocol to a family or a genome.
  - It embeds the parameters and lists the covered genome ids explicitly, because a family can gain
    members later.
  - It is accepted only before the earliest covered evaluation window closes (H1: 2026-11-29T00:00Z).
  - A genome is bound at most once. Re-binding the same target to the same protocol is a no-op;
    anything else is refused.
- **What `verify` checks:**
  - the parameter digest recomputes;
  - the covered genomes were registered earlier;
  - the record was written before every covered window closed;
  - the covered ids match the target;
  - no genome is bound twice.
- **Enforcement for a bound genome:**
  - the evaluator runs the bound protocol, and refuses if the installed version, parameters or code
    hash differ;
  - `record_result` and `record_monitor` refuse evidence from any other protocol.
- **Unbound genomes** keep today's behaviour (default `published-opening`, or `--protocol`). Their
  evidence carries `protocol_unbound: true`.
- **The bind pins the code hash.** After binding, any change to the hashed evaluator files (the twin
  package, `genome.py`, `registry.py`, `sequential.py`, `fair.py`, `refprice.py`, `brti_import.py`,
  …) makes the evaluator refuse that family, and there is no re-bind. Bind from the exact commit
  that will evaluate.

## What changes for H1–H4

- Under `received-raw-opening`, H1–H4 can quote from the first decision after raw second S-1 is
  received (about 0.65 s after S at the E29 p50), instead of after the published row (p50 311 s).
  `test_sealed_h1_genome_quotes_once_the_60_seconds_are_received` replays the sealed H1 genome
  (`h1-maker-h1-guard30`) with the published row at S+311 s:
  - 0 contracts quoted under `published-opening`;
  - quotes under `received-raw-opening`.
- The sealed genomes, their ids and their expression hashes are unchanged; only the evaluator input
  changes.
- **Binding mid-window is a choice made with interim data partly seen.** H1 opened 2026-11-26 and
  interim runs exist (E31, E35). Those were run under the old protocol, where H1 barely quoted, so
  the new protocol's P&L for that range has not been seen. Record this on binding.

## Comparison with the old protocol

`published-opening` stays available under its own name. Its replay is identical to the previous
evaluator apart from the extra `opening` field in each window row. `doppel-twin --opening
published|received_raw` runs either rule for diagnostics. Comparing the two protocols' verdicts on
the same snapshot isolates the effect of the opening rule. Fills, fees, gates and labels are the
same in both.

## Steps (owner)

1. On Apex, run `doppel-opening-parity --data /path/to/tape --from 2026-11-25T00:00:00Z --to
   2026-11-27T00:00:00Z`. It must PASS for both horizons.
2. After approval, from the commit that will evaluate, run `doppel-genome bind-protocol --family
   h1-maker-v1 --protocol received-raw-opening` (dry run) and check the printed record. Then rerun it
   with `--execute`, before 2026-11-29T00:00Z. Repeat for H2, H3 and H4 before their windows close.
3. Run `doppel-genome verify` and `doppel-genome dna`.
