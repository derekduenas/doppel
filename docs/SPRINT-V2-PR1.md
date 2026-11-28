# DOPPEL v2 · PR‑1 operations and Claude Code handoff

PR‑1 lives on `sprint/pr-1-protect-apex`. Use [GitHub PR #1](https://github.com/derekduenas/doppel/pull/1) for hosted checks. Run test suites, full tape replay, and research on Forge‑1 or GitHub Actions. The public workflow uses committed fixtures and pinned pod revisions. It carries no Apex or storage credentials.

The initial hosted run completed in about 4m26s. All five pod suites ran concurrently and all candidate Python shards and Rust checks passed. CI compares the frozen evaluator digest and verifies the preregistered cohort lineage. The actual Apex cohort-v1 file still matched `1775c0998506abf721e27ed3cb0043b5816b2dde587462034e55a4622a396b54` after the research jobs were parked. [Initial run](https://github.com/derekduenas/doppel/actions/runs/46085297192)

## Recorder commissioning

The branch adds `doppel-taped --only spot_l2,lead_feeds` for seven BRTI constituent book streams and six independent BTC lead streams. Binance spot and USD-M perpetuals supply native top-20 depth and trades. Hyperliquid supplies native L2 snapshots and trades. The recorder retains receipt timestamps and computes data age separately for each stream. All four official WebSocket routes accepted handshakes from Apex. A handshake gives connectivity evidence. Active intervals still require status and sealed segment checks.

The Linux binary and its checksums come from the CI artifact for the exact release commit. The release lead installs it under `/opt/doppel-bin`, creates an isolated lead tape directory on the mounted tape volume, and installs `doppel-taped-lead.service`. Set `DOPPEL_LEAD_ROOT` in `/etc/doppel-lead.env` to that directory. Use the existing PMUS/Kalshi recorder and its independent hourly receipts for the registered studies. The Rust lead unit has no venue credentials. Run the complete recorder cutover only after continuity, receipt and RTDS parity are demonstrated. The previous RTDS comparison failed.

The monitoring timer checks both filesystems and the primary recorder every 15 seconds. Put a Discord webhook URL in the systemd credential file `/etc/doppel/discord-webhook` out of band. For the isolated Rust recorder, add `DOPPEL_ADDITIONAL_RECORDERS="--recorder doppel-taped-lead.service=LEAD_ROOT"` to `/etc/doppel/tape-alerts.env`. The CLI supports `--dry-run` to print faults without writing state or sending. Verify a test delivery before relying on the timer.

## Cold upload and verified retention

Use a private R2 or B2 bucket. On Apex, configure `DOPPEL_TAPE_ROOT`, `DOPPEL_ARCHIVE_REMOTE` and `DOPPEL_RETAIN_DAYS` in `/etc/doppel-archive.env`. Select 10 days within the 7–14 day supported range. If the Rust root is separate, also set `DOPPEL_LEAD_ROOT` and `DOPPEL_LEAD_ARCHIVE_REMOTE` to a distinct object prefix. Put the scoped rclone configuration in the systemd credential file `/etc/doppel/rclone.conf`. Archive and pruning use the same private proof directory and lock.

```sh
python3 ops/archive_tape.py --source "$DOPPEL_TAPE_ROOT" --remote "$DOPPEL_ARCHIVE_REMOTE" \
  --state /var/lib/doppel-cold --streams auto --dry-run
python3 ops/tape_janitor.py "$DOPPEL_TAPE_ROOT" --remote "$DOPPEL_ARCHIVE_REMOTE" \
  --state /var/lib/doppel-cold --retain-days "$DOPPEL_RETAIN_DAYS" --dry-run
```

Run one bounded archive cycle with the configured service credential. The content-addressed objects include the data bytes, source manifest history, and original receipt when present. Their keys embed the SHA‑256. Before deleting an expired file, retention verifies the local proof, current local generation, protection list, and the remote bytes. Missing or inconsistent copies stay local. Retention preserves files with unresolved binding receipts and leaves reference and paid-source streams in place. A dry run lists candidates and performs no remote requests. Use the original receipt and selected manifest generation when restoring a registered evaluation snapshot.

## Legacy offshore tape

A private same-filesystem hold on Apex preserves 81 closed PERELMAN gzip hours, about 2.75 GB, from Nov 24 14:00 through Nov 27 22:00 UTC. The hold uses hard links so ordinary source pruning cannot discard those closed generations. The raw gzip files contain mixed streams. Stage them on the approved compute host with `ops/stage_perelman_offshore.py --source SOURCE --out STAGE --through YYYY-MM-DDTHH`. It emits only original Binance and Hyperliquid rows and their matching feed errors, plus the per-hour source and output SHA‑256 manifest. Use `archive_tape.py --historical-paths STAGE/upload-paths.txt` with a separate private prefix. Preserve the exact cutoff, inventory, and live archive receipt. Newer closed gzip hours need a follow-up inventory before Dec 1.

## Operational state at handoff

The existing Apex mechanism and cohort absorption research services and the cohort-freeze and fair-vs-market timers were parked. Retained scratch stays on Apex. Both existing recorders and the ingestion timer remained active. No tests, full replay, or heavy research ran on Apex during this sprint work.

The DigitalOcean account connector lists Apex and reports the team locked. Forge‑1 has no reachable SSH alias or runner. No private R2/B2 remote or webhook credential is configured. The 100 GiB tape volume cannot meet the 200 GB free gate. These conditions block the full live PR‑1 verdict and the official off-host exam. Main and the installed recorder still need the release lead's verified rollout. PR‑2 through PR‑6 remain gated by the requested merge order.
