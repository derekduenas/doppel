# BTC 5-minute answer-key lab inputs

The seed-1 baseline on Apex started at 01:44:40 UTC on Nov 27 with code `3dee8bb` and the
November 26 03:34 token map. In the simulator's own window-selection function that map
contains zero of the 36 training pairs and zero of the 36 holdout pairs for the requested
six hours. All six source book segments exist. The baseline was left running. Its results
cannot establish strategy recovery for these windows.

## Separate lab metadata

The [token map](tokens.jsonl) contains 144 public outcome tokens for 72 distinct conditions
from Nov 26 06:00–12:00 UTC. The split is 09:00 UTC. Its SHA-256 is
`7f4e995fa7f11f9a2f326170967076161016272dd0b60e6795f7108ca03e3809`.
Fetching the 72 exact market slugs took 72 unauthenticated requests to the venue’s Gamma
market-by-slug endpoint. Each response had the requested slug, exact start and end, a
closed timestamp at or after the end, a resolved UMA status, two distinct token IDs,
ordered Up/Down labels, and complementary binary outcome prices. The [retrieval receipt](gamma-provenance.json)
records the individual response digests, market IDs, venue close times and retrieval interval.

The map is specific to this retrospective lab. The original registered strategy map on Apex
continues to have SHA-256 `72f6055444f589d317fb24716b0dc9bb222a6d63b00fbc540ec82534abac0875`.
Resolution enters the hidden-signal subject and retrospective scoring under the existing lab
protocol. It must not be exposed as a predictor to the reconstruction tools.

## Repeating the input check

Run from a checkout containing the preflight on Apex. The verifier reads the sealed books and
compares the in-window token and condition IDs with the lab map. It also checks compressed
file SHA-256 against the manifest and reads every frame with CRC verification.

```sh
PYTHONPATH=src /opt/doppel/.venv/bin/python -m doppel.lab.answerkey_inputs \
  --data /var/backups/doppel-sealed-20261126/tape \
  --tokens evidence/2026-11-27-answerkey-inputs/tokens.jsonl \
  --stream clob_btc_5m --from 2026-11-26T06:00:00Z \
  --split 2026-11-26T09:00:00Z --to 2026-11-26T12:00:00Z
```

The [Apex receipt](book-preflight.json) matched all 36 training pairs and all 36 holdout
pairs. The six archived segment hashes matched the corresponding hot manifest entries,
and their listed byte lengths matched the hot files.

Positive coverage establishes the input identity and the presence of book quotes in each
window. Eligible windows, actual subject fills, and accuracy still have to be measured on
a run using these inputs.

The [reference availability check](fair-availability.json) ran the original simulator’s
`Fair.at` with the recorded receive-time mask on the hot and archived roots. All six
`ref_brti_raw` and `ref_prices` hour files were present in each. All 72 windows returned
at least one finite fair value at the four checked offsets. Of 288 checks, 284 were
available on each root. Fair-value error remains a separate measurement.
