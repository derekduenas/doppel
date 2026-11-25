# M0 tape — runbook

**Measured on the dev Mac, 2026-11-25, 60–120 s runs:** 15 streams, ~2k msgs/s, ~0.5 core, 76 MB RSS,
~12 MB/min compressed (≈17 GB/day). 95%+ of the volume is the intl `price_change` book deltas.

## Install (Ubuntu host)
```sh
sudo useradd --system --home /var/lib/doppel --shell /usr/sbin/nologin doppel
sudo mkdir -p /opt/doppel /var/lib/doppel/tape && sudo chown doppel: /var/lib/doppel -R
# copy the repo to /opt/doppel (git archive | tar), then:
cd /opt/doppel && uv sync --frozen --no-dev
sudo cp ops/systemd/doppel-tape.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now doppel-tape
```

## Health
- `cat /var/lib/doppel/tape/status.json`: per-source msgs, reconnects, stalls, last-message age.
- `ls /var/lib/doppel/tape/receipts/YYYYMMDD/`: hourly receipts (built automatically at each hour change).
- `doppel-tape verify --data /var/lib/doppel/tape`: sha256 and CRC over every closed segment.
- Alert if any data source's `last_msg_age_s` > 30 s, an hourly receipt is missing, or RTDS `tx_union`
  coverage on both sockets is < 0.99.

## Storage
At ~17 GB/day, keep 14 days hot on local disk and ship closed segments (with manifest lines) to object
storage nightly. Parquet compaction lands in M1.

## Deployment log
- **2026-11-25 ~17:58Z**: deployed 20fd3f6 to Apex (atlas-do) as a stopgap host. Code in `/opt/doppel`
  (`DEPLOYED_COMMIT`), venv from `ops/requirements.lock`, user `doppel`, data on the attached volume
  `/mnt/volume_nyc1_1793485122853/doppel/tape`. `doppel-janitor.timer` deletes the oldest closed day when
  that volume has < 12 GB free. At ~17 GB/day the volume holds **~3 days rolling**. Move to the dedicated
  research box or object storage before that history matters. First check: 15/15 streams live, 83 MB,
  ~0.4 core; PM US books 200 for btc/eth/sol/xrp.
- Deploy method: `git archive HEAD | ssh atlas-do` into `/opt/doppel.new`, venv build, atomic swap,
  then `systemctl restart doppel-tape`.
