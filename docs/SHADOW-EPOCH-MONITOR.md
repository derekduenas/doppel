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
