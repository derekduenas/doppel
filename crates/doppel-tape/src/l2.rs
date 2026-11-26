//! Spot L2 order books for the BRTI constituents (`spot_l2_<venue>`), BTC-USD only. Subscriptions
//! and channels are PERELMAN `recorder.py` FEEDS (verified live 2026-11-24); frames are recorded
//! raw and decoded offline by `doppel_fair::venue`. LMAX Digital is not here: it has no public
//! WebSocket (PERELMAN polls its REST book).
//!
//! Continuity, for the venues that send deltas:
//! - Coinbase `sequence_num` and Gemini `socket_sequence` count every frame on the connection; a
//!   jump is recorded as `ctl: seq_gap` and the connection is dropped, so the reconnect brings a
//!   fresh snapshot and a fresh sequence (Gemini's docs prescribe exactly this). `run_ws`'s
//!   backoff rate-limits repeated gaps.
//! - Crypto.com deltas carry `pu` = previous `u`: same handling.
//! - Kraken has no sequence, only a CRC32 of the top 10 levels; it is checked offline by the
//!   decoder (keeping book state out of the recorder).
//! - A backpressure drop leaves a hole in the tape even though the socket is fine, so delta venues
//!   reconnect for a fresh snapshot when `backpressure_drops` moves.

use std::sync::atomic::Ordering;

use async_trait::async_trait;
use serde_json::{json, Value};

use crate::recorder::Recorder;
use crate::ws::{Out, WsVenue};

/// Stream suffixes, in start order.
pub const L2_VENUES: [&str; 6] = ["coinbase", "kraken", "bitstamp", "gemini", "cryptocom", "bullish"];

const BULLISH_PING: &str = r#"{"jsonrpc":"2.0","type":"command","method":"keepalivePing","params":{},"id":"0"}"#;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum V {
    Coinbase,
    Kraken,
    Bitstamp,
    Gemini,
    Cryptocom,
    Bullish,
}

pub struct SpotL2 {
    pub url: String,
    v: V,
    last_seq: Option<u64>,
    drops_seen: u64,
    reconnect: Option<String>,
}

impl SpotL2 {
    /// `venue` is one of [`L2_VENUES`].
    pub fn new(venue: &str) -> Option<Self> {
        let v = match venue {
            "coinbase" => V::Coinbase,
            "kraken" => V::Kraken,
            "bitstamp" => V::Bitstamp,
            "gemini" => V::Gemini,
            "cryptocom" => V::Cryptocom,
            "bullish" => V::Bullish,
            _ => return None,
        };
        let url = match v {
            V::Coinbase => "wss://advanced-trade-ws.coinbase.com",
            V::Kraken => "wss://ws.kraken.com/v2",
            V::Bitstamp => "wss://ws.bitstamp.net",
            V::Gemini => "wss://api.gemini.com/v1/marketdata/BTCUSD?heartbeat=true&trades=false&auctions=false",
            V::Cryptocom => "wss://stream.crypto.com/exchange/v1/market",
            V::Bullish => "wss://api.exchange.bullish.com/trading-api/v1/market-data/orderbook",
        };
        Some(Self { url: url.into(), v, last_seq: None, drops_seen: 0, reconnect: None })
    }

    fn incremental(&self) -> bool {
        matches!(self.v, V::Coinbase | V::Kraken | V::Gemini | V::Cryptocom)
    }

    fn subscriptions(&self) -> Vec<Value> {
        match self.v {
            V::Coinbase => vec![
                json!({"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "level2"}),
                json!({"type": "subscribe", "product_ids": ["BTC-USD"], "channel": "heartbeats"}),
            ],
            V::Kraken => vec![json!({"method": "subscribe", "params": {"channel": "book", "symbol": ["BTC/USD"], "depth": 1000}})],
            V::Bitstamp => vec![json!({"event": "bts:subscribe", "data": {"channel": "order_book_btcusd"}})],
            V::Gemini => vec![], // the URL is the subscription
            V::Cryptocom => vec![
                json!({"id": 1, "method": "subscribe", "params": {"channels": ["book.BTC_USD.150"], "book_subscription_type": "SNAPSHOT_AND_UPDATE"}}),
            ],
            V::Bullish => vec![
                json!({"jsonrpc": "2.0", "type": "command", "method": "subscribe", "params": {"topic": "l2Orderbook", "symbol": "BTCUSD"}, "id": "1"}),
            ],
        }
    }

    fn gap(&mut self, rec: &Recorder, expected: u64, got: u64) {
        rec.stats.incr("seq_gaps");
        rec.ctl("seq_gap", json!({"expected": expected, "got": got}));
        self.reconnect = Some("seq_gap".into());
    }

    /// Per-connection counter that must step by exactly one.
    fn step(&mut self, rec: &Recorder, got: u64) {
        if let Some(prev) = self.last_seq {
            if got != prev + 1 {
                return self.gap(rec, prev + 1, got);
            }
        }
        self.last_seq = Some(got);
    }

    /// Continuity checks and venue protocol replies for one recorded frame.
    pub fn handle(&mut self, raw: &str, rec: &Recorder, out: &mut Out) {
        if self.reconnect.is_some() {
            return; // already leaving this connection
        }
        let Ok(m) = serde_json::from_str::<Value>(raw) else { return };
        match self.v {
            V::Coinbase => {
                if let Some(s) = m.get("sequence_num").and_then(Value::as_u64) {
                    self.step(rec, s);
                }
            }
            V::Gemini => {
                if let Some(s) = m.get("socket_sequence").and_then(Value::as_u64) {
                    self.step(rec, s);
                }
            }
            V::Cryptocom => {
                if m.get("method").and_then(Value::as_str) == Some("public/heartbeat") {
                    out.push(json!({"id": m.get("id"), "method": "public/respond-heartbeat"}).to_string());
                    return;
                }
                let Some(r) = m.get("result") else { return };
                let Some(d) = r.get("data").and_then(|d| d.get(0)) else { return };
                let u = d.get("u").or_else(|| d.get("s")).and_then(Value::as_u64);
                match r.get("channel").and_then(Value::as_str) {
                    Some("book") => self.last_seq = u,
                    Some("book.update") => {
                        if let (Some(prev), Some(pu)) = (self.last_seq, d.get("pu").and_then(Value::as_u64)) {
                            if pu != prev {
                                return self.gap(rec, prev, pu);
                            }
                        }
                        self.last_seq = u;
                    }
                    _ => {}
                }
            }
            V::Bitstamp => {
                if m.get("event").and_then(Value::as_str) == Some("bts:request_reconnect") {
                    self.reconnect = Some("request_reconnect".into());
                }
            }
            V::Kraken | V::Bullish => {}
        }
    }
}

#[async_trait]
impl WsVenue for SpotL2 {
    fn url(&self) -> String {
        self.url.clone()
    }
    fn stall_s(&self) -> f64 {
        match self.v {
            V::Coinbase | V::Kraken => 15.0, // 1 s heartbeats
            V::Cryptocom => 45.0,            // 30 s heartbeats
            _ => 30.0,                       // Gemini: 5 s heartbeats
        }
    }
    fn keepalive(&self) -> Option<(&'static str, f64)> {
        // Bullish has no protocol-level ping/pong and drops idle clients
        (self.v == V::Bullish).then_some((BULLISH_PING, 30.0))
    }
    fn wants_feedback(&self) -> bool {
        matches!(self.v, V::Coinbase | V::Gemini | V::Cryptocom | V::Bitstamp)
    }
    async fn prepare(&mut self, rec: &Recorder) -> bool {
        rec.stats.set_extra("seq_gaps", 0);
        rec.stats.set_extra("resyncs", 0);
        true
    }
    async fn on_open(&mut self, rec: &Recorder, out: &mut Out) -> Result<(), String> {
        self.last_seq = None;
        self.reconnect = None;
        self.drops_seen = rec.stats.backpressure_drops.load(Ordering::Relaxed);
        let subs = self.subscriptions();
        for s in &subs {
            out.push(s.to_string());
        }
        rec.ctl("subscribe", json!({"subs": subs}));
        Ok(())
    }
    async fn on_tick(&mut self, rec: &Recorder, _out: &mut Out) -> Result<(), String> {
        let drops = rec.stats.backpressure_drops.load(Ordering::Relaxed);
        if drops > self.drops_seen && self.incremental() && self.reconnect.is_none() {
            rec.stats.incr("resyncs");
            self.reconnect = Some("backpressure_drop".into());
        }
        self.drops_seen = drops;
        Ok(())
    }
    fn on_feedback(&mut self, raw: &str, rec: &Recorder, out: &mut Out) {
        self.handle(raw, rec, out);
    }
    fn on_feedback_overflow(&mut self, _rec: &Recorder) {
        // frames were recorded but not checked here: start clean
        self.reconnect = Some("feedback_overflow".into());
    }
    fn take_reconnect(&mut self) -> Option<String> {
        self.reconnect.take()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::recorder::spawn_writer;

    fn rec() -> (tempfile::TempDir, Recorder, crate::recorder::WriterThread) {
        let d = tempfile::tempdir().unwrap();
        let (r, w) = spawn_writer(d.path().to_path_buf(), "s", 3, 64).unwrap();
        (d, r, w)
    }

    #[test]
    fn sequence_checks_per_venue() {
        let (_d, r, w) = rec();
        let mut out = Out::new();
        let mut cb = SpotL2::new("coinbase").unwrap();
        for s in [0, 1, 2] {
            cb.handle(&json!({"channel": "heartbeats", "sequence_num": s}).to_string(), &r, &mut out);
        }
        assert!(cb.reconnect.is_none());
        cb.handle(&json!({"channel": "l2_data", "sequence_num": 4}).to_string(), &r, &mut out);
        assert_eq!(cb.take_reconnect().as_deref(), Some("seq_gap"));
        assert_eq!(r.stats.extra("seq_gaps").unwrap(), 1);

        let mut g = SpotL2::new("gemini").unwrap();
        g.handle(r#"{"type":"heartbeat","socket_sequence":0}"#, &r, &mut out);
        g.handle(r#"{"type":"heartbeat","socket_sequence":1}"#, &r, &mut out);
        assert!(g.reconnect.is_none());
        g.handle(r#"{"type":"heartbeat","socket_sequence":1}"#, &r, &mut out);
        assert_eq!(g.take_reconnect().as_deref(), Some("seq_gap"));

        let mut c = SpotL2::new("cryptocom").unwrap();
        let book = |ch: &str, d: Value| json!({"result": {"channel": ch, "data": [d]}}).to_string();
        c.handle(&book("book", json!({"u": 10})), &r, &mut out);
        c.handle(&book("book.update", json!({"u": 12, "pu": 10})), &r, &mut out);
        assert!(c.reconnect.is_none());
        c.handle(r#"{"id":77,"method":"public/heartbeat","code":0}"#, &r, &mut out);
        assert_eq!(std::mem::take(&mut out), [r#"{"id":77,"method":"public/respond-heartbeat"}"#]);
        c.handle(&book("book.update", json!({"u": 14, "pu": 13})), &r, &mut out);
        assert_eq!(c.take_reconnect().as_deref(), Some("seq_gap"));

        let mut b = SpotL2::new("bitstamp").unwrap();
        b.handle(r#"{"event":"bts:request_reconnect","channel":"","data":""}"#, &r, &mut out);
        assert_eq!(b.take_reconnect().as_deref(), Some("request_reconnect"));
        assert!(SpotL2::new("lmax").is_none());
        assert_eq!(r.stats.extra("seq_gaps").unwrap(), 3);
        drop(r);
        w.join();
    }

    #[tokio::test]
    async fn backpressure_drop_resyncs_delta_venues_only() {
        let (_d, r, w) = rec();
        let mut out = Out::new();
        let mut k = SpotL2::new("kraken").unwrap();
        let mut bs = SpotL2::new("bitstamp").unwrap();
        k.on_open(&r, &mut out).await.unwrap();
        bs.on_open(&r, &mut out).await.unwrap();
        assert_eq!(out.len(), 2);
        r.stats.backpressure_drops.fetch_add(1, Ordering::Relaxed);
        k.on_tick(&r, &mut out).await.unwrap();
        bs.on_tick(&r, &mut out).await.unwrap();
        assert_eq!(k.take_reconnect().as_deref(), Some("backpressure_drop"));
        assert_eq!(bs.take_reconnect(), None);
        k.on_tick(&r, &mut out).await.unwrap();
        assert_eq!(k.take_reconnect(), None);
        drop(r);
        w.join();
    }
}
