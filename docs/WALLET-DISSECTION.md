# Layer 3 wallet dissection

The registered candidate’s first signed order was a 60-share buy of Down at 7¢. The recorder saw two anonymous additions that could explain it, then a transaction-linked 934.2-share sell print that included the wallet’s 60 shares. The 7¢ bid and 8¢ ask survived both posting hypotheses and execution. Displayed size and recent public flow changed. Wallet ownership of either addition remains unresolved. The number of identified posting decisions in this case is zero. [Layer 2 case](../evidence/2026-11-27-wallet-inspection/first-trade.json)

The candidate is `0xf9b7876ba2c35370e0bedb7f8ee772da722aa37c`. The registered control is `0x28cc505fb60501dbb2199a9662ba175919af904f`. This walkthrough covers Nov 25 from 18:00 to 18:15 UTC, plus a separately registered descriptive check in the reconciled day. The prospective four-week selection schedule continues independently. [Primary registration](../evidence/2026-11-27-wallet-dissection/PROTOCOL.md) · [Additional opportunity check](../evidence/2026-11-27-wallet-dissection/OPPORTUNITY-CHECK.md)

## What was visible at the first candidate fill

Each row uses messages strictly before its recorder receipt, including earlier messages with the same venue millisecond. The complete equal-receipt frame is excluded. Sizes are shares. Flow counts public print messages on the signed token during the preceding second. It does not identify distinct economic aggressors or the wallet.

| Cutoff, Nov 25 UTC | Down best bid × size | Down best ask × size | Up best bid / ask | Down buy flow | Down sell flow |
|---|---:|---:|---:|---:|---:|
| First compatible addition, 18:02:22.997199250 | 7¢ × 1,570.84 | 8¢ × 872.82 | 92¢ / 93¢ | 5 prints, 80.714287 shares | 0 |
| Second compatible addition, 18:02:23.009910470 | 7¢ × 1,685.84 | 8¢ × 872.82 | 92¢ / 93¢ | 5 prints, 80.714287 shares | 0 |
| Aggregate execution, 18:02:24.042136014 | 7¢ × 3,093.03 | 8¢ × 5.00 | 92¢ / 93¢ | 3 prints, 55 shares | 0 |

Independent public trades had identified both outcome labels by 18:00:55.269 UTC, before all three cutoffs. [Timed listing witnesses](../evidence/2026-11-27-wallet-dissection/opportunities.json)

An independent one-token reader verified the raw packet, embedded offsets, sizes and aggregate transaction. Across all 16 distinct receipt boundaries from the first addition through the state immediately before the second, the touch and opposing size stayed constant. The bid took four distinct sizes. The 12.711220 milliseconds between the candidate receipts leave ownership and queue position unresolved. The wallet could have made its decision before either message reached the recorder. [Independent reader and per-cutoff observations](../evidence/2026-11-27-wallet-dissection/first-case-independent.json)

| Concurrent public observation | At either possible addition | Before execution |
|---|---:|---:|
| Polymarket US BTC five-minute Up REST quote | 85¢ / 86¢ | 85¢ / 86¢ |
| Kalshi BTC 15-minute YES REST quote | 70¢ / 71¢ | 69¢ / 70¢ |
| Coinbase BTC-USD last trade | $59,654.97 | $59,647.85 |

The two US quotes are local receipts of REST responses. Their upstream book freshness and the wallet's access to them are unknown. The Kalshi contract has a different horizon. The reduction in spot price is observable context. The tapes contain no account-linked outside execution that would establish a hedge. [External source keys, receipt ages and seals](../evidence/2026-11-27-wallet-dissection/first-case-independent.json)

The recorded BTC reference proxy had upstream receipt timestamps for all 301 required seconds at each cutoff. The largest measured upstream lag in the three histories was 42.960 seconds. The first retained opening-price publication arrived 4,591.495 seconds after the execution receipt. Its absence makes fair value unavailable at all three cuts. The archive has no simultaneous local live-reference feed for those hours. The wallet's private reference is unknown. [Reference and inventory receipts](../evidence/2026-11-27-wallet-dissection/first-reference-inventory.json)

Both outcome holdings opened at zero. The conditional-token ledger recorded no earlier movement in this condition, then the first Down buy at Polygon block 97,949,517. The wallet's first retained public identity receipt came 2.342323 seconds after the CLOB execution. The economic pre-block position was zero and the post-block net position was −60 Up-equivalent shares. The trader's client position, pending executions and wider portfolio at either proposed posting instant remain unobserved. [Signed economic reconstruction](../evidence/2026-11-27-wallet-dissection/first-reference-inventory.json)

## Population and opportunity coverage

| Registered wallet | Reconciled fills | Distinct signed maker orders | Located executions with two-sided signed-token book | Missing execution anchor | Prior receipt had newer venue depth timestamp |
|---|---:|---:|---:|---:|---:|
| Candidate | 30 | 17 | 17 | 0 | 1 |
| Control | 35 | 18 | 16 | 2 | 0 |

The single newer venue timestamp occurred on a different candidate order from the primary case. A researcher can reproduce the recorder's pre-receipt state there. Replaying the actual exchange-time queue would require an independent event ordering. The two missing control executions stay in the population with null context. [Wallet denominators and book source](../evidence/2026-11-27-wallet-dissection/first15-summary.json)

The independent listing check excludes both wallets and their transactions. It steps through every ten-second boundary in each scheduled BTC five-minute market. An eligible observation needs a previously witnessed listing, two-sided book and continuous surrounding capture. Six boundaries in the primary interval lacked an independently received identity, ten had a one-sided book, two had unknown depth, and one had an enclosing capture break.

| Market open, Nov 25 UTC | Independently listed cutoffs | Eligible cutoffs | Candidate fills | Control fills |
|---|---:|---:|---:|---:|
| 18:00 | 24/30 | 18/30 | 8 | 2 |
| 18:05 | 30/30 | 26/30 | 6 | 4 |
| 18:10 | 30/30 | 27/30 | 16 | 29 |
| 22:35 | 30/30 | 22/30 | 0 | 9 |
| 22:40 | 30/30 | 23/30 | 1 | 7 |

All three original markets had transactions from both wallets. The secondary pair was the first to satisfy the preregistered day rule after 12 earlier pairs. The day ledger confirms zero candidate fills in the 22:35 market. That market still had eight unobservable boundaries, and an unfilled order would leave no signed fill. Selecting this pair by its fill outcome supports a denominator check only. [Complete opportunity counts, exclusion causes and source hashes](../evidence/2026-11-27-wallet-dissection/opportunities.json)

Both wallets held both outcomes during parts of the sample. In the short window, 33.46% of the candidate's buy shares completed existing opposing holdings, compared with 22.56% for the control. In the reconciled day the corresponding figures were 36.10% and 27.90%. The candidate held both outcomes for 44.40% of share-weighted holding time that day and the control for 31.43%. Complete sets appear in realized holdings. These observations alone cannot distinguish an intended pairing rule from two independent buying decisions. [Signed pairing denominators](../evidence/2026-11-27-wallet-absorption/run-summary.json)

## Layer 4 handoff

A price-at-touch rule is compatible with both primary posting hypotheses. The differing queue sizes give a candidate discriminator for a threshold rule. The missing opening publication makes a fair-value discriminator unscoreable here. The historical sample supplies no identified fraction of posting decisions, and the public venue quotes supply no attributed hedge coverage. Preserve those outcomes as unknown.

Before opening a new test slice, freeze the competing rules, parameters, target label and whole-market holdout. Use independently attributed placements or controlled wallets with known decisions to test posting hypotheses. Execution prediction can be tested separately on signed fills, provided the question and baseline are declared as execution-level. Include observable markets with no wallet fill, the control wallet and missing-evidence counts. Keep the December selection tests and the three-seed real-book calibration exam reserved. Advance a strategy only after the rivals disagree on scoreable cases and the preferred rule wins out of sample.

## Reproduce the review

The review branch is `review/wallet-dissection-20261127` in `/Users/derekduenas/doppel-wallet-dissection-20261127`. The recorder runs separately. Use an isolated checkout for replay. On Apex the immutable book archive is `/var/backups/doppel-sealed-20261126/tape`. The research output directory is `/var/backups/doppel-sealed-20261126/research/wallet-dissection.aKSm3Q`.

The dissection verifies the Layer 2 order file against its summary and the corresponding book against its recorder seal. The first-case check repeats the original ten-second search on both clocks and binds each addition and the print to its raw message offset. The scanner expires token depth after three seconds without native depth, requires a new snapshot after a stream reset or a silence longer than the 20-second watchdog, and excludes the complete equal-time receive group. It stores the evidence of surrounding capture separately from the fields visible before the cutoff. Later packets can change that retrospective coverage assessment.

The six cutoff-by-outcome states matched when replayed on the full tape, a strict prefix ending before the cutoff, and that prefix with extreme replacement snapshots at the cutoff and a later trade. The independent signed-token reader also agreed on all 11 numeric fields at the three cutoffs. The file records counts of receipts used and the changes in retrospective coverage at EOF. [Prefix verifier receipt](../evidence/2026-11-27-wallet-dissection/prefix-verification.json)

The normal rulefit CLI had zero qualified candidate placement rows. It retained seven control rows, with just one fitting market. Both wallets produced reports and zero strategy files. Across the three markets, both normal fits recorded 71 eligible ten-second cells. Only the sealed BTC five-minute book and the two public listing streams for the selected hour were exposed. Anonymous signing times, block-only execution timestamps and imputed client inventory cannot establish a placement, taker cell, or real inventory-dependent rule. Static token files without an independent prior listing witness can describe historical economic outcomes. Diagnostic fair comparisons in dossier, adverse and vsmarket now disclose their retrospective scope. [CLI receipts](../evidence/2026-11-27-wallet-dissection/consumer-first15.json)

Retained Apex artifacts under the research directory are `first15-47809/{summary.json,orders.jsonl}`, `prefix-47809.json`, `case-2f72.json`, `reference-2f72.json`, `opportunity-v1/opportunities.json` and `consumer-61a06.json`. The full 35-order JSONL SHA-256 is `9c1e1a23555acc8f09152a43dd88ce329c980c90e29b72873c0f047e304b7b35`. The per-cutoff opportunity rows stay in the full 81,912-byte Apex artifact. Its compact review copy preserves contiguous coverage counts and carries the full artifact hash. Earlier scratch checkpoints remain available for comparison.

The commands below use new scratch outputs. Run in a checkout of the recorded source commit with its `src` on `PYTHONPATH`. The jobs read local archive inputs in separate low-priority systemd units. The book and prefix checks used 800 MiB, nice 19, idle I/O and a private network namespace. The normal rulefit consumer used 1 GiB. The opportunity replays used MemoryMax=1000M, CPUWeight=1, IOWeight=1, nice 19 and idle I/O. All raw recordings stay on Apex.

```sh
arc=/var/backups/doppel-sealed-20261126
study="$arc/research/wallet-dissection.aKSm3Q"
previous="$arc/research/wallet-inspection.jOPOHm/qualified-first15"
ledger="$arc/research/wallet-absorption.crsLww"
ev=evidence/2026-11-27-wallet-dissection
casefile=evidence/2026-11-27-wallet-inspection/first-trade.json
py=/opt/doppel/.venv/bin/python
export PYTHONPATH="$PWD/src" OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
dest=$(mktemp -d "$study/reproduce.XXXXXX")

"$py" -m doppel.lab.dissect_wallet --data "$arc/tape" \
  --inspection-summary "$previous/summary.json" --orders "$previous/orders.jsonl" \
  --case "$casefile" --out "$dest/first15"
"$py" "$ev/case_crosscheck.py" --data "$arc/tape" --case "$casefile" > "$dest/independent.json"
"$py" "$ev/verify_prefix.py" --data "$arc/tape" --case "$casefile" \
  --summary "$dest/first15/summary.json" --independent "$dest/independent.json" --scratch "$dest"
"$py" "$ev/reference_case.py" --tape "$arc/tape" \
  --ledger "$ledger/signed-fifteen/wallet-0xf9b7876ba2c35370e0bedb7f8ee772da722aa37c.json"
"$py" "$ev/opportunity_study.py" --tape "$arc/tape" --layer1 "$ledger"
"$py" "$ev/consumer.py" --archive "$arc" --out "$dest/consumer"
```

## Operations

At 13:18 UTC on Nov 27 the production recorder and separate six-hour answer-key baseline were active on Apex. Deployed commit `44aa7e2` was still serving. The 24-hour recorder comparison finished with 13 of 15 streams passing. RTDS A recorded 224 versus 233 control frames, and RTDS B recorded 232 versus 235. Both failed the control-frame comparison. The cutover verdict is **FAIL**. The comparison receipt is `/mnt/volume_nyc1_1793485122853/doppel/lab/shadow-compare-20261127.json` with SHA-256 `03e944bbd5bfb8571454d25574367712abed66a7b5f8ccfc53691464c83280c1`.

The hot tape had 7,762,714,624 free bytes (7.23 GiB), below its 12 GiB reserve. The root/archive disk had 29,256,216,576 free bytes (27.25 GiB), below its 35 GiB reserve. The continuing capacity shortfall puts future capture at risk. The owner’s pending infrastructure decision is to expand Apex storage. The six-hour baseline and local archive policy continue under the owner's prior instructions.
