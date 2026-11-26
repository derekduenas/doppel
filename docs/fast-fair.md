# Fast fair: `crates/doppel-fair` (W6 / C2)

Two engines in Rust. Both are pure and deterministic, with no I/O and no clock, and neither is wired into a recorder or a service.

| Module | Port of | What it does |
|---|---|---|
| `fair` | `doppel.lab.fair.fair_up_raw` (fair-v1) | P(settle ≥ ptb) at decision second `d`, from the raw 1 Hz BRTI received so far. The samples already received count as-is; the remaining ones are modelled as a zero-drift walk with variance taken from the trailing `vol_window` s. |
| `brti` | PERELMAN `brti.py` (CF RTI methodology v17.0) | Index value from constituent L2 books: staleness (> 10 s), erroneous-book and 5 % outlier filters, re-instatement hysteresis below 2.5 %, a dynamic order-size cap (trimmed mean + 5σ, not a fixed 100 BTC; 100 is the target level count), uncrossing, spacing (static/100 levels vs KDE mode), and the mid-price-volume curve with exponential depth weights. |
| `book` | the `Book` container in PERELMAN `books.py` | An in-memory L2 book (`put`, `replace`, `reset`, `best`) that produces a `BookSnapshot` stamped with the last update in ns. |
| `venue` | the WebSocket handlers in `books.py` | Rebuilds one venue's `L2Book` from the recorder's raw `spot_l2_<venue>` tape records (see below). |

The replica's output also says why each venue was or was not used: `Used`, `Reinstated`, `Delayed{lag_ns}`, `EmptySide`, `Crossed`, `Outlier{dev}` or `HeldOut{dev}`. The hysteresis set is explicit state: `brti::calculate(books, calc_ns, &excluded)` returns the next set, and `Replica` wraps it.

`Replica` has two entry points that return identical results:

- **`calculate(books, calc_ns)`** is the exact computation from a full snapshot of every venue.
- **`apply(venue, book, calc_ns)`** is the live path: one venue's new book.
  - It returns the same `ReplicaOutput` as `calculate`, bit for bit, on all current books in first-seen venue order with the same hysteresis state.
  - It takes `calc_ns`, because staleness needs a clock.
  - It re-cleans and re-sorts only that venue.
  - It reads only the band the methodology can use: every ask up to 1.05 × the first consolidated ask above the best bid, and symmetrically for bids. That band contains both 5 % sample bands (before and after uncrossing), every level uncrossing can consume, and the whole curve walk.
  - Deeper levels enter only through the side totals. Those matter only in three comparisons (static vs KDE spacing, the curve's depth limit, the 10^7 step cap), and `apply` decides them from bounds.
  - When a bound cannot decide, or the band has fewer than 50 levels while deeper ones exist, `apply` recomputes on the full books and counts it in `Replica::fallbacks`.

## Parity

Fixtures live in `tests/fixtures_fair/` and are written by `gen_fixtures.py`:

- `fair_v1.json`: 4 raw paths (calm, volatile with gaps, flat, dyadic) and 5,320 cases. These cover `d` before, inside, at and after the 60 s average, lags −2/0/1/3, vol windows 60/300/301/600, every `None` path, and exact ties.
- `brti_replica.json`: 17 scenarios and 3 direct `brti_from_books` cases, produced by PERELMAN `brti.py`. The scenarios are:
  - the PERELMAN unit cases;
  - a 5-step hysteresis sequence;
  - the staleness boundary at exactly 10 s and at 10 s + 1 ns;
  - dirty and duplicate levels;
  - a consolidated book that has to be uncrossed;
  - an empty curve;
  - KDE edge cases;
  - a recorded 6-venue snapshot from 2026-11-24, top 300 levels per side.

`brti.py` is vendored verbatim as `perelman_brti.py`. Its sha256 and the PERELMAN commit are recorded in the fixture, so nothing needs `~/perelman`.

Floats are stored as Python repr strings, which round-trip exactly in both languages.

- **Rust** (`crates/doppel-fair/tests/parity.rs`):
  - fair-v1 max |diff| is 1.1e-16 (3,700 of 3,976 valued cases are bit-exact; the rest differ in the last bits, most likely from `erf`, libm crate vs macOS libm).
  - The replica is bit-exact on all 20 values.
  - The full-depth recorded books (45,697 levels) are bit-exact against `brti.py`: value 60258.135944245965, spacing 0.013619679700137633, 21,850 curve levels. They are committed as `crates/doppel-fair/tests/books/perelman_snap_2026_11_24.bin` (731 KB; the layout is in `tests/incremental.rs`; `data/` is gitignored).
  - Every replica fixture step is also run through `apply` and must equal `calculate`.
- **Incremental** (`crates/doppel-fair/tests/incremental.rs`): replays of single-venue updates check that `apply` equals `calculate` on every step, value bits and every diagnostic included. The updates are:
  - size changes and cancels near the touch;
  - new out-of-order levels;
  - whole-book price moves;
  - stale, crossed and emptied books;
  - 6 % outliers that later return, so every venue status occurs.

  There are three replays:
  - 150 steps at full depth;
  - 600 steps on the fixture books;
  - 400 steps on sparse books, which take the full-book fallback 288 times.
- **Direct check against `brti.py`:** `DOPPEL_DUMP_REPLAY=<dir>` writes a sample of replay steps (40 fixture-depth, 3 full-depth). The vendored `brti.py` gave the identical value repr on all 43, checked 2026-11-26.
- **Python** (`tests/test_fair_parity_fixtures.py`): regenerates the inputs and recomputes the outputs with `fair.py` and `brti.py`, and checks a no-look-ahead property.
- **Tolerances:** the fixtures were generated on macOS with CPython 3.12. Both sides compare with the tolerances in `parity.rs`, so another libm or CPython 3.11 (naive `sum`) should still pass. The `two_valued_sizes` case is an exact KDE tie, which last-bit rounding decides, so it is the case most likely to move on another platform.

The replica can be exact because it reproduces the reference's float arithmetic:

- CPython 3.12 `sum` (Neumaier) where the reference uses `sum`;
- numpy pairwise summation inside `np.mean`;
- libm `pow` where the reference writes `** 2`. On macOS, `pow(x, 2)` differs from `x*x` in about 0.1 % of inputs.

The speedups do not change the result:

- **Consolidation:** each venue side is merged in price order (ties keep venue order), not re-sorted. This is the same order and summation order as a stable sort of the concatenation.
- **Size sorts:** a bucket pass on the top 16 key bits, then small sorts.
- **5 % sample counts:** a binary search, since the band is a prefix of a sorted side.
- **Price-volume walk:** a cursor replaces the rescan. The steps that stay on the same pair of levels are added as one run, with their bounds checked at the run's end.
- **KDE mode:** found in two passes.
  1. A certified candidate search: binned upper bounds per block of grid points, then a degree-p Taylor expansion of the kernel sum per block, with explicit truncation, rounding and tail bounds.
  2. The reference arithmetic, run only on the grid points whose value could be the maximum, usually one.

  Kernel terms beyond 40 h are exactly 0.0 and are skipped. Bit-identical inputs reuse the previous `pow`/`exp` result.
- **`x*x` for `pow(x, 2)`:** a Dekker two-product gives the exact rounding error of `x*x`. When that error is below 0.375 of the gap to the neighbouring double, `x*x` is returned, which is about 75 % of calls; otherwise libm `pow` decides.

  This rests on a measured assumption, not a proof: libm `pow(x, 2)` must stay within 0.625 ulp. On this Mac the worst seen was 0.515 ulp over 5·10^7 random inputs, and glibc documents about 0.52. A unit test checks 2·10^6 random inputs against libm. If a libm broke the assumption, the replay and fixture tests are where it would show.

## Deliberate differences from the Python

- **Window before `t0`:** fair-v1 returns `None` when the averaging window starts before `t0`. numpy would wrap the negative index. This only happens for a decision made at least 240 s after the window closes.
- **Empty consolidated side:** a direct `brti_from_books` call with an empty side returns `EmptySide`. Python raises `IndexError` there; the replica cannot reach it.
- **Curve step limit:** `MAX_CURVE_STEPS = 10^7`. A spacing that would need more steps than this fails with `CurveTooFine` (dust-sized spacing). Python would just run for a very long time.
- **NaN prices in `L2Book`:** they are ignored on input. The replica's cleaning drops them anyway.

## Bench

Run `cargo run --release -p doppel-fair --bin doppel-fair-bench`. It prints min/p50/p99/max for:

- fair-v1;
- `calculate` on a full snapshot;
- `apply` over a 5,000-step replay of single-venue updates;
- a per-stage profile of both.

It runs on the fixture books and on full depth. The machine is a 2-core i5-5250U at 1.6 GHz, shared with other agents, so the load average is recorded with each run.

Earlier numbers (2026-11-25, before the incremental work):

| Evaluation | p50 | p99 |
|---|---|---|
| fair-v1, d = E−30 | 0.9 µs | 1.3 µs |
| fair-v1, d = E−240 | 1.6 µs | 2.0 µs |
| replica, recorded 6 venues, 3,091 levels (fixture) | 2.4 ms | 3.2 ms |
| replica, recorded 6 venues, 45,697 levels (full depth) | 5.5 ms | 7.4 ms |
| replica, synthetic 5 venues × 2 × 2,000 levels | 21 ms | 28 ms |

For comparison, Python `brti.py` takes about 2 s on the recorded books and 9.4 s on the full snapshot.

Numbers after the incremental work, on 2026-11-26. "Before" is the main crate at 74fa902, measured in the same session. The machine never got quiet: the final run was at load ≈ 5.6, and the best run was at load ≈ 2.8.

| Evaluation (release) | before p50 / p99 | after p50 / p99, load 2.8 | after p50 / p99, load 5.6 |
|---|---|---|---|
| `calculate`, fixture, 3,091 levels | 3.15 / 5.41 ms | 0.64 / 0.75 ms | 0.80 / 1.84 ms |
| `calculate`, full depth, 45,697 levels | 7.26 / 22.96 ms | 2.15 / 2.51 ms | 2.53 / 4.83 ms |
| `apply`, fixture | n/a | 0.59 / 0.86 ms | 0.91 / 3.60 ms |
| `apply`, full depth | n/a | 1.14 / 1.85 ms | 1.26 / 2.70 ms |

Replay results:

- 0 fallbacks to full books in 5,000 steps.
- 0 mismatches in the spot checks against `calculate`.

`apply` stage p50 at full depth, load 5.6:

| Stage | p50 |
|---|---|
| venue update | 11 µs |
| band + merge | 111 µs |
| order-size cap | 227 µs |
| spacing sums + sample | 70 µs |
| KDE mode | 203 µs |
| curve walk | 78 µs |
| weights | 388 µs |

`calculate` at full depth also pays for cleaning (347 µs) and consolidating (835 µs) every venue. `apply` does not.

**Budget:** BLUEPRINT C6 is ≤ 1 ms compute. fair-v1 (1 to 2 µs) meets it.

The replica `apply` at full depth **does not meet it on this machine**: 1.14 ms p50 and 1.85 ms p99 at best. At fixture depth it does meet it (0.86 ms p99) when the machine is quiet.

What remains is work the methodology does once per update, whatever is cached:

- **Weights:** one libm `exp` per curve point (21,850 on the recorded books) plus two sequential Neumaier sums, about 0.39 ms.
- **Order-size cap:** sort, three sums and `pow` over about 5,000 band sizes.
- **KDE mode.**

`exp` and the Neumaier order are what bit parity with `brti.py` pins. The remaining levers are a second core for the `exp` calls, or a faster machine; no threads are used here.

## Not done here

- **No p99 ≤ 1 ms for `apply` at full depth** on this Mac (see Bench).
- **No validation against published BRTI.** W6's acceptance is $0.01 p99.
- **AMBIGUITY points unresolved.** The points `brti.py` marks as AMBIGUITY (the KDE grid, which book the spacing is taken from) are not settled.
- **No wiring.** The decoders exist (below) but nothing runs the replica over the tape yet.

## Constituent books: `spot_l2_<venue>` streams and `venue` decoders

`doppel-taped` records BTC-USD L2 for six of the seven constituents as raw frames, using the channels from PERELMAN `recorder.py`: `coinbase` (Advanced Trade `level2` + `heartbeats`), `kraken` (v2 `book`, depth 1000), `bitstamp` (`order_book_btcusd`, a full top-100 book per change), `gemini` (v1 market data), `cryptocom` (`book.BTC_USD.150`, a full 150-level book about 2.3 times a second; its delta mode exists only at depth 50 and rejects 150), `bullish` (`l2Orderbook`, full snapshots). LMAX Digital has no public WebSocket, so it is not recorded; PERELMAN polls its REST book.

These streams are opt-in: `--only spot_l2` starts all six, `spot_l2_<venue>` starts one, and `all` (the default) starts none. No systemd unit runs them.

Continuity:

- **Sequence numbers.** Coinbase `sequence_num` and Gemini `socket_sequence` count every frame on the connection, heartbeats included, so heartbeats are recorded. Crypto.com deltas carry `pu`, which must equal the previous `u`. On a jump the recorder writes `ctl: seq_gap {expected, got}` and reconnects; the new connection brings a fresh snapshot and restarts the sequence.
- **Kraken checksum.** Kraken has no sequence number, only a CRC32 over the top 10 levels per side. The decoder checks it offline, so the recorder holds no book state.
- **Backpressure.** A backpressure drop leaves a hole in the tape even while the socket is healthy. The delta venues therefore reconnect when `backpressure_drops` increases.
- **No data.** Recorded heartbeats keep the stall watchdog quiet, so a subscription the venue rejected could otherwise look healthy forever (seen live with Crypto.com delta mode at depth 150). Each venue must deliver a book frame at least every 30 s, or the recorder writes `ctl: no_data` and reconnects.

`venue::VenueBook::apply_record(raw, tw, meta)` replays a stream. Any control record (connect, disconnect, seq_gap, backpressure_drop, ...) resets the book, and so does an integrity failure. Either way the book stays invalid until the next snapshot. `retrieved_ns` is the record's receive time `tw`, never a venue timestamp. Gemini's WebSocket lagged about 5 s on 2026-11-24, which is why PERELMAN switched to its REST book; check `tw` against `venue_ts_ns` before trusting it.
