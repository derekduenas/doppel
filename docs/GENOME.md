# The DOPPEL genome: a strategy's DNA

A genome is the only thing that crosses from research to money. The biology metaphor is literal
about one thing only: **the genotype fully determines the behaviour, and that mapping is pinned.**

| Layer | What it is | Where | Pinned by |
|---|---|---|---|
| Genotype | Executable strategy JSON: universe, taker rules (conditions over the `FEATURES` vocabulary), maker rule, exit, risk, costs, kill rule, evidence, gates | `strategy/genome.py` | content hash = genome id (`canonical()`); fields added later are omitted at defaults so old ids never move |
| Expression | The interpreter: feature vector → quotes / pauses / takes | `Genome.maker_quotes`, `maker_paused`, `taker_actions` | **expression hash**: decisions on a fixed seeded probe set (4,000 vectors spanning every feature and regime), stored per sealed genome in `registry/expression.json`; `tests/test_dna.py` fails if any sealed genome's behaviour changes. The same probe set is the golden spec for the Rust interpreter |
| Environment | Features from the tape, fills, fees, latency | `twin/features.py`, `twin/engine.py`, `lab/flow.py`, `lab/fair.py` | twin tests + registry `code_sha`; golden replay of a committed tape slice is next |
| Phenotype | How it actually trades: net ¢/window and /contract, contracts/window, traded share, fill rate, short and 30 s markouts, stand-downs, venue share, drawdown | `dna.phenotype(verdict)` | fixed key set `PHENOTYPE_KEYS` |
| Fitness | Statistical verdict (block-bootstrap CI, Holm), anytime-valid e-processes (e-Holm), commercial hurdle | `twin/verdict.py`, `strategy/sequential.py`, registry | preregistered future windows, hash-chained registry |
| Lineage | Parents and operator (`mutate:<field>`, `crossover:<sections>`, `extract`, `manual`) | `Source.parents`, `Source.operator`, `dna.mutate`, `dna.crossover` | part of the id when set; siblings are charged to one family |
| Selection and decay | Promotion through gates; live-vs-twin phenotype drift triggers a swap | registry + `dna.phenotype_drift` | owner arms live; drift thresholds preregistered per family |

## Rules

1. Rules are data over named features; nothing executable hides in evidence or notes.
2. A sealed genome's id, expression hash and registration never change. A behaviour change is a new
   genome with a parent link, charged to its family.
3. Two genomes with the same expression hash are the same strategy, whatever their names. Registration
   prints a note when that happens (found on day one: `h3-base` = `h1-maker-h2-guard30`,
   `h3-ident` = `h2-maker-h2-flow100`).
4. Adding a feature requires adding it to the probe set and bumping `PROBE_VERSION`; existing hashes
   are re-derived only by an explicit, reviewed migration, never silently.

## Recording a final verdict

After the preregistered window closes, use `doppel-genome result --id ID --data SEALED_TAPE`.
For intl-flow genomes, also supply `--lab FROZEN_LAB` with the frozen token map. The command
replays the registered genome with the canonical settings and checks each segment against its
manifest and reads through the compressed frames with CRC verification. Every evaluation hour
needs a closed receipt covering the genome's market horizons.
The evaluator checks the receipt's recorded input streams against the snapshot. The registry
records the evaluator source hash, settings and a digest of its inputs.
New CLI registrations pin the evaluator before the window opens. Earlier registrations lack this
pin and report that fact in their result metadata. Imported JSON and manual p-values are nonbinding.

## What a genome cannot express yet (v2 schema work)

Hedge legs on another venue (S5, S6), side selection by condition (quote one side when flow is
one-sided), size schedules by time to close, and multi-contract structures (S8). These land as new
optional sections with defaults omitted from the id, so v1 ids stay valid.
