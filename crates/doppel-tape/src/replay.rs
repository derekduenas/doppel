//! `doppel-replica-replay`: the BRTI replica (`doppel_fair::brti::Replica`) run over the recorded
//! `spot_l2_<venue>` streams, with honest timestamps.
//!
//! - Each stream is read in file order (segment hours in order, records in seq order). The streams
//!   are merged on each one's next record's receive time `tw`, ties broken by venue order
//!   ([`Venue::ALL`]); a stream's own order is never changed, so a control record (which resets
//!   that venue's book) stays where the recorder wrote it.
//! - From `start` to `from` records are only decoded (books built, no index): the delta venues
//!   (Coinbase, Kraken, Gemini) snapshot only at connect, so `start` must reach back to a connect.
//! - At the first record at or after `from`, the replica is seeded with every venue's current book
//!   in [`Venue::ALL`] order (this pins the consolidation order), with an empty hysteresis set.
//!   From then on every record goes to `Replica::apply(venue, book, calc_ns = tw)`. A venue whose
//!   book is invalid (before its snapshot, after a reset) is applied as an empty book, so it
//!   leaves the index at once (`no_book`).
//! - For each whole second `s` in `[from, to)` one row: the replica output of the first record
//!   with `tw >= s`, and that `tw` as `known_ns` (the "known at" view: nothing after `known_ns`
//!   went into it). `value` is null when the replica failed (`reason` is the failure) or when the
//!   tape ends before `s` (`reason: "no_data"`).
//!
//! Rows depend only on the tape; timings go to the [`Summary`] only.

use std::io;
use std::path::{Path, PathBuf};
use std::time::Instant;

use doppel_fair::brti::{BookSnapshot, Replica, ReplicaOutput, VenueDiag, VenueStatus};
use doppel_fair::venue::{Venue, VenueBook};
use serde_json::{json, Map, Value};

use crate::segment::{segment_path, segment_records};

const NS: i64 = 1_000_000_000;
const HOUR_NS: i64 = 3600 * NS;

pub const USAGE: &str = "usage: doppel-replica-replay --data TAPE_ROOT --from TS --to TS --out FILE.jsonl [--start TS]\n\
TS is unix seconds or RFC 3339 UTC (2026-11-26T13:00:00Z). --start (decode-only warm-up) defaults to the start of --from's UTC hour.";

#[derive(Debug, PartialEq, Eq)]
pub struct Args {
    pub data: PathBuf,
    pub start_ns: i64,
    pub from_s: i64,
    pub to_s: i64,
    pub out: PathBuf,
}

/// Unix seconds, or RFC 3339 UTC; whole seconds only.
fn parse_ts(s: &str) -> Result<i64, String> {
    if let Ok(x) = s.parse::<i64>() {
        return Ok(x);
    }
    let ns = doppel_fair::venue::iso_ns(s).ok_or_else(|| format!("bad timestamp {s}"))?;
    if ns % NS != 0 {
        return Err(format!("{s}: whole seconds only"));
    }
    Ok(ns / NS)
}

pub fn parse_args(args: &[String]) -> Result<Args, String> {
    let (mut data, mut from, mut to, mut out, mut start) = (None, None, None, None, None);
    let mut it = args.iter();
    while let Some(a) = it.next() {
        let v = it.next().ok_or_else(|| format!("{a} needs a value\n{USAGE}"))?;
        match a.as_str() {
            "--data" => data = Some(PathBuf::from(v)),
            "--from" => from = Some(parse_ts(v)?),
            "--to" => to = Some(parse_ts(v)?),
            "--out" => out = Some(PathBuf::from(v)),
            "--start" => start = Some(parse_ts(v)?),
            other => return Err(format!("unknown argument {other}\n{USAGE}")),
        }
    }
    let need = |n: &str| format!("{n} is required\n{USAGE}");
    let (data, out) = (data.ok_or_else(|| need("--data"))?, out.ok_or_else(|| need("--out"))?);
    let (from_s, to_s) = (from.ok_or_else(|| need("--from"))?, to.ok_or_else(|| need("--to"))?);
    if to_s <= from_s {
        return Err("--to must be after --from".into());
    }
    let start_s = start.unwrap_or(from_s - from_s.rem_euclid(3600));
    if start_s > from_s {
        return Err("--start must not be after --from".into());
    }
    Ok(Args { data, start_ns: start_s * NS, from_s, to_s, out })
}

/// One output row (one second).
#[derive(Clone, Debug, PartialEq)]
pub struct Row {
    pub t: i64,
    pub value: Option<f64>,
    pub known_ns: Option<i64>,
    pub reason: Option<&'static str>,
    /// Venues in the index, sorted.
    pub used: Vec<String>,
    /// Per venue, in consolidation order: `used`, `reinstated`, `delayed`, `no_book`,
    /// `empty_side`, `crossed`, `outlier` or `held_out`.
    pub venues: Vec<(String, &'static str)>,
}

impl Row {
    pub fn to_json(&self) -> String {
        let venues: Map<String, Value> = self.venues.iter().map(|(v, s)| (v.clone(), Value::from(*s))).collect();
        json!({"t": self.t, "value": self.value, "known_ns": self.known_ns, "reason": self.reason, "used": self.used, "venues": venues})
            .to_string()
    }
}

/// Per-venue status label; `valid` is whether the decoder had a book.
pub fn status_label(d: &VenueDiag, valid: bool) -> &'static str {
    match d.status {
        VenueStatus::Used => "used",
        VenueStatus::Reinstated { .. } => "reinstated",
        VenueStatus::Delayed { .. } if !valid => "no_book",
        VenueStatus::Delayed { .. } => "delayed",
        VenueStatus::EmptySide if !valid => "no_book",
        VenueStatus::EmptySide => "empty_side",
        VenueStatus::Crossed => "crossed",
        VenueStatus::Outlier { .. } => "outlier",
        VenueStatus::HeldOut { .. } => "held_out",
    }
}

/// Timings and per-venue counts, for stderr.
#[derive(Debug, Default)]
pub struct Summary {
    pub records: u64,
    pub apply_ns: Vec<u64>,
    pub fallbacks: u64,
    pub rows: u64,
    pub null_rows: u64,
    /// Per venue: records, integrity failures, the first `tw` with a valid book, and the
    /// p50/p99 of `tw - venue timestamp` in ms over records with a valid book.
    pub venues: Map<String, Value>,
}

fn pct(sorted: &[i64], q: f64) -> Option<i64> {
    (!sorted.is_empty()).then(|| sorted[((sorted.len() - 1) as f64 * q).round() as usize])
}

impl Summary {
    pub fn to_json(&self) -> Value {
        let mut a: Vec<i64> = self.apply_ns.iter().map(|&x| x as i64).collect();
        a.sort_unstable();
        json!({
            "records": self.records, "updates": a.len(), "rows": self.rows, "null_rows": self.null_rows, "fallbacks": self.fallbacks,
            "apply_us_p50": pct(&a, 0.5).map(|x| x as f64 / 1e3), "apply_us_p99": pct(&a, 0.99).map(|x| x as f64 / 1e3),
            "apply_us_max": a.last().map(|&x| x as f64 / 1e3), "venues": self.venues,
        })
    }
}

/// One tape record of a venue's stream.
pub struct Rec {
    pub tw: i64,
    pub raw: String,
    pub meta: Option<Value>,
}

type Records = Box<dyn Iterator<Item = io::Result<Rec>>>;

/// A stream's records from the segments of every UTC hour in `[start_ns, end_ns]`, in order.
/// Records with `tw` before `start_ns` are dropped.
pub fn stream_records(root: &Path, stream: &str, start_ns: i64, end_ns: i64) -> Records {
    let files: Vec<PathBuf> = (start_ns.div_euclid(HOUR_NS)..=end_ns.div_euclid(HOUR_NS))
        .map(|h| segment_path(root, stream, h * HOUR_NS))
        .filter(|p| p.exists())
        .collect();
    let it = files.into_iter().flat_map(|f| -> Records {
        match segment_records(&f, true) {
            Ok(r) => Box::new(r.map(|x| x.and_then(rec))),
            Err(e) => Box::new(std::iter::once(Err(io::Error::new(e.kind(), format!("{}: {e}", f.display()))))),
        }
    });
    Box::new(it.filter(move |r| r.as_ref().map_or(true, |r| r.tw >= start_ns)))
}

fn rec(mut v: Value) -> io::Result<Rec> {
    let bad = || io::Error::new(io::ErrorKind::InvalidData, "record without tw/raw");
    let tw = v.get("tw").and_then(Value::as_i64).ok_or_else(bad)?;
    let raw = match v.get_mut("raw").map(Value::take) {
        Some(Value::String(s)) => s,
        _ => return Err(bad()),
    };
    Ok(Rec { tw, raw, meta: v.get_mut("meta").map(Value::take) })
}

/// The per-venue inputs: one record stream per venue, in [`Venue::ALL`] order.
pub fn tape_sources(root: &Path, start_ns: i64, to_s: i64) -> Vec<(Venue, Records)> {
    // one hour past `to`: the first record at or after the last second may be in the next file
    let end_ns = to_s * NS + HOUR_NS;
    Venue::ALL
        .into_iter()
        .filter(|v| root.join(format!("spot_l2_{}", v.name())).is_dir())
        .map(|v| (v, stream_records(root, &format!("spot_l2_{}", v.name()), start_ns, end_ns)))
        .collect()
}

struct Src {
    book: VenueBook,
    recs: Records,
    head: Option<Rec>,
    records: u64,
    first_valid: Option<i64>,
    lags: Vec<i64>,
}

impl Src {
    fn advance(&mut self) -> io::Result<()> {
        self.head = self.recs.next().transpose()?;
        Ok(())
    }

    fn book(&self, tw: i64) -> BookSnapshot {
        self.book.snapshot().unwrap_or(BookSnapshot { bids: Vec::new(), asks: Vec::new(), retrieved_ns: tw })
    }
}

fn row(t: i64, known_ns: i64, res: &Result<ReplicaOutput, doppel_fair::brti::ReplicaError>, srcs: &[Src]) -> Row {
    let (value, reason, used, diags) = match res {
        Ok(o) => (Some(o.value), None, o.venues.clone(), &o.exchanges),
        Err(e) => (None, Some(e.failure.message()), Vec::new(), &e.exchanges),
    };
    let venues = diags
        .iter()
        .map(|d| {
            let valid = srcs.iter().any(|s| s.book.venue.name() == d.venue && s.book.book.valid);
            (d.venue.clone(), status_label(d, valid))
        })
        .collect();
    Row { t, value, known_ns: Some(known_ns), reason, used, venues }
}

/// Run the replay over `sources` (one record stream per venue, in the fixed venue order) and
/// hand each row to `emit`, in order.
pub fn replay(sources: Vec<(Venue, Records)>, from_s: i64, to_s: i64, emit: &mut dyn FnMut(&Row) -> io::Result<()>) -> io::Result<Summary> {
    let mut srcs: Vec<Src> = Vec::new();
    for (v, recs) in sources {
        let mut s = Src { book: VenueBook::new(v), recs, head: None, records: 0, first_valid: None, lags: Vec::new() };
        s.advance()?;
        srcs.push(s);
    }
    let mut sum = Summary::default();
    let mut replica = Replica::default();
    let (mut seeded, mut next_s) = (false, from_s);
    let mut bad = vec![0u64; srcs.len()];
    while next_s < to_s {
        // the stream whose next record was received first (ties: venue order)
        let Some(i) = (0..srcs.len()).filter(|&i| srcs[i].head.is_some()).min_by_key(|&i| srcs[i].head.as_ref().map(|r| r.tw)) else {
            break;
        };
        let r = srcs[i].head.take().expect("head");
        srcs[i].advance()?;
        sum.records += 1;
        if !seeded && r.tw >= from_s * NS {
            for s in &srcs {
                let _ = replica.apply(s.book.venue.name(), &s.book(r.tw), r.tw);
            }
            seeded = true;
        }
        let s = &mut srcs[i];
        s.records += 1;
        let venue_ts = s.book.book.venue_ts_ns;
        if s.book.apply_record(&r.raw, r.tw, r.meta.as_ref()).is_err() {
            bad[i] += 1;
        }
        if s.book.book.valid {
            s.first_valid.get_or_insert(r.tw);
            // only frames that carried a venue timestamp (not heartbeats or acks)
            if s.book.book.venue_ts_ns != venue_ts {
                s.lags.push(r.tw - s.book.book.venue_ts_ns);
            }
        }
        if !seeded {
            continue;
        }
        let book = s.book(r.tw);
        let t0 = Instant::now();
        let res = replica.apply(s.book.venue.name(), &book, r.tw);
        sum.apply_ns.push(t0.elapsed().as_nanos() as u64);
        while next_s < to_s && r.tw >= next_s * NS {
            let row = row(next_s, r.tw, &res, &srcs);
            sum.rows += 1;
            sum.null_rows += u64::from(row.value.is_none());
            emit(&row)?;
            next_s += 1;
        }
    }
    for t in next_s..to_s {
        sum.rows += 1;
        sum.null_rows += 1;
        emit(&Row { t, value: None, known_ns: None, reason: Some("no_data"), used: Vec::new(), venues: Vec::new() })?;
    }
    sum.fallbacks = replica.fallbacks;
    for (mut s, b) in srcs.into_iter().zip(bad) {
        s.lags.sort_unstable();
        let ms = |q| pct(&s.lags, q).map(|x| x as f64 / 1e6);
        let v = json!({"records": s.records, "integrity_failures": b, "first_valid_ns": s.first_valid, "lag_ms_p50": ms(0.5), "lag_ms_p99": ms(0.99)});
        sum.venues.insert(s.book.venue.name().to_string(), v);
    }
    Ok(sum)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn args() {
        let a = parse_args(&s(&["--data", "/d", "--from", "2026-11-26T13:16:40Z", "--to", "1795699010", "--out", "o.jsonl"])).unwrap();
        assert_eq!(
            a,
            Args { data: "/d".into(), start_ns: 1_795_698_000 * NS, from_s: 1_795_699_000, to_s: 1_795_699_010, out: "o.jsonl".into() }
        );
        let b = parse_args(&s(&["--data", "/d", "--from", "100", "--to", "200", "--out", "o", "--start", "50"])).unwrap();
        assert_eq!((b.start_ns, b.from_s), (50 * NS, 100));
        assert!(parse_args(&s(&["--data", "/d", "--from", "200", "--to", "100", "--out", "o"])).is_err());
        assert!(parse_args(&s(&["--data", "/d", "--from", "100", "--to", "200", "--out", "o", "--start", "101"])).is_err());
        assert!(parse_args(&s(&["--data", "/d", "--from", "2026-11-26T13:16:40.5Z", "--to", "1795699010", "--out", "o"])).is_err());
        assert!(parse_args(&s(&["--from", "1", "--to", "2", "--out", "o"])).is_err());
    }

    fn src(v: Venue, recs: Vec<(i64, Value, Option<Value>)>) -> (Venue, Records) {
        (v, Box::new(recs.into_iter().map(|(tw, raw, meta)| Ok(Rec { tw, raw: raw.to_string(), meta }))))
    }

    fn bitstamp(tw: i64, bid: &str, ask: &str) -> (i64, Value, Option<Value>) {
        (tw, json!({"event": "data", "data": {"bids": [[bid, "1"]], "asks": [[ask, "1"]], "microtimestamp": "1"}}), None)
    }

    #[test]
    fn known_at_rows_no_book_and_no_data() {
        let lmax = |tw: i64, b: &str| {
            (
                tw,
                json!({"bids": [{"price": b, "quantity": "1"}], "asks": [{"price": "102", "quantity": "1"}]}),
                Some(json!({"status": 200})),
            )
        };
        let sources = vec![
            src(Venue::Bitstamp, vec![bitstamp(5 * NS, "100", "101"), bitstamp(10 * NS + 7, "100", "102"), bitstamp(13 * NS, "99", "101")]),
            src(Venue::Lmax, vec![lmax(9 * NS, "101"), (11 * NS, json!({}), Some(json!({"status": 429})))]),
        ];
        let mut rows = Vec::new();
        let sum = replay(sources, 10, 15, &mut |r| {
            rows.push(r.clone());
            Ok(())
        })
        .unwrap();
        let t: Vec<_> = rows.iter().map(|r| (r.t, r.known_ns)).collect();
        // second 10: first record at or after 10 s; 11: the failed LMAX poll; 12 and 13: the 13 s frame
        assert_eq!(t, [(10, Some(10 * NS + 7)), (11, Some(11 * NS)), (12, Some(13 * NS)), (13, Some(13 * NS)), (14, None)]);
        assert_eq!(rows[0].used, s(&["bitstamp", "lmax"]));
        assert_eq!(rows[0].venues, [("bitstamp".to_string(), "used"), ("lmax".to_string(), "used")]);
        assert_eq!(rows[1].venues[1], ("lmax".to_string(), "no_book"));
        assert_eq!(rows[1].used, s(&["bitstamp"]));
        assert_eq!(rows[4].reason, Some("no_data"));
        assert!(rows[4].value.is_none());
        // single-level books: the value is the mid of the consolidated touch
        assert_eq!(rows[2].value, Some(100.0));
        assert_eq!((sum.rows, sum.null_rows, sum.apply_ns.len()), (5, 1, 3));
        assert!(rows[2].to_json().starts_with(r#"{"t":12,"value":100.0,"known_ns":13000000000,"reason":null,"used":["bitstamp"]"#));
    }
}
