//! DOPPEL fast fair (roadmap W6 / C2). Two pure, deterministic engines:
//!
//! - [`brti`]: CF Benchmarks BRTI v17 replica from constituent L2 books, a port of PERELMAN
//!   `brti.py` (outlier + staleness filters, dynamic order-size cap, uncrossing, mid-price-volume
//!   curve with exponential depth weighting), with per-venue diagnostics. [`book`] is the
//!   in-memory book container from PERELMAN `books.py`.
//! - [`fair`]: fair-v1, an exact port of `doppel.lab.fair.fair_up_raw`.
//! - [`venue`]: per-venue L2 frame decoders (the `books.py` WebSocket handlers) that rebuild
//!   books from the recorder's raw `spot_l2_<venue>` tape.
//!
//! Parity with the Python references is checked against `tests/fixtures_fair/` (see
//! `docs/fast-fair.md`). Not wired into any recorder or service.
#![forbid(unsafe_code)]

pub mod book;
pub mod brti;
pub mod fair;
pub mod pymath;
pub mod venue;
