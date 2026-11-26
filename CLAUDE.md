# DOPPEL: working rules for agents

Read `docs/BLUEPRINT.md`, `docs/ROADMAP.md`, `docs/GENOME.md` and `evidence/2026-11-25-probes/FINDINGS.md` before changing anything.

## How to work (Karpathy guidelines, adopted 2026-11-26)
1. Think before coding: state assumptions; if a request has two readings, say so; push back when a simpler approach exists.
2. Simplicity first: the minimum code that solves the problem. No speculative features, abstractions or configurability.
3. Surgical changes: touch only what the task needs; match existing style; mention unrelated problems instead of fixing them.
4. Goal-driven: turn each task into a check (a test that reproduces the bug, a number that must match) and loop until it passes.

## Invariants (never break)
- No look-ahead: a decision at second d uses only data received by d (`tw`).
- Sealed genomes and `registry/genomes.jsonl` are append-only; `doppel-genome verify` and `doppel-genome dna` must pass.
- No orders, no credentials in code or logs, no deploys from agents; Apex deploys go through `ops/deploy.sh` by the lead only.
- Stage explicit paths (never `git add -A`); everything lives in git (GitHub + Apex mirror).

## Checks
`uv run pytest -q`, `uv run pyright src`; Rust: `cargo test --locked`, `clippy --all-targets -- -D warnings`, `fmt --check`.
