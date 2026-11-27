# Rule-twin build review · November 27, 2026 UTC

**Stage 5 in the draft v3 blueprint is open.** DOPPEL has a scripted-wallet lab, a rule
fitter, a fidelity evaluator and an absorption ledger. There is no passing real-book
calibration or reconciled end-to-end real twin. Stage 1 has usable source evidence for a
first wallet. Stage 0 persistence, Stage 6 independent forward agreement, and the paper
and live gates remain open. The earlier M0–M6 board still describes the infrastructure
and strategy workstreams. The overall build is at **M2 strategy validation** with open M0
recording coverage and M1 wallet-evidence requirements. M0 still needs an independently
measured 24 hours at ≥99.5% identity-transaction coverage. M1 has 42 reconciled wallets
out of 43 comparable wallets against a minimum sample of 50. M2 still needs admissible
tape and prospective after-fee results. [Rule-twin blueprint](BLUEPRINT-V3.md) · [Milestone board](BUILD-STATUS-20261126.md)

## What was recovered

The handoff started with main at `3cff434` and the shared system-audit branch at
`7cce7ac`. Since the November 26 audit, that branch added protected-day cleanup,
bounded cleanup per invocation, receipt retry changes, a 20-second restart check,
on-disk order-tape completeness, a paired P&L check for fidelity and the v3 draft.
The published answer-key and absorption branches were `3dee8bb` and `4f36d23`.
The Mac's main checkout was clean. A prior work-in-progress stash remains in its Git history.
The original fidelity (`304f01d`), reconstruction (`7f05f38`) and rule-fitting
(`bd1a4fe`) pod commits are all ancestors of the combined review revision.
At main `3cff434`, those three commits still await promotion.

Claude's interrupted calibration branch had merged the answer-key and system-audit branches
at `7d33a53`. Six modified source and test files were still local, with 1,157 insertions and
133 deletions. Their exact Git patch has SHA-256
`5726ddaefb653347ea6bb407485fc50267ce9a05f47668a60f28327205f5a510`. Checkpoint
`655b219` on `review/calibration-recovery-20261127` preserves the edits on both mirrors.
Claude's original working files remain in its calibration worktree.

## Decisions on the three steps

| Step | Verified state and remaining gate |
| --- | --- |
| **1. Calibrate rule recovery** | The repaired fitter requires sustained, provable overlap for ladder levels and evidence for side, timing and price before emitting a draft. Sparse or insufficient samples can return unknown. The scorecard honors confidence bounds and distinguishes a completed negative result from a tool failure. Training uses separate order matches and fills observed before the split. Held-out fidelity still needs enough identifiable maker orders and independent windows. |
| **2. Exercise the lab on actual books** | The original Apex run at `3dee8bb` started at 01:44:40 with zero mapped pairs in its training and holdout periods. The owner chose to leave it running. The replacement lab-only map has 36/36 resolved and book-confirmed pairs in each half. All six source files passed checksum, frame and CRC checks. Across 72 windows, 284 of 288 sampled as-of fair values were available. No formal exam outcome has been recorded. [Apex input checks](../evidence/2026-11-27-answerkey-inputs/README.md) |
| **3. Reconcile a real wallet** | The source tape contains 619,425 rows on 704 pages for 150 frozen-cohort addresses at the same fixed end time. Recovered cursor chains are complete for 148, including 15,982 rows for the proposed maker `0x0cb0…`. Two histories remain partial. No new cohort purchase is needed to pilot that maker over this interval. Independent cash, share and transaction reconciliation on the real wallet is still outstanding. [Pagination receipt](../evidence/2026-11-27-absorption-pagination.json) |

### Findings that changed the calibration result

- Cancel-and-replace overlap could pass as a ladder because the fitter used the latest
  possible end as proven resting time. Levels that touched the same order at different
  times could also inflate the reported depth. Use the demonstrated common resting
  interval and distinct prices actually present together. [Rule fitting](../src/doppel/lab/rulefit.py)
- A maker could receive a genome after failing the participation timing test. One saved
  fair-maker draft had timing skill −0.0012 with an interval crossing zero. Training-only
  order rows had also been combined with a footprint summary from the whole six-hour run.
  The revised fitter and driver check the evidence period and represent missing predictive
  evidence explicitly. [Answer-key driver](../src/doppel/lab/answerkey.py)
- Claude's two synthetic development cards reported zero confident errors. A first regrade
  flagged three interval misses. Two involved the wrong fitted quantity. A raw price residual
  was compared with an inventory-adjusted intercept, and an exploratory skew interval with a
  constant model's fixed skew. Comparing matching parameters leaves **one confidently wrong
  ladder price offset in each saved run**. Including claims about a single identifiable
  side gives final counts of **1/27** (3.70%) and **1/30** (3.33%). The interim counts
  of 1/25 and 1/28 omitted those partial claims. In both runs the fitter placed the quote
  at the touch, while the planted median was one cent behind it. These numbers measure the
  old fitter. The longer development run had
  36 scheduled holdout starts, four gated fidelity results and two subjects without a genome.
  Missing maker end times continued to block fidelity. [Development counts](../evidence/2026-11-27-answerkey-inputs/development-rescore.json)
- The first absorption implementation could report a perfectly reconciled closing balance
  after swapping the cash amounts of two fills. It could also omit an opening position on
  the opposite token if that token never traded in the interval. Reconciliation now checks
  the individual transaction legs and orders, the known complementary token and response
  cardinality. Log scans and historical balance reads use their respective RPC services
  under a shared request budget. Prior reported cash-balance snapshots alone do not
  establish P&L or reconciliation. [Absorption procedure](ABSORB.md)

## Apex, storage and release

At 03:03 UTC both recorders and the original baseline were active on Apex. The installed
release was `44aa7e2`. Root had 35,616,301,056 bytes free, about 1.83 GiB below the
**configured** 35 GiB archive reserve. Hot tape had 17,810,268,160 bytes free, about
4.59 GiB above its 12 GiB cleanup floor. The baseline's peak was 1,259,061,248 bytes
under a 1,572,864,000-byte memory cap.
The most recent verified independent archive extension was Nov 26 22 UTC. The 23, 00 and
01 UTC receipts were observed on hot tape and absent from the inspected independent archive.
The installed janitor predates protected-day support. No protection file was present on
the hot or Rust tape at the check. The ten legacy H1 hours lacking an original BRTI
generation checksum remain diagnostic. [Prior input audit](DEEP-AUDIT-20261126.md)

The baseline had written about 1.61 GB for its first subject by 02:47 UTC. Its five completed
hours imply about 1.69 GB per subject or 10 GB per six-subject seed, before downstream tool
outputs and simulated activity. Four separately materialized, retained runs could need roughly
41 GB of research scratch. The separate estimate for retaining the 96 registered hours from
Nov 27 00:00 through Dec 1 00:00 UTC was 50.7–58.6 GiB before warmup, post-close inputs,
filesystem overhead and reserve. A capacity decision must account for both.
[Measured retention forecast](../evidence/2026-11-26-retention-forecast.json)

Independently
verified immutable source books can be shared. A comparable old-versus-new analysis can also
reuse the same generated public evidence when the subject simulator and seed are fixed.
The current CLI produces a new per-subject tape for every invocation.

## Next work, in order

1. **Operations and owner:** Secure usable Apex capacity for both recording retention and
   lab scratch. Check availability and retention of each required closed hour before a
   release can activate the reviewed cleanup code. The Rust stop was scheduled at
   04:51:46 UTC and the recorder comparison at 05:05 UTC. Verify both receipts after those
   times. Application promotion and any service restart belong to the release lead.
2. **Calibration:** Run the original tools on populated public evidence for a comparable
   baseline. Then run the fixed evaluator using the frozen parameters and three reserved
   seeds. Use a fresh directory per seed, retain failure logs and report actual fidelity
   verdicts and missing evidence separately. Each applicable subject needs 30 comparable
   holdout starts. With only 36 scheduled, losing seven leaves that subject below the
   threshold. Zero confident errors alone cannot qualify the pilot. Keep development results out of the exam.
   [Exam protocol](../evidence/2026-11-27-answerkey-inputs/EXAM.md)
3. **Absorption:** After the calibration gate, compact the two sealed activity segments in
   a separate fill store and supply the recovered pagination receipt for a complete wallet.
   Use the recorded one-day interval, verified order tape, known token set, and historical
   opening and closing chain state. Reconcile every event, both token legs and cash. The
   first wallet's operational interval is Nov 25 18:52:07 through Nov 26 18:52:07 UTC.
   Resume only the two partial queries if the full cohort is subsequently needed. This wallet
   can test the ledger. Selecting a strategy target also requires Stage 0 persistence against
   matched controls in three of four prospective windows.

## Verification and pickup

The combined code revision is `932f07d` on `review/rule-twin-readiness-20261127`.
The Mac checkout is `/Users/derekduenas/doppel-rule-twin-readiness`.
The calibration tip is `2fb2b8a`, the absorption tip is `d30d185`, and the standalone
book preflight is `16d8871`. GitHub and Apex also hold the interrupted builder's exact
recovery snapshot at `655b219`. Claude's original worktree still contains its six local edits.

Pyright reported zero errors and warnings. The offline source distribution and wheel built,
the lockfile check passed, all 68 Python source modules matched the wheel byte for byte, and
all 32 packaged command entry points loaded. The strategy registry verified 21 entries and
the expression check reproduced semantic digest `49083cf45e14`. The historical development
report also reproduced byte for byte from its archived scoring inputs. The registered input map,
installed Apex release and main branch retain their existing revisions. Raw live tape and
private wallet-level pagination records stay on Apex.

The full Python suite passed **587/587** in 732.12 seconds on combined revision
`04076b9`. The subsequent code change affected only the scoring module and its
regressions. The final calibration branch passed **17/17** answer-key tests, including
the synthetic end-to-end path, in 384.20 seconds. The final combined checkout collected
the same 587 tests and passed all four focused scorecard regressions. The fixture
exercises the pipeline and its refusal gates. Real-book fidelity still requires the
reserved runs above.
