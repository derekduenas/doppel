# Sequential monitoring: e-processes and early KILL/PASS (C6)

Code: `src/doppel/strategy/sequential.py` (pure math), `Registry.monitor` / `record_monitor` /
`sequential_ledger`, CLI `doppel-genome monitor`. Tests: `tests/test_sequential.py`,
`tests/test_registry_sequential.py`.

To inspect an existing twin JSON, use `doppel-genome monitor --genome ID --results TWIN.json`.
To append a monitoring event, use `doppel-genome monitor --genome ID --data SEALED_TAPE --record`
and supply `--lab FROZEN_LAB` for genomes that use intl flow. This replays from the registered
start through the last closed UTC hour or the evaluation end, whichever comes first. The record
includes the input digest, evaluation bounds and evaluator version.

## Why

The end-of-window verdict looks once. If you re-check a bootstrap CI or a t-test after every window
and stop the first time it looks good, the false-pass rate goes far above alpha. In
`test_naive_peeking_would_inflate`, a one-sided z-test checked after every block rejects a mean-zero
strategy on more than 15% of paths at nominal 5%. An e-process can be watched after every window.
Under its null, P(sup_t E_t >= 1/alpha) <= alpha (Ville's inequality).

## The test

- **Data.** Eligible twin windows with `start >= eval start` and `end <= min(eval end, now)`, sorted by
  `(end, start)` as `twin.verdict` does. Each contributes `net_c`, its net cents; a window that did not
  trade counts as 0. Windows before or after the preregistered window never count.
- **Blocks.** The increments are means of consecutive complete blocks of 12 windows, the same block length
  as the verdict's block bootstrap. Blocks are anchored at the eval start. Because later data only
  appends, a block never changes after it forms.
- **Bound B.** By default B is the genome's physical worst-case loss per window:
  `100 * risk.max_notional_per_window` cents, times `(1 + fee coef)` for genomes that cross the spread.
  The twin caps the cash put at risk per window, and that cap never resets inside a window. For the
  sealed H1 to H4 makers this gives B = 4000c.
- **PASS e-process.** Tests H0: E[block mean | past blocks] <= 0. Wealth is
  prod(1 + lam_j * Y_j / B), with lam_j from aGRAPA on past blocks only, capped at 1/2.
  - Clipping Y at +B from above only lowers the increments, so it is always conservative.
  - Y >= -B is the validity condition, and it holds physically under the default B. If an observed
    block falls below -B, it is counted as a `bound_violation` and blocks an early pass.
- **KILL e-process.** The mirror test, H0': mean >= 0, built on -Y.
  - Clipping at -B is conservative.
  - Y <= +B is an assumption, because gains are not capped. An observed violation blocks an early kill.
- **Ville.** A process rejects when its running max reaches 1/alpha (20 at alpha 0.05). The anytime
  p-value is min(1, 1 / running max).

### What the assumptions mean

- The null is a conditional mean: the block means are treated as a martingale difference sequence.
  Correlation that a 12-window block absorbs is harmless.
- Strong persistence across blocks can make the test anti-conservative. With window-level AR(1)
  phi = 0.5, the simulated false-rejection rate is 0/400. At phi = 0.9 it rises to about 0.05.
- For a `net_c_per_contract` genome the sign of the test is the same, because the expected ratio
  sum(net) / sum(contracts) and the mean net per window have the same sign.

## Family control: e-Holm, not e-BH

- **Early pass.** An early pass needs the genome's anytime p-value to survive Holm across every genome
  registered in its family. Pending genomes count as p = 1, and killed or passed genomes still count.
- **Locking in.** A false rejection at any time implies that some true null's running max reached
  m0 / alpha. By Ville plus a union bound, P(any false early pass, ever) <= alpha. Passes can therefore
  be locked in as they happen. `test_family_e_holm_fwer_with_lock_in` checks this on a 6-genome
  global null.
- **Why not e-BH.** e-BH controls FDR on current e-values at a single stopping time. It is not valid on
  running maxima, or for the union of rejections made over time, so it is not used here.
- **Family growth.** A genome registered into the family after a pass raises m. As in the Holm ledger,
  the record stores the m it used.
- **Early kill.** An early kill is per genome at level alpha. Killing a real edge is the conservative
  error, so it needs no family correction.
- **Two separate alphas.** Sequential alpha and the end-of-window Holm alpha are separate guarantees.
  They are not jointly controlled.

## Decision rules (`sequential.decide`)

| Outcome | Requires |
|---|---|
| EARLY_KILL | kill running max >= 1/alpha and no block above +B. `kill.min_windows` is not required. |
| EARLY_PASS | pass running max >= 1/alpha, family e-Holm p <= alpha, no block below -B, and traded windows >= the sealed `kill.min_windows` |
| CONTINUE | anything else, including both processes crossing (this means the bound is wrong) |

## Registry events (append-only, hash-chained like every other line)

- **Event kinds.** `interim`, `early_pass` and `early_kill`. Each carries:
  - `as_of`
  - `params` (method, block, alpha, lambda cap, prior variance, bound)
  - `stats` (windows and blocks, e-values, running maxima, clipping and violation counts)
  - `family_holm`
  - `decision` and `reason`
  - `source` (results sha256, twin code sha256, range)
  - a note that the event is statistical only
- **When an event is allowed.**
  - Only once the eval window has opened, and `as_of` must fall inside the window.
  - Never from a DIAGNOSTIC twin run.
  - Never without a complete block.
  - Never with `as_of` earlier than the last event.
  - Only with the genome's (and the family's) first-event parameters, which are frozen.
- **Binding.** The first `early_*` event per genome is binding; later ones are kept and flagged
  non-binding, as with `result`.
- **What `verify` checks** on these lines:
  - window timing
  - the e-value threshold behind each early event
  - the binding flags
- **What stays unchanged.**
  - `ledger()` and `record_result()` (the end-of-window path) are untouched.
  - Old lines parse unchanged, and `registry/genomes.jsonl` still verifies.
  - Early events do not change genome status and do not touch the commercial hurdle (A6) or owner
    authorization.

## Power: the honest cost of a physical bound

The bet per block is capped at 1/(2B). With B = 4000c (the sealed makers), a simulated -30c/window
strategy (sd 200c) never reached 1/alpha within 288 blocks, which is 12 days of 5m windows. The same
holds for +30c and for +10c with sd 100c. With B = 400c, the median time to cross for +/-30c was about
95 blocks, about 4 days.

So for the sealed families, early stopping will in practice only trigger on enormous effects. A
tighter bound would need to be preregistered, for example as a genome or registration field (not built
here). It would then be an assumption that `bound_violations` checks on data, not a physical guarantee.
