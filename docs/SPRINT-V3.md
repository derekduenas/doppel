# DOPPEL Sprint v3 — prove the machine, find the first edge, reach shadow (2026-11-28 → 2026-12-05)

Supersedes the "DOPPEL v2 sprint spec (PR-1 … PR-6)". Authoritative for both agents (Claude lead, Codex).
It keeps that spec's goals and fixes its conflicts with the scoring freeze, the owner's standing rules,
and work already built (see `docs/handoffs/SYNC-CODEX-SPRINT-20261128.md`).

## 0. What "done" means this sprint

By **2026-12-05 00:00 UTC**, one report (§9) answers four questions with numbers, each against a
threshold written down *before* the number was seen:

1. **Does the machine work?** Layer 5 exam: can DOPPEL recover scripted strategies it was never told, and abstain on ones it can't?
2. **Is there a mechanism edge?** The four preregistered studies on the confirmation week (Nov 28–Dec 4), after real fees and real depth.
3. **Which wallets are worth reconstructing?** Cohort v1 + v2 scored forward (Period B), against controls and the winner's-curse null.
4. **Is anything ready for shadow?** At most three genomes enter live paper/shadow, each through the promotion gate.

"None" is an acceptable answer to 2–4. A false "yes" is not. The sprint fails only if a question is left unanswered or answered without its preregistered test.

## 1. Rules (non-negotiable)

**Evidence**
- **Scoring freeze until H4 is scored, 2026-12-01 00:00 UTC.** No edits to files under `twin.cli.code_sha256()` (list in CLAUDE.md); hash must stay `c2d01f50…3d25e4`. Work that needs them is built as new modules now and wired after Dec 1.
- **No look-ahead:** every decision, label and feature uses data received by its own time (`tw`).
- **Preregister before reading.** Every test that can promote anything has its sample, metric, threshold and grid size committed under `evidence/prereg/` *before* its data is read. Exploration is allowed and labelled; it never counts.
- **Count every trial.** Every variant tried goes into the study's trial ledger; the promotion gate refuses to run without `n_trials` (DSR, PBO and BH all use it).
- **Sealed partitions:** exploratory / confirmation / forward; a sample used to choose something is never used to judge it.
- **Fees and depth are real:** every net number is reported at four fee tiers side by side (`gateway_0095`, `api_base_0695`, `api_live_rebated` = 0.02085 effective to 2026-12-31, `maker_0`) and at book-walk sizes 25/100/500 with fill rate. An edge that only survives at `maker_0` is a maker hypothesis, not a taker edge.
- **The goal is the wallet's edge, not a 100% copy.** Fidelity is measured (agreement, timing delta, disagreement P&L, ghost), but promotion is decided by after-cost, out-of-sample P&L.

**Safety and owner rules**
- No orders, ever, until the owner arms `riskd`. No credentials in code, URLs that get logged, or chat. Never use `/etc/credstore.encrypted/pmus-riskd-retail-trading-key.cred`.
- No Telegram. Goldsky spend stays inside the free $100 (self-cap 7M rows; Edge RPC at $5/M requests counts against it).
- Venues and feeds: trade only lawful US venues (PM US, Kalshi). Recording offshore *public* market data (Binance, Hyperliquid) as signals is **owner decision D3**; until decided it stays off.

**Compute and operations**
- **Recorders first.** Apex's job is recording (and later execution). Research on Apex is allowed only until the research host exists (D1), and only under: `Nice=19`, `IOSchedulingClass=idle`, `MemoryMax` per job, `OOMScoreAdjust=800`, one heavy job at a time, and the watchdog (`/var/tmp/doppel-watch.sh`: stall > 40 %, free memory < 600 MB, recorder down).
- **Every long job is watched, not awaited:** progress metric (CPU seconds, rows, windows) checked at least hourly; a job without progress for 30 min is diagnosed, not left running.
- **CI:** full suites run in GitHub Actions (matrix, per-job `--basetemp`, no shared ports/paths) and must be green before merge. Until CI is green, suites run serially on the research host or Apex via `apex-test.sh` (flock, private basetemp).

**Two agents, one repo**
- Each branch has one owner (table §3). Never push to another agent's branch, never force-push `main`, always fetch first.
- `main` changes through a merge queue (§4), each merge reviewed by the other agent or a reviewer subagent, then a full suite on `main`.
- Only the Claude lead deploys, via `ops/deploy.sh` from `main`.

## 2. Architecture this sprint completes

```
DATA ─► 0 SELECT ─► 1 ABSORB ─► 2 INSPECT ─► 3 DISSECT ─► 4 RECONSTRUCT ─► 5 PROVE ─► 6 FIDELITY ─► GATE ─► PAPER/SHADOW
tape    cohort v1/v2  ledger      order tape   decision    rulefit         answer-key  markout,     promotion   paper runtime
BRTI    + controls    (chain-     + order      table with  simulator (SBI) exam        ghost,       (DSR, PBO,  + decay
perps   + winner's-   reconciled) matching     book state  inversion       (3 seeds)   disagreement BH, HAC,    watchdog
fills   curse null                (sliced)     at takes &  (latent p_w)                audit        e-value)
                                               passes
MECHANISMS (wallet-free): flb · final60 · overreaction · opening ─► prereg confirmation ─► genome candidates ─► same GATE
```

The one missing link today: **Layer 3 does not yet emit book state at takes and passes**, so the inversion
module cannot run on real wallets (it returns NOT_IDENTIFIED and names the missing fields). WS3-B closes it.

## 3. Workstreams, owners, acceptance

Status: ✅ built on a branch (suite passed) · 🔄 running · ⬜ to build.

### WS0 Platform (owner: Codex) — `sprint/pr-1-protect-apex`
| Item | Acceptance (measured, not asserted) |
|---|---|
| ⬜ GitHub Actions CI matrix | All suites green concurrently in < 15 min; per-job basetemp; a deliberate failing test turns CI red; no job touches Apex. |
| ⬜ Alerting (no Telegram; Discord or Pushover per D2) | Fires in a drill for: disk > 80 %, any recorder stream silent > 60 s, `doppel-tape` restart, research job stall > 40 % or free memory < 600 MB. Alert text contains no secrets. |
| ⬜ Cold tape offload (needs D4 account) | `archive_tape.py --dry-run`, then one live hour: upload, re-download, sha256 equal, manifest appended; janitor prunes only verified, unprotected hours. |
| ⬜ Lead-feed recorder (`doppel-taped-lead`) | Ships **disabled**; US-lawful perps first (`pod/perps-feeds`); offshore streams enabled only after D3. |

### WS1 Data (owner: Claude)
| Item | State | Acceptance |
|---|---|---|
| US perps feeds (Kalshi BTCPERP, Coinbase Derivatives) | ✅ `pod/perps-feeds` | Merged and deployed; 24 h with 0 sequence gaps or documented reconnects; Kalshi WS 401 → REST fallback confirmed. |
| BRTI absorption from PERELMAN | ✅ `pod/brti-absorb` | Shadow parity ≥ 24 h (0 differing seconds) before cutover; cutover only after Dec 1; gap check over (W0, W1] passes. |
| Goldsky Edge RPC (D5 key) | ⬜ | Key via local relay (never in logged URLs); old-block `eth_getLogs` works; remaining cohort v1 absorbed in < 3 h; spend logged against the $100. |
| Polymarket contract-version audit | ⬜ | Every fill source (exchange contracts by version) enumerated from chain; Goldsky coverage per contract per day vs chain logs = 100 % or the gap named; the `goldsky_incomplete` wallets explained. |

### WS2 Prove the machine (owner: Claude)
| Item | State | Acceptance |
|---|---|---|
| Sliced order matcher | ✅ `pod/ordermatch-sliced` | Byte-identical to whole span (tested); real-tape peak RSS from `tool-status.json` < 1.5 GB. |
| Layer 5 exam, 3 seeds | 🔄 on Apex | **Thresholds frozen now (`evidence/prereg/2026-11-28-layer5-exam.md`, below) before any seed is read.** |
| Simulator confirmation | ✅ code / ⬜ run | Rescore dev seeds; then the single seed-7777 confirmation run, reported whatever it says. |

**Layer 5 pass thresholds (pooled over 3 seeds × 6 scripted strategies):**
- confident-claim precision ≥ 90 % (a confident claim names the right family and its parameters' CIs cover the truth);
- recovery (right family, confident) ≥ 60 % of recoverable strategies;
- abstention on the random and decoy strategies: 100 %;
- median |entry-time delta| for recovered takers ≤ 1 s.
Below any threshold → the reconstruction stack is not trusted on real wallets; WS4 fixes the named failure first.

### WS3 Find the edge (owner: Claude, studies; Codex reviews)
| Item | State | Acceptance |
|---|---|---|
| A. Mechanism studies v2 | ✅ `pod/markout-v2`; 🔄 exploratory run | Confirmation run once after Dec 5 00:00 UTC per `evidence/prereg/2026-11-28-mechanism-studies.md`; directions fixed from exploration and appended to that file before the run. |
| B. Decision table with book state at takes and passes | ⬜ (new module; may not edit frozen `lab/flow.py`, `dossier.py`) | For each real taker fill and each matched pass: bid/ask/depth L1–L3, fair-v1, tte, inventory, received by `tw`. Inversion runs on ≥ 2 real wallets end to end. |
| C. Cohort v2 freeze | 🔄 01:20 UTC timer | File written once; sha256 recorded in this doc; `observed` vs `expected_null` reported per floor. |
| D. Forward scoring (Period B) | ⬜ | Daily: each slice vs controls, gross markout t and after-fee settlement P&L, BH across slices; nothing selected by Period B is judged by Period B. |
| E. Hourly/daily crypto + Kalshi extension | ⬜ after a BTC result | Only once a BTC 5m/15m study or wallet survives confirmation. |

### WS4 Reconstruct (owner: Claude)
| Item | State | Acceptance |
|---|---|---|
| Inversion (latent p_w) | ✅ `pod/inversion` | Answer key passed (β recovered, random/pure-fair abstain). Next: real wallets via WS3-B; a capital cap correlated with a feature added to the answer key. |
| Simulator (SBI) for makers | ✅ `pod/simulator` | Confirmation run (WS2). |
| Fusion | ⬜ | For a wallet, rulefit, simulator and inversion either agree within CIs or the disagreement is reported; a genome is proposed only from agreeing evidence. |

### WS5 Gate and shadow (owner: Claude builds; Codex reviews)
| Item | State | Acceptance |
|---|---|---|
| Promotion gate | ✅ `pod/promotion` | Merged; refuses without `n_trials`. |
| E-value governor | ⬜ new module now; wire into `sequential.py` after Dec 1 | 500 zero-edge seeds peeked every epoch: false promotion ≤ 5 %. |
| Decay watchdog | ✅ `lab/skilldecay.py` | Wired into paper: states `HEALTHY → WARN_THROTTLE_50 → KILL_DEMOTE_SHADOW`; 0 false kills on steady streams, kill ≤ 38 windows after decay (synthetic). |
| Paper runtime | ✅ `pod/paper` | Merged; 2 h live trial on Apex with the relay; journal hash chain verifies; twin agreement on the same windows. |
| **Shadow entry gate** (all required) | — | Promotion gate PROMOTE (DSR ≥ 0.95, PBO < 0.15, BH survivor, HAC and bootstrap p ≤ 0.05) **and** `api_live_rebated` net > 0 at a size with fill rate ≥ 50 % **and** markout +2 s/+10 s ≥ 0 (no adverse mirage) **and** replay-vs-paper slippage ≤ 0.3 ¢ **and** residual correlation < 0.30 with any active genome. |
| Live canary (needs owner arming riskd) | not this sprint | E_n ≥ 20 and epoch-clustered t_HAC ≥ 2.2 over ≥ 40 shadow windows, plus all shadow gates still holding. |

### WS6 Execution — not this sprint
Live adapter, OMS, reconciliation, riskd arming: only after a genome holds shadow gates for a week.

## 4. Merge queue (each: review → merge → full suite on main → deploy only if it changes Apex)

1. `pod/promotion` 2. `pod/absorb-scale` 3. `pod/ordermatch-sliced` 4. `pod/perps-feeds` (deploy; streams per D3)
5. `pod/paper` 6. `pod/markout` + `pod/markout-v2` 7. `pod/inversion` 8. `pod/simulator` 9. `pod/cohort-v2`
10. `sprint/pr-1-protect-apex` (Codex; after D2–D4) 11. `pod/brti-absorb` with its cutover, **after Dec 1**
12. Post-freeze wiring (e-value into `sequential.py`, decay into verdicts), **after H4 is scored**.

## 5. Calendar (UTC)

| Day | Date | Must happen |
|---|---|---|
| 1 | Nov 28 | Cohort v2 frozen 01:20. Exam seeds running. Exploratory studies read. Merges 1–4. Layer 5 thresholds committed (done in this file). |
| 2 | Nov 29 | **00:00 H1 closes → score H1.** Merges 5–7. WS3-B decision table. Goldsky Edge absorption (if key). |
| 3 | Nov 30 | **00:00 H2, H3 close → score.** Exam results vs thresholds. Paper 2 h live trial. Period B day 2. |
| 4 | Dec 1 | **00:00 H4 closes → score; freeze lifts.** BRTI shadow → cutover. Post-freeze wiring. Inversion on real wallets. |
| 5 | Dec 2–4 | Forward scoring daily; fusion; up to 3 candidates through the shadow gate. Dec 4 03:30 selection checkpoint. |
| — | Dec 5 00:00 | Confirmation week closes → run the four studies once → report (§9). |

## 6. Kill and stop rules

- Exam below threshold → no real-wallet reconstruction claims; fix the named failure first.
- A study with no confirmation effect at `api_live_rebated` is closed; it is not re-run with new parameters on the same week.
- A cohort slice whose `observed` ≤ 1.5× `expected_null` and whose Period-B t is ≤ 0 is treated as noise.
- A shadow genome is demoted on `KILL_DEMOTE_SHADOW`, on any after-fee loss beyond its preregistered limit, or on twin–paper disagreement > its bound.
- If recorders lose data (gap without receipt), all research on Apex stops until the cause is fixed.

## 7. Risks

| Risk | Mitigation |
|---|---|
| ~2 days of Period A → winner's curse in cohort v2 | t floors, null count, forward-only judging. |
| PM US rebate ends Dec 31 | Report `api_base_0695` beside every rebated number; nothing promoted on rebate alone without a post-Dec-31 plan. |
| Single host | D1 research host; alerting; recorders protected by OOM scores. |
| Two agents colliding | Branch ownership, merge queue, sync note. |
| Fee schedule uncertainty (gateway 0.095 vs docs 0.0695) | Four tiers; reconcile against a real fill statement before any live order. |

## 8. Owner decisions

| ID | Decision | Default if undecided |
|---|---|---|
| D1 | Research host (~$48–96/mo, 8–16 GB) | Research stays on Apex under caps |
| D2 | Alert channel: Discord or Pushover | Alerts written to journal only |
| D3 | Record Binance / Hyperliquid public data as signals | Off |
| D4 | Cold storage account (R2 or B2) | Local janitor with protected days; volume resize |
| D5 | Goldsky Edge RPC key | Free public RPC (slow) |

## 9. End-of-sprint report (one page, numbers only, each with its preregistered threshold)

1. H1–H4 scores under the bound protocol.
2. Layer 5: per-seed and pooled precision, recovery, abstention, timing delta → PASS/FAIL.
3. Studies (confirmation week): effect, CI, fill rate per size, net per fee tier → effect / no effect.
4. Cohorts: per slice, observed vs null, Period-B gross markout t and after-fee P&L vs controls; top 10 wallets with reconstructability (inversion/simulator verdicts).
5. Shadow: candidates, each gate's value, admitted or not.
6. Platform: recorder uptime and gaps, CI status, alerts fired, Goldsky spend.
