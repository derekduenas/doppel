# DOPPEL: working rules for agents

Read `docs/BLUEPRINT.md`, `docs/ROADMAP.md`, `docs/GENOME.md` and `evidence/2026-11-25-probes/FINDINGS.md` before changing anything.

Read [the recovery handoff](docs/CLAUDE-HANDOFF.md) before resuming replay work.

## How to work (Karpathy guidelines, adopted 2026-11-26)
1. Think before coding: state assumptions; if a request has two readings, say so; push back when a simpler approach exists.
2. Simplicity first: the minimum code that solves the problem. No speculative features, abstractions or configurability.
3. Surgical changes: touch only what the task needs; match existing style; mention unrelated problems instead of fixing them.
4. Goal-driven: turn each task into a check (a test that reproduces the bug, a number that must match) and loop until it passes.

## Invariants (never break)
- **Scoring freeze until H4 is scored (2026-12-01):** H1–H4 are bound to evaluation protocol `received-raw-opening` v1 with evaluator code hash `c2d01f50…` (registry seq 21–24). Do not change any file covered by `twin.cli.code_sha256()`: `src/doppel/twin/*.py`, `strategy/{genome,registry,sequential}.py`, `lab/{fair,flow,ledger,brti_import,dossier,census,scan,refprice}.py`, `tape/{segment,universe}.py`. A change makes the bound families unscorable. Check with `uv run python -c "from doppel.twin.cli import code_sha256; print(code_sha256())"` == `c2d01f506f6dcb32ab935df3632e500b798bab5444779c910a7a683e0f3d25e4`.
- No look-ahead: a decision at second d uses only data received by d (`tw`).
- Sealed genomes and `registry/genomes.jsonl` are append-only; `doppel-genome verify` and `doppel-genome dna` must pass.
- No orders, no credentials in code or logs, no deploys from agents; Apex deploys go through `ops/deploy.sh` by the lead only.
- Stage explicit paths (never `git add -A`); everything lives in git (GitHub + Apex mirror).

## Checks
`uv run pytest -q`, `uv run pyright src`; Rust: `cargo test --locked`, `clippy --all-targets -- -D warnings`, `fmt --check`.
