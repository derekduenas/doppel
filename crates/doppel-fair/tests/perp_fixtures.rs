//! The perp decoders against the fixtures shared with the Python reader (`tests/fixtures_perps/`,
//! checked by `tests/test_perps.py` too): the same top of book after every record, the same prints.

use std::path::PathBuf;

use doppel_fair::perp::{CbdeTrades, KalshiPerp, PerpTrade, CBDE_BTC_CONTRACT};
use doppel_fair::venue::{Venue, VenueBook};
use serde_json::Value;

fn fixture(name: &str) -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures_perps").join(name)
}

fn records(name: &str) -> Vec<Value> {
    std::fs::read_to_string(fixture(name)).unwrap().lines().map(|l| serde_json::from_str(l).unwrap()).collect()
}

fn expected(name: &str) -> Value {
    serde_json::from_str(&std::fs::read_to_string(fixture(name)).unwrap()).unwrap()
}

fn parts(r: &Value) -> (&str, i64, Option<&Value>) {
    (r["raw"].as_str().unwrap(), r["tw"].as_i64().unwrap(), r.get("meta"))
}

fn assert_top(got: Option<[f64; 4]>, exp: &Value, i: usize) {
    match (got, exp.as_array()) {
        (None, None) => {}
        (Some(g), Some(e)) => {
            for (a, b) in g.iter().zip(e) {
                let b = b.as_f64().unwrap();
                assert!((a - b).abs() <= 1e-9 * b.abs().max(1.0), "record {i}: {g:?} vs {e:?}");
            }
        }
        _ => panic!("record {i}: {got:?} vs {exp}"),
    }
}

fn assert_trades(got: &[PerpTrade], exp: &Value) {
    let exp = exp.as_array().unwrap();
    assert_eq!(got.len(), exp.len());
    for (g, e) in got.iter().zip(exp) {
        assert_eq!(g.tw, e[0].as_i64().unwrap());
        assert_eq!(g.id, e[1].as_str().unwrap());
        assert!((g.price - e[2].as_f64().unwrap()).abs() < 1e-9 && (g.qty - e[3].as_f64().unwrap()).abs() < 1e-12, "{g:?} {e}");
        assert_eq!(g.taker_buy, e[4].as_bool());
    }
}

#[test]
fn kalshi_ws_fixture() {
    let exp = expected("kalshi_ws.expected.json");
    let mut k = KalshiPerp::new();
    let recs = records("kalshi_ws.jsonl");
    assert_eq!(recs.len(), exp["tops"].as_array().unwrap().len());
    let mut gaps = 0;
    for (i, r) in recs.iter().enumerate() {
        let (raw, tw, meta) = parts(r);
        gaps += k.apply_record(raw, tw, meta).is_err() as u64;
        assert_top(k.top().map(|t| [t.bid, t.bid_qty, t.ask, t.ask_qty]), &exp["tops"][i], i);
    }
    assert_eq!(gaps, exp["seq_gaps"].as_u64().unwrap());
    assert_eq!(k.trade_gaps, exp["trade_gaps"].as_u64().unwrap());
    assert_trades(&k.trades, &exp["trades"]);
    let info = &exp["info"][0];
    assert_eq!(k.info.len(), 1);
    assert_eq!(k.info[0].tw, info[0].as_i64().unwrap());
    assert!((k.info[0].index.unwrap() - info[1].as_f64().unwrap()).abs() < 1e-9);
    assert!((k.info[0].mark.unwrap() - info[2].as_f64().unwrap()).abs() < 1e-9);
    assert_eq!(k.info[0].funding_rate, info[3].as_f64());
}

#[test]
fn cbde_ws_fixture() {
    let exp = expected("cbde_ws.expected.json");
    let mut b = VenueBook::new(Venue::Coinbase);
    let mut t = CbdeTrades::new();
    let recs = records("cbde_ws.jsonl");
    assert_eq!(recs.len(), exp["tops"].as_array().unwrap().len());
    for (i, r) in recs.iter().enumerate() {
        let (raw, tw, meta) = parts(r);
        b.apply_record(raw, tw, meta).unwrap();
        t.apply_record(raw, tw, meta);
        let top = b.snapshot().map(|s| {
            let c = CBDE_BTC_CONTRACT;
            [s.bids[0].0, s.bids[0].1 * c, s.asks[0].0, s.asks[0].1 * c]
        });
        assert_top(top, &exp["tops"][i], i);
    }
    assert_trades(&t.trades, &exp["trades"]);
}
