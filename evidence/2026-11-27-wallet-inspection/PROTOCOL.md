# Layer 2 inspection protocol

Frozen before looking at placement matches for the registered pair. The prior layer
already established the candidate's clock anomaly and the first fill's identity.

Use the wallet addresses, signed-order tapes, fill stores and economic receipts from
Layer 1. Take all fills in each receipt. Group maker fills by trading account and signed
order hash. Select on the fill interval so a bad creation clock cannot remove a subject.
Preserve counts of outstanding joins, takers, missing prints and missing book coverage.
Use the sealed international BTC five-minute book, the discovery cohort's primary market
for this candidate. Report other tokens in each wallet's denominator.

The primary case is the first witnessed candidate transaction, 60 shares at 7¢. Replicate
on both wallets in the original 15 minutes and separately on the subsequent captured
24 hours. The 37-minute gap lacks reconciled Layer 1 input. It is outside this experiment.
Use the existing timestamp-based matcher as a separate comparator for orders that pass
its clock screen. No thresholds will be fitted to these two wallets.

1. Locate the earliest observed maker fill using its transaction hash. Require compatible
   asset, execution price and aggressor side on the direct or complementary representation.
   Require enough print quantity, unambiguous timing, and a print within 120 seconds of
   its recorded block second. Multiple compatible representations at one time cannot
   create multiple fills. Report insufficient, contradictory or ambiguous evidence.
2. For each usable anchor, search the same token, maker side and signed limit price in
   the ten seconds preceding the print. Count native price-change increases within
   0.02 shares of the full signed size. Require strict venue and capture ordering.
   A snapshot difference does not establish a posting time. Keep zero, one and multiple
   compatible increases distinct. A single candidate can still belong to another trader.
3. Repeat the same ten-second test with the anchor shifted exactly 60 seconds earlier
   on the same asset. The paired comparison requires both search windows to be fully
   covered by continuous book state in the same market. Use the same matching rules
   and one-event competition checks for both searches. Report numerator, denominator
   and distinct conditions. Left censoring and missing hours cannot count as misses.
4. Never use the signed creation timestamp as a placement anchor in this test. A sole
   anonymous add provides a conditional interval to the fill. A directly observed
   zero-to-positive level transition can bound continuous displayed residence from
   above if it occurred in the same uninterrupted book state before the fill. Preserve
   unknown lifetimes, partial fills, and unknown cancellations. No public depth update
   identifies the account or proves a strategy. These retrospective outputs are
   diagnostic and are ineligible as training labels.

Research runs use previously retained sources, bounded memory, idle I/O priority and
small outputs on Apex. Compare the final receipt hashes on Apex and in the review branch.
