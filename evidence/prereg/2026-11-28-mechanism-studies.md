# Preregistration: mechanism studies v2 (frozen 2026-11-28, before any run on real tape)

Code: branch `pod/markout-v2` at 9f93dac (`doppel-mechanisms`), definitions in its `docs/MECHANISMS.md`.
No study output on real tape has been read before this file was committed.

| Sample | Period (UTC) | Role |
|---|---|---|
| Exploratory | 2026-11-25 00:00 – 2026-11-28 00:00 | Look, refine definitions if needed. Nothing here counts as evidence. |
| Confirmation | 2026-11-28 00:00 – 2026-12-05 00:00 | Binding. Run once, after it closes, with the code SHA used for the exploratory run, or a later SHA whose diff is recorded here *before* the confirmation run. |

Venue and scope: PM US BTC 5m and 15m; four studies `flb`, `final60`, `overreaction`, `opening`; grid sizes as the code reports
(3, 12, 3, 2). Bootstrap 15-minute clock blocks, `--boot 2000 --seed 20261128`.

An effect **counts** only if, on the confirmation sample, its Bonferroni-corrected 95% CI excludes 0 in the stated direction:
- `flb`: slope of (100y − mid) on (mid − 50) ≠ 0 (direction stated from the exploratory run, recorded here before confirmation).
- `final60`: net P&L > 0 under **`api_live_rebated`** at 100 contracts with fill rate ≥ 50%; also reported under the other three tiers.
- `overreaction`: signed mid move at +60 s < 0 (fade) or > 0 (follow) for a trigger, direction fixed from exploration.
- `opening`: dm(+60 s) along sign(mid − fair-v1) > 0.
Fee tiers are reported side by side; a study surviving only under `maker_0` is a maker hypothesis, not a taker edge.
A surviving effect is a candidate for a sealed genome via the registry, never a trade.
