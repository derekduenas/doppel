//! Parity with the Python references on the shared fixtures in `tests/fixtures_fair/`
//! (written by `tests/fixtures_fair/gen_fixtures.py`). Missing fixtures fail, never skip.

use std::collections::BTreeSet;
use std::path::PathBuf;

use doppel_fair::brti::{brti_from_books, BookSnapshot, IndexDiag, Level, Replica};
use doppel_fair::fair::fair_up_raw;
use serde_json::Value;

fn fixture(name: &str) -> Value {
    let p = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures_fair").join(name);
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("fixture {} missing ({e}): run gen_fixtures.py", p.display()));
    serde_json::from_str(&text).expect("fixture json")
}

/// Floats are Python repr strings (exact); null is NaN / None.
fn num(v: &Value) -> f64 {
    match v {
        Value::Null => f64::NAN,
        Value::String(s) => s.parse().unwrap_or_else(|_| panic!("bad float {s}")),
        other => panic!("expected float string, got {other}"),
    }
}

fn opt(v: &Value) -> Option<f64> {
    if v.is_null() {
        None
    } else {
        Some(num(v))
    }
}

fn levels(v: &Value) -> Vec<Level> {
    v.as_array().unwrap().iter().map(|l| (num(&l[0]), num(&l[1]))).collect()
}

fn int(v: &Value) -> i64 {
    v.as_i64().expect("int")
}

#[test]
fn fair_v1_matches_python() {
    let fx = fixture("fair_v1.json");
    let paths: Vec<(i64, Vec<f64>)> =
        fx["paths"].as_array().unwrap().iter().map(|p| (int(&p["t0"]), p["raw"].as_array().unwrap().iter().map(num).collect())).collect();
    let cases = fx["cases"].as_array().unwrap();
    assert!(cases.len() > 1000, "fixture too small");
    let (mut max_diff, mut exact, mut some) = (0.0f64, 0usize, 0usize);
    for (i, c) in cases.iter().enumerate() {
        let (t0, raw) = &paths[c["path"].as_u64().unwrap() as usize];
        let got =
            fair_up_raw(raw, *t0, num(&c["ptb"]), int(&c["d"]), int(&c["end"]), int(&c["lag"]), c["vol_window"].as_u64().unwrap() as usize);
        let want = opt(&c["p"]);
        match (got, want) {
            (None, None) => {}
            (Some(g), Some(w)) => {
                let d = (g - w).abs();
                assert!(d <= 1e-9, "case {i}: rust {g} python {w} ({c})");
                max_diff = max_diff.max(d);
                exact += usize::from(g.to_bits() == w.to_bits());
                some += 1;
            }
            _ => panic!("case {i}: rust {got:?} python {want:?} ({c})"),
        }
    }
    eprintln!("fair-v1 parity: {} cases, {some} valued, {exact} bit-exact, max |diff| {max_diff:e}", cases.len());
}

#[test]
fn fair_v1_never_reads_after_d() {
    let fx = fixture("fair_v1.json");
    let p = &fx["paths"][0];
    let (t0, raw): (i64, Vec<f64>) = (int(&p["t0"]), p["raw"].as_array().unwrap().iter().map(num).collect());
    for c in fx["cases"].as_array().unwrap().iter().filter(|c| c["path"] == 0) {
        let d = int(&c["d"]);
        let args = (num(&c["ptb"]), d, int(&c["end"]), int(&c["lag"]), c["vol_window"].as_u64().unwrap() as usize);
        let base = fair_up_raw(&raw, t0, args.0, args.1, args.2, args.3, args.4);
        let mut g = raw.clone();
        let from = (d - t0 + 1).max(0) as usize;
        for (k, x) in g.iter_mut().enumerate().skip(from) {
            *x = if k % 2 == 0 { f64::NAN } else { 1e12 };
        }
        assert_eq!(fair_up_raw(&g, t0, args.0, args.1, args.2, args.3, args.4).map(f64::to_bits), base.map(f64::to_bits), "{c}");
    }
}

fn check_index(tag: &str, value: f64, i: &IndexDiag, want: &Value, stats: &mut (f64, usize)) {
    let wv = num(&want["value"]);
    let d = (value - wv).abs();
    assert!(d <= 1e-9, "{tag}: value rust {value} python {wv}");
    stats.0 = stats.0.max(d);
    stats.1 += usize::from(value.to_bits() == wv.to_bits());
    for (name, got) in [("cap", i.cap), ("spacing", i.spacing), ("utilized_depth", i.utilized_depth)] {
        let w = num(&want[name]);
        assert!((got - w).abs() <= 1e-12 * w.abs().max(1.0), "{tag}: {name} rust {got} python {w}");
    }
    assert_eq!(i.levels_used as u64, want["levels_used"].as_u64().unwrap(), "{tag}: levels_used");
}

#[test]
fn brti_replica_matches_perelman() {
    let fx = fixture("brti_replica.json");
    let mut stats = (0.0f64, 0usize);
    let mut values = 0usize;
    for scn in fx["scenarios"].as_array().unwrap() {
        let name = scn["name"].as_str().unwrap();
        let mut rep = Replica::default();
        for (k, st) in scn["steps"].as_array().unwrap().iter().enumerate() {
            let tag = format!("{name}[{k}]");
            let books: Vec<(String, BookSnapshot)> = st["venues"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| {
                    let b = BookSnapshot { bids: levels(&v["bids"]), asks: levels(&v["asks"]), retrieved_ns: int(&v["retrieved_ns"]) };
                    (v["venue"].as_str().unwrap().to_string(), b)
                })
                .collect();
            let want = &st["expect"];
            // The incremental path on the same step: load the venues, then restore the hysteresis
            // state from before this step for the final apply. It must equal the exact path.
            let before = rep.excluded.clone();
            let mut inc = Replica::default();
            let mut banded = None;
            for (i, (venue, b)) in books.iter().enumerate() {
                if i + 1 == books.len() {
                    inc.excluded = before.clone();
                }
                banded = Some(inc.apply(venue, b, int(&st["calc_ns"])));
            }
            let exact = rep.calculate(&books, int(&st["calc_ns"]));
            assert_eq!(banded.as_ref(), Some(&exact), "{tag}: apply vs calculate");
            assert_eq!(
                banded.and_then(|r| r.ok()).map(|o| o.value.to_bits()),
                exact.as_ref().ok().map(|o| o.value.to_bits()),
                "{tag}: bits"
            );
            match exact {
                Ok(out) => {
                    assert!(want.get("error").is_none(), "{tag}: rust ok, python {want}");
                    check_index(&tag, out.value, &out.index, want, &mut stats);
                    let venues: Vec<String> = want["venues"].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
                    assert_eq!(out.venues, venues, "{tag}: venues");
                    values += 1;
                }
                Err(e) => assert_eq!(Some(e.failure.message()), want["error"].as_str(), "{tag}: rust {e:?}"),
            }
            let excl: BTreeSet<String> = st["excluded_after"].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
            assert_eq!(rep.excluded, excl, "{tag}: hysteresis state");
        }
    }
    for c in fx["index_cases"].as_array().unwrap() {
        let tag = c["name"].as_str().unwrap();
        let owned: Vec<(Vec<Level>, Vec<Level>)> =
            c["books"].as_array().unwrap().iter().map(|b| (levels(&b["bids"]), levels(&b["asks"]))).collect();
        let books: Vec<(&[Level], &[Level])> = owned.iter().map(|(b, a)| (b.as_slice(), a.as_slice())).collect();
        let want = &c["expect"];
        match brti_from_books(&books) {
            Ok((v, i)) => {
                check_index(tag, v, &i, want, &mut stats);
                values += 1;
            }
            Err(f) => assert_eq!(Some(f.message()), want["error"].as_str(), "{tag}"),
        }
    }
    assert!(values >= 20, "too few valued replica cases: {values}");
    eprintln!("replica parity: {values} values, {} bit-exact, max |diff| ${:e}", stats.1, stats.0);
}
