# Open-source and data references (checked 2026-11-25)

| Resource | Use in DOPPEL | License / status |
|---|---|---|
| [nkaz001/hftbacktest](https://github.com/nkaz001/hftbacktest) | Reference queue-position models (probabilistic queue, L2 risk-averse) to port into `twin` | MIT · ★5.1k · pushed 2025-12 |
| [nautechsystems/nautilus_trader](https://github.com/nautechsystems/nautilus_trader) | Reference for deterministic event-driven engine design + Polymarket BinaryOption modeling; not a runtime dependency (no PM US/Kalshi adapters, intl execution not used) | LGPL-3.0 · ★31.8k · active |
| [evan-kolberg/prediction-market-backtesting](https://github.com/evan-kolberg/prediction-market-backtesting) | Read its execution-modeling notes; compare twin results on a shared tape | no SPDX · reference only |
| [Polymarket/real-time-data-client](https://github.com/Polymarket/real-time-data-client) | RTDS topic/schema contract (topics, Trade schema); keepalive behavior | MIT |
| [Polymarket/rs-clob-client](https://github.com/Polymarket/rs-clob-client) | Rust types for CLOB WS/REST messages (read-only market data) | MIT |
| [Polymarket/py-clob-client](https://github.com/Polymarket/py-clob-client) | Research-side REST helpers | MIT |
| [paradigmxyz/cryo](https://github.com/paradigmxyz/cryo) | Independent chain-log extraction to Parquet for Goldsky span audits | Apache-2.0 · active |
| Goldsky `polymarket.order_filled` v2 | Primary historical identity/role/fee archive | commercial; pricing TBD |
| [SII-WANGZJ/Polymarket_data](https://huggingface.co/datasets/SII-WANGZJ/Polymarket_data) | Cross-check only; community-reported 9–60 h gaps | MIT dataset |
| [warproxxx/poly_data](https://github.com/warproxxx/poly_data) | Read for Goldsky/Gamma joins; do not vendor | GPL-3.0 (reference only) |
| [duckdb](https://github.com/duckdb/duckdb) + [polars](https://github.com/pola-rs/polars) | Research store + feature compute over Parquet | MIT |
| [getzep/graphiti](https://github.com/getzep/graphiti) | Optional: temporal memory for agent dossiers only (not the system of record) | Apache-2.0 |
| [kuzudb/kuzu](https://github.com/kuzudb/kuzu) | Considered for graph; **rejected** (last push 2025-10) — graph lives in DuckDB tables | MIT |
| Research: Yang/Qiao/Beling IRL trader identification (GPIRL, >95% ID accuracy on E-mini) | Method basis for policy inversion features | papers |
| Research: Akey et al. (CEPR DP21615), Jia & Sun (arXiv 2610.34754), Bailey et al. backtest overfitting, Howard et al. confidence sequences | Evaluation and selection-bias controls | papers |

## Ported from the owner's earlier repos (reference only, re-implemented with provenance notes)

| From | What | Why |
|---|---|---|
| perelman | BRTI / CF v17.0 replica, 60 s strike rule | US-venue settlement index for 5m/15m |
| Edge-engine | riskd sole-key-holder pattern, CRC WAL, Kalshi fee golden vectors (64,586 cases) | execution safety + fee exactness |
| polymarket-us-edge / Edge-engine | PM US half-even cent fee rounding + cumulative taker cap | fee exactness |
| quinn-research | three-clock rule, maker-pair preregistration + power check, checkpointed replay | evaluation discipline |
| sports-terminal | measured venue latencies, PM US/Kalshi gotchas | ops knowledge |
