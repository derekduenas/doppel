# BRTI absorb: DOPPEL owns its BRTI source

**Decision (owner, 2026-11-27):** DOPPEL must own its BRTI source, and PERELMAN must stop running on
its own. Today `doppel-brti-import` (`src/doppel/lab/brti_import.py`, under the scoring freeze) reads
PERELMAN's `/var/lib/private/perelman/stream/*.jsonl[.gz]`. H1–H4 scoring until 2026-12-01 is bound
to that pipeline. The cutover therefore has to be gap-free and must not touch any frozen file.

**What this adds** (branch `pod/brti-absorb`; nothing is installed, deployed or running):

| Path | What it is |
|---|---|
| `src/doppel/brti/recorder.py` | PERELMAN `recorder.py` vendored from commit `9b55eae`, sha256 `65e81e98…`. The CLI is `doppel-brti-record --out DIR [--without GROUPS]`. |
| `src/doppel/brti/books.py`, `fastbrti.py` | PERELMAN `books.py` and `fastbrti.py`, copied verbatim. Each has a header that cites the commit and the source sha256. |
| `src/doppel/brti/parity.py` | `doppel-brti-parity --a PERELMAN_STREAM --b DOPPEL_STREAM --from T --to T [--gate brti,rti]` |
| `ops/systemd/doppel-brti.service` | The recorder unit: User=doppel, LoadCredential, sandboxed. It is a file in the repo only and is not installed. |
| `tests/test_brti_absorb.py` | Tests for format, cutover, parity, rotation, restart and numerics. |

## Naming: `brti` vs `rti`

In PERELMAN's stream, **`kind:"brti"`** holds published BRTI. **`kind:"rti"`** holds the *other*
CF indexes (ETH, SOL, XRP and DOGE, tagged with `id`). The frozen importer reads only `brti`. The
parity PASS rule therefore applies to `brti`, and to each `rti` id when `--gate` includes `rti`.

## 1. Inventory: everything PERELMAN `recorder.py` records

All of it goes to one hourly stream (`<UTC hour of write>.jsonl`, gzipped when the hour rolls,
kept 14 days) plus `status.json` (1 Hz).

| Record kind | Source endpoint | Cadence | Credential | Used in DOPPEL | Kept? |
|---|---|---|---|---|---|
| `brti` (`sent`, `received`, `server_time`, `new:[[t_ms, value]]`) | `GET https://external-api.kalshi.com/trade-api/v2/cfbenchmarks/values?id=BRTI` (CF Benchmarks via Kalshi) | Poll every 0.5 s. Each response holds about 120 s of 1 Hz values, and only first-seen seconds are written. | Kalshi read-only key (RSA-PSS or Ed25519 signature) | **Yes.** `doppel-brti-import` writes it to tape `ref_brti_raw`, the scored H1–H4 input with `received` stamps. `ops/recover_brti_receipts.py` matches its bytes. | yes |
| `brti_error` | same | on error | same | no (health) | yes |
| `rti` (`id`, `sent`, `received`, `new`) | same endpoint, `id=ETHUSD_RTI`, `SOLUSD_RTI`, `XRPUSD_RTI`, `DOGEUSD_RTI` | Round robin, one request every 0.5 s, so each coin is polled every 2 s | same key | No. It is plausibly needed for non-BTC venues, and it centres the hourly ETH, SOL and XRP ladders. | yes (group `rti`) |
| `rti_error` | same | on error | same | no | yes |
| `replica` (`t`, `frozen_at`, `venues`, `used`, `excluded`, `flagged`, `value`, `nowcast`, `bias`, `calc_done`, `diag`) | Computed from the 7 constituent books (rows below) using CF RTI v17.0 | 1 Hz, on whole seconds | none | Not read. The W6 fast-fair work cross-checks against published `brti`, and `doppel-taped` records its own L2. It is plausibly needed as an independent replica. | yes (group `replica`) |
| `live` (`tick`, `value`, `nowcast`, `n`) | same computation | 4 Hz (the 200 ms ticks that do not fall on a whole second) | none | No. It is plausibly needed as the continuous fair value. | yes |
| `resync`, `feed_error`, `audit`, `audit_error` | Constituent feeds below. `audit` compares each WS book with the venue's REST top of book. | on event; audit every 30 s per venue | none | no (book health) | yes |
| (books, not records) | Coinbase `wss://advanced-trade-ws.coinbase.com` level2; Kraken `wss://ws.kraken.com/v2` book 1000; Bitstamp `wss://ws.bitstamp.net` order_book; Gemini REST `api.gemini.com/v1/book` (1 Hz); Crypto.com `wss://stream.crypto.com/exchange/v1/market` book.150; Bullish `wss://api.exchange.bullish.com/.../orderbook`; LMAX REST `public-data-api.london-digital.lmax.com` (1 Hz) | streaming / 1 Hz | none | feeds `replica` and `live` | yes (`lmax` can be dropped alone) |
| `kmarket`, `kq` (`yb`, `ya`, `ybs`, `yas`, `venue_ts`, `recv`, plus `sent_ms` and `seq` for 15m), `ktrade`, `kladder_recenter` | Kalshi REST `GET api.elections.kalshi.com/trade-api/v2/markets` (public) plus WS `wss://api.elections.kalshi.com/trade-api/ws/v2` `orderbook_delta` (and `trade` on hourly). Series: KXBTC15M, KXETH15M, KXSOL15M, KXXRP15M, KXDOGE15M, and hourly KXBTCD, KXETHD, KXSOLD, KXXRPD (8 strikes each). | on top-of-book change | Kalshi key (WS handshake signature). The REST markets call needs none. | No. DOPPEL has its own Kalshi capture: `kalshi_books` in `doppel-tape` and `kalshi_ws` in `doppel-taped`. It is plausibly needed because it is the same key's KXBTC15M top of book with PERELMAN stamps. | yes (group `kalshi`) |
| `hl` (`recv`, `ts`, `mid`, `bid_sz`, `ask_sz`) | `wss://api.hyperliquid.xyz/ws` bbo BTC perp | on BBO mid change | none | no | yes (group `hl`, **US-access flag**, off by default in the unit) |
| `bn` (`recv`, `mid`, `u`) | `wss://data-stream.binance.vision/ws/btcusdt@bookTicker` (Binance.com market-data domain) | on mid change | none | no | yes (group `bn`, **US-access flag**, off by default in the unit) |
| `books/<t>.json.gz` dumps (separate dir) | the frozen books | every 300 s | none | no | **removed** |

`status.json` stats keys: `replica`, `replica_fail`, `brti_polls`, `brti_errors`, `reconnects`,
`resyncs`, `live`, `live_fail`, `live_skipped`, `kalshi_quotes`, `kalshi_hourly_quotes`, `hl`, `bn`.

### US-access concerns (reported, not decided)

- **`hl`, Hyperliquid (`api.hyperliquid.xyz`).** Hyperliquid's public terms are understood to
  restrict US persons. Whether that covers reading public market data has not been checked here.
  **Flagged.**
- **`bn`, Binance.com (`data-stream.binance.vision`).** PERELMAN's own comment says this
  market-data domain is served to US addresses, while `api.binance.com` and `fapi` return HTTP 451.
  Binance.com's terms exclude US persons. Binance.US is the US entity. **Flagged.**
- **Constituent venues** (Crypto.com Exchange, Bullish, LMAX Digital, and the US-facing Coinbase,
  Kraken, Bitstamp and Gemini). Only public market data is read, and their market-data terms have
  not been checked here. These venues define BRTI itself (CF methodology), so dropping any of them
  breaks the replica.
- The unit ships with `hl` and `bn` **off** (`--without hl,bn` in `ExecStart`) until the owner
  decides. PERELMAN records both today. Turning them on is removing `hl,bn` from `--without`, a
  unit-argument change with no code change, and it does not touch `brti`.

## 2. What changed from PERELMAN

These changes leave the output untouched. Record kinds, keys, key order, `json.dumps(separators=(",", ":"))`
bytes, value pass-through (`row["value"]` is never parsed), file names and the hour-of-write rule
are all identical. The test `test_brti_record_bytes_match_perelman` pins the `brti` line byte for byte.

**Removed**, because DOPPEL does not use them:
- `dump_books`, the `books/` directory of 6 %-band book snapshots every 300 s. They existed for
  PERELMAN's offline calibration of methodology ambiguities, and `doppel-taped` records full L2 itself.
- `value_no_lmax` in `replica`. This was PERELMAN's paired LMAX A/B from PREREG 2026-11-24. It
  doubled the replica CPU, and DOPPEL never reads it. It is the only change to a record. That
  record is `replica`, which the importer never reads.
- The optional `fastbrti_rs` Rust import. DOPPEL always uses the numpy `fastbrti` (PERELMAN's
  fallback). PERELMAN's own test allows $0.01 between the two. `replica` therefore cannot be
  bit-identical to PERELMAN's in any case.
- The `PERELMAN_DATA` environment variable, replaced by `--out DIR`. `status.json` moves into `DIR`.
  The importer globs only `*.jsonl` and `*.jsonl.gz`, so this is harmless.

**Robustness changes** (cheap, and none of them changes a value):
- **Torn tail.** At open, a last line without `\n` (left by a crash) is cut. Without this fix, the
  next append, or the hour's gzip, would turn that line into a malformed record. The frozen
  importer raises on such a record, which would stop every later import.
- **Atomic gzip on a thread.** The hour is written to `X.jsonl.gz.tmp`, fsynced, renamed, its
  directory fsynced, and only then is `X.jsonl` unlinked. Before this change, the gzip ran inside
  `write()` on the event loop and stalled every feed. A crash could also leave a half-written `.gz`
  that the importer would mark done.
- **Restart repair.** Earlier hours still in `.jsonl` are gzipped. A leftover `.gz.tmp` is removed.
  A `.jsonl` whose `.gz` already exists is unlinked.
- **fsync** of the open hour once a second, from the status loop.
- **BRTI poll backoff.** After an error, the next poll waits 1, 2, 4, then at most 8 s, and
  returns to 0.5 s on the first success. Each response carries about 120 s of values, so no
  second is lost.
- **Credential at start.** The credential is loaded once at start and shared by every signed call.
  If it is missing, the process exits with code 2 and logs only the error type. (PERELMAN crashed
  inside a task.)
- **`--without GROUP,...`.** The groups are `replica`, `lmax`, `rti`, `kalshi`, `hl` and `bn`.
  Published BRTI is always on. KXBTCD strikes centre on the replica, so `kalshi` without `replica`
  records only the 15-minute series and the non-BTC hourlies.
- **status.json** also carries `source_commit` and `without`.

## 3. Parity rule

`doppel-brti-parity` reads `brti` with the frozen importer's own reader (`_iter_rows`), so B is
parsed exactly as the importer will parse it. For each publication second in `[from, to)` it takes
the first-seen value and receive stamp.

**PASS** requires three things for each gated kind (`brti`, and `rti` per id):
- every overlapping second has an identical value string;
- B has no second with conflicting values;
- coverage, overlap divided by A's seconds, is at least 99.9 %.

The report lists missing and extra seconds, with examples, and the receive-stamp difference
`B − A` in ms (p50, p99, |p99|, |max|). `replica` is reported (coverage, |B − A| p50/p99/max in USD)
and **never gates**, because the two recorders hold independent sockets. The exit code is 0 on
PASS and 1 on FAIL.

## 4. Plan

### Risk: shadowing loads the sources H1–H4 depend on

A second full recorder doubles four loads:
- the signed Kalshi REST load on the shared read-only key: 2 Hz BRTI plus 2 Hz `rti`, on top of
  PERELMAN, the `doppel-tape` BRTI poller and btc-window;
- 9 Kalshi WebSockets on the same key;
- the 1 Hz LMAX and Gemini REST polls. The repo notes that LMAX's public REST limit is shared per
  client, and extra LMAX polling got PERELMAN's feed a 429 on 2026-11-24 (`docs/fast-fair.md`).

A 429 on the key could thin PERELMAN's `brti`, which is the scored input. Kalshi's per-key limits
are not verified here. **The shadow therefore starts BRTI-only and widens only while both status
files stay clean.**

**Abort rule, at any phase.** Stop `doppel-brti` at once if either of these happens:
- the rate of `brti_errors` in **either** status file (PERELMAN's `/var/lib/private/perelman/status.json`
  or DOPPEL's `brti-stream/status.json`) rises above PERELMAN's pre-shadow baseline, taken in
  phase 0 (errors per poll, from `jq '.stats|{brti_polls,brti_errors}'`);
- `"code":429` appears in PERELMAN's stream. Only `rti_error` records carry a code, and the phase 1
  DOPPEL recorder writes no `rti` or feed records, so the DOPPEL stream cannot show it in phase 1;
  there the `brti_errors` rate is the signal. From phase 2 on, grep both streams.

### Phase 0: install (lead, via `ops/deploy.sh` and then root on Apex)

```sh
# after the branch is merged and deployed with ops/deploy.sh (it installs code only, not units)
install -d -o doppel -g doppel -m 0750 /mnt/volume_nyc1_1793485122853/doppel/brti-stream
install -m 0644 /opt/doppel/ops/systemd/doppel-brti.service /etc/systemd/system/doppel-brti.service
install -d /etc/systemd/system/doppel-brti.service.d
cat > /etc/systemd/system/doppel-brti.service.d/shadow.conf <<'EOF'
# Shadow phase 1: published BRTI only (2 Hz on the shared key; no sockets, no LMAX).
[Service]
ExecStart=
ExecStart=/opt/doppel/.venv/bin/doppel-brti-record --out /mnt/volume_nyc1_1793485122853/doppel/brti-stream --without replica,rti,kalshi,hl,bn
EOF
systemctl daemon-reload
systemctl enable --now doppel-brti.service
sleep 20; journalctl -u doppel-brti -n 50 --no-pager      # read any traceback now
# Freeze check on the host: must equal the scoring hash c2d01f506f6dcb32ab935df3632e500b798bab5444779c910a7a683e0f3d25e4
/opt/doppel/.venv/bin/python -c 'from doppel.twin.cli import code_sha256; print(code_sha256())'
cat /mnt/volume_nyc1_1793485122853/doppel/brti-stream/status.json
cat /var/lib/private/perelman/status.json                   # PERELMAN brti_errors baseline
```

`ops/install-release.sh` extracts the whole git archive into the release, so the unit is at
`/opt/doppel/ops/systemd/doppel-brti.service` after a deploy. If the freeze check prints any other
hash, stop: the deployed tree is not the one H1–H4 are scored with.

### Phase 1: BRTI-only shadow, at least 24 h

Check both status files every few hours with
`jq '.stats|{brti_polls,brti_errors}'`, and look for 429s with
`zcat -f /var/lib/private/perelman/stream/*.jsonl.gz /var/lib/private/perelman/stream/*.jsonl | grep -c '"code":429'`
(as root). Apply the abort rule above.

After at least 24 h, run the parity check:

```sh
FROM=$(date -u -d '24 hours ago' +%s); TO=$(date -u -d '10 minutes ago' +%s)
/opt/doppel/.venv/bin/doppel-brti-parity --a /var/lib/private/perelman/stream \
  --b /mnt/volume_nyc1_1793485122853/doppel/brti-stream --from $FROM --to $TO --gate brti \
  > /mnt/volume_nyc1_1793485122853/doppel/lab/brti-parity-phase1.json; echo exit=$?
```

Run it as root, because it reads PERELMAN's DynamicUser directory. It must print `exit=0`. Keep
the JSON as evidence.

### Phase 2 (optional, before or after cutover): widen

Once phase 1 has passed, remove `shadow.conf`. The unit's own `--without hl,bn` then applies, so
the offshore feeds stay off pending the US-access call. Then run `systemctl daemon-reload && systemctl restart doppel-brti`, re-apply the
abort rule for a day, and run parity with `--gate brti,rti`. The `replica` section is informational.

### Phase 3: cutover, only AFTER H4 is scored (2026-12-01)

Do the cutover between minutes 15 and 45 of a UTC hour, so no hour roll happens during it. It
changes one unit argument and no code.

```sh
T=/mnt/volume_nyc1_1793485122853/doppel
# 0. Parity PASS over the last 24 h (phase 1 command, fresh window) -> exit=0. Otherwise stop here.
# 1. Quiesce the importer and take one last import from PERELMAN.
systemctl stop doppel-brti-import.timer
while systemctl is-active -q doppel-brti-import.service; do sleep 2; done
systemctl start doppel-brti-import.service            # oneshot: returns when done
cat $T/tape/ref_brti_raw/import-watermark.json          # -> W0 (max_t_ms)
export W0=$(jq .max_t_ms $T/tape/ref_brti_raw/import-watermark.json)
cp -a $T/lab/brti-import-state.json $T/lab/brti-import-state.pre-cutover.json
# 2. Point --src at the DOPPEL stream (drop-in; the committed unit is updated afterwards).
install -d /etc/systemd/system/doppel-brti-import.service.d
cat > /etc/systemd/system/doppel-brti-import.service.d/src.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/opt/doppel/.venv/bin/doppel-brti-import --src /mnt/volume_nyc1_1793485122853/doppel/brti-stream --data /mnt/volume_nyc1_1793485122853/doppel/tape --state /mnt/volume_nyc1_1793485122853/doppel/lab/brti-import-state.json
EOF
systemctl daemon-reload
# 3. One import from the DOPPEL stream; the watermark must move forward.
systemctl start doppel-brti-import.service
journalctl -u doppel-brti-import -n 5 --no-pager        # {"added": N>0, "amendments": small, ...}
cat $T/tape/ref_brti_raw/import-watermark.json          # -> W1 > W0
# 4. Gap check: every second the DOPPEL stream has in (W0, W1] is in the tape. Seconds at or
#    below W0 were imported from PERELMAN and are not re-checked here.
/opt/doppel/.venv/bin/python - <<'EOF'
import orjson
from pathlib import Path
from doppel.lab.brti_import import _iter_rows, load_raw
T = Path("/mnt/volume_nyc1_1793485122853/doppel")
import os
w0 = int(os.environ["W0"]) // 1000
w1 = orjson.loads((T / "tape/ref_brti_raw/import-watermark.json").read_bytes())["max_t_ms"] // 1000
assert w1 > w0, (w0, w1)
tape = load_raw(T / "tape", include_live=False)
s = T / "brti-stream"                                   # the importer's own two globs
files = sorted([*s.glob("*.jsonl.gz"), *s.glob("*.jsonl")])
src = {t // 1000 for f in files for t, _, _ in _iter_rows(f)}
want = {x for x in src if w0 < x <= w1}
missing = sorted(want - set(tape))
print({"checked": len(want), "missing_in_tape": len(missing), "examples": missing[:10]})
EOF
# expected: missing_in_tape == 0
# 5. Resume the schedule, then watch two runs (30 min) and the next hour's receipt.
systemctl start doppel-brti-import.timer
```

Expected behaviour of the frozen importer across the switch, which `test_cutover_switch_is_gap_free`
reproduces:
- The state's `done` list names PERELMAN's gzipped hours. The DOPPEL stream uses the same names,
  so those hours are skipped. Every row in them is at or below the watermark anyway.
- The open hour and every later hour are read. Seconds above `max_t` are appended with DOPPEL's
  `received` stamps.
- A second at or below the watermark that PERELMAN missed and DOPPEL has goes to
  `import-amendments.jsonl`, never to the tape. This is expected and harmless: the scored tape
  only ever grows forward.
- Stamps before the switch are PERELMAN's, and stamps after it are DOPPEL's. Both are first-seen
  receive times of a 0.5 s poll on the same endpoint.

### Phase 4: retire PERELMAN's recorder (after phase 3 has run cleanly for at least 24 h)

```sh
systemctl disable --now perelman-recorder.service
chmod -R a-w /var/lib/private/perelman/stream          # audit copy; never `systemctl clean perelman-recorder`
# perelman-recorder no longer prunes, so the 14-day stream is now kept as it stands.
```

Then commit the new `--src` into `ops/systemd/doppel-brti-import.service` and delete the drop-in.
A later change can also drop `User=root` and `ReadOnlyPaths=/var/lib/private/perelman` from that
unit, because the DOPPEL stream is readable by `doppel`. That is separate from this cutover.
`perelman-alt` and `perelman-perps` are outside this plan.

## Rollback

| When | Action |
|---|---|
| Any shadow phase | `systemctl disable --now doppel-brti`. Scoring never read this stream, so there is no other effect. |
| Phase 3, before phase 4 (PERELMAN still running) | `rm /etc/systemd/system/doppel-brti-import.service.d/src.conf && systemctl daemon-reload && systemctl start doppel-brti-import.service`. The same importer state reads PERELMAN's open hour again. Seconds at or below the watermark are skipped or amended, and newer ones are appended, so no gap opens. Restoring `brti-import-state.pre-cutover.json` is **not** needed and must not be done after any DOPPEL-sourced import, because the importer refuses a state that is behind its watermark. |
| After phase 4 | Keep `--src` on DOPPEL unless the DOPPEL recorder itself is broken. If it is, run `chmod -R u+w /var/lib/private/perelman/stream && systemctl enable --now perelman-recorder` and then roll back as in the row above. Seconds that neither recorder saw while both were down remain a gap. Each poll covers about 120 s, so a restart within 2 minutes loses nothing. |
