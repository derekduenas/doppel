//! `Replica::apply` (incremental, banded) must return exactly what `Replica::calculate` (full,
//! exact) returns on every step of a replay, and both must match `brti.py` on the full-depth
//! recorded books.

use std::path::PathBuf;

use doppel_fair::brti::{BookSnapshot, Level, Replica, ReplicaError, ReplicaOutput};

const S: i64 = 1_000_000_000;

/// `tests/books/perelman_snap_2026_11_24.bin`: PERELMAN snap/ REST books of 2026-11-24, full depth
/// (45,697 levels). Layout: "DFSNAP1\n", u32 venues, then per venue u32 name length, name, u32 bids,
/// u32 asks, and (price, size) f64 pairs, all little-endian.
fn full_snapshot() -> Vec<(String, BookSnapshot)> {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/books/perelman_snap_2026_11_24.bin");
    let b = std::fs::read(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    assert_eq!(&b[..8], b"DFSNAP1\n");
    let mut at = 8;
    let u32_at = |at: &mut usize| {
        let v = u32::from_le_bytes(b[*at..*at + 4].try_into().unwrap()) as usize;
        *at += 4;
        v
    };
    let n = u32_at(&mut at);
    let mut out = Vec::new();
    for _ in 0..n {
        let len = u32_at(&mut at);
        let name = String::from_utf8(b[at..at + len].to_vec()).unwrap();
        at += len;
        let (nb, na) = (u32_at(&mut at), u32_at(&mut at));
        let side = |k: usize, at: &mut usize| -> Vec<Level> {
            (0..k)
                .map(|_| {
                    let p = f64::from_le_bytes(b[*at..*at + 8].try_into().unwrap());
                    let s = f64::from_le_bytes(b[*at + 8..*at + 16].try_into().unwrap());
                    *at += 16;
                    (p, s)
                })
                .collect()
        };
        let bids = side(nb, &mut at);
        let asks = side(na, &mut at);
        out.push((name, BookSnapshot { bids, asks, retrieved_ns: 0 }));
    }
    assert_eq!(at, b.len());
    out
}

fn fixture_snapshot() -> Vec<(String, BookSnapshot)> {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures_fair/snapshot_books.json");
    let j: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&p).expect("snapshot_books.json")).unwrap();
    let lv = |v: &serde_json::Value| -> Vec<Level> {
        v.as_array().unwrap().iter().map(|l| (l[0].as_str().unwrap().parse().unwrap(), l[1].as_str().unwrap().parse().unwrap())).collect()
    };
    j["venues"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| (v["venue"].as_str().unwrap().to_string(), BookSnapshot { bids: lv(&v["bids"]), asks: lv(&v["asks"]), retrieved_ns: 0 }))
        .collect()
}

fn same(tag: &str, got: &Result<ReplicaOutput, ReplicaError>, want: &Result<ReplicaOutput, ReplicaError>) {
    assert_eq!(got, want, "{tag}");
    if let (Ok(g), Ok(w)) = (got, want) {
        let bits = |o: &ReplicaOutput| [o.value, o.index.cap, o.index.spacing, o.index.utilized_depth, o.median_mid].map(f64::to_bits);
        assert_eq!(bits(g), bits(w), "{tag}: bits");
    }
}

#[test]
fn full_depth_snapshot_matches_brti_py() {
    // brti.py (PERELMAN 9b55eae) on these books, CPython 3.12 on macOS, 2026-11-25.
    let books = full_snapshot();
    assert_eq!(books.iter().map(|(_, b)| b.bids.len() + b.asks.len()).sum::<usize>(), 45_697);
    let out = Replica::default().calculate(&books, 0).unwrap();
    assert_eq!(out.value.to_bits(), 60258.135944245965f64.to_bits());
    assert_eq!(out.index.cap.to_bits(), 4.538816473379211f64.to_bits());
    assert_eq!(out.index.spacing.to_bits(), 0.013619679700137633f64.to_bits());
    assert_eq!(out.index.utilized_depth.to_bits(), 297.5900014480073f64.to_bits());
    assert_eq!(out.index.levels_used, 21_850);
    let mut inc = Replica::default();
    let mut last = None;
    for (name, b) in &books {
        last = Some(inc.apply(name, b, 0));
    }
    same("apply after loading all venues", &last.unwrap(), &Ok(out));
    assert_eq!(inc.fallbacks, 0, "the band decides the recorded books");
}

struct Lcg(u64);

impl Lcg {
    fn u(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }
    fn below(&mut self, n: usize) -> usize {
        ((self.u() * n as f64) as usize).min(n.saturating_sub(1))
    }
}

/// One venue update: size changes, cancels and new levels near the touch (appended, so the book
/// arrives out of order), whole-book price moves, a stale or crossed or emptied book, or a 6%
/// outlier that later returns (exercising 5.1-5.3 and the hysteresis).
fn perturb(b: &mut BookSnapshot, r: &mut Lcg, calc_ns: i64) {
    b.retrieved_ns = calc_ns;
    let k = r.u();
    let side = if r.u() < 0.5 { &mut b.bids } else { &mut b.asks };
    if k < 0.55 {
        for _ in 0..1 + r.below(12) {
            if side.is_empty() {
                break;
            }
            let i = r.below(side.len().min(60));
            if r.u() < 0.15 {
                side[i].1 = 0.0; // a cancel, dropped by cleaning
            } else {
                side[i].1 = ((side[i].1 * (0.3 + 1.4 * r.u())) * 1e8).round() / 1e8 + 1e-8;
            }
        }
    } else if k < 0.75 {
        if let Some(&(p, _)) = side.get(r.below(side.len().min(40))) {
            let np = ((p + (r.u() - 0.5) * 2.0) * 100.0).round() / 100.0;
            side.push((np, ((r.u() * 3.0).exp() - 0.9) * 0.1));
        }
    } else if k < 0.88 {
        let dp = ((r.u() - 0.5) * 40.0).round() * 0.01;
        for l in b.bids.iter_mut().chain(b.asks.iter_mut()) {
            l.0 = ((l.0 + dp) * 100.0).round() / 100.0;
        }
    } else if k < 0.92 {
        b.retrieved_ns = calc_ns - 11 * S;
    } else if k < 0.95 {
        let f = if b.bids.first().is_some_and(|l| l.0 > 63000.0) { 1.0 / 1.06 } else { 1.06 };
        for l in b.bids.iter_mut().chain(b.asks.iter_mut()) {
            l.0 = (l.0 * f * 100.0).round() / 100.0;
        }
    } else if k < 0.975 {
        if let (Some(a), Some(bb)) = (b.asks.first().copied(), b.bids.first_mut()) {
            bb.0 = a.0 + 0.01; // own book crossed: excluded as erroneous
        }
    } else if k < 0.98 {
        b.asks.clear(); // empty side
    } else if let (Some(a), Some(bb)) = (b.asks.first().copied(), b.bids.first().copied()) {
        if bb.0 >= a.0 {
            b.bids.remove(0); // repair a crossed book
        }
    }
}

/// With `DOPPEL_DUMP_REPLAY=<dir>`, every `dump_every`-th step's books, clock, prior hysteresis
/// state and Rust result go to `<dir>/<label>.json`, for checking against `brti.py` directly.
fn dump_step(
    out: &mut Vec<serde_json::Value>,
    books: &[(String, BookSnapshot)],
    calc_ns: i64,
    before: &std::collections::BTreeSet<String>,
    got: &Result<ReplicaOutput, ReplicaError>,
) {
    let lv = |v: &[Level]| v.iter().map(|l| serde_json::json!([format!("{:?}", l.0), format!("{:?}", l.1)])).collect::<Vec<_>>();
    let venues: Vec<serde_json::Value> = books
        .iter()
        .map(|(n, b)| serde_json::json!({"venue": n, "retrieved_ns": b.retrieved_ns, "bids": lv(&b.bids), "asks": lv(&b.asks)}))
        .collect();
    let result = match got {
        Ok(o) => serde_json::json!({"value": format!("{:?}", o.value), "levels_used": o.index.levels_used}),
        Err(e) => serde_json::json!({"error": e.failure.message()}),
    };
    out.push(serde_json::json!({"calc_ns": calc_ns, "excluded_before": before, "venues": venues, "rust": result}));
}

fn replay(label: &str, start: Vec<(String, BookSnapshot)>, steps: usize, seed: u64) -> u64 {
    replay_dumping(label, start, steps, seed, 0)
}

fn replay_dumping(label: &str, start: Vec<(String, BookSnapshot)>, steps: usize, seed: u64, dump_every: usize) -> u64 {
    let dump_dir = std::env::var("DOPPEL_DUMP_REPLAY").ok().filter(|_| dump_every > 0);
    let mut dumped: Vec<serde_json::Value> = Vec::new();
    let original = start.clone();
    let mut books = start;
    let mut reference = Replica::default();
    let mut inc = Replica::default();
    let mut calc_ns = 1_795_622_400 * S;
    for b in books.iter_mut() {
        b.1.retrieved_ns = calc_ns;
    }
    // Venues arrive one at a time; the reference sees the same venues in first-seen order.
    for k in 1..=books.len() {
        let got = inc.apply(&books[k - 1].0, &books[k - 1].1, calc_ns);
        same(&format!("{label} load {k}"), &got, &reference.calculate(&books[..k], calc_ns));
    }
    assert_eq!(inc.books().len(), books.len());
    let mut r = Lcg(seed);
    let (mut ok, mut err) = (0, 0);
    let mut seen = std::collections::BTreeSet::new();
    for step in 0..steps {
        calc_ns += 250_000_000;
        let v = r.below(books.len());
        let b = &books[v].1;
        let broken = b.bids.is_empty() || b.asks.is_empty() || b.bids[0].0 >= b.asks[0].0;
        if broken {
            books[v].1 = original[v].1.clone(); // the venue recovers with a fresh snapshot
        }
        perturb(&mut books[v].1, &mut r, calc_ns);
        let (name, book) = books[v].clone();
        let before = reference.excluded.clone();
        let got = inc.apply(&name, &book, calc_ns);
        let want = reference.calculate(&books, calc_ns);
        if dump_dir.is_some() && step % dump_every == 0 {
            dump_step(&mut dumped, &books, calc_ns, &before, &got);
        }
        same(&format!("{label} step {step} ({name})"), &got, &want);
        assert_eq!(inc.excluded, reference.excluded, "{label} step {step}: hysteresis state");
        let diags = match &got {
            Ok(o) => &o.exchanges,
            Err(e) => &e.exchanges,
        };
        for d in diags {
            let k = format!("{:?}", d.status);
            seen.insert(k.split([' ', '{']).next().unwrap_or("").to_string());
        }
        match &got {
            Ok(_) => ok += 1,
            Err(e) => {
                err += 1;
                if err < 4 {
                    eprintln!("{label} step {step}: {:?} {:?}", e.failure, e.exchanges.iter().map(|d| d.status).collect::<Vec<_>>());
                }
            }
        }
    }
    if let Some(dir) = dump_dir {
        let path = PathBuf::from(dir).join(format!("{}.json", label.replace(' ', "_")));
        std::fs::write(&path, serde_json::to_string(&dumped).unwrap()).unwrap();
        eprintln!("{label}: dumped {} steps to {}", dumped.len(), path.display());
    }
    assert!(ok > steps / 2, "{label}: {ok} ok / {err} failures, too few values to mean anything");
    eprintln!("{label}: {steps} steps, {ok} values, {err} failures, {} fallbacks, statuses {seen:?}", inc.fallbacks);
    for st in ["Used", "Delayed", "Crossed", "EmptySide", "Outlier"] {
        assert!(seen.contains(st), "{label}: replay never produced {st}");
    }
    inc.fallbacks
}

#[test]
fn apply_equals_calculate_on_replay_full_depth() {
    replay_dumping("full depth", full_snapshot(), 150, 1, 60);
}

#[test]
fn apply_equals_calculate_on_replay_fixture_books() {
    replay_dumping("fixture books", fixture_snapshot(), 600, 2, 15);
}

#[test]
fn apply_equals_calculate_when_the_band_cannot_decide() {
    // Sparse books: fewer than 50 levels inside the 5% band but deep levels beyond it, so the
    // band never has `min(len, 50)` levels and every calculation falls back to the full books.
    let book = |mid: f64, off: f64| {
        let near = (0..20).map(|i| (mid - off - i as f64 * 7.0, 0.5 + i as f64 * 0.01));
        let far = (0..80).map(|i| (mid * 0.9 - i as f64 * 3.0, 1.0));
        let bids: Vec<Level> = near.chain(far).collect();
        let asks: Vec<Level> = bids.iter().map(|&(p, s)| (2.0 * mid - p, s * 1.1)).collect();
        BookSnapshot { bids, asks, retrieved_ns: 0 }
    };
    let start: Vec<(String, BookSnapshot)> = (0..4).map(|v| (format!("v{v}"), book(60000.0 + v as f64 * 3.0, 1.0 + v as f64))).collect();
    let fallbacks = replay("sparse books", start, 400, 3);
    assert!(fallbacks > 0, "the fallback path must be exercised");
}
