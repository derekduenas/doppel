# Codex review for Sprint v3

Use `docs/SPRINT-V3.md` on `bbcca84` or later as the working plan. Codex owns WS0 and reviews evidence interfaces. Opus owns the registered exam, mechanism studies, merge queue and deployments. Codex paused the old PR‑3 and PR‑4 scratch work after the v3 change. Neither scratch branch was published. Opus's matcher revision `9eac081` already includes the compensated print-volume summation needed for parity.

## Promotion review

`pod/promotion` at `e918da2` can summarize candidate statistics. Its output requires a separate, fail-closed shadow admission decision.

- `promotion.py:224–235,264–276` requires an integer trial count. A partial or empty attempt ledger can still reach the statistics and a claimed single trial skips PBO. `load_variants` checks lengths without binding columns to ordered market window IDs or requiring one unique column per declared variant. Anchor the complete attempted-variant ledger and require the candidate and all trial IDs to match the comparison matrix.
- `promotion.py:291–306,358–364` accepts a submitted `PASS` and P&L rows. An input hash identifies bytes but does not establish that they came from a registered, untouched partition. Verify the canonical registry evidence, sealed snapshot, scope, costs and fixed statistical parameters before admitting a candidate.
- `promotion.py:238–256` does not enforce the v3 executable size and fill-rate test, short-horizon markouts, replay-to-paper price agreement or active-genome residual correlation. Each missing input should result in a hold. Keep admission in a new module until the H4 scoring freeze lifts.

## Real wallet decision evidence

The existing Layer 3 code reconstructed earlier book states for located fills, and Layer 4 preserved unknown actions. A new matched-pass table should distinguish an observable eligible opportunity with no signed take from actual owner intent. Missing or stale depth, conflicting signed receipts, and unsigned quote identities need their own exclusion counts. Bind the L1–L3 observations and BRTI publication, inventory and decision clocks to the same strictly earlier cutoff. Select matching and abstention rules before inspecting validation outcomes.

## Layer 5 scoring contract

At `9eac081` the scorecard totals classify component claims. The v3 preregistration additionally needs a denominator of recoverable strategies, family-level confidence with interval coverage, abstention on each negative control, and paired held-out taker truth/predicted entry timestamps. Freeze the aggregation and the timestamp pairing rule before opening any score. Reconcile the old component count with any new family-level measure explicitly. Treat a missing timing or family verdict as unmeasured evidence. A fitted timing model's aggregate skill and an observed fill's timestamp cannot supply an entry-time error.

The only taker in the six-policy exam is the random negative control. The four recoverable policies are makers, and rulefit v1 emits no taker entry predictions. Report the timing denominator as zero and the gate as unmeasured. A separate preregistered positive-taker trial is needed to measure it. The scorecard's component-level confidence and negative-control outcomes still have diagnostic value.

The original sample and seeds were frozen before the run. Git records the exam chain running at 00:05 UTC and the v3 threshold commit at 00:08 UTC. The absence of a final card at registration does not establish whether any earlier subject artifacts were opened. Keep the original exam's evidence and the v3 gate accounting explicit. The Codex review did not open any running results.
