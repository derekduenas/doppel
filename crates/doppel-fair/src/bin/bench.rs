//! `doppel-fair-bench [--fair-iters N] [--replica-iters N] [--updates N]`
//!
//! Prints p50/p99 in ns, release build only. It times:
//! - one fair-v1 evaluation;
//! - the exact replica (`Replica::calculate` on a full snapshot), with a per-stage profile;
//! - the incremental replica (`Replica::apply`, one venue's new book per call), on the recorded
//!   books: the 300-level fixture (`tests/fixtures_fair/snapshot_books.json`) and full depth
//!   (`crates/doppel-fair/tests/books/perelman_snap_2026_11_24.bin`, 45,697 levels).

use std::hint::black_box;
use std::path::PathBuf;
use std::time::Instant;

use doppel_fair::brti::{brti_from_books_profiled, clean, BookSnapshot, Level, Replica};
use doppel_fair::fair::fair_up_raw;

struct Lcg(u64);

impl Lcg {
    fn u(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }
}

/// (min, p50, p99, max).
fn pct(mut ns: Vec<u64>) -> (u64, u64, u64, u64) {
    ns.sort_unstable();
    let at = |q: f64| ns[((ns.len() as f64 - 1.0) * q).round() as usize];
    (ns[0], at(0.5), at(0.99), *ns.last().unwrap())
}

fn report(label: &str, ns: Vec<u64>) {
    let n = ns.len();
    let (min, p50, p99, max) = pct(ns);
    println!("{label:<48} n={n:<6} min={min:>9} p50={p50:>9} p99={p99:>9} max={max:>10} ns");
}

fn time<F: FnMut()>(label: &str, iters: usize, mut f: F) {
    for _ in 0..iters.min(50) {
        f();
    }
    let ns = (0..iters)
        .map(|_| {
            let t = Instant::now();
            f();
            t.elapsed().as_nanos() as u64
        })
        .collect();
    report(label, ns);
}

fn manifest(rel: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(rel)
}

fn load_json(p: &PathBuf) -> Vec<(String, BookSnapshot)> {
    let text = std::fs::read_to_string(p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    let j: serde_json::Value = serde_json::from_str(&text).expect("snapshot json");
    let num = |v: &serde_json::Value| v.as_str().and_then(|s| s.parse().ok()).unwrap_or(f64::NAN);
    let lv = |v: &serde_json::Value| -> Vec<Level> { v.as_array().unwrap().iter().map(|l| (num(&l[0]), num(&l[1]))).collect() };
    j["venues"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| (v["venue"].as_str().unwrap().to_string(), BookSnapshot { bids: lv(&v["bids"]), asks: lv(&v["asks"]), retrieved_ns: 0 }))
        .collect()
}

/// The binary full-depth fixture (layout documented in tests/incremental.rs).
fn load_bin(p: &PathBuf) -> Vec<(String, BookSnapshot)> {
    let b = std::fs::read(p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    assert_eq!(&b[..8], b"DFSNAP1\n");
    let mut at = 8;
    let u32_at = |at: &mut usize| {
        let v = u32::from_le_bytes(b[*at..*at + 4].try_into().unwrap()) as usize;
        *at += 4;
        v
    };
    let f64_at = |at: &mut usize| {
        let v = f64::from_le_bytes(b[*at..*at + 8].try_into().unwrap());
        *at += 8;
        v
    };
    let n = u32_at(&mut at);
    (0..n)
        .map(|_| {
            let len = u32_at(&mut at);
            let name = String::from_utf8(b[at..at + len].to_vec()).unwrap();
            at += len;
            let (nb, na) = (u32_at(&mut at), u32_at(&mut at));
            let mut side = |k: usize| -> Vec<Level> { (0..k).map(|_| (f64_at(&mut at), f64_at(&mut at))).collect() };
            let bids = side(nb);
            let asks = side(na);
            (name, BookSnapshot { bids, asks, retrieved_ns: 0 })
        })
        .collect()
}

/// A replay of single-venue updates: each step changes a few sizes near one venue's touch and
/// sometimes moves its whole book a few ticks. Returns (venue index, new book, calc_ns) per step.
fn updates(start: &[(String, BookSnapshot)], n: usize, r: &mut Lcg) -> Vec<(usize, BookSnapshot, i64)> {
    let mut books: Vec<BookSnapshot> = start.iter().map(|(_, b)| b.clone()).collect();
    let mut t = 1_795_622_400_000_000_000i64;
    (0..n)
        .map(|_| {
            t += 100_000_000;
            let v = ((r.u() * books.len() as f64) as usize).min(books.len() - 1);
            let b = &mut books[v];
            for _ in 0..1 + (r.u() * 8.0) as usize {
                let side = if r.u() < 0.5 { &mut b.bids } else { &mut b.asks };
                let i = ((r.u() * 30.0) as usize).min(side.len() - 1);
                side[i].1 = ((side[i].1 * (0.5 + r.u())) * 1e8).round() / 1e8 + 1e-8;
            }
            if r.u() < 0.1 {
                let dp = ((r.u() - 0.5) * 10.0).round() * 0.01;
                for l in b.bids.iter_mut().chain(b.asks.iter_mut()) {
                    l.0 = ((l.0 + dp) * 100.0).round() / 100.0;
                }
            }
            b.retrieved_ns = t;
            (v, b.clone(), t)
        })
        .collect()
}

/// Median and p99 ns per stage of the exact pipeline (cleaning + `brti_from_books` stages).
fn profile(label: &str, books: &[(String, BookSnapshot)], iters: usize) {
    let mut per: Vec<(&'static str, Vec<u64>)> = Vec::new();
    for _ in 0..iters {
        let t = Instant::now();
        let cleaned: Vec<(Vec<Level>, Vec<Level>)> = books.iter().map(|(_, b)| (clean(&b.bids), clean(&b.asks))).collect();
        let mut laps = vec![("clean", t.elapsed().as_nanos() as u64)];
        let refs: Vec<(&[Level], &[Level])> = cleaned.iter().map(|(b, a)| (b.as_slice(), a.as_slice())).collect();
        let (res, l) = brti_from_books_profiled(&refs);
        black_box(res.ok());
        laps.extend(l);
        for (i, (name, ns)) in laps.into_iter().enumerate() {
            if per.len() <= i {
                per.push((name, Vec::new()));
            }
            per[i].1.push(ns);
        }
    }
    println!("{label}:");
    for (name, ns) in per {
        let (min, p50, p99, _) = pct(ns);
        println!("  {name:<22} min={min:>9} p50={p50:>9} p99={p99:>9} ns");
    }
}

fn replica(label: &str, books: &[(String, BookSnapshot)], rep_iters: usize, n_updates: usize) {
    let lv: usize = books.iter().map(|(_, b)| b.bids.len() + b.asks.len()).sum();
    let label = format!("{label}, {} venues, {lv} levels", books.len());
    time(&format!("calculate  {label}"), rep_iters, || {
        let mut rep = Replica::default();
        black_box(rep.calculate(black_box(books), 0).map(|o| o.value).ok());
    });
    let steps = updates(books, n_updates, &mut Lcg(7));
    let mut inc = Replica::default();
    for (name, b) in books {
        let _ = inc.apply(name, b, steps[0].2);
    }
    let mut all = books.to_vec();
    let mut ns = Vec::with_capacity(steps.len());
    let mut mismatches = 0;
    for (k, (v, book, t)) in steps.iter().enumerate() {
        let name = all[*v].0.clone();
        let before = inc.excluded.clone();
        let t0 = Instant::now();
        let got = inc.apply(&name, black_box(book), *t);
        ns.push(t0.elapsed().as_nanos() as u64);
        all[*v].1 = book.clone();
        if k % 10 == 0 {
            // spot-check the identity outside the timed region (the tests check every step)
            let mut reference = Replica::default();
            reference.excluded = before;
            let want = reference.calculate(&all, *t);
            if got.as_ref().ok().map(|o| o.value.to_bits()) != want.as_ref().ok().map(|o| o.value.to_bits()) {
                mismatches += 1;
            }
        }
        black_box(got.ok());
    }
    report(&format!("apply      {label}"), ns);
    // per-stage profile of apply over the same replay
    let mut prof = Replica::default();
    for (name, b) in books {
        let _ = prof.apply(name, b, steps[0].2);
    }
    let mut per: Vec<(&'static str, Vec<u64>)> = Vec::new();
    for (v, book, t) in &steps {
        let (res, laps) = prof.apply_profiled(&books[*v].0, book, *t);
        black_box(res.ok());
        for (i, (name, ns)) in laps.into_iter().enumerate() {
            if per.len() <= i {
                per.push((name, Vec::new()));
            }
            per[i].1.push(ns);
        }
    }
    println!("apply stages, {label}:");
    for (name, ns) in per {
        let (min, p50, p99, _) = pct(ns);
        println!("  {name:<22} min={min:>9} p50={p50:>9} p99={p99:>9} ns");
    }
    println!("  apply fallbacks to full books: {} of {}; spot-check mismatches: {mismatches}", inc.fallbacks, steps.len());
    profile(&format!("calculate stages, {label}"), books, rep_iters);
    if let Ok(o) = Replica::default().calculate(books, 0) {
        println!("  value {:?} levels_used {} spacing {:?} cap {:?}", o.value, o.index.levels_used, o.index.spacing, o.index.cap);
    }
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let get = |k: &str| args.iter().position(|a| a == k).and_then(|i| args.get(i + 1)).cloned();
    let fair_iters: usize = get("--fair-iters").and_then(|s| s.parse().ok()).unwrap_or(200_000);
    let rep_iters: usize = get("--replica-iters").and_then(|s| s.parse().ok()).unwrap_or(300);
    let n_updates: usize = get("--updates").and_then(|s| s.parse().ok()).unwrap_or(2000);

    let mut r = Lcg(20261125);
    let mut x = 60000.0;
    let raw: Vec<f64> = (0..1800)
        .map(|_| {
            x += (r.u() - 0.5) * 6.0;
            x
        })
        .collect();
    let t0 = 1_795_622_400i64;
    let end = t0 + 1500;
    let ptb = raw[1139];
    for (label, d) in [("fair-v1 d=E-30 (inside the 60 s average)", end - 30), ("fair-v1 d=E-240 (before the average)", end - 240)] {
        time(label, fair_iters, || {
            black_box(fair_up_raw(black_box(&raw), t0, black_box(ptb), d, end, 0, 300));
        });
    }
    replica("fixture", &load_json(&manifest("../../tests/fixtures_fair/snapshot_books.json")), rep_iters, n_updates);
    replica("full depth", &load_bin(&manifest("tests/books/perelman_snap_2026_11_24.bin")), rep_iters, n_updates);
}
