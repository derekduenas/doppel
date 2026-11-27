# DOPPEL project status · November 26, 2026

**DOPPEL is in M2 strategy validation, with M0 data coverage and M1 wallet evidence still open.**
The team can record markets, reconstruct wallet activity, and replay a registered BTC strategy.
It has yet to demonstrate a repeatable after-fee return on a US venue. No strategy has qualified
for shadow trading. The most urgent delivery risk is whether the registered trials can be scored
from the inputs retained on Apex.

This assessment uses the [current published blueprint](site/blueprint-v3.html), the
[milestone and architecture specification](BLUEPRINT.md), and the [workstream roadmap](ROADMAP.md).
Where an old status label conflicts with a run or registration, the dated evidence below controls.

## Milestone board

| Milestone | What exists | Evidence still needed to close it |
| --- | --- | --- |
| **M0 · Market recording** | Python primary and Rust shadow recorders active. The original Apex archive contains 586 checksum-verified segments, 20.3 GB. Three additional hours through 22 UTC are preserved. The repaired importer and recorder passed an isolated Apex rehearsal. | Demonstrate 24 hours of ≥99.5% identity-transaction coverage against independent chain data. The audited hour lacked that denominator. The earlier 14-minute chain comparison measured 89.2% overall. Secure enough usable Apex capacity for the registered inputs and verify the repaired seals on the live recorder. |
| **M1 · Wallet ledger and census** | The pilot reconciled 42/43 comparable wallets (97.7%). Its corrected sample found about $61.7k of aggregate maker profit per day on international markets, on one comparable day per wallet. Cohort v1 is frozen. | G0 needs ≥95% on ≥50 comparable wallets. G1 needs post-freeze performance against matched controls in three of four rolling windows. The size of the pool transferable to US venues is unknown. |
| **M2 · Fair value, strategy registry and replay** | BTC settlement model, flow features, fill simulator and sealed registry run. Four families contain 21 variants. H1's corrected interim check covered 58 eligible windows and all six variants lost $25.3–$28.7 apiece. | H1 needs contemporaneous seals for ten hours. H2–H4 still need admissible tape through their closes. For these families, G4 requires ≥300 traded windows, a positive lower 95% confidence bound on after-fee returns, and family-adjusted statistical evidence. Check the commercial hurdle separately. Validate execution assumptions and full-depth fast fair. |
| **M3 · Shadow trading and risk controls** | The research desk runs against Apex, and statistical monitoring exists. The shadow process currently operating on Apex records market data. | Build the shared paper/live strategy runtime, risk daemon, venue gateways, order and inventory reconciliation, restart recovery and automatic demotion. G5 requires ≥14 days of paper-versus-twin agreement and an e-value ≥20. G6 micro-live requires the owner to arm it. |
| **M4 · Operator inversion and cloning** | Twelve pilot dossiers yielded two explained, six partial and four with insufficient data. Order matching, reconstruction, rule fitting and clone-fidelity evaluation are implemented in the review tree. | Select persistent operators, validate placement/expiry against independent order IDs, and evaluate a frozen rule on a disjoint period with independent fill coverage. No real clone has a passing verdict. A substantial part of the dossier history predates the book tape. |
| **M5 · Learned simulator, research agents and sports** | Design and proposals. | Learned-counterparty calibration, measured proposal survival and sports sensor parity remain downstream. These do not unlock the first BTC family decision. |
| **M6 · Perpetuals** | The P0 study joined two public trades to fills from one known account. | P1 needs recording on both venues and a receipt measuring how much of the tape is attributable. Fair value, census, transplant and shadow testing follow. |

Evidence for the board comes from the [corrected census](reports/census-v0-20261125.md),
[research findings E13, E19–20, E28 and E35–39](../evidence/2026-11-25-probes/FINDINGS.md),
[system audit](SYSTEM-AUDIT-20261126.md), [registration ledger](../registry/genomes.jsonl)
and [perps identity study](research/perps-p0.md).

## P0 · H1 lacks admissible inputs for ten hours

The [deep audit](DEEP-AUDIT-20261126.md) reconciled the twelve discrepancies described below.
All 22 closed hours through 21 UTC passed diagnostic input checks on a separate Apex copy.
Twelve have original seals, including two recovered historical revisions. Ten have
corroborating upstream data and lack the original checksum. A binding replay refuses those
ten. A separate input check also passed for 22 UTC, bringing the subtotal with acceptable
input evidence to 13 of 23. H1 remains blocked. Only 332 MiB remained above the archive
reserve at 23:05 UTC. The next 96 full hours alone project to 51–59 GiB at measured rates.
The release lead should verify retention capacity before deploying the repaired cleanup
and sealing jobs. [Retention forecast](../evidence/2026-11-26-retention-forecast.json)

### Original archive finding at 22:34 UTC

At 22:34 UTC the review evaluator's input check, `verify_hours`, passed **8 of 20** archived H1
hours for Nov 26 (00–19 UTC inclusive). All 12 failures were on the raw Bitcoin reference series
`ref_brti_raw`, at 00–06 and 12–16 UTC. The hourly receipts describe fewer bytes than the final
segments. At 00 UTC, for example, the receipt lists 81,836 bytes and the retained file has 108,632.
The hot manifest records multiple revisions as the importer appends observations to their original
publication hour. The later file can pass its own SHA-256 and still fail the evaluator's check
against the earlier receipt. This check ran without opening strategy results.

For hours 00 and 01, the original receipt-length prefix matches a prior manifest checksum.
The other ten have no matching size in the retained hot manifest. Recovery may still be possible
from the authenticated final bytes, original receipts and upstream receive-time history.
Preserve all versions, verify frame integrity, record counts and timestamps, and document which
hours have an original seal or a reconstructed one. Preserve an explicit evidence gap wherever
the earlier input cannot be established. For new hours, finalize receipts against an immutable
import revision and record late additions separately. [Hour-by-hour preflight](../evidence/2026-11-26-h1-input-preflight.json)
· [Canonical check](../src/doppel/twin/evaluate.py) · [Importer](../src/doppel/lab/brti_import.py)

The latest archive receipt covered 19 UTC and the hot recorder had receipts through 21 UTC.
Apex reported 24 GiB free on the tape volume and 38 GiB on the archive filesystem. The hot-volume
janitor can prune closed days below its free-space floor. Retaining the rolling evaluation inputs
and the frozen token map on the authorized Apex disks is time-critical. Both copies share a host.
Archive integrity and chain completeness require separate checks. [Retention runbook](../ops/RUNBOOK-M0.md)

## Delivery order and decision dates

| Priority and proposed lead | Deliverable | Acceptance or decision |
| --- | --- | --- |
| **Now · Data engineering and operations** | Protect each required closed hour, manifest revision, original receipt and frozen token map on Apex. Resolve archive capacity, then release and verify the prospective sealing repair. | Admissible inputs for every required hour. For H1, carry forward the ten missing original hashes as an input failure unless contemporaneous seals are found. |
| **At each registered close · Strategy research** | Score H1's six variants after **Nov 29 00:00 UTC**, H2's six and H3's five after **Nov 30 00:00 UTC**, and H4's four after **Dec 1 00:00 UTC**. | Record the canonical decision, family correction, traded-window count, drawdown and commercial result. Incomplete inputs or insufficient observations cannot qualify a family for shadow. The existing families predate evaluator-hash pinning. |
| **In parallel · Ledger and research** | Complete G0 on the expanded comparable cohort, score G1 on post-freeze days, and measure US-transferable net economics. | ≥50 comparable wallets for G0, and a documented G1 result against controls. The blueprint proposes a $500/day transferable-net threshold paired with a test of whether H1 shows an international-to-US lead. The owner threshold is still open in the project record. |
| **In parallel · Replay and market data** | Seek existing order-owner acknowledgements, register a meaningful prospective sample, and evaluate queue behavior. Validate the BTC replica against published BRTI and reconcile venue fees and rebates to statements. | The latest replay repaired 30/32 to 32/32 on discovery, but only 3/14 known-end orders had full price allocation and the holdout had zero eligible orders. In the earlier 13-minute run, 4/65 exceeded the pessimistic bound (6.15%, limit 5%) and processing took 16m 23s. Keep routine reconstruction off. Full-depth replica best p99 was 1.85 ms against a 1 ms budget. Published-index accuracy at the $0.01 p99 target is open. |
| **Before shadow promotion · Runtime and risk** | Prepare a paper-only engine that uses the same strategy code intended for live. Exercise limits, kill switch, partial fills, lost acknowledgements, restart and inventory reconciliation. | Demonstrate paper/twin agreement and the G5 observation period for a qualifying family. Gate funded execution and subsequent scaling separately. |

The operator-cloning track has two further tests. G2 asks whether public data predicts an
operator's actions out of sample. G3 asks whether DOPPEL receives that signal before the
target venue reprices. Neither has an accepted real-clone result. The blueprint's point-in-time
graph and live wallet-label service also lack a demonstrated production deployment and latency
measurement. The research desk and statistical monitor can already support diagnosis. Portfolio
allocation, live inventory reporting and automatic demotion depend on the execution plane.

The short registered trials give the first family decisions. The original M2 plan also calls for
at least two weeks of tape for broader validation. Keep the stronger multi-asset, multi-venue and
capacity claims open. The first Kalshi hedge design already lost 1.0–2.2¢ per pair across 16
windows, with 64–87% hedge misses. Shelve that direction. A reverse-leg design needs its own
registration and execution model. [Hedge study E38](../evidence/2026-11-25-probes/FINDINGS.md)
· [Fast-fair benchmarks](fast-fair.md) · [Queue-calibration trial](../evidence/2026-11-26-quote-expiry/RESULTS.md)

## Release position

The code and verification receipts are on `review/system-audit-20261126`. Main is
`3cff434` and the installed Apex release marker is `44aa7e2`. The archive stays on Apex
under the owner's retention decision. The [deep audit](DEEP-AUDIT-20261126.md) records
the tested changes, capacity position, release checks and outstanding evidence.
