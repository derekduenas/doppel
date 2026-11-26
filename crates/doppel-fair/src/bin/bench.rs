//! `doppel-fair-bench [--fair-iters N] [--replica-iters N] [--snapshot PATH]`
//!
//! Times one fair-v1 evaluation and one replica evaluation, prints p50/p99 in ns. Build with
//! `--release`. The recorded books come from `tests/fixtures_fair/snapshot_books.json`.

use std::hint::black_box;
use std::path::PathBuf;
use std::time::Instant;

use doppel_fair::brti::{BookSnapshot, Level, Replica};
use doppel_fair::fair::fair_up_raw;

struct Lcg(u64);

impl Lcg {
    fn u(&mut self) -> f64 {
        self.0 = self.0.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        (self.0 >> 11) as f64 / (1u64 << 53) as f64
    }
}

fn pct(mut ns: Vec<u64>) -> (u64, u64, u64) {
    ns.sort_unstable();
    let at = |q: f64| ns[((ns.len() as f64 - 1.0) * q).round() as usize];
    (at(0.5), at(0.99), *ns.last().unwrap())
}

fn time<F: FnMut()>(label: &str, iters: usize, mut f: F) {
    for _ in 0..iters.min(50) {
        f();
    }
    let mut ns = Vec::with_capacity(iters);
    for _ in 0..iters {
        let t = Instant::now();
        f();
        ns.push(t.elapsed().as_nanos() as u64);
    }
    let (p50, p99, max) = pct(ns);
    println!("{label:<44} n={iters:<7} p50={p50:>10} ns  p99={p99:>10} ns  max={max:>10} ns");
}

fn parse_num(v: &serde_json::Value) -> f64 {
    v.as_str().and_then(|s| s.parse().ok()).unwrap_or(f64::NAN)
}

fn load_snapshot(p: &PathBuf) -> Vec<(String, BookSnapshot)> {
    let text = std::fs::read_to_string(p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    let j: serde_json::Value = serde_json::from_str(&text).expect("snapshot json");
    let lv = |v: &serde_json::Value| -> Vec<Level> { v.as_array().unwrap().iter().map(|l| (parse_num(&l[0]), parse_num(&l[1]))).collect() };
    j["venues"]
        .as_array()
        .unwrap()
        .iter()
        .map(|v| (v["venue"].as_str().unwrap().to_string(), BookSnapshot { bids: lv(&v["bids"]), asks: lv(&v["asks"]), retrieved_ns: 0 }))
        .collect()
}

fn synthetic_books(r: &mut Lcg, venues: usize, levels: usize) -> Vec<(String, BookSnapshot)> {
    (0..venues)
        .map(|v| {
            let mid = 60000.0 + (r.u() - 0.5) * 10.0;
            let (mut pb, mut pa) = (mid - 0.01 * (1.0 + (r.u() * 30.0).floor()), mid + 0.01 * (1.0 + (r.u() * 30.0).floor()));
            let (mut bids, mut asks) = (Vec::with_capacity(levels), Vec::with_capacity(levels));
            for _ in 0..levels {
                bids.push(((pb * 100.0).round() / 100.0, (r.u() * 7.0 - 5.0).exp()));
                asks.push(((pa * 100.0).round() / 100.0, (r.u() * 7.0 - 5.0).exp()));
                pb -= 0.01 * (1.0 + (r.u() * 20.0).floor());
                pa += 0.01 * (1.0 + (r.u() * 20.0).floor());
            }
            (format!("v{v}"), BookSnapshot { bids, asks, retrieved_ns: 0 })
        })
        .collect()
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let get = |k: &str| args.iter().position(|a| a == k).and_then(|i| args.get(i + 1)).cloned();
    let fair_iters: usize = get("--fair-iters").and_then(|s| s.parse().ok()).unwrap_or(200_000);
    let rep_iters: usize = get("--replica-iters").and_then(|s| s.parse().ok()).unwrap_or(300);
    let snap = get("--snapshot")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures_fair/snapshot_books.json"));

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

    let recorded = load_snapshot(&snap);
    let lv: usize = recorded.iter().map(|(_, b)| b.bids.len() + b.asks.len()).sum();
    time(&format!("replica recorded 6 venues, {lv} levels"), rep_iters, || {
        let mut rep = Replica::default();
        black_box(rep.calculate(black_box(&recorded), 0).map(|o| o.value).ok());
    });
    let deep = synthetic_books(&mut r, 5, 2000);
    time("replica synthetic 5 venues x 2x2000 levels", rep_iters.div_ceil(3), || {
        let mut rep = Replica::default();
        black_box(rep.calculate(black_box(&deep), 0).map(|o| o.value).ok());
    });
    if let Ok(o) = Replica::default().calculate(&recorded, 0) {
        println!("recorded value {:?} levels_used {} spacing {:?} cap {:?}", o.value, o.index.levels_used, o.index.spacing, o.index.cap);
    }
}
