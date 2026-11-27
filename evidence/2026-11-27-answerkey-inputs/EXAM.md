# Three-seed rule recovery exam

Frozen before running these seeds. The scored subjects use the [exam parameters](exam-params.json).
They differ from Claude's short and long development fixtures. Use seeds **271031, 271067 and
271091**, each with all six subjects in order `a,b,c,d,e,f`. The decoy takes its rate from
the midpoint maker's training-period filled orders. The real-book interval is Nov 26 06:00 to
12:00 UTC, with a 09:00 split. There are 36 scheduled starts on each side. The
[lab token map](tokens.jsonl) is specific to this study.

For each seed, use a fresh output directory and the same source revision, map, public evidence
format, parameters and time boundaries. Save the source revision, input preflight, parameters hash,
all child exit codes, and each scorecard. The independent variable between the three runs is the
seed. Run the unfixed tools against the same populated public evidence for a comparable baseline.
Keep truth and the holdout inaccessible to fitting. For a synthetic exam, independently generate
synthetic tape with the corresponding reserved seed and retain its hash and the same train and
holdout durations. Report synthetic and real-book results separately.

Report each seed individually. The target is zero wrongly confident cells. Quote the count and
denominator, every interval miss, the total comparable cells and every subject's actual holdout
verdict and data gates. Both negative controls require an explicit result. An unexplained draft
for either negative control fails the no-invented-rule requirement. A tool failure, insufficient
evidence or an inconclusive positive control leaves the exam open. No seed can substitute for
another. Any change to the fitter or decision rules after inspecting the exam requires a new
prospective exam.

On Apex use a verified sealed source and an output filesystem with room for the full run.
The original zero-window baseline uses a different map and has no qualifying controls. It
cannot provide this comparison. Keep formal runs separate from that process.

For the fixed run, the following is the command to place inside the low-priority research
unit with its 1,500 MiB memory limit. Run from the combined review checkout. Set
`EXAM_SCRATCH` to the approved research filesystem with sufficient space and set
`EXAM_SEED` to one of the three reserved seeds above. Save each unit's exit status and logs.

```sh
PYTHONPATH="$PWD/src" /opt/doppel/.venv/bin/python -m doppel.lab.answerkey \
  --data /var/backups/doppel-sealed-20261126/tape \
  --tokens evidence/2026-11-27-answerkey-inputs/tokens.jsonl --stream clob_btc_5m \
  --from 2026-11-26T06:00:00Z --split 2026-11-26T09:00:00Z --to 2026-11-26T12:00:00Z \
  --strategies a,b,c,d,e,f --seed "${EXAM_SEED:?}" \
  --policy-params evidence/2026-11-27-answerkey-inputs/exam-params.json \
  --out "${EXAM_SCRATCH:?}/fixed-${EXAM_SEED}"
```

Revision `3dee8bb` predates the parameter-file option. For the paired old-tool result,
use that checkout's `answerkey.run_tools` on each matching subject's generated
`public/` directory, giving it a separate tool-output directory, the public wallet address
and the same UTC boundaries. Evaluate both revisions using the same scorer. Preserve
old-tool failures and its use of full-period order matches as observations about the old tools.
