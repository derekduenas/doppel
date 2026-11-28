# DOPPEL reference system design

Status: draft for owner review, written 2026-11-27 on branch `pod/system-design` from
`review/wallet-rivals-20261127` at `ac09403`. Every future build must fit this
document, or it must change this document first in a reviewed commit.

This document is a map of what exists and a contract for what comes next. It adds no
evidence. Where it cites a number, the number comes from the dated document named next
to it.

## How to read status

Four places can hold a piece of DOPPEL, and they differ today. Every claim about the
current system carries one of these tags.

| Tag | Meaning | How to check |
|---|---|---|
| **[Apex]** | Installed and running on the production host, as reported by a dated audit. Agents cannot ssh, so this is the latest written observation, not a live reading. | The cited audit; on the host, `/opt/doppel/DEPLOYED_COMMIT` and `systemctl status` |
| **[main]** | On `origin/main`, which is `3cff434`. | `git show origin/main:<path>` |
| **[review]** | Only on review branches. The source branch `review/wallet-rivals-20261127` was 133 commits ahead of main when this was written. | `git diff --stat origin/main...HEAD` |
| **[not built]** | No code exists. | — |

The installed release marker was `44aa7e2`, an ancestor of main, in the
[deep audit](DEEP-AUDIT-20261126.md) and still at 13:18 UTC on Nov 27 ([WALLET-DISSECTION.md](WALLET-DISSECTION.md) "Operations").
A file in `ops/systemd/` shows that a unit is defined, not that it is installed.
`ops/deploy.sh` never installs units.

**Conflicts between sources.** The later dated evidence wins. In particular:
- **Host.** [BLUEPRINT.md](BLUEPRINT.md) v2 places DOPPEL on two new boxes and leaves Apex alone. The build runs only on Apex.
- **Recorder.** v2 puts the tape in Rust. The Python recorder is primary ([ADR-0001](decisions/ADR-0001-m0-recorder-in-python.md)), and the Rust recorder is a shadow.
- **Fees.** v2 uses a PM US taker coefficient of 0.095. The measured value is 0.0695, with a 70% rebate to 2026-12-31 (E25).
- **Desk.** [ROADMAP.md](ROADMAP.md) calls the desk both merged and planned. A static desk builder exists, and the live console does not.
- **Retention.** The runbook's "14 days hot" is a plan. The deployment log records about 3 days rolling.

### Vocabulary

The documents use several numbering schemes. This one maps them onto each other.

| This document | Blueprint v3 stage | Tool | Earlier labels |
|---|---|---|---|
| Layer 0 · Select | 0 | `doppel-select` | G1, W8 |
| Layer 1 · Absorb | 1 | `doppel-absorb`, `doppel-goldsky`, `doppel-ledger` | C2, G0, M1 |
| Layer 2 · Inspect | 2 | `doppel-inspect`, `doppel-ordertape`, `doppel-ordermatch` | C5 (part) |
| Layer 3 · Dissect | 3 | `doppel-dissect` | C5 (part) |
| Layer 4 · Reverse-engineer | 4 | `doppel-rivals`, `doppel-rulefit` | C5, W2 |
| Layer 5 · Prove the machine | 5 | `doppel-answerkey` | — |
| Verify twin | 6 | `doppel-fidelity` | G2 |
| Strategy evaluation | — | `doppel-twin`, `doppel-genome result` | C7, C8, G4, M2 |
| Paper | 7 | [not built] | C9–C11, G5, M3 |
| Live | 8 | [not built] | C10, G6, G7 |

## 1. Purpose and non-goals

**Purpose.** DOPPEL tries to recover the trading rules of persistent, profitable
Polymarket international traders from public data. It proves that recovery against
known answers and forward data, and runs proven rules, or signals derived from them, on
US venues (Polymarket US, Kalshi). The thesis is in [BLUEPRINT-V3.md](BLUEPRINT-V3.md) §0.

The order of work is fixed:
1. **Research.** Each layer produces a result that can fail.
2. **Proof.** Out-of-sample agreement and after-fee economics are measured against preregistered windows.
3. **Money for us.** Paper first, then micro-live only when the owner arms it.
4. **Others later, maybe.** See §8.

**Non-goals, binding until the owner changes them in writing:**
- No orders and no live money unless the owner explicitly arms a specific strategy (G6). No agent can arm anything.
- No trading on Polymarket international. It is a read-only public data source ([BLUEPRINT.md](BLUEPRINT.md) §6).
- No selling, no external users and no customer data. §8 is a design note, not a plan.
- No dependency on LLM output for any decision. Models propose, and tests decide (§4).
- No new infrastructure without a measured need. The [working rules](../CLAUDE.md) favour the minimum that solves the problem.

## 2. Planes and their boundaries

There are six planes. Each owns its data and talks to the others only through the
contracts in §3. Data flows one way:

```
Data ──► Research ──► Registry & Evaluation ──► Execution ──► (venues)
  │          │                 │                     │
  └──────────┴─────────────────┴──────► Product surface (read-only)
                    Operations underpins all of them
```

The registry is the only bridge from research to money. A strategy that is not a sealed
genome with a binding verdict cannot reach the execution plane ([GENOME.md](GENOME.md)).

### 2.1 Data plane

**Responsibilities.**
- Record every market stream that cannot be backfilled.
- Seal each closed hour.
- Import reference series.
- Keep the evaluation inputs until their evaluations close.
- Archive sealed inputs.

**Current state.**

| Component | State | Reference |
|---|---|---|
| Python recorder, 15 streams: intl CLOB shards, RTDS ×2, PM US REST and WS, Kalshi books and live BRTI, spot ×3 | [Apex] primary | `ops/systemd/doppel-tape.service`, `src/doppel/tape/` |
| Rust recorder `doppel-taped`, run on a stream subset | Shadow only. It writes no hourly receipts. The 24-hour cutover comparison finished FAIL at 13:18 UTC Nov 27: 13 of 15 streams passed, and both RTDS streams failed on control frames. Whether the shadow unit kept running afterwards is not recorded. | `ops/systemd/doppel-taped-shadow.service`, `crates/doppel-tape/`, [WALLET-DISSECTION.md](WALLET-DISSECTION.md) "Operations" |
| Segment format and manifest | [main]. The v2 SHA-256 per segment revision is [review]. | `src/doppel/tape/segment.py` |
| Hourly receipts, schema v2: pinned bytes and SHA-256, `missing_expected_streams`, wait on the BRTI import watermark | [review]. Rehearsed on an isolated Apex copy. | `src/doppel/tape/receipts.py`, [DEEP-AUDIT](DEEP-AUDIT-20261126.md) |
| Raw BRTI import from PERELMAN, every 15 min | [Apex]. The staged, resumable importer is [review]. | `doppel-brti-import.*`, `src/doppel/lab/brti_import.py` |
| Settlement reference prices, every 10 min | [Apex] | `doppel-refprice.*` |
| Goldsky fill pulls with pagination record and spend ledger | [review] (bounded, locked ledger) | `src/doppel/lab/goldsky.py`, [ABSORB.md](ABSORB.md) |
| Free-space janitor | [Apex], an older version. The protected-day list and one-day-per-run cap are [review]. | `ops/tape_janitor.py`, `doppel-janitor.*` |
| Archive copy to a second filesystem | [review] tool, run by hand | `ops/archive_tape.py`, [RUNBOOK-M0.md](../ops/RUNBOOK-M0.md) |
| Object storage and Parquet compaction of tape (W12) | [not built]. Blocked on the owner. | [ROADMAP.md](ROADMAP.md) |
| Chain-coverage receipt: RTDS against on-chain fills | [not built] in receipts (`chain_completeness: "not_measured_until_M1"`). A one-off comparison exists. | `receipts.py`, `evidence/2026-11-26-chain-audit.json` |

Measured rates:
- The 15 archived streams wrote 567–655 MB per hour ([DEEP-AUDIT](DEEP-AUDIT-20261126.md)).
- At 13:26 UTC on Nov 27, hot tape had 7.10 GiB free against a 12 GiB floor. Root and archive had 27.09 GiB free against a 35 GiB reserve. Both were below their reserves ([WALLET-DISSECTION.md](WALLET-DISSECTION.md) "Operations").

**Target state.**
- A single recorder of record writes the receipts.
- Every closed hour has an immutable v2 receipt within a bounded delay (§6).
- A chain-coverage figure is in every hour's receipt.
- Sealed hours go to storage off the host.
- Retention is driven by the list of registered evaluation inputs, not by free space alone.

**Interfaces.**
- **In:** venue WebSockets and REST, RTDS, Polygon RPC, Goldsky, and the PERELMAN BRTI state, which is read-only.
- **Out:** segments, manifests, receipts and `status.json` under the tape root, plus the archive tree.
- **Contract:** §3.1.

**Invariants.**
- **D1.** Segments are append-only. A published manifest row is never rewritten. A later revision is a new row.
- **D2.** Every record carries the receive clocks `tw` and `tm`, and a CRC over the raw bytes as received.
- **D3.** A receipt is written once (`atomic_write(..., replace=False)`) and never claims a measure it did not take.
- **D4.** Retention never deletes a day listed as an evaluation input, `ref_*` streams, receipts or paid data.
- **D5.** Recorders hold no trading credentials and never send orders.

### 2.2 Research plane (Layers 0–5 as jobs)

**Responsibilities.** Turn tape and chain data into falsifiable statements about
traders. These cover who persists, what they did, what they could see, which rule
explains it, and how accurate the machine is on known answers.

**Current state.**
- Every layer runs as a CLI job by hand, mostly from review branches, on Apex scratch directories under `/var/backups/doppel-sealed-20261126/research/`.
- The only research job with a defined unit, `doppel-reconstruct`, is marked NOT INSTALLED.
- There is no job scheduler, queue or shared run manifest across layers.

| Layer | What exists | Latest result | Reference |
|---|---|---|---|
| 0 Select | Profit-blind scan, frozen cohort v1, prospective four-week wallet test with matched controls [review] | One candidate and one control registered. The verdict is due after four weeks. | [WALLET-SELECTION.md](WALLET-SELECTION.md) |
| 1 Absorb | Three-source reconciliation: fills, chain events and balances [review] | The candidate and control reconciled over 15 min and over one day. A 37 min 7 s gap has no Goldsky capture. | [WALLET-ABSORPTION.md](WALLET-ABSORPTION.md), [ABSORB.md](ABSORB.md) |
| 2 Inspect | Order tape (EIP-712 hash check), book matching with a shifted placebo [review] | The first candidate buy has two compatible additions, so its owner is unresolved. | [WALLET-INSPECTION.md](WALLET-INSPECTION.md), [RECONSTRUCTION.md](RECONSTRUCTION.md) |
| 3 Dissect | Pre-execution books at receipt time [review] | 33 of 35 signed maker orders reconstructed. Zero identified posting decisions. | [WALLET-DISSECTION.md](WALLET-DISSECTION.md) |
| 4 Reverse-engineer | Rival-rule registration and scorer; rule fitter with abstention [review] | Controls recovered. Both real wallets are unidentified, and the fitter abstained. | [WALLET-RIVALS.md](WALLET-RIVALS.md) |
| 5 Prove the machine | Answer-key lab with six scripted policies including a decoy and a hidden signal [review] | The three-seed exam is frozen and has not run. | [ANSWERKEY.md](ANSWERKEY.md), `evidence/2026-11-27-answerkey-inputs/EXAM.md` |
| Census and dossiers | `doppel-census`, `doppel-dossier`, `doppel-profile` [main/review] | G0 at 42/43 comparable wallets. The gate needs ≥50. | [BUILD-STATUS](BUILD-STATUS-20261126.md) |

**Target state.**
- Each layer is a job with declared inputs, outputs, code SHA, memory cap and exit codes.
- Each run writes a run manifest (§3.7) that the next layer must verify before reading, as `research_manifest.read_reconstruction` already does for one handoff.
- Jobs run on a research host separate from the recorders.

**Interfaces.**
- **In:** sealed tape hours (by receipt), fill stores, order tapes, and frozen cohort and token maps.
- **Out:** per-layer artifacts, each with a SHA-256 recorded in its manifest, and draft genomes or hypotheses with digests.
- **Constraint:** research never writes to the tape root or the registry, except through `doppel-genome register`.

**Invariants.**
- **R1.** No look-ahead. A decision at second d uses only data received by d (`tw`). Answer-key policy (f) breaks this on purpose, and its signal stays in `truth/`.
- **R2.** Inputs are read by verified digest. A changed checksum stops the job (`research_manifest.py`).
- **R3.** A layer reports unknowns as unknown, such as unresolved owners, censored cells and missing labels. It never fills them with an estimate labelled as a measurement ([RECONSTRUCTION.md](RECONSTRUCTION.md) "Identified, estimated, invisible").
- **R4.** Held-out data and truth files are unreadable to fitting code (`tests/test_answerkey.py` greps the public files).
- **R5.** Every job respects a memory budget below its unit cap. The 8 GB host also runs the recorders.

### 2.3 Registry and evaluation plane

**Responsibilities.**
- Seal strategies.
- Bind each strategy to a preregistered evaluation window and evaluator.
- Record binding results and sequential monitoring.
- Control false discoveries across families.

**Current state.**
- **Genome registry** [main], with hardening in [review]:
  - `registry/genomes.jsonl` holds 21 entries in four families, H1–H4. It is append-only and hash-chained, with each line's `prev` and `hash`.
  - Record kinds are `register`, `result`, `interim`, `early_pass` and `early_kill`.
  - `doppel-genome verify` and `doppel-genome dna` check the chain and the expression hashes in `registry/expression.json`.
  - Code: `src/doppel/strategy/registry.py`, `src/doppel/strategy/sequential.py`. The audited head was `7e4843a45ad1dc25` ([DEEP-AUDIT](DEEP-AUDIT-20261126.md)).
- **Evaluator binding** [review]:
  - `doppel-genome result` replays the canonical evaluator (`src/doppel/twin/evaluate.py`) against sealed tape.
  - It checks receipts, segment hashes and the token-map digest, and refuses `diagnostic_only` revisions.
  - New registrations pin the evaluator source hash. The 21 legacy registrations predate the pin ([GENOME.md](GENOME.md)).
- **Research protocols** [review]: a second sealing mechanism.
  - Layer 0–5 studies are frozen as `evidence/<date>/PROTOCOL.md` plus a machine-readable `registration.json`, committed to both mirrors before labels are opened.
  - Forecasts are sealed by digest, for example `real-forecast-seal.json`, and scorers take `--plan-sha256`.
  - These are not entries in `registry/genomes.jsonl`.
- **Audit log:** the registry file and git history. No separate audit log of operator actions exists.

**Target state.**
- One registry with three record types:
  - genome registrations and results, as today;
  - research protocol registrations, carrying the plan digest, commit and forecast seal;
  - promotion and demotion decisions with the owner's authorization reference.
- The registry head is anchored outside the repo, for example in a daily signed note in a second store. The docstring says an unanchored head cannot catch a rewritten last line.

**Interfaces.**
- **In:** a genome JSON, a family and window, code SHA, and sealed tape for results.
- **Out:** a binding verdict per genome, a family ledger (Holm), a sequential ledger (e-Holm) and the expression hash.
- **Contract:** §3.4–§3.6.

**Invariants.**
- **E1.** Registry lines are never edited or deleted. The first result per id is binding. Results bind only to the preregistered window, and a non-PASS never wins ([main] `3cff434`).
- **E2.** The evaluation window starts after registration. Every variant is charged to its family.
- **E3.** A behaviour change is a new genome with a parent link. The expression hash makes renamed duplicates visible.
- **E4.** Imported JSON and manual p-values are nonbinding.
- **E5.** Statistical PASS and the commercial hurdle (A6) are separate tests, and both are required. Owner authorization is separate from both.

### 2.4 Execution plane: NOT BUILT

**Responsibilities.**
- Run a sealed genome against live data on paper and, when armed, live.
- Use one strategy code path for both.
- Enforce risk limits independently of strategy code.
- Reconcile orders, fills, inventory and cash against the venue.
- Stop everything on command or on drift.

**Current state.**
- [not built] No order placement, signing or cancel code exists. A grep of `src/` and `crates/` for order POSTs, `/orders` and cancel calls finds none.
- The closest code is the simulator:
  - `src/doppel/twin/engine.py` is the PM US fill model: pessimistic queue, 250 ms latency, and caps on inventory and notional per window;
  - `src/doppel/lab/intltwin.py` is the intl replay;
  - `crates/doppel-fair` has the fast-fair engines, which are not wired in ([fast-fair.md](fast-fair.md)).
- The recorder units reference a PM US market-data signer socket (`/run/pmus-riskd-market-signer/markets.sock`) and a credential under `/etc/edge-engine/riskd/`. Both belong to another project on the host. DOPPEL uses only the read-only market-data signature.

**Target state** (from [BLUEPRINT.md](BLUEPRINT.md) C9–C11, ROADMAP W7, Carbon C7):

| Component | Requirement |
|---|---|
| Strategy runtime | Loads a sealed genome by id and verifies its expression hash at start. Feeds come from the same feature code as the twin. It emits intents only. Paper and live differ only in the gateway behind riskd. |
| Risk daemon (DOPPEL-owned) | The only holder of trading keys. Caps per strategy, venue, window and day. Global kill file. Idempotent client order ids. Reconcile loop at 1 s or less. GTD expiry on every resting quote, about 15 s, refreshed. Mass-cancel on a burst of adverse fills. Kill on clock drift. |
| Venue gateways | PM US and Kalshi. They return acks, fills and cancels to riskd and the order tape. |
| Reconciliation | Orders, fills, inventory and cash compared with venue state every cycle and after every restart. Any mismatch halts new orders. |
| Kill switch | A file or command checked before every order, plus venue-side expiry, so orders die if the host dies. |
| Sentinel | Per-fill markouts, e-processes and changepoints. Demotion is automatic. Promotion is never automatic. |

**Interfaces.**
- **In:** a sealed genome id with a binding PASS and owner arming, live feeds from the data plane, and fair values.
- **Out:** an order tape of intents, acks, fills and cancels; reconciliation reports; and demotion events into the registry.

**Invariants.**
- **X1.** No genome runs without a registry PASS for its exact id. No genome runs live without an owner arming record.
- **X2.** Keys live only in riskd. Strategy code, logs and agent prompts never see them.
- **X3.** Paper and live use the same strategy code. Paper must agree with the twin before live (G5).
- **X4.** Demotion is automatic and promotion is manual.
- **X5.** Every order expires on its own. A dead host leaves no resting risk beyond the GTD horizon.

### 2.5 Product surface: NOT BUILT beyond the static desk

**Responsibilities.**
- One place to see data health, wallet dossiers, the strategy book with verdicts, paper P&L and the audit trail.
- Every number links to its source.

**Current state.**
- `doppel-desk build` [main] reads `status.json`, the registry, twin verdicts and lab outputs into a deterministic `desk.json`. It renders through `desk/index.html.tmpl`.
- Its sections are recorder, strategies, evidence and calculator. Every block carries `src` (file, mtime) (`src/doppel/desk/build.py`).
- It is static and rebuilt by hand. It has no logins or roles.
- Not built: wallet dossier views, paper P&L, the audit-trail view and any write action.
- Nothing reads `vsmarket-latest.json` yet ([ROADMAP.md](ROADMAP.md) A3-9).

**Target state.**
- A read-only console served from the research host behind authentication, with a view for each of the five areas.
- Two roles:
  - **viewer**, who reads;
  - **owner**, who can arm or disarm a strategy.
- Arming is a signed request that riskd checks against the registry. The console holds no keys.

**Interfaces.**
- **In, read-only:** recorder `status.json` from both tape roots, `registry/genomes.jsonl`, twin verdict JSON, lab outputs such as census, adverse, clock-0 and xvenue, and disk readings.
- **Out:** `desk.json` and the rendered page.
- **Target out:** owner arming and disarming requests to riskd, recorded in the registry.

**Invariants.**
- **P1.** The console reads, it never computes a verdict. Numbers come from the registry or a sealed artifact and name their source.
- **P2.** The only write action is owner arming or disarming, and it is logged in the registry.

### 2.6 Operations plane

**Responsibilities.** Environments, release, monitoring, backups, capacity and
credentials (the last is covered in §7).

**Current state.**
- **One host.** Apex (`atlas-do`), shared with other projects.
  - The data volume is `/mnt/volume_nyc1_1793485122853/doppel/`.
  - The archive is `/var/backups/doppel-sealed-20261126` on the root filesystem of the same host.
- **Release.** `ops/deploy.sh`, by the lead only, from `main` only. Details are in §5.
- **Monitoring.** `status.json` per recorder (per-source msgs, reconnects, stalls, `last_msg_age_s`) and the static desk.
  - The runbook lists alert conditions ([RUNBOOK-M0.md](../ops/RUNBOOK-M0.md) "Health").
  - No code sends an alert. No pager or webhook exists, and a grep for them finds none.
- **CI.** None. The repo has no `.github/`. Checks run locally and inside `deploy.sh`.
- **Backups.** The archive copy is by hand and on the same host. Code is mirrored on GitHub and in the Apex git mirror `/srv/git/doppel.git`.
- **Resource caps.** systemd sets these per unit:

| Unit | Memory cap | CPU |
|---|---|---|
| tape | High 512M, Max 1G | 150% |
| shadow | High 512M, Max 1G | 150% |
| vsmarket | Max 1500M | — |
| reconstruct | High 2G, Max 3G, swap 0 | 100% |

**Interfaces.**
- **In:** a clean `main` checkout, both git mirrors (GitHub and `/srv/git/doppel.git`), `ops/requirements.lock`, and the unit files in `ops/systemd/`, which are installed by hand.
- **Out:** `/opt/doppel` with `DEPLOYED_COMMIT`, `/opt/doppel.old`, and timers that invoke `/opt/doppel/.venv/bin/*` and `/opt/doppel/ops/*`.
- **Observed through:** `status.json`, receipts, `df`, and `systemctl` state such as `NRestarts`.

**Target state.** Covered in §5–§7.

**Invariants.**
- **O1.** Only `main` is released, and only by the lead.
- **O2.** A failed release leaves the previous release running.
- **O3.** Every artifact is in git (GitHub plus the Apex mirror) or on Apex. Nothing lives only on a laptop or in scratch.
- **O4.** Capacity is checked against usable space after reserves before any job or release that writes data.

## 3. Data contracts

All money is integer micro-units in ledgers (1 share = 1 USDC = 10⁶). Clocks are ns
unless stated otherwise. A file is **immutable** once its digest is recorded somewhere
else: a manifest, receipt, registry line, protocol or git commit.

| # | Artifact | Format and location | Immutable after | Verified by |
|---|---|---|---|---|
| 3.1 | Tape segment | `<root>/<stream>/<YYYYMMDD>/<HH>.dtape.zst`. Independent zstd frames of NDJSON `{seq, stream, tw, tm, crc, raw, meta?}`. Control events carry `meta.ctl`. Python and Rust are byte-compatible. | Its manifest row (`file, sha256, bytes, records, raw_bytes, first_tw, last_tw, last_seq, closed_tw`) | `doppel-tape verify` (SHA-256 and CRC). `read_segment(verify=True, strict=True)`. |
| 3.1 | Hourly receipt | `<root>/receipts/YYYYMMDD/HH.json`, `schema_version` 2. Per stream: records, control events, longest silence, CRC validity, pinned bytes and SHA-256. Also `missing_expected_streams` and `rtds_pair` coverage. | Written once | The evaluator checks receipt membership and segment hashes. `archive_tape.py --require-receipts`. |
| 3.1 | Import ledgers | `ref_brti_raw/import-watermark.json` and `import-amendments.jsonl`. Late observations keep their original receive time. | Append-only | Receipt waits on the watermark [review] |
| 3.2 | Order tape | Parquet `day=YYYY-MM-DD/part-NNNNNN.parquet` plus `_state/flush-*.json`. Schema `ordertape.SCHEMA` (`tx, block, order_id, maker, signer, side, size, limit, created_ms, ts_flag, verified, …`). | Its flush manifest | Recomputed EIP-712 hash equals the fill's `order_id` (`verified`). `created_ms` is self-reported and screened by `ts_flag`. |
| 3.2 | Fill store | Parquet per tape segment, compacted from Goldsky pages. State file lists byte sizes. | Re-derivable from tape | Pagination record `goldsky_activity/pagination.jsonl` (`complete` only without `next_cursor`) |
| 3.3 | Wallet ledger | `wallet-<addr>.json`, `cohort.tsv/json` from `doppel-absorb` | No tool seal. The tool records no output digests, so outputs are sealed only when an evidence receipt or commit records their hashes. | `reconciled` only when every rule in [ABSORB.md](ABSORB.md) §Definitions holds. Otherwise `not_reconciled` with `causes`. |
| 3.4 | Genome | JSON. Id = content hash of `canonical()`. Fields added later are omitted at defaults. | Registration | `doppel-genome verify` (id), `doppel-genome dna` (expression hash on 4,000 seeded probes) |
| 3.5 | Registry record | One line in `registry/genomes.jsonl`: `{kind, tw, genome_id, family, …, prev, hash}` | Append | `Registry.verify()` re-chains every line and re-hashes every genome |
| 3.6 | Evaluation protocol | Genomes: `eval_window`, `family`, `code_sha`, evaluator hash in the `register` line. Research: `evidence/<date>/PROTOCOL.md`, `registration.json` and forecast seals. | Commit on both mirrors, before labels or the window open | Scorer `--plan-sha256`. The evaluator source hash is recorded with each result. |
| 3.7 | Run manifest | Reconstruction: `inputs` (with store-state SHA-256), `outputs` (per-file SHA-256), `counts`, `GATES`, code, `peak_rss_mb`. Answer-key and rivals: scorecards and verification JSON with digests. | Job end | `research_manifest.read_reconstruction` refuses unfinished runs, changed checksums and out-of-range intervals |
| 3.8 | Evidence | `evidence/<date>/…` with protocol, receipts and digests. FINDINGS rows E1–E39. | Commit | Reproduction commands in each document |

**Gaps.**
- `schemas/` contains only `.gitkeep`. Every contract above lives in module docstrings and code, with no machine-readable schema.
- The target is a JSON Schema per artifact in `schemas/`, tested against producers. This is [not built].
- The Rust recorder's compatibility is tested (`crates/doppel-tape/tests/compat.rs`), but it writes no receipts.
- No contract exists yet for the execution plane: intents, acks, venue fills, reconciliation reports and arming records. Each must be specified here before the plane is built.

## 4. Trust model

Each stage claims one thing and carries a test that can fail. A model may propose a
rule, threshold or explanation. Only a deterministic test on data the proposal never saw
may accept it ([BLUEPRINT-V3.md](BLUEPRINT-V3.md) §6; ROADMAP A4, A7).

| Stage | What it proves | Mechanism | What it cannot prove |
|---|---|---|---|
| Tape | These bytes were received at these times and not altered | CRC per record, SHA-256 per segment revision, append-only manifest, write-once receipt | Completeness against the chain, which is not yet measured per hour. That the venue sent truthful data. |
| Receipt-bound input | An evaluation read exactly the sealed bytes | Receipt membership and v2 hashes. `diagnostic_only` blocks binding use. | Ten H1 hours lack original seals and stay diagnostic ([DEEP-AUDIT](DEEP-AUDIT-20261126.md)) |
| Absorption | A wallet's fills explain its on-chain share and cash balances | Three sources agree: Goldsky with terminal pagination, chain logs and `eth_call` balances | Beneficial ownership. Activity on V1 contracts. |
| Order tape | A filled order's terms are genuine | EIP-712 hash matches the fill's order id | `created_ms`, which is self-reported. Orders that never filled. |
| Book match | A placement is likely at a time | Match rate against a shifted placebo in a shared denominator | Who placed an anonymous addition. Cancel versus replace. |
| Rule recovery (Layer 4) | One registered rule beats a rival where they disagree | Preregistered rivals, sealed forecast, exact sign test, always-silent baseline | Rules outside the registered family. Size, cancel and inventory policy. |
| Answer key (Layer 5) | How often the machine recovers known rules, and how often it is confidently wrong | Scripted policies on real books, decoy and hidden-signal subjects that must not get a confident rule | Real traders' accuracy. That needs independent decision records. |
| Fidelity (stage 6) | A frozen genome behaves like the wallet on later markets | Activity, one-to-one fills, position path and P&L, each against a same-window chance baseline. INCONCLUSIVE on thin data. | Causality. Live agreement. |
| Strategy verdict (G4) | After-fee edge on future windows | Preregistered window, block bootstrap, Holm per family, e-processes with Ville bound, commercial hurdle | Execution realism beyond the fill model. The queue model is not yet calibrated (4/65 over the pessimistic bound, [BUILD-STATUS](BUILD-STATUS-20261126.md)). |
| Paper (G5) | Live behaviour matches the twin | ≥14 days, paper against twin, e-value ≥20 | [not built] |
| Micro-live (G6) | Our own fills match the twin's predicted band | Owner-armed probes | [not built] |

**Rules that hold across stages.**
1. A gate reads counts before rounding. Too little data is INCONCLUSIVE, never PASS.
2. Every comparison names its placebo or baseline. A guard must beat a rate-matched placebo (A7).
3. Development cases are never exam cases. Changing a fitter after seeing an exam requires a new exam (`EXAM.md`).
4. Stated uncertainty is part of the output: unknown, estimated or censored.

## 5. Environments and release

### Today

There is one environment, which is production on Apex. Development happens on the Mac
and in scratch checkouts.

Release steps on **main** (`git show origin/main:ops/deploy.sh`):
1. Refuse a dirty tree or any branch other than `main`.
2. Run pytest and pyright.
3. Check the lock file.
4. Push to GitHub and the Apex mirror, and verify that both match HEAD.
5. `git archive` over ssh into `/opt/doppel.new`.
6. Build the venv.
7. `mv /opt/doppel /opt/doppel.old`, then `mv /opt/doppel.new /opt/doppel`.
8. Restart the recorder only with `--restart-tape`.

Step 7 is two renames, not an atomic swap, and nothing rolls back on a failed start.

The installer on **[review]** (`ops/install-release.sh`):
- takes a lock;
- verifies that the mirror's main equals the revision;
- exchanges directories atomically (`renameat2 RENAME_EXCHANGE`);
- checks `doppel-tape --help`;
- with `--restart-tape`, checks that the unit made no new restarts within 20 s;
- restores the previous tree on failure.

It was staged on macOS and Apex, with the package install and service simulated on Apex
([release staging receipt](../evidence/2026-11-26-release-staging.json)).

**Gaps.**

| # | Gap |
|---|---|
| G-1 | No staging host. Every release is tested first in production. |
| G-2 | `deploy.sh` runs no Rust checks (`cargo test`, `clippy`, `fmt`). The Rust binary is built by hand in `/srv/doppel-build` and installed to `/opt/doppel-bin`, outside the release swap and outside `DEPLOYED_COMMIT` ([RUNBOOK-M0.md](../ops/RUNBOOK-M0.md)). |
| G-3 | Systemd unit changes need a separate manual install and daemon reload. The installed units may differ from `ops/systemd/` ([DEEP-AUDIT](DEEP-AUDIT-20261126.md) release step 2). |
| G-4 | Only one rollback generation (`/opt/doppel.old`). |
| G-5 | Installed `44aa7e2` lags main `3cff434`, and the reviewed repairs (receipts v2, importer, janitor, installer) are not on main. |
| G-6 | No CI. Nothing checks a review branch automatically before a human merges it. |
| G-7 | A recorder restart is a capture gap on the only recorder. No canary runs beside it. |

### Target

| Step | Gate |
|---|---|
| CI on every push to a review branch | `uv run pytest -q`, `uv run pyright src`, `cargo test --locked`, clippy with `-D warnings`, `fmt --check`, `doppel-genome verify` and `doppel-genome dna` |
| Merge to main | Human review plus green CI |
| Build a release artifact | One versioned bundle from one SHA: wheel, Rust binaries and units. Its digest is recorded. |
| Staging host | Install the bundle. Run recorders on a stream subset for 1 h. Verify one closed hour gives a v2 receipt that passes `doppel-tape verify`. |
| Recorder canary in production | Start the new recorder version beside the old one on its own root, as the shadow unit was set up to do. Cut over only after an hourly comparison with `doppel-tape-compare` passes on every stream, RTDS included. |
| Production install | Atomic exchange plus unit install plus daemon reload. Post-activation checks. Keep N=3 prior releases. |
| Post-release check | The next closed hour has an immutable receipt, a matching SHA-256 and an advancing import watermark (the audit's release step 2) |

## 6. Reliability targets (proposed SLOs)

None of these is measured continuously today. Each one is anchored to an existing number
or rule. Until an alert sender exists, the "page" column says what a human must check.

| SLO | Target | Today | Page when |
|---|---|---|---|
| Identity capture completeness: RTDS against on-chain fills, per hour | ≥99.5% over 24 h (M0 gate) | 89.2% in one 14-minute comparison. Not in receipts. | Any hour <99.5%, or no measurement for 2 h |
| RTDS socket agreement | `tx_union` coverage per socket ≥0.99 | In receipts as `rtds_pair` | <0.99 on both sockets (runbook) |
| Stream liveness | `last_msg_age_s` ≤30 s on active markets | In `status.json` | >30 s on any source (runbook) |
| Receipt sealing latency | Receipt for hour H exists within 45 min of H+1, given the 15-min import timer | Not measured | Missing after 60 min |
| Receipt integrity | 100% of closed hours have a write-once v2 receipt that passes verification | 13 of 23 Nov 26 hours had binding input evidence | Any `pending_unsealed_streams` or `missing_expected_streams` |
| Disk headroom: hot tape | Free above the 12 GiB floor ≥ 48 h of writes at the measured 655 MB/h (about 31 GB) | 7.10 GiB free, below the floor (13:26 UTC Nov 27) | Below 24 h of writes, or the janitor exits 1 |
| Disk headroom: archive | Usable space above the 35 GiB reserve ≥ the remaining registered hours | 27.09 GiB free, below the reserve (13:26 UTC Nov 27) | Below the next 24 h of registered inputs |
| Evaluation inputs retained | 100% of registered hours present and verified until their result is recorded | Ten H1 hours diagnostic only | Any registered hour missing or failing preflight |
| Job memory | Every job peak below its `MemoryHigh` with swap 0 | rulefit 433.7 MiB ([WALLET-RIVALS](WALLET-RIVALS.md)). Baseline 1.26 GB under a 1.5 GB cap ([RULE-TWIN-REVIEW](RULE-TWIN-REVIEW-20261127.md)). | Any OOM kill or `--mem-budget-mb` stop (exit 2) |
| Recorder restarts | ≤1 unplanned restart per day per unit | `NRestarts` is visible in systemd | >3 in 1 h |
| Execution (when built) | Reconcile mismatch 0. Kill-switch to all-cancelled ≤1 s plus venue latency. | [not built] | Any mismatch, or any order without GTD |

**To build:** a small alert sender that reads `status.json`, receipts and `df`, sends
to one owner channel, and is tested by a drill. This is the M0 watchdog drill in [BLUEPRINT.md](BLUEPRINT.md) §4.

## 7. Security

### Credentials today

| Credential | Where it lives | How DOPPEL gets it | Notes |
|---|---|---|---|
| Kalshi read-only API profile | `/etc/edge-engine/riskd/kalshi-production-readonly.json` on Apex | systemd `LoadCredential=` into `$CREDENTIALS_DIRECTORY`, readable only by the unit | Shared with PERELMAN and btc-window. The shadow unit's subset drop-in avoids doubling REST load (`doppel-taped-shadow.service.d/subset.conf`). |
| PM US market-data key | Held by the host signer daemon | Unix socket `/run/pmus-riskd-market-signer/markets.sock` via `SupplementaryGroups=pmus-market-data`. DOPPEL receives a per-connect signature, never the key. | Header values are scrubbed from status, logs and tape. Redirects are refused. |
| Goldsky feeds key | Environment variable `GOLDSKY_FEEDS_API_KEY` (`src/doppel/lab/goldsky.py`) | Set by the operator per run. The repo does not show how. | Spend is bounded by an append-only ledger (one-time $100 credit, 7M-row default cap) |
| Polygon RPC | Free public endpoints | None | — |
| Trading keys | None | — | DOPPEL holds none |

Other facts:
- `doppel-brti-import.service` runs as `root` to read PERELMAN's DynamicUser state. `ReadOnlyPaths` makes only that directory read-only. The unit sets no `ProtectSystem`, `NoNewPrivileges` or memory cap.
- The recorder units set `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, and narrow `ReadWritePaths`. The shadow can write only its own root.
- Releases reach the host over ssh as `atlas-do` (`ops/deploy.sh`). The repo defines no access logging.

### Rule

**No credential ever enters code, logs, tape, evidence, commits or agent prompts.** The
first two are already a [CLAUDE.md](../CLAUDE.md) invariant. This document extends the rule. Agents
never read credential files, never run with trading credentials in their environment,
and never print exception text that could contain request headers.

### Target

| Item | Target |
|---|---|
| Secret store | Credentials in a secret manager or an encrypted systemd credential (`LoadCredentialEncrypted=`), never plain files under another project's path |
| Least privilege | One credential per consumer: a DOPPEL-only read-only Kalshi key. No root unit. Import BRTI through a read-only export from PERELMAN or a group-readable file. Research hosts get no venue keys. |
| Trading keys (when built) | Only riskd holds them, on the trading host, with withdrawal permissions disabled at the venue |
| Rotation | Documented rotation for every key, at least quarterly and on any suspected exposure. A rotation drill before live. |
| Access logs | Log who read which credential, and every ssh session and release. Keep the logs off-host. |
| Separation from other projects | DOPPEL stops depending on edge-engine paths and sockets before live. The trading host is DOPPEL-only. |

## 8. Multi-tenant later (deferred)

**Explicitly deferred until DOPPEL makes money for us.** Nothing below is to be built
now. This section exists so that today's choices do not block a later version.

| Area | What would change |
|---|---|
| Account isolation | A tenant id on every registry record, artifact and run manifest. Separate execution plane and keys per client. No shared inventory or cash. |
| Data entitlements | Venue market-data licences may forbid redistribution of recorded books and prints. Each stream needs a licence review before any client sees it, raw or derived. |
| Wallet dossiers | Dossiers profile identifiable on-chain addresses. Offering them to others raises privacy and conduct questions. **Needs legal review.** |
| Billing | Metering per tenant (jobs, storage, strategies) and invoices. Not designed. |
| Compliance review | Offering strategies, signals or execution to others may be regulated activity, such as advice, management or brokerage. Venue rules may restrict third-party or automated access. **Needs legal review before any design work.** |
| Venue eligibility | Who may trade which venue depends on jurisdiction and venue rules. DOPPEL itself trades only US venues and reads intl only as public data (§1). Client eligibility checks are **subject to legal review**, and this document makes no legal claim. |
| Product surface | Per-tenant roles, SSO and an audit trail exported to the client |

Choices now that keep this open at no cost:
- Contracts are versioned (`schema_version`).
- Every artifact carries its provenance.
- The console is read-only with explicit roles.
- Keys sit behind a single riskd.

## 9. Roadmap mapped to planes

The order follows the audits' release sequence and the existing gates. Each step ends on
its acceptance test or a recorded kill. A step does not start until the one it depends
on has passed.

| # | Plane | Step | Acceptance test |
|---|---|---|---|
| 1 | Ops, Data | **Capacity.** Owner decision on Apex storage (BLUEPRINT-V3 §9.1). | Usable space above both reserves ≥ the remaining registered hours plus 41 GB lab scratch ([RULE-TWIN-REVIEW](RULE-TWIN-REVIEW-20261127.md)), measured with `df` and recorded |
| 2 | Ops | **Release the reviewed integration** to main and Apex: receipts v2, importer, janitor with protected days, installer, evaluator pinning | The audit's release step 2: the next real closed hour yields one immutable v2 receipt with matching SHA-256 and byte length, passing CRCs, an advancing watermark and active capture. Units reconciled with `ops/systemd/`. |
| 3 | Ops | **CI** with the full check list in §5 | A deliberately failing test blocks a review branch |
| 4 | Ops | **Alert sender** for §6 | A drill (stop one stream, fill a test volume) pages within 5 min |
| 5 | Data | **Chain coverage in receipts** | 24 h at ≥99.5% identity-transaction coverage against independent chain data (M0 gate) |
| 6 | Data | **Off-host durable storage** (W12) | A sealed hour restores from the second store and passes `doppel-tape verify` and the evaluator preflight |
| 7 | Registry | **Score H1–H4 at their closes:** H1 Nov 29, H2/H3 Nov 30, H4 Dec 1, 00:00 UTC | Binding result per genome with family Holm, traded-window count, drawdown and commercial hurdle. Incomplete inputs give INCONCLUSIVE. |
| 8 | Research | **Layer 5 exam:** three reserved seeds | Per `EXAM.md`: zero wrongly confident cells, explicit results for both negative controls, every positive control decided |
| 9 | Research | **Layer 1 at scale (G0)** | ≥95% reconciled on ≥50 comparable wallets |
| 10 | Research | **Layer 0 verdict (G1)** | Candidate beats matched controls in 3 of 4 prospective weeks, with the bootstrap lower bound >0 (`selection_plan.DECISION`) |
| 11 | Research | **Layers 2–4 on a persistent wallet,** with independent decision evidence | A real rule beats its rival on held-out markets under a sealed forecast. Then a stage-6 fidelity PASS on later markets. |
| 12 | Data, Research | **Fast fair wired in** (W6). Spot L2 streams recorded. | Replica within $0.01 of published BRTI at p99. Latency within the 1 ms budget ([fast-fair.md](fast-fair.md)). |
| 13 | Registry | **One registry for genomes, research protocols and promotions.** Head anchored off-repo. | `verify()` covers all three record types. A rewritten last line is detected against the anchor. |
| 14 | Execution | **Paper runtime + riskd + reconciliation + kill switch** (W7), paper only, for a family with a binding PASS | Exercise limits, kill switch, partial fills, lost acks, restart and inventory reconciliation in a test harness. Then G5: ≥14 days of paper against twin, e-value ≥20. |
| 15 | Product | **Console v1**, read-only, with the five views and roles | Every number links to a registry line or sealed artifact. The arming action is logged in the registry. |
| 16 | Execution, Security | **Micro-live probe,** owner-armed (G6) | Probe fills within the twin's predicted band. Separate trading host, DOPPEL-only keys, rotation drill done. |
| 17 | All | **Scale (G7)** | Marginal dollars >0 under stressed fills. Automatic demotion proven on a synthetic decaying genome (W9). |
| — | All | **Multi-tenant** (§8) | Not scheduled. Requires a profitable G7 and legal review. |
