//! `doppel-replica-replay` on a real tape: `tests/fixtures_replay/` is the first 4.3 s of a
//! `doppel-taped run --only spot_l2` recording (2026-11-26 15:47:35Z, all seven venues incl. the
//! LMAX REST poll), lines byte-identical, recompressed at zstd level 19.
//!
//! Checks: the rows are deterministic (byte-identical across runs), and every row equals an
//! independent oracle: its own `VenueBook`s fed in the same merged order, and the exact full
//! `brti::calculate` on those books at every record (the same hysteresis evolution).

use std::collections::BTreeSet;
use std::path::PathBuf;

use doppel_fair::brti::{calculate, BookSnapshot};
use doppel_fair::venue::{Venue, VenueBook};
use doppel_tape::replay::{replay, status_label, stream_records, tape_sources, Rec, Row};

const NS: i64 = 1_000_000_000;
const START_S: i64 = 1_795_708_055; // the recording starts at 55.71 s
const FROM_S: i64 = 1_795_708_057;
const TO_S: i64 = 1_795_708_060; // the fixture ends just before 60 s

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures_replay")
}

fn run() -> (Vec<Row>, String) {
    let (mut rows, mut text) = (Vec::new(), String::new());
    let sum = replay(tape_sources(&root(), START_S * NS, TO_S), FROM_S, TO_S, &mut |r| {
        text.push_str(&r.to_json());
        text.push('\n');
        rows.push(r.clone());
        Ok(())
    })
    .unwrap();
    assert_eq!(sum.rows, (TO_S - FROM_S) as u64);
    (rows, text)
}

/// The fixture's records, merged by (tw, venue order) with each stream kept in file order.
fn merged() -> Vec<(usize, Rec)> {
    let mut all = Vec::new();
    for (vi, v) in Venue::ALL.iter().enumerate() {
        let recs: Vec<Rec> =
            stream_records(&root(), &format!("spot_l2_{}", v.name()), START_S * NS, TO_S * NS + 3600 * NS).map(Result::unwrap).collect();
        assert!(!recs.is_empty(), "{}", v.name());
        // with tw non-decreasing in every stream, a stable sort is that merge
        assert!(recs.windows(2).all(|w| w[0].tw <= w[1].tw), "{}", v.name());
        all.extend(recs.into_iter().map(|r| (vi, r)));
    }
    all.sort_by_key(|(vi, r)| (r.tw, *vi));
    all
}

#[test]
fn replay_is_deterministic_and_equals_calculate() {
    let (rows, text) = run();
    let (rows2, text2) = run();
    assert_eq!(text, text2);
    assert_eq!(rows, rows2);

    // the oracle
    let mut books: Vec<VenueBook> = Venue::ALL.iter().map(|&v| VenueBook::new(v)).collect();
    let snap = |b: &VenueBook, tw: i64| b.snapshot().unwrap_or(BookSnapshot { bids: vec![], asks: vec![], retrieved_ns: tw });
    let mut excluded = BTreeSet::new();
    let (mut seeded, mut next_s, mut want) = (false, FROM_S, Vec::new());
    for (vi, r) in merged() {
        if next_s >= TO_S {
            break;
        }
        if !seeded && r.tw >= FROM_S * NS {
            // the replay's seeding: venue k joins the replica, one calculation on the first k
            for k in 1..=books.len() {
                excluded = calculate(&all(&books[..k], &snap, r.tw), r.tw, &excluded).1;
            }
            seeded = true;
        }
        let _ = books[vi].apply_record(&r.raw, r.tw, r.meta.as_ref());
        if !seeded {
            continue;
        }
        let (res, next) = calculate(&all(&books, &snap, r.tw), r.tw, &excluded);
        excluded = next;
        while next_s < TO_S && r.tw >= next_s * NS {
            let diags = match &res {
                Ok(o) => &o.exchanges,
                Err(e) => &e.exchanges,
            };
            let venues: Vec<(String, &str)> = diags
                .iter()
                .map(|d| (d.venue.clone(), status_label(d, books.iter().any(|b| b.venue.name() == d.venue && b.book.valid))))
                .collect();
            want.push((next_s, res.as_ref().ok().map(|o| o.value.to_bits()), r.tw, venues));
            next_s += 1;
        }
    }
    let got: Vec<_> = rows.iter().map(|r| (r.t, r.value.map(f64::to_bits), r.known_ns.unwrap(), r.venues.clone())).collect();
    assert_eq!(got, want);

    // and the tape is a real seven-venue book: every venue in the index in the last second
    let last = rows.last().unwrap();
    assert_eq!(last.used, ["bitstamp", "bullish", "coinbase", "cryptocom", "gemini", "kraken", "lmax"]);
    assert!(rows.iter().all(|r| r.value.is_some_and(|v| (10_000.0..1_000_000.0).contains(&v))));
}

fn all(books: &[VenueBook], snap: &dyn Fn(&VenueBook, i64) -> BookSnapshot, tw: i64) -> Vec<(String, BookSnapshot)> {
    books.iter().map(|b| (b.venue.name().to_string(), snap(b, tw))).collect()
}
