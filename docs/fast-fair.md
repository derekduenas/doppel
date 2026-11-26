# Fast fair: `crates/doppel-fair` (W6 / C2)

Two engines in Rust. Both are pure and deterministic, with no I/O and no clock, and neither is wired into a recorder or a service.

| Module | Port of | What it does |
|---|---|---|
| `fair` | `doppel.lab.fair.fair_up_raw` (fair-v1) | P(settle ≥ ptb) at decision second `d`, from the raw 1 Hz BRTI received so far. The samples already received count as-is; the remaining ones are modelled as a zero-drift walk with variance taken from the trailing `vol_window` s. |
| `brti` | PERELMAN `brti.py` (CF RTI methodology v17.0) | Index value from constituent L2 books: staleness (> 10 s), erroneous-book and 5 % outlier filters, re-instatement hysteresis below 2.5 %, a dynamic order-size cap (trimmed mean + 5σ, not a fixed 100 BTC; 100 is the target level count), uncrossing, spacing (static/100 levels vs KDE mode), and the mid-price-volume curve with exponential depth weights. |
| `book` | the `Book` container in PERELMAN `books.py` | An in-memory L2 book (`put`, `replace`, `reset`, `best`) that produces a `BookSnapshot` stamped with the last update in ns. |

The replica's output also says why each venue was or was not used: `Used`, `Reinstated`, `Delayed{lag_ns}`, `EmptySide`, `Crossed`, `Outlier{dev}` or `HeldOut{dev}`. The hysteresis set is explicit state: `brti::calculate(books, calc_ns, &excluded)` returns the next set, and `Replica` wraps it.

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
  - The full-depth recorded books (45,697 levels, not committed) are also bit-exact against `brti.py`: value 60258.135944245965, spacing 0.013619679700137633, 21,850 curve levels.
- **Python** (`tests/test_fair_parity_fixtures.py`): regenerates the inputs and recomputes the outputs with `fair.py` and `brti.py`, and checks a no-look-ahead property.
- **Tolerances:** the fixtures were generated on macOS with CPython 3.12. Both sides compare with the tolerances in `parity.rs`, so another libm or CPython 3.11 (naive `sum`) should still pass. The `two_valued_sizes` case is an exact KDE tie, which last-bit rounding decides, so it is the case most likely to move on another platform.

The replica can be exact because it reproduces the reference's float arithmetic:

- CPython 3.12 `sum` (Neumaier) where the reference uses `sum`;
- numpy pairwise summation inside `np.mean`;
- libm `pow` where the reference writes `** 2`. On macOS, `pow(x, 2)` differs from `x*x` in about 0.1 % of inputs.

The speedups do not change the result:

- **Price-volume walk:** a cursor replaces the rescan.
- **KDE mode:** a branch-and-bound over the 4001-point grid with rigorous bounds, then the reference arithmetic on the surviving points. Kernel terms beyond 40 h are exactly 0.0 and are skipped.

## Deliberate differences from the Python

- **Window before `t0`:** fair-v1 returns `None` when the averaging window starts before `t0`. numpy would wrap the negative index. This only happens for a decision made at least 240 s after the window closes.
- **Empty consolidated side:** a direct `brti_from_books` call with an empty side returns `EmptySide`. Python raises `IndexError` there; the replica cannot reach it.
- **Curve step limit:** `MAX_CURVE_STEPS = 10^7`. A spacing that would need more steps than this fails with `CurveTooFine` (dust-sized spacing). Python would just run for a very long time.
- **NaN prices in `L2Book`:** they are ignored on input. The replica's cleaning drops them anyway.

## Bench

Run `cargo run --release -p doppel-fair --bin doppel-fair-bench`. Numbers below are from this Mac (x86_64, 2026-11-25):

| Evaluation | p50 | p99 |
|---|---|---|
| fair-v1, d = E−30 | 0.9 µs | 1.3 µs |
| fair-v1, d = E−240 | 1.6 µs | 2.0 µs |
| replica, recorded 6 venues, 3,091 levels (fixture) | 2.4 ms | 3.2 ms |
| replica, recorded 6 venues, 45,697 levels (full depth) | 5.5 ms | 7.4 ms |
| replica, synthetic 5 venues × 2 × 2,000 levels | 21 ms | 28 ms |

For comparison, Python `brti.py` takes about 2 s on the recorded books and 9.4 s on the full snapshot.

Against BLUEPRINT C6's ≤ 1 ms compute budget, fair-v1 is well inside it and **the replica is not**: 2.4 to 5.5 ms p50 on real books. Its cost grows with the levels inside the 5 % band (the consolidation sort, the KDE mode search, and the curve walk); this has not been profiled per stage. Incremental book maintenance and a true hot path are W6 work.

## Not done here

- **No validation against published BRTI.** W6's acceptance is $0.01 p99.
- **AMBIGUITY points unresolved.** The points `brti.py` marks as AMBIGUITY (the KDE grid, which book the spacing is taken from) are not settled.
- **No venue frame decoders.** These are the `books.py` WebSocket handlers.
- **No wiring.**
