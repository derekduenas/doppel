# H1 hourly reference recovery

The original audit found 12 Nov 26 raw BRTI reference segments whose final size exceeds the
size in the original hourly receipt. The separate recovery view covers Nov 26 00:00–22:00 UTC
(exclusive end). It includes earlier available reference and market inputs for warmup.
The 00 and 01 UTC prefixes also match an original manifest seal written before the receipt.
The ten other prefixes carry `evidence_use=diagnostic_only` in their new manifest rows.

For each of the 12 affected hours the helper checked the complete archived file against its
seal, copied the exact receipt-length byte prefix, and verified strict Zstandard-frame integrity,
per-record CRCs, counts, control totals, first and last timestamps, and maximum silence against
the original receipt. All 37,924 recovered observations matched archived PERELMAN publication
milliseconds, value and receive time. PERELMAN was the importer's original source. The retained
proof includes the compressed input file hashes and the corresponding original upstream lines.

| Evidence class | Hours | Recovered observations |
| --- | ---: | ---: |
| Contemporaneous prefix seal and upstream match | 2 | 5,450 |
| Authenticated final-file prefix, original receipt summary, and upstream match | 10 | 32,474 |

The latter ten hours have no contemporaneous checksum for their particular byte boundary. They
can support diagnosis. The revised evaluator refuses a binding replay when the selected input
contains any of these marked generations. H1 still requires admissible data across its full
registered interval for a binding result. The historical receipts, final source segments, and
original manifest records remain accessible with their original contents.

## Apex evidence

The retained run is under
`/var/backups/doppel-sealed-20261126/research/input-recovery.OigFxO`.

- `closed-inputs` preserves the next two closed hours (20 and 21 UTC), 30 segments across
  15 registered-input streams and two original receipts. It copied 1,083,812,109 bytes onto the
  separate archive disk. The archive tool reported zero missing inputs and zero receipt issues.
- `h1/recovery/tape` is the independently copied H1 view with original receipts and isolated
  reference generations. `h1/recovery/originals` contains the complete committed lines of
  the historical raw-reference manifest and corroborating subsets of the upstream captures.
- `h1/recovery/report.json` is the [recovery receipt](recovery-report.json). Its SHA-256 is
  `5baffc99b45017e2ca52512f8fa5221ab8265548737d58ebe256652897d37d4e`. The initial standalone
  evaluator from `2688f00` verified all 22 hours and strictly read 182 files. Its fingerprint
  was `3f238ffb8b5b5b33177155dc0476fae67d385a16c2230f603fe12a11c81b6594`.
- The [final review check](final-review.json) strictly read 187 input files, including the five stream
  manifests. All 22 hourly checks passed and the fingerprint was
  `a73ff4ca16e2fbe9152abdea35bd0fb9e467fb0d53238ab8ed3fd7a5000e0940`. Binding admission
  rejected the 02 UTC raw-reference generation. Both checks ran before any strategy execution.
  All tape and upstream capture bytes are on Apex.
- The [preceding full check](review-check.json) produced the same complete input fingerprint
  before the evaluator added token-map provenance to its result. The independently copied
  recovery view now has read-only file and directory permissions.
- `closed-hour-22` is a separate post-rollover extension with 571,858,846 bytes across 15
  streams. Strict required-receipt mode returned incomplete while the recorder worked and
  succeeded once the original receipt was published at 23:04:46 UTC. All five original raw
  BRTI generations matched the corresponding archived byte-prefix checksums. The receipt
  covered all 3,600 raw reference seconds. Canonical input integrity passed for this hour.
  At 23:05:56 UTC, 348,266,496 bytes (332 MiB) remained above the configured 35 GiB archive
  reserve. Full stream histories and the source comparison remain on Apex. [Extension receipt](hour22-proof.json)

Use the review checkout and the Apex Python environment to repeat the checks. A new recovery
build needs a fresh output directory. Inspection of an existing view only reads it.

```sh
root=/var/backups/doppel-sealed-20261126
audit="$root/research/input-recovery.OigFxO"
PYTHONPATH=src /opt/doppel/.venv/bin/python \
  evidence/2026-11-26-brti-recovery/check_snapshot.py "$audit/h1/recovery"
```

The original build used `ops/recover_brti_receipts.py --from 20261126/00 --through 20261126/22`
with archive roots `$root/tape` and `$audit/closed-inputs`, the hot raw BRTI manifest
and `/var/lib/private/perelman/stream` as upstream. The hot manifest was frozen for recovery in
`$audit/h1/recovery/originals/ref_brti_raw-manifest.jsonl`. Use this frozen history for replication.
