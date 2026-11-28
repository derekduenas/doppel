# Where DOPPEL data lives

[`ops/storage-policy.toml`](../ops/storage-policy.toml) assigns each R2 class one uploader. `doppel-archive.timer`
runs `archive_tape.py` at :05 UTC for native-sealed `tape` and `tape-rust` segments. It publishes immutable
payloads, checksums and hourly receipts, and verifies their SHA-256 by reading back from R2. `doppel-storage.timer`
runs `storage.py` at :20 UTC to copy the remaining classes, including their flat index sidecars, and audit all
classes for overdue uploads and unclassified folders. It publishes
`/var/tmp/doppel-storage-audit.json` atomically. `verify` streams each planned object over the same bandwidth-capped
R2 configuration and reports the exact local and remote SHA-256 comparison.

| Home | What | Kept |
|---|---|---|
| **DigitalOcean (Apex)** | live tape (`tape/`, `tape-rust/`, `tape-lead/`), research data (`lab/`, `goldsky/`), releases and venvs (`/opt/doppel*`), scratch and CI checkouts (`/var/tmp`), the git mirror | tape 14 days once verified in R2 (protected days longer); research data indefinitely |
| **Cloudflare R2** (`doppel-tape`) | every sealed tape hour, stream manifests and receipts, research data, sealed archives (`sealed/…`) | permanently |
| **Git** (GitHub + Apex mirror) | code, registry, docs, evidence and preregistrations | permanently |
| **Never leaves Apex** | credentials (`/etc/doppel`, `/etc/credstore*`, `/etc/edge-engine`), logs, status files | — |

Rules:
- Nothing is uploaded until it is sealed (`min_age`, 2 h for tape segments); indexes (manifests, receipts) are re-copied every run.
- The audit's `verified_paths` shortlist means same size. The production janitor requires fresh service-owned audit data bound to the exact policy. It verifies the local seal, streams the remote segment and any hourly receipt, and rechecks the local generation and protected dates before pruning a closed hour beyond local retention. For `tape` and `tape-rust`, the exact immutable payload and checksum manifest must both exist. Stale or missing evidence holds data locally. Check disk headroom before the first archive cycle and while retention catches up with the 12-file readback limit.
- A new folder on the volume makes the audit fail until it is added to the policy.
- The rclone config `/etc/doppel/rclone.conf` holds the R2 key; it is root-only and never printed. Use `/usr/local/bin/rclone-r2` (apt rclone 1.60 fails against R2).

## Check the staged tape and historical inventory

Use the same policy as the installed storage timer. The dry run lists closed, eligible local objects and their expected native seals where available. The byte comparison runs only in live verification mode. Supplying an explicit inventory prevents a later count from silently replacing the original archive scope. Use one relative path per line.

```sh
python3 ops/archive_tape.py --storage-class tape --state /var/lib/doppel-cold --dry-run
python3 ops/storage.py verify --class tape --inventory /path/to/closed-tape-paths.txt --dry-run
python3 ops/storage.py verify --class tape --inventory /path/to/closed-tape-paths.txt
python3 ops/storage.py verify --class perelman-offshore-hold-20261128 --inventory /path/to/held-hours.txt
```

Keep the completed JSON manifests under the lead's operational receipt path. A report with any missing object, mismatched checksum or unsealed local segment exits nonzero.

## Uploader cutover

1. Pause the old storage and retention timers and wait for any active copy to finish. Disable any older `doppel-cold-retention` units.
2. Install `storage.py`, `storage-policy.toml`, `archive_tape.py`, `cold_tape.py`, and `doppel-cold-tape.sh` together under `/opt/doppel-storage`. Install the matching `doppel-storage`, `doppel-archive`, and `doppel-janitor` units and reload systemd. The new storage policy prevents `storage.py copy` from uploading either delegated tape class.
3. Run the archive dry run and check for missing seals or unexpected inventory. Start the archive and storage timers. The first verified archive cycle may need to resume on a later run. Each completed proof is saved in `/var/lib/doppel-cold`. Existing flat objects remain readable under their original keys. New payloads use `data/<relative-path>/<sha256>` beneath the same R2 class prefix so a revised native seal has its own object.
4. Verify the registered inventories by exact SHA-256 and refresh the audit with the new policy. Run the production janitor after the new audit has been published. The legacy janitor shell entry point exits with an error.
