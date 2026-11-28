//! Offline decoders for the BTC perpetual-futures streams recorded by `doppel-taped`
//! (`perp_kalshi_btc`, `perp_kalshi_btc_rest`, `perp_cbde_btc`, `perp_cbde_btc_rest`).
//!
//! Every price is normalised to USD per BTC and every size to BTC, so the two venues compare
//! directly: Kalshi KXBTCPERP quotes dollars per contract of 0.0001 BTC (`contract_size` on
//! `GET /margin/markets/KXBTCPERP`); Coinbase BIP-20DEC30-CDE quotes USD per BTC for a contract of
//! 0.01 BTC (`future_product_details.contract_size`).
//!
//! Kalshi margin WebSocket (docs.kalshi.com/perps_asyncapi.yaml):
//! - `orderbook_snapshot {sid, seq, msg: {market_ticker, bid: [[px, count]], ask: [...]}}` replaces
//!   the book; `orderbook_delta {sid, seq, msg: {price, delta, side: bid|ask}}` is a signed change
//!   at one level. `seq` is per subscription (sid): a gap on the book's sid resets the book until
//!   the next snapshot (`Integrity::SeqGap`); a gap on the trade sid is counted (`trade_gaps`).
//!   A level driven below zero resets the book.
//! - `trade {sid, seq, msg: {trade_id, price, count, taker_side: bid|ask}}`: `taker_side` is the
//!   taker's book side ("Side of the taker in this trade", REST get-trades), so `bid` = taker
//!   bought. Read from the docs only: a 1 Hz REST book could not confirm it (2026-11-27), so the
//!   sign of Kalshi flow is provisional until checked against the WS book.
//! - `ticker {msg: {reference_price, settlement_mark_price, funding_rate}}` (no `seq`, at most one
//!   per second): the CF Benchmarks reference index, the mark, and the funding estimate.
//!
//! Kalshi public REST (meta `key`): `orderbook` `{orderbook: {bids, asks}}` replaces the book (a
//! failed poll resets it); `trades` `{trades: [...]}` newest first, deduplicated by `trade_id`;
//! the first trades poll after a reset only marks what it saw (those prints happened before we
//! were watching); `market` and `funding` give mark, reference and funding.
//!
//! Coinbase: the book is [`crate::venue::VenueBook`] with `Venue::Coinbase` (same `level2`
//! format, `sequence_num` checked across every frame of the connection). [`CbdeTrades`] reads
//! `market_trades`: the first event of a subscription is a `snapshot` of past trades (skipped, ids
//! remembered); `side` is the MAKER's side (Advanced Trade docs), so `SELL` = taker bought.
//! [`CbdeTrades`] also reads the public product REST body (`perp_cbde_btc_rest`) for
//! `index_price` and `funding_rate`.
//!
//! As everywhere in DOPPEL, an output's time is the record's receive time `tw`, never a venue
//! timestamp, and any control record other than `subscribe` ends continuity.

use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};

use serde_json::Value;

use crate::venue::{arr, num, Integrity};

/// KXBTCPERP `contract_size` (BTC per contract), verified on the live market 2026-11-27.
pub const KALSHI_BTC_CONTRACT: f64 = 0.0001;
/// BIP-20DEC30-CDE `contract_size` (BTC per contract), verified on the live product 2026-11-27.
pub const CBDE_BTC_CONTRACT: f64 = 0.01;
const PX: f64 = 10_000.0; // Kalshi prices are 4-decimal dollars: keys in units of $0.0001
const EPS: f64 = 1e-9;
const SEEN_CAP: usize = 50_000;

/// Top of book in USD per BTC and BTC.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PerpTop {
    pub bid: f64,
    pub bid_qty: f64,
    pub ask: f64,
    pub ask_qty: f64,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PerpTrade {
    pub tw: i64,
    pub id: String,
    /// USD per BTC
    pub price: f64,
    /// BTC
    pub qty: f64,
    /// Some(true) = taker bought, Some(false) = taker sold, None = unknown side
    pub taker_buy: Option<bool>,
}

/// Reference/index, mark and funding as published (prices per BTC).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct PerpInfo {
    pub tw: i64,
    pub index: Option<f64>,
    pub mark: Option<f64>,
    pub funding_rate: Option<f64>,
}

/// Bounded first-seen set of trade ids.
#[derive(Clone, Debug, Default)]
struct Seen {
    set: HashSet<String>,
    order: VecDeque<String>,
}

impl Seen {
    fn insert(&mut self, id: &str) -> bool {
        if !self.set.insert(id.to_string()) {
            return false;
        }
        self.order.push_back(id.to_string());
        while self.order.len() > SEEN_CAP {
            if let Some(o) = self.order.pop_front() {
                self.set.remove(&o);
            }
        }
        true
    }
}

fn ctl(meta: Option<&Value>) -> Option<&str> {
    meta.and_then(|m| m.get("ctl")).and_then(Value::as_str)
}

/// Kalshi KXBTCPERP book, prints and ticker from `perp_kalshi_btc` (WS) or `perp_kalshi_btc_rest`.
#[derive(Clone, Debug)]
pub struct KalshiPerp {
    pub contract_size: f64,
    bids: BTreeMap<i64, f64>,
    asks: BTreeMap<i64, f64>,
    pub valid: bool,
    book_sid: Option<u64>,
    seq: HashMap<u64, u64>,
    seen: Seen,
    /// the REST trades poll has a baseline since the last reset
    rest_primed: bool,
    pub integrity_failures: u64,
    pub trade_gaps: u64,
    /// REST trades polls none of whose trades had been seen before (more than one page per poll)
    pub trade_overflows: u64,
    pub trades: Vec<PerpTrade>,
    pub info: Vec<PerpInfo>,
}

impl Default for KalshiPerp {
    fn default() -> Self {
        Self::new()
    }
}

impl KalshiPerp {
    pub fn new() -> Self {
        Self {
            contract_size: KALSHI_BTC_CONTRACT,
            bids: BTreeMap::new(),
            asks: BTreeMap::new(),
            valid: false,
            book_sid: None,
            seq: HashMap::new(),
            seen: Seen::default(),
            rest_primed: false,
            integrity_failures: 0,
            trade_gaps: 0,
            trade_overflows: 0,
            trades: Vec::new(),
            info: Vec::new(),
        }
    }

    fn reset(&mut self) {
        self.bids.clear();
        self.asks.clear();
        self.valid = false;
        self.book_sid = None;
        self.seq.clear();
        self.rest_primed = false;
    }

    /// Top of book per BTC, when a snapshot has been applied and both sides are non-empty.
    pub fn top(&self) -> Option<PerpTop> {
        if !self.valid {
            return None;
        }
        let (bp, bq) = self.bids.iter().next_back()?;
        let (ap, aq) = self.asks.iter().next()?;
        let k = 1.0 / (PX * self.contract_size);
        Some(PerpTop { bid: *bp as f64 * k, bid_qty: bq * self.contract_size, ask: *ap as f64 * k, ask_qty: aq * self.contract_size })
    }

    fn per_btc(&self, px_dollars: f64) -> f64 {
        px_dollars / self.contract_size
    }

    fn levels(v: Option<&Value>) -> Option<BTreeMap<i64, f64>> {
        let mut out = BTreeMap::new();
        for l in arr(v) {
            let a = l.as_array()?;
            let (p, q) = (num(a.first())?, num(a.get(1))?);
            if q > EPS {
                out.insert((p * PX).round() as i64, q);
            }
        }
        Some(out)
    }

    /// Apply one tape record. Errors are integrity failures; the book is already reset.
    pub fn apply_record(&mut self, raw: &str, tw: i64, meta: Option<&Value>) -> Result<(), Integrity> {
        if ctl(meta).is_some_and(|c| c != "subscribe") {
            self.reset();
            return Ok(());
        }
        let Ok(m) = serde_json::from_str::<Value>(raw) else {
            if meta.and_then(|m| m.get("key")).and_then(Value::as_str) == Some("orderbook") {
                self.reset();
            }
            return Ok(());
        };
        if let Some(key) = meta.and_then(|m| m.get("key")).and_then(Value::as_str) {
            let ok = meta.and_then(|m| m.get("status")).and_then(Value::as_u64) == Some(200);
            self.rest(key, ok, &m, tw);
            return Ok(());
        }
        let typ = m.get("type").and_then(Value::as_str).unwrap_or("");
        let msg = m.get("msg");
        let sid = m.get("sid").and_then(Value::as_u64);
        if let (Some(sid), Some(s)) = (sid, m.get("seq").and_then(Value::as_u64)) {
            if typ != "orderbook_snapshot" {
                if let Some(&prev) = self.seq.get(&sid) {
                    if s != prev + 1 {
                        if typ == "orderbook_delta" {
                            self.integrity_failures += 1;
                            self.reset();
                            return Err(Integrity::SeqGap { expected: prev + 1, got: s });
                        }
                        self.trade_gaps += 1;
                    }
                }
            }
            self.seq.insert(sid, s);
        }
        match typ {
            "orderbook_snapshot" => {
                let (Some(b), Some(a)) = (Self::levels(msg.and_then(|x| x.get("bid"))), Self::levels(msg.and_then(|x| x.get("ask"))))
                else {
                    self.reset();
                    return Ok(());
                };
                self.bids = b;
                self.asks = a;
                self.valid = true;
                self.book_sid = sid;
            }
            "orderbook_delta" => {
                if !self.valid || sid != self.book_sid {
                    return Ok(());
                }
                let (Some(p), Some(d)) = (num(msg.and_then(|x| x.get("price"))), num(msg.and_then(|x| x.get("delta")))) else {
                    self.reset();
                    return Ok(());
                };
                let side = match msg.and_then(|x| x.get("side")).and_then(Value::as_str) {
                    Some("bid") => &mut self.bids,
                    Some("ask") => &mut self.asks,
                    _ => {
                        self.reset();
                        return Ok(());
                    }
                };
                let k = (p * PX).round() as i64;
                let q = side.get(&k).copied().unwrap_or(0.0) + d;
                if q < -EPS {
                    self.integrity_failures += 1;
                    self.reset();
                    return Ok(());
                }
                if q <= EPS {
                    side.remove(&k);
                } else {
                    side.insert(k, q);
                }
            }
            "trade" => {
                if let Some(t) = msg.and_then(|x| self.trade(x, tw)) {
                    self.trades.push(t);
                }
            }
            "ticker" => {
                if let Some(x) = msg {
                    self.ticker(x, tw);
                }
            }
            _ => {}
        }
        Ok(())
    }

    fn trade(&mut self, t: &Value, tw: i64) -> Option<PerpTrade> {
        let id = t.get("trade_id").and_then(Value::as_str)?.to_string();
        if !self.seen.insert(&id) {
            return None;
        }
        let (p, q) = (num(t.get("price"))?, num(t.get("count"))?);
        let taker_buy = match t.get("taker_side").and_then(Value::as_str) {
            Some("bid") => Some(true),
            Some("ask") => Some(false),
            _ => None,
        };
        Some(PerpTrade { tw, id, price: self.per_btc(p), qty: q * self.contract_size, taker_buy })
    }

    fn ticker(&mut self, x: &Value, tw: i64) {
        let px = |k: &str| num(x.get(k).and_then(|v| v.get("price"))).map(|p| p / self.contract_size);
        self.info.push(PerpInfo {
            tw,
            index: px("reference_price"),
            mark: px("settlement_mark_price"),
            funding_rate: x.get("funding_rate").and_then(|f| num(f.get("rate"))),
        });
    }

    fn rest(&mut self, key: &str, ok: bool, m: &Value, tw: i64) {
        match key {
            "orderbook" => {
                let ob = m.get("orderbook");
                match (ok, Self::levels(ob.and_then(|o| o.get("bids"))), Self::levels(ob.and_then(|o| o.get("asks")))) {
                    (true, Some(b), Some(a)) if ob.is_some() => {
                        self.bids = b;
                        self.asks = a;
                        self.valid = true;
                    }
                    _ => {
                        self.bids.clear();
                        self.asks.clear();
                        self.valid = false;
                    }
                }
            }
            "trades" => {
                if !ok {
                    return;
                }
                let list = arr(m.get("trades"));
                let primed = self.rest_primed;
                self.rest_primed = true;
                let mut fresh = Vec::new();
                let mut any_seen = false;
                for t in list.iter().rev() {
                    // REST field names: `created_time`, `taker_side`, `count`, `price`, `trade_id`
                    match self.trade(t, tw) {
                        Some(x) => fresh.push(x),
                        None => any_seen = true,
                    }
                }
                if primed {
                    if !any_seen && !list.is_empty() {
                        self.trade_overflows += 1;
                    }
                    self.trades.extend(fresh);
                }
            }
            "market" if ok => {
                let Some(x) = m.get("market") else { return };
                if let Some(cs) = num(x.get("contract_size")).filter(|c| *c > 0.0) {
                    self.contract_size = cs;
                }
                let px = |k: &str| num(x.get(k).and_then(|v| v.get("price"))).map(|p| p / self.contract_size);
                self.info.push(PerpInfo { tw, index: px("reference_price"), mark: px("settlement_mark_price"), funding_rate: None });
            }
            "funding" if ok => {
                self.info.push(PerpInfo {
                    tw,
                    index: None,
                    mark: num(m.get("mark_price")).map(|p| p / self.contract_size),
                    funding_rate: num(m.get("funding_rate")),
                });
            }
            _ => {}
        }
    }
}

/// Coinbase `market_trades` prints and product REST info for BIP-20DEC30-CDE.
#[derive(Clone, Debug)]
pub struct CbdeTrades {
    pub contract_size: f64,
    seen: Seen,
    pub trades: Vec<PerpTrade>,
    pub info: Vec<PerpInfo>,
}

impl Default for CbdeTrades {
    fn default() -> Self {
        Self::new()
    }
}

impl CbdeTrades {
    pub fn new() -> Self {
        Self { contract_size: CBDE_BTC_CONTRACT, seen: Seen::default(), trades: Vec::new(), info: Vec::new() }
    }

    pub fn apply_record(&mut self, raw: &str, tw: i64, meta: Option<&Value>) {
        if meta.and_then(|m| m.get("ctl")).is_some() {
            return; // prints are deduplicated by id, so continuity does not matter here
        }
        let Ok(m) = serde_json::from_str::<Value>(raw) else { return };
        if meta.and_then(|x| x.get("key")).and_then(Value::as_str) == Some("product") {
            if meta.and_then(|x| x.get("status")).and_then(Value::as_u64) != Some(200) {
                return;
            }
            let f = m.get("future_product_details");
            if let Some(cs) = num(f.and_then(|f| f.get("contract_size"))).filter(|c| *c > 0.0) {
                self.contract_size = cs;
            }
            self.info.push(PerpInfo {
                tw,
                index: num(f.and_then(|f| f.get("index_price"))),
                mark: None,
                funding_rate: num(f.and_then(|f| f.get("funding_rate"))),
            });
            return;
        }
        if m.get("channel").and_then(Value::as_str) != Some("market_trades") {
            return;
        }
        for ev in arr(m.get("events")) {
            let snap = ev.get("type").and_then(Value::as_str) == Some("snapshot");
            for t in arr(ev.get("trades")) {
                let Some(id) = t.get("trade_id").and_then(Value::as_str) else { continue };
                if !self.seen.insert(id) || snap {
                    continue;
                }
                let (Some(p), Some(q)) = (num(t.get("price")), num(t.get("size"))) else { continue };
                let taker_buy = match t.get("side").and_then(Value::as_str) {
                    Some("SELL") => Some(true), // maker sold: the taker bought
                    Some("BUY") => Some(false),
                    _ => None,
                };
                self.trades.push(PerpTrade { tw, id: id.to_string(), price: p, qty: q * self.contract_size, taker_buy });
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn ws(k: &mut KalshiPerp, v: Value, tw: i64) -> Result<(), Integrity> {
        k.apply_record(&v.to_string(), tw, None)
    }

    #[test]
    fn kalshi_ws_snapshot_delta_gap_trade_ticker() {
        let mut k = KalshiPerp::new();
        let snap = json!({"type": "orderbook_snapshot", "sid": 1, "seq": 1,
            "msg": {"market_ticker": "KXBTCPERP", "bid": [["5.9931", "195.00"], ["5.9930", "10.00"]], "ask": [["5.9933", "598.00"]]}});
        ws(&mut k, snap.clone(), 10).unwrap();
        let t = k.top().unwrap();
        assert!((t.bid - 59931.0).abs() < 1e-6 && (t.ask - 59933.0).abs() < 1e-6);
        assert!((t.bid_qty - 0.0195).abs() < 1e-12 && (t.ask_qty - 0.0598).abs() < 1e-12);
        let delta = |seq: u64, p: &str, d: &str, side: &str| json!({"type": "orderbook_delta", "sid": 1, "seq": seq, "msg": {"market_ticker": "KXBTCPERP", "price": p, "delta": d, "side": side}});
        ws(&mut k, delta(2, "5.9931", "-195.00", "bid"), 11).unwrap();
        assert!((k.top().unwrap().bid - 59930.0).abs() < 1e-6);
        // the trade sid has its own sequence
        ws(
            &mut k,
            json!({"type": "trade", "sid": 2, "seq": 1, "msg": {"trade_id": "a", "price": "5.9933", "count": "3.00", "taker_side": "bid"}}),
            12,
        )
        .unwrap();
        ws(
            &mut k,
            json!({"type": "trade", "sid": 2, "seq": 3, "msg": {"trade_id": "b", "price": "5.9930", "count": "1.00", "taker_side": "ask"}}),
            13,
        )
        .unwrap();
        assert_eq!(k.trade_gaps, 1);
        assert!(k.valid, "a trade gap does not touch the book");
        assert_eq!(k.trades.iter().map(|t| t.taker_buy).collect::<Vec<_>>(), [Some(true), Some(false)]);
        assert!((k.trades[0].qty - 0.0003).abs() < 1e-12);
        ws(&mut k, json!({"type": "ticker", "sid": 3, "msg": {"reference_price": {"price": "5.9927", "ts_ms": 1}, "settlement_mark_price": {"price": "5.9935", "ts_ms": 1},
            "funding_rate": {"rate": 0.00001, "next_funding_time_ms": 2, "ts_ms": 1}}}), 14).unwrap();
        let i = &k.info[0];
        assert!((i.index.unwrap() - 59927.0).abs() < 1e-6 && (i.mark.unwrap() - 59935.0).abs() < 1e-6 && i.funding_rate == Some(0.00001));
        // a book gap resets until the next snapshot
        assert_eq!(ws(&mut k, delta(4, "5.9930", "1.00", "bid"), 15), Err(Integrity::SeqGap { expected: 3, got: 4 }));
        assert!(k.top().is_none());
        ws(&mut k, delta(5, "5.9930", "1.00", "bid"), 16).unwrap();
        assert!(k.top().is_none(), "deltas before a snapshot are ignored");
        let mut s2 = snap;
        s2["seq"] = json!(6);
        ws(&mut k, s2, 17).unwrap();
        assert!(k.top().is_some());
        // a level below zero resets; a ctl record resets
        ws(&mut k, delta(7, "5.9933", "-999.00", "ask"), 18).unwrap();
        assert!(k.top().is_none() && k.integrity_failures == 2);
        k.apply_record("", 19, Some(&json!({"ctl": "disconnect"}))).unwrap();
        assert!(!k.valid);
    }

    #[test]
    fn kalshi_rest_book_trades_market() {
        let mut k = KalshiPerp::new();
        let m = |key: &str, status: u64| json!({"key": key, "status": status});
        let book = r#"{"orderbook":{"asks":[["6.0000","1.00"],["5.9933","2.00"]],"bids":[["5.9931","4.00"],["5.9000","1.00"]]}}"#;
        k.apply_record(book, 1, Some(&m("orderbook", 200))).unwrap();
        let t = k.top().unwrap();
        assert!((t.ask - 59933.0).abs() < 1e-6 && (t.bid - 59931.0).abs() < 1e-6 && (t.ask_qty - 0.0002).abs() < 1e-12);
        k.apply_record(r#"{"error":"x"}"#, 2, Some(&m("orderbook", 429))).unwrap();
        assert!(k.top().is_none());
        let tr = |ids: &[&str]| {
            let ts: Vec<Value> = ids
                .iter()
                .map(|i| json!({"trade_id": i, "price": "5.9937", "count": "70.00", "taker_side": "bid", "created_time": "x"}))
                .collect();
            json!({"trades": ts, "cursor": ""}).to_string()
        };
        k.apply_record(&tr(&["b", "a"]), 3, Some(&m("trades", 200))).unwrap();
        assert!(k.trades.is_empty(), "the first poll is the baseline");
        k.apply_record(&tr(&["d", "c", "b"]), 4, Some(&m("trades", 200))).unwrap();
        assert_eq!(k.trades.iter().map(|t| t.id.as_str()).collect::<Vec<_>>(), ["c", "d"], "oldest first");
        k.apply_record(&tr(&["f", "e"]), 5, Some(&m("trades", 200))).unwrap();
        assert_eq!(k.trade_overflows, 1);
        let mk = r#"{"market":{"contract_size":"0.000100","reference_price":{"price":"5.9927","ts_ms":1},"settlement_mark_price":{"price":"5.9935","ts_ms":1}}}"#;
        k.apply_record(mk, 6, Some(&m("market", 200))).unwrap();
        k.apply_record(r#"{"funding_rate":0.0001,"mark_price":"5.9828"}"#, 7, Some(&m("funding", 200))).unwrap();
        assert!((k.info[0].index.unwrap() - 59927.0).abs() < 1e-6);
        assert_eq!(k.info[1].funding_rate, Some(0.0001));
        assert!((k.info[1].mark.unwrap() - 59828.0).abs() < 1e-6);
    }

    #[test]
    fn cbde_trades_skip_snapshot_dedupe_and_maker_side() {
        let mut c = CbdeTrades::new();
        let f = |typ: &str, ts: Value| {
            json!({"channel": "market_trades", "sequence_num": 2, "events": [{"type": typ, "trades": ts}]}).to_string()
        };
        let t =
            |id: &str, side: &str| json!({"product_id": "BIP-20DEC30-CDE", "trade_id": id, "price": "59825", "size": "6", "side": side});
        c.apply_record(&f("snapshot", json!([t("1", "SELL")])), 1, None);
        c.apply_record(&f("update", json!([t("1", "SELL"), t("2", "BUY"), t("3", "SELL")])), 2, None);
        assert_eq!(c.trades.iter().map(|x| (x.id.as_str(), x.taker_buy)).collect::<Vec<_>>(), [("2", Some(false)), ("3", Some(true))]);
        assert!((c.trades[0].qty - 0.06).abs() < 1e-12 && c.trades[0].price == 59825.0);
        let prod = r#"{"future_product_details":{"contract_size":"0.01","funding_rate":"0.000005","index_price":"59915.312335"}}"#;
        c.apply_record(prod, 3, Some(&json!({"key": "product", "status": 200})));
        assert_eq!(c.info[0].funding_rate, Some(0.000005));
        assert_eq!(c.info[0].index, Some(59915.312335));
    }
}
