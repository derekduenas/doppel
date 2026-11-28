# DOPPEL canary OMS

This crate consumes trusted local JSONL events from the decision, book, reference and watchdog producers. Configure the venue and ledger in TOML.

```toml
ledger = "/path/to/canary-dry-run.jsonl"

[[venues]]
venue = "pmus"

[risk]
max_risk_per_window_usd = 50.0
max_daily_loss_usd = 250.0
max_open_positions = 3
fractional_kelly = 0.25
```

`doppel-exec --mode dry-run --config config.toml` uses a fixture signer and records prepared requests locally. The ledger includes the exact method, path and body, along with the hash chain. API authentication headers and signatures stay out of the ledger. Dry-run fills are never fabricated.

| Input `type` | Payload |
| --- | --- |
| `signals` | `signals` array of genome/market decisions with `genome_id`, `market`, `ts_ns`, `p_hat`, `epistemic_var`, signed `target_contracts`, `kind`, `shadow_expected_price_bps`. All approved contributors for a market publish at the same decision timestamp, including flat targets. |
| `book` | `market` and `book` with `received_ns` and sorted `bids`/`asks`. Levels use integer YES `price_bps` and whole `quantity`. |
| `feed` | `feed` (`brti`, `binance`, `hyperliquid`) and native local `received_ns`. |
| `watchdog` | `genome_id`, `state`, `ts_ns`. |
| `binance_sweep` | `active`, `ts_ns`. |
| `mock_rtt` | Dry-run only. `venue`, `rtt_ms`. |

`market` objects are `{"venue":"pmus","id":"<verified market slug>"}` or the Kalshi equivalent. A publisher must form the ensemble's probability variance from causal information. The existing paper forecast schema supplies `p_hat`. The complete execution-input bridge must additionally publish variance, signed targets, shadow price, L2, and feed receipt clocks.

## Operator handoff

Use dedicated accounts and whole-contract markets. Canary mode requires a root-owned TOML, owner-only venue key files, and a detached Ed25519 approval signed by the trusted public key in TOML. Configure `key_id` and `key_file` per venue. Configure `[approval]` with `manifest`, `signature` and `trusted_public_key_hex`. The signature covers `DOPPEL-CANARY-APPROVAL/v1`, a NUL, and the exact manifest bytes. The manifest binds the account scope, venue key IDs, absolute ledger path, issue/expiry times, initial flat-account attestation and per-genome market grants to the Shadow receipt, E-value and window count. Kalshi maker markets must be verified for zero fees and listed in `kalshi_zero_fee_markets`.

Opening risk uses gross reserve with actual fill receipts. Timeout or ambiguous state retains the submitted reservation and requires reconciliation. Clean startup verifies flat accounts. Kalshi startup uses scoped V2 mass cancel and the documented 60-second quiet interval. Settle or close tracked positions before restart. Approval, reconciliation and deployment stay with the operator. The circuit tests exercise a latched daily stop, stale feeds, RTT, demotion and reduce-only flattening. A later UTC day and a reconciled flat account are required for the explicit `--reset-daily-stop` switch.

Mock order and WebSocket tests run in GitHub Actions. The review uses no real venue credentials. [PMUS orders](https://docs.polymarket.us/api-reference/orders/create-order) · [PMUS fees](https://docs.polymarket.us/fees) · [Kalshi V2 orders](https://docs.kalshi.com/api-reference/orders/create-order-v2) · [Kalshi auth](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests)
