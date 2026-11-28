# Sync note for the Codex sprint (DOPPEL v2 sprint spec, PR-1 … PR-6) — 2026-11-28

From the Claude lead. Purpose: avoid building the same thing twice and avoid breaking the scoring freeze.
All branches below are pushed to origin and apex; none is merged yet. Rebase on them instead of re-implementing.

## Already built (use these)

| Sprint item | Branch @ SHA | State |
|---|---|---|
| PR-3 time-sliced order matcher | `pod/ordermatch-sliced` @ 9eac081 | Byte-identical to whole-span at 60/97/600/10000 s slices; `--slice-s` (default 600) in ordermatch and answerkey; 60 tests. Follow-ups: orderreplay `quotes`/`kept` memory; rulefit `Fair.feat` cache (frozen path). Layer 5 exam (3 seeds) now running on it on Apex, 2 GB cap. |
| PR-4 fee tiers, L2 book walk, overreaction fix | `pod/markout-v2` @ 9f93dac (on `pod/markout` ef51c36) | Four tiers side by side: `gateway_0095`, `api_base_0695`, `api_live_rebated` (0.02085 effective), `maker_0`; exact book walk 25/100/500 with latency miss rule; overreaction masks first 45 s, z = Δln B / (σ√τ) from pre-move data; separate `opening` study. 29 tests, 0/20 null false effects per study. Exploratory run on Nov 25–27 tape in progress; **preregistration on main: `evidence/prereg/2026-11-28-mechanism-studies.md` (binding confirmation sample Nov 28–Dec 4).** |
| PR-5 cohort v2 snipers + donors | `pod/cohort-v2` @ d1eb412 | Ranks 101–2,500, ≥50 windows, $50–$5,000 median notional; +2/+10/+60 s gross markout, window-clustered Newey–West t, top-3 winsorized; floors t10 > 1 (snipers), t2,t10 < −1 + split-half sign (donors); 50 seeded controls; winner's-curse null count recorded; Period B unreadable by code (tested). **Freeze scheduled on Apex 2026-11-28 01:20 UTC with `--freeze-at 2026-11-28T00:00:00Z`.** Log-loss/Brier deliberately omitted: a fill only bounds the wallet's belief, it is not a probability (see `pod/inversion`). Hourly/daily crypto not included. |
| PR-5 batched absorption | `pod/absorb-scale` @ 0d0f7c1 | Resumable cohort runner (running on Apex for cohort v1). Goldsky Edge RPC needs a separate dashboard key (our two keys return 401/403); owner adding `GOLDSKY_EDGE_KEY` to /etc/doppel/goldsky.env. Never put the key in a URL that gets logged — use a local relay. |
| PR-6 paper runtime | `pod/paper` @ 968e49f | Full suite 871 passed on Apex. |
| PR-6 decay watchdog | `pod/markout` `lab/skilldecay.py` | Page–Hinkley on ΔLL; 0/200 false alarms, decay flagged within ≤38 windows. `watchdog_update(state, dll)` ready to call. |
| PR-6 promotion statistics | `pod/promotion` @ e918da2 | DSR, PBO (CSCV), BH/Holm, Newey–West vs block bootstrap, ghost + disagreement audit. 834 passed. |
| PR-2 BRTI cutover | `pod/brti-absorb` @ 2c95e85 | PERELMAN recorder inside DOPPEL, byte-compatible, parity tool, phased shadow → cutover plan; `--without hl,bn` default. 828 passed. |
| PR-2 Asian-TWAP kernel | already `fair-v1` (`lab/fair.py`, E19) | Accrued 60 s mean + shrinking remaining variance. |
| — | `pod/perps-feeds` @ a71edbf (Kalshi BTCPERP, Coinbase Derivatives), `pod/inversion` @ 648082f, `pod/simulator` @ 44a3dd2, `pod/system-design` @ e9feab2 | Built; suites passed (inversion/markout pending). |

## Conflicts with the sprint spec (owner decisions, do not proceed without them)

1. **Scoring freeze until H4 is scored (Dec 1 00:00 UTC).** PR-6 targets `strategy/sequential.py`, `twin/verdict.py`, `twin/engine.py` — all frozen (CLAUDE.md). Evaluator hash must stay `c2d01f50…3d25e4`. Do PR-6's statistics as new modules or wait until after Dec 1.
2. **"No telegram"** is a standing owner rule. PR-1 alerting must not use Telegram.
3. **Offshore feeds (Binance, Hyperliquid).** Recording their public data was an open owner decision (lead recommended off; DOPPEL's BRTI copy defaults `--without hl,bn`). PR-1 `doppel-taped-lead` turns them on — confirm with the owner first.
4. **"Never run research on Apex" / `Forge-1`.** Today the owner asked for heavy work on Apex. There is no Forge-1 host yet (not in ssh config or DNS). Until it exists, research stays on Apex under caps (OOM score 800, MemoryMax, Nice 19) with a stall/memory watchdog; the recorders are protected first.
5. **R2/B2 object store** needs an owner account and credentials; none exist on Apex. Cost decision for the owner.
6. **GitHub Actions minutes** on a private repo are billed beyond the free quota; fine, but owner should know.

## Coordination

- Both agents push to the same repos. Fetch first, never force-push `main`, never rewrite another agent's branch.
- Only the lead deploys, via `ops/deploy.sh` from `main`.
- Running on Apex now (do not stop): Layer 5 exam chain (`doppel-exam-chain`), cohort v1 absorption (`doppel-absorb-nov26`), mechanism studies (`doppel-mech`), cohort v2 freeze timer, CI checkouts under `/var/tmp/doppel-ci`.

## Storage (added 2026-11-28 00:40 UTC)

R2 is live (bucket `doppel-tape`, config `/etc/doppel/rclone.conf`, use `/usr/local/bin/rclone-r2`; apt rclone 1.60 fails with
`NotImplemented`, and `acl = private` must not be set). Where data lives is now defined by `ops/storage-policy.toml`, with
`ops/storage.py copy|audit` and `doppel-storage.timer` (hourly :20) installed on Apex from main 09c22dc (`docs/STORAGE.md`).
For PR-1: make `archive_tape.py`/`tape_janitor.py` read that policy instead of their own paths, and have the janitor prune
only hours listed in `verified_paths` of `/var/tmp/doppel-storage-audit.json` — do not add a second uploader.

## Division of work acknowledged (2026-11-28 ~00:50 UTC)

- Codex: shadow-admission gate and Layer 5b on their own branches. Claude lead: merges, deploys, live jobs, shadow activation.
- Shadow admission: build as new modules; `twin/*`, `strategy/{genome,registry,sequential}.py` stay untouched until H4 is scored (Dec 1 00:00 UTC). Thresholds as in SPRINT-V3 §3 WS5.
- Layer 5b: do not reuse seeds 271031/271067/271091 or read `/var/tmp/doppel-exam/*` (Layer 5 is preregistered and running); use fresh seeds and commit their prereg before running. Nothing may read Nov 28–Dec 4 PM US tape for study design (confirmation week).
- PR #1: next in the review queue. When it merges, its `archive_tape.py` (with proofs) owns uploads of `tape/` and `tape-rust/`; `ops/storage.py` then stops copying those two classes and keeps the other classes plus the audit. One uploader per class.

## Integration review of v3-B (2026-11-28 ~02:00 UTC, PR #2)

Independent review: no critical findings (no order path, no secret leak, no scored-code change, no look-ahead, admission fails closed).
Fixed in PR #2: `paper/`, `brti-stream/` classified (R2) and `perelman-relay/` do-only, so an unclassified folder can no longer block retention.
Handled at deploy (lead): install the policy-bound `doppel-janitor`, `doppel-storage`, `doppel-archive` units together and run the janitor
from the same release copy that writes the audit (no version skew between `/opt/doppel/ops` and `/opt/doppel-storage`).
Open for Codex (do not merge without lead review):
1. Shadow gate requires a recorded Layer 5b scorecard with Binance + Hyperliquid receipts and `taker_timing` for every genome
   (makers too). That makes shadow unreachable while D3 is off. Proposal: require `taker_timing` only for taker genomes; allow a
   fair-only Layer 5b plan without offshore receipts; update fixtures accordingly. Owner decision D3 decides the lead family.
2. `cold_tape.py` offload walks all history; hours pruned by the old floor janitor without proof fail every run and mask real
   failures. Record a one-time historical-missing inventory or bound with `--from`.
3. No producer yet for `doppel.shadow-{replay,trial-plan,trial-ledger,gates}/v1` from `lab/promotion.py` + twin replay.
4. Renewed/expired approval strands a shadow journal (fixed receipt path); allow settle-only resume.
5. `tape_alerts.py` resends every 15 s while Discord fails (no backoff); unit hard-codes research unit names.
6. **Do not stop live Apex jobs.** At 00:00:32 UTC a root session stopped the cohort absorption, mechanism studies, the cohort v2
   freeze timer and `doppel-vsmarket.timer`; the lead restored them. Live jobs belong to the lead (SPRINT-V3 §1).

## PR #6 `doppel-exec` canary OMS: parked (lead, 2026-11-28)
- State: stays **draft**. CI green (run 46165425441), no frozen or ops files touched, and live mode is gated behind `--mode canary-live`, a signed approval, owner-only key files and a root-owned TOML.
- Why parked: the owner's code freeze on new features is in effect until first shadow admission. Zero genomes are admitted, so nothing can feed this OMS yet.
- Merge conditions:
  1. At least one genome clears `admit_shadow()`.
  2. The freeze is lifted by the owner.
  3. A full lead review is done, covering netting, reservation/reconciliation, the Kalshi mass-cancel path and the Kelly sizing on `epistemic_var`.
  4. The owner decides how the canary approval relates to the standing rule "no orders unless the owner arms riskd". Either the approval key is issued only by riskd arming, or the owner explicitly accepts it as the arming mechanism.
- No further work on this branch until then. The publisher bridge (variance, targets, shadow price) is post-admission work too.
