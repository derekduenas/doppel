# M0 tape — runbook

**Measured on the dev Mac, 2026-11-25, 60–120 s runs:** 15 streams, ~2k msgs/s, ~0.5 core, 76 MB RSS,
~12 MB/min compressed (≈17 GB/day). 95%+ of the volume is the intl `price_change` book deltas.

## Install (Ubuntu host)
```sh
sudo useradd --system --home /var/lib/doppel --shell /usr/sbin/nologin doppel
sudo mkdir -p /opt/doppel /mnt/volume_nyc1_1793485122853/doppel/tape && sudo chown doppel: /var/lib/doppel -R
# copy the repo to /opt/doppel (git archive | tar), then:
cd /opt/doppel && python3 -m venv .venv && .venv/bin/pip install -r ops/requirements.lock && .venv/bin/pip install --no-deps .   # what ops/deploy.sh does
sudo cp ops/systemd/doppel-tape.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now doppel-tape
```

## Authenticated streams (DOPPEL holds no keys)
Two streams need venue auth. Neither key is stored in this repo, in `/opt/doppel`, or in the tape.

- **`pmus_ws`**: Polymarket US markets WebSocket (`wss://api.polymarket.us/v1/ws/markets`), full book
  (`SUBSCRIPTION_TYPE_MARKET_DATA`) and trades (`SUBSCRIPTION_TYPE_TRADE`) for the current and next
  5m/15m window of btc/eth/sol/xrp. The market-data key belongs to the host signer daemon. Before
  every (re)connect the recorder connects to `/run/pmus-riskd-market-signer/markets.sock`, sends
  `sign-market-ws\n`, reads one line `access_key\ttimestamp_ms\tsignature` (2 s timeout) and sends
  those values as the `X-PM-Access-Key`, `X-PM-Timestamp` and `X-PM-Signature` handshake headers.
  The unit gets socket access through `SupplementaryGroups=pmus-market-data`. Header values are
  scrubbed from status.json, logs and the tape, and redirects are refused so the headers never reach
  another host. One subscribe request per (type, horizon, window). A window is dropped with the
  documented `{"unsubscribe": {"request_id": ...}}` 30 s after it ends. A request answered with
  `error` is retried 5 s later once its window has started; before the open, 3 s after the open
  (at most 30 s later), since PM US lists a window a few seconds after it opens. A started window whose book
  request has not produced data for every slug within 20 s (doubling to 120 s) is unsubscribed and
  re-sent (`ctl: no_data`). Server `{"heartbeat": {}}` frames keep the stall watchdog (30 s) happy
  and are not recorded. If the signer is down, `status.json` shows
  `SignerError: signer connect failed: ...` and the source retries with backoff.
- **`ref_brti_live`** (`--only` key `kalshi_brti`): Kalshi's CF Benchmarks BRTI passthrough,
  `GET https://external-api.kalshi.com/trade-api/v2/cfbenchmarks/values?id=BRTI` every 5 s,
  signed with a **read-only** Kalshi profile. systemd hands it in with
  `LoadCredential=kalshi-profile.json:/etc/edge-engine/riskd/kalshi-production-readonly.json`, and the
  recorder reads `$CREDENTIALS_DIRECTORY/kalshi-profile.json` (`{"key_id", "private_key_pem"}`, RSA-PSS
  or Ed25519) at start. Each raw body is recorded with `meta = {status, sent_tw, sent_tm, new}`, where
  `new` lists the `[time_ms, value]` pairs not seen before. With no credential file (e.g. a dev
  run) it logs once and idles (`status.json`: `idle: no_credentials` or `bad_credentials`).
  `ref_*` streams are never touched by the janitor.

## Health
- `cat /mnt/volume_nyc1_1793485122853/doppel/tape/status.json`: per-source msgs, reconnects, stalls, last-message age.
- `ls /mnt/volume_nyc1_1793485122853/doppel/tape/receipts/YYYYMMDD/` lists the retained hourly receipts.
  With the reviewed v2 producer installed, each final receipt pins compressed byte length and
  SHA-256 to a published segment revision. The Apex recorder waits for the raw BRTI importer
  to commit a publication at or beyond the start of the following UTC hour. The 15-minute import
  timer can therefore delay an hourly receipt. The recorder retries pending hours after restart.
  Original receipts survive reruns. A completed hour missing its expected raw BRTI stream has
  an explicit `missing_expected_streams` entry and cannot support a binding evaluation.
- The reviewed importer stages revisions in `ref_brti_raw/.import-work` and resumes prepared
  transactions on retry. `ref_brti_raw/import-watermark.json` records the last committed
  publication in milliseconds. Previously unseen older observations are retained separately
  in `ref_brti_raw/import-amendments.jsonl` with their original receive time. Sealed evaluations
  use the committed stream. Preserve any amendments with the importer state when investigating
  upstream data revisions.
- `doppel-tape verify --data /mnt/volume_nyc1_1793485122853/doppel/tape`: sha256 and CRC over every closed segment.
- `pmus_ws`: `heartbeats` should rise, `subscribed` (open requests) should be 8 (2 horizons x current
  and next window x book and trades), up to 12 while an ended window lingers, and `sub_errors` / `resubscribes` should stay
  small. `ref_brti_live`: `http_401` / `http_403` mean the profile or the signature is wrong.
- Alert if any data source's `last_msg_age_s` > 30 s, an hourly receipt is missing, or RTDS `tx_union`
  coverage on both sockets is < 0.99.

## Storage
At ~17 GB/day, keep 14 days hot on local disk and ship closed segments (with manifest lines) to object
storage nightly. Parquet compaction lands in M1.

For a primary evaluation tape, archive a bounded range after the hourly receipt appears.

```sh
python3 ops/archive_tape.py --source TAPE --destination ARCHIVE \
  --from YYYYMMDD/HH --through YYYYMMDD/HH --require-receipts --streams STREAM ...
```

The first UTC hour is inclusive and the end is exclusive. The copy requires a separate
filesystem and defaults to a 35 GiB free-space reserve. It preserves historical manifest rows,
checks each segment's SHA-256, and selects the published generation named in a v2 receipt.
A missing receipt, a receipt discrepancy or a missing segment yields a nonzero exit. Inspect
both `missing` and `receipt_issues` in its JSON. A legacy file whose final bytes exceed its
receipt can still be retained for investigation. Keep any reconstructed input in a separate
directory and retain its evidence classification. The Rust shadow currently has no hourly
receipts of its own. Omit `--require-receipts` when archiving shadow-only data.
The Apex archive survives the tape-volume janitor. Both filesystems share the host.
The reviewed janitor uses the exact free-byte count and prunes valid past UTC days from
the allowed high-volume streams, shadow first. The ten-minute timer invokes the script in
`/opt/doppel` directly. Before releasing the corrected job, verify that required scoring
inputs from candidate days have been preserved.

## Deployment log
- **2026-11-25 ~17:58Z**: deployed 20fd3f6 to Apex (atlas-do) as a stopgap host. Code in `/opt/doppel`
  (`DEPLOYED_COMMIT`), venv from `ops/requirements.lock`, user `doppel`, data on the attached volume
  `/mnt/volume_nyc1_1793485122853/doppel/tape`. `doppel-janitor.timer` deletes the oldest closed day when
  that volume has < 12 GB free. At ~17 GB/day (Python) plus the Rust shadow while it runs, the volume holds **~3 days rolling**; the janitor prunes the shadow first. Move to the dedicated
  research box or object storage before that history matters. First check: 15/15 streams live, 83 MB,
  ~0.4 core; PM US books 200 for btc/eth/sol/xrp.
- Deploy method: `git archive HEAD | ssh atlas-do` into `/opt/doppel.new`, venv build, atomic swap,
  then `systemctl restart doppel-tape` only with `ops/deploy.sh --restart-tape`.

## Rust recorder binary

`doppel-taped` lives at `/opt/doppel-bin/doppel-taped`, OUTSIDE `/opt/doppel`, because `ops/deploy.sh` swaps `/opt/doppel` atomically and would delete it (2026-11-26: a restart after a deploy failed with 203/EXEC). Build in `/srv/doppel-build` with the toolchain at `/opt/doppel-rust`, then `install -m 755 .../target/release/doppel-taped /opt/doppel-bin/`.
