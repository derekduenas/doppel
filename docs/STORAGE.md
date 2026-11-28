# Where DOPPEL data lives

The single source of truth is [`ops/storage-policy.toml`](../ops/storage-policy.toml). `ops/storage.py` reads it:
`copy` uploads every R2 class (checksummed, 25 MB/s cap, never deletes) and `audit` compares Apex with R2 and fails on
overdue uploads or any folder no rule covers. `doppel-storage.timer` runs both hourly at :20; the latest audit is
`/var/tmp/doppel-storage-audit.json` on Apex.

| Home | What | Kept |
|---|---|---|
| **DigitalOcean (Apex)** | live tape (`tape/`, `tape-rust/`), research data (`lab/`, `goldsky/`), releases and venvs (`/opt/doppel*`), scratch and CI checkouts (`/var/tmp`), the git mirror | tape 14 days once verified in R2 (protected days longer); research data indefinitely |
| **Cloudflare R2** (`doppel-tape`) | every sealed tape hour, stream manifests and receipts, research data, sealed archives (`sealed/…`) | permanently |
| **Git** (GitHub + Apex mirror) | code, registry, docs, evidence and preregistrations | permanently |
| **Never leaves Apex** | credentials (`/etc/doppel`, `/etc/credstore*`, `/etc/edge-engine`), logs, status files | — |

Rules:
- Nothing is uploaded until it is sealed (`min_age`, 2 h for tape segments); indexes (manifests, receipts) are re-copied every run.
- Local tape may be pruned only for hours the audit lists in `verified_paths` (same size in R2). Pruning is the janitor's job, never `storage.py`'s.
- A new folder on the volume makes the audit fail until it is added to the policy.
- The rclone config `/etc/doppel/rclone.conf` holds the R2 key; it is root-only and never printed. Use `/usr/local/bin/rclone-r2` (apt rclone 1.60 fails against R2).
