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
  One asset and horizon are supported. Maker e-values require the offline
  matcher's feed-volume coverage evidence, which the live runtime lacks.
- Paper markouts use the twin's fill-side sign, fill price, and the latest
  received two-sided midpoint at each requested horizon. The quantity-weighted
  average at +2 s or +10 s requires complete in-market observations for every
  paper fill in the window. A horizon at or after market close lacks a market
  midpoint. Supply `--markouts-s 2 10` with the matching signed simulation
  settings to measure both horizons.
- The decay watchdog reports `UNMEASURED`. Its Page–Hinkley statistic requires
  a sealed strategy forecast and simultaneous received market mid at a fixed
  scoring opportunity, plus the observed outcome. Shared fair-value features
  and order intents cannot supply a strategy forecast. A future integration
  needs these rows for the registered opportunity set and a preregistered
  throttle threshold and position-sizing rule.

`e_n` measures simulated return. Its statistical interpretation assumes a
nonpositive conditional mean under the null at the block level. The journal's
E-value, markouts, and watchdog status are telemetry. Existing admission
expiry, revocation, and paper risk limits govern the session.
