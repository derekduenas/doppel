# Offline taker decisions for wallet inversion

Run on a detached, closed recording and a single wallet's signed order tape and reconciled absorption report. Use an absorption interval that brackets the requested study interval. Allow at least two extra minutes at the right edge for the existing signed-fill join tolerance.

```bash
python -m doppel.lab.wallet_decisions \
  --data /research/tape --brti /research/reference \
  --orders /research/ordertape --absorption /research/wallet-ADDRESS.json \
  --wallet ADDRESS --from START_UNIX_S --to END_UNIX_S --horizon 5m \
  --out /research/new-wallet-decisions

doppel-invert --decisions /research/new-wallet-decisions/decisions.jsonl \
  --venue intl --fee-coef VERIFIED_WALLET_TAKER_COEFFICIENT --out /research/inversion.json
```

The second command uses the inversion consumer on `pod/inversion`. Review `evidence.json` and require `OBSERVED_DECISIONS` before passing its rows to inversion. The evidence file records the input digests, missing tape hours, window exclusions, and the output digest. The export status certifies input observations. The inversion model independently determines the sample size and identification gates.

- A take requires a verified signed call, a compatible public CLOB print, and exact share movement from a reconciled wallet ledger. The exchange print timestamp caps the feature cutoff at execution. The local print receipt is preserved separately, with a three-second maximum lag. Transaction block time bounds the reconciliation and helps reject mismatched prints. Private order intent remains unobserved.
- Market identity comes from public listings or a dated mate witness. The normalized quotes and depths come from the actual Up and Down asks, strictly before the cutoff with a three-second freshness limit. The fair value uses the existing receipt-masked BRTI proxy and an opening published before that cutoff. That proxy is a modeling feature for international BTC markets.
- Inventory starts from the ledger's chain-reconciled token balances. It includes only earlier authenticated fill receipts and abstains if movements, earlier fills, or their order relative to the cutoff are ambiguous. Complete reconciliation applies to the stated chain interval. The exporter excludes the right margin needed for executions within the allocator's tolerance.
- The `none` label represents a fixed ten-second cell after an eligible take was publicly observed in that window. Both outcomes have executable asks at the cell start and the reconciled ledger shows no wallet execution overlapping the cell. A deliberate pass, resting quote, private attention, or execution outside the coverage of the supplied sources remains unobserved.
