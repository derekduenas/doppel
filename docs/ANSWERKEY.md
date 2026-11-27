# Answer-key lab: how accurately does DOPPEL recover a strategy?

`doppel-answerkey` (`src/doppel/lab/answerkey.py`) runs scripted wallets with known rules on real
recorded intl CLOB books. It gives DOPPEL's reconstruction tools only the evidence a public wallet
leaves, then scores their answers against the rules. The module docstring is the full
specification. This page covers how to run it, what goes into the evidence and what the numbers
mean.

```sh
doppel-answerkey --data TAPE --tokens LAB/tokens.jsonl --stream clob_btc_5m \
    --from 2026-11-26T12:00 --to 2026-11-26T13:00 --out LAB/answerkey/run1 [--strategies a,b,c,d,e,f] [--seed 1]
```

`--split` (default: the middle of the range) separates the two parts of the range. rulefit fits
on `[from, split)` and fidelity holds out `[split, to)`. The token map is read only, so resolved
payouts must already be in it. Policy (f) needs them, and so do P&L and fidelity outcomes. Policy
(b) and every fair-v1 genome need `ref_brti_raw` and `ref_prices` under `--data`.

## Subjects

| key | policy | rule (parameters in `params`) |
|---|---|---|
| a | midpoint_maker | BUY Up at floor(mid−2), BUY Down so that the Up ask is ceil(mid+2). Size drawn from {10, 25, 50}. Re-posts every 5 s or on a 1 c mid move. |
| b | fair_maker | Quotes fair-v1 ± 3 − 0.05·inv. Size 20. Re-posts on a 1 c target move or every 10 s. BTC only. |
| c | complete_set | Rests BUY Up and BUY Down at both best bids when their sum is ≤ 98 c. Buys at most one size more of one side than the other. Holds to settlement. |
| d | touch_ladder | Bids at the touch and 1 and 2 c behind it on both tokens. Sizes 97–100. |
| e | random_taker | Decoy. Places FAK buys at random times, at (a)'s visible order rate. |
| f | hidden_signal | Joins the touch on one token per window. A private signal picks the token: the resolved outcome, flipped with probability 0.25. |

**Policy (c) differs from the brief.** The venue mirrors every book, so the Up ask plus the Down
ask is always 100 c plus the spread. A complete-set taker that waits for that sum to fall below
1 − m never trades. Policy (c) is therefore the maker form, which is what the E36 wallet does.

**Policy (f) looks ahead on purpose.** Its signal is the subject's private information. The signal
is written only to `truth/truth.json`.

**Policy (d) shares its sizes with a real wallet.** Sizes 97–100 are the real 0x0cb0 wallet's
fingerprint (E36, E39). On real tape that wallet's quotes will enter (d)'s fill footprint and
copyability cells and inflate the candidate count. Read (d) with that in mind.

## Fill model (truth)

The simulator makes one forward pass in tape order. It only uses data the subject has received:
- A policy decides at a record's receive time, from the book as of that record. Its own orders are
  excluded; a wallet knows its own quotes.
- An action lands 80–400 ms later (seeded) in an injected record.

Resting orders:
- Each resting order runs through `orderreplay.QuoteSim`, the twin engine's maker fill code,
  unchanged, with the pessimistic queue: everything at the level on landing is ahead of the order.
- Prints from the order's token and its mate are mirrored, and mirror duplicates are dropped.
- Prints reach own orders best price first, and each fill is taken out of the print before the next
  order sees it.
- A cancel takes effect when it lands.
- A post-only order that would cross is rejected. A rejected order never appears in the evidence.

Takers place FAK orders at the decision's best ask. On landing they fill up to the size displayed
at that level. Fees are 0.

Three files in `truth/` hold the full record:
- `orders.jsonl`: every order, with its placement, landing, queue, fills, end, status and reference
  values;
- `truth.json`: the parameters, a summary measured by fixed rules, P&L by resolved payout,
  inventory per window and, for (f), the signal;
- the summary inside `truth.json`: fill ratio, re-post gap, cancel life, resting times, offsets and
  taker share.

## What is injected into `public/`, and nothing else

**`tape/<stream>`: the stream, rewritten.** `price_change` sizes and `book` levels are absolute
totals, so the subject's resting remainder is added to every later frame that touches its level,
on both the token's view and the mirrored view.

| subject event | how it appears in the rewritten stream |
|---|---|
| placement | one injected `price_change` with both views, at landing |
| maker fill | the real print it came from shrinks by the shares filled, and so does that print's mirror copy. An injected `last_trade_price` carries the subject's tx hash and the fill, followed by an injected level decrease. Total printed volume is conserved. |
| cancel | one injected decrease of the remainder |
| taker fill | an injected print and a decrease of the level it hit. The decrease lasts until that level's next real update. |

Other stream directories are linked unchanged, except `rtds_*` and `goldsky_activity`, which are
left out.

**`orders.jsonl`: `ordertape.SCHEMA` rows for orders that filled at least once.**
- There is one row per fill transaction.
- The subject's own rows have `verified: true`, the signed `created_ms` (decision time on the
  venue-clock estimate) and an integer `block_ts` 1–2 s after the fill.
- Every transaction is a complete call. The anonymous counterparty row is included with
  `verified: false`, which is how a cohort tape holds other makers. That row is the taker for a
  maker fill and the maker for a taker fill.

**`fills/`**: the fill store, built with `fillstore.compact` from `goldsky/` frames of the
subject's fills.

**`lab/tokens.jsonl`**: a copy of the public token map.

**`coverage_receipt.json`**: covers fidelity's holdout. It is true by construction because the lab
wrote every fill. On real data this receipt must come from an independent coverage check.

**Never written to `public/`:**
- cancels as events;
- unfilled, rejected or in-flight-cancelled orders and their ids;
- parameters, policy names, the signal.

`tests/test_answerkey.py` checks this list. It greps every public file.

**Known limits of the injection:**
- Injected quotes and trades do not change any other participant's behaviour.
- When the opposite side reaches a resting quote without a print (a book-cross fill), the quote gets
  an injected print and can fill twice in orderreplay. Such fills are counted in `fill_via`.
- The `best_bid`, `best_ask` and `hash` fields of rewritten frames are not recomputed.
- While an asset's state is unknown nothing is injected for it; the next snapshot carries the
  overlay.
- The subject's RTDS activity rows are absent. The order-tape stage (RPC decode and EIP-712 check)
  is not exercised: its output is written directly.

## Scoring

The tools run as child processes (`python -m doppel.lab.<tool>`) on `public/` only:
1. `doppel-ordermatch` on the whole range;
2. `doppel-orderreplay` with `--orders`;
3. `doppel-profile`;
4. `doppel-rulefit` on `[from, split)`;
5. `doppel-fidelity` on `[split, to)`, run only when rulefit emitted a draft genome. It is given the
   receipt and the ordermatch directory.

`EXPECT`, the tolerances and the source of each cell are fixed in code before a run:

| obligation | true value | answered by | correct when |
|---|---|---|---|
| size_rule | the sizes the policy placed | profile `size_rule` | the size sets are equal |
| price_reference | mid / fair / touch | rulefit `price_rule.<side>.chosen` (or its tie) | the reference matches on every side |
| price_offset | median (limit − true reference) over ALL placements | rulefit `rule.offset_c` | within 1 c on every side |
| skew | 0, or k for (b) | rulefit `rule.skew_c_per_contract` | within max(0.02, 0.5k) |
| cadence | median re-post gap per token, all placements | rulefit `timing_rule.gap_s`; status from profile `placement_cadence` | within a factor of 2 |
| cancel_rule | median life of all cancelled orders | rulefit `timing_rule.cancel.life_ms`; status from profile `cancel_replace` | within a factor of 2 |
| ladder | true only for (d) | rulefit `ladder.ladder_share` ≥ 0.1 (n ≥ 20) | the yes/no answer matches |
| fill_footprint | landed orders with a fill ÷ landed | profile `fill_footprint` | within a factor of 2 |
| copyability | profile's COPY rule applied to the true quantities | profile `copyability` | the verdict matches |
| fidelity_verdict | (b) should PASS; (e) and (f) must not; (a), (c), (d) not scored | fidelity verdict | see the categories below |
| explained_by_observed_inputs | true for a–d, false for e and f | rulefit emits a draft genome (claimed if fidelity PASSES) | the answer matches |

Each cell falls in one category:

| category | meaning |
|---|---|
| `correct` | the tool's answer matches the truth within tolerance |
| `wrong` | the answer is wrong but hedged (ESTIMATED, or a CI containing the truth) |
| `wrongly_confident` | the answer is wrong and VERIFIED, or its CI excludes the truth |
| `correctly_unknown` | there is no true rule and the tool gave none |
| `missed` | there is a true rule and the tool said UNKNOWN |
| `not_scored` | the cell has no defined answer |

For (e) and (f), an explicit negative rule-fit result with no draft can count as correctly
unknown. An indeterminate explanation, missing tool output or gated fidelity run supplies
no successful negative control. Each fidelity cell records
`by_gate`, which marks an INCONCLUSIVE caused by a data gate. One hour has fewer than 30 window
starts, so every fidelity run on one hour is INCONCLUSIVE by gate. "The decoy never PASSes" only
means something on a holdout of at least 30 window starts.

The two headline rates:
- `recovery_accuracy` = correct ÷ cells with a true value (correct, wrong, wrongly_confident,
  missed).
- `false_confidence_rate` = wrongly_confident ÷ scored cells with a substantive confident
  answer or a checkable interval or bound. A bound that excludes truth counts as a confident
  error even when the point estimate passes its tolerance.

`ci_coverage` records empirical coverage of the reported limits, including one-sided timing
bounds. Re-running `doppel-answerkey` needs a fresh output directory. Before simulating any
subject, the CLI verifies resolved pairs against actual quoted tokens in both periods. The
training matcher and fitter see only fills published before the split. The full-span matcher
is retained for diagnostics and holdout replay. The [exam specification](../evidence/2026-11-27-answerkey-inputs/EXAM.md)
gives the reserved seeds and subject parameters.

`hidden_and_decoy` reports, for (e) and (f), whether rulefit emitted a draft genome, the fidelity
verdict, and whether the result was a confident-but-wrong rule. A genome emitted without a PASS is
an unvalidated wrong rule.

The scorecard also has a `pipeline` section for each strategy:
- the ordermatch match and placebo rates and outcomes against the truth outcomes of the visible
  orders;
- the orderreplay pessimistic ratio and bracket violations.

The same fill code produced the truth, so a ratio far from 1 is an adapter problem, not a model
finding.

Outputs: `<out>/scorecard.json`, `<out>/scorecard.md`, and
`<out>/<policy>/{truth,public,tools}/`.

## Runtime and memory

The simulator holds the book, the open orders and one small record per order. Each tool reports its
own peak RSS in `strategies.<k>.tools.rss_mb`. The simulator's own peak is in `rss_mb_after_sim`
and `peak_rss_mb.self`. The figures below were measured on synthetic data only; validate them on Apex.

The synthetic run covered one strategy-hour of a 5m BTC stream at 10 messages a second, with all
six subjects, on macOS. The peak RSS of the whole process, including building the synthetic tape,
was 260 MB. The largest tool child process peaked at 313 MB (orderreplay on (d)). Simulation took
16–37 s per subject.

A real BTC stream is denser, so check `rss_mb` there before trusting the 1.5 GB budget. ordermatch
replays every linked `clob_*` stream; that is its own documented cost.

## First synthetic result (one hour; rulefit on the first 45 min, fidelity on the last 15)

Pipeline sanity, every maker subject:
- ordermatch match rate 1.0;
- orderreplay pessimistic ratio 0.989–0.999;
- bracket violations 0 for (a), (b) and (c), 10 for (d) and 1 for (f). These are attributed to the
  book-cross limit described above, but that is not yet verified.

Scores:
- Recovery accuracy was 0.67 (37 of 55 cells).
- The false-confidence rate was 0.40 (8 of 20 confident claims).

The 8 wrongly confident cells:

| cells | tool | what happened |
|---|---|---|
| (a), (c), (f) | rulefit ladder | Reported a ladder. A cancel and its replacement land at independent latencies, so old and new quotes overlap briefly at different prices, and that overlap reaches the 0.1 ladder share. |
| (a), (c), (d) | rulefit `gap_s` | The bootstrap CI excluded the true re-post gap. The gap is measured between FILLED orders, so it is 2–2.4× the true gap. |
| (a), (d) | rulefit cancel life | The bootstrap CI excluded the true cancel life. Only cancels of partly filled remainders are visible, so the measured life is 3–4× the true one. |

For (f), the hidden-signal subject, rulefit emitted a draft genome: an unvalidated wrong rule. On
one 15-minute holdout fidelity was INCONCLUSIVE by gate, so no confident-but-wrong PASS was
observed, but that holdout cannot test for one. The decoy (e) produced no rule.

## What to validate on real data

- Pipeline checks first. The ordermatch match rate should be about 1 and the placebo rate low. The
  orderreplay pessimistic ratio should be about 1 with zero bracket violations. Otherwise the
  scores measure the injection, not the tools.
- Run at least 30 window starts in the holdout (for example 6 h of 5m windows) before reading any
  fidelity cell.
- Read (d) against the real 0x0cb0 fingerprint.
- Re-run with two or three seeds; one seed is one draw of every random choice.
