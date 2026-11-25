# DOPPEL — System Blueprint v2 (ground-up)

*2026-11-25 · new standalone project at `~/doppel` · paper/research until the owner arms live trading*
*Evidence for every "measured" number: `evidence/2026-11-25-probes/FINDINGS.md` (E1–E10).*

---

## 0. The one-of-one thesis

Polymarket international is the deepest prediction market in the world. It is also the only one
where **every fill is public, with identity, role and fee, about 2 seconds after it prints** (E1–E5).
US persons can't trade it, so the people who trade it mostly can't trade **Polymarket US** or
**Kalshi**, which list the same short-dated crypto windows (E8) and the same sports.

DOPPEL turns that asymmetry into a business:

> **Learn who is right on intl and why, then make markets on the US venues using that knowledge, in
> the same seconds, where those operators can't follow.**

Three capabilities, none of which exists together anywhere else:

1. **Flow Atlas.** An exact, role-labeled, operator-clustered ledger of the whole intl market. It is
   rebuilt from chain truth and kept live from the real-time identity tape. Every operator gets a
   point-in-time skill score and a mechanism fingerprint, and every live fill is labeled
   "informed / neutral / retail" about 2 s after it prints.
2. **Policy inversion.** For operators whose skill is real and explainable from public state, we
   rebuild the information they had at t0 and learn their rule, so our engine fires on the same
   trigger. Operators whose actions *can't* be explained become toxicity labels.
3. **The 5m/15m complex engine.** One fair-value engine for the same BTC/ETH/SOL/XRP windows on
   three venues with three settlement indexes and three fee schedules: intl (Chainlink TWAP, taker
   0.05·p(1−p)), PM US (taker coefficient 0.095), and Kalshi (BRTI). It consumes intl prices and
   informed flow as sensors and quotes on US venues.

**What "1000× better" means here.** v1 asked "which wallet should we imitate?" v2 asks "what does
the entire intl market know, two seconds after it knows it, and where is that knowledge not yet in
a US price?" It shifts the unit from one wallet to the whole market, from copying to sensing plus
making, and from "maybe someday" to a first-dollar path through the PM US 5m maker.

---

## 1. What changed from v1 (add / remove / improve)

| Change | v1 | v2 | Why (evidence) |
|---|---|---|---|
| **Add** real-time identity tape | Chain/Goldsky only (t3 ≥ 1 block) | RTDS `activity/trades` maker+taker rows, ×2 redundant sockets, reconciled to chain | E1–E5: free, role-labeled, ~2.2 s behind the print |
| **Add** live operator labels on every fill | Batch atlas | Streaming "who is trading right now" state, per window | turns the atlas into a sensor |
| **Add** 5m complex fair-value engine | Implicit | First-class component with three settlement replicas | E7, E8: same windows, different indexes and fees |
| **Re-rank** first strategy | Latency taker clone | **PM US 5m/15m maker** guarded by intl flow; taker clones second | E8: US taker fee 0.095 (≈2.4¢ at 50¢) punishes takers and pays patient makers |
| **Add** learned market simulator | — | Multi-agent replay with inverted operator policies as counterparties (M5) | Lets us test makers against cloned adversaries before live |
| **Add** own execution + riskd | Edge-engine desk | DOPPEL-native Rust gateway + riskd (patterns ported, code new) | Owner: brand-new project |
| **Add** feed-integrity layer | — | Message-gap watchdog, dual sockets, hourly completeness receipts | E9: official client can stall silently |
| **Remove** heavyweight graph DB and 8-agent swarm | Graph + 8 personas | DuckDB property tables; 4 batch agents | Cost and complexity with no profit link |
| **Remove** per-wallet copy tests | Kept as "is edge alive" check | Gone; the anticipation test replaces them | Copy is settled negative |
| **Keep** | Census kill line, exact ledger G0, PIT skill, twin with queue model, prereg + FDR, auto-demotion | same | These stop us from fooling ourselves |

---

## 2. Profit hypotheses, ranked by distance to the first dollar

| # | Hypothesis | Where | Why it could pay | What kills it |
|---|---|---|---|---|
| **H1** | **Guarded US maker.** Quote both sides of PM US 5m/15m BTC (then ETH/SOL/XRP) around a fair value from the index replica + intl mid + intl informed flow. Pull or widen when atlas-labeled informed operators are hitting intl in the same window. | PM US (+ Kalshi 15m) | US takers pay 0.095·p(1−p). Volume is real (141k–220k sh/window, E8). Intl is a faster, deeper price-discovery venue. | Markouts negative after the guard; queue too long; US quotes already track intl within our latency |
| **H2** | **Cross-venue lead.** Intl price + informed flow moves first; US books lag. Re-quote faster (maker) or take only where edge > fee (fee is small near 0/1). | PM US, Kalshi | Hasbrouck-style information share likely favors the deeper venue | Lag < our tick-to-trade; fee eats the edge |
| **H3** | **Latency-taker clone.** Invert the intl snipers (BTC moved toward side before 89–93 % of fills in Oct). Fire the same trigger on US windows. | Kalshi KXBTC15M (lower fee), PM US extremes | The trigger is public (spot/index) | Sensor parity fails; fee |
| **H4** | **Endgame + structural.** Last-minute near-certain windows; ladder and neg-risk consistency | PM US, Kalshi | Mechanical | Tiny margin, tail risk |
| **H5** | **In-play clones** (esports, tennis, soccer) | PM US sports | Proven intl operators exist (98 % of fills ≤60 s after a game event) | Needs a paid fast event feed (G3) |

*Illustrative capacity, not a forecast.* At ~180k shares per BTC 5m window on PM US and 288
windows/day, the series trades on the order of 50M shares/day. A maker that captured 0.5¢/share on 2 %
of that volume would earn about $5k/day before adverse selection. Milestones M1–M2 exist to replace
this arithmetic with measured markouts.

---

## 3. Architecture

```
SENSORS ───────────────────────────────────────────────────────────────────────────────────────────
 intl CLOB WS (anon prints, books)   RTDS trades ×2 (identity, role, fee)   Polygon finalized logs
 PM US WS (books, trades)            Kalshi WS                               Spot/index: Coinbase,
 Goldsky order_filled v2 (history)   rules/fee snapshots                      Kraken, Bitstamp, BRTI replica,
                                                                              Chainlink-TWAP approximation
        │
        ▼
 C1 TAPE (Rust) — append-only CRC segments, 3 clocks, gap detector, dual-socket merge, hourly
                  completeness receipt, Parquet compaction → object storage
        │                                    │
        │ (research plane, batch)            │ (hot plane, live)
        ▼                                    ▼
 C2 LEDGER ─► C3 FLOW ATLAS (batch) ─► C3L ATLAS-LIVE: label each identity row (operator, class,
 exact P&L     PIT skill, operators,       informed score) ~2 s after the print; per-window flow state
 G0 recon      mechanisms, census                        │
        │                                                ▼
        ▼                                   C6 FAIR VALUE: 5m complex (per-venue settlement replica,
 C4 GRAPH (DuckDB tables, bitemporal)          jump-diffusion + intl mid + informed flow) → fair ± σ
        │                                                │
        ▼                                                ▼
 C5 INVERSION ─► C7 FOUNDRY (genome,      ─► C8 TWIN (queue models, measured latency, fees)
 state@t0,         prereg, FDR ledger)            │ sealed genome hash (only bridge)
 π̂, anticipation                                  ▼
                                           C9 STRATEGY RUNTIME ─► C10 RISKD (sole key holder) ─► PM US / Kalshi
                                                  │
                                           C11 SENTINEL (markouts, e-values, changepoints, auto-demote)
                                                  │
                                           C12 DESK (Atlas · Complex · Strategies · Sentinel · Health)
 C13 AGENTS (batch, capped): Profiler · Skeptic · Quartermaster · Coroner — propose only
```

### Component specs

| ID | Component | Inputs → Outputs | Tech | SLO / acceptance |
|---|---|---|---|---|
| C1 | **Tape** | all sensors → CRC segments + hourly Parquet + completeness receipts | Rust (tokio, tungstenite, zstd), cryo for chain audit | Gap detected < 5 s. Dual RTDS sockets with a message-gap watchdog (no msg in 10 s on an active market → reconnect + flag). Hourly receipt: RTDS fills vs chain fills ≥ 99.5 %. |
| C2 | **Ledger** | Goldsky v2 + log tail → per wallet × market cash flows, P&L by mechanism | Python, Polars, DuckDB | **G0**: ≥ 95 % of a stratified 200-wallet sample within max($50, 2 %) of `v2/user-pnl` |
| C3 | **Flow Atlas** | ledger → PIT skill, operator clusters, mechanism fingerprints, counterparty map, census | Polars, scikit-learn, LightGBM | Weekly PIT ranks frozen before scoring. Controls are activity/price/category matched. |
| C3L | **Atlas-live** | RTDS rows + atlas tables → per-fill label + per-window flow state | Rust lookup service (atlas snapshot loaded hourly) | Label latency < 50 ms after RTDS receipt |
| C4 | **Graph** | everything → bitemporal property tables (nodes/edges with valid + knowledge time) | DuckDB | Any feature recomputable as of a past decision (replay test) |
| C5 | **Inversion lab** | state@t0−δ + labeled actions → π̂ (hazard, side, size, inverse-quote), explainability ceiling, info horizon, anticipation rate | LightGBM, PyTorch (sequence models only if they beat tabular on sealed holdout) | Reported per operator with CIs |
| C6 | **Fair value: 5m complex** | index replicas, spot ticks, intl mid, flow state → fair ± σ per venue per window | Rust hot path; params fit in Python | ≤ 1 ms compute; calibration (Brier, reliability) per venue on settled windows |
| C7 | **Foundry** | genomes → preregistrations, FDR ledger, verdicts | Python, JSON Schema, append-only registry with sha256 | Every variant charged. Holm per family. Power + futility bounds before unblinding. |
| C8 | **Twin** | tape + genome → simulated orders/fills/P&L | Rust; queue models ported from hftbacktest (MIT) | **Fidelity**: twin-predicted vs actual fills on our probes, error bound preregistered |
| C9 | **Strategy runtime** | sealed genome + live tape + fair → intents | Rust | Tick-to-intent p99 < 2 ms internal. Paper and live share one code path. |
| C10 | **riskd + gateway** | intents → venue orders | Rust; PM US + Kalshi native APIs | Sole key holder. Per-strategy/venue/window caps, global kill file, idempotent client IDs, 1 s reconcile loop. Promotion needs the owner; demotion is automatic. |
| C11 | **Sentinel** | fills, markouts (1 s/10 s/60 s/settle), forecasts → e-values, BOCPD/CUSUM, demotions | Python service + Rust counters | Edge e-process for "edge ≤ 0" crossing 1/α demotes within one window |
| C12 | **Desk** | all → views | FastAPI + one React app; Grafana for ops metrics | Every number links to a ledger row, receipt, or trial |
| C13 | **Agents** | sealed dossiers → proposals, falsifiers, feasibility cards, post-mortems | Claude Agent SDK, batch nightly | Hard monthly cap. Claims without graph IDs are dropped. Scored by survival rate. |

### Hosting

- **Research box** (new): 8 vCPU / 32 GB / 1 TB NVMe. Runs ledger, atlas, inversion, twin, foundry,
  desk, and the batch agents. Runs the intl/RTDS recorders too, since intl data is location-insensitive.
- **Trading box** (new, after a latency probe): placed nearest the US venues. Kalshi WS is AWS
  us-east-2 (~15.5 ms RTT from NYC). PM US endpoint RTT is to be measured from NYC, us-east-1 and
  us-east-2 before choosing. Runs C1 US-venue recorders, C3L, C6, C9, C10, C11.
- **Apex (atlas-do)** is untouched. It keeps its current jobs; DOPPEL shares nothing with it.

### Core data contracts (abridged)

```
TapeEvent   { src, venue, stream, seq, t_event, t_publish?, t_recv_wall, t_recv_mono, payload_hash, payload }
Fill        { venue, tx|trade_id, block?, asset, side, price_µ, size_µ, fee_µ, role(maker|taker), wallet?, order_hash?, counterparty?, source(goldsky|chain|rtds|venue) }
OperatorLbl { wallet, operator_id, class(informed|neutral|retail|unknown), skill_pit, mechanism[], asof_valid, asof_known }
FairQuote   { venue, window_id, t, fair, sigma, inputs_hash }
Genome      { universe, trigger, fair_value, execution(maker|taker|pair|recovery), sizing, exit, risk_envelope, data_windows, metric, power, futility, kill_rule, code_sha, parent_id }
```

All money is integer micro-USD. All prices are integer ticks. Clocks: event, publish (if known),
receipt wall, and receipt monotonic.

### Repo layout (`~/doppel`)

```
crates/   tape · book · fair · twin · runtime · riskd · gateway-pmus · gateway-kalshi · sentinel-core
lab/      ledger · atlas · inversion · foundry · census · agents      (Python, uv workspace)
schemas/  tape.avsc · fill.json · genome.schema.json
registry/ preregistrations/*.json · fdr_ledger.jsonl (append-only, hashed)
desk/     api/ · web/
ops/      systemd units · deploy · receipts
evidence/ dated probe folders with SHA256SUMS
docs/     BLUEPRINT.md · decisions/ (ADR per owner decision)
third_party/REFERENCES.md
```

---

## 4. Plan (milestones with acceptance tests)

| M | Duration | Build | Done when | Decision |
|---|---|---|---|---|
| **M0 Recorders** | days 1–3 | C1 for: intl CLOB books+prints on all crypto 5m/15m/1h; RTDS trades ×2; PM US 5m/15m books+trades; Kalshi KXBTC15M/ETH15M; Coinbase/Kraken/Bitstamp ticks + BRTI replica | 24 h of tape with hourly completeness receipts ≥ 99.5 %, watchdog drill passed | Books can't be backfilled, so start M0 first |
| **M1 Census** | weeks 1–2 | Goldsky 120-day backfill; C2 ledger + G0; C3 atlas, operators, mechanism census, counterparty map | G0 passes; census report shows $/day by mechanism × venue-transferable | **Program kill line**: clonable + US-transferable net < owner's line (suggest $500/day) and H1 has no measurable intl→US lead → stop, keep the atlas |
| **M2 Complex + H1 twin** | weeks 2–4 | C6 fair value (3 replicas, calibration); C3L live labels; C8 twin with ported queue models; H1 genome in foundry | Calibrated fair per venue; H1 passes G4 in twin on ≥ 2 weeks of PM US tape | Shadow H1 |
| **M3 Shadow + riskd** | weeks 4–6 | C9/C10/C11 paper on live data; desk v1 | G5: ≥ 14 days, paper ≈ twin, e-value ≥ 20 | Owner arms micro-live ($25–100 caps) |
| **M4 Inversion → H3** | weeks 5–8 | C5 on top atlas operators; anticipation tests; H2/H3 genomes | G2–G4 on held-out weeks | Add strategies |
| **M5 Simulator + agents + H5** | weeks 8+ | Multi-agent learned simulator; C13 agents; sports/esports after G3 | Agents' proposals survive at a measured rate; simulator fills match twin | Scale |

### Gates (unchanged in spirit, tightened)

G0 ledger truth · G1 PIT skill vs controls (3 of 4 rolling windows) · G2 clonable (side AUC ≥ 0.65,
timing log-lik gain > 0, anticipation ≥ 50 %) · G3 sensor parity (our trigger receipt p90 < venue
repricing half-life) · G4 twin economics (Holm-adjusted t ≥ 2.5; positive at 1× and 2×; positive
excluding the largest window or event) · G5 shadow (≥ 14 d, e ≥ 20) · G6 micro-live (owner arms) ·
G7 scale (marginal $ > 0 under stressed fills).

---

## 5. Budget

| Item | Monthly |
|---|---|
| Research box (8c/32 GB/1 TB) | $100–200 |
| Trading box near venues | $40–80 |
| Object storage (Parquet, receipts) | $10–40 |
| Goldsky (pricing to confirm) + paid Polygon RPC | $100–300 |
| LLM agents (hard cap) | $150 |
| **Total** | **≈ $400–770** |

---

## 6. Known unknowns (each is checked in a named milestone)

- RTDS completeness vs chain at scale (E1 was 25 s). M0 hourly receipt.
- PM US 5m settlement source text (prior: CF index). M0: pull the rules and pin the hash.
- Whether the intl Chainlink TWAP stream is public in real time, or must be approximated from exchange ticks. M2.
- PM US maker economics: fees, rebates/incentives, queue priority, order-rate limits. M2–M3.
- Whether any intl→US lead exceeds our tick-to-trade (H1/H2 core). M1–M2.
- Regulatory: US venues only; intl is read-only public data. No VPN, no intl account.

---

## 7. Owner decisions (three)

1. **Start M0 recorders on two new boxes** (research + trading), leaving Apex alone? Order books can't be backfilled, so every day of delay is a day of tape lost.
2. **Budget**: ≈ $400–770/month including Goldsky + RPC + capped agents.
3. **Kill line + venues**: PM US + Kalshi only, with the M1 census kill line at $500/day of clonable, US-transferable net profit (or your number).
