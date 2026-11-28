# Shadow epoch measurements

An admitted paper session writes `shadow_monitor` on each settled window. The
verified journal already binds that session to its admission receipt and
simulation settings. Read measurements from committed cycles. The paper runtime
regenerates them during journal recovery.

- The positive e-process uses the frozen aGRAPA betting parameters and exact
  simulator `net_c`, including zero for a fully observed window with no trades.
  It starts at the journal's first scheduled market and uses complete 12-window
  blocks, ordered by scheduled start. A gap, late window data, unresolved
  settlement, halt or bound violation makes the series `UNMEASURED`. A later
  outcome arriving before an earlier one waits for that earlier settlement.
  One asset and horizon are supported. The maker path additionally requires
  fresh native venue volume counters bracketing the window and exact
  reconciliation to received trade prints. Feed gaps, counter resets, inferred
  prints and unexplained counter changes leave the maker E-value unmeasured.
- Paper markouts use the twin's fill-side sign, fill price, and the latest
  received two-sided midpoint at each requested horizon. The quantity-weighted
  average at +2 s or +10 s requires complete in-market observations for every
  paper fill in the window. A horizon at or after market close lacks a market
  midpoint. Supply `--markouts-s 2 10` with the matching signed simulation
  settings to measure both horizons.
- An approved session also emits `shadow_probs.jsonl`. The scoring opportunity
  and fitted coefficients are fixed by the signed candidate probability
  artifact. Closing outcomes append separate reconciliation records. The
  in-process detector consumes the same reconciliation records in forecast
  order. [Probability and watchdog operator contract](SHADOW-PROBABILITY.md)

`e_n` measures simulated return. Its statistical interpretation assumes a
nonpositive conditional mean under the null at the block level. The E-value
is telemetry. Watchdog transitions can reduce paper order size and permanently
halt the session. Existing admission expiry, revocation, and paper risk limits
also govern the session.

## Measured behaviour and limits (lead verification, 2026-11-28, 200 seeds per case)

Simulated ΔLL streams fed through `paper.watchdog.DecayBlocks` exactly as wired (100 markets per block, 20 baseline blocks, 20 confirm blocks):

| truth vs mid | steady edge: false demotions | edge lost at block 30: demoted |
|---|---|---|
| 0.70 vs 0.50 | 0 / 200 | 200 / 200, median block 39 (~1,000 markets after loss) |
| 0.80 vs 0.60 | 0 / 200 | 200 / 200, median block 39 |
| 0.60 vs 0.50 | 0 / 200 | 73 / 200 (the sensor never arms on 127 seeds) |
| 0.55 vs 0.50 | 0 / 200 | 0 / 200 (never arms) |

Limits: the sensor needs 2,000 markets (~7 days of 5-minute markets) before it can arm, and it arms only when the baseline edge is
clearly above its floor. **A small-edge genome is not protected by the ΔLL sensor**; its protection is the after-fee P&L limit and the
e-value governor. Detection after a real loss takes ~10 blocks (~3.5 days on 5-minute markets).
