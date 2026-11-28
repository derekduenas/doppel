# DOPPEL platform · WS0 operations and Claude Code handoff

Sprint v3 governs this branch. Codex owns WS0 on `sprint/pr-1-protect-apex`. Opus owns the merge queue, research jobs and deployments. Use [GitHub PR #1](https://github.com/derekduenas/doppel/pull/1) for hosted checks. Run full suites in GitHub Actions. Opus owns the capped Apex research fallback specified by Sprint v3. The public workflow uses committed fixtures and pinned pod revisions. It carries no Apex or storage credentials.

The complete code revision `0e1251d` passed hosted CI in 5m08s. All five pod suites and the candidate Python shards, Pyright, package checks, Rust checks and release build passed. CI compares the frozen evaluator digest and verifies the preregistered cohort lineage. The actual Apex cohort-v1 file still matched `1775c0998506abf721e27ed3cb0043b5816b2dde587462034e55a4622a396b54` after the research jobs were parked. [Complete hosted run](https://github.com/derekduenas/doppel/actions/runs/46085959551)

## Recorder commissioning

The branch adds `doppel-taped --only spot_l2,lead_feeds` for seven BRTI constituent book streams and six independent BTC lead streams. Binance spot and USD-M perpetuals supply native top-20 depth and trades. Hyperliquid supplies native L2 snapshots and trades. The recorder retains receipt timestamps and computes data age separately for each stream. All four official WebSocket routes accepted handshakes from Apex. A handshake gives connectivity evidence. Active intervals still require status and sealed segment checks.

The Linux binary and its checksums come from the CI artifact for the exact release commit. The release lead installs it under `/opt/doppel-bin`, creates an isolated lead tape directory on the mounted tape volume, and installs `doppel-taped-lead.service`. The unit ships disabled. If commissioning constituent books, set `DOPPEL_LEAD_ROOT` in `/etc/doppel-lead.env` to that directory. Its default selection is `spot_l2`. Leave the offshore streams off unless the owner explicitly opts in. Use the existing PMUS/Kalshi recorder and its independent hourly receipts for the registered studies. The Rust lead unit has no venue credentials. Run the complete recorder cutover only after continuity, receipt and RTDS parity are demonstrated. The previous RTDS comparison failed.

On Nov 28 an 85-second isolated Apex run of the CI-built merge candidate `2cecfb5` wrote all 13 streams. Each stream had native data and a byte-verified SHA-256 manifest with zero stalls, dropped messages or write errors. The probe retained 1,809,806 bytes and peaked at 9.4 MiB of memory. Its status and tape are in private Apex scratch `/var/tmp/doppel-pr1-live.gwgM6C/tape`. Both production recorders remained active. This was a timed capture, and the always-on lead service still requires activation.

The monitoring timer checks both filesystems, recorder health, available host memory, and the named research cgroups every 15 seconds. Alerts go to the system journal by default. Optional Discord delivery uses a systemd drop-in with `LoadCredential=discord-webhook:/etc/doppel/discord-webhook` after its URL is provisioned out of band. For the isolated Rust recorder, add `DOPPEL_ADDITIONAL_RECORDERS="--recorder doppel-taped-lead.service=LEAD_ROOT"` to `/etc/doppel/tape-alerts.env`. The CLI supports `--dry-run` to print faults without writing state or sending. Exercise the configured delivery path before relying on the timer.

## Cold upload and verified retention

Use a private R2 or B2 bucket. On Apex, configure `DOPPEL_TAPE_ROOT`, `DOPPEL_ARCHIVE_REMOTE` and `DOPPEL_RETAIN_DAYS` in `/etc/doppel-archive.env`. Select 10 days within the 7–14 day supported range. Until cold storage is configured, the existing local floor and protected-day rules remain available. After the first verified cold cycle, the lead can switch from `doppel-janitor.timer` to `doppel-cold-retention.timer` and enable `doppel-archive.timer`. If the Rust root is separate, also set `DOPPEL_LEAD_ROOT` and `DOPPEL_LEAD_ARCHIVE_REMOTE` to a distinct object prefix. Put the scoped rclone configuration in the systemd credential file `/etc/doppel/rclone.conf`. Archive and pruning use the same private proof directory and lock.

```sh
python3 ops/archive_tape.py --source "$DOPPEL_TAPE_ROOT" --remote "$DOPPEL_ARCHIVE_REMOTE" \
  --state /var/lib/doppel-cold --streams auto --dry-run
python3 ops/tape_janitor.py "$DOPPEL_TAPE_ROOT" --remote "$DOPPEL_ARCHIVE_REMOTE" \
  --state /var/lib/doppel-cold --retain-days "$DOPPEL_RETAIN_DAYS" --dry-run
```

Run one bounded archive cycle with the configured service credential. The content-addressed objects include the data bytes, source manifest history, and original receipt when present. Their keys embed the SHA‑256. Before deleting an expired file, retention verifies the local proof, current local generation, protection list, and the remote bytes. Missing or inconsistent copies stay local. Retention preserves files with unresolved binding receipts and leaves reference and paid-source streams in place. A dry run lists candidates and performs no remote requests. Use the original receipt and selected manifest generation when restoring a registered evaluation snapshot.

The first Apex metadata-only dry run found 1,088 closed segments totaling 36,416,912,587 bytes, with zero inventory issues. It created no upload proofs and made no requests to object storage.

## Legacy offshore tape

A private same-filesystem hold on Apex preserves 81 closed PERELMAN gzip hours, about 2.75 GB, from Nov 24 14:00 through Nov 27 22:00 UTC. The hold uses hard links so ordinary source pruning cannot discard those closed generations. The raw gzip files contain mixed streams. Stage them on the approved compute host with `ops/stage_perelman_offshore.py --source SOURCE --out STAGE --through YYYY-MM-DDTHH`. It emits only original Binance and Hyperliquid rows and their matching feed errors, plus the per-hour source and output SHA‑256 manifest. Use `archive_tape.py --historical-paths STAGE/upload-paths.txt` with a separate private prefix. Preserve the exact cutoff, inventory, and live archive receipt. Newer closed gzip hours need a follow-up inventory before Dec 1.

## Operational handoff to Opus

Before v3 arrived, Codex parked the mechanism and cohort absorption services and the cohort-freeze and fair-vs-market timers under the then-current v2 policy. The original recorders and ingestion timer remained active. Opus's Layer 5 chain was active at 00:12 UTC on Nov 28. The cohort freeze timer was absent at that check. Opus owns re-arming it or running the original 00:00 UTC cutoff at the planned 01:20 UTC slot. Existing research outputs were preserved. Codex ran no suites or replays on Apex.

DigitalOcean reported that the connected team is locked. No Forge-1 or configured cold remote was available. Sprint v3 defaults apply. Alerts use the journal, offshore stays disabled, and all production changes go through the lead. The timed capture above remains isolated. The [promotion review](handoffs/CODEX-V3-REVIEW-20261128.md) records evidence checks that matter to the sprint.
