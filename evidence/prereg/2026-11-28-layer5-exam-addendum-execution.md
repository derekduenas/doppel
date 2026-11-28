# Layer 5 exam: execution addendum (2026-11-28, written before any exam result exists)

The rules, code (9eac081), inputs, seeds and pass thresholds of `2026-11-28-layer5-exam.md` are unchanged. Only where and how the subjects are run changes.

**Change**
- The exam runs in GitHub Actions (`.github/workflows/exam-matrix.yml`) as 18 parallel jobs, one subject × seed each, instead of three sequential seeds on Apex.
- Reason: Apex has 4 cores, and the sequential run would take until about Nov 30. The owner decided no new server.

**Why it is the same experiment**
- Every subject builds its own seeded policy and simulator (`run_strategy`, seed per subject), so a one-subject run reproduces that subject's part of the sequential run.
- The one coupling is subject e's rate. In the sequential run it comes from subject a of the same seed (`answerkey.py:1384`). The e job reads that value from the a job's scorecard and passes it as `rate_per_s`, computed by the same expression: `max(train_visible_maker_orders, 1) / (split - from)`.
- Consequently e's card records the sha256 of the derived parameter file, not `e6a7faf1`. The derived file is uploaded with e's artifact.
- The tape is the six sealed segments, verified against the `input-preflight.json` hashes before each run.

**Cross-host check, recorded before reading any verdict**
- On Apex, seed 271031's subject a simulation finished before this change. Its `truth/orders.jsonl` must hash-equal the GitHub job's.
- A mismatch means the hosts are not interchangeable. The matrix result is then not used, and the exam reruns sequentially on Apex.

**Scoring**
- The pooled 3 seeds × 6 subjects scorecard is assembled from the 18 job scorecards with answerkey's own `totals` function.
